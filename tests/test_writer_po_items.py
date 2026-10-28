"""Standalone items, purchase orders and PO lines: payloads, rules, and "{new:...}" references.

Same setup as test_writer.py: canned reads, a mocked write transport, the switch in a temp folder.
"""

from __future__ import annotations

import json

import httpx
import pytest

from lightspeed_mcp.errors import WriteRefused
from lightspeed_mcp.reader import InventoryReader
from lightspeed_mcp.rules import RuleViolation
from lightspeed_mcp.writer import InventoryWriter, Plan, PlannedRequest, fill_in_new_ids, new_ref
from tests.conftest import turn_on
from tests.test_writer import CannedReads


@pytest.fixture
def reads():
    return CannedReads()


@pytest.fixture
def writer(reads, make_write_client, fake_api):
    fake_api.handler = lambda request: httpx.Response(200, json={"Item": {"itemID": "2001"}})
    return InventoryWriter(InventoryReader(reads), make_write_client())


SHIRT = {"description": "Dress Shirt White 15.5 32/33", "category_id": 116, "vendor_id": 21, "cost": 18,
         "price": 59.95}


# ---- create_items ---------------------------------------------------------------------------------


def test_standalone_item_payload_gets_the_next_free_sku(writer):
    result = writer.create_items([SHIRT])
    [request] = result["requests"]
    assert request["method"] == "POST" and request["path"].endswith("/Item.json")
    assert request["body"] == {
        "description": "Dress Shirt White 15.5 32/33", "itemType": "default", "categoryID": "116",
        "defaultVendorID": "21", "tax": "true", "taxClassID": "1", "customSku": "013101", "defaultCost": "18.00",
        "Prices": {"ItemPrice": [{"amount": "59.95", "useTypeID": "1", "useType": "Default"}]},
    }
    assert "itemMatrixID" not in request["body"] and "ItemAttributes" not in request["body"]


def test_standalone_items_get_distinct_skus_and_keep_given_ones(writer):
    result = writer.create_items([SHIRT, {**SHIRT, "description": "Dress Shirt Blue 16 34/35", "custom_sku": "013102"}])
    assert [r["body"]["customSku"] for r in result["requests"]] == ["013101", "013102"]
    result = writer.create_items([{**SHIRT, "custom_sku": "013101"}, {**SHIRT, "description": "Dress Shirt Pink 15"}])
    assert [r["body"]["customSku"] for r in result["requests"]] == ["013101", "013102"]


def test_suit_like_standalone_items_are_flagged(writer):
    result = writer.create_items([{**SHIRT, "description": "Caravelli Navy 44R Slim", "category_id": 83}])
    assert any("looks like a suit" in w or "variant of matrix 7" in w for w in result["warnings"])


@pytest.mark.parametrize("entry, message", [
    ({**SHIRT, "description": "Caravelli Pants Black 34"}, "Pants are stocked in their own matrices"),
    ({**SHIRT, "description": "Caravelli Suits"}, "already has the description"),   # an active item's
    ({**SHIRT, "description": "Shirt A0000200IX White"}, "vendor code"),
    ({**SHIRT, "description": "Boys Dress Shirt 8"}, "boys"),
    ({**SHIRT, "custom_sku": "012911"}, "already used by item 1345"),
    ({**SHIRT, "category_id": 999}, "category 999"),
    ({**SHIRT, "vendor_id": 999}, "vendor 999"),
    ({**SHIRT, "price": 0}, "more than 0"),
    ({**SHIRT, "cost": None}, "not a number"),
])
def test_standalone_item_rules(writer, fake_api, entry, message):
    with pytest.raises(RuleViolation, match=message):
        writer.create_items([entry])
    assert fake_api.requests == []


def test_the_same_new_description_twice_is_refused(writer):
    with pytest.raises(RuleViolation, match="already has the description"):
        writer.create_items([SHIRT, dict(SHIRT)])


# ---- create_purchase_order ------------------------------------------------------------------------


def test_new_po_payload(writer):
    result = writer.create_purchase_order(vendor_id=1, ref_num="INV-5521", ship_cost=58.82)
    [request] = result["requests"]
    assert request["method"] == "POST" and request["path"].endswith("/Order.json")
    assert request["body"] == {"vendorID": "1", "shopID": "1", "refNum": "INV-5521", "shipCost": "58.82",
                               "otherCost": "0.00"}


