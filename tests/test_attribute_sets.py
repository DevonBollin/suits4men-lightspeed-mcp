"""Attribute sets (matrix types), and matrices on a set other than 5 Color/Size/Fit.

Canned sets (test_writer.py): 3 "Color" (built-in), 5 "Color/Size/Fit" (suiting), 6 "Color/Neck/Sleeve"
(store-made, for shirts), 7 "Neck/Sleeve" (store-made, no Color). Matrix 13 "Christopher Lena Dress Shirt"
uses set 6; matrices 7, 8 and 12 use set 5.
"""

from __future__ import annotations

import httpx
import pytest

from lightspeed_mcp import rules
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


# ---- the rules ------------------------------------------------------------------------------------------


SET5 = {"itemAttributeSetID": "5", "name": "Color/Size/Fit", "attributeName1": "Color", "attributeName2": "Size",
        "attributeName3": "Fit", "system": "false"}
SET6 = {"itemAttributeSetID": "6", "name": "Color/Neck/Sleeve", "attributeName1": "Color", "attributeName2": "Neck",
        "attributeName3": "Sleeve", "system": "false"}


def test_suiting_stays_on_set_5():
    assert rules.check_matrix_attribute_set(SET5, suiting=True) == {1: "Color", 2: "Size", 3: "Fit"}
    with pytest.raises(RuleViolation, match="Suits, tuxedos, vests, pants and sport coats use"):
        rules.check_matrix_attribute_set(SET6, suiting=True)


def test_other_garments_may_use_a_store_set_with_color():
    assert rules.check_matrix_attribute_set(SET6, suiting=False) == {1: "Color", 2: "Neck", 3: "Sleeve"}
    with pytest.raises(RuleViolation, match="built-in"):
        rules.check_matrix_attribute_set({**SET6, "itemAttributeSetID": "1", "system": "true"}, suiting=False)
    with pytest.raises(RuleViolation, match="no Color attribute"):
        rules.check_matrix_attribute_set({**SET6, "attributeName1": "Neck", "attributeName2": "Sleeve",
                                          "attributeName3": ""}, suiting=False)


@pytest.mark.parametrize("description, suiting", [("Caravelli Suit", True), ("Giorgio Fiorelli Tux", True),
                                                  ("Caravelli Pants", True), ("Christopher Lena Dress Shirt", False),
                                                  ("Florsheim Oxford Shoes", False)])
def test_is_suiting(description, suiting):
    assert rules.is_suiting(description) is suiting


@pytest.mark.parametrize("names, message", [([], "1 to 3"), (["Color", "A", "B", "C"], "1 to 3"),
                                            (["Neck", "Sleeve"], "Color"), (["Color", "color"], "twice"),
                                            (["Color", "Neck/Sleeve"], "no '/'")])
def test_attribute_name_rules(names, message):
    with pytest.raises(RuleViolation, match=message):
        rules.check_attribute_names(names)


# ---- create_attribute_set ----------------------------------------------------------------------------------


def test_create_attribute_set_payload(writer):
    result = writer.create_attribute_set("Color/Size/Width", ["Color", "Size", "Width"])
    [request] = result["requests"]
    assert request["method"] == "POST" and request["path"].endswith("/ItemAttributeSet.json")
    assert request["body"] == {"name": "Color/Size/Width", "attributeName1": "Color", "attributeName2": "Size",
                               "attributeName3": "Width"}
    assert any("contradictory" in w for w in result["warnings"])


@pytest.mark.parametrize("name, names, message", [
    ("color/neck/sleeve", ["Color", "Collar"], "already called"),
    ("Shirt sizes", ["color", "NECK", "Sleeve"], "already has these attributes"),
    ("Widths", ["Size", "Width"], "Color"),
])
def test_create_attribute_set_rules(writer, fake_api, name, names, message):
    with pytest.raises(RuleViolation, match=message):
        writer.create_attribute_set(name, names)
    assert fake_api.requests == []


# ---- update_attribute_set ------------------------------------------------------------------------------------


def test_rename_attributes_of_a_set_in_use_lists_its_matrices(writer):
    result = writer.update_attribute_set(6, attribute_names=["Color", "Collar", "Sleeve"])
    assert result["requests"][0]["body"] == {"attributeName2": "Collar"}
    assert result["requests"][0]["path"].endswith("/ItemAttributeSet/6.json")
    assert any("13 'Christopher Lena Dress Shirt'" in w for w in result["warnings"])


def test_adding_an_attribute_warns(writer):
    result = writer.update_attribute_set(7, attribute_names=["Neck", "Sleeve", "Color"])  # set 7 is in no matrix
    assert result["requests"][0]["body"] == {"attributeName3": "Color"}


