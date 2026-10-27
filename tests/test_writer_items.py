"""Items outside a matrix: vendor, brand and category.

Same setup as test_writer.py: canned reads, a mocked write transport, the switch in a temp folder.
"""

from __future__ import annotations

import httpx
import pytest

from lightspeed_mcp.reader import InventoryReader
from lightspeed_mcp.rules import RuleViolation
from lightspeed_mcp.writer import InventoryWriter
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
