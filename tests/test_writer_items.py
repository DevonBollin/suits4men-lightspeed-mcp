"""Item tools: vendor, brand and category; UPCs; stock changes.

Same setup as test_writer.py: canned reads, a mocked write transport, the switch in a temp folder.
"""

from __future__ import annotations

import json

import httpx
import pytest

from lightspeed_mcp.errors import WriteRefused
from lightspeed_mcp.reader import InventoryReader
from lightspeed_mcp.rules import RuleViolation
from lightspeed_mcp.writer import InventoryWriter
from tests.conftest import turn_on
from tests.test_writer import CannedReads


@pytest.fixture
def reads():
    return CannedReads()


@pytest.fixture
def writer(reads, make_write_client, fake_api):
    fake_api.handler = lambda request: httpx.Response(200, json={"Item": {"itemID": "2001"}})
    return InventoryWriter(InventoryReader(reads), make_write_client())


# ---- reassign_items ------------------------------------------------------------------------------------------


def test_reassign_sends_only_the_fields_that_change(writer):
    result = writer.reassign_items([{"item_id": 1186, "vendor_id": 21, "manufacturer_id": 1, "category_id": 84}])
    [request] = result["requests"]
    # Brand 1 is already the item's brand, so only vendor and category are sent.
    assert request["method"] == "PUT" and request["path"].endswith("/Item/1186.json")
    assert request["body"] == {"defaultVendorID": "21", "categoryID": "84"}
    assert "vendor 1 -> 21 (Fiorelli Supply)" in request["does"] and "Suits/Tuxedos" in request["does"]


def test_reassign_warns_that_the_vendor_number_stays_with_the_old_vendor(writer, reads):
    by_id = {i["itemID"]: i for i in reads.data["Item"]}
    by_id["1186"]["ItemVendorNums"] = {"ItemVendorNum": {"itemVendorNumID": "4", "vendorID": "1", "value": "CS-ALL"}}
    result = writer.reassign_items([{"item_id": 1186, "vendor_id": 21}])
    assert any("CS-ALL stays filed under the old vendor" in w for w in result["warnings"])


@pytest.mark.parametrize("items, message", [
    ([{"item_id": 1644, "vendor_id": 21}], "in matrix 7"),          # update_matrix changes a whole matrix
    ([{"item_id": 1700, "vendor_id": 21}], "archived"),
    ([{"item_id": 1186, "vendor_id": 1}], "nothing to change"),
    ([{"item_id": 1186, "vendor_id": 999}], "vendor 999 does not exist"),
    ([{"item_id": 1186, "manufacturer_id": 99}], "manufacturer 99 does not exist"),
    ([{"item_id": 1186, "category_id": 999}], "category 999 does not exist"),
    ([{"item_id": 1186, "vendor_id": 21}, {"item_id": 1186, "category_id": 84}], "listed twice"),
    ([], "at least one"),
])
def test_reassign_rules(writer, fake_api, items, message):
    with pytest.raises(RuleViolation, match=message):
        writer.reassign_items(items)
    assert fake_api.requests == []


# ---- UPCs ---------------------------------------------------------------------------------------------------

UPC = "036000291452"


@pytest.fixture
def labeled(reads):
    """Item 1346 already has a UPC; item 1700 (archived) has another."""
    by_id = {i["itemID"]: i for i in reads.data["Item"]}
    by_id["1346"]["upc"] = UPC
    by_id["1700"]["upc"] = "4006381333931"
    return reads


def test_set_upcs_sends_only_the_upc(writer):
    result = writer.set_upcs([{"item_id": 1345, "upc": "0 36000 29145 2"}])
    [request] = result["requests"]
    assert request["method"] == "PUT" and request["path"].endswith("/Item/1345.json")
    assert request["body"] == {"upc": UPC}


def test_set_upcs_warns_when_replacing_one(writer, labeled):
    result = writer.set_upcs([{"item_id": 1346, "upc": "03600029145"}])
    assert any(f"replaces UPC {UPC}" in w for w in result["warnings"])


