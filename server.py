"""MCP server for Claude Desktop: tools for the Suits 4 Men Lightspeed account.

Claude Desktop starts this file over stdio (see README.md). Do not print() in
here: stdout carries the MCP protocol. Logging goes to stderr, which Claude
Desktop saves in its MCP logs (%APPDATA%\\Claude\\logs\\mcp-server-lightspeed.log).

The read tools' logic lives in lightspeed_mcp/reader.py and only ever sends
GET. The five write tools' logic lives in lightspeed_mcp/writer.py: they
default to dry runs, and a real write needs the master write switch
(lightspeed_mcp/write_path.py) plus the confirm code of an approved dry run.
This file only describes the tools for Claude and turns results into compact JSON text.
"""

from __future__ import annotations

import inspect
import json
import logging
import sys
import threading
from typing import Annotated

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from lightspeed_mcp import snapshots
from lightspeed_mcp.client import LightspeedClient
from lightspeed_mcp.config import HTTP_TIMEOUT_SECONDS
from lightspeed_mcp.errors import LightspeedError, NotBootstrappedError
from lightspeed_mcp.reader import PO_STATUSES, InventoryReader
from lightspeed_mcp.secret_store import SecretStore
from lightspeed_mcp.tokens import TokenManager
from lightspeed_mcp.write_path import WriteClient, WriteLog, WriteSwitch, write_log_report
from lightspeed_mcp.writer import InventoryWriter

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every request URL at INFO; keep the log about our own events.
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = MCPServer(
    "suits4men-lightspeed",
    instructions=(
        "Access to the Suits 4 Men Lightspeed Retail (R-Series) account: items, item matrices, attribute sets, "
        "vendors, manufacturers, categories, purchase orders, stock and stock history, sales history, inventory "
        "counts, special orders, vendor returns and vendor catalogs. Results are trimmed JSON with IDs; long lists "
        "come as {columns, rows} tables. Use api_get for any field the other tools leave out. The read tools and "
        "api_get can never change anything. Eight write tools (create_matrix, add_matrix_variants, "
        "update_item_fields, add_po_lines, receive_po_lines, move_items_into_matrix, assign_custom_skus, "
        "archive_item) default to dry runs that send nothing. Matrices can never be archived. Never make a "
        "real write unless the user, in this chat, has approved that exact dry run; an approval covers one write "
        "only. Real writes are refused while the store owner has writes turned off. suggest_custom_skus gives "
        "unused SKUs, show_write_log lists what has been written, and take_snapshot / compare_snapshots save and "
        "compare a read-only copy of the inventory data (good before and after a real write)."
    ),
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
# Tells Claude Desktop these tools can change data (even though they default to dry runs).
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)
# take_snapshot only reads Lightspeed, but it adds files to backups/ on this computer.
LOCAL_FILES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)

_reader: InventoryReader | None = None
_reader_lock = threading.Lock()
_writer: InventoryWriter | None = None
_writer_lock = threading.Lock()
_snapshots: snapshots.SnapshotRunner | None = None
_snapshots_lock = threading.Lock()


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


def writer() -> InventoryWriter:
    """The write tools' logic. Its WriteClient shares the read client's tokens and rate limiter."""
    global _writer
    read = reader()
    with _writer_lock:
        if _writer is None:
            client = read.client
            _writer = InventoryWriter(read, WriteClient(client.account_id, client.tokens, client.limiter))
        return _writer


def snapshot_runner() -> snapshots.SnapshotRunner:
    """Takes snapshots in the background with the GET-only read client."""
    global _snapshots
    read = reader()
    with _snapshots_lock:
        if _snapshots is None:
            _snapshots = snapshots.SnapshotRunner(read.client)
        return _snapshots


def local():
    """For tools that only look at files on this computer: no Lightspeed client needed."""
    return None


def run(call, logic=reader) -> str:
    """Run one tool call; return compact JSON, or raise ToolError with a readable message."""
    try:
        result = call(logic())
    except (LightspeedError, ValueError) as e:
        raise ToolError(str(e)) from e
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def tool(fn):
    return mcp.tool(annotations=READ_ONLY, structured_output=False)(fn)


