"""MCP server for Claude Desktop: read-only tools for the Suits 4 Men Lightspeed account.

Claude Desktop starts this file over stdio (see README.md). Do not print() in
here: stdout carries the MCP protocol. Logging goes to stderr, which Claude
Desktop saves in its MCP logs (%APPDATA%\\Claude\\logs\\mcp-server-lightspeed.log).

The tool logic lives in lightspeed_mcp/reader.py. This file only describes
the tools for Claude and turns results into compact JSON text.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from typing import Annotated

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from lightspeed_mcp.client import LightspeedClient
from lightspeed_mcp.config import HTTP_TIMEOUT_SECONDS
from lightspeed_mcp.errors import LightspeedError, NotBootstrappedError
from lightspeed_mcp.reader import PO_STATUSES, InventoryReader
from lightspeed_mcp.secret_store import SecretStore
from lightspeed_mcp.tokens import TokenManager

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every request URL at INFO; keep the log about our own events.
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = MCPServer(
    "suits4men-lightspeed",
    instructions=(
        "Read-only access to the Suits 4 Men Lightspeed Retail (R-Series) account: items, item matrices, "
        "attribute sets, vendors, manufacturers, categories, purchase orders, stock and stock history, sales "
        "history, inventory counts, special orders, vendor returns and vendor catalogs. Nothing here can change "
        "Lightspeed. Results are trimmed JSON with IDs; long lists come as {columns, rows} tables. Use api_get "
        "for any field the other tools leave out."
    ),
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

_reader: InventoryReader | None = None
_reader_lock = threading.Lock()


def reader() -> InventoryReader:
    """Build the client on first use, so the server starts even before bootstrap.py has run."""
    global _reader
    with _reader_lock:
        if _reader is None:
            store = SecretStore()
            app = store.load_app()
            if app is None or not app.account_id:
                raise NotBootstrappedError("This computer has not been connected to Lightspeed yet.")
            tokens = TokenManager(store, httpx.Client(timeout=HTTP_TIMEOUT_SECONDS))
            _reader = InventoryReader(LightspeedClient(app.account_id, tokens))
        return _reader


def run(call) -> str:
    """Run one tool call; return compact JSON, or raise ToolError with a readable message."""
    try:
        result = call(reader())
    except (LightspeedError, ValueError) as e:
        raise ToolError(str(e)) from e
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def tool(fn):
    return mcp.tool(annotations=READ_ONLY, structured_output=False)(fn)


# ---- items ------------------------------------------------------------------------


@tool
def search_items(
    description: Annotated[str | None, Field(description="Words in the item description, in this order, case-insensitive. 'navy slim' finds 'Navy Super 120s Slim Suit'.")] = None,
    upc: Annotated[str | None, Field(description="UPC. Exact match; use % as a wildcard, e.g. '0123%'.")] = None,
    custom_sku: Annotated[str | None, Field(description="Custom SKU. Exact match; % is a wildcard.")] = None,
    manufacturer_sku: Annotated[str | None, Field(description="Manufacturer SKU (vendor style code). Exact match; % is a wildcard.")] = None,
    vendor: Annotated[str | None, Field(description="Default vendor: a vendor ID, or part of the vendor name.")] = None,
    category: Annotated[str | None, Field(description="A category ID or part of its path; subcategories are included.")] = None,
    include_archived: Annotated[bool, Field(description="Also return archived items.")] = False,
    limit: Annotated[int, Field(description="Maximum items to return (1-500).")] = 50,
) -> str:
    """Find items. Filters combine with AND; give at least one.

    Each result has item_id, description, SKUs (custom_sku/upc/manufacturer_sku are
    shown as null when blank), matrix_id, attribute values (e.g. Color/Size/Fit),
    vendor, category_id, total quantity on hand, default price and default cost.
    Use get_item for full detail or get_matrix for a whole matrix.
    """
    return run(lambda r: r.search_items(description, upc, custom_sku, manufacturer_sku, vendor, category,
                                        include_archived, limit))


@tool
def get_item(
    item_id: Annotated[int | None, Field(description="Lightspeed itemID.")] = None,
    sku: Annotated[str | None, Field(description="Instead of item_id: a custom SKU, UPC, EAN, manufacturer SKU or system SKU.")] = None,
) -> str:
    """Full detail for one item: SKUs, matrix, attribute set and values, vendor and vendor
    item numbers, category, manufacturer, cost, prices, and stock per shop (qoh, reorder
    point/level, average cost)."""
    return run(lambda r: r.get_item(item_id, sku))


@tool
def get_matrix(
    matrix_id: Annotated[int, Field(description="Lightspeed itemMatrixID (search_items returns it as matrix_id).")],
    attribute_value: Annotated[str | None, Field(description="Only list children with this exact attribute value, e.g. 'Black', '42R' or 'Slim'.")] = None,
    in_stock_only: Annotated[bool, Field(description="Only list children with quantity on hand above 0.")] = False,
    include_archived: Annotated[bool, Field(description="Also include archived child items.")] = False,
) -> str:
    """An item matrix: its attribute set, the attribute values in use, counts (children,
    children missing a custom SKU, total quantity on hand), and the child items as a table
    (columns + rows) with SKUs, attribute values, quantity on hand, price and default cost.
    The counts always cover the whole matrix; the filters only narrow the table."""
    return run(lambda r: r.get_matrix(matrix_id, attribute_value, in_stock_only, include_archived))


@tool
def list_attribute_sets(
    include_archived: Annotated[bool, Field(description="Include archived attribute sets.")] = False,
) -> str:
    """Attribute sets (the matrix types, e.g. Color/Size/Fit) with their attribute names."""
    return run(lambda r: r.list_attribute_sets(include_archived))


# ---- vendors and categories ---------------------------------------------------------------


@tool
def list_vendors(
    name_contains: Annotated[str | None, Field(description="Only vendors whose name contains these words, in order.")] = None,
    include_archived: Annotated[bool, Field(description="Include archived vendors.")] = False,
    limit: Annotated[int, Field(description="Maximum vendors to return (1-1000).")] = 200,
) -> str:
    """Vendors with vendor_id, name and account number, sorted by name. The account has
    several hundred vendors, so pass name_contains when looking for one."""
    return run(lambda r: r.list_vendors(name_contains, include_archived, limit))


@tool
def list_categories(
    name_contains: Annotated[str | None, Field(description="Only categories whose full path contains this text.")] = None,
    include_archived: Annotated[bool, Field(description="Include archived categories.")] = False,
) -> str:
    """The category tree: category_id, name, full path, parent_id and depth, sorted by path."""
    return run(lambda r: r.list_categories(name_contains, include_archived))


# ---- purchase orders ------------------------------------------------------------------------


@tool
def list_purchase_orders(
    vendor: Annotated[str | None, Field(description="A vendor ID or part of the vendor name.")] = None,
    status: Annotated[str, Field(description=f"One of: {', '.join(PO_STATUSES)}. 'not_finished' = anything not completed.")] = "all",
    limit: Annotated[int, Field(description="Maximum purchase orders to return (1-200), newest first.")] = 25,
) -> str:
    """Purchase orders, newest first: order_id, status, vendor, reference number, dates and total quantity."""
    return run(lambda r: r.list_purchase_orders(vendor, status, limit))


@tool
def get_purchase_order(
    order_id: Annotated[int, Field(description="Lightspeed orderID.")],
) -> str:
    """One purchase order with every line: item, SKUs, quantity ordered, quantity checked in,
    unit cost and line total, plus order totals."""
    return run(lambda r: r.get_purchase_order(order_id))


# ---- matrices and manufacturers ---------------------------------------------------------------


@tool
def search_matrices(
    description: Annotated[str | None, Field(description="Words in the matrix description, in order, e.g. 'caravelli'.")] = None,
    vendor: Annotated[str | None, Field(description="Default vendor: an ID or part of the name.")] = None,
    category: Annotated[str | None, Field(description="A category ID or part of its path; subcategories included.")] = None,
    attribute_set_id: Annotated[int | None, Field(description="Only matrices using this attribute set (5 = Color/Size/Fit).")] = None,
    include_archived: Annotated[bool, Field(description="Also return archived matrices.")] = False,
    limit: Annotated[int, Field(description="Maximum matrices to return (1-500).")] = 50,
) -> str:
    """Find item matrices: matrix_id, description, attribute set, vendor, category, default
    price and cost. Use get_matrix for a matrix's child items."""
    return run(lambda r: r.search_matrices(description, vendor, category, attribute_set_id, include_archived, limit))