@pytest.mark.parametrize("kwargs, message", [
    (dict(vendor_id=1, ref_num="112423"), "already purchase order 853"),   # the same invoice entered twice
    (dict(vendor_id=1, ref_num=" 112423 "), "already purchase order 853"),
    (dict(vendor_id=1, ref_num=""), "invoice or order number"),
    (dict(vendor_id=999, ref_num="1"), "vendor 999"),
    (dict(vendor_id=1, ref_num="1", ship_cost=-5), "negative"),
])
def test_new_po_rules(writer, fake_api, kwargs, message):
    with pytest.raises(RuleViolation, match=message):
        writer.create_purchase_order(**kwargs)
    assert fake_api.requests == []


def test_another_vendor_may_use_the_same_reference(writer):
    assert writer.create_purchase_order(vendor_id=21, ref_num="112423")["request_count"] == 1


# ---- remove_po_lines ----------------------------------------------------------------------------------


def test_remove_unreceived_line(writer, reads, fake_api, write_log, write_switch):
    reads.data["OrderLine"].append({"orderLineID": "13", "orderID": "853", "itemID": "1345", "quantity": "2",
                                    "numReceived": "0", "checkedIn": "0", "price": "63.50", "timeStamp": "t"})
    result = writer.remove_po_lines(853, [13])
    [request] = result["requests"]
    assert request["method"] == "DELETE" and request["path"].endswith("/OrderLine/13.json") and request["body"] is None
    turn_on(write_switch)
    writer.remove_po_lines(853, [13], dry_run=False, confirm_code=result["confirm_code"])
    assert fake_api.requests[0].method == "DELETE" and fake_api.requests[0].content == b""
    assert write_log.entries()[0]["before"]["quantity"] == "2"


@pytest.mark.parametrize("order_id, line_ids, message", [
    (853, [10], "1 received"),            # something already received
    (853, [11], "1 checked in"),
    (853, [12], "not on purchase order 853"),
    (400, [1], "finished"),
    (853, [], "at least one"),
])
def test_remove_line_rules(writer, fake_api, order_id, line_ids, message):
    with pytest.raises((RuleViolation, WriteRefused), match=message):
        writer.remove_po_lines(order_id, line_ids)
    assert fake_api.requests == []


# ---- "{new:...}": a later request uses an ID created earlier in the same plan --------------------------


def test_new_ids_are_filled_in_from_earlier_replies(writer, fake_api, write_switch):
    turn_on(write_switch)
    replies = iter([httpx.Response(200, json={"Order": {"orderID": "900"}}),
                    httpx.Response(200, json={"Item": {"itemID": "2500"}}),
                    httpx.Response(200, json={"OrderLine": {"orderLineID": "3000"}})])
    fake_api.handler = lambda request: next(replies)
    plan = Plan("intake_invoice", "test", [
        PlannedRequest("po", "POST", "Order.json", {"vendorID": "1", "shopID": "1", "refNum": "X"}, creates="order"),
        PlannedRequest("item", "POST", "Item.json", {"description": "Tie Navy", "customSku": "013101"}, creates="item:1"),
        PlannedRequest("line", "POST", "OrderLine.json", {"orderID": new_ref("order"), "itemID": new_ref("item:1"),
                                                          "quantity": "1"}),
    ])
    result = writer._execute(plan, plan.confirm_code())
    assert result["finished"] is True and result["created"] == {"order": "900", "item:1": "2500"}
    assert json.loads(fake_api.requests[2].content) == {"orderID": "900", "itemID": "2500", "quantity": "1"}


def test_an_unknown_new_reference_is_an_error():
    with pytest.raises(WriteRefused, match="was not created"):
        fill_in_new_ids({"orderID": new_ref("order")}, {})


# ---- update_purchase_order and update_po_lines ---------------------------------------------------------


def test_update_po_sends_only_changes(writer):
    result = writer.update_purchase_order(853, ref_num="112423", ship_cost=58.82, ordered_date="2026-10-20",
                                          received_date="2026-10-27")
    assert result["requests"][0]["body"] == {"shipCost": "58.82", "orderedDate": "2026-10-20",
                                             "receivedDate": "2026-10-27"}
    assert any("Check In" in w for w in result["warnings"])


