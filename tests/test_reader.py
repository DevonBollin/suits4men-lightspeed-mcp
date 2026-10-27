"""Tool logic: query building and trimming, with a canned (fake) client.

The canned payloads are modelled on real responses from the store's account
(see NOTES.md), e.g. attribute set 5 "Color/Size/Fit" and ItemShops with a
shopID 0 total row.
"""

from __future__ import annotations

import json
import re

import pytest

from lightspeed_mcp.client import LightspeedClient
from lightspeed_mcp.errors import ApiError
from lightspeed_mcp.reader import InventoryReader

ATTRIBUTE_SETS = [
    {"itemAttributeSetID": "2", "name": "Size", "attributeName1": "", "attributeName2": "Size", "attributeName3": "",
     "system": "true", "archived": "false"},
    {"itemAttributeSetID": "5", "name": "Color/Size/Fit", "attributeName1": "Color", "attributeName2": "Size",
     "attributeName3": "Fit", "system": "false", "archived": "false"},
]
VENDORS = [{"vendorID": "7", "name": "Tallia Orange", "archived": "false"},
           {"vendorID": "8", "name": "Tallia Black", "archived": "false"},
           {"vendorID": "9", "name": "Statement", "archived": "false"}]
CATEGORIES = [{"categoryID": "82", "name": "Suits", "fullPathName": "Suits", "parentID": "0"},
              {"categoryID": "83", "name": "Suit", "fullPathName": "Suits/Suit", "parentID": "82"},
              {"categoryID": "90", "name": "Pants", "fullPathName": "Pants", "parentID": "0"}]


def make_item(item_id, color, size, fit, qoh, sku=""):
    return {
        "itemID": str(item_id), "systemSku": f"2100000{item_id}", "customSku": sku, "upc": "", "manufacturerSku": "",
        "description": f"Caravelli {color} {size} {fit}", "itemMatrixID": "7", "defaultVendorID": "7",
        "categoryID": "83", "manufacturerID": "1", "defaultCost": "65.95", "archived": "false",
        "ItemAttributes": {"attribute1": color, "attribute2": size, "attribute3": fit, "itemAttributeSetID": "5"},
        "ItemShops": {"ItemShop": [{"shopID": "0", "qoh": str(qoh), "averageCost": "60"},
                                   {"shopID": "1", "qoh": str(qoh), "reorderPoint": "1", "averageCost": "0"}]},
        "Prices": {"ItemPrice": [{"amount": "599.95", "useType": "Default"}, {"amount": "695", "useType": "MSRP"}]},
    }


ITEMS = [make_item(1345, "Beige", "50S", "Classic", 0, "012911"),
         make_item(1644, "Black", "42R", "Slim", 2, "002427"),
         make_item(1650, "Black", "44R", "Slim", 0)]


def ids_in(value: str) -> set[str]:
    return set(re.findall(r"\d+", value))


