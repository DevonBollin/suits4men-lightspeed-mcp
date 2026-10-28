"""401 retry, rate limiting, pagination and the GET-only guard."""

from __future__ import annotations

import httpx
import pytest

from lightspeed_mcp.client import LightspeedClient
from lightspeed_mcp.errors import ApiError, AuthError, RateLimitedError, WriteBlockedError
from tests.conftest import BASE, seed_tokens
from tests.fakes import long_token


def bearer(request: httpx.Request) -> str:
    return request.headers["Authorization"].removeprefix("Bearer ")


@pytest.fixture
def client_with_valid_token(store, token_server, make_manager, make_client, clock):
    seed_tokens(store, token_server, expires_at=clock() + 3600)
    return make_client(make_manager())


# ---- 401 retry ------------------------------------------------------------------


def test_401_refreshes_once_and_retries(store, token_server, make_manager, make_client, fake_api, clock):
    seed_tokens(store, token_server, expires_at=clock() + 3600)  # looks valid, but Lightspeed revoked it

    def api(request):
        if bearer(request) == long_token("access", 0):
            return httpx.Response(401, json={"message": "Access token has been revoked"})
        return httpx.Response(200, json={"Item": {"itemID": "1"}})

    fake_api.handler = api
    client, _ = make_client(make_manager())
    assert client.get("Item/1.json") == {"Item": {"itemID": "1"}}
    assert len(fake_api.requests) == 2
    assert len(token_server.calls) == 1


def test_second_401_gives_a_clear_error_instead_of_looping(store, token_server, make_manager, make_client, fake_api, clock):
    seed_tokens(store, token_server, expires_at=clock() + 3600)
    fake_api.handler = lambda request: httpx.Response(401, json={"message": "Access token could not be verified"})
    client, _ = make_client(make_manager())

    with pytest.raises(AuthError, match="bootstrap.py"):
        client.get("Item.json")
    assert len(fake_api.requests) == 2  # original + exactly one retry
    assert len(token_server.calls) == 1


# ---- rate limiting -------------------------------------------------------------------


def test_429_waits_for_retry_after_then_succeeds(client_with_valid_token, fake_api):
    client, sleeps = client_with_valid_token
    replies = iter([
        httpx.Response(429, headers={"Retry-After": "3", "X-LS-API-RateLimit-Type": "api-leaky-bucket",
                                     "X-LS-API-Bucket-Level": "92.5/90"}),
        httpx.Response(200, json={"Item": []}, headers={"X-LS-API-Bucket-Level": "60/90"}),
    ])
    fake_api.handler = lambda request: next(replies)

    assert client.get("Item.json") == {"Item": []}
    assert 3.0 in sleeps
    assert len(fake_api.requests) == 2


def test_burst_limit_429_without_retry_after_waits_one_second(client_with_valid_token, fake_api):
    client, sleeps = client_with_valid_token
    replies = iter([
        httpx.Response(429, headers={"X-LS-API-RateLimit-Type": "api-burst-limiter"}),
        httpx.Response(200, json={"Item": []}),
    ])
    fake_api.handler = lambda request: next(replies)
    client.get("Item.json")
    assert 1.0 in sleeps


def test_429_gives_up_after_max_retries(client_with_valid_token, fake_api):
    client, sleeps = client_with_valid_token
    fake_api.handler = lambda request: httpx.Response(429, headers={"Retry-After": "1"})
    with pytest.raises(RateLimitedError):
        client.get("Item.json")
    assert len(fake_api.requests) == client.max_rate_limit_retries + 1


def test_pauses_before_overrunning_the_bucket(client_with_valid_token, fake_api):
    """After a response says 85/90 used, the next request waits for the bucket to drain."""
    client, sleeps = client_with_valid_token
    fake_api.handler = lambda request: httpx.Response(
        200, json={"Item": []}, headers={"X-LS-API-Bucket-Level": "85/90", "X-LS-API-Drip-Rate": "1"})

    client.get("Item.json")
    assert sleeps == []  # bucket was empty
    client.get("Item.json")
    # 85 used + 1 for this request must come down to 90 - 10 headroom = 80: wait 6s at 1 drip/s.
    assert sleeps == [6.0]


def test_no_pause_when_bucket_has_room(client_with_valid_token, fake_api):
    client, sleeps = client_with_valid_token
    fake_api.handler = lambda request: httpx.Response(
        200, json={"Item": []}, headers={"X-LS-API-Bucket-Level": "10/90", "X-LS-API-Drip-Rate": "1"})
    for _ in range(5):
        client.get("Item.json")
    assert sleeps == []


# ---- pagination ------------------------------------------------------------------------


def page(records, next_url=""):
    return httpx.Response(200, json={"@attributes": {"next": next_url, "previous": ""}, "Item": records})