# Appended to every write tool's description.
WRITE_STEPS = """
How every write tool works:
1. Call it with dry_run=true (the default). Nothing is sent. It returns the exact method, path and JSON body of
   each request, a plain-language summary, warnings, and a confirm_code.
2. Show the user the summary, the warnings and the request bodies, and ask whether to make this change.
3. Only if the user approves in this chat, call the tool again with the same arguments, dry_run=false and that
   confirm_code. An approval covers that one change only. If anything changed in between, the code no longer
   matches and the call is refused: run the dry run again.
Real writes are refused while the store owner has writes turned off on this computer; say so and stop. The
store's rules (attribute set 5 Color/Size/Fit, matrices by garment type not color, vendor naming kept as is,
pants by waist only, no 3-piece or boys' items, plain descriptions, one unique 6-digit custom SKU per item) are
enforced: a refusal says which rule.
"""


def write_tool(fn):
    description = inspect.cleandoc(fn.__doc__ or "") + "\n" + WRITE_STEPS
    return mcp.tool(annotations=WRITES, description=description, structured_output=False)(fn)


DryRun = Annotated[bool, Field(description="True (default): only show what would be sent. False: send it, which needs "
                                           "confirm_code and the user's approval of the dry run.")]
ConfirmCode = Annotated[str | None, Field(description="With dry_run=false: the confirm_code from the approved dry run.")]


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


# ---- SKUs, the write log, snapshots ---------------------------------------------------------


@tool
def suggest_custom_skus(
    count: Annotated[int, Field(description="How many SKUs to suggest (1-200).")] = 10,
) -> str:
    """The next unused 6-digit custom SKUs, continuing after the highest SKU in use (archived
    items included, so old numbers are never reused). Also the number of active items without
    a SKU. Suggestions are not reserved; the write tools check again before using one."""
    return run(lambda r: r.suggest_custom_skus(count))


@tool
def show_write_log(
    limit: Annotated[int, Field(description="Maximum writes to list, newest first (1-500).")] = 20,
) -> str:
    """Whether writes to Lightspeed are turned on, and every write this computer has sent: when,
    which tool, method, path, the JSON payload, the outcome (done / failed / unknown), HTTP status
    and the IDs it affected. Reads the local log only."""
    return run(lambda _: write_log_report(WriteSwitch(), WriteLog(), limit), local)


@mcp.tool(annotations=LOCAL_FILES, structured_output=False)
def take_snapshot() -> str:
    """Start a snapshot of the store's inventory data: every item (stock, prices, costs, SKUs,
    attributes), matrix, attribute set, category, manufacturer, vendor, tax class, purchase order,
    PO line and receipt, saved as JSON under backups/ on this computer. GET only: it changes
    nothing in Lightspeed. Runs in the background (about 75 seconds) and returns at once; use
    compare_snapshots to see when it is complete. Good before and after any real write."""
    return run(lambda _: snapshot_runner().start(), local)


@tool
def compare_snapshots(
    older: Annotated[str | None, Field(description="Older snapshot name, e.g. '2026-10-26_202204'. Leave both empty for the two newest.")] = None,
    newer: Annotated[str | None, Field(description="Newer snapshot name.")] = None,
    record_type: Annotated[str | None, Field(description="Only this record type, e.g. 'Item', 'ItemMatrix', 'OrderLine'.")] = None,
    limit: Annotated[int, Field(description="Records listed per type (1-200); counts always cover everything.")] = 20,
) -> str:
    """List snapshots (newest first, with complete / running / failed), and compare two: records
    added, removed and changed, with each changed field's old and new value. Changes from normal
    store activity (sales change stock and timestamps) show up too. Reads local files only."""
    return run(lambda _: snapshots.compare_report(older, newer, record_type, limit), local)


# ---- write tools (dry run by default) --------------------------------------------------------


class Variant(BaseModel):
    color: Annotated[str, Field(description="Color exactly as the vendor names it, e.g. 'Midnight Blue'. Sent as given, never re-cased.")]
    size: Annotated[str, Field(description="Size, e.g. '42R' for suits. Pants: waist only, e.g. '34'.")]
    fit: Annotated[str, Field(description="Fit exactly as the vendor names it, e.g. 'Slim'. May be blank.")] = ""
    custom_sku: Annotated[str, Field(description="A new, unused 6-digit custom SKU, as text so leading zeros stay, e.g. '013471'.")]


