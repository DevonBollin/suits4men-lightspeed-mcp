"""The write path: the only code in this project that can change Lightspeed.

The read client (client.py) only sends GET, and every read tool and api_get
use it. Writes go through WriteClient, defined here, and only the write tools
in writer.py hold one. Before anything leaves the computer, these checks must pass:

  1. The master write switch is ON. The switch is a file, state/WRITES_ENABLED,
     that only the store owner creates, by hand. While it is missing (as
     shipped), every write is refused. It is checked on every request, so
     deleting the file turns writes off at once, without a restart.
  2. The request is one that the calling write tool is allowed to make
     (WRITE_ROUTES). For example update_item_fields can only PUT
     Item/<id>.json. The only DELETE is archive_item's, on one Item/<id>.json
     (Lightspeed archives an item on DELETE). No route can update or archive a
     matrix: archiving a matrix archives every item in it, so that stays manual.
  3. The JSON body holds only the fields that route allows (a DELETE has no
     body). Prices can only be set when creating a matrix or a new item (or by
     update_item_prices); stock on hand (ItemShops qoh) only by adjust_stock,
     one shop's stock row at a time.
  4. An httpx request hook repeats checks 1 and 2 as the last line of defence,
     so code that reaches past WriteClient still cannot send anything else.

The write tools add two more checks on top (writer.py): each write must first
be shown as a dry run, and the real call must carry that dry run's confirm code.

Every write that is sent is recorded in state/write_log.jsonl: one JSON object
per line, only ever appended. A "sending" line is written just before the
request goes out and a "done" or "failed" line when the answer comes back, so a
crash in between still leaves a record. Tokens are never logged.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from filelock import FileLock

from .client import BucketLimiter, _error_message
from .config import API_ROOT, HTTP_TIMEOUT_SECONDS, STATE_DIR, USER_AGENT
from .errors import (ApiError, AuthError, LightspeedError, RateLimitedError, WriteBlockedError,
                     WriteOutcomeUnknown, WritesDisabledError)
from .tokens import TokenManager

log = logging.getLogger(__name__)

# The master write switch. Writes are possible only while this file exists.
WRITE_SWITCH_PATH = STATE_DIR / "WRITES_ENABLED"
WRITE_LOG_PATH = STATE_DIR / "write_log.jsonl"

WRITES_OFF_MESSAGE = (
    "Writes to Lightspeed are turned OFF on this computer, so nothing was sent and nothing changed. "
    "Only the store owner can turn writes on, by hand (README.md, 'Turning writes on and off'). "
    "Dry runs (dry_run=true) still work."
)

# Lightspeed charges 1 drip per request plus 0.5 per nested record in the body
# (developers.lightspeedhq.com/retail/introduction/ratelimits). 2 is a safe
# estimate; the real level comes back in every response's headers.
WRITE_COST = 2.0


# What a nested record in a body may contain, wherever it appears. A price can only carry an
# amount and its level; a stock row (ItemShops, which only adjust_stock's route allows) only its
# ID and the new quantity on hand.
NESTED_FIELDS = {
    ("Prices", "ItemPrice"): frozenset({"amount", "useTypeID", "useType"}),
    ("ItemAttributes", None): frozenset({"itemAttributeSetID", "attribute1", "attribute2", "attribute3"}),
    ("ItemVendorNums", "ItemVendorNum"): frozenset({"itemVendorNumID", "vendorID", "value", "cost"}),
    ("ItemShops", "ItemShop"): frozenset({"itemShopID", "qoh"}),
}


@dataclass(frozen=True)
class Route:
    """One kind of request a write tool may send."""

    method: str
    path: re.Pattern  # relative to the account, e.g. Item/123.json
    fields: frozenset[str]  # top-level JSON fields the body may contain (nested ones: NESTED_FIELDS)


def nested_problem(body: dict) -> str | None:
    """What is wrong with the body's nested records (see NESTED_FIELDS), or None."""
    for (outer, inner), allowed in NESTED_FIELDS.items():
        if outer not in body:
            continue
        value = body[outer]
        if inner is not None:
            if not isinstance(value, dict) or set(value) != {inner}:
                return f"{outer} must hold only {inner}"
            value = value[inner]
        for record in value if isinstance(value, list) else [value]:
            if not isinstance(record, dict):
                return f"{outer} holds something that isn't a record"
            extra = sorted(set(record) - allowed)
            if extra:
                return f"{outer} may not contain {', '.join(extra)}"
    return None


