"""Stock history, sales history, counts, special orders, vendor returns, catalog and stock issues.

The canned client copies the live account's quirks (see NOTES.md):
InventoryLog ignores itemID, sales only count when the Sale is completed and
not voided, and VendorReturn reasons live in VendorReturnItemReason.reason.
"""

from __future__ import annotations

import json

import pytest

from lightspeed_mcp import reader as reader_module
from lightspeed_mcp.reader import InventoryReader, line_net_sales
from tests.test_reader import ITEMS, CannedClient, ids_in, make_item

LOGS = [  # newest first, mixed items, like the real store-wide log
    {"inventoryLogID": "3", "itemID": "1644", "qohChange": "-1", "reason": "removeInventoryForTransaction",
     "causedNegative": "true", "saleID": "25259", "createTime": "2026-10-26T21:37:00+00:00", "automated": "false"},
    {"inventoryLogID": "2", "itemID": "9999", "qohChange": "-2", "reason": "removeInventoryForTransaction",
     "causedNegative": "true", "saleID": "25262", "createTime": "2026-10-26T20:00:00+00:00", "automated": "false"},
    {"inventoryLogID": "1", "itemID": "1644", "qohChange": "3", "reason": "checkinPurchaseOrderInventory",
     "causedNegative": "false", "orderID": "853", "createTime": "2026-10-20T19:32:17+00:00", "automated": "false"},
]
SALE_LINES = [
    {"saleLineID": "1", "saleID": "100", "itemID": "1644", "unitQuantity": "2", "calcTotal": "110", "calcTax1": "10",
     "calcTax2": "0", "avgCost": "60", "createTime": "2026-10-01T10:00:00+00:00"},
    {"saleLineID": "2", "saleID": "101", "itemID": "1644", "unitQuantity": "-1", "calcTotal": "-55", "calcTax1": "-5",
     "calcTax2": "0", "avgCost": "60", "createTime": "2026-10-05T10:00:00+00:00"},
    {"saleLineID": "3", "saleID": "102", "itemID": "1345", "unitQuantity": "1", "calcTotal": "660", "calcTax1": "60",
     "calcTax2": "0", "avgCost": "65", "createTime": "2026-10-06T10:00:00+00:00"},
    {"saleLineID": "4", "saleID": "103", "itemID": "1345", "unitQuantity": "5", "calcTotal": "999", "calcTax1": "0",
     "calcTax2": "0", "avgCost": "65", "createTime": "2026-10-07T10:00:00+00:00"},  # on a voided sale
]
SALES = [{"saleID": "100", "completed": "true", "voided": "false"},
         {"saleID": "101", "completed": "true", "voided": "false"},
         {"saleID": "102", "completed": "true", "voided": "false"},
         {"saleID": "103", "completed": "true", "voided": "true"}]


class HistoryClient(CannedClient):
    def get_all(self, path, params=None, max_records=None):
        params = dict(params or {})
        if path == "InventoryLog.json":
            self.calls.append((path, params))
            return list(LOGS), False  # itemID is ignored, as on the live API
        if path == "SaleLine.json":
            self.calls.append((path, params))
            rows = SALE_LINES
            if "itemID" in params:
                rows = [line for line in rows if line["itemID"] in ids_in(params["itemID"])]
            return list(rows), False
        if path == "Sale.json":
            self.calls.append((path, params))
            return [s for s in SALES if s["saleID"] in ids_in(params["saleID"])], False
        if path == "InventoryCountCalc.json":
            self.calls.append((path, params))
            return [{"itemID": "1644", "calcQoh": "2", "countedQoh": "0", "inventoryCountID": "25"},
                    {"itemID": "1345", "calcQoh": "1", "countedQoh": "1", "inventoryCountID": "25"}], False
        if path == "CatalogVendorItem.json":
            self.calls.append((path, params))
            return [{"catalogVendorItemID": "5", "vendorNumber": "T-500", "description": "Slim suit", "brand": "Tallia",
                     "colorName": "Navy", "sizeName": "40R", "cost": "120", "msrp": "450", "archived": "false"}], False
        return super().get_all(path, params, max_records)

    def get(self, path, params=None):
        if path.startswith("InventoryCount/"):
            self.calls.append((path, dict(params or {})))
            return {"InventoryCount": {"inventoryCountID": "25", "name": "DAMAGED GOODS", "shopID": "1",
                                       "reconcileStatus": "reconciliation_success"}}
        if path.startswith("VendorReturn/"):
            self.calls.append((path, dict(params or {})))
            return {"VendorReturn": {
                "vendorReturnID": "1", "vendorID": "7", "status": "done", "refNum": "RMA # 3185",
                "sentDate": "2023-12-21T06:00:00+00:00", "closedDate": "2024-01-23T06:00:00+00:00",
                "VendorReturnItems": {"VendorReturnItem": [{
                    "vendorReturnItemID": "1", "itemID": "1644", "Item": ITEMS[1], "quantity": "2", "cost": "63.5",
                    "subtotal": "127", "orderID": "15",
                    "VendorReturnItemReason": {"vendorReturnItemReasonID": "2", "reason": "Defective"}}]}}}
        return super().get(path, params)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(reader_module, "utc_now", lambda: datetime(2026, 10, 27, tzinfo=timezone.utc))


def test_stock_history_filters_the_store_wide_log_to_the_item():
    client = HistoryClient()
    result = InventoryReader(client).get_stock_history(item_id=1644, days=30)
    log_query = client.queries("InventoryLog.json")[0]
    assert log_query["createTime"] == ">=,2026-09-27T00:00:00+00:00"
    assert "itemID" not in log_query  # Lightspeed ignores it, so we don't pretend
    assert result["changes"] == 2 and result["net_qoh_change"] == 2
    assert result["changes_that_went_negative"] == 1
    assert result["by_reason"]["checkinPurchaseOrderInventory"] == {"changes": 1, "qoh_change": 3}
    assert result["entries"][0]["sale_id"] == 25259
    assert result["target"]["item_id"] == 1644


