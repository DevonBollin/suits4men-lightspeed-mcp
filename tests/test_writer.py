"""The write tools: dry runs, the confirm code, the master switch, and the store's rules.

Reads come from a canned read client (it has no way to send anything but a
read). Writes go through a real WriteClient whose transport is a mock
(fake_api), and the switch lives in a temporary folder. The canned records are
modelled on the store's real data (NOTES.md): matrix 7 "Caravelli" sells at
599.95 although its own Default price is 61.21, attribute set 5 is
Color/Size/Fit, custom SKUs are 6 digits.
"""

from __future__ import annotations

import copy
import json
import re

import httpx
import pytest

from lightspeed_mcp.client import LightspeedClient
from lightspeed_mcp.errors import ApiError, WriteRefused, WritesDisabledError
from lightspeed_mcp.reader import InventoryReader
from lightspeed_mcp.rules import RuleViolation
from lightspeed_mcp.writer import InventoryWriter
from tests.conftest import turn_on

ATTRIBUTE_SETS = [
    {"itemAttributeSetID": "3", "name": "Color", "attributeName1": "Color", "attributeName2": "", "attributeName3": "",
     "system": "true", "archived": "false"},
    {"itemAttributeSetID": "5", "name": "Color/Size/Fit", "attributeName1": "Color", "attributeName2": "Size",
     "attributeName3": "Fit", "system": "false", "archived": "false"},
]
CATEGORIES = [
    {"categoryID": "79", "name": "Pants", "fullPathName": "Pants", "archived": "false"},
    {"categoryID": "82", "name": "Suits", "fullPathName": "Suits", "archived": "false"},
    {"categoryID": "83", "name": "Suit", "fullPathName": "Suits/Suit", "archived": "false"},
    {"categoryID": "84", "name": "Tuxedos", "fullPathName": "Suits/Tuxedos", "archived": "false"},
    {"categoryID": "116", "name": "Standard", "fullPathName": "Dress Shirts, Sweaters, & Vests/Vests/Standard", "archived": "false"},
]
VENDORS = [{"vendorID": "1", "name": "San Malone", "archived": "false"},
           {"vendorID": "21", "name": "Fiorelli Supply", "archived": "false"}]
MANUFACTURERS = [{"manufacturerID": "1", "name": "Caravelli"}, {"manufacturerID": "3", "name": "Giorgio Fiorelli"},
                 {"manufacturerID": "5", "name": "Ascott Brown"}]


def matrix(matrix_id, description, set_id, category, colors=(), sizes=(), fits=(), price="549.95", cost="63.15"):
    return {"itemMatrixID": str(matrix_id), "description": description, "itemAttributeSetID": str(set_id),
            "categoryID": str(category), "defaultVendorID": "1", "manufacturerID": "1", "taxClassID": "1",
            "tax": "true", "itemType": "default", "defaultCost": cost, "archived": "false",
            "Prices": {"ItemPrice": [{"amount": price, "useTypeID": "1", "useType": "Default"}]},
            "attribute1Values": list(colors), "attribute2Values": list(sizes), "attribute3Values": list(fits)}


def item(item_id, description, sku="", matrix_id="0", attrs=None, qoh=0, cost="63.50", archived=False,
         vendor="1", price="599.95", category="83"):
    record = {"itemID": str(item_id), "description": description, "customSku": sku, "itemMatrixID": str(matrix_id),
              "defaultVendorID": vendor, "categoryID": category, "manufacturerID": "1",
              "defaultCost": cost, "archived": "true" if archived else "false",
              "ItemShops": {"ItemShop": [{"shopID": "0", "qoh": str(qoh)}, {"shopID": "1", "qoh": str(qoh)}]},
              "Prices": {"ItemPrice": [{"amount": price, "useTypeID": "1", "useType": "Default"}]}}
    if attrs:
        record["ItemAttributes"] = {"attribute1": attrs[0], "attribute2": attrs[1], "attribute3": attrs[2],
                                    "itemAttributeSetID": "5"}
    return record