ITEM_FIELDS = {"description", "itemType", "tax", "taxClassID", "categoryID", "manufacturerID", "defaultVendorID",
               "defaultCost", "Prices"}

# The kinds of request the write tools can make. Each tool lists the ones it may use (WRITE_ROUTES below).
NEW_MATRIX = Route("POST", re.compile(r"ItemMatrix\.json"), frozenset(ITEM_FIELDS | {"itemAttributeSetID"}))
# An existing matrix. Lightspeed applies description, category, brand and vendor ("Shared values") to
# every item in it; price and cost are defaults for new items. Never the attribute set, attribute
# values or anything that archives.
UPDATE_MATRIX = Route("PUT", re.compile(r"ItemMatrix/\d+\.json"),
                      frozenset({"description", "categoryID", "manufacturerID", "defaultVendorID", "defaultCost",
                                 "Prices"}))
NEW_MATRIX_ITEM = Route("POST", re.compile(r"Item\.json"),
                        frozenset(ITEM_FIELDS | {"itemMatrixID", "customSku", "upc", "ItemAttributes", "ItemVendorNums"}))
ITEM_SKU_COST_DESCRIPTION = Route("PUT", re.compile(r"Item/\d+\.json"),
                                  frozenset({"customSku", "defaultCost", "description"}))  # never prices or stock
# Put an existing item into a matrix: its matrix, Color/Size/Fit, and (optionally) the
# matrix's description pattern, category, vendor and manufacturer. Never prices, cost or stock.
ITEM_INTO_MATRIX = Route("PUT", re.compile(r"Item/\d+\.json"),
                         frozenset({"itemMatrixID", "ItemAttributes", "description", "categoryID", "manufacturerID",
                                    "defaultVendorID"}))
ITEM_SKU = Route("PUT", re.compile(r"Item/\d+\.json"), frozenset({"customSku"}))
# An item's default vendor, brand and category, for items outside a matrix (a matrix sets these for
# all its items: update_matrix). Never prices, cost, stock or vendor numbers.
ITEM_VENDOR_BRAND_CATEGORY = Route("PUT", re.compile(r"Item/\d+\.json"),
                                   frozenset({"defaultVendorID", "manufacturerID", "categoryID"}))
# An item's prices, and nothing else. Only update_item_prices may send this.
ITEM_PRICES = Route("PUT", re.compile(r"Item/\d+\.json"), frozenset({"Prices"}))
# An item's reorder point and level, on Lightspeed's ItemShop endpoint, which (per its docs) only
# updates those two settings. Stock itself (qoh) is not a field here and can never be sent.
ITEM_SHOP_REORDER = Route("PUT", re.compile(r"ItemShop/\d+\.json"), frozenset({"reorderPoint", "reorderLevel"}))
# Vendors: rename / account number, and archive (Lightspeed archives a vendor on DELETE).
UPDATE_VENDOR = Route("PUT", re.compile(r"Vendor/\d+\.json"), frozenset({"name", "accountNumber"}))
ARCHIVE_VENDOR = Route("DELETE", re.compile(r"Vendor/\d+\.json"), frozenset())
# A brand's name (Lightspeed can't delete a brand).
RENAME_BRAND = Route("PUT", re.compile(r"Manufacturer/\d+\.json"), frozenset({"name"}))
# Categories. Lightspeed deletes a category for good (it refuses one with subcategories).
NEW_CATEGORY = Route("POST", re.compile(r"Category\.json"), frozenset({"name", "fullPathName", "parentID"}))
RENAME_CATEGORY = Route("PUT", re.compile(r"Category/\d+\.json"), frozenset({"name", "fullPathName"}))
DELETE_CATEGORY = Route("DELETE", re.compile(r"Category/\d+\.json"), frozenset())
# An item's stock on hand in one shop: its stock row (by itemShopID) with the new qoh, and nothing
# else. Lightspeed's Item update takes ItemShops; the ItemShop endpoint itself only takes reorder
# settings. Only adjust_stock may send it (README, "Changing stock").
ITEM_STOCK = Route("PUT", re.compile(r"Item/\d+\.json"), frozenset({"ItemShops"}))
# A category's place in the tree: its new parent and the path that goes with it. Never its name.
MOVE_CATEGORY = Route("PUT", re.compile(r"Category/\d+\.json"), frozenset({"parentID", "fullPathName"}))
# Attribute sets (matrix types): a name and up to three attribute names. Never `archived` or `system`.
ATTRIBUTE_SET_FIELDS = frozenset({"name", "attributeName1", "attributeName2", "attributeName3"})
NEW_ATTRIBUTE_SET = Route("POST", re.compile(r"ItemAttributeSet\.json"), ATTRIBUTE_SET_FIELDS)
UPDATE_ATTRIBUTE_SET = Route("PUT", re.compile(r"ItemAttributeSet/\d+\.json"), ATTRIBUTE_SET_FIELDS)
# An inventory count and its counted items. Stock changes only when the count is reconciled, which
# is never sent from here (it stays a click in Lightspeed).
NEW_COUNT = Route("POST", re.compile(r"InventoryCount\.json"), frozenset({"name", "shopID"}))
NEW_COUNT_ITEM = Route("POST", re.compile(r"InventoryCountItem\.json"),
                       frozenset({"inventoryCountID", "itemID", "qty", "employeeID"}))