@tool
def list_manufacturers(
    name_contains: Annotated[str | None, Field(description="Only manufacturers whose name contains these words.")] = None,
    limit: Annotated[int, Field(description="Maximum to return (1-1000).")] = 200,
) -> str:
    """Manufacturers (brands, e.g. Caravelli), with manufacturer_id. Vendors are who the store buys from."""
    return run(lambda r: r.list_manufacturers(name_contains, limit))


# ---- stock and sales history --------------------------------------------------------------------


@tool
def get_stock_history(
    item_id: Annotated[int | None, Field(description="One item.")] = None,
    sku: Annotated[str | None, Field(description="Or one item by custom SKU, UPC, EAN, manufacturer or system SKU.")] = None,
    matrix_id: Annotated[int | None, Field(description="Or every child of a matrix.")] = None,
    days: Annotated[int, Field(description="How far back to look (1-180 days).")] = 30,
    limit: Annotated[int, Field(description="Maximum log entries (or, store-wide, items) to list.")] = 100,
) -> str:
    """Every change to quantity on hand (sales, PO check-ins, counts, returns, manual
    adjustments, negative-stock auto-fixes), newest first, with a summary by reason and how
    many changes pushed stock negative. With no item, SKU or matrix: a store-wide summary and
    the items that went negative most often. Reads the whole store's log for the period
    (about 1,300 entries per 30 days), so long periods take a while."""
    return run(lambda r: r.get_stock_history(item_id, sku, matrix_id, days, limit))


