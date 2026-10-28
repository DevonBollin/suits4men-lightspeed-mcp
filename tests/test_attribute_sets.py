"""Attribute sets (matrix types MT1-MT4 in the draft, §2), and matrices on each.

Canned sets (test_writer.py): 1 "Color/Size" and 3 "Color" (Lightspeed's built-in sets, MT3 and MT4),
5 "Color/Size/Fit" (MT1), 6 "Color/Size/Width" (store-made, MT2), 7 "Neck/Sleeve" (store-made, no Color).
Matrix 13 "Stacy Adams Dickinson" (shoes) uses set 6; matrices 7, 8 and 12 use set 5; matrix 9
"Angelo Rossi" is a suit matrix on the built-in Color set.
"""

from __future__ import annotations

import httpx
import pytest

import tests.test_writer as canned
from lightspeed_mcp import rules
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


@pytest.fixture
def no_shoe_set(monkeypatch):
    """The store hasn't created its Color/Size/Width set yet."""
    monkeypatch.setattr(canned, "ATTRIBUTE_SETS", [s for s in canned.ATTRIBUTE_SETS if s["itemAttributeSetID"] != "6"])


# ---- the rules ------------------------------------------------------------------------------------------


SETS = {s["itemAttributeSetID"]: s for s in canned.ATTRIBUTE_SETS}


def test_each_garment_type_uses_its_matrix_type():
    g = rules.GARMENTS
    assert rules.check_matrix_attribute_set(SETS["5"], g["suit"]) == {1: "Color", 2: "Size", 3: "Fit"}
    assert rules.check_matrix_attribute_set(SETS["5"], g["dress shirt"]) == {1: "Color", 2: "Size", 3: "Fit"}
    assert rules.check_matrix_attribute_set(SETS["6"], g["shoes"]) == {1: "Color", 2: "Size", 3: "Width"}
    # The draft uses Lightspeed's built-in sets for MT3 and MT4.
    assert rules.check_matrix_attribute_set(SETS["1"], g["sweater"]) == {1: "Color", 2: "Size"}
    assert rules.check_matrix_attribute_set(SETS["3"], g["accessory"]) == {1: "Color"}
    with pytest.raises(RuleViolation, match=r"suit matrices use matrix type MT1 \(Color / Size / Fit"):
        rules.check_matrix_attribute_set(SETS["6"], g["suit"])
    with pytest.raises(RuleViolation, match="casual pants matrices use matrix type MT3"):
        rules.check_matrix_attribute_set(SETS["5"], g["casual pants"])
    with pytest.raises(RuleViolation, match="tuxedo matrices use matrix type MT1"):
        rules.check_matrix_attribute_set(SETS["3"], g["tuxedo"])        # a Color-only tux matrix


def test_a_set_without_color_is_refused_whatever_the_garment():
    for garment in (None, rules.GARMENTS["shoes"]):
        with pytest.raises(RuleViolation, match="no Color attribute"):
            rules.check_matrix_attribute_set(SETS["7"], garment)
    assert rules.check_matrix_attribute_set(SETS["1"], None) == {1: "Color", 2: "Size"}   # garment type unknown


@pytest.mark.parametrize("names, message", [([], "1 to 3"), (["Color", "A", "B", "C"], "1 to 3"),
                                            (["Neck", "Sleeve"], "Color"), (["Color", "color"], "twice"),
                                            (["Color", "Neck/Sleeve"], "no '/'")])
def test_attribute_name_rules(names, message):
    with pytest.raises(RuleViolation, match=message):
        rules.check_attribute_names(names)


# ---- create_attribute_set ----------------------------------------------------------------------------------


def test_create_the_shoe_set(writer, no_shoe_set):
    result = writer.create_attribute_set("Color/Size/Width", ["Color", "Size", "Width"])
    [request] = result["requests"]
    assert request["method"] == "POST" and request["path"].endswith("/ItemAttributeSet.json")
    assert request["body"] == {"name": "Color/Size/Width", "attributeName1": "Color", "attributeName2": "Size",
                               "attributeName3": "Width"}
    assert "matrix type MT2: create_matrix uses it for shoes" in result["summary"]
    assert any("contradictory" in w for w in result["warnings"])


