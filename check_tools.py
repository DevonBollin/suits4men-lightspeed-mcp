"""Smoke-test every read tool against the live account, the way Claude Desktop runs them.

    .venv\\Scripts\\python.exe check_tools.py            # summary: size and first lines of each result
    .venv\\Scripts\\python.exe check_tools.py --full     # print each result in full

It starts server.py as a stdio subprocess (exactly like Claude Desktop does)
and calls each tool once with sample arguments. Everything is a GET; nothing
in Lightspeed changes. Useful after bootstrap or after changing the code.
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
]


async def main(full: bool) -> int:
    server = StdioServerParameters(command=str(PROJECT_DIR / ".venv" / "Scripts" / "python.exe"),
                                   args=[str(PROJECT_DIR / "server.py")])
    failures = 0
    async with Client(server) as client:
        for name, args in CALLS:
            started = time.perf_counter()
            result = await client.call_tool(name, args)
            seconds = time.perf_counter() - started
            text = result.content[0].text if result.content else ""
            status = "ERROR" if result.is_error else "ok"
            failures += bool(result.is_error)
            print(f"\n=== {name} {json.dumps(args)} -> {status}, {len(text)} chars, {seconds:.1f}s")
            if full or result.is_error:
                print(text)
            else:
                print(text[:600] + (" ..." if len(text) > 600 else ""))
    print(f"\n{len(CALLS) - failures}/{len(CALLS)} calls succeeded.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--full" in sys.argv)))