@pytest.mark.parametrize("kwargs, message", [
    (dict(order_id=854, ref_num="112423"), "already purchase order 853"),
    (dict(order_id=853, ref_num="112423"), "nothing to change"),
    (dict(order_id=853, ordered_date="20/10/2026"), "not a date"),
    (dict(order_id=400, ship_cost=1), "finished"),
])
def test_update_po_rules(writer, kwargs, message):
    with pytest.raises((RuleViolation, WriteRefused), match=message):
        writer.update_purchase_order(**kwargs)


def test_dates_need_lines(writer, reads):
    reads.data["Order"].append({"orderID": "855", "vendorID": "1", "refNum": "9999", "complete": "false",
                                "archived": "false"})
    with pytest.raises(RuleViolation, match="no lines yet"):
        writer.update_purchase_order(855, ordered_date="2026-10-20")


def test_update_line_quantity_and_cost(writer):
    result = writer.update_po_lines(853, [{"order_line_id": 10, "quantity": 5, "unit_cost": 70}])
    assert result["requests"][0]["body"] == {"quantity": "5", "price": "70.00", "originalPrice": "70.00"}


def test_line_rules(writer, reads):
    reads.data["OrderLine"][0]["numReceived"] = "2"
    with pytest.raises(RuleViolation, match="can't be lowered to 1"):
        writer.update_po_lines(853, [{"order_line_id": 10, "quantity": 1}])
    with pytest.raises(RuleViolation, match="already added to inventory"):
        writer.update_po_lines(853, [{"order_line_id": 11, "unit_cost": 50}])   # 1 checked in
    with pytest.raises(RuleViolation, match="nothing to change"):
        writer.update_po_lines(853, [{"order_line_id": 10, "quantity": 3}])


# ---- set_vendor_numbers -------------------------------------------------------------------------------------


@pytest.fixture
def numbered(reads):
    by_id = {i["itemID"]: i for i in reads.data["Item"]}
    by_id["1346"]["ItemVendorNums"] = {"ItemVendorNum": {"itemVendorNumID": "7", "vendorID": "1", "value": "CV-40RC"}}
    by_id["1345"]["ItemVendorNums"] = {"ItemVendorNum": {"itemVendorNumID": "8", "vendorID": "21", "value": "GF-1"}}
    return reads


def test_vendor_number_payload_keeps_existing_numbers(writer, numbered):
    result = writer.set_vendor_numbers(1, [{"item_id": 1345, "vendor_number": "CV-50SC"}])
    # The new number carries the item's cost: Lightspeed would otherwise set the vendor cost to 0.
    assert result["requests"][0]["body"] == {"ItemVendorNums": {"ItemVendorNum": [
        {"itemVendorNumID": "8", "vendorID": "21", "value": "GF-1"},
        {"vendorID": "1", "value": "CV-50SC", "cost": "63.50"}]}}


def test_one_style_code_on_many_items(writer, numbered):
    """Renoir prints '201-19' for every size and fit of a style and color: it may go on all of them."""
    result = writer.set_vendor_numbers(1, [{"item_id": 1644, "vendor_number": "cv-40rc"},
                                           {"item_id": 1345, "vendor_number": "CV-40RC"}])
    assert [r["body"]["ItemVendorNums"]["ItemVendorNum"][-1]["value"] for r in result["requests"]] == [
        "cv-40rc", "CV-40RC"]
    # Already on item 1346: noted once, not refused.
    [note] = [w for w in result["warnings"] if "also on" in w]
    assert "item 1346" in note and "matched by matrix" in note


@pytest.mark.parametrize("items, message", [
    ([{"item_id": 1346, "vendor_number": "CV-NEW"}], "already has vendor number 'CV-40RC'"),  # one per vendor
    ([{"item_id": 1644, "vendor_number": " "}], "blank"),
    ([{"item_id": 1644, "vendor_number": "A"}, {"item_id": 1644, "vendor_number": "B"}], "listed twice"),
])
def test_vendor_number_rules(writer, numbered, items, message):
    with pytest.raises(RuleViolation, match=message):
        writer.set_vendor_numbers(1, items)


# ---- intake_invoice ------------------------------------------------------------------------------------------


