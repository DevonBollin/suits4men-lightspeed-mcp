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
