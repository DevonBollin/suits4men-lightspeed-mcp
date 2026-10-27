"""The read tools' logic, kept apart from the MCP wiring so it can be tested.

Every method returns a small dict (built with format.py). Things that rarely
change are cached for a few minutes: attribute sets and categories (a handful
of each) in full, and vendor names one ID at a time as they are needed (the
account has hundreds of vendors, so loading them all for every search would
be slow).

Live-account findings that shaped this file are in NOTES.md, notably:
  * ItemMatrix answers 403 with employee:inventory_read, so get_matrix falls
    back to describing the matrix from its child items.
  * OrderLine refuses load_relations=["Item"], so line items are fetched with
    a second query (Item.json?itemID=IN,[...]).
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from . import format as fmt
from .client import LightspeedClient
from .errors import ApiError

CACHE_SECONDS = 600
MAX_IN_IDS = 100  # Lightspeed's limit for an IN,[...] filter
SKU_LOOKUP_FIELDS = ("customSku", "upc", "ean", "manufacturerSku", "systemSku")
PO_STATUSES = ("all", "not_finished", "open", "ordered", "checking_in", "finished")
MATRIX_FORBIDDEN_NOTE = (
    "The matrix record itself is not readable with the current scopes (Lightspeed answers 403 for "
    "ItemMatrix with employee:inventory_read; its docs list employee:product_cost). Matrix details "
    "below are worked out from the child items, and the matrix's own description is not available."
)


def relations(*names: str) -> str:
    """load_relations takes a JSON array: load_relations=["ItemShops","ItemAttributes"]."""
    return json.dumps(list(names))


def match(value: str) -> str:
    """Exact match, or Lightspeed's LIKE operator (~) when the value contains a % wildcard."""
    value = value.strip()
    return f"~,{value}" if "%" in value else value


def words_like(text: str) -> str:
    """'navy slim' -> '~,%navy%slim%': every word, in that order, anywhere in the field (case-insensitive)."""
    return "~,%" + "%".join(text.split()) + "%"


def id_filter(ids: list[str]) -> str:
    return ids[0] if len(ids) == 1 else f"IN,[{','.join(ids)}]"