@pytest.mark.parametrize("set_id, kwargs, message", [
    (3, dict(name="Colour"), "built-in"),
    (6, dict(attribute_names=["Color", "Neck"]), "can't be removed"),
    (6, dict(attribute_names=["Neck", "Color", "Sleeve"]), "Color must stay"),
    (6, dict(attribute_names=["Color", "Sleeve", "Neck"]), "would move from attribute 3 to attribute 2"),
    (6, dict(name="Color/Size/Fit"), "already called"),
    (6, dict(name="Color/Neck/Sleeve"), "nothing to change"),
    (99, dict(name="X"), "does not exist"),
])
def test_update_attribute_set_rules(writer, fake_api, set_id, kwargs, message):
    with pytest.raises(RuleViolation, match=message):
        writer.update_attribute_set(set_id, **kwargs)
    assert fake_api.requests == []


def test_renaming_set_5_attributes_warns_about_the_rules(writer):
    result = writer.update_attribute_set(5, attribute_names=["Color", "Size", "Cut"])
    assert any("suiting set" in w for w in result["warnings"])


# ---- matrices on another set ------------------------------------------------------------------------------------


def test_a_shirt_matrix_on_its_own_set(writer):
    result = writer.create_matrix("Christopher Lena Oxford Shirt", "shirt", 116, 21, attribute_set_id=6)
    body = result["requests"][0]["body"]
    assert body["itemAttributeSetID"] == "6"
    assert "Color / Neck / Sleeve" in result["summary"]


@pytest.mark.parametrize("kwargs, message", [
    (dict(description="Giorgio Fiorelli Vest", garment_type="vest", attribute_set_id=6), "use the store's set 5"),
    (dict(description="Christopher Lena Oxford Shirt", garment_type="shirt", attribute_set_id=3), "built-in"),
    (dict(description="Christopher Lena Oxford Shirt", garment_type="shirt", attribute_set_id=7), "no Color"),
])
def test_matrix_set_rules(writer, kwargs, message):
    with pytest.raises(RuleViolation, match=message):
        writer.create_matrix(category_id=116, vendor_id=21, **kwargs)


def test_shirt_variants_by_attribute_name(writer):
    result = writer.add_matrix_variants(13, [{"values": {"color": "Light Blue", "Neck": "16", "SLEEVE": "34/35"},
                                              "custom_sku": "013471"}], price=59.5, cost=15)
    body = result["requests"][0]["body"]
    assert body["ItemAttributes"] == {"itemAttributeSetID": "6", "attribute1": "Light Blue", "attribute2": "16",
                                      "attribute3": "34/35"}
    assert body["description"] == "Christopher Lena Dress Shirt Light Blue 16 34/35"


@pytest.mark.parametrize("variant, message", [
    ({"values": {"Color": "White", "Neck": "15"}, "custom_sku": "013471"}, "needs a sleeve"),
    ({"values": {"Color": "White", "Neck": "15", "Sleeve": "32/33", "Fit": "Slim"}, "custom_sku": "013471"},
     "it has no fit"),
    ({"values": {"Color": "White", "Neck": "15.5", "Sleeve": "32/33"}, "custom_sku": "013471"}, "already has an item"),
])
def test_shirt_variant_rules(writer, reads, variant, message):
    reads.data["Item"].append({"itemID": "3000", "description": "Christopher Lena Dress Shirt White 15.5 32/33",
                               "customSku": "013200", "itemMatrixID": "13", "archived": "false",
                               "ItemAttributes": {"attribute1": "White", "attribute2": "15.5", "attribute3": "32/33",
                                                  "itemAttributeSetID": "6"}})
    with pytest.raises(RuleViolation, match=message):
        writer.add_matrix_variants(13, [variant], price=59.5, cost=15)


def test_set_5_variants_still_take_color_size_fit(writer):
    result = writer.add_matrix_variants(7, [{"color": "Cobalt", "size": "44R", "fit": "Slim", "custom_sku": "013471"}],
                                        price=599.95, cost=63.5)
    assert result["requests"][0]["body"]["ItemAttributes"] == {
        "itemAttributeSetID": "5", "attribute1": "Cobalt", "attribute2": "44R", "attribute3": "Slim"}


def test_intake_line_for_a_shirt_matrix(writer):
    result = writer.intake_invoice(vendor_id=1, invoice_number="CL-77", lines=[
        {"matrix_id": 13, "values": {"Color": "Pink", "Neck": "16.5", "Sleeve": "34/35"}, "quantity": 3,
         "unit_cost": 15, "price": 59.5}])
    item = next(r for r in result["requests"] if r["path"].endswith("/Item.json"))
    assert item["body"]["ItemAttributes"]["attribute2"] == "16.5"
    assert item["body"]["description"] == "Christopher Lena Dress Shirt Pink 16.5 34/35"
