"""Smoke-test the tools against the live account, the way Claude Desktop runs them.

    .venv\\Scripts\\python.exe check_tools.py               # read tools: size and first lines of each result
    .venv\\Scripts\\python.exe check_tools.py --full        # print each result in full
    .venv\\Scripts\\python.exe check_tools.py --dry-runs    # write tools, as dry runs only, printed in full

It starts server.py as a stdio subprocess (exactly like Claude Desktop does)
and calls each tool once with sample arguments. Read tools only send GET.
The write tools are only ever called with dry_run=true, which reads (GET) and
shows what would be sent, but sends nothing: nothing in Lightspeed changes.
Useful after bootstrap or after changing the code.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

from mcp import Client, StdioServerParameters

PROJECT_DIR = Path(__file__).resolve().parent

# (tool, arguments). IDs are from the Suits 4 Men account; change them if those records go away.
CALLS = [
    ("list_attribute_sets", {}),
    ("list_categories", {}),
    ("list_vendors", {"name_contains": "malone"}),
    ("search_items", {"description": "caravelli black slim", "limit": 5}),
    ("search_items", {"custom_sku": "007693"}),
    ("search_items", {"vendor": "San Malone", "category": "Suits", "limit": 3}),
    ("get_item", {"item_id": 1345}),
    ("get_item", {"sku": "007693"}),
    ("get_matrix", {"matrix_id": 7, "attribute_value": "Black", "in_stock_only": True}),
    ("list_purchase_orders", {"status": "not_finished", "limit": 5}),
    ("get_purchase_order", {"order_id": 853}),
    ("search_matrices", {"attribute_set_id": 5}),
    ("get_matrix", {"matrix_id": 8}),
    ("list_manufacturers", {}),
    ("get_stock_history", {"item_id": 1644, "days": 90}),
    ("get_stock_history", {"days": 30, "limit": 10}),
    ("get_sales_history", {"matrix_id": 7, "days": 90, "limit": 10}),
    ("get_sales_history", {"days": 30, "limit": 10}),
    ("find_stock_issues", {"limit_per_issue": 5}),
    ("inventory_counts", {}),
    ("inventory_counts", {"count_id": 25}),
    ("list_special_orders", {"status": "all", "limit": 3}),
    ("vendor_returns", {}),
    ("vendor_returns", {"vendor_return_id": 1}),
    ("search_vendor_catalog", {"vendor_number": "A0000200IX"}),
    ("api_get", {"path": "Manufacturer.json", "params": {"limit": "2"}}),
    ("suggest_custom_skus", {"count": 5}),
    ("show_write_log", {}),
    ("compare_snapshots", {"limit": 5}),  # local files only; take_snapshot is left out (it adds a folder)
    ("match_invoice_lines", {"vendor_id": 1, "lines": [
        {"matrix_id": 7, "color": "Black", "size": "42R", "fit": "Slim", "quantity": 2, "unit_cost": 63.5},
        {"description": "CARAVELLI 2PC SUIT BURGUNDY 58L", "quantity": 1, "unit_cost": 63.5}]}),
]

# Dry runs of the write tools on real records. dry_run is always True (checked below).
# 013471-013473 are the next unused custom SKUs (the highest in use on 2026-10-26 was 013470).
DRY_RUNS = [
    # A pants matrix for Caravelli; cost and price from the existing catch-all item 1485 "Caravelli Pants".
    ("create_matrix", {"description": "Caravelli Pants", "garment_type": "pants", "category_id": 79, "vendor_id": 1,
                       "manufacturer_id": 1, "default_cost": 20.5, "price": 159.95}),
    # Two sizes matrix 7 doesn't have yet. Its own price is 61.21 (= cost); its items sell at 599.95.
    ("add_matrix_variants", {"matrix_id": 7, "price": 599.95, "cost": 63.5, "variants": [
        {"color": "Cobalt", "size": "58R", "fit": "Slim", "custom_sku": "013471"},
        {"color": "Midnight Blue", "size": "58R", "fit": "Slim", "custom_sku": "013472"}]}),
    # One of the 57 items without a custom SKU.
    ("update_item_fields", {"item_id": 2321, "custom_sku": "013473"}),
    # Open PO 853 (San Malone).
    ("add_po_lines", {"order_id": 853, "lines": [{"item_id": 1345, "quantity": 1}]}),
    ("receive_po_lines", {"order_id": 853, "lines": [{"order_line_id": 2684, "quantity": 1},
                                                     {"order_line_id": 2692, "quantity": 3}]}),
    # Item 2321 "Caravelli Burgundy 58L Slim" is not in matrix 7, where it belongs.
    ("move_items_into_matrix", {"matrix_id": 7, "items": [{"item_id": 2321, "color": "Burgundy", "size": "58L",
                                                           "fit": "Slim"}]}),
    # Five of the items without a custom SKU; the SKUs are picked automatically.
    ("assign_custom_skus", {"items": [{"item_id": i} for i in (2321, 2322, 2323, 2324, 2325)]}),
    # Only to show the request: 1345 has stock 0 and is on no open PO.
    ("archive_item", {"item_id": 1345}),
    # A shirt outside any matrix, like item 1468 "Premium Dress Shirts" (Christopher Lena, vendor 11).
    ("create_items", {"items": [{"description": "Dress Shirt White 15.5 32/33", "category_id": 118, "vendor_id": 11,
                                 "cost": 15, "price": 59.5}]}),
    ("create_purchase_order", {"vendor_id": 1, "ref_num": "112500", "ship_cost": 58.82}),
    ("update_purchase_order", {"order_id": 853, "ordered_date": "2026-10-20"}),
    ("update_po_lines", {"order_id": 853, "lines": [{"order_line_id": 2695, "quantity": 2}]}),
    ("remove_po_lines", {"order_id": 853, "order_line_ids": [2695]}),
    ("set_vendor_numbers", {"vendor_id": 1, "items": [{"item_id": 1622, "vendor_number": "SM-CV-BEI-54R-C"}]}),
    # A made-up San Malone packing slip: an existing suit, a size matrix 7 lacks, and a new tie.
    ("intake_invoice", {"vendor_id": 1, "invoice_number": "SM-DRYRUN-1", "invoice_date": "2026-10-24",
                        "ship_cost": 25, "lines": [
                            {"matrix_id": 7, "color": "Black", "size": "42R", "fit": "Slim", "quantity": 2,
                             "unit_cost": 63.5},
                            {"matrix_id": 7, "color": "Cobalt", "size": "58R", "fit": "Slim", "quantity": 1,
                             "unit_cost": 63.5, "price": 599.95},
                            {"new_item": {"description": "Silk Tie Navy Paisley", "category_id": 63, "price": 39.95},
                             "quantity": 6, "unit_cost": 8}]}),
    ("update_matrix", {"matrix_id": 7, "description": "Caravelli Suit"}),
    ("update_matrix", {"matrix_id": 7, "price": 599.95}),
    ("create_vendor", {"name": "Bruno Piattelli", "account_number": "BP-1001"}),
    ("create_brand", {"name": "Bruno Piattelli"}),
    ("update_item_prices", {"items": [{"item_id": 2321, "msrp": 695}]}),
    ("set_reorder_levels", {"items": [{"item_id": 1644, "reorder_point": 1, "reorder_level": 3}]}),
    ("archive_purchase_order", {"order_id": 353}),          # 2024, nothing received
    ("archive_vendor", {"vendor_id": 700}),                 # active copy of archived "Broder Brothers" (101)
    ("create_category", {"name": "Hats", "parent_id": 60}),
    ("delete_category", {"category_id": 119}),              # empty
    ("create_inventory_count", {"name": "Neck ties check", "items": [{"item_id": 1387, "counted": 150}]}),
    # Belt and silk scarf: off the archived "Ties, Boxers & Socks" (51) onto the active "Ties, Boxers, and Socks" (2).
    ("reassign_items", {"items": [{"item_id": 1380, "vendor_id": 2}, {"item_id": 1390, "vendor_id": 2}]}),
]


async def main(full: bool, dry_runs: bool) -> int:
    server = StdioServerParameters(command=str(PROJECT_DIR / ".venv" / "Scripts" / "python.exe"),
                                   args=[str(PROJECT_DIR / "server.py")])
    calls = CALLS
    if dry_runs:
        calls = [(name, {**args, "dry_run": True}) for name, args in DRY_RUNS]
        assert all(args["dry_run"] is True and "confirm_code" not in args for _, args in calls)
        full = True
    failures = 0
    async with Client(server) as client:
        for name, args in calls:
            started = time.perf_counter()
            result = await client.call_tool(name, args)
            seconds = time.perf_counter() - started
            text = result.content[0].text if result.content else ""
            status = "ERROR" if result.is_error else "ok"
            failures += bool(result.is_error)
            print(f"\n=== {name} {json.dumps(args)} -> {status}, {len(text)} chars, {seconds:.1f}s")
            if dry_runs and not result.is_error:
                print(json.dumps(json.loads(text), indent=1, ensure_ascii=False))
            elif full or result.is_error:
                print(text)
            else:
                print(text[:600] + (" ..." if len(text) > 600 else ""))
    print(f"\n{len(calls) - failures}/{len(calls)} calls succeeded.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--full" in sys.argv, "--dry-runs" in sys.argv)))