def test_store_wide_stock_history_ranks_items_that_go_negative():
    result = InventoryReader(HistoryClient()).get_stock_history(days=7)
    assert result["target"] == {"scope": "whole store"}
    assert result["changes"] == 3
    rows = result["items_going_negative_most"]["rows"]
    assert {row[0] for row in rows} == {1644, 9999}


def test_sales_history_counts_only_completed_unvoided_sales():
    result = InventoryReader(HistoryClient()).get_sales_history(matrix_id=7, days=90)
    assert result["units_sold"] == 3  # 2 of 1644 + 1 of 1345; the voided sale of 5 is ignored
    assert result["units_returned"] == 1
    assert result["net_units"] == 2
    assert result["net_sales"] == round(100 - 50 + 600, 2)
    assert result["net_units_by_attribute"]["Fit"] == {"Slim": 1, "Classic": 1}
    assert result["items"]["rows"][0][:2] == [1644, "002427"]


def test_line_net_sales_is_after_discount_before_tax():
    # A real discounted line: calcSubtotal 159.95, calcLineDiscount 79.98, displayableSubtotal 79.97.
    line = {"calcTotal": "88.0573661", "calcTax1": "8.0873661", "calcTax2": "0", "calcSubtotal": "159.95"}
    assert round(line_net_sales(line), 2) == 79.97


def test_inventory_count_shows_differences():
    result = InventoryReader(HistoryClient()).inventory_counts(count_id=25)
    assert result["items_counted"] == 2 and result["items_with_differences"] == 1 and result["net_difference"] == -2
    assert result["items"]["rows"] == [[1644, "002427", "Caravelli Black 42R Slim", "Black / 42R / Slim", 2, 0, -2]]
    assert result["reconcile_status"] == "reconciliation_success"


def test_vendor_return_detail_has_reason_and_item():
    result = InventoryReader(HistoryClient()).vendor_returns(vendor_return_id=1)
    line = result["lines"][0]
    assert line["reason"] == "Defective"
    assert line["item"]["custom_sku"] == "002427"
    assert (result["status"], result["closed"], result["quantity"]) == ("done", "2024-01-23T06:00:00+00:00", 2)


def test_catalog_lookup_is_exact_only():
    client = HistoryClient()
    result = InventoryReader(client).search_vendor_catalog(vendor_number=" T-500 ")
    assert client.queries("CatalogVendorItem.json")[0] == {"vendorNumber": "T-500"}
    assert result["catalog_items"][0]["color"] == "Navy"
    with pytest.raises(ValueError):
        InventoryReader(client).search_vendor_catalog()


def test_find_stock_issues():
    items = ITEMS + [make_item(1700, "Black", "46R", "Slim", -3, "002427"),  # duplicate SKU, negative
                     {**make_item(1800, "Navy", "40R", "Slim", 0), "itemMatrixID": "0", "defaultCost": "0"}]

    class IssuesClient(HistoryClient):
        def get_all(self, path, params=None, max_records=None):
            if path == "Item.json":
                return list(items), False
            return super().get_all(path, params, max_records)

    result = InventoryReader(IssuesClient()).find_stock_issues()
    issues = result["issues"]
    assert result["scanned"] == 5
    assert issues["negative_stock"]["count"] == 1 and issues["negative_stock"]["total_units_negative"] == -3
    assert issues["missing_custom_sku"]["count"] == 2
    assert issues["duplicate_custom_sku"]["groups"][0]["custom_sku"] == "002427"
    assert issues["no_default_cost"]["count"] == 1
    assert issues["not_in_a_matrix"]["count"] == 1
    assert issues["at_or_below_reorder_point"]["count"] == 4  # reorderPoint 1, qoh <= 1
    json.dumps(result)


def test_purchase_order_includes_receipts():
    class ShipmentClient(HistoryClient):
        def get(self, path, params=None):
            if path.startswith("Order/"):
                assert "OrderShipments" in params["load_relations"]
                return {"Order": {"orderID": "852", "vendorID": "7", "complete": "true", "OrderShipments": {
                    "OrderShipment": {"orderShipmentID": "820", "status": "received", "totalQtyReceived": "1",
                                      "receptionDate": "2026-10-20T19:32:17+00:00", "totalCost": "63.5"}}}}
            return super().get(path, params)

    po = InventoryReader(ShipmentClient()).get_purchase_order(852)
    assert po["receipts"] == [{"shipment_id": 820, "status": "received", "received": "2026-10-20T19:32:17+00:00",
                               "quantity_received": 1, "total_cost": 63.5}]


def test_special_orders_read_item_through_sale_line():
    class SpecialClient(HistoryClient):
        def get_all(self, path, params=None, max_records=None):
            if path == "SpecialOrder.json":
                assert params["completed"] == "false"
                return [{"specialOrderID": "8", "unitQuantity": "1", "completed": "false", "contacted": "false",
                         "status": "Not Ordered", "customerID": "13773", "saleLineID": "216", "orderLineID": "0",
                         "SaleLine": {"itemID": "1644", "unitPrice": "50", "Item": ITEMS[1]}}], False
            return super().get_all(path, params, max_records)

    result = InventoryReader(SpecialClient()).list_special_orders()
    order = result["special_orders"][0]
    assert order["status"] == "Not Ordered"
    assert order["item"]["description"] == "Caravelli Black 42R Slim"
    assert "order_line_id" not in order  # 0 means none