def test_a_new_set_is_logged_with_its_id_and_seen_at_once(writer, fake_api, write_log, write_switch, no_shoe_set,
                                                          monkeypatch):
    assert "6" not in writer.reader.attribute_sets()                  # cached without the new set
    result = writer.create_attribute_set("Color/Size/Width", ["Color", "Size", "Width"])
    fake_api.handler = lambda request: httpx.Response(200, json={"ItemAttributeSet": {
        "itemAttributeSetID": "6", "name": "Color/Size/Width", "attributeName1": "Color", "attributeName2": "Size",
        "attributeName3": "Width", "system": "false", "archived": "false"}})
    turn_on(write_switch)
    done = writer.create_attribute_set("Color/Size/Width", ["Color", "Size", "Width"], dry_run=False,
                                       confirm_code=result["confirm_code"])
    assert done["done"][0]["affected_ids"] == {"ItemAttributeSet": {"itemAttributeSetID": "6"}}
    assert write_log.entries()[-1]["affected_ids"] == {"ItemAttributeSet": {"itemAttributeSetID": "6"}}
    monkeypatch.undo()                                                # Lightspeed now has set 6
    assert "6" in writer.reader.attribute_sets()                      # the write cleared the cache


def test_a_set_of_no_matrix_type_says_so(writer):
    result = writer.create_attribute_set("Color/Waist/Inseam", ["Color", "Waist", "Inseam"])
    assert "matches none of the draft's matrix types" in result["summary"]


@pytest.mark.parametrize("name, names, message", [
    ("color/size/width", ["Color", "Collar"], "already called"),
    ("Shoe sizes", ["color", "SIZE", "Width"], "already has these attributes"),
    ("Widths", ["Size", "Width"], "Color"),
])
def test_create_attribute_set_rules(writer, fake_api, name, names, message):
    with pytest.raises(RuleViolation, match=message):
        writer.create_attribute_set(name, names)
    assert fake_api.requests == []


# ---- update_attribute_set ------------------------------------------------------------------------------------


def test_rename_attributes_of_a_set_in_use_lists_its_matrices(writer):
    result = writer.update_attribute_set(6, attribute_names=["Color", "Size", "Wide"])
    assert result["requests"][0]["body"] == {"attributeName3": "Wide"}
    assert result["requests"][0]["path"].endswith("/ItemAttributeSet/6.json")
    assert any("13 'Stacy Adams Dickinson'" in w for w in result["warnings"])
    assert any("matrix type MT2" in w and "no longer counts" in w for w in result["warnings"])


def test_adding_an_attribute_warns(writer):
    result = writer.update_attribute_set(7, attribute_names=["Neck", "Sleeve", "Color"])  # set 7 is in no matrix
    assert result["requests"][0]["body"] == {"attributeName3": "Color"}


@pytest.mark.parametrize("set_id, kwargs, message", [
    (3, dict(name="Colour"), "built-in"),
    (6, dict(attribute_names=["Color", "Size"]), "can't be removed"),
    (6, dict(attribute_names=["Size", "Color", "Width"]), "Color must stay"),
    (6, dict(attribute_names=["Color", "Width", "Size"]), "would move from attribute 3 to attribute 2"),
    (6, dict(name="Color/Size/Fit"), "already called"),
    (6, dict(name="Color/Size/Width"), "nothing to change"),
    (99, dict(name="X"), "does not exist"),
])
def test_update_attribute_set_rules(writer, fake_api, set_id, kwargs, message):
    with pytest.raises(RuleViolation, match=message):
        writer.update_attribute_set(set_id, **kwargs)
    assert fake_api.requests == []


def test_renaming_set_5_attributes_warns_about_the_rules(writer):
    result = writer.update_attribute_set(5, attribute_names=["Color", "Size", "Cut"])
    assert any("matrix type MT1" in w for w in result["warnings"])


# ---- matrices of each type ------------------------------------------------------------------------------------


def test_the_set_follows_from_the_garment_type(writer):
    for description, garment, category, set_id in (
            ("Giorgio Fiorelli Suit", "suit", 83, "5"),
            ("Stacy Adams Madison", "shoes", 81, "6"),                  # MT2, the style name stands in
            ("Pellagio Quarter-Zip Sweater", "sweater", 130, "1")):     # MT3, the built-in Color/Size
        result = writer.create_matrix(description, garment, category, 21)
        assert result["requests"][0]["body"]["itemAttributeSetID"] == set_id, garment
    assert "shoes, matrix type MT2" in writer.create_matrix("Stacy Adams Madison", "shoes", 81, 21)["summary"]


def test_a_shoe_matrix_needs_the_shoe_set_first(writer, no_shoe_set):
    with pytest.raises(RuleViolation, match="no attribute set has those attributes yet"):
        writer.create_matrix("Stacy Adams Madison", "shoes", 81, 21)