@pytest.mark.parametrize("items, message", [
    ([{"item_id": 1345, "upc": UPC}], "already on item 1346"),
    ([{"item_id": 1345, "upc": "4006381333931"}], r"already on item 1700 .*\(archived\)"),
    ([{"item_id": 1346, "upc": UPC}], "already has UPC"),
    ([{"item_id": 1345, "upc": "036000291453"}], "check digit"),
    ([{"item_id": 1345, "upc": "03600029145"}, {"item_id": 1644, "upc": "03600029145"}], "listed twice"),
    ([{"item_id": 1700, "upc": "03600029145"}], "archived"),
])
def test_set_upcs_rules(writer, labeled, fake_api, items, message):
    with pytest.raises(RuleViolation, match=message):
        writer.set_upcs(items)
    assert fake_api.requests == []


def test_new_variant_carries_its_upc(writer, labeled):
    result = writer.add_matrix_variants(7, [{"color": "Cobalt", "size": "42R", "fit": "Slim", "custom_sku": "013471",
                                             "upc": "03600029145"}], price=599.95, cost=63.5)
    assert result["requests"][0]["body"]["upc"] == "03600029145"
    with pytest.raises(RuleViolation, match="already on item 1346"):
        writer.add_matrix_variants(7, [{"color": "Cobalt", "size": "42R", "fit": "Slim", "custom_sku": "013471",
                                        "upc": UPC}], price=599.95, cost=63.5)


def test_new_standalone_item_carries_its_upc(writer, labeled):
    entry = {"description": "Dress Shoe Black 10.5", "category_id": 83, "vendor_id": 1, "price": 129.95, "cost": 60,
             "custom_sku": "013480"}
    result = writer.create_items([{**entry, "upc": "03600029145"}])
    assert result["requests"][0]["body"]["upc"] == "03600029145"
    with pytest.raises(RuleViolation, match="already on item 1346"):
        writer.create_items([{**entry, "upc": UPC}])


def test_intake_matches_by_upc_and_puts_it_on_new_items(writer, labeled):
    result = writer.intake_invoice(vendor_id=1, invoice_number="INV-UPC", lines=[
        {"upc": UPC, "quantity": 1, "unit_cost": 63.5},                                       # item 1346
        {"matrix_id": 7, "color": "Cobalt", "size": "44R", "fit": "Slim", "upc": "03600029145", "quantity": 1,
         "unit_cost": 63.5, "price": 599.95}])
    new_item = next(r for r in result["requests"] if r["path"].endswith("/Item.json"))
    assert new_item["body"]["upc"] == "03600029145"
    lines = [r["body"]["itemID"] for r in result["requests"] if r["path"].endswith("/OrderLine.json")]
    assert lines == ["1346", "{new:item:2}"]


def test_intake_notes_an_existing_item_without_the_upc(writer):
    result = writer.intake_invoice(vendor_id=1, invoice_number="INV-UPC2", lines=[
        {"item_id": 1345, "upc": "03600029145", "quantity": 1, "unit_cost": 63.5}])
    assert any("has no UPC; set_upcs can save 03600029145" in w for w in result["warnings"])


# ---- adjust_stock --------------------------------------------------------------------------------------------


@pytest.fixture
def stock_rows(reads):
    """Give every canned stock row an ID: '9' + shop + item (shop 1's row of item 1644 is 911644)."""
    for item in reads.data["Item"]:
        for row in item["ItemShops"]["ItemShop"]:
            row["itemShopID"] = "9" + row["shopID"] + item["itemID"]
    return reads


@pytest.fixture
def confirmed(write_log):
    """A first stock change has been done and checked."""
    write_log.append({"write_id": "x", "tool": "adjust_stock", "event": "done", "status": 200})