# Lightspeed archives a PO on DELETE. Only POs with nothing received (archive_purchase_order checks).
ARCHIVE_ORDER = Route("DELETE", re.compile(r"Order/\d+\.json"), frozenset())
# A new item outside any matrix (shirts, ties, shoes): no itemMatrixID, no ItemAttributes.
NEW_STANDALONE_ITEM = Route("POST", re.compile(r"Item\.json"),
                            frozenset(ITEM_FIELDS | {"customSku", "upc", "ItemVendorNums"}))
# The barcode number (UPC) on an existing item, and nothing else.
ITEM_UPC = Route("PUT", re.compile(r"Item/\d+\.json"), frozenset({"upc"}))
# The vendor's own item number(s) on an existing item. The body lists the item's existing vendor
# numbers too, so they are kept whether Lightspeed merges or replaces the list.
ITEM_VENDOR_NUMBERS = Route("PUT", re.compile(r"Item/\d+\.json"), frozenset({"ItemVendorNums"}))
# Lightspeed archives an item on DELETE (items are never really deleted). Items only, never a matrix.
ARCHIVE_ITEM = Route("DELETE", re.compile(r"Item/\d+\.json"), frozenset())
NEW_ORDER_LINE = Route("POST", re.compile(r"OrderLine\.json"),
                       frozenset({"orderID", "itemID", "quantity", "price", "originalPrice", "numReceived"}))
ORDER_LINE_RECEIVED = Route("PUT", re.compile(r"OrderLine/\d+\.json"), frozenset({"numReceived"}))
# Lightspeed deletes a PO line for good (no archive). Only lines with nothing received, on unfinished POs.
REMOVE_ORDER_LINE = Route("DELETE", re.compile(r"OrderLine/\d+\.json"), frozenset())
# A new vendor: its name and the store's account number with it. Nothing else (catalog
# settings, contacts) is sent. Vendors are never updated or archived here.
NEW_VENDOR = Route("POST", re.compile(r"Vendor\.json"), frozenset({"name", "accountNumber"}))
# A new brand (Lightspeed calls it a Manufacturer): only its name. Lightspeed can't delete one.
NEW_BRAND = Route("POST", re.compile(r"Manufacturer\.json"), frozenset({"name"}))
# A new, empty purchase order (lines are added separately; the docs don't allow lines in this POST).
NEW_ORDER = Route("POST", re.compile(r"Order\.json"),
                  frozenset({"vendorID", "shopID", "refNum", "shipCost", "otherCost"}))
# A PO's own fields. orderedDate moves it to Ordered, receivedDate to Check In. Never `complete`
# or `archived`: finishing a PO stays a click in Lightspeed, after Add Received To Inventory.
ORDER_HEADER = Route("PUT", re.compile(r"Order/\d+\.json"),
                     frozenset({"refNum", "shipCost", "otherCost", "orderedDate", "receivedDate"}))
# A line's quantity and unit cost (price = originalPrice = the invoice's unit cost; Lightspeed
# spreads shipping into price later, by the PO's own setting).
ORDER_LINE_EDIT = Route("PUT", re.compile(r"OrderLine/\d+\.json"), frozenset({"quantity", "price", "originalPrice"}))
# Intake: the same, plus what was received.
ORDER_LINE_INTAKE = Route("PUT", re.compile(r"OrderLine/\d+\.json"),
                          frozenset({"quantity", "price", "originalPrice", "numReceived"}))