@pytest.mark.parametrize("kwargs, message", [
    (dict(description="Giorgio Fiorelli Vest", garment_type="vest", category_id=116, attribute_set_id=6), "MT1"),
    (dict(description="Stacy Adams Madison", garment_type="shoes", category_id=81, attribute_set_id=3), "MT2"),
    (dict(description="Stacy Adams Madison", garment_type="shoes", category_id=81, attribute_set_id=7), "no Color"),
    (dict(description="Giorgio Fiorelli Suit", garment_type="suit", category_id=84), "holds tuxedo matrices"),
    (dict(description="Giorgio Fiorelli Sweater", garment_type="sweater", category_id=83), "holds suit matrices"),
])
def test_matrix_type_rules(writer, kwargs, message):
    with pytest.raises(RuleViolation, match=message):
        writer.create_matrix(vendor_id=21, **kwargs)


def test_a_category_that_holds_no_garment_type_is_flagged(writer, monkeypatch):
    monkeypatch.setattr(canned, "CATEGORIES", canned.CATEGORIES + [
        {"categoryID": "140", "name": "Clearance", "fullPathName": "Clearance", "archived": "false"}])
    result = writer.create_matrix("Pellagio Sweater", "sweater", 140, 21)
    assert any("doesn't say which garment type" in w for w in result["warnings"])


def test_shoe_variants_by_attribute_name(writer):
    result = writer.add_matrix_variants(13, [{"values": {"color": "Black", "Size": "11", "WIDTH": "W"},
                                              "custom_sku": "013471"}], price=129.95, cost=60)
    body = result["requests"][0]["body"]
    assert body["ItemAttributes"] == {"itemAttributeSetID": "6", "attribute1": "Black", "attribute2": "11",
                                      "attribute3": "W"}
    assert body["description"] == "Stacy Adams Dickinson Black 11 W"


@pytest.mark.parametrize("variant, message", [
    ({"values": {"Color": "Black", "Size": "11"}, "custom_sku": "013471"}, "needs a width"),
    ({"values": {"Color": "Black", "Size": "11", "Width": "M", "Fit": "Slim"}, "custom_sku": "013471"}, "it has no fit"),
    ({"values": {"Color": "Cognac", "Size": "10.5", "Width": "M"}, "custom_sku": "013471"}, "already has an item"),
    ({"values": {"Color": "Black", "Size": "11 1/2", "Width": "M"}, "custom_sku": "013471"}, "shoes sizes are"),
])
def test_shoe_variant_rules(writer, reads, variant, message):
    reads.data["Item"].append({"itemID": "3000", "description": "Stacy Adams Dickinson Cognac 10.5 M",
                               "customSku": "013200", "itemMatrixID": "13", "archived": "false",
                               "ItemAttributes": {"attribute1": "Cognac", "attribute2": "10.5", "attribute3": "M",
                                                  "itemAttributeSetID": "6"}})
    with pytest.raises(RuleViolation, match=message):
        writer.add_matrix_variants(13, [variant], price=129.95, cost=60)


def test_set_5_variants_still_take_color_size_fit(writer):
    result = writer.add_matrix_variants(7, [{"color": "Cobalt", "size": "44R", "fit": "Slim", "custom_sku": "013471"}],
                                        price=599.95, cost=63.5)
    assert result["requests"][0]["body"]["ItemAttributes"] == {
        "itemAttributeSetID": "5", "attribute1": "Cobalt", "attribute2": "44R", "attribute3": "Slim"}


def test_a_fit_is_never_blank(writer):
    with pytest.raises(RuleViolation, match="when the vendor names no fit, use 'Standard'"):
        writer.add_matrix_variants(7, [{"color": "Cobalt", "size": "44R", "custom_sku": "013471"}],
                                   price=599.95, cost=63.5)


def test_a_color_only_suit_matrix_takes_no_new_items(writer):
    """Matrix 9 'Angelo Rossi' is a suit on the built-in Color set: it is rebuilt, not added to."""
    with pytest.raises(RuleViolation, match="MT1.*can't take new items: build a new matrix"):
        writer.add_matrix_variants(9, [{"color": "Navy", "custom_sku": "013471"}], price=549.95, cost=60)


def test_intake_line_for_a_shoe_matrix(writer):
    result = writer.intake_invoice(vendor_id=1, invoice_number="SA-77", lines=[
        {"matrix_id": 13, "values": {"Color": "Black", "Size": "9.5", "Width": "W"}, "quantity": 3,
         "unit_cost": 60, "price": 129.95}])
    item = next(r for r in result["requests"] if r["path"].endswith("/Item.json"))
    assert item["body"]["ItemAttributes"]["attribute2"] == "9.5"
    assert item["body"]["description"] == "Stacy Adams Dickinson Black 9.5 W"