def fresh_data() -> dict:
    return {
        "ItemMatrix": [
            matrix(7, "Caravelli", 5, 83, ["Beige", "Black", "Midnight Blue"], ["40R", "42R", "50S"], ["Classic", "Slim"],
                   price="61.21", cost="61.21"),
            matrix(8, "Giorgio Fiorelli", 5, 83, ["Black", "Navy"], [], []),
            matrix(9, "Angelo Rossi", 3, 83, ["Black"]),
            matrix(12, "Caravelli Pants", 5, 79, ["Black"], ["34"], []),
        ],
        "Item": [
            item(1644, "Caravelli Black 42R Slim", "002427", 7, ("Black", "42R", "Slim"), qoh=-27),
            item(1345, "Caravelli Beige 50S Classic", "012911", 7, ("Beige", "50S", "Classic")),
            item(1186, "Caravelli Suits", "", qoh=-16, cost="61.21"),
            item(2000, "Boy's Suit", "009001"),
            item(1700, "Old Caravelli Black 40R Slim", "013000", 7, ("Black", "40R", "Slim"), archived=True),
            item(1801, "Caravelli Pants Black 34", "013100", 12, ("Black", "34", "")),
            item(1346, "Caravelli Black 40R Classic", "013050", 7, ("Black", "40R", "Classic"), qoh=3),
            item(2321, "Caravelli Burgundy 58L Slim", "", category="82"),  # belongs in matrix 7 but isn't
        ],
        "Order": [
            {"orderID": "853", "vendorID": "1", "refNum": "112423", "complete": "false", "archived": "false"},
            {"orderID": "400", "vendorID": "1", "refNum": "9", "complete": "true", "archived": "false"},
            {"orderID": "854", "vendorID": "1", "refNum": "112500", "complete": "false", "archived": "false"},
        ],
        "OrderLine": [
            {"orderLineID": "10", "orderID": "853", "itemID": "1644", "quantity": "3", "numReceived": "1", "checkedIn": "0",
             "price": "65"},
            {"orderLineID": "11", "orderID": "853", "itemID": "1186", "quantity": "2", "numReceived": "0", "checkedIn": "1",
             "price": "61.21"},
            {"orderLineID": "12", "orderID": "854", "itemID": "1801", "quantity": "1", "numReceived": "0", "checkedIn": "0",
             "price": "30"},
        ],
    }


def ids_in(value: str) -> set[str]:
    return set(re.findall(r"\d+", value))


class CannedReads:
    """Stands in for the GET-only read client. It has get and get_all, nothing that could write."""

    record_key = staticmethod(LightspeedClient.record_key)

    def __init__(self):
        self.data = fresh_data()
        self.calls: list[tuple[str, dict]] = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        name, _, record_id = path.removesuffix(".json").partition("/")
        tables = {**self.data, "Vendor": VENDORS, "Manufacturer": MANUFACTURERS}
        key = {"ItemMatrix": "itemMatrixID", "Item": "itemID", "Order": "orderID", "Vendor": "vendorID",
               "Manufacturer": "manufacturerID"}[name]
        for record in tables[name]:
            if record[key] == record_id:
                return {name: copy.deepcopy(record)}
        raise ApiError(404, f"{name} {record_id} not found")

    def get_all(self, path, params=None, max_records=None):
        params = dict(params or {})
        self.calls.append((path, params))
        name = path.removesuffix(".json")
        rows = {"ItemAttributeSet": ATTRIBUTE_SETS, "Category": CATEGORIES, "Vendor": VENDORS,
                "TaxClass": [{"taxClassID": "1", "name": "Item"}], "Shop": [{"shopID": "1", "name": "Suits 4 Men Inc"}],
                "Manufacturer": MANUFACTURERS, **self.data}[name]
        if params.get("archived") != "true":
            rows = [r for r in rows if r.get("archived") != "true"]
        for field in ("itemMatrixID", "customSku", "orderID"):
            if field in params:
                rows = [r for r in rows if r.get(field) == params[field]]
        for field in ("itemID", "vendorID"):
            if field in params:
                rows = [r for r in rows if r.get(field) in ids_in(params[field])]
        return copy.deepcopy(rows), False


@pytest.fixture
def reads():
    return CannedReads()


@pytest.fixture
def writer(reads, make_write_client, fake_api):
    fake_api.handler = lambda request: httpx.Response(200, json={"Item": {"itemID": "2001"}})
    return InventoryWriter(InventoryReader(reads), make_write_client())