@tool
def get_sales_history(
    item_id: Annotated[int | None, Field(description="One item.")] = None,
    sku: Annotated[str | None, Field(description="Or one item by custom SKU, UPC, EAN, manufacturer or system SKU.")] = None,
    matrix_id: Annotated[int | None, Field(description="Or every child of a matrix (adds units by Color/Size/Fit).")] = None,
    days: Annotated[int, Field(description="How far back to look (1-365 days).")] = 90,
    limit: Annotated[int, Field(description="Maximum items to list.")] = 50,
) -> str:
    """Units sold, units returned, net sales (after discounts, before tax) and cost of goods
    from completed sales. For a matrix, also net units by each attribute value (which colors,
    sizes and fits sell). With no item, SKU or matrix: the store's top sellers. Rows include
    current quantity on hand for comparison."""
    return run(lambda r: r.get_sales_history(item_id, sku, matrix_id, days, limit))


@tool
def find_stock_issues(
    category: Annotated[str | None, Field(description="Only items in this category (ID or path text); subcategories included.")] = None,
    vendor: Annotated[str | None, Field(description="Only items from this default vendor (ID or name).")] = None,
    matrix_id: Annotated[int | None, Field(description="Only children of this matrix.")] = None,
    limit_per_issue: Annotated[int, Field(description="Items listed per issue (counts always cover everything).")] = 25,
) -> str:
    """Scan active items for problems: negative stock, missing custom SKU, duplicate custom SKU,
    at or below reorder point, no default cost, no price, and items not in any matrix."""
    return run(lambda r: r.find_stock_issues(category, vendor, matrix_id, limit_per_issue))


# ---- counts, special orders, vendor returns, catalogs -------------------------------------------