class CannedClient:
    """Stands in for LightspeedClient, records requests, and answers like the live account."""

    record_key = staticmethod(LightspeedClient.record_key)

    def __init__(self, matrix_readable=False):
        self.calls = []
        self.matrix_readable = matrix_readable

    def get_all(self, path, params=None, max_records=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if path == "Vendor.json":
            rows = VENDORS
            if "vendorID" in params:
                rows = [v for v in rows if v["vendorID"] in ids_in(params["vendorID"])]
            if "name" in params:
                words = params["name"].removeprefix("~,").strip("%").split("%")
                rows = [v for v in rows if all(w.lower() in v["name"].lower() for w in words)]
            return list(rows), False
        if path == "Item.json":
            rows = ITEMS
            if "itemID" in params:
                rows = [i for i in rows if i["itemID"] in ids_in(params["itemID"])]
            return list(rows), False
        if path == "OrderLine.json":
            return [{"orderLineID": "1", "orderID": "853", "itemID": "1644", "quantity": "2", "checkedIn": "0",
                     "price": "63.5", "total": "127"}], False
        if path == "Shop.json":
            raise ApiError(403, "Lightspeed returned HTTP 403: Not Authorized")
        table = {"ItemAttributeSet.json": ATTRIBUTE_SETS, "Category.json": CATEGORIES}
        return list(table.get(path, [])), False

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path.startswith("Item/"):
            return {"Item": ITEMS[1]}
        if path.startswith("ItemMatrix/"):
            if not self.matrix_readable:
                raise ApiError(403, "Lightspeed returned HTTP 403: Not Authorized")
            return {"ItemMatrix": {"itemMatrixID": "7", "description": "Caravelli Suit", "itemAttributeSetID": "5",
                                   "defaultVendorID": "7", "categoryID": "83"}}
        if path.startswith("Order/"):
            return {"Order": {"orderID": "853", "vendorID": "7", "complete": "false", "totalCost": "1166.99",
                              "Note": {"note": "Fall restock"}}}
        return {}

    def queries(self, path):
        return [params for p, params in self.calls if p == path]


def test_description_words_become_an_in_order_like_filter():
    client = CannedClient()
    InventoryReader(client).search_items(description="navy  slim")
    assert client.queries("Item.json")[0]["description"] == "~,%navy%slim%"


def test_vendor_name_resolves_to_ids_through_the_api():
    client = CannedClient()
    InventoryReader(client).search_items(vendor="tallia")
    assert client.queries("Vendor.json")[0]["name"] == "~,%tallia%"
    assert client.queries("Item.json")[0]["defaultVendorID"] == "IN,[7,8]"


def test_category_includes_subcategories():
    client = CannedClient()
    InventoryReader(client).search_items(category="82")
    assert client.queries("Item.json")[0]["categoryID"] == "IN,[82,83]"


def test_sku_wildcard_uses_like_operator():
    client = CannedClient()
    InventoryReader(client).search_items(custom_sku="0129%")
    assert client.queries("Item.json")[0]["customSku"] == "~,0129%"


def test_search_needs_a_filter():
    with pytest.raises(ValueError):
        InventoryReader(CannedClient()).search_items()


def test_search_result_is_trimmed_and_named():
    result = InventoryReader(CannedClient()).search_items(description="caravelli")
    assert result["items"][2] == {
        "item_id": 1650, "description": "Caravelli Black 44R Slim", "custom_sku": None, "upc": None,
        "manufacturer_sku": None, "system_sku": "21000001650", "matrix_id": 7,
        "attributes": {"Color": "Black", "Size": "44R", "Fit": "Slim"},
        "vendor": "Tallia Orange", "vendor_id": 7, "category_id": 83, "qoh": 0, "price": 599.95,
        "default_cost": 65.95,
    }
    json.dumps(result)


def test_vendor_names_are_fetched_only_for_needed_ids_and_cached():
    client = CannedClient()
    reader = InventoryReader(client)
    reader.search_items(description="caravelli")
    reader.search_items(description="black")
    vendor_queries = client.queries("Vendor.json")
    assert vendor_queries == [{"vendorID": "7", "archived": "true"}]


def test_get_item_survives_unreadable_matrix_and_shop():
    detail = InventoryReader(CannedClient(matrix_readable=False)).get_item(item_id=1644)
    assert detail["matrix"] == {"matrix_id": 7}
    assert detail["attribute_set"] == {"attribute_set_id": 5, "name": "Color/Size/Fit"}
    assert detail["stock"] == [{"shop_id": 1, "qoh": 2, "reorder_point": 1}]  # shopID 0 total row left out
    assert (detail["qoh"], detail["average_cost"]) == (2, 60.0)  # average cost comes from the total row
    assert detail["prices"] == {"Default": 599.95, "MSRP": 695.0}


def test_get_matrix_falls_back_to_children_when_matrix_is_forbidden():
    result = InventoryReader(CannedClient(matrix_readable=False)).get_matrix(7)
    assert "not readable" in result["note"]
    assert result["attribute_set"]["attributes"] == ["Color", "Size", "Fit"]
    assert result["attribute_values"] == {"Color": ["Beige", "Black"], "Size": ["42R", "44R", "50S"],
                                          "Fit": ["Classic", "Slim"]}
    assert result["vendor"] == {"vendor_id": 7, "name": "Tallia Orange"}
    assert result["category_path"] == "Suits/Suit"
    assert (result["item_count"], result["items_missing_custom_sku"], result["total_qoh"]) == (3, 1, 2)
    table = result["items"]
    assert table["columns"] == ["item_id", "custom_sku", "upc", "manufacturer_sku", "Color", "Size", "Fit",
                                "qoh", "price", "default_cost"]
    assert table["rows"][0] == [1345, "012911", None, None, "Beige", "50S", "Classic", 0, 599.95, 65.95]


def test_get_matrix_uses_the_matrix_record_when_readable():
    result = InventoryReader(CannedClient(matrix_readable=True)).get_matrix(7)
    assert result["description"] == "Caravelli Suit"
    assert "note" not in result


def test_get_matrix_filters_narrow_only_the_table():
    result = InventoryReader(CannedClient()).get_matrix(7, attribute_value="black", in_stock_only=True)
    assert result["item_count"] == 3
    assert result["items_shown"] == 1
    assert result["items"]["rows"][0][0] == 1644


def test_size_only_attribute_set_keeps_its_position():
    from lightspeed_mcp import format as fmt
    item = {"ItemAttributes": {"attribute1": "", "attribute2": "34", "attribute3": "", "itemAttributeSetID": "2"}}
    sets = {s["itemAttributeSetID"]: s for s in ATTRIBUTE_SETS}
    assert fmt.attribute_values(item, sets) == {"Size": "34"}
    assert fmt.matrix_from_children(9, [item], sets, {})["attribute_values"] == {"Size": ["34"]}


def test_purchase_order_lines_get_item_details_from_a_second_query():
    client = CannedClient()
    po = InventoryReader(client).get_purchase_order(853)
    assert client.queries("OrderLine.json")[0] == {"orderID": "853"}  # no load_relations: Lightspeed refuses Item
    assert client.queries("Item.json")[0]["itemID"] == "1644"
    assert po["lines"][0]["description"] == "Caravelli Black 42R Slim"
    assert po["lines"][0]["custom_sku"] == "002427"
    assert po["note"] == "Fall restock"
    assert po["total_cost"] == 1166.99
    assert po["vendor"] == "Tallia Orange"