class NewPoLine(BaseModel):
    item_id: Annotated[int, Field(description="The item to order.")]
    quantity: Annotated[int, Field(description="Units to order.")]
    unit_cost: Annotated[float | None, Field(description="Unit cost on this PO. Default: the item's default cost.")] = None


class MoveItem(BaseModel):
    item_id: Annotated[int, Field(description="The existing item to put into the matrix.")]
    color: Annotated[str, Field(description="Its color, exactly as the vendor names it.")]
    size: Annotated[str, Field(description="Its size, e.g. '58L'. Pants: waist only.")]
    fit: Annotated[str, Field(description="Its fit, exactly as the vendor names it. May be blank.")] = ""


class SkuAssignment(BaseModel):
    item_id: Annotated[int, Field(description="An item that has no custom SKU yet.")]
    custom_sku: Annotated[str | None, Field(description="A 6-digit SKU as text. Leave empty to take the next free one.")] = None


class Receipt(BaseModel):
    quantity: Annotated[int, Field(description="Units that arrived now (added to what the line already shows as received).")]
    order_line_id: Annotated[int | None, Field(description="The PO line (get_purchase_order lists order_line_id).")] = None
    item_id: Annotated[int | None, Field(description="Instead of order_line_id: the item, if it is on one line of the PO.")] = None


@write_tool
def create_matrix(
    description: Annotated[str, Field(description="Plain-language matrix description naming the garment type, no vendor codes or colors, e.g. 'Giorgio Fiorelli Vest'.")],
    garment_type: Annotated[str, Field(description="One of: suit (2-piece), tuxedo, vest, pants, sport coat. No 3-piece.")],
    category_id: Annotated[int, Field(description="Category for the matrix (list_categories), e.g. 83 Suits/Suit.")],
    vendor_id: Annotated[int, Field(description="Default vendor (list_vendors).")],
    manufacturer_id: Annotated[int | None, Field(description="Manufacturer / brand (list_manufacturers).")] = None,
    default_cost: Annotated[float | None, Field(description="The matrix's default cost.")] = None,
    price: Annotated[float | None, Field(description="The matrix's Default selling price. Left unset unless given.")] = None,
    dry_run: DryRun = True,
    confirm_code: ConfirmCode = None,
) -> str:
    """Create a new, empty item matrix under the store's attribute set 5 (Color/Size/Fit).
    One matrix per brand line and garment type; colors, sizes and fits are added later by
    add_matrix_variants (Lightspeed adds each variant's values to the matrix by itself).
    Refuses a description with a vendor code or a color, a 3-piece or boys' matrix, or a
    description that is already used."""
    return run(lambda w: w.create_matrix(description, garment_type, category_id, vendor_id, manufacturer_id,
                                         default_cost, price, dry_run, confirm_code), writer)


@write_tool
def add_matrix_variants(
    matrix_id: Annotated[int, Field(description="The matrix to add items to (must use attribute set 5).")],
    variants: Annotated[list[Variant], Field(description="The new items: color, size, fit and custom SKU each (max 100).")],
    price: Annotated[float, Field(description="Default selling price for every new item (not the matrix's own price).")],
    cost: Annotated[float, Field(description="Default cost for every new item.")],
    dry_run: DryRun = True,
    confirm_code: ConfirmCode = None,
) -> str:
    """Add new child items (variants) to a Color/Size/Fit matrix: one item per color/size/fit,
    each with its own new custom SKU, the given price and cost, and the matrix's category,
    vendor, manufacturer and tax. Descriptions are '<matrix description> <color> <size> <fit>'.
    Stock starts at 0. Never changes the matrix or its existing items. Refuses duplicate SKUs
    or variants, pants sizes that are not waist-only, and 3-piece or boys' values. Warns about
    new color/size/fit spellings and prices that differ from the matrix's other items."""
    return run(lambda w: w.add_matrix_variants(matrix_id, [v.model_dump() for v in variants], price, cost,
                                               dry_run, confirm_code), writer)


@write_tool
def update_item_fields(
    item_id: Annotated[int, Field(description="The item to change.")],
    custom_sku: Annotated[str | None, Field(description="New 6-digit custom SKU (must be unused).")] = None,
    cost: Annotated[float | None, Field(description="New default cost.")] = None,
    description: Annotated[str | None, Field(description="New plain-language description (no vendor codes).")] = None,
    dry_run: DryRun = True,
    confirm_code: ConfirmCode = None,
) -> str:
    """Change an item's custom SKU, default cost and/or description. Only the fields given (and
    actually different) are sent. Prices, stock and everything else are never touched."""
    return run(lambda w: w.update_item_fields(item_id, custom_sku, cost, description, dry_run, confirm_code), writer)