def test_first_stock_change_is_one_item_with_the_shop_row_only(writer, stock_rows):
    result = writer.adjust_stock([{"item_id": 1346, "change": -1}], "Broke suit: pants sold alone")
    [request] = result["requests"]
    assert request["method"] == "PUT" and request["path"].endswith("/Item/1346.json")
    assert request["body"] == {"ItemShops": {"ItemShop": [{"itemShopID": "911346", "qoh": "2"}]}}  # 3 - 1
    assert "stock 3 -> 2 (-1)" in request["does"]
    assert "Broke suit" in result["summary"] and "first stock change" in result["after"]
    with pytest.raises(RuleViolation, match="one item per call"):
        writer.adjust_stock([{"item_id": 1346, "change": -1}, {"item_id": 1801, "change": 1}], "Broke suit")


def test_breaking_a_suit_after_the_first_change(writer, stock_rows, confirmed):
    result = writer.adjust_stock([{"item_id": 1346, "change": -1}, {"item_id": 1801, "change": 1}],
                                 "Broke suit 1346: jacket sold alone, pants to Caravelli Pants 34")
    assert [r["body"]["ItemShops"]["ItemShop"][0]["qoh"] for r in result["requests"]] == ["2", "1"]
    assert "after" not in result
    assert any("Cost control" in w for w in result["warnings"])       # +1 on the pants


def test_set_to_zero_settles_a_placeholder(writer, stock_rows):
    result = writer.adjust_stock([{"item_id": 1186, "set_to": 0}], "Settle placeholder before archiving")
    assert result["requests"][0]["body"]["ItemShops"]["ItemShop"][0]["qoh"] == "0"
    assert "stock -16 -> 0 (+16)" in result["requests"][0]["does"]


def test_a_sale_after_the_dry_run_makes_it_stale(writer, reads, stock_rows, fake_api, write_switch):
    turn_on(write_switch)
    args = ([{"item_id": 1186, "set_to": 0}], "Settle placeholder before archiving")
    code = writer.adjust_stock(*args)["confirm_code"]
    for row in next(i for i in reads.data["Item"] if i["itemID"] == "1186")["ItemShops"]["ItemShop"]:
        row["qoh"] = "-17"                                             # one more sold meanwhile
    with pytest.raises(WriteRefused, match="does not match"):
        writer.adjust_stock(*args, dry_run=False, confirm_code=code)
    assert fake_api.requests == []


def test_the_reason_and_old_stock_are_logged(writer, stock_rows, fake_api, write_log, write_switch):
    turn_on(write_switch)
    args = ([{"item_id": 1346, "change": -1}], "Broke suit: pants sold alone")
    code = writer.adjust_stock(*args)["confirm_code"]
    writer.adjust_stock(*args, dry_run=False, confirm_code=code)
    [sending] = [e for e in write_log.entries() if e["event"] == "sending"]
    assert sending["before"] == {"itemID": "1346", "itemShopID": "911346", "shopID": "1", "qoh": "3",
                                 "reason": "Broke suit: pants sold alone"}
    assert json.loads(fake_api.requests[0].content) == {"ItemShops": {"ItemShop": [{"itemShopID": "911346",
                                                                                     "qoh": "2"}]}}


@pytest.mark.parametrize("items, reason, message", [
    ([{"item_id": 1346, "change": -1}], "", "say why"),
    ([{"item_id": 1346}], "Recount", "either change"),
    ([{"item_id": 1346, "change": -1, "set_to": 2}], "Recount", "either change"),
    ([{"item_id": 1346, "change": 0}], "Recount", "other than 0"),
    ([{"item_id": 1346, "change": 1.5}], "Recount", "other than 0"),
    ([{"item_id": 1346, "set_to": -1}], "Recount", "from 0 to"),
    ([{"item_id": 1346, "set_to": 3}], "Recount", "already has stock 3"),
    ([{"item_id": 1700, "change": 1}], "Recount", "archived"),
    ([{"item_id": 99999, "change": 1}], "Recount", "not found"),
])
def test_stock_change_rules(writer, stock_rows, fake_api, items, reason, message):
    with pytest.raises(RuleViolation, match=message):
        writer.adjust_stock(items, reason)
    assert fake_api.requests == []


