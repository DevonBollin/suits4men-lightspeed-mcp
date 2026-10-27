"""The write path: master switch, allowed requests, the request hook, the write log, retries.

Every request here goes to a mock (fake_api); the switch and log live in a
temporary folder. Nothing reaches Lightspeed.
"""

from __future__ import annotations

import json

import httpx
import pytest

from lightspeed_mcp.config import STATE_DIR
from lightspeed_mcp.errors import ApiError, WriteBlockedError, WriteOutcomeUnknown, WritesDisabledError
from lightspeed_mcp.reader import InventoryReader
from lightspeed_mcp.write_path import WRITE_ROUTES, WriteClient, WriteLog, WriteSwitch
from tests.conftest import BASE, turn_on
from tests.fakes import long_token

ITEM_BODY = {"description": "Giorgio Fiorelli Navy 42R Slim", "itemMatrixID": "8", "customSku": "013471"}


def created(request):
    return httpx.Response(200, json={"@attributes": {"count": "1"},
                                     "Item": {"itemID": "2001", "itemMatrixID": "8", "customSku": "013471"}})


# ---- the master switch -------------------------------------------------------------------


def test_switch_is_off_until_the_file_exists_and_off_again_when_deleted(write_switch):
    assert not write_switch.is_on()
    with pytest.raises(WritesDisabledError, match="turned OFF"):
        write_switch.require_on()
    turn_on(write_switch)
    assert write_switch.is_on()
    write_switch.require_on()
    write_switch.path.unlink()
    assert not write_switch.is_on()  # read fresh every time: no restart needed


def test_switch_and_log_live_in_state():
    assert WriteSwitch().path == STATE_DIR / "WRITES_ENABLED"
    assert WriteLog().path == STATE_DIR / "write_log.jsonl"


def test_off_message_does_not_explain_how_to_turn_writes_on(write_switch):
    with pytest.raises(WritesDisabledError) as e:
        write_switch.require_on()
    assert "WRITES_ENABLED" not in str(e.value)
    assert "store owner" in str(e.value)


def test_send_with_switch_off_sends_and_logs_nothing(make_write_client, fake_api, write_log):
    client = make_write_client()
    fake_api.handler = created
    with pytest.raises(WritesDisabledError):
        client.send("add_matrix_variants", "POST", "Item.json", ITEM_BODY, "code")
    assert fake_api.requests == []
    assert write_log.entries() == []


def test_request_hook_refuses_even_an_allowed_request_with_switch_off(make_write_client, fake_api):
    """Code reaching past WriteClient.send to its httpx client still cannot write while the switch is off."""
    client = make_write_client()
    fake_api.handler = created
    with pytest.raises(WritesDisabledError):
        client.http.request("POST", BASE + "Item.json", json=ITEM_BODY)
    assert fake_api.requests == []


# ---- only the write tools' own requests ---------------------------------------------------------


def test_exactly_these_write_tools():
    assert set(WRITE_ROUTES) == {"create_matrix", "add_matrix_variants", "update_item_fields", "add_po_lines",
                                 "receive_po_lines", "move_items_into_matrix", "assign_custom_skus", "archive_item"}


def test_the_only_delete_archives_one_item():
    deletes = {tool: r for tool, r in WRITE_ROUTES.items() if r.method == "DELETE"}
    assert list(deletes) == ["archive_item"]
    route = deletes["archive_item"]
    assert route.path.fullmatch("Item/1345.json") and route.fields == frozenset()
    for path in ("Item.json", "ItemMatrix/7.json", "OrderLine/1.json", "Order/853.json", "Item/1345/Image.json"):
        assert not route.path.fullmatch(path)


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "POST"])
def test_no_route_can_change_an_existing_matrix(method):
    """A matrix can be created, never updated or archived: a matrix price save can reach every
    item, and archiving a matrix archives all of its items. That stays manual in Lightspeed."""
    assert not any(r.method == method and r.path.fullmatch("ItemMatrix/7.json") for r in WRITE_ROUTES.values())


def test_prices_and_stock_can_only_be_set_on_new_records():
    for tool, route in WRITE_ROUTES.items():
        assert "ItemShops" not in route.fields
        if "Prices" in route.fields:
            assert route.method == "POST" and tool in ("create_matrix", "add_matrix_variants")
    assert WRITE_ROUTES["update_item_fields"].fields == {"customSku", "defaultCost", "description"}
    assert WRITE_ROUTES["receive_po_lines"].fields == {"numReceived"}
    assert WRITE_ROUTES["assign_custom_skus"].fields == {"customSku"}
    assert WRITE_ROUTES["move_items_into_matrix"].fields == {"itemMatrixID", "ItemAttributes", "description",
                                                             "categoryID", "manufacturerID", "defaultVendorID"}