# Tool name -> the kinds of request it may send. Nothing else can be written.
WRITE_ROUTES: dict[str, tuple[Route, ...]] = {
    "create_matrix": (NEW_MATRIX,),
    "add_matrix_variants": (NEW_MATRIX_ITEM,),
    "update_item_fields": (ITEM_SKU_COST_DESCRIPTION,),
    "add_po_lines": (NEW_ORDER_LINE,),
    "receive_po_lines": (ORDER_LINE_RECEIVED,),
    "move_items_into_matrix": (ITEM_INTO_MATRIX,),
    "assign_custom_skus": (ITEM_SKU,),
    "archive_item": (ARCHIVE_ITEM,),
    "create_items": (NEW_STANDALONE_ITEM,),
    "create_purchase_order": (NEW_ORDER,),
    "remove_po_lines": (REMOVE_ORDER_LINE,),
    "update_purchase_order": (ORDER_HEADER,),
    "update_po_lines": (ORDER_LINE_EDIT,),
    "set_vendor_numbers": (ITEM_VENDOR_NUMBERS,),
    # One invoice or packing slip, approved once: a PO (new or existing), new items, vendor
    # numbers, lines and received quantities, and the PO's dates.
    "intake_invoice": (NEW_VENDOR, NEW_BRAND, NEW_ORDER, NEW_MATRIX_ITEM, NEW_STANDALONE_ITEM, ITEM_VENDOR_NUMBERS,
                       NEW_ORDER_LINE, ORDER_LINE_INTAKE, ORDER_HEADER),
    "update_matrix": (UPDATE_MATRIX,),
    "create_vendor": (NEW_VENDOR,),
    "create_brand": (NEW_BRAND,),
    "update_item_prices": (ITEM_PRICES,),
    "set_reorder_levels": (ITEM_SHOP_REORDER,),
    "archive_purchase_order": (ARCHIVE_ORDER,),
    "correct_vendor_numbers": (ITEM_VENDOR_NUMBERS,),
    "update_vendor": (UPDATE_VENDOR,),
    "archive_vendor": (ARCHIVE_VENDOR,),
    "rename_brand": (RENAME_BRAND,),
    "create_category": (NEW_CATEGORY,),
    "rename_category": (RENAME_CATEGORY,),
    "delete_category": (DELETE_CATEGORY,),
    "create_inventory_count": (NEW_COUNT, NEW_COUNT_ITEM),
    "create_attribute_set": (NEW_ATTRIBUTE_SET,),
    "update_attribute_set": (UPDATE_ATTRIBUTE_SET,),
    "reassign_items": (ITEM_VENDOR_BRAND_CATEGORY,),
    "set_upcs": (ITEM_UPC,),
    "move_category": (MOVE_CATEGORY,),
    "adjust_stock": (ITEM_STOCK,),
}


def all_routes() -> list[tuple[str, Route]]:
    """Every (tool, route) pair."""
    return [(tool, route) for tool, routes in WRITE_ROUTES.items() for route in routes]


class WriteSwitch:
    """The master switch: ON only while the switch file exists. Nothing in this project creates it."""

    def __init__(self, path: Path = WRITE_SWITCH_PATH):
        self.path = Path(path)

    def is_on(self) -> bool:
        return self.path.is_file()

    def require_on(self) -> None:
        if not self.is_on():
            raise WritesDisabledError(WRITES_OFF_MESSAGE)


class WriteLog:
    """Append-only record of every write sent (state/write_log.jsonl)."""

    def __init__(self, path: Path = WRITE_LOG_PATH):
        self.path = Path(path)

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"time": datetime.now().astimezone().isoformat(timespec="seconds"), **entry},
                          ensure_ascii=False)
        # The lock keeps lines from two processes (e.g. two Claude windows) from interleaving.
        with FileLock(str(self.path) + ".lock", timeout=30):
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def entries(self) -> list[dict]:
        """Every line, oldest first. A line that isn't valid JSON (e.g. cut short by a crash) is skipped."""
        if not self.path.exists():
            return []
        found = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                found.append(json.loads(line))
            except ValueError:
                if line.strip():
                    log.warning("Skipped an unreadable line in %s.", self.path)
        return found