INVOICE = dict(vendor_id=1, invoice_number="INV-7001", invoice_date="2026-10-24", ship_cost=40, lines=[
    {"item_id": 1345, "quantity": 2, "unit_cost": 63.5, "vendor_number": "CV-50SC"},
    {"matrix_id": 7, "color": "Cobalt", "size": "44R", "fit": "Slim", "quantity": 1, "unit_cost": 63.5, "price": 599.95},
    {"new_item": {"description": "Silk Tie Navy", "category_id": 116, "price": 39.95}, "quantity": 6, "unit_cost": 8},
])


def test_intake_plan_for_a_new_invoice(writer):
    result = writer.intake_invoice(**INVOICE)
    reqs = result["requests"]
    assert [(r["method"], r["path"].split("/Account/12345/")[1]) for r in reqs] == [
        ("POST", "Order.json"), ("PUT", "Item/1345.json"), ("POST", "Item.json"), ("POST", "Item.json"),
        ("POST", "OrderLine.json"), ("POST", "OrderLine.json"), ("POST", "OrderLine.json"),
        ("PUT", "Order/{new:order}.json")]
    assert reqs[0]["body"] == {"vendorID": "1", "shopID": "1", "refNum": "INV-7001", "shipCost": "40.00",
                               "otherCost": "0.00"}
    assert reqs[1]["body"] == {"ItemVendorNums": {"ItemVendorNum": [{"vendorID": "1", "value": "CV-50SC",
                                                                     "cost": "63.50"}]}}
    variant, tie = reqs[2]["body"], reqs[3]["body"]
    assert variant["itemMatrixID"] == "7" and variant["description"] == "Caravelli Cobalt 44R Slim"
    assert variant["Prices"]["ItemPrice"][0]["amount"] == "599.95" and variant["customSku"] == "013101"
    assert tie["description"] == "Silk Tie Navy" and tie["defaultVendorID"] == "1" and tie["customSku"] == "013102"
    assert [r["body"]["itemID"] for r in reqs[4:7]] == ["1345", "{new:item:2}", "{new:item:3}"]
    assert all(r["body"]["orderID"] == "{new:order}" for r in reqs[4:7])
    assert [r["body"]["numReceived"] for r in reqs[4:7]] == ["2", "1", "6"]
    assert reqs[7]["body"]["orderedDate"] == "2026-10-24" and "receivedDate" in reqs[7]["body"]
    assert "Add Received To Inventory" in result["after"]


def test_intake_runs_with_real_ids_filled_in(writer, fake_api, write_switch):
    turn_on(write_switch)
    new_ids = iter(["2501", "2502"])

    def api(request):
        path = request.url.path.split("/Account/12345/")[1]
        if path == "Order.json":
            return httpx.Response(200, json={"Order": {"orderID": "900"}})
        if path == "Item.json":
            return httpx.Response(200, json={"Item": {"itemID": next(new_ids)}})
        return httpx.Response(200, json={path.split("/")[0].removesuffix(".json"): {"ok": "1"}})

    fake_api.handler = api
    code = writer.intake_invoice(**INVOICE)["confirm_code"]
    result = writer.intake_invoice(**INVOICE, dry_run=False, confirm_code=code)
    assert result["finished"] is True and result["created"] == {"order": "900", "item:2": "2501", "item:3": "2502"}
    lines = [json.loads(r.content) for r in fake_api.requests if r.url.path.endswith("/OrderLine.json")]
    assert [(line["orderID"], line["itemID"]) for line in lines] == [("900", "1345"), ("900", "2501"), ("900", "2502")]
    assert fake_api.requests[-1].method == "PUT" and fake_api.requests[-1].url.path.endswith("/Order/900.json")


def test_intake_against_an_existing_po(writer):
    result = writer.intake_invoice(vendor_id=1, invoice_number="112423", order_id=853, lines=[
        {"item_id": 1644, "quantity": 3, "unit_cost": 65.0}])
    [line, header] = result["requests"]
    assert line["path"].endswith("/OrderLine/10.json")
    # 1 already received + 3 now = 4, more than the 3 ordered: the quantity is raised to match.
    assert line["body"] == {"numReceived": "4", "quantity": "4"}
    assert any("more arrived than was ordered" in w for w in result["warnings"])
    assert any("negative stock (-27); after check-in it would be -24" in w for w in result["warnings"])
    assert header["path"].endswith("/Order/853.json") and "receivedDate" in header["body"]


