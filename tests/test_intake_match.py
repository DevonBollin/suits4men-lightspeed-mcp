"""Matching invoice lines to items (GET only, canned data from test_writer.py)."""

from __future__ import annotations

import pytest

from lightspeed_mcp.intake import InvoiceMatcher, match_report
from lightspeed_mcp.reader import InventoryReader
from tests.test_writer import CannedReads


@pytest.fixture
def reads():
    r = CannedReads()
    by_id = {i["itemID"]: i for i in r.data["Item"]}
    # San Malone's style number for Caravelli suits, saved on two of matrix 7's items.
    for item_id in ("1644", "1346"):
        by_id[item_id]["ItemVendorNums"] = {"ItemVendorNum": [{"vendorID": "1", "value": "CV-100"}]}
    by_id["1186"]["ItemVendorNums"] = {"ItemVendorNum": {"vendorID": "1", "value": "CS-ALL"}}  # one record = object
    return r


def match(reads, *lines, vendor_id=1):
    return InvoiceMatcher(InventoryReader(reads)).match(vendor_id, list(lines))


def test_by_item_id(reads):
    [m] = match(reads, {"item_id": 1345, "quantity": 1, "unit_cost": 63.5})
    assert m.status == "existing" and m.item["itemID"] == "1345"


def test_by_vendor_number_alone_when_it_is_one_item(reads):
    [m] = match(reads, {"vendor_number": "cs-all", "quantity": 1, "unit_cost": 60})
    assert m.status == "existing" and m.item["itemID"] == "1186" and m.vendor_number_known


def test_by_vendor_number_and_values(reads):
    [m] = match(reads, {"vendor_number": "CV-100", "color": "Black", "size": "40R", "fit": "Classic",
                        "quantity": 1, "unit_cost": 63.5})
    assert m.status == "existing" and m.item["itemID"] == "1346"


def test_vendor_number_points_to_its_matrix_for_a_new_variant(reads):
    [m] = match(reads, {"vendor_number": "CV-100", "color": "Cobalt", "size": "44R", "fit": "Slim",
                        "quantity": 2, "unit_cost": 63.5, "price": 599.95})
    assert m.status == "new_variant" and m.matrix["itemMatrixID"] == "7" and m.values == ("Cobalt", "44R", "Slim")


def test_a_code_used_in_several_matrices_asks_for_the_garment_type(reads):
    """One fabric code on the suits and the pants: the line's matrix decides."""
    by_id = {i["itemID"]: i for i in reads.data["Item"]}
    by_id["1801"]["ItemVendorNums"] = {"ItemVendorNum": [{"vendorID": "1", "value": "CV-100"}]}  # Caravelli Pants
    [m] = match(reads, {"vendor_number": "CV-100", "color": "Black", "size": "36", "quantity": 1, "unit_cost": 30})
    assert m.status == "unresolved" and "matrices 7 'Caravelli', 12 'Caravelli Pants'" in m.reason
    [m] = match(reads, {"vendor_number": "CV-100", "matrix_id": 12, "color": "Black", "size": "34",
                        "quantity": 1, "unit_cost": 30})
    assert m.status == "existing" and m.item["itemID"] == "1801"


def test_by_matrix_and_values_ignoring_case(reads):
    [m] = match(reads, {"matrix_id": 7, "color": "black", "size": "42r", "fit": "slim", "quantity": 1, "unit_cost": 63.5})
    assert m.status == "existing" and m.item["itemID"] == "1644" and not m.vendor_number_known


def test_new_variant_and_new_item(reads):
    variant, new = match(reads,
                         {"matrix_id": 8, "color": "Navy", "size": "42R", "fit": "Slim", "quantity": 1, "unit_cost": 63},
                         {"new_item": {"description": "Silk Tie Navy"}, "quantity": 6, "unit_cost": 8})
    assert variant.status == "new_variant" and new.status == "new_item"


@pytest.mark.parametrize("line, reason", [
    ({"description": "Caravelli Black Suit 46R", "quantity": 1, "unit_cost": 63.5}, "Not matched"),
    ({"item_id": 99999, "quantity": 1, "unit_cost": 1}, "not found"),
    ({"item_id": 1700, "quantity": 1, "unit_cost": 1}, "archived"),
    ({"matrix_id": 404, "color": "Black", "size": "42R", "quantity": 1, "unit_cost": 1}, "Matrix 404"),
    ({"matrix_id": 7, "color": "Black", "quantity": 1, "unit_cost": 1}, "Color / Size / Fit: give Size"),
])
def test_unresolved_lines_say_why(reads, line, reason):
    [m] = match(reads, line)
    assert m.status == "unresolved" and reason in m.reason


def test_candidates_by_description(reads):
    [m] = match(reads, {"description": "CARAVELLI BLACK 42R SLIM SUIT", "quantity": 1, "unit_cost": 63.5})
    assert m.candidates[0]["item_id"] == 1644


def test_vendor_numbers_are_per_vendor(reads):
    [m] = match(reads, {"vendor_number": "CS-ALL", "quantity": 1, "unit_cost": 60}, vendor_id=21)
    assert m.status == "unresolved"


def test_report_counts(reads):
    report = match_report(match(reads, {"item_id": 1345, "quantity": 1, "unit_cost": 1},
                                {"description": "mystery", "quantity": 1, "unit_cost": 1}))
    assert report["counts"] == {"existing": 1, "unresolved": 1}
    assert report["matches"][0]["item"]["item_id"] == 1345