def write_log_report(switch: WriteSwitch, write_log: WriteLog, limit: int = 20) -> dict:
    """The switch state and the logged writes, one row per write, newest first."""
    writes: dict[str, dict] = {}
    for e in write_log.entries():
        w = writes.setdefault(e.get("write_id", "?"), {
            "write_id": e.get("write_id"), "tool": e.get("tool"), "method": e.get("method"), "path": e.get("path"),
            "confirm_code": e.get("confirm_code")})
        if e.get("event") == "sending":
            w["sent"] = e.get("time")
            w["payload"] = e.get("payload")
            if e.get("before"):
                w["before"] = e["before"]
        elif e.get("event") == "done":
            w.update(outcome="done", status=e.get("status"), affected_ids=e.get("affected_ids"))
        elif e.get("event") == "failed":
            w.update(outcome="failed", status=e.get("status"), error=e.get("error"))
    for w in writes.values():
        # A "sending" line with no answer: the process stopped while the request was out.
        w.setdefault("outcome", "unknown: no answer was recorded; check the record in Lightspeed")
    rows = list(reversed(list(writes.values())))
    limit = max(1, min(int(limit), 500))
    result = {"writes_enabled": switch.is_on(), "log_file": str(write_log.path), "writes_logged": len(rows),
              "writes": rows[:limit]}
    if not rows:
        result["note"] = "No write has ever been sent from this computer."
    return result


def route_allows(route: Route, method: str, relative_path: str) -> bool:
    return route.method == method.upper() and route.path.fullmatch(relative_path) is not None