def test_intake_updates_the_cost_when_the_invoice_differs(writer):
    result = writer.intake_invoice(vendor_id=1, invoice_number="112423", order_id=853, lines=[
        {"item_id": 1644, "quantity": 1, "unit_cost": 70}])
    assert result["requests"][0]["body"] == {"numReceived": "2", "price": "70.00", "originalPrice": "70.00"}


def test_intake_without_receiving(writer):
    result = writer.intake_invoice(**{**INVOICE, "mark_received": False})
    lines = [r for r in result["requests"] if r["path"].endswith("/OrderLine.json")]
    assert {r["body"]["numReceived"] for r in lines} == {"0"}
    assert "receivedDate" not in result["requests"][-1]["body"]


def test_style_codes_on_several_lines_are_saved_on_each_item(writer):
    result = writer.intake_invoice(vendor_id=1, invoice_number="INV-7002", lines=[
        {"item_id": 1345, "quantity": 1, "unit_cost": 63.5, "vendor_number": "CV-STYLE"},
        {"item_id": 1644, "quantity": 1, "unit_cost": 63.5, "vendor_number": "CV-STYLE"}])
    saved = {r["path"].rsplit("/", 1)[1]: r["body"]["ItemVendorNums"]["ItemVendorNum"][-1]["value"]
             for r in result["requests"] if "ItemVendorNums" in (r["body"] or {})}
    assert saved == {"1345.json": "CV-STYLE", "1644.json": "CV-STYLE"}


@pytest.mark.parametrize("overrides, message", [
    (dict(lines=[{"description": "mystery jacket", "quantity": 1, "unit_cost": 5}]), "match an item"),
    (dict(invoice_number="112423"), "already purchase order 853"),   # the same invoice twice
    (dict(lines=[{"item_id": 1345, "quantity": 1, "unit_cost": 1}, {"item_id": 1345, "quantity": 2, "unit_cost": 1}]),
     "same item"),
    (dict(lines=[{"matrix_id": 7, "color": "Cobalt", "size": "44R", "fit": "Slim", "quantity": 1, "unit_cost": 63.5}]),
     "give its selling price"),
    (dict(vendor_id=21, order_id=853), "another vendor"),
    (dict(lines=[{"item_id": 2000, "quantity": 1, "unit_cost": 1}]), "boys"),
    (dict(lines=[{"matrix_id": 12, "color": "Black", "size": "34x32", "fit": "Standard", "quantity": 1,
                  "unit_cost": 20, "price": 99}]), "waist alone"),
    (dict(lines=[]), "lines"),
])
def test_intake_rules(writer, fake_api, overrides, message):
    with pytest.raises((RuleViolation, WriteRefused), match=message):
        writer.intake_invoice(**{**INVOICE, **overrides})
    assert fake_api.requests == []


# ---- create_vendor / create_brand, and intake from a new vendor ---------------------------------------------------


@pytest.fixture
def with_archived_vendor(monkeypatch):
    import tests.test_writer as canned
    monkeypatch.setattr(canned, "VENDORS", canned.VENDORS + [
        {"vendorID": "30", "name": "Old Ties Ltd", "archived": "true"}])


def test_new_vendor_payload(writer):
    result = writer.create_vendor("Bruno Piattelli Inc.", account_number="SM-4411")
    assert result["requests"][0]["body"] == {"name": "Bruno Piattelli Inc.", "accountNumber": "SM-4411"}
    assert result["requests"][0]["path"].endswith("/Vendor.json")


@pytest.mark.parametrize("name, message", [
    ("San Malone, Inc.", "vendor 1 is already called 'San Malone'"),
    ("old ties", "archived: unarchive it in Lightspeed"),
    ("San Malonee", "looks like an existing vendor"),
    ("  ", "empty"),
])
def test_new_vendor_rules(writer, with_archived_vendor, fake_api, name, message):
    with pytest.raises(RuleViolation, match=message):
        writer.create_vendor(name)
    assert fake_api.requests == []


def test_a_similar_vendor_can_be_created_once_confirmed(writer):
    result = writer.create_vendor("San Malone Shirts", allow_similar_name=True)
    assert any("Similar existing vendor" in w and "San Malone" in w for w in result["warnings"])