GOOD_CALLS = {
    "create_matrix": dict(description="Giorgio Fiorelli Vest", garment_type="vest", category_id=116, vendor_id=21,
                          manufacturer_id=3, default_cost=25, price=89.95),
    "add_matrix_variants": dict(matrix_id=7, price=599.95, cost=63.5, variants=[
        {"color": "Cobalt", "size": "42R", "fit": "Slim", "custom_sku": "013471"},
        {"color": "Black", "size": "44R", "fit": "Classic", "custom_sku": "013472"}]),
    "update_item_fields": dict(item_id=1186, custom_sku="013473", cost=62, description="Caravelli Suit"),
    "add_po_lines": dict(order_id=853, lines=[{"item_id": 1345, "quantity": 2}]),
    "receive_po_lines": dict(order_id=853, lines=[{"order_line_id": 10, "quantity": 2}]),
    "move_items_into_matrix": dict(matrix_id=7, items=[{"item_id": 2321, "color": "Burgundy", "size": "58L",
                                                        "fit": "Slim"}]),
    "assign_custom_skus": dict(items=[{"item_id": 1186}, {"item_id": 2321, "custom_sku": "013200"}]),
    "archive_item": dict(item_id=1345),
}


def call(writer, tool, **overrides):
    return getattr(writer, tool)(**{**GOOD_CALLS[tool], **overrides})


# ---- dry runs, the switch and the confirm code ----------------------------------------------------


@pytest.mark.parametrize("tool", GOOD_CALLS)
@pytest.mark.parametrize("switch_on", [False, True])
def test_dry_run_is_the_default_and_sends_nothing(writer, fake_api, write_log, write_switch, tool, switch_on):
    if switch_on:
        turn_on(write_switch)
    result = call(writer, tool)
    assert result["dry_run"] is True and "Nothing was sent" in result["nothing_sent"]
    assert result["requests"] and all(r["method"] in ("POST", "PUT", "DELETE") for r in result["requests"])
    assert all(r["path"].startswith("/API/V3/Account/12345/") for r in result["requests"])
    assert re.fullmatch(r"[0-9a-f]{10}", result["confirm_code"])
    assert result["writes_enabled"] is switch_on
    assert fake_api.requests == []
    assert write_log.entries() == []


@pytest.mark.parametrize("tool", GOOD_CALLS)
def test_real_call_with_switch_off_is_refused_before_anything_is_read(writer, reads, fake_api, tool):
    code = call(writer, tool)["confirm_code"]
    reads.calls.clear()
    with pytest.raises(WritesDisabledError, match="turned OFF"):
        call(writer, tool, dry_run=False, confirm_code=code)
    assert reads.calls == []
    assert fake_api.requests == []


@pytest.mark.parametrize("tool", GOOD_CALLS)
@pytest.mark.parametrize("code", [None, "", "0000000000"])
def test_real_call_needs_the_dry_runs_confirm_code(writer, fake_api, write_switch, tool, code):
    turn_on(write_switch)
    with pytest.raises(WriteRefused):
        call(writer, tool, dry_run=False, confirm_code=code)
    assert fake_api.requests == []


@pytest.mark.parametrize("tool", GOOD_CALLS)
def test_real_call_sends_exactly_what_the_dry_run_showed(writer, fake_api, write_log, write_switch, tool):
    turn_on(write_switch)
    dry = call(writer, tool)
    result = call(writer, tool, dry_run=False, confirm_code=dry["confirm_code"])

    assert result["finished"] is True and len(result["done"]) == len(dry["requests"])
    sent = [(r.method, r.url.path, json.loads(r.content) if r.content else None) for r in fake_api.requests]
    shown = [(r["method"], r["path"], r["body"]) for r in dry["requests"]]
    assert sent == shown
    sending = [e for e in write_log.entries() if e["event"] == "sending"]
    assert [e["payload"] for e in sending] == [r["body"] for r in dry["requests"]]
    assert all(e["confirm_code"] == dry["confirm_code"] for e in sending)


def test_confirm_code_changes_with_the_arguments(writer):
    assert call(writer, "add_po_lines")["confirm_code"] != call(
        writer, "add_po_lines", lines=[{"item_id": 1345, "quantity": 3}])["confirm_code"]


def test_change_in_lightspeed_since_the_dry_run_is_refused(writer, reads, fake_api, write_switch):
    turn_on(write_switch)
    code = call(writer, "receive_po_lines")["confirm_code"]
    reads.data["OrderLine"][0]["numReceived"] = "0"  # someone changed the line in Lightspeed meanwhile
    # (the new total would now be 2, not the 3 the user approved)
    with pytest.raises(WriteRefused, match="does not match"):
        call(writer, "receive_po_lines", dry_run=False, confirm_code=code)
    assert fake_api.requests == []