@pytest.mark.parametrize("tool, method, path", [
    ("api_get", "GET", "Item.json"),                       # not a write tool
    ("delete_item", "DELETE", "Item/1.json"),              # not a write tool
    ("add_po_lines", "DELETE", "OrderLine/1.json"),        # no DELETE, ever
    ("create_matrix", "PUT", "ItemMatrix/7.json"),         # matrices are never updated
    ("create_matrix", "DELETE", "ItemMatrix/7.json"),
    ("update_item_fields", "POST", "Item.json"),           # wrong method for this tool
    ("update_item_fields", "PUT", "ItemShop/5.json"),      # stock
    ("update_item_fields", "PUT", "Item/1.json?x=1"),
    ("add_matrix_variants", "PUT", "Item/1.json"),         # another tool's route
    ("receive_po_lines", "PUT", "Order/853.json"),
    ("receive_po_lines", "POST", "Customer.json"),
    ("archive_item", "DELETE", "ItemMatrix/7.json"),       # never a matrix
    ("archive_item", "DELETE", "Item.json"),
    ("archive_item", "PUT", "Item/1.json"),
    ("assign_custom_skus", "DELETE", "Item/1.json"),       # only archive_item may DELETE
    ("move_items_into_matrix", "PUT", "ItemMatrix/7.json"),
])
def test_requests_outside_a_tools_route_are_refused(make_write_client, fake_api, write_log, write_switch, tool, method, path):
    turn_on(write_switch)  # even with writes on
    client = make_write_client()
    fake_api.handler = created
    with pytest.raises(WriteBlockedError):
        client.send(tool, method, path, {"description": "x"}, "code")
    assert fake_api.requests == []
    assert write_log.entries() == []


@pytest.mark.parametrize("tool, method, path, body", [
    ("update_item_fields", "PUT", "Item/1.json", {"Prices": {"ItemPrice": [{"amount": "1", "useTypeID": "1"}]}}),
    ("update_item_fields", "PUT", "Item/1.json", {"customSku": "013471", "ItemShops": {}}),
    ("update_item_fields", "PUT", "Item/1.json", {"itemMatrixID": "7"}),
    ("add_matrix_variants", "POST", "Item.json", {**ITEM_BODY, "ItemShops": {"ItemShop": {"qoh": "5"}}}),
    ("receive_po_lines", "PUT", "OrderLine/1.json", {"numReceived": "1", "quantity": "9"}),
    ("receive_po_lines", "PUT", "OrderLine/1.json", {}),
    ("assign_custom_skus", "PUT", "Item/1.json", {"customSku": "013471", "description": "x"}),
    ("move_items_into_matrix", "PUT", "Item/1.json", {"itemMatrixID": "7", "Prices": {"ItemPrice": []}}),
    ("move_items_into_matrix", "PUT", "Item/1.json", {"itemMatrixID": "7", "customSku": "013471"}),
    ("archive_item", "DELETE", "Item/1.json", {"archived": "true"}),  # a DELETE has no body
])
def test_fields_outside_a_route_are_refused(make_write_client, fake_api, write_switch, tool, method, path, body):
    turn_on(write_switch)
    client = make_write_client()
    fake_api.handler = created
    with pytest.raises(WriteBlockedError):
        client.send(tool, method, path, body, "code")
    assert fake_api.requests == []


@pytest.mark.parametrize("method, url", [
    ("GET", BASE + "Item.json"),
    ("DELETE", BASE + "ItemMatrix/7.json"),
    ("DELETE", BASE + "OrderLine/1.json"),
    ("PUT", BASE + "ItemMatrix/7.json"),
    ("POST", BASE + "Customer.json"),
    ("POST", "https://evil.example.com/API/V3/Account/12345/Item.json"),
    ("POST", BASE + "Item.json?load_relations=x"),
])
def test_request_hook_blocks_everything_but_write_routes(make_write_client, fake_api, write_switch, method, url):
    turn_on(write_switch)
    client = make_write_client()
    fake_api.handler = created
    with pytest.raises(WriteBlockedError):
        client.http.request(method, url, json={"description": "x"})
    assert fake_api.requests == []


# ---- sending and logging -----------------------------------------------------------------------


def test_a_write_is_sent_once_and_logged_without_the_token(make_write_client, fake_api, write_log, write_switch):
    turn_on(write_switch)
    client = make_write_client()
    fake_api.handler = created

    result = client.send("add_matrix_variants", "POST", "Item.json", ITEM_BODY, "abc123")

    assert result["status"] == 200
    assert result["affected_ids"] == {"Item": {"itemID": "2001", "itemMatrixID": "8"}}
    [request] = fake_api.requests
    assert request.method == "POST" and str(request.url) == BASE + "Item.json"
    assert json.loads(request.content) == ITEM_BODY
    assert request.headers["Authorization"] == "Bearer " + long_token("access", 0)

    sending, done = write_log.entries()
    assert sending["event"] == "sending" and sending["payload"] == ITEM_BODY
    assert sending["path"] == "/API/V3/Account/12345/Item.json" and sending["confirm_code"] == "abc123"
    assert done["event"] == "done" and done["status"] == 200 and done["affected_ids"] == result["affected_ids"]
    assert sending["write_id"] == done["write_id"]
    log_text = write_log.path.read_text(encoding="utf-8")
    assert long_token("access", 0) not in log_text and "Bearer" not in log_text