def batches(ids: list[str], size: int = MAX_IN_IDS):
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def since_iso(days: int) -> str:
    """`days` days ago in Lightspeed's timestamp format (UTC, whole seconds)."""
    return (utc_now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def line_net_sales(line: dict) -> float:
    """What the customer paid for a sale line, before tax and after all discounts.

    Checked on live sales: calcTotal - calcTax1 - calcTax2 equals
    displayableSubtotal on discounted lines, while calcSubtotal is pre-discount.
    """
    return ((fmt.number(line.get("calcTotal")) or 0) - (fmt.number(line.get("calcTax1")) or 0)
            - (fmt.number(line.get("calcTax2")) or 0))


# ---- what api_get may read ----------------------------------------------------------------
#
# The token can read more than inventory: Customer and Employee came along
# with one of the scopes (NOTES.md, "Access map"). The purpose-built tools
# never ask for them, but api_get takes any path, so it gets an allowlist.
# Every named segment of the path must be listed here. Anything else
# (Customer, Employee, Discount, Register, payments, ...) is refused before
# a request is made. To allow another inventory endpoint, add it here.

API_GET_ENDPOINTS = frozenset({
    # items and stock
    "Item", "ItemShop", "ItemMatrix", "ItemAttributeSet", "Image", "CustomField", "Serialized", "TaxClass",
    # reference lists
    "Category", "Manufacturer", "Vendor", "Shop",
    # buying, receiving and returning
    "Order", "OrderLine", "Shipment", "VendorReturn", "CatalogVendorItem",
    # stock movement and counts
    "InventoryLog", "InventoryCount", "InventoryCountItem", "InventoryCountCalc",
    # sales, for sell-through (records carry customer and employee IDs only, never names)
    "Sale", "SaleLine", "SpecialOrder",
})

# Allowed endpoints can still pull people or payment records in through
# load_relations (e.g. Sale with ["Customer"] or SaleLine with ["Employee"]).
# The request is refused if any of these words appears anywhere in the
# load_relations text, ignoring case (no inventory relation contains them).
BLOCKED_RELATION_WORDS = re.compile(r"customer|employee|payment|cccharge|creditaccount", re.IGNORECASE)


def check_api_get_request(path: str, params: dict[str, str]) -> None:
    """Raise ValueError unless `path` and its load_relations stay inside inventory data."""
    clean = (path or "").strip().replace("\\", "/").split("?")[0].lstrip("/")
    clean = re.sub(r"^(API/V3/)?(Account/\d+/)?", "", clean).removesuffix(".json")
    for segment in (s for s in clean.split("/") if s):
        if not segment.isdigit() and segment not in API_GET_ENDPOINTS:
            raise ValueError(
                f"api_get only reads inventory data, and '{segment}' is not one of its endpoints. "
                f"Allowed: {', '.join(sorted(API_GET_ENDPOINTS))}."
            )

    relations_text = params.get("load_relations") or ""
    blocked = BLOCKED_RELATION_WORDS.search(relations_text)
    if blocked:
        raise ValueError(
            f"api_get does not load relations involving '{blocked.group(0)}': customer, employee and payment "
            "records are kept out of this server."
        )


class InventoryReader:
    def __init__(self, client: LightspeedClient, now=time.monotonic):
        self.client = client
        self.now = now
        self._cache: dict[str, tuple[float, object]] = {}
        self._vendor_names: dict[str, str] = {}
        self._vendor_names_since = now()
        self._matrix_forbidden = False  # set after the first ItemMatrix 403
        self._lock = threading.Lock()

    # ---- cached lookups --------------------------------------------------------

    def _cached(self, name: str, load):
        with self._lock:
            hit = self._cache.get(name)
            if hit and hit[0] > self.now():
                return hit[1]
        value = load()
        with self._lock:
            self._cache[name] = (self.now() + CACHE_SECONDS, value)
        return value

    def attribute_sets(self) -> dict[str, dict]:
        def load():
            rows, _ = self.client.get_all("ItemAttributeSet.json", {"archived": "true"})
            return {str(r.get("itemAttributeSetID")): r for r in rows}
        return self._cached("attribute_sets", load)

    def categories(self) -> dict[str, dict]:
        def load():
            rows, _ = self.client.get_all("Category.json", {"archived": "true"})
            return {str(r.get("categoryID")): r for r in rows}
        return self._cached("categories", load)

    def shop_names(self) -> dict[str, str]:
        """Shop names, or {} when the scopes cannot read Shop (true with the Phase 1 scopes)."""
        def load():
            try:
                rows, _ = self.client.get_all("Shop.json")
            except ApiError as e:
                if e.status_code in (401, 403):
                    return {}
                raise
            return {str(r.get("shopID")): fmt.text(r.get("name")) or "" for r in rows}
        return self._cached("shops", load)

    def vendor_names(self, vendor_ids) -> dict[str, str]:
        """{vendor_id: name} for just these IDs, fetching unknown ones in batches of 100."""
        wanted = {str(v) for v in vendor_ids if v and str(v) != "0"}
        with self._lock:
            if self.now() - self._vendor_names_since > CACHE_SECONDS:
                self._vendor_names, self._vendor_names_since = {}, self.now()
            missing = sorted(wanted - self._vendor_names.keys(), key=int)
        for batch in batches(missing):
            rows, _ = self.client.get_all("Vendor.json", {"vendorID": id_filter(batch), "archived": "true"})
            self._remember_vendors(rows)
            with self._lock:
                for vid in batch:
                    self._vendor_names.setdefault(vid, "")  # unknown ID: don't ask again
        with self._lock:
            return {vid: self._vendor_names.get(vid, "") for vid in wanted}

    def _remember_vendors(self, rows: list[dict]) -> None:
        with self._lock:
            for r in rows:
                self._vendor_names[str(r.get("vendorID"))] = fmt.text(r.get("name")) or ""

    # ---- name -> ID resolution ---------------------------------------------------

    def vendor_ids(self, vendor: str) -> list[str]:
        """A vendor ID, or every vendor (archived too) whose name contains the words in `vendor`."""
        vendor = vendor.strip()
        if vendor.isdigit():
            return [vendor]
        rows, truncated = self.client.get_all(
            "Vendor.json", {"name": words_like(vendor), "archived": "true"}, max_records=MAX_IN_IDS)
        if truncated:
            raise ValueError(f"Vendor '{vendor}' matches more than {MAX_IN_IDS} vendors; be more specific.")
        self._remember_vendors(rows)
        return [str(r.get("vendorID")) for r in rows]

    def category_ids(self, category: str) -> list[str]:
        """A category (by ID or part of its path) plus all of its subcategories."""
        category = category.strip()
        paths = {cid: fmt.text(c.get("fullPathName")) or "" for cid, c in self.categories().items()}
        if category.isdigit():
            if category not in paths:
                return [category]
            roots = [paths[category]]
        else:
            roots = [p for p in paths.values() if category.lower() in p.lower()]
        roots = [r for r in roots if r]

        # A category's subcategories have paths like "Suits/Suit" under "Suits".
        ids = []
        for cid, path in paths.items():
            if any(path == root or path.startswith(root + "/") for root in roots):
                ids.append(cid)
        if len(ids) > MAX_IN_IDS:
            raise ValueError(f"Category '{category}' covers {len(ids)} categories; be more specific.")
        return ids

    # ---- items --------------------------------------------------------------------

    def search_items(
        self,
        description: str | None = None,
        upc: str | None = None,
        custom_sku: str | None = None,
        manufacturer_sku: str | None = None,
        vendor: str | None = None,
        category: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
    ) -> dict:
        params: dict[str, str] = {"load_relations": relations("ItemShops", "ItemAttributes")}
        if description:
            params["description"] = words_like(description)
        if upc:
            params["upc"] = match(upc)
        if custom_sku:
            params["customSku"] = match(custom_sku)
        if manufacturer_sku:
            params["manufacturerSku"] = match(manufacturer_sku)
        if vendor:
            ids = self.vendor_ids(vendor)
            if not ids:
                return {"count": 0, "items": [], "note": f"No vendor name contains '{vendor}'."}
            params["defaultVendorID"] = id_filter(ids)
        if category:
            ids = self.category_ids(category)
            if not ids:
                return {"count": 0, "items": [], "note": f"No category path contains '{category}'."}
            params["categoryID"] = id_filter(ids)
        if len(params) == 1:
            raise ValueError("Give at least one filter (description, upc, custom_sku, manufacturer_sku, vendor or category).")
        if include_archived:
            params["archived"] = "true"

        limit = max(1, min(int(limit), 500))
        rows, truncated = self.client.get_all("Item.json", params, max_records=limit)
        sets = self.attribute_sets()
        vendors = self.vendor_names(r.get("defaultVendorID") for r in rows)
        result = {"count": len(rows), "items": [fmt.item_summary(r, sets, vendors) for r in rows]}
        if truncated:
            result["more_available"] = True
            result["note"] = f"Showing the first {len(rows)}. Narrow the search or raise limit (max 500)."
        return result

    def get_item(self, item_id: int | None = None, sku: str | None = None) -> dict:
        item_relations = relations("ItemShops", "ItemAttributes", "Category", "Manufacturer", "ItemVendorNums", "Note")
        if item_id is not None:
            item = self.client.get(f"Item/{int(item_id)}.json", {"load_relations": item_relations}).get("Item")
        elif sku:
            matches = self._find_by_sku(sku.strip(), item_relations)
            if not matches:
                return {"found": False, "note": f"No item has '{sku}' as its custom SKU, UPC, EAN, manufacturer SKU or system SKU."}
            if len(matches) > 1:
                sets = self.attribute_sets()
                vendors = self.vendor_names(m.get("defaultVendorID") for m in matches)
                return {"found": False, "note": f"{len(matches)} items match '{sku}'. Call get_item with one item_id.",
                        "matches": [fmt.item_summary(m, sets, vendors) for m in matches]}
            item = matches[0]
        else:
            raise ValueError("Give item_id or sku.")
        if not isinstance(item, dict):
            return {"found": False, "note": f"Item {item_id} not found."}

        matrix = None
        matrix_id = fmt.ref_id(item.get("itemMatrixID"))
        if matrix_id:
            record = self._matrix_record(matrix_id)
            matrix = {"matrix_id": matrix_id}
            if record:
                matrix["description"] = fmt.text(record.get("description"))
        vendor_ids = [item.get("defaultVendorID")] + [
            v.get("vendorID") for v in fmt.as_list((item.get("ItemVendorNums") or {}).get("ItemVendorNum"))]
        return fmt.item_detail(item, self.attribute_sets(), self.vendor_names(vendor_ids), self.shop_names(), matrix)

    def _find_by_sku(self, sku: str, item_relations: str) -> list[dict]:
        """Try each identifier field in turn; stop at the first field that matches."""
        for field in SKU_LOOKUP_FIELDS:
            rows, _ = self.client.get_all(
                "Item.json", {field: sku, "archived": "true", "load_relations": item_relations}, max_records=20
            )
            if rows:
                return rows
        return []

    # ---- matrices and attribute sets -------------------------------------------------

    def _matrix_record(self, matrix_id: int, load_relations: str | None = None) -> dict | None:
        """The ItemMatrix record, or None if the scopes cannot read it (403) or it does not exist."""
        if self._matrix_forbidden:
            return None
        params = {"load_relations": load_relations} if load_relations else None
        try:
            record = self.client.get(f"ItemMatrix/{int(matrix_id)}.json", params).get("ItemMatrix")
        except ApiError as e:
            if e.status_code == 403:
                self._matrix_forbidden = True  # same answer every time; stop asking
                return None
            if e.status_code == 404:
                return None
            raise
        return record if isinstance(record, dict) else None

    def get_matrix(
        self,
        matrix_id: int,
        attribute_value: str | None = None,
        in_stock_only: bool = False,
        include_archived: bool = False,
    ) -> dict:
        record = self._matrix_record(matrix_id, relations("ItemAttributeSet", "Category"))

        params = {"itemMatrixID": str(int(matrix_id)), "load_relations": relations("ItemAttributes", "ItemShops")}
        if include_archived:
            params["archived"] = "true"
        children, truncated = self.client.get_all("Item.json", params, max_records=2000)
        if record is None and not children:
            return {"found": False, "note": f"No matrix {matrix_id}, or it has no {'' if include_archived else 'active '}items."}

        sets = self.attribute_sets()
        vendor_ids = [record.get("defaultVendorID")] if record else [c.get("defaultVendorID") for c in children]
        vendors = self.vendor_names(vendor_ids)
        if record:
            result = fmt.matrix_summary(record, sets, vendors)
        else:
            result = fmt.matrix_from_children(int(matrix_id), children, sets, vendors)
            result["note"] = MATRIX_FORBIDDEN_NOTE
        if result.get("category_id"):
            result["category_path"] = fmt.text((self.categories().get(str(result["category_id"])) or {}).get("fullPathName"))

        # Whole-matrix counts, before any filtering.
        result["item_count"] = len(children)
        result["items_missing_custom_sku"] = sum(1 for c in children if not fmt.text(c.get("customSku")))
        result["total_qoh"] = sum(fmt.total_qoh(c) or 0 for c in children)

        shown = children
        if attribute_value:
            wanted = attribute_value.strip().lower()
            shown = [c for c in shown if any((fmt.text((c.get("ItemAttributes") or {}).get(f"attribute{i}")) or "").lower()
                                             == wanted for i in (1, 2, 3))]
        if in_stock_only:
            shown = [c for c in shown if (fmt.total_qoh(c) or 0) > 0]
        shown = sorted(shown, key=lambda c: tuple(
            fmt.text((c.get("ItemAttributes") or {}).get(f"attribute{i}")) or "" for i in (1, 2, 3)))
        if attribute_value or in_stock_only:
            result["items_shown"] = len(shown)
        result["items"] = fmt.matrix_child_table(shown, sets, include_archived)
        if truncated:
            result["more_available"] = True
        return result

    def list_attribute_sets(self, include_archived: bool = False) -> dict:
        rows = [fmt.attribute_set_summary(s) for s in self.attribute_sets().values()]
        if not include_archived:
            rows = [r for r in rows if not r.get("archived")]
        return {"count": len(rows), "attribute_sets": sorted(rows, key=lambda r: r.get("attribute_set_id") or 0)}

    # ---- vendors and categories --------------------------------------------------------

    def list_vendors(self, name_contains: str | None = None, include_archived: bool = False, limit: int = 200) -> dict:
        params: dict[str, str] = {}
        if name_contains:
            params["name"] = words_like(name_contains)
        if include_archived:
            params["archived"] = "true"
        limit = max(1, min(int(limit), 1000))
        rows, truncated = self.client.get_all("Vendor.json", params, max_records=limit)
        self._remember_vendors(rows)
        vendors = sorted((fmt.vendor_summary(v) for v in rows), key=lambda r: (r.get("name") or "").lower())
        result = {"count": len(vendors), "vendors": vendors}
        if truncated:
            result["more_available"] = True
            result["note"] = f"Showing {len(vendors)} vendors. Use name_contains to narrow, or raise limit (max 1000)."
        return result

    def list_categories(self, name_contains: str | None = None, include_archived: bool = False) -> dict:
        rows = [fmt.category_summary(c) for c in self.categories().values()]
        if not include_archived:
            rows = [r for r in rows if not r.get("archived")]
        if name_contains:
            rows = [r for r in rows if name_contains.lower() in (r.get("path") or r.get("name") or "").lower()]
        return {"count": len(rows), "categories": sorted(rows, key=lambda r: (r.get("path") or "").lower())}

    # ---- purchase orders ---------------------------------------------------------------

    def list_purchase_orders(self, vendor: str | None = None, status: str = "all", limit: int = 25) -> dict:
        status = (status or "all").lower()
        if status not in PO_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(PO_STATUSES)}.")
        limit = max(1, min(int(limit), 200))

        params: dict[str, str] = {"sort": "-orderID"}  # newest first
        if vendor:
            ids = self.vendor_ids(vendor)
            if not ids:
                return {"count": 0, "purchase_orders": [], "note": f"No vendor name contains '{vendor}'."}
            params["vendorID"] = id_filter(ids)
        if status == "finished":
            params["complete"] = "true"
        elif status != "all":
            params["complete"] = "false"

        # open / ordered / checking_in are worked out from dates, so fetch extra and filter here.
        needs_local_filter = status in ("open", "ordered", "checking_in")
        rows, truncated = self.client.get_all("Order.json", params, max_records=500 if needs_local_filter else limit)
        vendors = self.vendor_names(o.get("vendorID") for o in rows)
        orders = [fmt.order_summary(o, vendors) for o in rows]
        if needs_local_filter:
            orders = [o for o in orders if o["status"] == status]
            truncated = truncated or len(orders) > limit
            orders = orders[:limit]
        result = {"count": len(orders), "purchase_orders": orders}
        if truncated:
            result["more_available"] = True
        return result

    def get_purchase_order(self, order_id: int) -> dict:
        order = self.client.get(
            f"Order/{int(order_id)}.json", {"load_relations": relations("Note", "OrderShipments")}).get("Order")
        if not isinstance(order, dict):
            return {"found": False, "note": f"Purchase order {order_id} not found."}
        lines, truncated = self.client.get_all("OrderLine.json", {"orderID": str(int(order_id))}, max_records=1000)

        # OrderLine refuses load_relations=["Item"], so look the items up in batches.
        items: dict[str, dict] = {}
        item_ids = sorted({str(line.get("itemID")) for line in lines if fmt.ref_id(line.get("itemID"))}, key=int)
        for batch in batches(item_ids):
            rows, _ = self.client.get_all("Item.json", {"itemID": id_filter(batch), "archived": "true"})
            items.update({str(r.get("itemID")): r for r in rows})

        result = fmt.order_summary(order, self.vendor_names([order.get("vendorID")]))
        note = order.get("Note")
        if isinstance(note, dict) and fmt.text(note.get("note")):
            result["note"] = fmt.text(note.get("note"))
        for key, label in (("shipInstructions", "ship_instructions"), ("stockInstructions", "stock_instructions")):
            if fmt.text(order.get(key)):
                result[label] = fmt.text(order.get(key))
        formatted = [fmt.order_line(line, items.get(str(line.get("itemID")))) for line in lines]
        result["line_count"] = len(formatted)
        result["quantity_ordered"] = sum(line.get("quantity") or 0 for line in formatted)
        result["quantity_checked_in"] = sum(line.get("checked_in") or 0 for line in formatted)
        result["lines_total"] = round(sum(line.get("total") or 0 for line in formatted), 2)
        # Each time stock was received against this PO ("Shipment History" in Lightspeed).
        shipments = fmt.as_list((order.get("OrderShipments") or {}).get("OrderShipment"))
        if shipments:
            result["receipts"] = [fmt.order_shipment(s) for s in shipments]
        result["lines"] = formatted
        if truncated:
            result["more_available"] = True
        return result

    # ---- matrices and manufacturers (lists) ---------------------------------------------

    def search_matrices(
        self,
        description: str | None = None,
        vendor: str | None = None,
        category: str | None = None,
        attribute_set_id: int | None = None,
        include_archived: bool = False,
        limit: int = 50,
    ) -> dict:
        params: dict[str, str] = {}
        if description:
            params["description"] = words_like(description)
        if vendor:
            ids = self.vendor_ids(vendor)
            if not ids:
                return {"count": 0, "matrices": [], "note": f"No vendor name contains '{vendor}'."}
            params["defaultVendorID"] = id_filter(ids)
        if category:
            ids = self.category_ids(category)
            if not ids:
                return {"count": 0, "matrices": [], "note": f"No category path contains '{category}'."}
            params["categoryID"] = id_filter(ids)
        if attribute_set_id:
            params["itemAttributeSetID"] = str(int(attribute_set_id))
        if include_archived:
            params["archived"] = "true"
        rows, truncated = self.client.get_all("ItemMatrix.json", params, max_records=max(1, min(int(limit), 500)))
        sets = self.attribute_sets()
        vendors = self.vendor_names(m.get("defaultVendorID") for m in rows)
        paths = {cid: fmt.text(c.get("fullPathName")) or "" for cid, c in self.categories().items()}
        result = {"count": len(rows), "matrices": [fmt.matrix_list_entry(m, sets, vendors, paths) for m in rows]}
        if truncated:
            result["more_available"] = True
        return result

    def list_manufacturers(self, name_contains: str | None = None, limit: int = 200) -> dict:
        params = {"name": words_like(name_contains)} if name_contains else {}
        rows, truncated = self.client.get_all("Manufacturer.json", params, max_records=max(1, min(int(limit), 1000)))
        manufacturers = sorted((fmt.manufacturer_summary(m) for m in rows), key=lambda m: (m.get("name") or "").lower())
        result = {"count": len(manufacturers), "manufacturers": manufacturers}
        if truncated:
            result["more_available"] = True
        return result

    # ---- which items a history question is about ----------------------------------------

    def _target_items(self, item_id: int | None, sku: str | None, matrix_id: int | None) -> tuple[dict[str, dict], dict]:
        """({item_id: item}, description of the target) for an item, a SKU or a whole matrix.

        Returns ({}, {"scope": "whole store"}) when nothing was given.
        """
        item_relations = relations("ItemAttributes", "ItemShops")
        if matrix_id:
            rows, _ = self.client.get_all(
                "Item.json", {"itemMatrixID": str(int(matrix_id)), "archived": "true", "load_relations": item_relations},
                max_records=2000)
            if not rows:
                raise ValueError(f"Matrix {matrix_id} has no items.")
            record = self._matrix_record(int(matrix_id))
            target = {"scope": "matrix", "matrix_id": int(matrix_id), "item_count": len(rows)}
            if record:
                target["description"] = fmt.text(record.get("description"))
            return {str(r.get("itemID")): r for r in rows}, target
        if item_id is not None:
            item = self.client.get(f"Item/{int(item_id)}.json", {"load_relations": item_relations}).get("Item")
            if not isinstance(item, dict):
                raise ValueError(f"Item {item_id} not found.")
        elif sku:
            matches = self._find_by_sku(sku.strip(), item_relations)
            if not matches:
                raise ValueError(f"No item has '{sku}' as a SKU, UPC or EAN.")
            if len(matches) > 1:
                raise ValueError(f"{len(matches)} items match '{sku}'; pass item_id instead "
                                 f"(candidates: {', '.join(str(m.get('itemID')) for m in matches[:10])}).")
            item = matches[0]
        else:
            return {}, {"scope": "whole store"}
        target = {"scope": "item", **fmt.item_ref(item, self.attribute_sets()), "qoh": fmt.total_qoh(item)}
        return {str(item.get("itemID")): item}, target

    # ---- stock history (InventoryLog) ------------------------------------------------------

    def get_stock_history(
        self,
        item_id: int | None = None,
        sku: str | None = None,
        matrix_id: int | None = None,
        days: int = 30,
        limit: int = 100,
    ) -> dict:
        """Every change to quantity on hand in the last `days` days.

        InventoryLog ignores itemID (and every other filter except createTime
        and inventoryLogID; see NOTES.md), so this reads the whole store's log
        for the period and filters it here. About 1,300 entries per 30 days.
        """
        days = max(1, min(int(days), 180))
        since = since_iso(days)
        targets, target = self._target_items(item_id, sku, matrix_id)
        logs, truncated = self.client.get_all(
            "InventoryLog.json", {"createTime": f">=,{since}", "sort": "-inventoryLogID"}, max_records=20000)
        if targets:
            logs = [entry for entry in logs if str(entry.get("itemID")) in targets]

        by_reason: dict[str, dict] = {}
        for entry in logs:
            row = by_reason.setdefault(fmt.text(entry.get("reason")) or "unknown", {"changes": 0, "qoh_change": 0})
            row["changes"] += 1
            row["qoh_change"] += fmt.integer(entry.get("qohChange")) or 0
        result = {
            "target": target,
            "period_days": days,
            "since": since,
            "changes": len(logs),
            "net_qoh_change": sum(fmt.integer(e.get("qohChange")) or 0 for e in logs),
            "changes_that_went_negative": sum(1 for e in logs if fmt.flag(e.get("causedNegative"))),
            "by_reason": dict(sorted(by_reason.items(), key=lambda kv: -kv[1]["changes"])),
        }

        if targets:
            sets = self.attribute_sets()
            entries = []
            for entry in logs[: max(1, min(int(limit), 1000))]:
                row = fmt.inventory_log_entry(entry)
                if len(targets) > 1:  # matrix: say which child
                    row = {"item": fmt.item_ref(targets.get(str(entry.get("itemID"))), sets), **row}
                entries.append(row)
            result["entries"] = entries
            if len(logs) > len(entries):
                result["more_available"] = True
        else:
            # Whole store: which items change (and go negative) most.
            counts: dict[str, list[int]] = {}
            for entry in logs:
                c = counts.setdefault(str(entry.get("itemID")), [0, 0])
                c[0] += 1
                c[1] += 1 if fmt.flag(entry.get("causedNegative")) else 0
            top = sorted(counts.items(), key=lambda kv: (-kv[1][1], -kv[1][0]))[: max(1, min(int(limit), 100))]
            items = self._items_by_id([item for item, _ in top])
            result["items_going_negative_most"] = {
                "columns": ["item_id", "custom_sku", "description", "changes", "went_negative", "qoh"],
                "rows": [[int(i), fmt.text(items.get(i, {}).get("customSku")), fmt.text(items.get(i, {}).get("description")),
                          n, neg, fmt.total_qoh(items.get(i, {}))] for i, (n, neg) in top],
            }
        if truncated:
            result["more_available"] = True
        return result

    def _items_by_id(self, item_ids: list[str], item_relations: str | None = None) -> dict[str, dict]:
        """Look up items by ID, 100 per request (archived ones included)."""
        found: dict[str, dict] = {}
        ids = sorted({str(i) for i in item_ids if fmt.ref_id(i)}, key=int)
        for batch in batches(ids):
            params = {"itemID": id_filter(batch), "archived": "true",
                      "load_relations": item_relations or relations("ItemShops", "ItemAttributes")}
            rows, _ = self.client.get_all("Item.json", params)
            found.update({str(r.get("itemID")): r for r in rows})
        return found

    # ---- sales history ---------------------------------------------------------------------

    def get_sales_history(
        self,
        item_id: int | None = None,
        sku: str | None = None,
        matrix_id: int | None = None,
        days: int = 90,
        limit: int = 50,
    ) -> dict:
        """Units sold, returns and net sales from completed sales in the last `days` days.

        For a matrix it also totals units by each attribute (e.g. which sizes
        sell). With no item, SKU or matrix it lists the store's top sellers.
        """
        days = max(1, min(int(days), 365))
        targets, target = self._target_items(item_id, sku, matrix_id)
        since = since_iso(days)

        lines: list[dict] = []
        if targets:
            for batch in batches(sorted(targets, key=int)):
                rows, _ = self.client.get_all(
                    "SaleLine.json", {"itemID": id_filter(batch), "createTime": f">=,{since}"}, max_records=20000)
                lines.extend(rows)
        else:
            lines, _ = self.client.get_all("SaleLine.json", {"createTime": f">=,{since}"}, max_records=20000)

        # Only count lines on completed, non-voided sales (open layaways and quotes are not sales yet).
        completed: set[str] = set()
        for batch in batches(sorted({str(line.get("saleID")) for line in lines if fmt.ref_id(line.get("saleID"))}, key=int)):
            sales, _ = self.client.get_all("Sale.json", {"saleID": id_filter(batch), "archived": "true"})
            completed.update(str(s.get("saleID")) for s in sales
                             if fmt.flag(s.get("completed")) and not fmt.flag(s.get("voided")))
        lines = [line for line in lines if str(line.get("saleID")) in completed]

        per_item: dict[str, dict] = {}
        for line in lines:
            qty = fmt.integer(line.get("unitQuantity")) or 0
            row = per_item.setdefault(str(line.get("itemID")),
                                      {"sold": 0, "returned": 0, "net_sales": 0.0, "cost": 0.0, "last_sold": None})
            if qty >= 0:
                row["sold"] += qty
                created = fmt.date(line.get("createTime"))
                if created and (row["last_sold"] is None or created > row["last_sold"]):
                    row["last_sold"] = created
            else:
                row["returned"] += -qty
            row["net_sales"] += line_net_sales(line)
            row["cost"] += (fmt.number(line.get("avgCost")) or 0) * qty

        def total(key):
            return sum(r[key] for r in per_item.values())

        result = {
            "target": target,
            "period_days": days,
            "since": since,
            "sales_lines": len(lines),
            "units_sold": total("sold"),
            "units_returned": total("returned"),
            "net_units": total("sold") - total("returned"),
            "net_sales": round(total("net_sales"), 2),
            "cost_of_goods": round(total("cost"), 2),
        }

        sets = self.attribute_sets()
        if targets:
            items = targets
            ranked = sorted(targets, key=lambda i: -(per_item.get(i, {}).get("sold") or 0))
        else:
            ranked = sorted(per_item, key=lambda i: -per_item[i]["sold"])[: max(1, min(int(limit), 200))]
            items = self._items_by_id(ranked)

        if len(targets) > 1:  # a matrix: units by each attribute
            slots = fmt.attribute_slots(sets.get(str((next(iter(targets.values())).get("ItemAttributes") or {})
                                                     .get("itemAttributeSetID"))))
            by_attribute: dict[str, dict[str, int]] = {}
            for iid, item in targets.items():
                sold = (per_item.get(iid) or {}).get("sold", 0) - (per_item.get(iid) or {}).get("returned", 0)
                for i in (1, 2, 3):
                    value = fmt.text((item.get("ItemAttributes") or {}).get(f"attribute{i}"))
                    if value:
                        bucket = by_attribute.setdefault(slots.get(i, f"attribute{i}"), {})
                        bucket[value] = bucket.get(value, 0) + sold
            result["net_units_by_attribute"] = {
                name: dict(sorted(values.items(), key=lambda kv: -kv[1])) for name, values in by_attribute.items()}
            ranked = [i for i in ranked if per_item.get(i)][: max(1, min(int(limit), 500))]  # only children that sold

        rows = []
        for iid in ranked:
            stats, item = per_item.get(iid) or {}, items.get(iid) or {}
            attrs = fmt.attribute_values(item, sets)
            rows.append([fmt.integer(iid), fmt.text(item.get("customSku")), fmt.text(item.get("description")),
                         " / ".join(attrs.values()) if attrs else None,
                         stats.get("sold", 0), stats.get("returned", 0), round(stats.get("net_sales", 0.0), 2),
                         stats.get("last_sold"), fmt.total_qoh(item)])
        result["items"] = {"columns": ["item_id", "custom_sku", "description", "attributes", "sold", "returned",
                                       "net_sales", "last_sold", "qoh"], "rows": rows}
        return result

    # ---- inventory counts --------------------------------------------------------------------

    def inventory_counts(
        self, count_id: int | None = None, only_differences: bool = True, include_archived: bool = False, limit: int = 25
    ) -> dict:
        if count_id is None:
            params = {"sort": "-inventoryCountID"}
            if include_archived:
                params["archived"] = "true"
            rows, truncated = self.client.get_all("InventoryCount.json", params, max_records=max(1, min(int(limit), 200)))
            result = {"count": len(rows), "inventory_counts": [
                {**fmt.inventory_count_summary(r), **({"reconcile_status": fmt.text(r.get("reconcileStatus"))}
                                                       if fmt.text(r.get("reconcileStatus")) else {})} for r in rows]}
            if truncated:
                result["more_available"] = True
            return result

        record = self.client.get(f"InventoryCount/{int(count_id)}.json").get("InventoryCount")
        if not isinstance(record, dict):
            return {"found": False, "note": f"Inventory count {count_id} not found."}
        calcs, _ = self.client.get_all("InventoryCountCalc.json", {"inventoryCountID": str(int(count_id))}, max_records=20000)
        items = self._items_by_id([c.get("itemID") for c in calcs])
        sets = self.attribute_sets()

        rows, net = [], 0
        for calc in calcs:
            expected, counted = fmt.integer(calc.get("calcQoh")) or 0, fmt.integer(calc.get("countedQoh")) or 0
            net += counted - expected
            if only_differences and counted == expected:
                continue
            item = items.get(str(calc.get("itemID"))) or {}
            attrs = fmt.attribute_values(item, sets)
            rows.append([fmt.integer(calc.get("itemID")), fmt.text(item.get("customSku")), fmt.text(item.get("description")),
                         " / ".join(attrs.values()) if attrs else None, expected, counted, counted - expected])
        rows.sort(key=lambda r: r[6])
        result = fmt.inventory_count_summary(record)
        if fmt.text(record.get("reconcileStatus")):
            result["reconcile_status"] = fmt.text(record.get("reconcileStatus"))
        result.update({
            "items_counted": len(calcs),
            "items_with_differences": sum(1 for c in calcs if c.get("calcQoh") != c.get("countedQoh")),
            "net_difference": net,
            "items": {"columns": ["item_id", "custom_sku", "description", "attributes", "expected", "counted", "difference"],
                      "rows": rows},
        })
        return result

    # ---- special orders and vendor returns ----------------------------------------------------

    def list_special_orders(self, status: str = "open", limit: int = 25) -> dict:
        status = (status or "open").lower()
        if status not in ("open", "completed", "all"):
            raise ValueError("status must be open, completed or all.")
        params = {"sort": "-specialOrderID", "load_relations": relations("SaleLine", "SaleLine.Item")}
        if status != "all":
            params["completed"] = "true" if status == "completed" else "false"
        rows, truncated = self.client.get_all("SpecialOrder.json", params, max_records=max(1, min(int(limit), 200)))
        sets = self.attribute_sets()
        result = {"count": len(rows), "special_orders": [fmt.special_order_summary(r, sets) for r in rows]}
        if truncated:
            result["more_available"] = True
        return result

    def vendor_returns(
        self, vendor_return_id: int | None = None, vendor: str | None = None, status: str | None = None, limit: int = 25
    ) -> dict:
        if vendor_return_id is not None:
            record = self.client.get(f"VendorReturn/{int(vendor_return_id)}.json", {"load_relations": relations(
                "VendorReturnItems", "VendorReturnItems.Item", "VendorReturnItems.VendorReturnItemReason", "Note")}
            ).get("VendorReturn")
            if not isinstance(record, dict):
                return {"found": False, "note": f"Vendor return {vendor_return_id} not found."}
            sets = self.attribute_sets()
            lines = [fmt.vendor_return_line(line, sets)
                     for line in fmt.as_list((record.get("VendorReturnItems") or {}).get("VendorReturnItem"))]
            result = fmt.vendor_return_summary(record, self.vendor_names([record.get("vendorID")]))
            if fmt.date(record.get("closedDate")):
                result["closed"] = fmt.date(record.get("closedDate"))
            note = record.get("Note")
            if isinstance(note, dict) and fmt.text(note.get("note")):
                result["note"] = fmt.text(note.get("note"))
            result["quantity"] = sum(line.get("quantity") or 0 for line in lines)
            result["lines"] = lines
            return result

        params: dict[str, str] = {"sort": "-vendorReturnID"}
        if vendor:
            ids = self.vendor_ids(vendor)
            if not ids:
                return {"count": 0, "vendor_returns": [], "note": f"No vendor name contains '{vendor}'."}
            params["vendorID"] = id_filter(ids)
        if status:
            params["status"] = status.strip().lower()
        rows, truncated = self.client.get_all("VendorReturn.json", params, max_records=max(1, min(int(limit), 200)))
        vendors = self.vendor_names(r.get("vendorID") for r in rows)
        result = {"count": len(rows), "vendor_returns": [
            {**fmt.vendor_return_summary(r, vendors), **({"closed": fmt.date(r.get("closedDate"))} if fmt.date(r.get("closedDate")) else {})}
            for r in rows]}
        if truncated:
            result["more_available"] = True
        return result

    # ---- vendor catalogs ---------------------------------------------------------------------

    def search_vendor_catalog(
        self,
        upc: str | None = None,
        ean: str | None = None,
        vendor_number: str | None = None,
        manufacturer_number: str | None = None,
        limit: int = 25,
    ) -> dict:
        """Exact lookups only. A LIKE search on description timed out (30 s) on this
        account's catalogs, so there is no text search."""
        params = {k: v.strip() for k, v in (("upc", upc), ("ean", ean), ("vendorNumber", vendor_number),
                                             ("manufacturerNumber", manufacturer_number)) if v}
        if not params:
            raise ValueError("Give upc, ean, vendor_number or manufacturer_number (exact match).")
        rows, truncated = self.client.get_all("CatalogVendorItem.json", params, max_records=max(1, min(int(limit), 100)))
        result = {"count": len(rows), "catalog_items": [fmt.catalog_item(r) for r in rows]}
        if truncated:
            result["more_available"] = True
        return result

    # ---- stock problems ------------------------------------------------------------------------

    def find_stock_issues(
        self,
        category: str | None = None,
        vendor: str | None = None,
        matrix_id: int | None = None,
        limit_per_issue: int = 25,
    ) -> dict:
        """Scan active items for the problems that matter before receiving or relabelling stock."""
        params: dict[str, str] = {"load_relations": relations("ItemShops")}
        if matrix_id:
            params["itemMatrixID"] = str(int(matrix_id))
        if vendor:
            ids = self.vendor_ids(vendor)
            if not ids:
                return {"scanned": 0, "note": f"No vendor name contains '{vendor}'."}
            params["defaultVendorID"] = id_filter(ids)
        if category:
            ids = self.category_ids(category)
            if not ids:
                return {"scanned": 0, "note": f"No category path contains '{category}'."}
            params["categoryID"] = id_filter(ids)
        items, truncated = self.client.get_all("Item.json", params, max_records=20000)
        limit = max(1, min(int(limit_per_issue), 500))

        def table(rows: list[dict], extra: list[tuple[str, object]] = ()) -> dict:
            columns = ["item_id", "custom_sku", "description", "qoh", *[name for name, _ in extra]]
            return {"columns": columns, "rows": [
                [fmt.integer(i.get("itemID")), fmt.text(i.get("customSku")), fmt.text(i.get("description")),
                 fmt.total_qoh(i), *[getter(i) for _, getter in extra]] for i in rows[:limit]]}

        def issue(rows: list[dict], extra=()) -> dict:
            return {"count": len(rows), "items": table(rows, extra)}

        def reorder_point(i):
            return max((fmt.integer(s.get("reorderPoint")) or 0
                        for s in fmt.as_list((i.get("ItemShops") or {}).get("ItemShop")) if str(s.get("shopID")) != "0"),
                       default=0)

        negative = sorted((i for i in items if (fmt.total_qoh(i) or 0) < 0), key=lambda i: fmt.total_qoh(i) or 0)
        missing_sku = [i for i in items if not fmt.text(i.get("customSku"))]
        by_sku: dict[str, list[dict]] = {}
        for i in items:
            if fmt.text(i.get("customSku")):
                by_sku.setdefault(fmt.text(i.get("customSku")), []).append(i)
        duplicates = [group for group in by_sku.values() if len(group) > 1]
        below_reorder = [i for i in items if reorder_point(i) > 0 and (fmt.total_qoh(i) or 0) <= reorder_point(i)]
        no_cost = [i for i in items if (fmt.number(i.get("defaultCost")) or 0) <= 0]
        no_price = [i for i in items if (fmt.prices(i).get("Default") or 0) <= 0]
        not_in_matrix = [i for i in items if not fmt.ref_id(i.get("itemMatrixID"))]

        result = {
            "scanned": len(items),
            "issues": {
                "negative_stock": {**issue(negative), "total_units_negative": sum(fmt.total_qoh(i) or 0 for i in negative)},
                "missing_custom_sku": issue(missing_sku),
                "duplicate_custom_sku": {"count": len(duplicates), "groups": [
                    {"custom_sku": fmt.text(g[0].get("customSku")),
                     "items": [[fmt.integer(i.get("itemID")), fmt.text(i.get("description"))] for i in g]}
                    for g in duplicates[:limit]]},
                "at_or_below_reorder_point": issue(below_reorder, [("reorder_point", reorder_point)]),
                "no_default_cost": issue(no_cost),
                "no_price": issue(no_price),
                "not_in_a_matrix": issue(not_in_matrix),
            },
        }
        if truncated:
            result["more_available"] = True
        return result

    # ---- escape hatch ---------------------------------------------------------------------

    def api_get(self, path: str, params: dict | None = None, all_pages: bool = False, max_records: int = 200) -> dict:
        params = {str(k): v if isinstance(v, str) else json.dumps(v) for k, v in (params or {}).items()}
        check_api_get_request(path, params)  # inventory endpoints only; see API_GET_ENDPOINTS
        if not all_pages:
            return self.client.get(path, params)
        max_records = max(1, min(int(max_records), 1000))
        rows, truncated = self.client.get_all(path, params, max_records=max_records)
        return {"record_key": self.client.record_key(path), "count": len(rows),
                "more_available": truncated, "records": rows}