def test_stops_at_the_first_failure_and_says_what_was_not_sent(writer, fake_api, write_switch):
    turn_on(write_switch)
    replies = iter([httpx.Response(200, json={"Item": {"itemID": "2001", "itemMatrixID": "7"}}),
                    httpx.Response(422, json={"message": "Validation failed"})])
    fake_api.handler = lambda request: next(replies)
    calls = dict(GOOD_CALLS["add_matrix_variants"], variants=GOOD_CALLS["add_matrix_variants"]["variants"] + [
        {"color": "Beige", "size": "40R", "fit": "Slim", "custom_sku": "013474"}])
    code = writer.add_matrix_variants(**calls)["confirm_code"]
    result = writer.add_matrix_variants(**calls, dry_run=False, confirm_code=code)
    assert result["finished"] is False
    assert len(result["done"]) == 1 and "Validation failed" in result["failed"]["error"]
    assert len(result["not_sent"]) == 1
    assert len(fake_api.requests) == 2


# ---- create_matrix ------------------------------------------------------------------------------


def test_create_matrix_payload(writer):
    [request] = call(writer, "create_matrix")["requests"]
    assert request["method"] == "POST" and request["path"].endswith("/ItemMatrix.json")
    assert request["body"] == {
        "description": "Giorgio Fiorelli Vest", "itemAttributeSetID": "5", "itemType": "default", "categoryID": "116",
        "defaultVendorID": "21", "manufacturerID": "3", "tax": "true", "taxClassID": "1", "defaultCost": "25.00",
        "Prices": {"ItemPrice": [{"amount": "89.95", "useTypeID": "1", "useType": "Default"}]},
    }


def test_create_matrix_sets_no_price_unless_asked(writer):
    result = call(writer, "create_matrix", price=None, default_cost=None)
    body = result["requests"][0]["body"]
    assert "Prices" not in body and "defaultCost" not in body
    assert any("No price" in w for w in result["warnings"])


@pytest.mark.parametrize("overrides, message", [
    (dict(description="Giorgio Fiorelli 320-0S2 Vest"), "vendor code"),
    (dict(description="Giorgio Fiorelli A0000200IX Vest"), "vendor code"),
    (dict(description="Giorgio Fiorelli Navy Vest"), "names the color 'Navy'"),
    (dict(description="Giorgio Fiorelli Midnight Blue Vest"), "names the color"),
    (dict(description="Giorgio Fiorelli Classic"), "does not say it is a vest"),
    (dict(description="Giorgio Fiorelli 3pc Suit", garment_type="suit"), "3-piece"),
    (dict(description="Giorgio Fiorelli Three Piece Suit", garment_type="suit"), "3-piece"),
    (dict(description="Giorgio Fiorelli Suit", garment_type="3pc suit"), "3-piece"),
    (dict(description="Giorgio Fiorelli Boys Vest"), "boys"),
    (dict(description="Giorgio Fiorelli Shirt", garment_type="shirt"), "not one of"),
    (dict(description="Caravelli Pants", garment_type="pants", category_id=79), "already called"),
    (dict(category_id=999), "category 999"),
    (dict(vendor_id=999), "vendor 999"),
    (dict(price=0), "more than 0"),
    (dict(description=""), "empty"),
])
def test_create_matrix_rules(writer, fake_api, overrides, message):
    with pytest.raises(RuleViolation, match=message):
        call(writer, "create_matrix", **overrides)
    assert fake_api.requests == []


def test_brand_names_with_color_words_are_not_a_color_split(writer):
    result = call(writer, "create_matrix", description="Ascott Brown Pants", garment_type="pants", category_id=79,
                  manufacturer_id=5)
    assert result["requests"][0]["body"]["description"] == "Ascott Brown Pants"


def test_create_matrix_always_uses_attribute_set_5(writer):
    for garment, description, category in (("suit", "Giorgio Fiorelli Suit", 83), ("tuxedo", "Giorgio Fiorelli Tux", 84),
                                            ("pants", "Giorgio Fiorelli Pants", 79)):
        result = call(writer, "create_matrix", description=description, garment_type=garment, category_id=category)
        assert result["requests"][0]["body"]["itemAttributeSetID"] == "5"


# ---- add_matrix_variants ---------------------------------------------------------------------------