def test_an_item_without_a_shop_row_is_refused(writer):
    with pytest.raises(RuleViolation, match="no stock row for shop 1"):
        writer.adjust_stock([{"item_id": 1346, "change": -1}], "Recount")   # canned rows have no itemShopID


def test_still_negative_and_large_changes_are_flagged(writer, stock_rows, confirmed):
    result = writer.adjust_stock([{"item_id": 1644, "change": 2}, {"item_id": 1345, "set_to": 150}], "Recount")
    assert any("stock would be -25, still negative" in w for w in result["warnings"])
    assert any("+150 units" in w for w in result["warnings"])


# ---- set_item_tags ------------------------------------------------------------------------------------------


def test_first_tag_write_is_one_item_with_its_whole_list(writer, reads):
    by_id = {i["itemID"]: i for i in reads.data["Item"]}
    by_id["1346"]["Tags"] = {"@attributes": {"count": "1"}, "tag": "wool"}       # one tag comes as a string
    result = writer.set_item_tags([{"item_id": 1346, "add": ["super-150s", "wool"]}])
    [request] = result["requests"]
    assert request["method"] == "PUT" and request["path"].endswith("/Item/1346.json")
    assert request["body"] == {"Tags": {"tag": ["wool", "super-150s"]}}
    assert "+super-150s" in request["does"]
    assert any("older API's form" in w for w in result["warnings"]) and "first tag write" in result["after"]
    with pytest.raises(RuleViolation, match="one item per call"):
        writer.set_item_tags([{"item_id": 1346, "add": ["wool"]}, {"item_id": 1644, "add": ["wool"]}])


def test_tags_are_removed_and_read_from_tag_relations_too(writer, reads, write_log):
    write_log.append({"write_id": "x", "tool": "set_item_tags", "event": "done", "status": 200})
    by_id = {i["itemID"]: i for i in reads.data["Item"]}
    by_id["1346"]["TagRelations"] = {"TagRelation": [{"Tag": {"name": "wool"}}, {"Tag": {"name": "stretch"}}]}
    result = writer.set_item_tags([{"item_id": 1346, "remove": ["Stretch"], "add": ["non-iron"]},
                                   {"item_id": 1644, "add": ["wool"]}])
    assert [r["body"] for r in result["requests"]] == [{"Tags": {"tag": ["wool", "non-iron"]}},
                                                       {"Tags": {"tag": ["wool"]}}]


@pytest.mark.parametrize("entry, message", [
    ({"item_id": 1346, "add": ["Super 150s"]}, "e.g. 'super-150s'"),
    ({"item_id": 1346, "add": ["wool"], "remove": ["wool"]}, "added and removed"),
    ({"item_id": 1346, "remove": ["linen"]}, "has no tag linen"),
    ({"item_id": 1346}, "nothing to change"),
    ({"item_id": 1700, "add": ["wool"]}, "archived"),
])
def test_tag_rules(writer, fake_api, entry, message):
    with pytest.raises(RuleViolation, match=message):
        writer.set_item_tags([entry])
    assert fake_api.requests == []


# ---- archive_items ------------------------------------------------------------------------------------------


@pytest.fixture
def archived_once(write_log):
    """A first archive has been done and checked."""
    write_log.append({"write_id": "x", "tool": "archive_item", "event": "done", "status": 200})


def test_first_batch_archive_is_one_item(writer):
    result = writer.archive_items([1345])
    [request] = result["requests"]
    assert request["method"] == "DELETE" and request["path"].endswith("/Item/1345.json") and request["body"] is None
    assert "first archive" in result["after"]
    with pytest.raises(RuleViolation, match="one item per call"):
        writer.archive_items([1345, 2321])