def test_new_brand_payload_and_rules(writer):
    result = writer.create_brand("Bruno Piattelli")
    assert result["requests"][0]["body"] == {"name": "Bruno Piattelli"}
    assert result["requests"][0]["path"].endswith("/Manufacturer.json")
    with pytest.raises(RuleViolation, match="brand 1 is already called 'Caravelli'"):
        writer.create_brand("CARAVELLI")
    with pytest.raises(RuleViolation, match="looks like an existing brand"):
        writer.create_brand("Giorgio Fiorrelli")


NEW_VENDOR_INVOICE = dict(vendor_id=None, new_vendor={"name": "Bruno Piattelli Inc.", "account_number": "88"},
                          invoice_number="BP-100", lines=[
    {"new_item": {"description": "Silk Tie Teal", "category_id": 116, "price": 49.95, "new_brand": "Bruno Piattelli"},
     "quantity": 4, "unit_cost": 12, "vendor_number": "BP-TIE-TEAL"},
    {"item_id": 1345, "quantity": 1, "unit_cost": 63.5}])


def test_intake_creates_the_new_vendor_and_brand_first(writer):
    result = writer.intake_invoice(**NEW_VENDOR_INVOICE)
    reqs = result["requests"]
    assert [(r["method"], r["path"].split("/Account/12345/")[1]) for r in reqs][:4] == [
        ("POST", "Vendor.json"), ("POST", "Manufacturer.json"), ("POST", "Order.json"), ("POST", "Item.json")]
    assert reqs[0]["body"] == {"name": "Bruno Piattelli Inc.", "accountNumber": "88"}
    assert reqs[2]["body"]["vendorID"] == "{new:vendor}"
    tie = reqs[3]["body"]
    assert tie["defaultVendorID"] == "{new:vendor}" and tie["manufacturerID"] == "{new:brand:1}"
    assert tie["ItemVendorNums"] == {"ItemVendorNum": [{"vendorID": "{new:vendor}", "value": "BP-TIE-TEAL",
                                                        "cost": "12.00"}]}
    assert result["summary"].startswith("Creates the new vendor 'Bruno Piattelli Inc.' and 1 new brand(s) first.")


def test_intake_fills_in_the_new_vendor_and_brand_ids(writer, fake_api, write_switch):
    turn_on(write_switch)
    replies = {"Vendor.json": {"Vendor": {"vendorID": "700"}}, "Manufacturer.json": {"Manufacturer": {"manufacturerID": "60"}},
               "Order.json": {"Order": {"orderID": "901"}}, "Item.json": {"Item": {"itemID": "2600"}}}
    fake_api.handler = lambda request: httpx.Response(200, json=replies.get(
        request.url.path.split("/Account/12345/")[1], {"OrderLine": {"orderLineID": "1"}}))
    code = writer.intake_invoice(**NEW_VENDOR_INVOICE)["confirm_code"]
    result = writer.intake_invoice(**NEW_VENDOR_INVOICE, dry_run=False, confirm_code=code)
    assert result["finished"] is True
    sent = {r.url.path.split("/Account/12345/")[1]: json.loads(r.content) if r.content else None
            for r in fake_api.requests if r.method == "POST"}
    assert sent["Order.json"]["vendorID"] == "700"
    assert sent["Item.json"]["defaultVendorID"] == "700" and sent["Item.json"]["manufacturerID"] == "60"
    assert sent["Item.json"]["ItemVendorNums"]["ItemVendorNum"][0]["vendorID"] == "700"


@pytest.mark.parametrize("overrides, message", [
    (dict(vendor_id=1), "not both"),
    (dict(new_vendor=None), "give vendor_id"),
    (dict(new_vendor={"name": "San Malone LLC"}), "already called 'San Malone'"),
    (dict(order_id=853), "can't belong to a new vendor"),
    (dict(lines=[{"new_item": {"description": "Silk Tie Teal", "category_id": 116, "price": 49.95,
                               "new_brand": "Caravelli"}, "quantity": 1, "unit_cost": 1}]), "already called 'Caravelli'"),
])
def test_intake_new_vendor_rules(writer, fake_api, overrides, message):
    with pytest.raises(RuleViolation, match=message):
        writer.intake_invoice(**{**NEW_VENDOR_INVOICE, **overrides})
    assert fake_api.requests == []