def test_variant_payload_uses_the_given_price_never_the_matrix_price(writer):
    result = call(writer, "add_matrix_variants")
    first = result["requests"][0]["body"]
    assert first == {
        "description": "Caravelli Cobalt 42R Slim", "itemMatrixID": "7", "itemType": "default", "customSku": "013471",
        "defaultCost": "63.50",
        "ItemAttributes": {"itemAttributeSetID": "5", "attribute1": "Cobalt", "attribute2": "42R", "attribute3": "Slim"},
        "Prices": {"ItemPrice": [{"amount": "599.95", "useTypeID": "1", "useType": "Default"}]},
        "categoryID": "83", "manufacturerID": "1", "defaultVendorID": "1", "taxClassID": "1", "tax": "true",
    }
    assert "61.21" not in json.dumps(result["requests"])  # matrix 7's own price (= its cost) is never copied
    assert any("New color" in w and "Cobalt" in w for w in result["warnings"])


def test_vendor_naming_is_kept_exactly(writer):
    result = call(writer, "add_matrix_variants", variants=[
        {"color": "  Lt. Grey Sharkskin ", "size": "42R", "fit": "Modern-Slim", "custom_sku": "013471"}])
    attrs = result["requests"][0]["body"]["ItemAttributes"]
    assert attrs["attribute1"] == "Lt. Grey Sharkskin" and attrs["attribute3"] == "Modern-Slim"


def test_pants_take_waist_sizes_and_may_have_no_fit(writer):
    result = call(writer, "add_matrix_variants", matrix_id=12, price=129.95, cost=30, variants=[
        {"color": "Black", "size": "36", "custom_sku": "013471"}])
    body = result["requests"][0]["body"]
    assert body["description"] == "Caravelli Pants Black 36"
    assert "attribute3" not in body["ItemAttributes"]


@pytest.mark.parametrize("overrides, message", [
    (dict(matrix_id=9), "attribute set 3"),
    (dict(matrix_id=404), "not found"),
    (dict(matrix_id=12, variants=[{"color": "Black", "size": "34x32", "custom_sku": "013471"}]), "waist only"),
    (dict(matrix_id=12, variants=[{"color": "Black", "size": "34R", "custom_sku": "013471"}]), "waist only"),
    (dict(variants=[{"color": "Navy", "size": "42R", "fit": "Slim", "custom_sku": "002427"}]), "already in use"),
    (dict(variants=[{"color": "Navy", "size": "42R", "fit": "Slim", "custom_sku": "013000"}]), "already in use"),
    (dict(variants=[{"color": "Navy", "size": "42R", "fit": "Slim", "custom_sku": "12345"}]), "6-digit"),
    (dict(variants=[{"color": "Navy", "size": "42R", "fit": "Slim", "custom_sku": "01347A"}]), "6-digit"),
    (dict(variants=[{"color": "Navy", "size": "42R", "fit": "Slim", "custom_sku": "013471"},
                    {"color": "Navy", "size": "44R", "fit": "Slim", "custom_sku": "013471"}]), "used twice"),
    (dict(variants=[{"color": "Black", "size": "42R", "fit": "Slim", "custom_sku": "013471"}]), "already has an item"),
    (dict(variants=[{"color": "Black", "size": "40R", "fit": "Slim", "custom_sku": "013471"}]), "archived item"),
    (dict(variants=[{"color": "black", "size": "44R", "fit": "Slim", "custom_sku": "013471"}]), "only in capitals"),
    (dict(variants=[{"color": "Navy", "size": "42R", "fit": "Slim", "custom_sku": "013471"},
                    {"color": "Navy", "size": "42r", "fit": "slim", "custom_sku": "013472"}]), "listed twice"),
    (dict(variants=[{"color": "Navy", "size": "42R", "fit": "3pc Slim", "custom_sku": "013471"}]), "3-piece"),
    (dict(variants=[{"color": "Navy", "size": "Boys 12", "fit": "Slim", "custom_sku": "013471"}]), "boys"),
    (dict(variants=[{"color": "", "size": "42R", "fit": "Slim", "custom_sku": "013471"}]), "needs a color"),
    (dict(variants=[]), "at least one"),
    (dict(price=0), "more than 0"),
])
def test_variant_rules(writer, fake_api, overrides, message):
    with pytest.raises(RuleViolation, match=message):
        call(writer, "add_matrix_variants", **overrides)
    assert fake_api.requests == []


