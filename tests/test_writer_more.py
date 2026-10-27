"""Vendor-number corrections, vendors, brands, categories and inventory counts.

Same setup as test_writer.py: canned reads, a mocked write transport, the switch in a temp folder.
"""

from __future__ import annotations

import json

import httpx
import pytest

import tests.test_writer as canned
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


# ---- correct_vendor_numbers ------------------------------------------------------------------------------


@pytest.fixture
def numbered(reads):
    by_id = {i["itemID"]: i for i in reads.data["Item"]}
    by_id["1346"]["ItemVendorNums"] = {"ItemVendorNum": [
        {"itemVendorNumID": "7", "vendorID": "1", "value": "CV-40RC", "cost": "63.5"},
        {"itemVendorNumID": "9", "vendorID": "21", "value": "OTHER-1"}]}
    by_id["1644"]["ItemVendorNums"] = {"ItemVendorNum": {"itemVendorNumID": "8", "vendorID": "1", "value": "CV-42RS"}}
    return reads


def test_correction_keeps_the_record_id_cost_and_other_vendors(writer, numbered):
    result = writer.correct_vendor_numbers(1, [{"item_id": 1346, "vendor_number": "CV-40R-C"}])
    assert result["requests"][0]["body"] == {"ItemVendorNums": {"ItemVendorNum": [
        {"itemVendorNumID": "7", "vendorID": "1", "value": "CV-40R-C", "cost": "63.5"},
        {"itemVendorNumID": "9", "vendorID": "21", "value": "OTHER-1"}]}}


@pytest.mark.parametrize("items, message", [
    ([{"item_id": 1345, "vendor_number": "X"}], "has no vendor number for this vendor"),
    ([{"item_id": 1346, "vendor_number": "CV-42RS"}], "already on item 1644"),
    ([{"item_id": 1346, "vendor_number": "CV-40RC"}], "already has vendor number"),
    ([{"item_id": 1346, "vendor_number": ""}], "can't be blank"),
])
def test_correction_rules(writer, numbered, items, message):
    with pytest.raises(RuleViolation, match=message):
        writer.correct_vendor_numbers(1, items)


# ---- vendors ------------------------------------------------------------------------------------------------


def test_rename_vendor_and_account_number(writer):
    result = writer.update_vendor(21, name="Fiorelli Supply Co.", account_number="FS-77")
    # "Co." doesn't make a different company name, but this is the vendor itself: allowed.
    assert result["requests"][0]["body"] == {"name": "Fiorelli Supply Co.", "accountNumber": "FS-77"}
    assert result["requests"][0]["path"].endswith("/Vendor/21.json")


def test_rename_vendor_to_another_vendors_name_is_refused(writer):
    with pytest.raises(RuleViolation, match="vendor 1 is already called 'San Malone'"):
        writer.update_vendor(21, name="San Malone")
    with pytest.raises(RuleViolation, match="nothing to change"):
        writer.update_vendor(21, name="Fiorelli Supply")


def test_archive_vendor(writer):
    result = writer.archive_vendor(21)        # no items, no open POs
    [request] = result["requests"]
    assert request["method"] == "DELETE" and request["path"].endswith("/Vendor/21.json") and request["body"] is None


def test_archive_vendor_in_use_is_refused(writer):
    with pytest.raises(RuleViolation, match="default vendor"):
        writer.archive_vendor(1)              # every canned item uses vendor 1


def test_archive_vendor_with_an_open_po_is_refused(writer, reads):
    reads.data["Order"].append({"orderID": "860", "vendorID": "21", "complete": "false", "archived": "false"})
    with pytest.raises(RuleViolation, match="unfinished purchase order"):
        writer.archive_vendor(21)


# ---- brands --------------------------------------------------------------------------------------------------


def test_rename_brand(writer):
    result = writer.rename_brand(5, "Ascott Browne")
    assert result["requests"][0]["body"] == {"name": "Ascott Browne"}
    assert result["requests"][0]["path"].endswith("/Manufacturer/5.json")


@pytest.mark.parametrize("brand_id, name, message", [
    (5, "Caravelli", "brand 1 is already called 'Caravelli'"),
    (5, "Giorgio Fiorrelli", "looks like an existing brand"),
    (5, "Ascott Brown", "already called 'Ascott Brown'"),
    (99, "X", "does not exist"),
])
def test_rename_brand_rules(writer, brand_id, name, message):
    with pytest.raises(RuleViolation, match=message):
        writer.rename_brand(brand_id, name)


# ---- categories --------------------------------------------------------------------------------------------------


@pytest.fixture
def tree(monkeypatch):
    monkeypatch.setattr(canned, "CATEGORIES", [
        {"categoryID": "79", "name": "Pants", "fullPathName": "Pants", "parentID": "0"},
        {"categoryID": "82", "name": "Suits", "fullPathName": "Suits", "parentID": "0"},
        {"categoryID": "83", "name": "Suit", "fullPathName": "Suits/Suit", "parentID": "82"},
        {"categoryID": "84", "name": "Tuxedos", "fullPathName": "Suits/Tuxedos", "parentID": "82"},
        {"categoryID": "90", "name": "Ponchos", "fullPathName": "Suits/Ponchos", "parentID": "82"},
    ])