def test_archiving_sends_a_bodyless_delete_and_logs_the_record_before(make_write_client, fake_api, write_log,
                                                                      write_switch):
    turn_on(write_switch)
    client = make_write_client()
    fake_api.handler = lambda request: httpx.Response(200, json={"Item": {"itemID": "1345", "archived": "true"}})
    before = {"itemID": "1345", "description": "Caravelli Beige 50S Classic", "itemType": "default", "tax": "true"}

    client.send("archive_item", "DELETE", "Item/1345.json", None, "code", before=before)

    [request] = fake_api.requests
    assert request.method == "DELETE" and request.content == b""
    sending, done = write_log.entries()
    assert sending["payload"] is None and sending["before"] == before
    assert done["affected_ids"] == {"Item": {"itemID": "1345"}}


def test_the_log_is_only_appended_to(make_write_client, fake_api, write_log, write_switch):
    turn_on(write_switch)
    client = make_write_client()
    fake_api.handler = created
    client.send("add_matrix_variants", "POST", "Item.json", ITEM_BODY, "one")
    first = write_log.path.read_text(encoding="utf-8")
    client.send("add_matrix_variants", "POST", "Item.json", ITEM_BODY, "two")
    assert write_log.path.read_text(encoding="utf-8").startswith(first)
    assert len(write_log.entries()) == 4


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_never_retried(make_write_client, fake_api, write_log, write_switch, status):
    """The write may have gone through, so sending it again could create a duplicate."""
    turn_on(write_switch)
    client = make_write_client()
    fake_api.handler = lambda request: httpx.Response(status, json={"message": "oops"})
    with pytest.raises(WriteOutcomeUnknown, match="may or may not"):
        client.send("add_matrix_variants", "POST", "Item.json", ITEM_BODY, "code")
    assert len(fake_api.requests) == 1
    assert [e["event"] for e in write_log.entries()] == ["sending", "failed"]


def test_network_errors_are_never_retried(make_write_client, fake_api, write_switch):
    turn_on(write_switch)
    client = make_write_client()

    def broken(request):
        raise httpx.ReadTimeout("timed out", request=request)

    fake_api.handler = broken
    with pytest.raises(WriteOutcomeUnknown):
        client.send("add_matrix_variants", "POST", "Item.json", ITEM_BODY, "code")
    assert len(fake_api.requests) == 1


def test_rate_limited_write_is_retried(make_write_client, fake_api, write_switch):
    """A 429 means Lightspeed did not act, so it is safe to send again."""
    turn_on(write_switch)
    client = make_write_client()
    replies = iter([httpx.Response(429, headers={"Retry-After": "2"}), created(None)])
    fake_api.handler = lambda request: next(replies)
    assert client.send("add_matrix_variants", "POST", "Item.json", ITEM_BODY, "code")["status"] == 200
    assert len(fake_api.requests) == 2


def test_401_refreshes_once_and_retries(make_write_client, fake_api, write_switch, token_server):
    turn_on(write_switch)
    client = make_write_client()

    def api(request):
        if request.headers["Authorization"] == "Bearer " + long_token("access", 0):
            return httpx.Response(401, json={"message": "revoked"})
        return created(request)

    fake_api.handler = api
    assert client.send("add_matrix_variants", "POST", "Item.json", ITEM_BODY, "code")["status"] == 200
    assert len(fake_api.requests) == 2 and len(token_server.calls) == 1


def test_validation_errors_are_reported_and_logged(make_write_client, fake_api, write_log, write_switch):
    turn_on(write_switch)
    client = make_write_client()
    fake_api.handler = lambda request: httpx.Response(422, json={"message": "customSku already exists"})
    with pytest.raises(ApiError, match="customSku already exists") as e:
        client.send("add_matrix_variants", "POST", "Item.json", ITEM_BODY, "code")
    assert e.value.status_code == 422
    assert write_log.entries()[-1]["event"] == "failed" and write_log.entries()[-1]["status"] == 422


# ---- the read side never gets the write path -------------------------------------------------


def test_reader_holds_no_write_client(make_client, make_manager, store, token_server, clock):
    from tests.conftest import seed_tokens
    seed_tokens(store, token_server, expires_at=clock() + 3600)
    client, _ = make_client(make_manager())
    reader = InventoryReader(client)
    for value in list(vars(reader).values()) + list(vars(client).values()):
        assert not isinstance(value, WriteClient)
    with pytest.raises(WriteBlockedError):
        client.http.request("POST", BASE + "Item.json", json=ITEM_BODY)