# ---- update_item_fields -------------------------------------------------------------------------


def test_update_sends_only_the_changed_fields_and_never_prices(writer):
    result = call(writer, "update_item_fields", custom_sku=None, cost=61.21, description="Caravelli Suit")
    [request] = result["requests"]
    assert request["method"] == "PUT" and request["path"].endswith("/Item/1186.json")
    assert request["body"] == {"description": "Caravelli Suit"}  # cost is unchanged, so not sent
    assert "Prices" not in json.dumps(result)


def test_update_all_three_fields(writer):
    [request] = call(writer, "update_item_fields")["requests"]
    assert request["body"] == {"customSku": "013473", "defaultCost": "62.00", "description": "Caravelli Suit"}


@pytest.mark.parametrize("overrides, message", [
    (dict(custom_sku="012911", cost=None, description=None), "already used by item 1345"),
    (dict(custom_sku="013000", cost=None, description=None), "already used by item 1700"),  # archived item
    (dict(custom_sku="12911", cost=None, description=None), "6-digit"),
    (dict(custom_sku=None, cost=None, description="320-0S2 Suit Grey 62R Classic"), "vendor code"),
    (dict(custom_sku=None, cost=None, description="Caravelli 3 pc Suit"), "3-piece"),
    (dict(custom_sku=None, cost=-5, description=None), "more than 0"),
    (dict(custom_sku=None, cost=None, description=None), "at least one"),
    (dict(custom_sku=None, cost=61.21, description="Caravelli Suits"), "nothing to change"),
    (dict(item_id=99999), "not found"),
])
def test_update_rules(writer, fake_api, overrides, message):
    with pytest.raises(RuleViolation, match=message):
        call(writer, "update_item_fields", **overrides)
    assert fake_api.requests == []


def test_keeping_an_items_own_sku_is_not_a_duplicate(writer):
    result = call(writer, "update_item_fields", item_id=1345, custom_sku="012911", cost=70, description=None)
    assert result["requests"][0]["body"] == {"defaultCost": "70.00"}


# ---- add_po_lines -------------------------------------------------------------------------------------


def test_po_line_payload_defaults_to_the_items_cost(writer):
    result = call(writer, "add_po_lines")
    assert result["requests"][0]["body"] == {"orderID": "853", "itemID": "1345", "quantity": "2", "price": "63.50",
                                             "originalPrice": "63.50", "numReceived": "0"}


def test_po_line_warnings(writer):
    result = call(writer, "add_po_lines", lines=[{"item_id": 1801, "quantity": 1, "unit_cost": 30}])
    assert result["requests"][0]["body"]["price"] == "30.00"
    result = writer.add_po_lines(853, [{"item_id": 1345, "quantity": 1}])
    assert not result.get("warnings")


@pytest.mark.parametrize("overrides, message", [
    (dict(order_id=400), "finished"),
    (dict(order_id=404), "not found"),
    (dict(lines=[{"item_id": 1644, "quantity": 1}]), "already on PO 853"),
    (dict(lines=[{"item_id": 2000, "quantity": 1}]), "boys"),
    (dict(lines=[{"item_id": 1700, "quantity": 1}]), "archived"),
    (dict(lines=[{"item_id": 1345, "quantity": 0}]), "at least 1"),
    (dict(lines=[{"item_id": 1345, "quantity": 1.5}]), "whole number"),
    (dict(lines=[{"item_id": 1345, "quantity": 1}, {"item_id": 1345, "quantity": 2}]), "listed twice"),
    (dict(lines=[]), "at least one"),
])
def test_po_line_rules(writer, fake_api, overrides, message):
    with pytest.raises((RuleViolation, WriteRefused), match=message):
        call(writer, "add_po_lines", **overrides)
    assert fake_api.requests == []


# ---- receive_po_lines ---------------------------------------------------------------------------------


def test_receiving_sets_the_new_running_total_only(writer):
    result = call(writer, "receive_po_lines")
    [request] = result["requests"]
    assert request["method"] == "PUT" and request["path"].endswith("/OrderLine/10.json")
    assert request["body"] == {"numReceived": "3"}  # 1 already received + 2 now
    assert "Add Received To Inventory" in result["after"]
    assert any("negative stock (-27)" in w for w in result["warnings"])


def test_receive_by_item_id(writer):
    result = call(writer, "receive_po_lines", lines=[{"item_id": 1186, "quantity": 1}])
    assert result["requests"][0]["path"].endswith("/OrderLine/11.json")
    assert result["requests"][0]["body"] == {"numReceived": "1"}