def make_write_http(base_url: str, switch: WriteSwitch, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """An httpx client that can only send the write tools' own requests, and only while the switch is on."""

    def check_request(request: httpx.Request) -> None:
        # Last line of defence: runs on every request, just before it is sent.
        if not switch.is_on():
            raise WritesDisabledError(WRITES_OFF_MESSAGE)
        url = str(request.url)
        relative = url[len(base_url):] if url.startswith(base_url) else None
        if relative is None or request.url.query or not any(
                route_allows(r, request.method, relative) for _, r in all_routes()):
            raise WriteBlockedError(f"Blocked {request.method} {request.url.path}: not one of the write tools' requests.")

    return httpx.Client(
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        event_hooks={"request": [check_request]},
        follow_redirects=False,
        transport=transport,
    )


# The ID fields worth recording from a response record.
ID_FIELDS = ("itemMatrixID", "itemID", "orderID", "orderLineID", "vendorID", "manufacturerID", "categoryID",
             "inventoryCountID", "inventoryCountItemID", "itemShopID")


def affected_ids(body: dict) -> dict:
    """{"Item": {"itemID": "2001", "itemMatrixID": "8"}} from a response like {"Item": {...}}."""
    found = {}
    for key, record in (body or {}).items():
        if key != "@attributes" and isinstance(record, dict):
            found[key] = {f: record[f] for f in ID_FIELDS if record.get(f) not in (None, "", "0")}
    return found


class WriteClient:
    """Sends the write tools' requests. Held only by InventoryWriter (writer.py)."""

    def __init__(
        self,
        account_id: str,
        tokens: TokenManager,
        limiter: BucketLimiter,
        switch: WriteSwitch | None = None,
        write_log: WriteLog | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
        max_rate_limit_retries: int = 5,
    ):
        self.account_id = str(account_id)
        self.base_url = f"{API_ROOT}Account/{self.account_id}/"
        self.tokens = tokens
        self.limiter = limiter  # shared with the read client: one bucket per account
        self.switch = switch or WriteSwitch()
        self.write_log = write_log or WriteLog()
        self.http = make_write_http(self.base_url, self.switch, transport)
        self.sleep = sleep
        self.max_rate_limit_retries = max_rate_limit_retries

    def full_path(self, path: str) -> str:
        """What a dry run shows: the path exactly as it would be requested."""
        return f"/API/V3/Account/{self.account_id}/{path}"

    def check(self, tool: str, method: str, path: str, body: dict | None) -> None:
        """Refuse anything but the tool's own kind of request with allowed fields. Sends nothing.

        Dry runs call this too, so every payload a dry run shows is one the write path would accept.
        """
        routes = WRITE_ROUTES.get(tool)
        if routes is None:
            raise WriteBlockedError(f"'{tool}' is not a write tool; only {', '.join(WRITE_ROUTES)} can write.")
        matching = [r for r in routes if route_allows(r, method, path)]
        if not matching:
            allowed = "; ".join(f"{r.method} {r.path.pattern}" for r in routes)
            raise WriteBlockedError(f"{tool} may only send {allowed}, not {method} {path}.")
        if method.upper() == "DELETE":
            if body:
                raise WriteBlockedError(f"{tool}: a DELETE has no body.")
            return
        if not isinstance(body, dict) or not body:
            raise WriteBlockedError(f"{tool}: the request body must be a non-empty JSON object.")
        if not any(set(body) <= r.fields for r in matching):
            extra = sorted(set(body) - frozenset().union(*(r.fields for r in matching)))
            raise WriteBlockedError(f"{tool} may not send the field(s) {', '.join(extra) or 'in this combination'}.")
        problem = nested_problem(body)
        if problem:
            raise WriteBlockedError(f"{tool}: {problem}.")
        if "ItemShops" in body:
            # One shop's own stock row, named by its ID. Never a row without one (Lightspeed's shop 0 row
            # is the all-shops total), never several at once.
            rows = body["ItemShops"]["ItemShop"]
            rows = rows if isinstance(rows, list) else [rows]
            if len(rows) != 1 or set(rows[0]) != {"itemShopID", "qoh"} or not str(rows[0]["itemShopID"]).isdigit() \
                    or str(rows[0]["itemShopID"]) == "0" or not re.fullmatch(r"-?\d+", str(rows[0]["qoh"])):
                raise WriteBlockedError(f"{tool}: a stock change is exactly one stock row: its itemShopID and the new "
                                        "whole-number qoh.")

    def send(self, tool: str, method: str, path: str, body: dict | None, confirm_code: str,
             before: dict | None = None) -> dict:
        """Send one write. Returns {"status", "affected_ids", "record"}; raises on any refusal or failure.

        `before` is the record as it was, logged with the request so a change
        (e.g. archiving) can be put back by hand later.
        """
        self.switch.require_on()
        self.check(tool, method, path, body)
        write_id = uuid.uuid4().hex[:12]
        entry = {"write_id": write_id, "tool": tool, "confirm_code": confirm_code, "method": method,
                 "path": self.full_path(path)}
        sending = {**entry, "event": "sending", "payload": body}
        if before:
            sending["before"] = before
        self.write_log.append(sending)
        try:
            status, response_body = self._send(method, path, body)
        except LightspeedError as e:
            self.write_log.append({**entry, "event": "failed", "status": getattr(e, "status_code", None),
                                   "error": str(e)})
            raise
        ids = affected_ids(response_body)
        self.write_log.append({**entry, "event": "done", "status": status, "affected_ids": ids})
        record = next((v for k, v in response_body.items() if k != "@attributes" and isinstance(v, dict)), {})
        return {"status": status, "affected_ids": ids, "record": record}

    def _send(self, method: str, path: str, body: dict | None) -> tuple[int, dict]:
        """Auth, rate limit and error handling for one write.

        Retried: 401 (once, with a fresh token) and 429, because Lightspeed did not
        act on those. Never retried: network errors and 5xx, because the write
        may have gone through; those raise WriteOutcomeUnknown.
        """
        url = self.base_url + path
        retried_auth = False
        rate_limit_retries = 0
        while True:
            self.limiter.wait_for_room(WRITE_COST)
            token = self.tokens.access_token()
            try:
                response = self.http.request(method, url, json=body or None,
                                             headers={"Authorization": f"Bearer {token}"})
            except httpx.TransportError as e:
                raise WriteOutcomeUnknown(
                    f"Network error during {method} {path} ({type(e).__name__}). Lightspeed may or may not have made "
                    "this change: check the record in Lightspeed before trying again."
                ) from None

            self.limiter.update(response.headers)
            status = response.status_code
            if status == 401 and not retried_auth:
                retried_auth = True
                self.tokens.refresh_after_401(token)
                continue
            if status == 401:
                raise AuthError("Lightspeed rejected a freshly refreshed access token (401). Re-run bootstrap.py.")
            if status == 429:
                rate_limit_retries += 1
                if rate_limit_retries > self.max_rate_limit_retries:
                    raise RateLimitedError("Lightspeed is still rate limiting this write. Nothing was changed; try again later.")
                self.sleep(self.limiter.retry_delay(response.headers))
                continue
            if status >= 500:
                raise WriteOutcomeUnknown(
                    f"Lightspeed answered HTTP {status} to {method} {path}. It may or may not have made this change: "
                    "check the record in Lightspeed before trying again."
                )
            if status >= 400:
                raise ApiError(status, _error_message(response))
            try:
                return status, response.json()
            except ValueError:
                raise WriteOutcomeUnknown(
                    f"Lightspeed answered HTTP {status} to {method} {path} without JSON. Check the record in Lightspeed."
                ) from None