def test_create_category(writer, tree):
    result = writer.create_category("Vests", parent_id=82)
    assert result["requests"][0]["body"] == {"name": "Vests", "fullPathName": "Suits/Vests", "parentID": "82"}
    assert writer.create_category("Hats")["requests"][0]["body"] == {"name": "Hats", "fullPathName": "Hats",
                                                                     "parentID": "0"}


@pytest.mark.parametrize("name, parent, message", [
    ("suit", 82, "already has this name"),
    ("Hats/Caps", None, "can't contain '/'"),
    ("Boys Suits", 82, "boys"),
    ("Vests", 999, "does not exist"),
])
def test_create_category_rules(writer, tree, name, parent, message):
    with pytest.raises(RuleViolation, match=message):
        writer.create_category(name, parent_id=parent)


def test_rename_category(writer, tree):
    result = writer.rename_category(84, "Tuxedo")
    assert result["requests"][0]["body"] == {"name": "Tuxedo", "fullPathName": "Suits/Tuxedo"}


def test_rename_category_with_subcategories_is_refused(writer, tree):
    with pytest.raises(RuleViolation, match="subcategories"):
        writer.rename_category(82, "Suiting")


def test_delete_only_an_empty_category(writer, tree):
    result = writer.delete_category(90)
    assert result["requests"][0]["method"] == "DELETE" and result["requests"][0]["path"].endswith("/Category/90.json")
    with pytest.raises(RuleViolation, match="still has items"):
        writer.delete_category(83)           # canned items are in 83
    with pytest.raises(RuleViolation, match="subcategories"):
        writer.delete_category(82)
    with pytest.raises(RuleViolation, match="matrix 12 uses"):
        writer.delete_category(79)           # matrix 12 "Caravelli Pants" is in 79


# ---- inventory counts -------------------------------------------------------------------------------------------------


def test_new_count_with_items(writer):
    result = writer.create_inventory_count("Neck ties 2026-10-27", [{"item_id": 1644, "counted": 0},
                                                                    {"item_id": 1186, "counted": 4}])
    reqs = result["requests"]
    assert [(r["method"], r["path"].split("/Account/12345/")[1]) for r in reqs] == [
        ("POST", "InventoryCount.json"), ("POST", "InventoryCountItem.json"), ("POST", "InventoryCountItem.json")]
    assert reqs[0]["body"] == {"name": "Neck ties 2026-10-27", "shopID": "1"}
    assert reqs[1]["body"] == {"inventoryCountID": "{new:count}", "itemID": "1644", "qty": "0", "employeeID": "1"}
    # On hand -27 and -16; counted 0 and 4: stock would change by +47 if reconciled.
    assert "+47" in result["summary"]
    assert any("on hand -27, counted 0 (+27)" in w for w in result["warnings"])
    assert "reconciled" in result["after"]


def test_count_is_never_reconciled(writer, fake_api, write_switch):
    turn_on(write_switch)
    replies = {"InventoryCount.json": {"InventoryCount": {"inventoryCountID": "26"}}}
    fake_api.handler = lambda request: httpx.Response(200, json=replies.get(
        request.url.path.split("/Account/12345/")[1], {"InventoryCountItem": {"inventoryCountItemID": "1"}}))
    args = ("Ties", [{"item_id": 1644, "counted": 3}])
    code = writer.create_inventory_count(*args)["confirm_code"]
    writer.create_inventory_count(*args, dry_run=False, confirm_code=code)
    paths = [r.url.path for r in fake_api.requests]
    assert not any("Reconcile" in p for p in paths)
    assert json.loads(fake_api.requests[1].content)["inventoryCountID"] == "26"


def test_add_to_an_existing_count(writer, reads):
    reads.data["InventoryCount"] = [{"inventoryCountID": "26", "name": "Ties", "archived": "false",
                                     "reconcileStatus": "awaiting_reconciliation"}]
    reads.data["InventoryCountItem"] = [{"inventoryCountItemID": "1", "inventoryCountID": "26", "itemID": "1644"}]
    result = writer.create_inventory_count(None, [{"item_id": 1186, "counted": 2}], count_id=26)
    assert [r["path"].split("/Account/12345/")[1] for r in result["requests"]] == ["InventoryCountItem.json"]
    with pytest.raises(RuleViolation, match="already in count 26"):
        writer.create_inventory_count(None, [{"item_id": 1644, "counted": 2}], count_id=26)
    reads.data["InventoryCount"][0]["reconcileStatus"] = "reconciliation_success"
    with pytest.raises(RuleViolation, match="already reconciled"):
        writer.create_inventory_count(None, [{"item_id": 1186, "counted": 2}], count_id=26)


@pytest.mark.parametrize("items, message", [
    ([{"item_id": 1644, "counted": -1}], "0 or more"),
    ([{"item_id": 1644, "counted": 1.5}], "0 or more"),
    ([{"item_id": 1644, "counted": 1}, {"item_id": 1644, "counted": 2}], "listed twice"),
    ([{"item_id": 1700, "counted": 1}], "archived"),
    ([], "at least one"),
])
def test_count_rules(writer, fake_api, items, message):
    with pytest.raises(RuleViolation, match=message):
        writer.create_inventory_count("Ties", items)
    assert fake_api.requests == []