@pytest.mark.parametrize("overrides, message", [
    (dict(lines=[{"order_line_id": 10, "quantity": 3}]), "more than ordered"),       # 1 + 3 > 3
    (dict(lines=[{"order_line_id": 11, "quantity": 2}]), "more than ordered"),       # 1 checked in + 2 > 2
    (dict(lines=[{"order_line_id": 99, "quantity": 1}]), "not on purchase order"),
    (dict(lines=[{"item_id": 1345, "quantity": 1}]), "on 0 lines"),
    (dict(lines=[{"order_line_id": 10, "quantity": 1}, {"item_id": 1644, "quantity": 1}]), "listed twice"),
    (dict(lines=[{"quantity": 1}]), "needs order_line_id or item_id"),
    (dict(order_id=400), "finished"),
])
def test_receive_rules(writer, fake_api, overrides, message):
    with pytest.raises((RuleViolation, WriteRefused), match=message):
        call(writer, "receive_po_lines", **overrides)
    assert fake_api.requests == []


# ---- move_items_into_matrix -------------------------------------------------------------------------


def test_move_payload_sets_matrix_attributes_and_matches_the_matrix(writer):
    result = call(writer, "move_items_into_matrix")
    [request] = result["requests"]
    assert request["method"] == "PUT" and request["path"].endswith("/Item/2321.json")
    # The description already follows the pattern and vendor/manufacturer match, so only the category is aligned.
    assert request["body"] == {
        "itemMatrixID": "7",
        "ItemAttributes": {"itemAttributeSetID": "5", "attribute1": "Burgundy", "attribute2": "58L", "attribute3": "Slim"},
        "categoryID": "83",
    }
    warnings = " ".join(result["warnings"])
    assert "New color" in warnings and "Burgundy" in warnings
    assert "no custom SKU" in warnings
    assert "sells at 599.95" in warnings and "61.21" in warnings  # the matrix's own price is flagged, never sent
    assert "do not describe moving" in result["after"]


def test_move_can_change_only_the_matrix_and_attributes(writer):
    result = call(writer, "move_items_into_matrix", only_matrix_and_attributes=True)
    assert set(result["requests"][0]["body"]) == {"itemMatrixID", "ItemAttributes"}


def test_move_never_sends_prices_cost_sku_or_stock(writer):
    body = call(writer, "move_items_into_matrix")["requests"][0]["body"]
    assert not {"Prices", "defaultCost", "customSku", "ItemShops"} & set(body)


def test_move_out_of_another_matrix_is_flagged(writer):
    result = call(writer, "move_items_into_matrix", items=[{"item_id": 1801, "color": "Black", "size": "36"}])
    assert any("moves out of matrix 12" in w for w in result["warnings"])
    body = result["requests"][0]["body"]
    assert body["description"] == "Caravelli Black 36" and "attribute3" not in body["ItemAttributes"]


def test_first_move_is_one_item_until_a_move_has_been_done(writer, write_log):
    two = [{"item_id": 2321, "color": "Burgundy", "size": "58L", "fit": "Slim"},
           {"item_id": 1801, "color": "Black", "size": "36"}]
    with pytest.raises(RuleViolation, match="Move one item first"):
        call(writer, "move_items_into_matrix", items=two)
    write_log.append({"write_id": "x", "tool": "move_items_into_matrix", "event": "done", "status": 200})
    assert call(writer, "move_items_into_matrix", items=two)["request_count"] == 2


@pytest.mark.parametrize("overrides, message", [
    (dict(matrix_id=9), "attribute set 3"),
    (dict(matrix_id=404), "not found"),
    (dict(items=[{"item_id": 1644, "color": "Black", "size": "42R", "fit": "Slim"}]), "already in matrix 7"),
    (dict(items=[{"item_id": 2321, "color": "Black", "size": "42R", "fit": "Slim"}]), "already has an item"),
    (dict(items=[{"item_id": 2321, "color": "burgundy", "size": "40R", "fit": "slim"}]), "only in capitals"),
    (dict(items=[{"item_id": 2000, "color": "Navy", "size": "12", "fit": ""}]), "boys"),
    (dict(items=[{"item_id": 1700, "color": "Navy", "size": "42R", "fit": "Slim"}]), "archived"),
    (dict(items=[{"item_id": 99999, "color": "Navy", "size": "42R", "fit": "Slim"}]), "not found"),
    (dict(matrix_id=12, items=[{"item_id": 2321, "color": "Black", "size": "34x32"}]), "waist only"),
    (dict(items=[{"item_id": 2321, "color": "", "size": "58L"}]), "needs a color"),
    (dict(items=[]), "at least one"),
])
def test_move_rules(writer, fake_api, overrides, message):
    with pytest.raises(RuleViolation, match=message):
        call(writer, "move_items_into_matrix", **overrides)
    assert fake_api.requests == []