def test_pagination_follows_next_links_until_empty(client_with_valid_token, fake_api):
    client, _ = client_with_valid_token
    next1 = BASE + "Item.json?limit=100&after=WzEwMF0%3D"
    next2 = BASE + "Item.json?limit=100&after=WzIwMF0%3D"
    pages = {
        BASE + "Item.json?description=~%2C%25navy%25&limit=100": page([{"itemID": "1"}, {"itemID": "2"}], next1),
        next1: page([{"itemID": "3"}], next2),
        next2: page({"itemID": "4"}),  # last page holds one record, sent as an object
    }
    fake_api.handler = lambda request: pages[str(request.url)]

    records, truncated = client.get_all("Item.json", {"description": "~,%navy%"})
    assert [r["itemID"] for r in records] == ["1", "2", "3", "4"]
    assert truncated is False
    assert [str(r.url) for r in fake_api.requests][1:] == [next1, next2]


def test_pagination_with_no_results(client_with_valid_token, fake_api):
    client, _ = client_with_valid_token
    fake_api.handler = lambda request: httpx.Response(200, json={"@attributes": {"next": "", "previous": ""}})
    assert client.get_all("Item.json") == ([], False)


def test_pagination_stops_at_max_records(client_with_valid_token, fake_api):
    client, _ = client_with_valid_token
    fake_api.handler = lambda request: page([{"itemID": str(i)} for i in range(5)], BASE + "Item.json?after=x")
    records, truncated = client.get_all("Item.json", max_records=5)
    assert len(records) == 5 and truncated is True
    assert len(fake_api.requests) == 1
    assert fake_api.requests[0].url.params["limit"] == "5"


def test_pagination_refuses_a_next_link_to_another_host(client_with_valid_token, fake_api):
    client, _ = client_with_valid_token
    fake_api.handler = lambda request: page([{"itemID": "1"}], "https://evil.example.com/API/V3/Account/12345/Item.json?after=x")
    with pytest.raises(ApiError, match="outside this account"):
        client.get_all("Item.json")
    assert len(fake_api.requests) == 1  # the token never went to the other host


def test_record_key():
    assert LightspeedClient.record_key("Item.json") == "Item"
    assert LightspeedClient.record_key("Item/123.json") == "Item"
    assert LightspeedClient.record_key("ItemMatrix") == "ItemMatrix"


# ---- GET-only guard -----------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post"])
def test_client_refuses_every_method_but_get(client_with_valid_token, fake_api, method):
    client, _ = client_with_valid_token
    fake_api.handler = lambda request: httpx.Response(200, json={})
    with pytest.raises(WriteBlockedError):
        client.request(method, "Item/1.json")
    assert fake_api.requests == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_underlying_http_client_also_blocks_writes(client_with_valid_token, fake_api, method):
    """Even code that reaches past LightspeedClient to its httpx client cannot send a write."""
    client, _ = client_with_valid_token
    fake_api.handler = lambda request: httpx.Response(200, json={})
    with pytest.raises(WriteBlockedError):
        client.http.request(method, BASE + "Item/1.json", json={"description": "x"})
    assert fake_api.requests == []


def test_client_has_no_write_helpers():
    for name in ("post", "put", "patch", "delete"):
        assert not hasattr(LightspeedClient, name)


@pytest.mark.parametrize("bad", [
    "https://evil.example.com/Item.json", "../Account.json", "Item.json?x=1", "Account/999/Item.json", "",
])
def test_paths_outside_the_account_are_rejected(client_with_valid_token, bad):
    client, _ = client_with_valid_token
    with pytest.raises(ValueError):
        client.url_for(bad)


def test_paths_are_normalised(client_with_valid_token):
    client, _ = client_with_valid_token
    assert client.url_for("Item") == BASE + "Item.json"
    assert client.url_for("/Item/5.json") == BASE + "Item/5.json"
    assert client.url_for("API/V3/Account/12345/Vendor.json") == BASE + "Vendor.json"


def test_http_errors_carry_lightspeed_message(client_with_valid_token, fake_api):
    client, _ = client_with_valid_token
    fake_api.handler = lambda request: httpx.Response(404, json={"httpCode": "404", "message": "Item not found."})
    with pytest.raises(ApiError, match="Item not found") as e:
        client.get("Item/999.json")
    assert e.value.status_code == 404


# ---- certificates: the Windows certificate store, never certifi alone ---------------------------------


def test_https_is_checked_against_the_windows_certificate_store():
    """Norton 360 on the store's computer re-signs HTTPS with a root only Windows trusts."""
    import truststore

    from lightspeed_mcp.client import make_api_http, make_http, tls_context

    assert isinstance(tls_context(), truststore.SSLContext)
    for client in (make_http(), make_api_http()):
        with client:
            assert client._transport._pool._ssl_context.__class__ is truststore.SSLContext


def test_every_httpx_client_in_the_project_gets_the_windows_store():
    """Every httpx.Client(...) outside the tests passes verify=tls_context()."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    files = [root / name for name in ("server.py", "bootstrap.py", "backup_lightspeed.py", "check_tools.py")]
    files += sorted((root / "lightspeed_mcp").glob("*.py"))
    found = 0
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and ast.unparse(node.func) in ("httpx.Client", "httpx.AsyncClient"):
                found += 1
                verify = next((k.value for k in node.keywords if k.arg == "verify"), None)
                assert verify is not None and ast.unparse(verify) == "tls_context()", f"{path.name}:{node.lineno}"
    assert found == 3          # make_http, make_api_http, make_write_http