def test_similar_names_need_confirming_in_intake_too(writer):
    with pytest.raises(RuleViolation, match="allow_similar_names=true"):
        writer.intake_invoice(**{**NEW_VENDOR_INVOICE, "new_vendor": {"name": "San Malonee"}})
    assert writer.intake_invoice(**{**NEW_VENDOR_INVOICE, "new_vendor": {"name": "San Malonee"},
                                    "allow_similar_names": True})["request_count"] >= 5


# ---- update_item_prices -----------------------------------------------------------------------------------------


@pytest.fixture
def three_levels(reads):
    for item in reads.data["Item"]:
        item["Prices"] = {"ItemPrice": [{"amount": "599.95", "useTypeID": "1", "useType": "Default"},
                                        {"amount": "695", "useTypeID": "2", "useType": "MSRP"},
                                        {"amount": "549.95", "useTypeID": "3", "useType": "Online"}]}
    return reads


def test_price_change_sends_every_level_with_only_the_asked_one_changed(writer, three_levels):
    result = writer.update_item_prices([{"item_id": 1644, "msrp": 795}])
    [request] = result["requests"]
    assert request["method"] == "PUT" and request["path"].endswith("/Item/1644.json")
    assert request["body"] == {"Prices": {"ItemPrice": [
        {"amount": "599.95", "useTypeID": "1", "useType": "Default"},
        {"amount": "795.00", "useTypeID": "2", "useType": "MSRP"},
        {"amount": "549.95", "useTypeID": "3", "useType": "Online"}]}}
    assert "MSRP 695.0 -> 795.00" in request["does"]


@pytest.mark.parametrize("entry, message", [
    ({"item_id": 1644, "default": 599.95}, "nothing to change"),
    ({"item_id": 1644, "default": 0}, "more than 0"),
    ({"item_id": 1644, "online": -1}, "negative"),
    ({"item_id": 1700, "default": 1}, "archived"),
])
def test_price_rules(writer, three_levels, entry, message):
    with pytest.raises(RuleViolation, match=message):
        writer.update_item_prices([entry])


def test_price_warnings(writer, three_levels):
    result = writer.update_item_prices([{"item_id": 1644, "default": 60, "msrp": 50}])
    assert any("not above its cost" in w for w in result["warnings"])
    assert any("MSRP 50.00 is below" in w for w in result["warnings"])


# ---- set_reorder_levels ---------------------------------------------------------------------------------------------


def test_reorder_payload_is_the_shop_row_only(writer, reads):
    for item in reads.data["Item"]:
        for row in item["ItemShops"]["ItemShop"]:
            row["itemShopID"] = "9" + row["shopID"] + item["itemID"]
    result = writer.set_reorder_levels([{"item_id": 1644, "reorder_point": 1, "reorder_level": 3}])
    [request] = result["requests"]
    assert request["method"] == "PUT" and request["path"].endswith("/ItemShop/911644.json")   # the shop-1 row
    assert request["body"] == {"reorderPoint": "1", "reorderLevel": "3"}
    assert "qoh" not in json.dumps(result["requests"])


def test_reorder_rules(writer, reads):
    for item in reads.data["Item"]:
        for row in item["ItemShops"]["ItemShop"]:
            row["itemShopID"] = "9" + row["shopID"] + item["itemID"]
    with pytest.raises(RuleViolation, match="whole number of 0 or more"):
        writer.set_reorder_levels([{"item_id": 1644, "reorder_point": -1}])
    with pytest.raises(RuleViolation, match="nothing to change"):
        writer.set_reorder_levels([{"item_id": 1644, "reorder_point": 0}])
    result = writer.set_reorder_levels([{"item_id": 1644, "reorder_point": 4, "reorder_level": 2}])
    assert any("below its reorder point" in w for w in result["warnings"])


# ---- archive_purchase_order --------------------------------------------------------------------------------------------


def test_archive_po_payload_and_log(writer, reads, fake_api, write_log, write_switch):
    reads.data["Order"].append({"orderID": "353", "vendorID": "1", "refNum": "267751", "complete": "false",
                                "archived": "false", "createTime": "2024-06-05T00:00:00+00:00", "timeStamp": "t"})
    reads.data["OrderLine"].append({"orderLineID": "50", "orderID": "353", "itemID": "1345", "quantity": "2",
                                    "numReceived": "0", "checkedIn": "0", "price": "63.50"})
    result = writer.archive_purchase_order(353)
    [request] = result["requests"]
    assert request["method"] == "DELETE" and request["path"].endswith("/Order/353.json") and request["body"] is None
    turn_on(write_switch)
    writer.archive_purchase_order(353, dry_run=False, confirm_code=result["confirm_code"])
    before = write_log.entries()[0]["before"]
    assert before["order"]["refNum"] == "267751" and before["lines"][0]["quantity"] == "2"