# ---- assign_custom_skus ---------------------------------------------------------------------------------


def test_batch_skus_fill_in_given_and_next_free(writer):
    result = call(writer, "assign_custom_skus")
    bodies = {r["path"].rsplit("/", 1)[1]: r["body"] for r in result["requests"]}
    # Highest in use is 013100 (archived items count), so the next free is 013101.
    assert bodies == {"1186.json": {"customSku": "013101"}, "2321.json": {"customSku": "013200"}}
    assert "1 picked automatically" in result["summary"] and "013100" in result["summary"]


def test_batch_skus_skip_numbers_given_in_the_same_request(writer):
    result = call(writer, "assign_custom_skus", items=[{"item_id": 2321, "custom_sku": "013101"}, {"item_id": 1186}])
    assert [r["body"]["customSku"] for r in result["requests"]] == ["013101", "013102"]


@pytest.mark.parametrize("items, message", [
    ([{"item_id": 1345}], "already has a SKU"),
    ([{"item_id": 1700}], "archived"),
    ([{"item_id": 2321, "custom_sku": "012911"}], "already used by item 1345"),
    ([{"item_id": 2321, "custom_sku": "013000"}], "already used by item 1700"),  # archived item's SKU
    ([{"item_id": 2321, "custom_sku": "12345"}], "6-digit"),
    ([{"item_id": 2321, "custom_sku": "013300"}, {"item_id": 1186, "custom_sku": "013300"}], "used twice"),
    ([{"item_id": 2321}, {"item_id": 2321}], "listed twice"),
    ([{"item_id": 99999}], "not found"),
    ([], "at least one"),
])
def test_batch_sku_rules(writer, fake_api, items, message):
    with pytest.raises(RuleViolation, match=message):
        call(writer, "assign_custom_skus", items=items)
    assert fake_api.requests == []


# ---- archive_item ---------------------------------------------------------------------------------------


def test_archive_dry_run(writer):
    result = call(writer, "archive_item")
    [request] = result["requests"]
    assert request["method"] == "DELETE" and request["path"].endswith("/Item/1345.json")
    assert request["body"] is None
    assert any("non_inventory" in w for w in result["warnings"])
    assert any("matrix 7" in w and "not touched" in w for w in result["warnings"])


def test_archive_logs_the_item_as_it_was(writer, fake_api, write_log, write_switch):
    turn_on(write_switch)
    code = call(writer, "archive_item")["confirm_code"]
    call(writer, "archive_item", dry_run=False, confirm_code=code)
    [request] = fake_api.requests
    assert request.method == "DELETE" and request.content == b""
    before = write_log.entries()[0]["before"]
    assert before["description"] == "Caravelli Beige 50S Classic" and before["customSku"] == "012911"
    assert before["ItemAttributes"]["attribute1"] == "Beige" and before["Prices"]


def test_archive_refused_if_the_item_changed_since_the_dry_run(writer, reads, fake_api, write_switch):
    turn_on(write_switch)
    code = call(writer, "archive_item")["confirm_code"]
    next(i for i in reads.data["Item"] if i["itemID"] == "1345")["timeStamp"] = "2026-10-27T10:00:00+00:00"
    with pytest.raises(WriteRefused, match="does not match"):
        call(writer, "archive_item", dry_run=False, confirm_code=code)
    assert fake_api.requests == []


@pytest.mark.parametrize("item_id, message", [
    (1346, "has stock 3"),        # archiving would zero it, and that can't be undone
    (1644, "has stock -27"),      # negative stock too
    (1801, "open purchase order"),
    (1700, "already archived"),
    (99999, "not found"),
])
def test_archive_rules(writer, fake_api, item_id, message):
    with pytest.raises(RuleViolation, match=message):
        call(writer, "archive_item", item_id=item_id)
    assert fake_api.requests == []