@write_tool
def add_po_lines(
    order_id: Annotated[int, Field(description="An open (not finished) purchase order.")],
    lines: Annotated[list[NewPoLine], Field(description="Items, quantities and optional unit costs (max 100).")],
    dry_run: DryRun = True,
    confirm_code: ConfirmCode = None,
) -> str:
    """Add lines to an open purchase order: one line per item. Refuses items already on the PO,
    archived items, and 3-piece or boys' items. Warns about catch-all items not in a matrix, items
    without a custom SKU, negative stock, and items from another vendor. Receives nothing."""
    return run(lambda w: w.add_po_lines(order_id, [line.model_dump() for line in lines], dry_run, confirm_code), writer)


@write_tool
def receive_po_lines(
    order_id: Annotated[int, Field(description="An open (not finished) purchase order.")],
    lines: Annotated[list[Receipt], Field(description="What arrived: order_line_id (or item_id) and quantity per line.")],
    dry_run: DryRun = True,
    confirm_code: ConfirmCode = None,
) -> str:
    """Record goods received against purchase-order lines (sets each line's numReceived to its
    new running total). Refuses receiving more than was ordered. Stock on hand does not change
    until someone finishes the check-in in Lightspeed ('Add Received To Inventory' on the PO):
    Lightspeed's API has no call for that step. Tell the user so."""
    return run(lambda w: w.receive_po_lines(order_id, [line.model_dump() for line in lines], dry_run, confirm_code),
               writer)


@write_tool
def move_items_into_matrix(
    matrix_id: Annotated[int, Field(description="The Color/Size/Fit matrix (attribute set 5) to put the items in.")],
    items: Annotated[list[MoveItem], Field(description="Existing items and their color, size and fit (max 100; one until a first move has been checked).")],
    only_matrix_and_attributes: Annotated[bool, Field(description="True: change only the matrix and Color/Size/Fit. False (default): also give each item the matrix's description pattern, category, manufacturer and vendor.")] = False,
    dry_run: DryRun = True,
    confirm_code: ConfirmCode = None,
) -> str:
    """Put existing items (not in a matrix, or in another one, e.g. a Color-only matrix) into a
    Color/Size/Fit matrix, with their color, size and fit. Never sends prices, cost, SKUs or
    stock. Refuses a combination the matrix already has, spellings that differ from the matrix's
    only in capitals, non-waist pants sizes, and 3-piece or boys' items. Lightspeed's docs don't
    describe moving existing items, so until one move has been done and checked, it moves one
    item per call."""
    return run(lambda w: w.move_items_into_matrix(matrix_id, [i.model_dump() for i in items],
                                                  only_matrix_and_attributes, dry_run, confirm_code), writer)


@write_tool
def assign_custom_skus(
    items: Annotated[list[SkuAssignment], Field(description="Items without a SKU, each with a SKU or none to take the next free one (max 100).")],
    dry_run: DryRun = True,
    confirm_code: ConfirmCode = None,
) -> str:
    """Give items that have no custom SKU their own one, many in one approval. SKUs left empty are
    picked automatically, continuing after the highest SKU in use. Only customSku is sent.
    Refuses items that already have a SKU (update_item_fields changes those), archived items,
    and SKUs that are taken (archived items included) or not 6 digits."""
    return run(lambda w: w.assign_custom_skus([i.model_dump() for i in items], dry_run, confirm_code), writer)


@write_tool
def archive_item(
    item_id: Annotated[int, Field(description="The one item to archive.")],
    dry_run: DryRun = True,
    confirm_code: ConfirmCode = None,
) -> str:
    """Archive one item (Lightspeed archives items rather than deleting them). Only items with
    stock exactly 0 that are on no open purchase order: archiving zeroes stock in Lightspeed,
    and that can't be undone. One item per call. Matrices can never be archived with this
    server; that stays manual in Lightspeed. The item's values are saved in the write log first."""
    return run(lambda w: w.archive_item(item_id, dry_run, confirm_code), writer)


if __name__ == "__main__":
    mcp.run()  # stdio