def test_batch_archive_after_the_first(writer, archived_once, fake_api, write_log, write_switch):
    turn_on(write_switch)
    code = writer.archive_items([1345, 2321])["confirm_code"]
    result = writer.archive_items([1345, 2321], dry_run=False, confirm_code=code)
    assert result["finished"] is True
    assert [(r.method, r.url.path.rsplit("/", 1)[1]) for r in fake_api.requests] == [("DELETE", "1345.json"),
                                                                                       ("DELETE", "2321.json")]
    befores = [e["before"]["itemID"] for e in write_log.entries() if e["event"] == "sending"]
    assert befores == ["1345", "2321"]                                  # each item's values logged first


@pytest.mark.parametrize("ids, message", [
    ([1345, 1644], "has stock -27"),                 # one item with stock refuses the whole call
    ([1345, 1700], "already archived"),
    ([1345, 99999], "99999 not found"),
    ([1345, 1345], "listed twice"),
    ([], "at least one"),
])
def test_batch_archive_rules(writer, archived_once, fake_api, ids, message):
    with pytest.raises(RuleViolation, match=message):
        writer.archive_items(ids)
    assert fake_api.requests == []


def test_batch_archive_refuses_an_item_on_an_open_po(writer, reads, archived_once):
    reads.data["OrderLine"].append({"orderLineID": "13", "orderID": "854", "itemID": "1345", "quantity": "1",
                                    "numReceived": "0", "checkedIn": "0", "price": "60"})
    with pytest.raises(RuleViolation, match="open purchase order"):
        writer.archive_items([2321, 1345])


# ---- new variants take the tags their matrix's items share ---------------------------------------------------


@pytest.fixture
def tagged(reads):
    """Matrix 7's active items (1644, 1345, 1346) all have 'poly-viscose'; two also have 'stretch'."""
    by_id = {i["itemID"]: i for i in reads.data["Item"]}
    by_id["1644"]["Tags"] = {"tag": ["poly-viscose", "stretch"]}
    by_id["1345"]["TagRelations"] = {"TagRelation": [{"Tag": {"name": "stretch"}}, {"Tag": {"name": "poly-viscose"}}]}
    by_id["1346"]["Tags"] = {"tag": "poly-viscose"}
    return reads


@pytest.fixture
def tags_confirmed(write_log):
    write_log.append({"write_id": "x", "tool": "set_item_tags", "event": "done", "status": 200})


NEW_VARIANT = [{"color": "Cobalt", "size": "44R", "fit": "Slim", "custom_sku": "013471"}]


def test_new_variants_take_the_tags_every_sibling_has(writer, tagged, tags_confirmed):
    result = writer.add_matrix_variants(7, NEW_VARIANT, price=599.95, cost=63.5)
    assert result["requests"][0]["body"]["Tags"] == {"tag": ["poly-viscose"]}
    assert "tags poly-viscose from its other items" in result["summary"]


def test_before_a_first_tag_write_the_tags_are_only_named(writer, tagged):
    result = writer.add_matrix_variants(7, NEW_VARIANT, price=599.95, cost=63.5)
    assert "Tags" not in result["requests"][0]["body"]
    assert any("tags poly-viscose; new items will get them automatically" in w for w in result["warnings"])


def test_untagged_siblings_give_no_tags(writer, tags_confirmed):
    result = writer.add_matrix_variants(7, NEW_VARIANT, price=599.95, cost=63.5)
    assert "Tags" not in result["requests"][0]["body"]


def test_intake_new_variants_take_sibling_tags(writer, tagged, tags_confirmed):
    result = writer.intake_invoice(vendor_id=1, invoice_number="INV-TAGS", lines=[
        {"matrix_id": 7, "color": "Cobalt", "size": "44R", "fit": "Slim", "quantity": 1, "unit_cost": 63.5,
         "price": 599.95}])
    item = next(r for r in result["requests"] if r["path"].endswith("/Item.json"))
    assert item["body"]["Tags"] == {"tag": ["poly-viscose"]}