@tool
def inventory_counts(
    count_id: Annotated[int | None, Field(description="One count to open. Leave empty to list counts, newest first.")] = None,
    only_differences: Annotated[bool, Field(description="With count_id: list only items where counted differs from expected.")] = True,
    include_archived: Annotated[bool, Field(description="When listing: include archived counts.")] = False,
    limit: Annotated[int, Field(description="When listing: maximum counts (1-200).")] = 25,
) -> str:
    """Inventory counts. Without count_id: the list (name, date, reconcile status). With
    count_id: every counted item with expected vs counted quantity and the difference."""
    return run(lambda r: r.inventory_counts(count_id, only_differences, include_archived, limit))


@tool
def list_special_orders(
    status: Annotated[str, Field(description="open, completed or all.")] = "open",
    limit: Annotated[int, Field(description="Maximum to return (1-200), newest first.")] = 25,
) -> str:
    """Customer special orders: item, quantity, status, whether the customer was contacted,
    customer_id, and the PO line ordering it (order_line_id)."""
    return run(lambda r: r.list_special_orders(status, limit))


@tool
def vendor_returns(
    vendor_return_id: Annotated[int | None, Field(description="One return to open. Leave empty to list returns, newest first.")] = None,
    vendor: Annotated[str | None, Field(description="When listing: a vendor ID or part of the name.")] = None,
    status: Annotated[str | None, Field(description="When listing: e.g. open, sent, done.")] = None,
    limit: Annotated[int, Field(description="When listing: maximum returns (1-200).")] = 25,
) -> str:
    """Returns to vendors (RMAs). Without an ID: the list with status, vendor, dates and totals.
    With vendor_return_id: each returned item with quantity, cost and reason (e.g. Defective)."""
    return run(lambda r: r.vendor_returns(vendor_return_id, vendor, status, limit))


@tool
def search_vendor_catalog(
    upc: Annotated[str | None, Field(description="Exact UPC.")] = None,
    ean: Annotated[str | None, Field(description="Exact EAN.")] = None,
    vendor_number: Annotated[str | None, Field(description="Exact vendor part number / style code.")] = None,
    manufacturer_number: Annotated[str | None, Field(description="Exact manufacturer part number.")] = None,
    limit: Annotated[int, Field(description="Maximum to return (1-100).")] = 25,
) -> str:
    """Look up items in the vendor catalogs Lightspeed provides (not yet in this store's
    inventory): brand, description, color, size, numbers, cost and MSRP. Exact matches only;
    text search over the catalogs is too slow on Lightspeed's side."""
    return run(lambda r: r.search_vendor_catalog(upc, ean, vendor_number, manufacturer_number, limit))


# ---- escape hatch ---------------------------------------------------------------------------


@tool
def api_get(
    path: Annotated[str, Field(description="Path under this account, e.g. 'Item.json', 'Item/123.json', 'ItemMatrix.json'.")],
    params: Annotated[dict[str, str] | None, Field(description='Query parameters, e.g. {"description": "~,%navy%", "load_relations": "[\\"ItemShops\\"]", "limit": "10"}.')] = None,
    all_pages: Annotated[bool, Field(description="Follow pagination and return all records (up to max_records).")] = False,
    max_records: Annotated[int, Field(description="With all_pages: stop after this many records (1-1000).")] = 200,
) -> str:
    """Read-only GET of a Lightspeed V3 inventory endpoint for this account, returning
    Lightspeed's raw (untrimmed) JSON. For anything the other tools do not cover. GET only:
    it cannot change anything. Inventory endpoints only: Item, ItemShop, ItemMatrix,
    ItemAttributeSet, Category, Manufacturer, Vendor, Shop, Order, OrderLine, Shipment,
    VendorReturn, CatalogVendorItem, InventoryLog, InventoryCount(Item/Calc), Sale, SaleLine,
    SpecialOrder, TaxClass, Image, CustomField, Serialized. Customer, employee and payment
    records (and relations that load them) are refused. Filter syntax: field=value,
    field=~,%text% (like), field=IN,[1,2], field=>,5. See developers.lightspeedhq.com/retail."""
    return run(lambda r: r.api_get(path, params, all_pages, max_records))


if __name__ == "__main__":
    mcp.run()  # stdio