@pytest.mark.parametrize("order_id, message", [
    (853, "goods received or checked in"),   # line 10 has 1 received
    (400, "finished"),
    (404, "not found"),
])
def test_archive_po_rules(writer, fake_api, order_id, message):
    with pytest.raises(RuleViolation, match=message):
        writer.archive_purchase_order(order_id)
    assert fake_api.requests == []


# ---- update_matrix ---------------------------------------------------------------------------------------------


def test_matrix_rename_previews_every_item(writer):
    result = writer.update_matrix(7, description="Caravelli Suit")
    assert result["requests"][0]["body"] == {"description": "Caravelli Suit"}
    renames = next(w for w in result["warnings"] if "renames all" in w)
    assert "all 3 item(s)" in renames and "-> 'Caravelli Suit Black 42R Slim'" in renames


def test_matrix_price_keeps_other_levels_and_shows_every_value_items_would_get(writer, reads, three_levels):
    reads.data["ItemMatrix"][0]["Prices"]["ItemPrice"] += [{"amount": "995", "useTypeID": "2", "useType": "MSRP"},
                                                           {"amount": "0", "useTypeID": "3", "useType": "Online"}]
    result = writer.update_matrix(7, price=599.95)
    assert result["requests"][0]["body"] == {"Prices": {"ItemPrice": [
        {"amount": "599.95", "useTypeID": "1", "useType": "Default"},
        {"amount": "995", "useTypeID": "2", "useType": "MSRP"},
        {"amount": "0", "useTypeID": "3", "useType": "Online"}]}}
    warning = next(w for w in result["warnings"] if "Reset Items To Defaults" in w)
    assert "Default 599.95 (0 of 3 differ today: 3 at 599.95)" in warning
    assert "MSRP 995.00 (3 of 3 differ today: 3 at 695.00)" in warning
    assert "Online 0.00 (3 of 3 differ today: 3 at 549.95)" in warning
    assert "Every price level is sent" in warning and "default cost" not in warning


def test_matrix_cost_warning_names_only_the_cost(writer, reads):
    reads.data["Item"][0]["defaultCost"] = "70"
    result = writer.update_matrix(7, default_cost=70)
    assert result["requests"][0]["body"] == {"defaultCost": "70.00"}
    warning = next(w for w in result["warnings"] if "Reset Items To Defaults" in w)
    assert "default cost 70.00 (" in warning and "differ today" in warning
    assert "MSRP" not in warning and "Every price level is sent" not in warning


def test_matrix_category_change_counts_affected_items(writer):
    result = writer.update_matrix(7, category_id=84)
    assert result["requests"][0]["body"] == {"categoryID": "84"}
    assert any("all 3 item(s); 3 of them" in w for w in result["warnings"])


@pytest.mark.parametrize("kwargs, message", [
    (dict(description="Caravelli Classic"), "holds suit matrices, so its description names the garment"),
    (dict(description="Caravelli Pants"), "names the garment"),          # in Suits/Suit
    (dict(category_id=81), "shoes matrices use matrix type MT2"),
    (dict(description="Caravelli Black Suit"), "names the color 'Black'"),
    (dict(description="Caravelli 320-0S2 Suit"), "vendor code"),
    (dict(description="Caravelli"), "nothing to change"),
    (dict(category_id=83), "nothing to change"),
    (dict(matrix_id=404, price=1), "not found"),
    (dict(manufacturer_id=999), "manufacturer 999"),
])
def test_matrix_update_rules(writer, fake_api, kwargs, message):
    with pytest.raises(RuleViolation, match=message):
        writer.update_matrix(**{"matrix_id": 7, **kwargs})
    assert fake_api.requests == []


def test_matrix_update_refuses_another_matrixs_name(writer, reads):
    reads.data["ItemMatrix"][1]["description"] = "Giorgio Fiorelli Suit"     # matrix 8
    with pytest.raises(RuleViolation, match="already called"):
        writer.update_matrix(7, description="Giorgio Fiorelli Suit")
