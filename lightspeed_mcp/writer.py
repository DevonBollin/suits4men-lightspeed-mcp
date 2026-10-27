"""The write tools' logic: plan a change, show it as a dry run, and send it only when allowed.

Each public method of InventoryWriter is one write tool (server.py describes them for Claude):

  create_matrix         POST ItemMatrix.json         a new, empty matrix under attribute set 5
  add_matrix_variants   POST Item.json, per item     new child items (color/size/fit) in a matrix
  update_item_fields    PUT  Item/<id>.json          custom SKU, default cost, description; never prices
  add_po_lines          POST OrderLine.json, per line   new lines on an open purchase order
  receive_po_lines      PUT  OrderLine/<id>.json     mark quantities as received on PO lines
  move_items_into_matrix  PUT Item/<id>.json, per item   put existing items into a matrix with Color/Size/Fit
  assign_custom_skus    PUT  Item/<id>.json, per item   give items without a SKU their own one
  archive_item          DELETE Item/<id>.json        archive one item (only with stock 0); never a matrix

Every method works the same way:
  1. With dry_run=False and the master write switch off, refuse at once, before
     reading anything.
  2. Read what it needs (GET only, through the read client) and check the
     store's rules (rules.py). Any problem refuses the whole request.
  3. Build the exact requests it would send (a Plan), and run each through
     WriteClient.check, so a dry run only ever shows payloads the write path accepts.
  4. dry_run=True, the default: return the plan (method, path, JSON body,
     plain-language summary, warnings) and its confirm code. Nothing is sent.
  5. dry_run=False: the confirm code must match the plan as rebuilt now, i.e.
     the payload the user saw is unchanged. Then each request goes through
     WriteClient.send (write_path.py), which checks the switch again and logs it.

What the Lightspeed docs say about these requests, and what is still
unconfirmed on the live account, is in NOTES.md ("Phase 2: writes").
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from . import format as fmt
from . import intake, rules
from .config import COUNT_EMPLOYEE_ID
from .errors import ApiError, LightspeedError, WriteRefused
from .reader import InventoryReader, batches, id_filter, relations
from .rules import RuleViolation
from .write_path import WriteClient

MAX_REQUESTS_PER_CALL = 100  # variants or PO lines in one call
MAX_LINE_QUANTITY = 1000  # a sanity limit for one PO line
MAX_INVOICE_LINES = 100  # one invoice; each line can need up to 3 requests (item, vendor number, PO line)


# ---- the plan: exactly what would be sent ---------------------------------------------------


@dataclass
class PlannedRequest:
    label: str  # what this one request does, in a few words
    method: str
    path: str  # relative to the account, e.g. "Item.json"
    body: dict | None  # None for a DELETE
    before: dict | None = None  # the record as it is now, saved in the write log (archiving, removing)
    # For a request that creates a record another request in the same plan needs (an invoice's
    # new PO, a new item): its name, e.g. "order". Later bodies say "{new:order}" where the new
    # record's ID goes; the real ID is filled in from Lightspeed's reply when the plan is run.
    creates: str | None = None


# Which ID field a created record's ID is read from, by record type.
CREATED_ID_FIELDS = {"Order": "orderID", "Item": "itemID", "OrderLine": "orderLineID", "ItemMatrix": "itemMatrixID",
                     "Vendor": "vendorID", "Manufacturer": "manufacturerID", "Category": "categoryID",
                     "InventoryCount": "inventoryCountID"}


def new_ref(name: str) -> str:
    """How a body refers to a record the same plan creates first."""
    return "{new:" + name + "}"


NEW_REF = re.compile(r"\{new:([^}]+)\}")


def fill_in_path(path: str, created: dict[str, str]) -> str:
    """'Order/{new:order}.json' -> 'Order/900.json' once the new PO exists."""
    def replace(m):
        if m.group(1) not in created:
            raise WriteRefused(f"'{m.group(1)}' was not created, so this request can't be sent.")
        return created[m.group(1)]
    return NEW_REF.sub(replace, path)


def fill_in_new_ids(value, created: dict[str, str]):
    """Replace every "{new:name}" in a body with the ID that request got. Unknown names are an error."""
    if isinstance(value, dict):
        return {k: fill_in_new_ids(v, created) for k, v in value.items()}
    if isinstance(value, list):
        return [fill_in_new_ids(v, created) for v in value]
    if isinstance(value, str) and value.startswith("{new:") and value.endswith("}"):
        name = value[5:-1]
        if name not in created:
            raise WriteRefused(f"'{name}' was not created, so this request can't be sent.")
        return created[name]
    return value


@dataclass
class Plan:
    tool: str
    summary: str
    requests: list[PlannedRequest]
    warnings: list[str] = field(default_factory=list)
    after: str | None = None  # anything the user still has to do in Lightspeed afterwards
    # Facts the plan relies on that the requests don't show (e.g. an item's stock before
    # archiving). They are part of the confirm code, so a change since the dry run is caught.
    basis: dict | None = None
    max_requests: int = 100  # an invoice intake may need more (a PO, items, lines, dates)

    def confirm_code(self) -> str:
        """A short fingerprint of the exact requests (and basis). Any change to either changes it."""
        canonical = json.dumps([self.tool, [[r.method, r.path, r.body] for r in self.requests], self.basis],
                               sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]


# ---- small value helpers --------------------------------------------------------------------


def money(value, what: str) -> str:
    """A positive amount as Lightspeed's string form with 2 decimals, e.g. 65.5 -> "65.50"."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise RuleViolation(f"Refused: {what} '{value}' is not a number.") from None
    if not math.isfinite(amount) or amount <= 0:
        raise RuleViolation(f"Refused: {what} must be more than 0 (got {value}).")
    return f"{round(amount, 2):.2f}"


def quantity(value, what: str = "quantity") -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise RuleViolation(f"Refused: {what} '{value}' is not a whole number.") from None
    if n != float(value) or n < 1:
        raise RuleViolation(f"Refused: {what} must be a whole number of at least 1 (got {value}).")
    if n > MAX_LINE_QUANTITY:
        raise RuleViolation(f"Refused: {what} {n} is more than {MAX_LINE_QUANTITY}; split it or check the number.")
    return n


def money_or_zero(value, what: str) -> str:
    """Like money(), but 0 is allowed (e.g. no shipping cost on an invoice)."""
    if value in (None, "", 0, 0.0, "0"):
        return "0.00"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise RuleViolation(f"Refused: {what} '{value}' is not a number.") from None
    if not math.isfinite(amount) or amount < 0:
        raise RuleViolation(f"Refused: {what} can't be negative (got {value}).")
    return f"{round(amount, 2):.2f}"


def signed_money(value, what: str) -> str:
    """An amount that may be negative (a PO's other costs: negative is a discount, per Lightspeed's docs)."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise RuleViolation(f"Refused: {what} '{value}' is not a number.") from None
    if not math.isfinite(amount):
        raise RuleViolation(f"Refused: {what} '{value}' is not a number.")
    return f"{round(amount, 2):.2f}"


def iso_date(value, what: str) -> str:
    """A date as YYYY-MM-DD (Lightspeed accepts a date alone and stores it as a full timestamp)."""
    text = rules.clean(value)
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise RuleViolation(f"Refused: {what} '{value}' is not a date like 2026-10-26.") from None
    return text


def default_price(amount: str) -> dict:
    """The Prices block for a Default price only. MSRP and Online are never sent."""
    return {"ItemPrice": [{"amount": amount, "useTypeID": "1", "useType": "Default"}]}


def item_label(item: dict) -> str:
    sku = fmt.text(item.get("customSku"))
    return f"item {item.get('itemID')} '{fmt.text(item.get('description'))}'" + (f" (SKU {sku})" if sku else " (no SKU)")


class SkuAllocator:
    """Custom SKUs for one plan: checks the ones given, picks the next free ones for the rest.

    Built from every item (archived ones too), so a SKU still on an old label is never reused.
    Picked SKUs continue after the highest 6-digit SKU in use; gaps below it are not reused.
    """

    def __init__(self, all_items: list[dict], given: list[str | None]):
        self.items_by_id = {str(i.get("itemID")): i for i in all_items}
        self.holders: dict[str, dict] = {}
        for i in all_items:
            if fmt.text(i.get("customSku")):
                self.holders.setdefault(fmt.text(i.get("customSku")), i)
        six_digit = sorted(s for s in self.holders if rules.CUSTOM_SKU_PATTERN.match(s))
        self.highest = six_digit[-1] if six_digit else None
        self.next_free = int(self.highest) + 1 if self.highest else 1
        self.picked = 0
        # The SKUs given in the request are checked and set aside first, so none is picked for another item.
        given = [rules.check_custom_sku_format(sku) for sku in given if sku]
        rules.check_skus_unique_in_request(given)
        for sku in given:
            if sku in self.holders:
                raise RuleViolation(f"Refused: custom SKU {sku} is already used by {item_label(self.holders[sku])}. "
                                    "Every item gets its own SKU.")
        self.reserved = set(given)

    def take(self, given: str | None) -> str:
        """The given SKU (already checked), or the next number nobody holds or is about to get."""
        if given:
            return rules.clean(given)
        while f"{self.next_free:06d}" in self.holders or f"{self.next_free:06d}" in self.reserved:
            self.next_free += 1
        if self.next_free > 999_999:
            raise RuleViolation("Refused: the 6-digit SKU range is used up.")
        sku = f"{self.next_free:06d}"
        self.reserved.add(sku)
        self.picked += 1
        return sku

    def picked_note(self) -> str:
        if not self.picked:
            return ""
        return f" ({self.picked} picked automatically, continuing after the highest in use, {self.highest})"


class InventoryWriter:
    def __init__(self, reader: InventoryReader, writes: WriteClient):
        self.reader = reader
        self.client = reader.client  # the GET-only read client, for lookups
        self.writes = writes

    # ---- shared steps ---------------------------------------------------------------------

    def _run(self, tool: str, dry_run: bool, confirm_code: str | None, build) -> dict:
        if not dry_run:
            self.writes.switch.require_on()  # refuse before reading anything
            if not (confirm_code or "").strip():
                raise WriteRefused(
                    f"Refused: {tool} with dry_run=false needs the confirm_code from a dry run of this exact change, "
                    "after the user has seen and approved that dry run. Nothing was sent."
                )
        plan = build()
        if len(plan.requests) > plan.max_requests:
            raise RuleViolation(f"Refused: {len(plan.requests)} requests in one call; the limit is {plan.max_requests}.")
        for r in plan.requests:
            # A path naming a record the plan creates first is checked with a stand-in ID.
            self.writes.check(tool, r.method, NEW_REF.sub("0", r.path), r.body)
        code = plan.confirm_code()
        if dry_run:
            return self._dry_run_result(plan, code)
        if confirm_code.strip() != code:
            raise WriteRefused(
                f"Refused: confirm_code '{confirm_code}' does not match this change (now '{code}'). The arguments or "
                "the data in Lightspeed changed since the dry run. Nothing was sent. Run the dry run again and show "
                "the user the new payload."
            )
        return self._execute(plan, code)

    def _dry_run_result(self, plan: Plan, code: str) -> dict:
        writes_on = self.writes.switch.is_on()
        how = (f"Show the user the summary, any warnings and every request body above. Only if the user approves this "
               f"exact change in chat, call {plan.tool} again with the same arguments plus dry_run=false and "
               f"confirm_code=\"{code}\". An approval covers this one change only.")
        if not writes_on:
            how += " Writes are currently turned off on this computer, so that call would be refused."
        result = {
            "dry_run": True,
            "nothing_sent": "This was a dry run. Nothing was sent to Lightspeed.",
            "summary": plan.summary,
            "warnings": plan.warnings,
            "after": plan.after,
            "request_count": len(plan.requests),
            "requests": [{"does": r.label, "method": r.method, "path": self.writes.full_path(r.path), "body": r.body}
                         for r in plan.requests],
            "confirm_code": code,
            "writes_enabled": writes_on,
            "how_to_apply": how,
        }
        return fmt.prune(result)

    def _execute(self, plan: Plan, code: str) -> dict:
        done = []
        created: dict[str, str] = {}  # "{new:...}" name -> the ID Lightspeed gave the new record
        for index, r in enumerate(plan.requests):
            try:
                path, body = fill_in_path(r.path, created), fill_in_new_ids(r.body, created)
                sent = self.writes.send(plan.tool, r.method, path, body, code, before=r.before)
            except LightspeedError as e:
                failed = {"does": r.label, "method": r.method, "path": self.writes.full_path(r.path), "error": str(e)}
                return self._stopped(plan, done, created, failed, plan.requests[index + 1:])
            done.append({"does": r.label, "status": sent["status"], "affected_ids": sent["affected_ids"]})
            if r.creates:
                record_type = r.path.removesuffix(".json").split("/")[0]
                new_id = fmt.text(sent["record"].get(CREATED_ID_FIELDS.get(record_type, "")))
                if not new_id:
                    failed = {"does": r.label, "error": f"Lightspeed's reply had no new {record_type} ID, so nothing "
                                                        "that depends on it was sent."}
                    return self._stopped(plan, done, created, failed, plan.requests[index + 1:])
                created[r.creates] = new_id
        return fmt.prune({"dry_run": False, "finished": True, "summary": plan.summary, "done": done,
                          "created": created, "after": plan.after, "write_log": str(self.writes.write_log.path)})

    def _stopped(self, plan: Plan, done: list, created: dict, failed: dict, not_sent: list[PlannedRequest]) -> dict:
        return fmt.prune({
            "dry_run": False,
            "finished": False,
            "summary": plan.summary,
            "done": done,
            "created": created,
            "failed": failed,
            "not_sent": [p.label for p in not_sent],
            "note": "Stopped at the first failure. Requests under 'done' were made in Lightspeed; nothing after "
                    "the failure was sent. Check Lightspeed before trying anything again.",
            "write_log": str(self.writes.write_log.path),
        })

    # ---- lookups (GET only) -------------------------------------------------------------

    def _get_record(self, path: str, key: str, params: dict | None = None) -> dict | None:
        try:
            record = self.client.get(path, params).get(key)
        except ApiError as e:
            if e.status_code == 404:
                return None
            raise
        return record if isinstance(record, dict) else None

    def _all_matrices(self) -> list[dict]:
        rows, _ = self.client.get_all("ItemMatrix.json", {"archived": "true"})
        return rows

    def _known_colors(self, matrices: list[dict]) -> set[str]:
        """Every color value the store's matrices use, plus common color words."""
        colors = set(rules.COMMON_COLOR_WORDS)
        for m in matrices:
            slots = fmt.attribute_slots(self.reader.attribute_sets().get(str(m.get("itemAttributeSetID"))))
            for index, name in slots.items():
                if name.lower() == "color":
                    colors.update(fmt.matrix_attribute_values(m, index))
        return colors

    def _items_with_skus(self, skus: list[str]) -> dict[str, list[dict]]:
        """{sku: [items that already use it]} across all items, archived ones too."""
        found: dict[str, list[dict]] = {}
        if len(skus) <= 8:
            for sku in skus:  # exact-match lookups
                rows, _ = self.client.get_all("Item.json", {"customSku": sku, "archived": "true"}, max_records=10)
                found[sku] = [r for r in rows if fmt.text(r.get("customSku")) == sku]
        else:  # many SKUs: one pass over every item is fewer requests
            rows, _ = self.client.get_all("Item.json", {"archived": "true"})
            wanted = set(skus)
            for r in rows:
                sku = fmt.text(r.get("customSku"))
                if sku in wanted:
                    found.setdefault(sku, []).append(r)
        return {sku: found.get(sku, []) for sku in skus}

    def _tax_fields(self, warnings: list[str]) -> dict:
        """Taxable, with the store's tax class, like every existing matrix and item (one tax class: 1 "Item")."""
        tax_classes, _ = self.client.get_all("TaxClass.json")
        if len(tax_classes) == 1:
            return {"tax": "true", "taxClassID": str(tax_classes[0].get("taxClassID"))}
        warnings.append(f"The account has {len(tax_classes)} tax classes, so none is set; Lightspeed's default applies.")
        return {}

    def _active_vendor(self, vendor_id) -> dict:
        vendor = self._get_record(f"Vendor/{int(vendor_id)}.json", "Vendor")
        if not vendor or fmt.flag(vendor.get("archived")):
            raise RuleViolation(f"Refused: vendor {vendor_id} does not exist (or is archived).")
        return vendor

    def _active_category_path(self, category_id) -> str:
        category = self.reader.categories().get(str(int(category_id)))
        if not category or fmt.flag(category.get("archived")):
            raise RuleViolation(f"Refused: category {category_id} does not exist (or is archived).")
        path = fmt.text(category.get("fullPathName")) or ""
        rules.check_not_boys(path)
        return path

    def _standalone_item(self, entry: dict, skus: SkuAllocator, tax: dict, taken_descriptions: dict[str, dict],
                         matrices: list[dict], warnings: list[str]) -> tuple[dict, str]:
        """The POST Item.json body for one new item outside any matrix, and its description."""
        desc = rules.check_plain_description(entry.get("description"), "item description", allow_sizes=True)
        if rules.is_pants(desc):
            raise RuleViolation(f"Refused: '{desc}' is pants. Pants are stocked in their own matrices, sized by waist: "
                                "use create_matrix (garment type pants) and add_matrix_variants.")
        if desc.lower() in taken_descriptions:
            raise RuleViolation(f"Refused: {item_label(taken_descriptions[desc.lower()])} already has the description "
                                f"'{desc}'. Use that item, or describe the new one differently.")
        taken_descriptions[desc.lower()] = {"itemID": "(new)", "description": desc}
        for garment, words in rules.GARMENT_WORDS.items():
            if garment != "pants" and any(re.search(rf"\b{re.escape(w)}\b", desc, re.IGNORECASE) for w in words):
                warnings.append(f"'{desc}' looks like a {garment}; suits, tuxedos, vests and sport coats usually go in "
                                "a matrix (add_matrix_variants).")
                break
        for m in matrices:
            m_desc = fmt.text(m.get("description")) or ""
            if m_desc and not fmt.flag(m.get("archived")) and desc.lower().startswith(m_desc.lower() + " "):
                warnings.append(f"'{desc}' looks like a variant of matrix {m.get('itemMatrixID')} '{m_desc}'.")
                break

        category_path = self._active_category_path(entry.get("category_id"))
        # During intake the vendor or brand may be created earlier in the same plan ("{new:vendor}").
        vendor_ref = entry.get("vendor_ref") or str(self._active_vendor(entry.get("vendor_id")).get("vendorID"))
        body: dict = {"description": desc, "itemType": "default", "categoryID": str(int(entry["category_id"])),
                      "defaultVendorID": vendor_ref, **tax}
        if entry.get("brand_ref"):
            body["manufacturerID"] = entry["brand_ref"]
        elif entry.get("manufacturer_id"):
            if not self._get_record(f"Manufacturer/{int(entry['manufacturer_id'])}.json", "Manufacturer"):
                raise RuleViolation(f"Refused: manufacturer {entry['manufacturer_id']} does not exist.")
            body["manufacturerID"] = str(int(entry["manufacturer_id"]))
        price_text, cost_text = money(entry.get("price"), "price"), money(entry.get("cost"), "cost")
        if float(price_text) <= float(cost_text):
            warnings.append(f"'{desc}': the price {price_text} is not above the cost {cost_text}.")
        body.update(customSku=skus.take(entry.get("custom_sku")), defaultCost=cost_text,
                    Prices=default_price(price_text))
        return body, desc

    @staticmethod
    def _active_descriptions(skus: SkuAllocator) -> dict[str, dict]:
        """Lower-cased description -> active item, to refuse a second item with the same description."""
        return {(fmt.text(i.get("description")) or "").lower(): i for i in skus.items_by_id.values()
                if not fmt.flag(i.get("archived")) and fmt.text(i.get("description"))}

    def _sku_allocator(self, given: list[str | None]) -> SkuAllocator:
        """One pass over every item (archived too): who holds which SKU, and the highest in use."""
        everything, _ = self.client.get_all("Item.json", {"archived": "true"})
        return SkuAllocator(everything, given)

    def _items_by_id(self, item_ids: list[str]) -> dict[str, dict]:
        items: dict[str, dict] = {}
        for batch in batches(sorted(set(item_ids), key=int)):
            rows, _ = self.client.get_all("Item.json", {"itemID": id_filter(batch), "archived": "true",
                                                        "load_relations": relations("ItemShops", "ItemAttributes",
                                                                                    "ItemVendorNums")})
            items.update({str(r.get("itemID")): r for r in rows})
        return items

    def _vendor_name(self, vendor_id) -> str:
        return self.reader.vendor_names([str(vendor_id)]).get(str(vendor_id)) or ""

    def _open_order(self, order_id: int) -> dict:
        order = self._get_record(f"Order/{int(order_id)}.json", "Order")
        if order is None:
            raise WriteRefused(f"Refused: purchase order {order_id} was not found.")
        if fmt.flag(order.get("archived")):
            raise WriteRefused(f"Refused: purchase order {order_id} is archived.")
        if fmt.flag(order.get("complete")):
            raise WriteRefused(f"Refused: purchase order {order_id} is finished (complete). Lines can't be changed.")
        return order

    def _order_lines(self, order_id: int) -> list[dict]:
        rows, _ = self.client.get_all("OrderLine.json", {"orderID": str(int(order_id))})
        return rows

    # ---- matrix checks shared by add_matrix_variants and move_items_into_matrix --------------

    def _target_matrix(self, matrix_id: int) -> tuple[dict, str, bool]:
        """The matrix items will go into: (record, description, is a pants matrix). Must be an active set-5 matrix."""
        matrix = self.reader._matrix_record(int(matrix_id))
        if matrix is None:
            raise RuleViolation(f"Refused: matrix {matrix_id} was not found (or can't be read).")
        if fmt.flag(matrix.get("archived")):
            raise RuleViolation(f"Refused: matrix {matrix_id} is archived.")
        rules.check_attribute_set(matrix.get("itemAttributeSetID"))
        matrix_desc = fmt.text(matrix.get("description")) or ""
        category_path = fmt.text((self.reader.categories().get(str(matrix.get("categoryID"))) or {}).get("fullPathName")) or ""
        rules.check_not_three_piece(matrix_desc)
        rules.check_not_boys(matrix_desc, category_path)
        return matrix, matrix_desc, rules.is_pants(matrix_desc, category_path)

    @staticmethod
    def _variant_body(matrix: dict, matrix_desc: str, values: tuple[str, str, str], sku: str, cost_text: str,
                      price_text: str) -> dict:
        """The POST Item.json body for a new item in a matrix: '<matrix description> <color> <size> <fit>',
        the given SKU, cost and Default price, and the matrix's category, vendor, manufacturer and tax."""
        color, size, fit = values
        attributes = {"itemAttributeSetID": str(rules.STORE_ATTRIBUTE_SET_ID), "attribute1": color, "attribute2": size}
        if fit:
            attributes["attribute3"] = fit
        body = {
            "description": " ".join(p for p in (matrix_desc, color, size, fit) if p),
            "itemMatrixID": str(matrix.get("itemMatrixID")),
            "itemType": fmt.text(matrix.get("itemType")) or "default",
            "customSku": sku,
            "defaultCost": cost_text,
            "ItemAttributes": attributes,
            "Prices": default_price(price_text),
        }
        for key in ("categoryID", "manufacturerID", "defaultVendorID", "taxClassID"):
            if fmt.ref_id(matrix.get(key)):
                body[key] = str(matrix.get(key))
        if matrix.get("tax") not in (None, ""):
            body["tax"] = str(matrix.get("tax"))
        return body

    # ---- vendor item numbers (Lightspeed's "Vendor ID" on an item; one per vendor) -----------------

    def _vendor_number_holder(self, vendor_id, value: str, items: list[dict] | None = None) -> dict | None:
        """The item (archived too) that already has this vendor number for this vendor, if any."""
        if items is None:
            items, _ = self.client.get_all("Item.json", {
                "ItemVendorNums.value": value, "archived": "true", "load_relations": relations("ItemVendorNums")},
                max_records=20)
        for item in items:
            for v in intake.vendor_numbers(item):
                if str(v.get("vendorID")) == str(vendor_id) and (fmt.text(v.get("value")) or "").lower() == value.lower():
                    return item
        return None

    def _check_vendor_number(self, vendor_id, value: str, item: dict | None, items: list[dict] | None = None) -> str:
        """Refuse a number another item already has for this vendor, or a second number for one vendor on an item."""
        value = rules.clean(value)
        if not value:
            raise RuleViolation("Refused: a vendor number can't be blank.")
        holder = self._vendor_number_holder(vendor_id, value, items)
        if holder and (item is None or str(holder.get("itemID")) != str(item.get("itemID"))):
            raise RuleViolation(
                f"Refused: vendor number '{value}' is already on {item_label(holder)} for this vendor. Each vendor "
                "number must belong to one item (Lightspeed's receive-by-Vendor-ID stops working otherwise), so a "
                "style number shared by every size or color can't be saved as a vendor number.")
        for v in intake.vendor_numbers(item or {}):
            if str(v.get("vendorID")) == str(vendor_id) and (fmt.text(v.get("value")) or "").lower() != value.lower():
                raise RuleViolation(f"Refused: {item_label(item)} already has vendor number '{fmt.text(v.get('value'))}' "
                                    "for this vendor, and Lightspeed allows one per vendor.")
        return value

    @staticmethod
    def _vendor_numbers_body(item: dict | None, vendor_id, value: str, cost=None) -> dict:
        """ItemVendorNums for a PUT/POST: the item's existing numbers (kept as they are) plus the new one.

        Lightspeed sets a vendor number's cost to 0 when none is given, so the new one carries a cost
        (the invoice's unit cost, or the item's default cost).
        """
        kept = [{k: v.get(k) for k in ("itemVendorNumID", "vendorID", "value", "cost") if v.get(k) not in (None, "")}
                for v in intake.vendor_numbers(item or {})]
        new = {"vendorID": str(vendor_id), "value": value}
        cost = cost if cost is not None else (item or {}).get("defaultCost")
        if fmt.number(cost):
            new["cost"] = f"{round(float(cost), 2):.2f}"
        return {"ItemVendorNum": kept + [new]}

    @staticmethod
    def _clean_values(entry: dict, what: str) -> tuple[str, str, str]:
        """Color, size and fit, trimmed but otherwise exactly as given (vendor naming)."""
        color, size, fit = (rules.clean(entry.get(k)) for k in ("color", "size", "fit"))
        if not color or not size:
            raise RuleViolation(f"Refused: {what} needs a color and a size (fit may be blank).")
        rules.check_not_three_piece(color, size, fit)
        rules.check_not_boys(color, size, fit)
        return color, size, fit

    def _check_matrix_values(self, matrix_id: int, matrix: dict, pants: bool, combos: list[tuple[str, str, str]],
                             warnings: list[str]) -> list[dict]:
        """Refuse waist-less pants sizes, repeated combinations, combinations the matrix already has
        (archived items too) and values that differ from the matrix's only in capitals. Warn about new
        values. Returns the matrix's items (archived included)."""
        if pants:
            rules.check_waist_sizes([size for _, size, _ in combos])
        seen = set()
        for color, size, fit in combos:
            combo = (color.lower(), size.lower(), fit.lower())
            if combo in seen:
                raise RuleViolation(f"Refused: {color} / {size} / {fit or '(no fit)'} is listed twice.")
            seen.add(combo)

        children, _ = self.client.get_all("Item.json", {
            "itemMatrixID": str(int(matrix_id)), "archived": "true", "load_relations": relations("ItemAttributes")})
        existing = {}
        for c in children:
            attrs = c.get("ItemAttributes") or {}
            existing[tuple((fmt.text(attrs.get(f"attribute{i}")) or "").lower() for i in (1, 2, 3))] = c
        for color, size, fit in combos:
            child = existing.get((color.lower(), size.lower(), fit.lower()))
            if child:
                state = "an archived item" if fmt.flag(child.get("archived")) else "an item"
                raise RuleViolation(f"Refused: matrix {matrix_id} already has {state} for {color} / {size} / "
                                    f"{fit or '(no fit)'}: {item_label(child)}.")

        for index, kind, values in ((1, "color", [c[0] for c in combos]), (2, "size", [c[1] for c in combos]),
                                    (3, "fit", [c[2] for c in combos if c[2]])):
            offered = fmt.matrix_attribute_values(matrix, index)
            by_lower = {o.lower(): o for o in offered}
            new = []
            for value in dict.fromkeys(values):
                if value in offered:
                    continue
                if value.lower() in by_lower:
                    raise RuleViolation(f"Refused: {kind} '{value}' differs only in capitals from the matrix's "
                                        f"existing '{by_lower[value.lower()]}'. Use the existing spelling.")
                new.append(value)
            if new:
                warnings.append(f"New {kind} value(s) for this matrix: {', '.join(new)}. Existing: "
                                f"{', '.join(offered) or 'none'}. Check the vendor's spelling.")
        return children

    # ---- 1. create a matrix --------------------------------------------------------------

    def create_matrix(
        self,
        description: str,
        garment_type: str,
        category_id: int,
        vendor_id: int,
        manufacturer_id: int | None = None,
        default_cost: float | None = None,
        price: float | None = None,
        dry_run: bool = True,
        confirm_code: str | None = None,
    ) -> dict:
        def build() -> Plan:
            desc = rules.check_plain_description(description, "matrix description")
            garment = rules.check_garment_type(garment_type, desc)
            warnings: list[str] = []

            category = self.reader.categories().get(str(int(category_id)))
            if not category or fmt.flag(category.get("archived")):
                raise RuleViolation(f"Refused: category {category_id} does not exist (or is archived).")
            category_path = fmt.text(category.get("fullPathName")) or ""
            rules.check_not_boys(category_path)

            vendor = self._get_record(f"Vendor/{int(vendor_id)}.json", "Vendor")
            if not vendor or fmt.flag(vendor.get("archived")):
                raise RuleViolation(f"Refused: vendor {vendor_id} does not exist (or is archived).")
            vendor_name = fmt.text(vendor.get("name")) or ""
            manufacturer_name = ""
            if manufacturer_id:
                manufacturer = self._get_record(f"Manufacturer/{int(manufacturer_id)}.json", "Manufacturer")
                if not manufacturer:
                    raise RuleViolation(f"Refused: manufacturer {manufacturer_id} does not exist.")
                manufacturer_name = fmt.text(manufacturer.get("name")) or ""

            matrices = self._all_matrices()
            rules.check_not_split_by_color(desc, self._known_colors(matrices), (vendor_name, manufacturer_name))
            for m in matrices:
                if (fmt.text(m.get("description")) or "").lower() == desc.lower():
                    if not fmt.flag(m.get("archived")):
                        raise RuleViolation(f"Refused: matrix {m.get('itemMatrixID')} is already called '{desc}'. "
                                            "Add variants to it instead (add_matrix_variants).")
                    warnings.append(f"Archived matrix {m.get('itemMatrixID')} has the same description.")

            if garment == "pants" and "pant" not in category_path.lower():
                warnings.append(f"This is a pants matrix but category '{category_path}' is not a pants category.")
            elif garment != "pants" and not any(w.split()[0] in category_path.lower() for w in rules.GARMENT_WORDS[garment]):
                warnings.append(f"Category '{category_path}' does not look like a {garment} category.")

            body: dict = {
                "description": desc,
                "itemAttributeSetID": str(rules.STORE_ATTRIBUTE_SET_ID),
                "itemType": "default",
                "categoryID": str(int(category_id)),
                "defaultVendorID": str(int(vendor_id)),
            }
            if manufacturer_id:
                body["manufacturerID"] = str(int(manufacturer_id))
            # Taxable, with the store's tax class, like every existing matrix.
            tax_classes, _ = self.client.get_all("TaxClass.json")
            if len(tax_classes) == 1:
                body["tax"] = "true"
                body["taxClassID"] = str(tax_classes[0].get("taxClassID"))
            else:
                warnings.append(f"The account has {len(tax_classes)} tax classes, so none is set; Lightspeed's default applies.")
            cost_text = money(default_cost, "default cost") if default_cost is not None else None
            price_text = money(price, "price") if price is not None else None
            if cost_text:
                body["defaultCost"] = cost_text
            if price_text:
                body["Prices"] = default_price(price_text)
                if cost_text and float(price_text) <= float(cost_text):
                    warnings.append(f"The price {price_text} is not above the cost {cost_text}.")
            else:
                warnings.append("No price is set on the matrix. New variants always get the price you give "
                                "add_matrix_variants, never the matrix's.")

            summary = (
                f"Create a new, empty item matrix '{desc}' ({garment}) using attribute set 5 Color/Size/Fit, in "
                f"category '{category_path}', vendor '{vendor_name}'"
                + (f", manufacturer '{manufacturer_name}'" if manufacturer_name else "")
                + (f", default cost {cost_text}" if cost_text else "")
                + (f", Default price {price_text}" if price_text else ", no price")
                + ". No items are created: add them with add_matrix_variants. Each variant's color, size and fit are "
                  "added to the matrix automatically when the variant is created."
            )
            return Plan("create_matrix", summary,
                        [PlannedRequest(f"create matrix '{desc}'", "POST", "ItemMatrix.json", body)], warnings)

        return self._run("create_matrix", dry_run, confirm_code, build)

    # ---- 2. add variants to a matrix -------------------------------------------------------

    def add_matrix_variants(
        self,
        matrix_id: int,
        variants: list[dict],
        price: float,
        cost: float,
        dry_run: bool = True,
        confirm_code: str | None = None,
    ) -> dict:
        def build() -> Plan:
            if not variants:
                raise RuleViolation("Refused: give at least one variant.")
            if len(variants) > MAX_REQUESTS_PER_CALL:
                raise RuleViolation(f"Refused: {len(variants)} variants; the limit is {MAX_REQUESTS_PER_CALL} per call.")
            matrix, matrix_desc, pants = self._target_matrix(matrix_id)
            price_text, cost_text = money(price, "price"), money(cost, "cost")
            warnings: list[str] = []

            cleaned = []
            for n, v in enumerate(variants, 1):
                color, size, fit = self._clean_values(v, f"variant {n}")
                cleaned.append((color, size, fit, rules.check_custom_sku_format(v.get("custom_sku"))))
            rules.check_skus_unique_in_request([sku for *_, sku in cleaned])
            children = self._check_matrix_values(matrix_id, matrix, pants, [c[:3] for c in cleaned], warnings)

            taken = {sku: items for sku, items in self._items_with_skus([s for *_, s in cleaned]).items() if items}
            if taken:
                raise RuleViolation("Refused: custom SKU already in use: " + "; ".join(
                    f"{sku} by {item_label(items[0])}" for sku, items in taken.items()) + ". Every item gets its own SKU.")

            if float(price_text) <= float(cost_text):
                warnings.append(f"The price {price_text} is not above the cost {cost_text}.")
            sibling_prices = [p for p in (fmt.prices(c).get("Default") for c in children if not fmt.flag(c.get("archived")))
                              if p is not None]
            common = max(set(sibling_prices), key=sibling_prices.count) if sibling_prices else None
            if common is not None and abs(common - float(price_text)) > 0.001:
                warnings.append(f"Most items in this matrix sell at {common:.2f}; these would sell at {price_text}.")

            requests = []
            for color, size, fit, sku in cleaned:
                body = self._variant_body(matrix, matrix_desc, (color, size, fit), sku, cost_text, price_text)
                requests.append(PlannedRequest(f"create '{body['description']}' (SKU {sku})", "POST", "Item.json", body))

            summary = (f"Create {len(requests)} new item(s) in matrix {matrix_id} '{matrix_desc}'"
                       + (" (pants: sized by waist)" if pants else "")
                       + f", each with its own custom SKU, cost {cost_text} and Default price {price_text}. "
                         "Category, vendor, manufacturer and tax come from the matrix. Stock starts at 0. "
                         "The matrix itself, its price and its other items are not changed.")
            return Plan("add_matrix_variants", summary, requests, warnings)

        return self._run("add_matrix_variants", dry_run, confirm_code, build)

    # ---- 3. update an item's SKU, cost or description ---------------------------------------

    def update_item_fields(
        self,
        item_id: int,
        custom_sku: str | None = None,
        cost: float | None = None,
        description: str | None = None,
        dry_run: bool = True,
        confirm_code: str | None = None,
    ) -> dict:
        def build() -> Plan:
            if custom_sku is None and cost is None and description is None:
                raise RuleViolation("Refused: give at least one of custom_sku, cost or description.")
            item = self._get_record(f"Item/{int(item_id)}.json", "Item", {"load_relations": relations("ItemAttributes")})
            if item is None:
                raise RuleViolation(f"Refused: item {item_id} was not found.")
            body: dict = {}
            changes: list[str] = []
            warnings: list[str] = []
            if fmt.flag(item.get("archived")):
                warnings.append("This item is archived.")

            if custom_sku is not None:
                sku = rules.check_custom_sku_format(custom_sku)
                current = fmt.text(item.get("customSku"))
                if sku != current:
                    others = [i for i in self._items_with_skus([sku])[sku] if str(i.get("itemID")) != str(item.get("itemID"))]
                    if others:
                        raise RuleViolation(f"Refused: custom SKU {sku} is already used by {item_label(others[0])}. "
                                            "Every item gets its own SKU.")
                    body["customSku"] = sku
                    changes.append(f"custom SKU {current or '(none)'} -> {sku}")
                    if current:
                        warnings.append(f"This replaces the existing SKU {current}; labels printed with it will no longer scan.")

            if cost is not None:
                cost_text = money(cost, "cost")
                current = fmt.number(item.get("defaultCost"))
                if current is None or abs(current - float(cost_text)) > 0.001:
                    body["defaultCost"] = cost_text
                    changes.append(f"default cost {current if current is not None else '(none)'} -> {cost_text}")

            if description is not None:
                desc = rules.check_plain_description(description, "item description", allow_sizes=True)
                current = fmt.text(item.get("description"))
                if desc != current:
                    body["description"] = desc
                    changes.append(f"description '{current}' -> '{desc}'")
                    if fmt.ref_id(item.get("itemMatrixID")):
                        warnings.append("This item is in a matrix, where descriptions are normally '<matrix description> "
                                        "<color> <size> <fit>'. Lightspeed may rebuild it if the matrix is edited.")

            if not body:
                raise RuleViolation(f"Refused: nothing to change; {item_label(item)} already has these values.")
            summary = (f"Change {item_label(item)}: " + "; ".join(changes) + ". Nothing else on the item changes: "
                       "prices, stock, vendor, category and matrix stay as they are.")
            if "defaultCost" in body:
                summary += (" The default cost is what new purchase-order lines start from; average cost and existing "
                            "PO lines are not changed.")
            return Plan("update_item_fields", summary,
                        [PlannedRequest(f"update item {item.get('itemID')}", "PUT", f"Item/{int(item_id)}.json", body)],
                        warnings)

        return self._run("update_item_fields", dry_run, confirm_code, build)

    # ---- 4a. add lines to a purchase order ------------------------------------------------

    def add_po_lines(self, order_id: int, lines: list[dict], dry_run: bool = True,
                     confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            if not lines:
                raise RuleViolation("Refused: give at least one line.")
            if len(lines) > MAX_REQUESTS_PER_CALL:
                raise RuleViolation(f"Refused: {len(lines)} lines; the limit is {MAX_REQUESTS_PER_CALL} per call.")
            order = self._open_order(order_id)
            existing = {str(line.get("itemID")): line for line in self._order_lines(order_id)}
            wanted = [str(int(line.get("item_id"))) for line in lines]
            if len(set(wanted)) != len(wanted):
                raise RuleViolation("Refused: the same item is listed twice. Use one line per item.")
            items = self._items_by_id(wanted)
            vendor_id = str(order.get("vendorID"))
            warnings: list[str] = []
            if fmt.date(order.get("receivedDate")):
                warnings.append("This PO is already being checked in.")

            requests, units, total = [], 0, 0.0
            for line, item_id in zip(lines, wanted):
                item = items.get(item_id)
                if item is None:
                    raise RuleViolation(f"Refused: item {item_id} was not found.")
                if fmt.flag(item.get("archived")):
                    raise RuleViolation(f"Refused: {item_label(item)} is archived.")
                rules.check_not_three_piece(fmt.text(item.get("description")) or "")
                rules.check_not_boys(fmt.text(item.get("description")) or "")
                if item_id in existing:
                    raise RuleViolation(f"Refused: {item_label(item)} is already on PO {order_id} (line "
                                        f"{existing[item_id].get('orderLineID')}). Use one line per item.")
                qty = quantity(line.get("quantity"))
                if line.get("unit_cost") is not None:
                    unit_cost = money(line.get("unit_cost"), "unit cost")
                elif (fmt.number(item.get("defaultCost")) or 0) > 0:
                    unit_cost = money(item.get("defaultCost"), "the item's default cost")
                else:
                    raise RuleViolation(f"Refused: {item_label(item)} has no default cost; give unit_cost for it.")
                if str(item.get("defaultVendorID")) != vendor_id:
                    warnings.append(f"{item_label(item)} has a different default vendor from this PO.")
                if not fmt.ref_id(item.get("itemMatrixID")):
                    warnings.append(f"{item_label(item)} is not in a matrix (a catch-all item?).")
                if not fmt.text(item.get("customSku")):
                    warnings.append(f"{item_label(item)} has no custom SKU.")
                qoh = fmt.total_qoh(item)
                if qoh is not None and qoh < 0:
                    warnings.append(f"{item_label(item)} has negative stock ({qoh}).")
                units += qty
                total += qty * float(unit_cost)
                body = {"orderID": str(int(order_id)), "itemID": item_id, "quantity": str(qty), "price": unit_cost,
                        "originalPrice": unit_cost, "numReceived": "0"}
                requests.append(PlannedRequest(f"add {qty} x {item_label(item)} at {unit_cost}", "POST", "OrderLine.json", body))

            vendor = self._vendor_name(vendor_id)
            summary = (f"Add {len(requests)} line(s), {units} unit(s), {total:.2f} at cost, to purchase order {order_id} "
                       f"(vendor '{vendor}', ref {fmt.text(order.get('refNum')) or '-'}). Nothing is received and stock "
                       "does not change. Item costs and prices are not changed.")
            return Plan("add_po_lines", summary, requests, warnings)

        return self._run("add_po_lines", dry_run, confirm_code, build)

    # ---- 4b. receive quantities on purchase-order lines ---------------------------------------

    def receive_po_lines(self, order_id: int, lines: list[dict], dry_run: bool = True,
                         confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            if not lines:
                raise RuleViolation("Refused: give at least one line.")
            order = self._open_order(order_id)
            order_lines = self._order_lines(order_id)
            by_id = {str(line.get("orderLineID")): line for line in order_lines}
            by_item: dict[str, list[dict]] = {}
            for line in order_lines:
                by_item.setdefault(str(line.get("itemID")), []).append(line)

            resolved = []
            for n, wanted in enumerate(lines, 1):
                if wanted.get("order_line_id") is not None:
                    line = by_id.get(str(int(wanted["order_line_id"])))
                    if line is None:
                        raise RuleViolation(f"Refused: line {wanted['order_line_id']} is not on purchase order {order_id}.")
                elif wanted.get("item_id") is not None:
                    matches = by_item.get(str(int(wanted["item_id"])), [])
                    if len(matches) != 1:
                        raise RuleViolation(f"Refused: item {wanted['item_id']} is on {len(matches)} lines of PO "
                                            f"{order_id}; give order_line_id instead.")
                    line = matches[0]
                else:
                    raise RuleViolation(f"Refused: entry {n} needs order_line_id or item_id.")
                resolved.append((line, quantity(wanted.get("quantity"), "quantity received")))
            line_ids = [str(line.get("orderLineID")) for line, _ in resolved]
            if len(set(line_ids)) != len(line_ids):
                raise RuleViolation("Refused: the same PO line is listed twice.")

            items = self._items_by_id([str(line.get("itemID")) for line, _ in resolved])
            warnings: list[str] = []
            requests = []
            for line, qty in resolved:
                ordered = fmt.integer(line.get("quantity")) or 0
                checked_in = fmt.integer(line.get("checkedIn")) or 0
                received = fmt.integer(line.get("numReceived")) or 0
                new_received = received + qty
                item = items.get(str(line.get("itemID"))) or {"itemID": line.get("itemID")}
                if checked_in + new_received > ordered:
                    raise RuleViolation(
                        f"Refused: line {line.get('orderLineID')} ({item_label(item)}) has {ordered} ordered, "
                        f"{checked_in} checked in and {received} received; {qty} more would be "
                        f"{checked_in + new_received}, more than ordered.")
                if not fmt.ref_id(item.get("itemMatrixID")):
                    warnings.append(f"{item_label(item)} is not in a matrix (a catch-all item?).")
                qoh = fmt.total_qoh(item)
                if qoh is not None and qoh < 0:
                    warnings.append(f"{item_label(item)} has negative stock ({qoh}).")
                requests.append(PlannedRequest(
                    f"line {line.get('orderLineID')}: received {received} -> {new_received} of {ordered} "
                    f"({item_label(item)})",
                    "PUT", f"OrderLine/{line.get('orderLineID')}.json", {"numReceived": str(new_received)}))

            summary = (f"Mark {sum(q for _, q in resolved)} unit(s) as received on {len(requests)} line(s) of purchase "
                       f"order {order_id}. numReceived is a running total, so each line gets its new total. Only "
                       "numReceived changes: quantities ordered, costs and prices stay as they are.")
            after = (f"Stock on hand does NOT change yet. Lightspeed counts these as 'received, not checked in'. To add "
                     f"them to inventory, open purchase order {order_id} in Lightspeed and finish the check-in there "
                     "('Add Received To Inventory'). Lightspeed's API has no call for that step.")
            return Plan("receive_po_lines", summary, requests, warnings, after)

        return self._run("receive_po_lines", dry_run, confirm_code, build)

    # ---- 5. move existing items into a matrix --------------------------------------------------

    def _moves_confirmed(self) -> bool:
        """Has a move ever come back done? Until one has, moves go one item per call (see below)."""
        return any(e.get("tool") == "move_items_into_matrix" and e.get("event") == "done"
                   for e in self.writes.write_log.entries())

    def move_items_into_matrix(
        self,
        matrix_id: int,
        items: list[dict],
        only_matrix_and_attributes: bool = False,
        dry_run: bool = True,
        confirm_code: str | None = None,
    ) -> dict:
        def build() -> Plan:
            if not items:
                raise RuleViolation("Refused: give at least one item.")
            # The docs list itemMatrixID and ItemAttributes as item fields but never describe moving an
            # existing item into a matrix. So the first move is the real test: one item, checked afterwards.
            limit = MAX_REQUESTS_PER_CALL if self._moves_confirmed() else 1
            if len(items) > limit:
                if limit == 1:
                    raise RuleViolation(
                        "Refused: no item has been moved into a matrix through the API yet, and Lightspeed's docs "
                        "don't describe it. Move one item first, check it (get_item, compare_snapshots), then move "
                        "the rest.")
                raise RuleViolation(f"Refused: {len(items)} items; the limit is {limit} per call.")
            matrix, matrix_desc, pants = self._target_matrix(matrix_id)
            ids = [str(int(entry.get("item_id"))) for entry in items]
            if len(set(ids)) != len(ids):
                raise RuleViolation("Refused: the same item is listed twice.")
            found = self._items_by_id(ids)
            warnings: list[str] = []

            moves = []
            for entry, item_id in zip(items, ids):
                item = found.get(item_id)
                if item is None:
                    raise RuleViolation(f"Refused: item {item_id} was not found.")
                if fmt.flag(item.get("archived")):
                    raise RuleViolation(f"Refused: {item_label(item)} is archived.")
                current_matrix = fmt.ref_id(item.get("itemMatrixID"))
                if current_matrix == int(matrix_id):
                    raise RuleViolation(f"Refused: {item_label(item)} is already in matrix {matrix_id}.")
                rules.check_not_three_piece(fmt.text(item.get("description")) or "")
                rules.check_not_boys(fmt.text(item.get("description")) or "")
                moves.append((item, *self._clean_values(entry, item_label(item)), current_matrix))
            self._check_matrix_values(matrix_id, matrix, pants, [(c, s, f) for _, c, s, f, _ in moves], warnings)

            matrix_price = fmt.prices(matrix).get("Default")
            requests = []
            for item, color, size, fit, current_matrix in moves:
                attributes = {"itemAttributeSetID": str(rules.STORE_ATTRIBUTE_SET_ID), "attribute1": color,
                              "attribute2": size}
                if fit:
                    attributes["attribute3"] = fit
                body: dict = {"itemMatrixID": str(int(matrix_id)), "ItemAttributes": attributes}
                changes = [f"matrix {current_matrix or 'none'} -> {matrix_id}", f"{color} / {size} / {fit or '-'}"]
                if not only_matrix_and_attributes:
                    # Like the matrix's other items: its description pattern, category, manufacturer and vendor.
                    old_desc = fmt.text(item.get("description")) or ""
                    new_desc = " ".join(p for p in (matrix_desc, color, size, fit) if p)
                    if new_desc != old_desc:
                        body["description"] = new_desc
                        changes.append(f"description '{old_desc}' -> '{new_desc}'")
                    for key, label in (("categoryID", "category"), ("manufacturerID", "manufacturer"),
                                       ("defaultVendorID", "vendor")):
                        target = fmt.ref_id(matrix.get(key))
                        if target and fmt.ref_id(item.get(key)) != target:
                            body[key] = str(target)
                            changes.append(f"{label} {fmt.ref_id(item.get(key)) or 'none'} -> {target}")
                if current_matrix:
                    warnings.append(f"{item_label(item)} moves out of matrix {current_matrix}; that matrix keeps its "
                                    "other items.")
                if not fmt.text(item.get("customSku")):
                    warnings.append(f"{item_label(item)} has no custom SKU (assign_custom_skus).")
                item_price = fmt.prices(item).get("Default")
                if matrix_price is not None and item_price is not None and abs(item_price - matrix_price) > 0.001:
                    warnings.append(
                        f"{item_label(item)} sells at {item_price:.2f}, but the matrix's own Default price is "
                        f"{matrix_price:.2f}. No price is sent and the docs don't say joining a matrix changes it, "
                        "but that is unconfirmed: check the item's price afterwards.")
                requests.append(PlannedRequest(f"{item_label(item)}: " + "; ".join(changes), "PUT",
                                               f"Item/{item.get('itemID')}.json", body))

            summary = (f"Move {len(requests)} existing item(s) into matrix {matrix_id} '{matrix_desc}' (attribute set "
                       "5 Color/Size/Fit)"
                       + (", changing only the matrix and Color/Size/Fit" if only_matrix_and_attributes else
                          ", with the matrix's description pattern, category, manufacturer and vendor")
                       + ". SKUs, prices, cost and stock are not sent and stay as they are.")
            after = ("Lightspeed's docs list itemMatrixID and ItemAttributes as item fields but do not describe moving "
                     "an existing item into a matrix. After this, check the item (get_item) and compare snapshots "
                     "before moving more.")
            return Plan("move_items_into_matrix", summary, requests, warnings, after)

        return self._run("move_items_into_matrix", dry_run, confirm_code, build)

    # ---- 6. give items without a SKU their own one ---------------------------------------------

    def assign_custom_skus(self, items: list[dict], dry_run: bool = True, confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            if not items:
                raise RuleViolation("Refused: give at least one item.")
            if len(items) > MAX_REQUESTS_PER_CALL:
                raise RuleViolation(f"Refused: {len(items)} items; the limit is {MAX_REQUESTS_PER_CALL} per call.")
            ids = [str(int(entry.get("item_id"))) for entry in items]
            if len(set(ids)) != len(ids):
                raise RuleViolation("Refused: the same item is listed twice.")

            skus = self._sku_allocator([entry.get("custom_sku") for entry in items])
            requests = []
            for entry, item_id in zip(items, ids):
                item = skus.items_by_id.get(item_id)
                if item is None:
                    raise RuleViolation(f"Refused: item {item_id} was not found.")
                if fmt.flag(item.get("archived")):
                    raise RuleViolation(f"Refused: {item_label(item)} is archived.")
                if fmt.text(item.get("customSku")):
                    raise RuleViolation(f"Refused: {item_label(item)} already has a SKU. This tool only fills in "
                                        "missing ones; update_item_fields changes an existing SKU.")
                sku = skus.take(entry.get("custom_sku"))
                requests.append(PlannedRequest(f"{item_label(item)} -> SKU {sku}", "PUT", f"Item/{item_id}.json",
                                               {"customSku": sku}))

            summary = (f"Give {len(requests)} item(s) their own custom SKU{skus.picked_note()}. Only customSku is "
                       "sent: nothing else on the items changes. Each SKU is checked against every item, archived "
                       "ones included.")
            return Plan("assign_custom_skus", summary, requests)

        return self._run("assign_custom_skus", dry_run, confirm_code, build)

    # ---- 7. archive one item ---------------------------------------------------------------------

    def archive_item(self, item_id: int, dry_run: bool = True, confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            item = self._get_record(f"Item/{int(item_id)}.json", "Item",
                                    {"load_relations": relations("ItemShops", "ItemAttributes")})
            if item is None:
                raise RuleViolation(f"Refused: item {item_id} was not found.")
            if fmt.flag(item.get("archived")):
                raise RuleViolation(f"Refused: {item_label(item)} is already archived.")

            # Lightspeed: "If you archive an item with inventory, the inventory will be zeroed out.
            # This can not be undone." So only items with no stock (and no negative stock) anywhere.
            stock = {str(s.get("shopID")): fmt.integer(s.get("qoh")) or 0
                     for s in fmt.as_list((item.get("ItemShops") or {}).get("ItemShop"))}
            if any(stock.values()):
                raise RuleViolation(
                    f"Refused: {item_label(item)} has stock {fmt.total_qoh(item)}. Archiving zeroes an item's stock in "
                    "Lightspeed, and that can't be undone. Only items with stock exactly 0 are archived here: settle "
                    "the stock first (e.g. with an inventory count), or archive it by hand in Lightspeed if that is "
                    "really intended.")

            lines, _ = self.client.get_all("OrderLine.json", {"itemID": str(int(item_id))})
            order_ids = sorted({str(line.get("orderID")) for line in lines if fmt.ref_id(line.get("orderID"))}, key=int)
            open_orders = []
            for batch in batches(order_ids):
                orders, _ = self.client.get_all("Order.json", {"orderID": id_filter(batch), "archived": "true"})
                open_orders += [o.get("orderID") for o in orders
                                if not fmt.flag(o.get("complete")) and not fmt.flag(o.get("archived"))]
            if open_orders:
                raise RuleViolation(f"Refused: {item_label(item)} is on open purchase order(s) "
                                    f"{', '.join(map(str, open_orders))}. Finish those or take the item off them first.")

            warnings = ["Lightspeed's docs show an archived item's type becoming non_inventory and its tax false. "
                        "Unarchiving (archived=false) is possible but may not restore those, so the item's current "
                        "values are saved in the write log."]
            matrix_id = fmt.ref_id(item.get("itemMatrixID"))
            if matrix_id:
                warnings.append(f"The item is in matrix {matrix_id}; the matrix and its other items are not touched.")
            before = {k: item.get(k) for k in (
                "itemID", "description", "customSku", "itemType", "tax", "taxClassID", "categoryID", "manufacturerID",
                "defaultVendorID", "itemMatrixID", "defaultCost", "archived", "ItemAttributes", "Prices")
                if item.get(k) is not None}
            summary = (f"Archive {item_label(item)}. Its stock is 0, so no stock is lost, and it is on no open PO. "
                       "Lightspeed hides archived items from sales and searches. Only this one item is archived, "
                       "never a matrix.")
            return Plan("archive_item", summary,
                        [PlannedRequest(f"archive {item_label(item)}", "DELETE", f"Item/{int(item_id)}.json", None,
                                        before=before)],
                        warnings,
                        basis={"itemID": item.get("itemID"), "timeStamp": item.get("timeStamp"), "stock": stock})

        return self._run("archive_item", dry_run, confirm_code, build)

    # ---- 8. create items outside any matrix -------------------------------------------------------

    def create_items(self, items: list[dict], dry_run: bool = True, confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            if not items:
                raise RuleViolation("Refused: give at least one item.")
            if len(items) > MAX_REQUESTS_PER_CALL:
                raise RuleViolation(f"Refused: {len(items)} items; the limit is {MAX_REQUESTS_PER_CALL} per call.")
            warnings: list[str] = []
            skus = self._sku_allocator([entry.get("custom_sku") for entry in items])
            taken = self._active_descriptions(skus)
            tax, matrices = self._tax_fields(warnings), self._all_matrices()
            requests = []
            for entry in items:
                body, desc = self._standalone_item(entry, skus, tax, taken, matrices, warnings)
                requests.append(PlannedRequest(f"create '{desc}' (SKU {body['customSku']}, cost {body['defaultCost']}, "
                                               f"price {body['Prices']['ItemPrice'][0]['amount']})", "POST",
                                               "Item.json", body))
            summary = (f"Create {len(requests)} new item(s) outside any matrix, each with its own custom SKU"
                       f"{skus.picked_note()}, the cost and Default price given, and the store's tax class. Stock "
                       "starts at 0.")
            return Plan("create_items", summary, requests, warnings)

        return self._run("create_items", dry_run, confirm_code, build)

    # ---- 9. purchase orders: create, remove unreceived lines ------------------------------------

    def _single_shop(self) -> str:
        """The store's one shop ID (a PO needs it)."""
        shops = [s for s in sorted(self.reader.shop_names(), key=lambda s: int(s) if s.isdigit() else 0) if s != "0"]
        if len(shops) != 1:
            raise RuleViolation(f"Refused: the account has {len(shops)} shops readable; this tool expects exactly one.")
        return shops[0]

    def _new_order(self, vendor_id, ref_num, ship_cost=None, other_cost=None) -> tuple[dict, str, list[str]]:
        """The POST Order.json body for a new, empty PO; the vendor's name; summary parts."""
        vendor = self._active_vendor(vendor_id)
        ref = rules.clean(ref_num)
        if not ref:
            raise RuleViolation("Refused: give the invoice or order number (ref_num), so the PO can be found later and "
                                "the same invoice can't be entered twice.")
        # The same vendor + reference already on a PO (any status, archived too) means it was entered before.
        same, _ = self.client.get_all("Order.json", {"vendorID": str(vendor.get("vendorID")), "refNum": ref,
                                                     "archived": "true"})
        same = [o for o in same if (fmt.text(o.get("refNum")) or "").lower() == ref.lower()]
        if same:
            raise RuleViolation(f"Refused: {fmt.text(vendor.get('name'))} reference {ref} is already purchase order "
                                f"{same[0].get('orderID')}. Use that PO instead of making a second one.")
        body = {"vendorID": str(vendor.get("vendorID")), "shopID": self._single_shop(), "refNum": ref,
                "shipCost": money_or_zero(ship_cost, "shipping cost"), "otherCost": money_or_zero(other_cost, "other cost")}
        parts = [f"shipping {body['shipCost']}", f"other costs {body['otherCost']}"]
        return body, fmt.text(vendor.get("name")) or "", parts

    def create_purchase_order(self, vendor_id: int, ref_num: str, ship_cost: float | None = None,
                              other_cost: float | None = None, dry_run: bool = True,
                              confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            body, vendor_name, parts = self._new_order(vendor_id, ref_num, ship_cost, other_cost)
            summary = (f"Create a new, empty purchase order for {vendor_name}, reference {body['refNum']}, "
                       f"{', '.join(parts)}. Add its lines with add_po_lines. Lightspeed only lets a PO be marked "
                       "ordered or received once it has lines.")
            return Plan("create_purchase_order", summary,
                        [PlannedRequest(f"create PO for {vendor_name}, ref {body['refNum']}", "POST", "Order.json", body)])

        return self._run("create_purchase_order", dry_run, confirm_code, build)

    def remove_po_lines(self, order_id: int, order_line_ids: list[int], dry_run: bool = True,
                        confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            if not order_line_ids:
                raise RuleViolation("Refused: give at least one order_line_id.")
            wanted = [str(int(i)) for i in order_line_ids]
            if len(set(wanted)) != len(wanted):
                raise RuleViolation("Refused: the same PO line is listed twice.")
            self._open_order(order_id)
            lines = {str(line.get("orderLineID")): line for line in self._order_lines(order_id)}
            items = self._items_by_id([str(lines[w].get("itemID")) for w in wanted if w in lines])
            requests = []
            for line_id in wanted:
                line = lines.get(line_id)
                if line is None:
                    raise RuleViolation(f"Refused: line {line_id} is not on purchase order {order_id}.")
                item = items.get(str(line.get("itemID"))) or {"itemID": line.get("itemID")}
                received, checked_in = fmt.integer(line.get("numReceived")) or 0, fmt.integer(line.get("checkedIn")) or 0
                if received or checked_in:
                    raise RuleViolation(f"Refused: line {line_id} ({item_label(item)}) has {received} received and "
                                        f"{checked_in} checked in. Only lines with nothing received can be removed.")
                before = {k: line.get(k) for k in ("orderLineID", "orderID", "itemID", "quantity", "price",
                                                   "originalPrice", "numReceived", "checkedIn")}
                requests.append(PlannedRequest(f"remove line {line_id}: {fmt.integer(line.get('quantity'))} x "
                                               f"{item_label(item)} at {line.get('price')}", "DELETE",
                                               f"OrderLine/{line_id}.json", None, before=before))
            warnings = ["Lightspeed deletes PO lines for good; each line's values are saved in the write log."]
            if len(wanted) == len(lines):
                warnings.append(f"This removes every line; purchase order {order_id} would be left empty.")
            summary = (f"Remove {len(requests)} line(s) with nothing received from purchase order {order_id}. The "
                       "items themselves, their stock and other lines are not touched.")
            return Plan("remove_po_lines", summary, requests, warnings,
                        basis={line_id: lines[line_id].get("timeStamp") for line_id in wanted})

        return self._run("remove_po_lines", dry_run, confirm_code, build)

    def _ref_taken(self, vendor_id, ref: str, except_order=None) -> dict | None:
        """Another PO (any status, archived too) for this vendor with this reference number."""
        rows, _ = self.client.get_all("Order.json", {"vendorID": str(vendor_id), "refNum": ref, "archived": "true"})
        return next((o for o in rows if (fmt.text(o.get("refNum")) or "").lower() == ref.lower()
                     and str(o.get("orderID")) != str(except_order)), None)

    def update_purchase_order(self, order_id: int, ref_num: str | None = None, ship_cost: float | None = None,
                              other_cost: float | None = None, ordered_date: str | None = None,
                              received_date: str | None = None, dry_run: bool = True,
                              confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            order = self._open_order(order_id)
            body: dict = {}
            changes: list[str] = []
            warnings: list[str] = []
            if ref_num is not None:
                ref = rules.clean(ref_num)
                if not ref:
                    raise RuleViolation("Refused: the reference number can't be blank.")
                if ref != fmt.text(order.get("refNum")):
                    other = self._ref_taken(order.get("vendorID"), ref, except_order=order_id)
                    if other:
                        raise RuleViolation(f"Refused: reference {ref} is already purchase order {other.get('orderID')} "
                                            "for this vendor.")
                    body["refNum"] = ref
                    changes.append(f"reference {fmt.text(order.get('refNum')) or '-'} -> {ref}")
            for value, key, label, convert in ((ship_cost, "shipCost", "shipping", money_or_zero),
                                               (other_cost, "otherCost", "other costs", signed_money)):
                if value is not None:
                    text = convert(value, label)
                    if abs(float(text) - (fmt.number(order.get(key)) or 0)) > 0.001:
                        body[key] = text
                        changes.append(f"{label} {fmt.number(order.get(key)) or 0:.2f} -> {text}")
            for value, key, label in ((ordered_date, "orderedDate", "ordered date (status Ordered)"),
                                      (received_date, "receivedDate", "received date (status Check In)")):
                if value is not None:
                    day = iso_date(value, label)
                    if (fmt.date(order.get(key)) or "")[:10] != day:
                        body[key] = day
                        changes.append(f"{label} {(fmt.date(order.get(key)) or 'none')[:10]} -> {day}")
            if not body:
                raise RuleViolation(f"Refused: nothing to change on purchase order {order_id}.")
            if ("orderedDate" in body or "receivedDate" in body) and not self._order_lines(order_id):
                raise RuleViolation(f"Refused: purchase order {order_id} has no lines yet, and Lightspeed only lets a "
                                    "PO be marked ordered or received once it has lines.")
            if "receivedDate" in body:
                warnings.append("A received date puts the PO in Check In. Stock still changes only when someone "
                                "clicks Add Received To Inventory on it in Lightspeed.")
            summary = (f"Change purchase order {order_id}: " + "; ".join(changes) + ". Its lines are not touched, and it "
                       "is not marked finished (that stays a click in Lightspeed).")
            return Plan("update_purchase_order", summary,
                        [PlannedRequest(f"update PO {order_id}", "PUT", f"Order/{int(order_id)}.json", body)], warnings)

        return self._run("update_purchase_order", dry_run, confirm_code, build)

    def update_po_lines(self, order_id: int, lines: list[dict], dry_run: bool = True,
                        confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            if not lines:
                raise RuleViolation("Refused: give at least one line.")
            self._open_order(order_id)
            on_po = {str(line.get("orderLineID")): line for line in self._order_lines(order_id)}
            wanted = [str(int(entry.get("order_line_id"))) for entry in lines]
            if len(set(wanted)) != len(wanted):
                raise RuleViolation("Refused: the same PO line is listed twice.")
            items = self._items_by_id([str(on_po[w].get("itemID")) for w in wanted if w in on_po])
            requests = []
            for entry, line_id in zip(lines, wanted):
                line = on_po.get(line_id)
                if line is None:
                    raise RuleViolation(f"Refused: line {line_id} is not on purchase order {order_id}.")
                item = items.get(str(line.get("itemID"))) or {"itemID": line.get("itemID")}
                ordered = fmt.integer(line.get("quantity")) or 0
                received = fmt.integer(line.get("numReceived")) or 0
                checked_in = fmt.integer(line.get("checkedIn")) or 0
                body, changes = {}, []
                if entry.get("quantity") is not None:
                    qty = quantity(entry["quantity"])
                    if qty < received + checked_in:
                        raise RuleViolation(f"Refused: line {line_id} ({item_label(item)}) already has {received} "
                                            f"received and {checked_in} checked in; it can't be lowered to {qty}.")
                    if qty != ordered:
                        body["quantity"] = str(qty)
                        changes.append(f"quantity {ordered} -> {qty}")
                if entry.get("unit_cost") is not None:
                    cost = money(entry["unit_cost"], "unit cost")
                    if abs(float(cost) - (fmt.number(line.get("originalPrice") or line.get("price")) or 0)) > 0.001:
                        if checked_in:
                            raise RuleViolation(f"Refused: line {line_id} ({item_label(item)}) has {checked_in} already "
                                                "added to inventory at its current cost; its cost can't change now.")
                        body.update(price=cost, originalPrice=cost)
                        changes.append(f"unit cost {line.get('originalPrice')} -> {cost}")
                if body:
                    requests.append(PlannedRequest(f"line {line_id} ({item_label(item)}): " + "; ".join(changes), "PUT",
                                                   f"OrderLine/{line_id}.json", body))
            if not requests:
                raise RuleViolation("Refused: nothing to change on these lines.")
            summary = (f"Change {len(requests)} line(s) of purchase order {order_id}: quantities ordered and/or unit "
                       "costs only. Received quantities, items and stock are not touched. Shipping is spread onto the "
                       "lines by the PO's own setting.")
            return Plan("update_po_lines", summary, requests)

        return self._run("update_po_lines", dry_run, confirm_code, build)

    def set_vendor_numbers(self, vendor_id: int, items: list[dict], dry_run: bool = True,
                           confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            if not items:
                raise RuleViolation("Refused: give at least one item.")
            if len(items) > MAX_REQUESTS_PER_CALL:
                raise RuleViolation(f"Refused: {len(items)} items; the limit is {MAX_REQUESTS_PER_CALL} per call.")
            vendor = self._active_vendor(vendor_id)
            ids = [str(int(entry.get("item_id"))) for entry in items]
            numbers = [rules.clean(entry.get("vendor_number")) for entry in items]
            if len(set(ids)) != len(ids):
                raise RuleViolation("Refused: the same item is listed twice.")
            if len({n.lower() for n in numbers}) != len(numbers):
                raise RuleViolation("Refused: the same vendor number is listed for two items. Each vendor number must "
                                    "belong to one item.")
            found = self._items_by_id(ids)
            requests = []
            for item_id, number in zip(ids, numbers):
                item = found.get(item_id)
                if item is None:
                    raise RuleViolation(f"Refused: item {item_id} was not found.")
                if fmt.flag(item.get("archived")):
                    raise RuleViolation(f"Refused: {item_label(item)} is archived.")
                number = self._check_vendor_number(vendor_id, number, item)
                if any(str(v.get("vendorID")) == str(vendor_id) for v in intake.vendor_numbers(item)):
                    raise RuleViolation(f"Refused: {item_label(item)} already has vendor number {number}.")
                requests.append(PlannedRequest(f"{item_label(item)}: vendor number {number}", "PUT", f"Item/{item_id}.json",
                                               {"ItemVendorNums": self._vendor_numbers_body(item, vendor_id, number)}))
            summary = (f"Save {fmt.text(vendor.get('name'))}'s own item number on {len(requests)} item(s) (Lightspeed's "
                       "Vendor ID field), so their invoices match these items. Any vendor numbers the items already "
                       "have are sent back unchanged. Nothing else on the items changes.")
            return Plan("set_vendor_numbers", summary, requests)

        return self._run("set_vendor_numbers", dry_run, confirm_code, build)

    # ---- 10. an invoice or packing slip: one plan, one approval -------------------------------------

    def intake_invoice(
        self,
        vendor_id: int | None,
        invoice_number: str,
        lines: list[dict],
        invoice_date: str | None = None,
        ship_cost: float | None = None,
        other_cost: float | None = None,
        order_id: int | None = None,
        mark_received: bool = True,
        save_vendor_numbers: bool = True,
        dry_run: bool = True,
        confirm_code: str | None = None,
        new_vendor: dict | None = None,
        allow_similar_names: bool = False,
    ) -> dict:
        """See intake.py for how lines are matched. The plan, in order: a new vendor and brands if the
        document has them, the PO (new, or the one given),
        new items, vendor numbers, PO lines with what arrived, and the PO's dates (ordered = the
        invoice date, received = today, which puts it in Check In)."""

        def build() -> Plan:
            if not lines:
                raise RuleViolation("Refused: give the invoice's lines.")
            if len(lines) > MAX_INVOICE_LINES:
                raise RuleViolation(f"Refused: {len(lines)} lines; the limit is {MAX_INVOICE_LINES} per invoice.")
            if (vendor_id is None) == (new_vendor is None):
                raise RuleViolation("Refused: give vendor_id for an existing vendor, or new_vendor for one Lightspeed "
                                    "doesn't have yet (not both).")
            warnings: list[str] = []
            requests: list[PlannedRequest] = []

            # 0. A vendor or brands Lightspeed doesn't have yet are created first, in the same plan.
            if new_vendor is not None:
                if order_id is not None:
                    raise RuleViolation("Refused: an existing purchase order can't belong to a new vendor.")
                vendor_name = self._check_new_name("vendor", new_vendor.get("name"), allow_similar_names, warnings,
                                                   option="allow_similar_names")
                vendor_body = {"name": vendor_name}
                if rules.clean(new_vendor.get("account_number")):
                    vendor_body["accountNumber"] = rules.clean(new_vendor.get("account_number"))
                requests.append(PlannedRequest(f"create vendor '{vendor_name}'", "POST", "Vendor.json", vendor_body,
                                               creates="vendor"))
                vendor_ref = new_ref("vendor")
            else:
                vendor = self._active_vendor(vendor_id)
                vendor_name, vendor_ref = fmt.text(vendor.get("name")) or "", str(vendor.get("vendorID"))
            brand_refs: dict[str, str] = {}  # brand name key -> "{new:brand:N}"
            brand_names = [rules.clean((line.get("new_item") or {}).get("new_brand")) for line in lines]
            if any(brand_names):
                existing_brands = self._existing_names("brand")
                for name in dict.fromkeys(n for n in brand_names if n):
                    brand = self._check_new_name("brand", name, allow_similar_names, warnings, existing_brands,
                                                 option="allow_similar_names")
                    if rules.name_key(brand) in brand_refs:
                        continue
                    label = f"brand:{len(brand_refs) + 1}"
                    requests.append(PlannedRequest(f"create brand '{brand}'", "POST", "Manufacturer.json",
                                                   {"name": brand}, creates=label))
                    brand_refs[rules.name_key(brand)] = new_ref(label)

            matcher = intake.InvoiceMatcher(self.reader)
            matches = matcher.match(vendor_ref, lines)
            unresolved = [m for m in matches if m.status == "unresolved"]
            if unresolved:
                raise RuleViolation("Refused: some lines don't match an item yet. " + " ".join(
                    f"Line {m.number}: {m.reason}" for m in unresolved[:10]) + " match_invoice_lines lists candidates.")
            invoice_day = iso_date(invoice_date, "invoice date") if invoice_date else None

            # 1. The purchase order.
            if order_id is not None:
                order = self._open_order(order_id)
                if str(order.get("vendorID")) != vendor_ref:
                    raise RuleViolation(f"Refused: purchase order {order_id} is for another vendor.")
                order_ref, on_po = str(int(order_id)), self._order_lines(order_id)
                ref = rules.clean(invoice_number)
                if ref and ref.lower() != (fmt.text(order.get("refNum")) or "").lower():
                    warnings.append(f"PO {order_id}'s reference is {fmt.text(order.get('refNum')) or 'blank'}; this "
                                    f"document's number is {ref}.")
                po_text = f"purchase order {order_id}"
            else:
                if new_vendor is not None:  # a new vendor has no POs yet, so its reference can't be taken
                    ref = rules.clean(invoice_number)
                    if not ref:
                        raise RuleViolation("Refused: give the invoice or order number (invoice_number).")
                    body = {"vendorID": vendor_ref, "shopID": self._single_shop(), "refNum": ref,
                            "shipCost": money_or_zero(ship_cost, "shipping cost"),
                            "otherCost": money_or_zero(other_cost, "other cost")}
                    parts = [f"shipping {body['shipCost']}", f"other costs {body['otherCost']}"]
                else:
                    body, _, parts = self._new_order(vendor_id, invoice_number, ship_cost, other_cost)
                requests.append(PlannedRequest(f"create a PO for {vendor_name}, ref {body['refNum']} ({', '.join(parts)})",
                                               "POST", "Order.json", body, creates="order"))
                order, order_ref, on_po = {}, new_ref("order"), []
                po_text = f"the new purchase order (ref {body['refNum']})"

            # 2. Each item once per invoice.
            seen: dict[tuple, int] = {}
            for m in matches:
                if m.status == "existing":
                    key = ("item", str(m.item.get("itemID")))
                elif m.status == "new_variant":
                    key = ("variant", str(m.matrix.get("itemMatrixID")), *(v.lower() for v in m.values))
                else:
                    key = ("new", rules.clean((m.line.get("new_item") or {}).get("description")).lower())
                if key in seen:
                    raise RuleViolation(f"Refused: lines {seen[key]} and {m.number} are the same item. Combine them "
                                        "into one line.")
                seen[key] = m.number

            # 3. New items: variants (checked per matrix, like add_matrix_variants) and standalone items.
            new_matches = [m for m in matches if m.status != "existing"]
            skus = self._sku_allocator([m.line.get("custom_sku") or (m.line.get("new_item") or {}).get("custom_sku")
                                        for m in new_matches])
            matrix_info: dict[str, tuple[dict, str]] = {}
            for mid in dict.fromkeys(str(m.matrix.get("itemMatrixID")) for m in matches if m.status == "new_variant"):
                matrix, matrix_desc, pants = self._target_matrix(mid)
                group = [m for m in matches if m.status == "new_variant" and str(m.matrix.get("itemMatrixID")) == mid]
                combos = [self._clean_values({"color": m.values[0], "size": m.values[1], "fit": m.values[2]},
                                             f"line {m.number}") for m in group]
                self._check_matrix_values(mid, matrix, pants, combos, warnings)
                matrix_info[mid] = (matrix, matrix_desc)
            tax = self._tax_fields(warnings) if any(m.status == "new_item" for m in matches) else {}
            taken_desc = self._active_descriptions(skus)
            matrices = self._all_matrices() if any(m.status == "new_item" for m in matches) else []

            # Vendor numbers printed on several lines are style numbers shared by sizes or colors:
            # Lightspeed's Vendor ID must belong to one item, so those are not saved.
            printed: dict[str, int] = {}
            for m in matches:
                number = rules.clean(m.line.get("vendor_number")).lower()
                if number:
                    printed[number] = printed.get(number, 0) + 1
            shared = sorted(n for n, count in printed.items() if count > 1)
            if save_vendor_numbers and shared:
                warnings.append(f"Not saved as vendor numbers (each is on several lines, so it's a style number): "
                                f"{', '.join(shared)}.")

            item_refs: dict[int, str] = {}
            costs: dict[int, tuple[int, str]] = {}
            numbers_saved = 0
            for m in matches:
                qty = quantity(m.line.get("quantity"), f"line {m.number} quantity")
                cost = money(m.line.get("unit_cost"), f"line {m.number} unit cost")
                costs[m.number] = (qty, cost)
                number = rules.clean(m.line.get("vendor_number"))
                save_number = False
                if save_vendor_numbers and number and number.lower() not in shared and not m.vendor_number_known:
                    try:
                        self._check_vendor_number(vendor_ref, number, m.item, matcher.items)
                        save_number = True
                    except RuleViolation as e:
                        warnings.append(f"Line {m.number}: vendor number not saved. {str(e).removeprefix('Refused: ')}")
                if m.status == "new_variant":
                    matrix, matrix_desc = matrix_info[str(m.matrix.get("itemMatrixID"))]
                    if m.line.get("price") is None:
                        raise RuleViolation(f"Refused: line {m.number} is a new item in matrix "
                                            f"{matrix.get('itemMatrixID')} '{matrix_desc}': give its selling price.")
                    body = self._variant_body(matrix, matrix_desc, m.values, skus.take(m.line.get("custom_sku")), cost,
                                              money(m.line["price"], f"line {m.number} price"))
                elif m.status == "new_item":
                    entry = {**m.line["new_item"], "cost": m.line.get("unit_cost"), "vendor_ref": vendor_ref}
                    entry["custom_sku"] = entry.get("custom_sku") or m.line.get("custom_sku")
                    if rules.clean(entry.get("new_brand")):
                        entry["brand_ref"] = brand_refs[rules.name_key(entry["new_brand"])]
                    body, _ = self._standalone_item(entry, skus, tax, taken_desc, matrices, warnings)
                else:
                    item = m.item
                    desc = fmt.text(item.get("description")) or ""
                    rules.check_not_three_piece(desc)
                    rules.check_not_boys(desc)
                    item_refs[m.number] = str(item.get("itemID"))
                    if save_number:
                        numbers_saved += 1
                        requests.append(PlannedRequest(
                            f"line {m.number}: save vendor number {number} on {item_label(item)}", "PUT",
                            f"Item/{item.get('itemID')}.json",
                            {"ItemVendorNums": self._vendor_numbers_body(item, vendor_ref, number, cost)}))
                    if not fmt.text(item.get("customSku")):
                        warnings.append(f"Line {m.number}: {item_label(item)} has no custom SKU (assign_custom_skus).")
                    if not fmt.ref_id(item.get("itemMatrixID")):
                        warnings.append(f"Line {m.number}: {item_label(item)} is not in a matrix (a catch-all item?).")
                    qoh = fmt.total_qoh(item)
                    if qoh is not None and qoh < 0:
                        warnings.append(f"Line {m.number}: {item_label(item)} has negative stock ({qoh}); after check-in "
                                        f"it would be {qoh + qty}.")
                    continue
                if save_number:
                    numbers_saved += 1
                    body["ItemVendorNums"] = self._vendor_numbers_body(None, vendor_ref, number, cost)
                name = f"item:{m.number}"
                requests.append(PlannedRequest(
                    f"line {m.number}: create '{body['description']}' (SKU {body['customSku']}, cost {cost}, price "
                    f"{body['Prices']['ItemPrice'][0]['amount']})", "POST", "Item.json", body, creates=name))
                item_refs[m.number] = new_ref(name)

            # 4. The PO lines: what arrived, at the invoice's unit cost.
            units, total = 0, 0.0
            for m in matches:
                qty, cost = costs[m.number]
                units += qty
                total += qty * float(cost)
                item_ref = item_refs[m.number]
                existing = [line for line in on_po if str(line.get("itemID")) == item_ref]
                if len(existing) > 1:
                    raise RuleViolation(f"Refused: line {m.number}'s item is on {len(existing)} lines of {po_text}.")
                if existing:
                    line = existing[0]
                    ordered = fmt.integer(line.get("quantity")) or 0
                    received = fmt.integer(line.get("numReceived")) or 0
                    checked_in = fmt.integer(line.get("checkedIn")) or 0
                    body, changes = {}, []
                    if mark_received:
                        body["numReceived"] = str(received + qty)
                        changes.append(f"received {received} -> {received + qty}")
                        if checked_in + received + qty > ordered:
                            body["quantity"] = str(checked_in + received + qty)
                            changes.append(f"ordered {ordered} -> {checked_in + received + qty}")
                            warnings.append(f"Line {m.number}: more arrived than was ordered on PO line "
                                            f"{line.get('orderLineID')}; its quantity is raised to match.")
                    if abs(float(cost) - (fmt.number(line.get("originalPrice") or line.get("price")) or 0)) > 0.001:
                        if checked_in:
                            warnings.append(f"Line {m.number}: the invoice cost {cost} differs from the PO's "
                                            f"{line.get('originalPrice')}, but some were already checked in; not changed.")
                        else:
                            body.update(price=cost, originalPrice=cost)
                            changes.append(f"unit cost {line.get('originalPrice')} -> {cost}")
                    if body:
                        requests.append(PlannedRequest(f"line {m.number}: PO line {line.get('orderLineID')}: "
                                                       + "; ".join(changes), "PUT",
                                                       f"OrderLine/{line.get('orderLineID')}.json", body))
                else:
                    requests.append(PlannedRequest(
                        f"line {m.number}: add {qty} x " + (item_label(m.item) if m.item else f"new item {item_ref}")
                        + f" at {cost}" + (", received" if mark_received else ""), "POST", "OrderLine.json",
                        {"orderID": order_ref, "itemID": item_ref, "quantity": str(qty), "price": cost,
                         "originalPrice": cost, "numReceived": str(qty) if mark_received else "0"}))
            if order_id is not None:
                invoiced = set(item_refs.values())
                missing = [line for line in on_po if str(line.get("itemID")) not in invoiced
                           and (fmt.integer(line.get("quantity")) or 0) > (fmt.integer(line.get("checkedIn")) or 0)
                           + (fmt.integer(line.get("numReceived")) or 0)]
                if missing:
                    warnings.append(f"{len(missing)} line(s) on {po_text} are not on this document (still to come?).")

            # 5. The PO's own fields: costs on an existing PO, ordered date, Check In.
            header: dict = {}
            if order_id is not None:
                for value, key, label, convert in ((ship_cost, "shipCost", "shipping", money_or_zero),
                                                   (other_cost, "otherCost", "other costs", signed_money)):
                    if value is not None:
                        text = convert(value, label)
                        if abs(float(text) - (fmt.number(order.get(key)) or 0)) > 0.001:
                            header[key] = text
            if invoice_day and not fmt.date(order.get("orderedDate")):
                header["orderedDate"] = invoice_day
            if mark_received and not fmt.date(order.get("receivedDate")):
                header["receivedDate"] = date.today().isoformat()
            if header:
                requests.append(PlannedRequest(f"{po_text}: " + ", ".join(f"{k} {v}" for k, v in header.items()), "PUT",
                                               f"Order/{order_ref}.json", header))

            counts = {s: sum(1 for m in matches if m.status == s) for s in ("existing", "new_variant", "new_item")}
            created_first = ([f"the new vendor '{vendor_name}'"] if new_vendor is not None else []) + (
                [f"{len(brand_refs)} new brand(s)"] if brand_refs else [])
            summary = (("Creates " + " and ".join(created_first) + " first. " if created_first else "")
                       + f"{vendor_name} document {rules.clean(invoice_number) or '-'}: {len(matches)} line(s), {units} "
                       f"unit(s), {total:.2f} at cost, on {po_text}. Existing items: {counts['existing']}; new items in "
                       f"a matrix: {counts['new_variant']}; new items outside a matrix: {counts['new_item']}"
                       f"{skus.picked_note()}; vendor numbers saved: {numbers_saved}. "
                       + ("Every line is marked received. " if mark_received else "Nothing is marked received. ")
                       + "Prices of existing items are not touched.")
            after = (f"Stock does NOT change yet. In Lightspeed, open {po_text}: it is in Check In with the received "
                     "quantities filled in. Check them against the goods, click Add Received To Inventory, then "
                     "Finished. Lightspeed's API has no call for that step. Shipping is spread onto the lines by the "
                     "PO's own setting." if mark_received else
                     f"Nothing is received. When the goods arrive, run this again with mark_received=true for {po_text}.")
            return Plan("intake_invoice", summary, requests, warnings, after, max_requests=4 * MAX_INVOICE_LINES)

        return self._run("intake_invoice", dry_run, confirm_code, build)

    # ---- 11. update an existing matrix ----------------------------------------------------------------

    def update_matrix(
        self,
        matrix_id: int,
        description: str | None = None,
        category_id: int | None = None,
        manufacturer_id: int | None = None,
        vendor_id: int | None = None,
        default_cost: float | None = None,
        price: float | None = None,
        dry_run: bool = True,
        confirm_code: str | None = None,
    ) -> dict:
        """Lightspeed's Retail help splits a matrix's fields in two ("Creating matrixes"): Shared values
        (description, category, brand, vendor) are applied to every item in the matrix when it is saved,
        and Default values (price, cost) are only used for new items or the "Reset Items To Defaults"
        button. That split is not documented for the API, so the dry run spells out what each change
        would reach if it did cascade."""

        def build() -> Plan:
            matrix = self.reader._matrix_record(int(matrix_id))
            if matrix is None:
                raise RuleViolation(f"Refused: matrix {matrix_id} was not found (or can't be read).")
            if fmt.flag(matrix.get("archived")):
                raise RuleViolation(f"Refused: matrix {matrix_id} is archived.")
            current_desc = fmt.text(matrix.get("description")) or ""
            children, _ = self.client.get_all("Item.json", {
                "itemMatrixID": str(int(matrix_id)), "load_relations": relations("ItemAttributes")})
            n = len(children)
            body: dict = {}
            changes: list[str] = []
            warnings: list[str] = []

            if description is not None and rules.clean(description) != current_desc:
                desc = rules.check_plain_description(description, "matrix description")
                if not any(re.search(rf"\b{re.escape(w)}\b", desc, re.IGNORECASE)
                           for words in rules.GARMENT_WORDS.values() for w in words):
                    raise RuleViolation(f"Refused: the matrix description '{desc}' doesn't say what garment it is "
                                        f"(e.g. '{desc} Suit'). Matrices are split by garment type.")
                matrices = self._all_matrices()
                names = [self._vendor_name(matrix.get("defaultVendorID"))]
                if fmt.ref_id(matrix.get("manufacturerID")):
                    m = self._get_record(f"Manufacturer/{matrix.get('manufacturerID')}.json", "Manufacturer") or {}
                    names.append(fmt.text(m.get("name")) or "")
                rules.check_not_split_by_color(desc, self._known_colors(matrices), names)
                for other in matrices:
                    if (str(other.get("itemMatrixID")) != str(matrix_id) and not fmt.flag(other.get("archived"))
                            and (fmt.text(other.get("description")) or "").lower() == desc.lower()):
                        raise RuleViolation(f"Refused: matrix {other.get('itemMatrixID')} is already called '{desc}'.")
                if desc != current_desc and not children:
                    body["description"] = desc
                    changes.append(f"description '{current_desc}' -> '{desc}'")
                    warnings.append("The matrix has no items, so no item is renamed.")
                elif desc != current_desc:
                    body["description"] = desc
                    changes.append(f"description '{current_desc}' -> '{desc}'")
                    examples = []
                    for child in children[:3]:
                        new_child = " ".join(p for p in (desc, *intake.attribute_values(child)) if p)
                        examples.append(f"'{fmt.text(child.get('description'))}' -> '{new_child}'")
                    custom = [c for c in children if fmt.text(c.get("description")) != " ".join(
                        p for p in (current_desc, *intake.attribute_values(c)) if p)]
                    warnings.append(f"Lightspeed renames all {n} item(s) in the matrix to '<description> <color> <size> "
                                    f"<fit>', e.g. " + "; ".join(examples) + "."
                                    + (f" {len(custom)} of them have a description that doesn't follow the pattern "
                                       "today and would be renamed too." if custom else ""))

            for value, key, label, check in (
                    (category_id, "categoryID", "category", lambda v: self._active_category_path(v)),
                    (manufacturer_id, "manufacturerID", "brand (manufacturer)", self._manufacturer_name),
                    (vendor_id, "defaultVendorID", "vendor", lambda v: fmt.text(self._active_vendor(v).get("name")))):
                if value is None or str(int(value)) == str(matrix.get(key)):
                    continue
                name = check(value)
                body[key] = str(int(value))
                changes.append(f"{label} {matrix.get(key)} -> {int(value)} ({name})")
                differ = sum(1 for c in children if str(c.get(key)) != str(int(value)))
                warnings.append(f"Lightspeed applies the matrix's {label} to all {n} item(s); {differ} of them have a "
                                "different one today.")

            if default_cost is not None:
                cost = money(default_cost, "default cost")
                if abs(float(cost) - (fmt.number(matrix.get("defaultCost")) or 0)) > 0.001:
                    body["defaultCost"] = cost
                    changes.append(f"default cost {matrix.get('defaultCost')} -> {cost}")
            if price is not None:
                new_price = money(price, "price")
                levels = fmt.as_list((matrix.get("Prices") or {}).get("ItemPrice"))
                if abs(float(new_price) - (fmt.prices(matrix).get("Default") or 0)) > 0.001:
                    # Every level is sent back, only Default changed: an empty or missing level could blank it.
                    kept = [{k: p.get(k) for k in ("amount", "useTypeID", "useType")} for p in levels
                            if str(p.get("useTypeID")) != "1"]
                    body["Prices"] = {"ItemPrice": [{"amount": new_price, "useTypeID": "1", "useType": "Default"},
                                                    *kept]}
                    changes.append(f"Default price {fmt.prices(matrix).get('Default')} -> {new_price}")
            if "defaultCost" in body or "Prices" in body:
                selling: dict[float, int] = {}
                for c in children:
                    p = fmt.prices(c).get("Default")
                    if p is not None:
                        selling[p] = selling.get(p, 0) + 1
                spread = ", ".join(f"{count} at {p:.2f}" for p, count in sorted(selling.items(), key=lambda s: -s[1]))
                warnings.append("Lightspeed's help describes a matrix's price and cost as defaults for new items "
                                "(existing items keep their own unless someone clicks Reset Items To Defaults). This "
                                f"isn't documented for the API: if it did reach the items, all {n} would change. Today "
                                f"they sell at: {spread or 'no prices'}.")
            if not body:
                raise RuleViolation(f"Refused: nothing to change on matrix {matrix_id}.")

            summary = (f"Change matrix {matrix_id} '{current_desc}' ({n} active item(s)): " + "; ".join(changes) + ". "
                       "Its attribute set, attribute values and items' SKUs, stock and own prices are not sent.")
            after = ("Take a snapshot before (take_snapshot) and compare after (compare_snapshots): it shows exactly "
                     "which items Lightspeed changed. This is the first matrix update, so what Lightspeed does to the "
                     "items is still to be confirmed.")
            return Plan("update_matrix", summary,
                        [PlannedRequest(f"update matrix {matrix_id}", "PUT", f"ItemMatrix/{int(matrix_id)}.json", body)],
                        warnings, after,
                        basis={"timeStamp": matrix.get("timeStamp"), "items": n})

        return self._run("update_matrix", dry_run, confirm_code, build)

    # ---- 12. new vendors and brands ---------------------------------------------------------------

    def _existing_names(self, kind: str) -> list[dict]:
        """Every vendor (archived too) or brand, as {"id", "name", "archived"}."""
        if kind == "vendor":
            rows, _ = self.client.get_all("Vendor.json", {"archived": "true"})
            return [{"id": r.get("vendorID"), "name": fmt.text(r.get("name")) or "", "archived": fmt.flag(r.get("archived"))}
                    for r in rows]
        rows, _ = self.client.get_all("Manufacturer.json")
        return [{"id": r.get("manufacturerID"), "name": fmt.text(r.get("name")) or "", "archived": False} for r in rows]

    def _check_new_name(self, kind: str, name, allow_similar: bool, warnings: list[str],
                        existing: list[dict] | None = None, option: str = "allow_similar_name") -> str:
        """Refuse a name that a vendor or brand already has; refuse a near-match unless allowed."""
        name = rules.check_record_name(name, kind)
        same, similar = rules.same_and_similar_names(name, self._existing_names(kind) if existing is None else existing)
        if same:
            record = same[0]
            raise RuleViolation(f"Refused: {kind} {record['id']} is already called '{record['name']}'"
                                + (" (archived: unarchive it in Lightspeed instead)." if record["archived"] else ". Use it."))
        listed = ", ".join(f"'{r['name']}' ({kind} {r['id']}{', archived' if r['archived'] else ''})" for r in similar)
        if similar and not allow_similar:
            raise RuleViolation(f"Refused: '{name}' looks like an existing {kind}: {listed}. If it really is a different "
                                f"{kind}, ask again with {option}=true.")
        if similar:
            warnings.append(f"Similar existing {kind}(s): {listed}. Created anyway, as asked.")
        return name

    def create_vendor(self, name: str, account_number: str | None = None, allow_similar_name: bool = False,
                      dry_run: bool = True, confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            warnings: list[str] = []
            vendor_name = self._check_new_name("vendor", name, allow_similar_name, warnings)
            body = {"name": vendor_name}
            if rules.clean(account_number):
                body["accountNumber"] = rules.clean(account_number)
            summary = (f"Create the vendor '{vendor_name}'" + (f", account number {body['accountNumber']}"
                                                               if "accountNumber" in body else "")
                       + ". Only the name (and account number) are sent; Lightspeed's other vendor settings keep their "
                         "defaults. Its name is kept exactly as given.")
            return Plan("create_vendor", summary,
                        [PlannedRequest(f"create vendor '{vendor_name}'", "POST", "Vendor.json", body)], warnings)

        return self._run("create_vendor", dry_run, confirm_code, build)

    def create_brand(self, name: str, allow_similar_name: bool = False, dry_run: bool = True,
                     confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            warnings: list[str] = []
            brand = self._check_new_name("brand", name, allow_similar_name, warnings)
            warnings.append("Lightspeed can't delete a brand once it exists (it can only be renamed).")
            summary = (f"Create the brand '{brand}' (Lightspeed's Manufacturer). Only its name is sent, exactly as "
                       "given.")
            return Plan("create_brand", summary,
                        [PlannedRequest(f"create brand '{brand}'", "POST", "Manufacturer.json", {"name": brand})], warnings)

        return self._run("create_brand", dry_run, confirm_code, build)

    def _manufacturer_name(self, manufacturer_id) -> str:
        record = self._get_record(f"Manufacturer/{int(manufacturer_id)}.json", "Manufacturer")
        if record is None:
            raise RuleViolation(f"Refused: manufacturer {manufacturer_id} does not exist.")
        return fmt.text(record.get("name")) or ""

    # ---- 13. item prices -------------------------------------------------------------------------------

    PRICE_LEVELS = (("default", "1", "Default"), ("msrp", "2", "MSRP"), ("online", "3", "Online"))

    def update_item_prices(self, items: list[dict], dry_run: bool = True, confirm_code: str | None = None) -> dict:
        """The only tool that changes an existing item's prices: each change is asked for explicitly."""

        def build() -> Plan:
            if not items:
                raise RuleViolation("Refused: give at least one item.")
            if len(items) > MAX_REQUESTS_PER_CALL:
                raise RuleViolation(f"Refused: {len(items)} items; the limit is {MAX_REQUESTS_PER_CALL} per call.")
            ids = [str(int(entry.get("item_id"))) for entry in items]
            if len(set(ids)) != len(ids):
                raise RuleViolation("Refused: the same item is listed twice.")
            found = self._items_by_id(ids)
            requests, warnings = [], []
            for entry, item_id in zip(items, ids):
                item = found.get(item_id)
                if item is None:
                    raise RuleViolation(f"Refused: item {item_id} was not found.")
                if fmt.flag(item.get("archived")):
                    raise RuleViolation(f"Refused: {item_label(item)} is archived.")
                current = {str(p.get("useTypeID")): p for p in fmt.as_list((item.get("Prices") or {}).get("ItemPrice"))}
                levels, changes = [], []
                for key, type_id, use_type in self.PRICE_LEVELS:
                    old = fmt.number((current.get(type_id) or {}).get("amount"))
                    new_value = entry.get(key)
                    if new_value is None:
                        amount = (current.get(type_id) or {}).get("amount")
                        if amount is not None:  # unchanged, but sent back so no level can be blanked
                            levels.append({"amount": str(amount), "useTypeID": type_id, "useType": use_type})
                        continue
                    amount = money(new_value, f"{use_type} price") if key == "default" else money_or_zero(
                        new_value, f"{use_type} price")
                    levels.append({"amount": amount, "useTypeID": type_id, "useType": use_type})
                    if old is None or abs(old - float(amount)) > 0.001:
                        changes.append(f"{use_type} {old if old is not None else '(none)'} -> {amount}")
                if not changes:
                    raise RuleViolation(f"Refused: nothing to change on {item_label(item)}'s prices.")
                new = {p["useType"]: float(p["amount"]) for p in levels}
                cost = fmt.number(item.get("defaultCost")) or 0
                if new.get("Default", 0) <= cost:
                    warnings.append(f"{item_label(item)}: Default price {new.get('Default'):.2f} is not above its cost "
                                    f"{cost:.2f}.")
                if new.get("MSRP") and new.get("MSRP") < new.get("Default", 0):
                    warnings.append(f"{item_label(item)}: MSRP {new['MSRP']:.2f} is below the Default price.")
                if fmt.ref_id(item.get("itemMatrixID")):
                    warnings.append(f"{item_label(item)} is in matrix {item.get('itemMatrixID')}; only this item's own "
                                    "prices change, not the matrix or its other items.")
                requests.append(PlannedRequest(f"{item_label(item)}: " + "; ".join(changes), "PUT",
                                               f"Item/{item_id}.json", {"Prices": {"ItemPrice": levels}}))
            summary = (f"Change the prices of {len(requests)} item(s) as listed. Each item's three price levels are "
                       "sent together; levels not asked for are sent back unchanged. Nothing else on the items changes.")
            return Plan("update_item_prices", summary, requests, warnings)

        return self._run("update_item_prices", dry_run, confirm_code, build)

    # ---- 14. reorder point and level ---------------------------------------------------------------------

    def set_reorder_levels(self, items: list[dict], dry_run: bool = True, confirm_code: str | None = None) -> dict:
        """reorder_point: order more when stock falls to this. reorder_level: order up to this many."""

        def build() -> Plan:
            if not items:
                raise RuleViolation("Refused: give at least one item.")
            if len(items) > MAX_REQUESTS_PER_CALL:
                raise RuleViolation(f"Refused: {len(items)} items; the limit is {MAX_REQUESTS_PER_CALL} per call.")
            ids = [str(int(entry.get("item_id"))) for entry in items]
            if len(set(ids)) != len(ids):
                raise RuleViolation("Refused: the same item is listed twice.")
            shop = self._single_shop()
            found = self._items_by_id(ids)
            requests, warnings = [], []
            for entry, item_id in zip(items, ids):
                item = found.get(item_id)
                if item is None:
                    raise RuleViolation(f"Refused: item {item_id} was not found.")
                if fmt.flag(item.get("archived")):
                    raise RuleViolation(f"Refused: {item_label(item)} is archived.")
                row = next((s for s in fmt.as_list((item.get("ItemShops") or {}).get("ItemShop"))
                            if str(s.get("shopID")) == shop), None)
                if row is None or not fmt.ref_id(row.get("itemShopID")):
                    raise RuleViolation(f"Refused: {item_label(item)} has no stock row for shop {shop}.")
                record, changes = {}, []
                for key, field_name, label in (("reorder_point", "reorderPoint", "reorder point"),
                                               ("reorder_level", "reorderLevel", "reorder level")):
                    if entry.get(key) is None:
                        continue
                    try:
                        value = int(entry[key])
                    except (TypeError, ValueError):
                        raise RuleViolation(f"Refused: {label} '{entry[key]}' is not a whole number.") from None
                    if value < 0 or value != float(entry[key]):
                        raise RuleViolation(f"Refused: {label} must be a whole number of 0 or more.")
                    old = fmt.integer(row.get(field_name)) or 0
                    if value != old:
                        record[field_name] = str(value)
                        changes.append(f"{label} {old} -> {value}")
                if not changes:
                    raise RuleViolation(f"Refused: nothing to change on {item_label(item)}'s reorder settings.")
                point = int(record.get("reorderPoint", fmt.integer(row.get("reorderPoint")) or 0))
                level = int(record.get("reorderLevel", fmt.integer(row.get("reorderLevel")) or 0))
                if level and level < point:
                    warnings.append(f"{item_label(item)}: reorder level {level} is below its reorder point {point}.")
                requests.append(PlannedRequest(f"{item_label(item)}: " + "; ".join(changes), "PUT",
                                               f"ItemShop/{row.get('itemShopID')}.json", record))
            summary = (f"Set the reorder point and/or level of {len(requests)} item(s) for shop {shop}, through "
                       "Lightspeed's ItemShop endpoint, which only updates those two settings. Stock on hand is never "
                       "sent.")
            return Plan("set_reorder_levels", summary, requests, warnings)

        return self._run("set_reorder_levels", dry_run, confirm_code, build)

    # ---- 15. archive a purchase order --------------------------------------------------------------------

    def archive_purchase_order(self, order_id: int, dry_run: bool = True, confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            order = self._get_record(f"Order/{int(order_id)}.json", "Order")
            if order is None:
                raise RuleViolation(f"Refused: purchase order {order_id} was not found.")
            if fmt.flag(order.get("archived")):
                raise RuleViolation(f"Refused: purchase order {order_id} is already archived.")
            lines = self._order_lines(order_id)
            touched = [line for line in lines if (fmt.integer(line.get("numReceived")) or 0)
                       or (fmt.integer(line.get("checkedIn")) or 0)]
            if touched:
                raise RuleViolation(f"Refused: purchase order {order_id} has {len(touched)} line(s) with goods received "
                                    "or checked in. Only POs where nothing arrived are archived here; finish this one "
                                    "in Lightspeed instead.")
            if fmt.flag(order.get("complete")):
                raise RuleViolation(f"Refused: purchase order {order_id} is finished; it doesn't need archiving.")
            vendor = self._vendor_name(order.get("vendorID"))
            ordered = sum(fmt.integer(line.get("quantity")) or 0 for line in lines)
            before = {"order": {k: order.get(k) for k in ("orderID", "vendorID", "refNum", "orderedDate", "createTime",
                                                         "shipCost", "otherCost", "complete") if order.get(k) is not None},
                      "lines": [{k: line.get(k) for k in ("orderLineID", "itemID", "quantity", "price", "originalPrice")}
                                for line in lines]}
            summary = (f"Archive purchase order {order_id} ({vendor}, ref {fmt.text(order.get('refNum')) or '-'}, created "
                       f"{(fmt.date(order.get('createTime')) or '?')[:10]}): {len(lines)} line(s), {ordered} unit(s) "
                       "ordered, nothing received. Lightspeed archives a PO on DELETE (it is not erased). Items and "
                       "stock are not touched.")
            warnings = ["Its lines and values are saved in the write log first."]
            return Plan("archive_purchase_order", summary,
                        [PlannedRequest(f"archive PO {order_id}", "DELETE", f"Order/{int(order_id)}.json", None,
                                        before=before)],
                        warnings, basis={"timeStamp": order.get("timeStamp"), "lines": len(lines)})

        return self._run("archive_purchase_order", dry_run, confirm_code, build)

    # ---- 16. correct a vendor number ----------------------------------------------------------------------

    def correct_vendor_numbers(self, vendor_id: int, items: list[dict], dry_run: bool = True,
                               confirm_code: str | None = None) -> dict:
        """Change the value of a vendor number an item already has for this vendor (its ID and cost are kept).
        Removing one isn't documented by Lightspeed, so it isn't offered."""

        def build() -> Plan:
            if not items:
                raise RuleViolation("Refused: give at least one item.")
            vendor = self._active_vendor(vendor_id)
            ids = [str(int(entry.get("item_id"))) for entry in items]
            numbers = [rules.clean(entry.get("vendor_number")) for entry in items]
            if len(set(ids)) != len(ids) or len({n.lower() for n in numbers}) != len(numbers):
                raise RuleViolation("Refused: the same item or the same vendor number is listed twice.")
            found = self._items_by_id(ids)
            requests = []
            for item_id, number in zip(ids, numbers):
                item = found.get(item_id)
                if item is None:
                    raise RuleViolation(f"Refused: item {item_id} was not found.")
                current = [v for v in intake.vendor_numbers(item) if str(v.get("vendorID")) == str(vendor_id)]
                if not current:
                    raise RuleViolation(f"Refused: {item_label(item)} has no vendor number for this vendor; "
                                        "set_vendor_numbers adds one.")
                old = fmt.text(current[0].get("value")) or ""
                if not number:
                    raise RuleViolation("Refused: a vendor number can't be blank (removing one isn't supported).")
                if number.lower() == old.lower() and number == old:
                    raise RuleViolation(f"Refused: {item_label(item)} already has vendor number {number}.")
                holder = self._vendor_number_holder(vendor_id, number)
                if holder and str(holder.get("itemID")) != item_id:
                    raise RuleViolation(f"Refused: vendor number '{number}' is already on {item_label(holder)}. Each "
                                        "vendor number must belong to one item.")
                records = []
                for v in intake.vendor_numbers(item):
                    record = {k: v.get(k) for k in ("itemVendorNumID", "vendorID", "value", "cost")
                              if v.get(k) not in (None, "")}
                    if v is current[0]:
                        record["value"] = number
                    records.append(record)
                requests.append(PlannedRequest(f"{item_label(item)}: vendor number '{old}' -> '{number}'", "PUT",
                                               f"Item/{item_id}.json", {"ItemVendorNums": {"ItemVendorNum": records}}))
            summary = (f"Correct {fmt.text(vendor.get('name'))}'s vendor number on {len(requests)} item(s). The number "
                       "keeps its record ID and cost; the items' other vendor numbers are sent back unchanged.")
            return Plan("correct_vendor_numbers", summary, requests,
                        ["Changing an existing vendor number isn't documented by Lightspeed: check the first one "
                         "afterwards (get_item) before correcting more."])

        return self._run("correct_vendor_numbers", dry_run, confirm_code, build)

    # ---- 17. vendors: rename / account number, archive -------------------------------------------------------

    def update_vendor(self, vendor_id: int, name: str | None = None, account_number: str | None = None,
                      allow_similar_name: bool = False, dry_run: bool = True, confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            vendor = self._active_vendor(vendor_id)
            current_name = fmt.text(vendor.get("name")) or ""
            body, changes, warnings = {}, [], []
            if name is not None and rules.clean(name) != current_name:
                others = [r for r in self._existing_names("vendor") if str(r["id"]) != str(vendor_id)]
                new_name = self._check_new_name("vendor", name, allow_similar_name, warnings, others)
                body["name"] = new_name
                changes.append(f"name '{current_name}' -> '{new_name}'")
                warnings.append("Every item, PO and return that uses this vendor shows the new name.")
            if account_number is not None and rules.clean(account_number) != (fmt.text(vendor.get("accountNumber")) or ""):
                body["accountNumber"] = rules.clean(account_number)
                changes.append(f"account number '{fmt.text(vendor.get('accountNumber')) or ''}' -> "
                               f"'{body['accountNumber']}'")
            if not body:
                raise RuleViolation(f"Refused: nothing to change on vendor {vendor_id}.")
            summary = f"Change vendor {vendor_id} '{current_name}': " + "; ".join(changes) + ". Nothing else is sent."
            return Plan("update_vendor", summary,
                        [PlannedRequest(f"update vendor {vendor_id}", "PUT", f"Vendor/{int(vendor_id)}.json", body)],
                        warnings)

        return self._run("update_vendor", dry_run, confirm_code, build)

    def archive_vendor(self, vendor_id: int, dry_run: bool = True, confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            vendor = self._active_vendor(vendor_id)
            items, _ = self.client.get_all("Item.json", {"defaultVendorID": str(int(vendor_id))}, max_records=5)
            if items:
                raise RuleViolation(f"Refused: active items still have vendor {vendor_id} as their default vendor (e.g. "
                                    f"{item_label(items[0])}). Move them to another vendor first.")
            orders, _ = self.client.get_all("Order.json", {"vendorID": str(int(vendor_id)), "complete": "false"},
                                            max_records=5)
            if orders:
                raise RuleViolation(f"Refused: vendor {vendor_id} has unfinished purchase order(s), e.g. "
                                    f"{orders[0].get('orderID')}. Finish or archive them first.")
            before = {k: vendor.get(k) for k in ("vendorID", "name", "accountNumber", "priceLevel", "updatePrice",
                                                 "updateCost", "updateDescription", "shareSellThrough")
                      if vendor.get(k) is not None}
            summary = (f"Archive vendor {vendor_id} '{fmt.text(vendor.get('name'))}'. No active item uses it as default "
                       "vendor and it has no unfinished PO. Lightspeed archives a vendor on DELETE (it is not erased); "
                       "its past POs and returns keep it.")
            return Plan("archive_vendor", summary,
                        [PlannedRequest(f"archive vendor {vendor_id}", "DELETE", f"Vendor/{int(vendor_id)}.json", None,
                                        before=before)],
                        ["Unarchiving a vendor isn't documented for the API; it may need doing in Lightspeed."],
                        basis={"timeStamp": vendor.get("timeStamp")})

        return self._run("archive_vendor", dry_run, confirm_code, build)

    # ---- 18. rename a brand ------------------------------------------------------------------------------------

    def rename_brand(self, manufacturer_id: int, name: str, allow_similar_name: bool = False, dry_run: bool = True,
                     confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            record = self._get_record(f"Manufacturer/{int(manufacturer_id)}.json", "Manufacturer")
            if record is None:
                raise RuleViolation(f"Refused: brand {manufacturer_id} does not exist.")
            current = fmt.text(record.get("name")) or ""
            if rules.clean(name) == current:
                raise RuleViolation(f"Refused: brand {manufacturer_id} is already called '{current}'.")
            warnings: list[str] = []
            others = [r for r in self._existing_names("brand") if str(r["id"]) != str(manufacturer_id)]
            new_name = self._check_new_name("brand", name, allow_similar_name, warnings, others)
            items, _ = self.client.get_all("Item.json", {"manufacturerID": str(int(manufacturer_id)), "archived": "true"})
            warnings.append(f"{len(items)} item(s) (archived included) and any matrix with this brand show the new name.")
            summary = f"Rename brand {manufacturer_id} '{current}' -> '{new_name}'. Only the name is sent."
            return Plan("rename_brand", summary,
                        [PlannedRequest(f"rename brand {manufacturer_id}", "PUT",
                                        f"Manufacturer/{int(manufacturer_id)}.json", {"name": new_name})], warnings)

        return self._run("rename_brand", dry_run, confirm_code, build)

    # ---- 19. categories: create, rename, delete ------------------------------------------------------------------

    def _category(self, category_id) -> dict:
        category = self.reader.categories().get(str(int(category_id)))
        if not category:
            raise RuleViolation(f"Refused: category {category_id} does not exist.")
        return category

    def _check_category_name(self, name, parent_id: str, except_id: str | None = None) -> str:
        """A plain name, not used by a sibling (same parent), ignoring case. '/' would break the path."""
        name = rules.check_record_name(name, "category")
        if "/" in name:
            raise RuleViolation("Refused: a category name can't contain '/' (Lightspeed uses it between levels).")
        rules.check_not_boys(name)
        for cid, c in self.reader.categories().items():
            if (str(c.get("parentID") or "0") == parent_id and cid != except_id
                    and (fmt.text(c.get("name")) or "").lower() == name.lower()):
                raise RuleViolation(f"Refused: category {cid} '{fmt.text(c.get('fullPathName'))}' already has this name "
                                    "at that level.")
        return name

    def create_category(self, name: str, parent_id: int | None = None, dry_run: bool = True,
                        confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            parent = str(int(parent_id)) if parent_id else "0"
            parent_path = fmt.text(self._category(parent).get("fullPathName")) if parent != "0" else ""
            new_name = self._check_category_name(name, parent)
            path = f"{parent_path}/{new_name}" if parent_path else new_name
            summary = f"Create the category '{path}'" + (f" under '{parent_path}'." if parent_path else " (top level).")
            return Plan("create_category", summary,
                        [PlannedRequest(f"create category '{path}'", "POST", "Category.json",
                                        {"name": new_name, "fullPathName": path, "parentID": parent})])

        return self._run("create_category", dry_run, confirm_code, build)

    def rename_category(self, category_id: int, name: str, dry_run: bool = True,
                        confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            category = self._category(category_id)
            cid = str(int(category_id))
            children = [c for c in self.reader.categories().values() if str(c.get("parentID")) == cid]
            if children:
                raise RuleViolation(f"Refused: category {cid} has {len(children)} subcategories. Lightspeed doesn't say "
                                    "whether their paths follow a rename, so only categories without subcategories "
                                    "are renamed here.")
            parent = str(category.get("parentID") or "0")
            new_name = self._check_category_name(name, parent, except_id=cid)
            if new_name == fmt.text(category.get("name")):
                raise RuleViolation(f"Refused: category {cid} is already called '{new_name}'.")
            old_path = fmt.text(category.get("fullPathName")) or ""
            path = (old_path.rsplit("/", 1)[0] + "/" + new_name) if "/" in old_path else new_name
            items, _ = self.client.get_all("Item.json", {"categoryID": cid, "archived": "true"})
            summary = (f"Rename category {cid} '{old_path}' -> '{path}'. Its {len(items)} item(s) stay in it and show "
                       "the new name.")
            return Plan("rename_category", summary,
                        [PlannedRequest(f"rename category {cid}", "PUT", f"Category/{cid}.json",
                                        {"name": new_name, "fullPathName": path})])

        return self._run("rename_category", dry_run, confirm_code, build)

    def delete_category(self, category_id: int, dry_run: bool = True, confirm_code: str | None = None) -> dict:
        def build() -> Plan:
            category = self._category(category_id)
            cid = str(int(category_id))
            children = [c for c in self.reader.categories().values() if str(c.get("parentID")) == cid]
            if children:
                raise RuleViolation(f"Refused: category {cid} has subcategories; Lightspeed won't delete it.")
            items, _ = self.client.get_all("Item.json", {"categoryID": cid, "archived": "true"}, max_records=5)
            if items:
                raise RuleViolation(f"Refused: category {cid} still has items (archived ones count too), e.g. "
                                    f"{item_label(items[0])}. Lightspeed doesn't say where they would end up.")
            matrices, _ = self.client.get_all("ItemMatrix.json", {"categoryID": cid, "archived": "true"}, max_records=5)
            if matrices:
                raise RuleViolation(f"Refused: matrix {matrices[0].get('itemMatrixID')} uses category {cid}.")
            path = fmt.text(category.get("fullPathName")) or ""
            before = {k: category.get(k) for k in ("categoryID", "name", "fullPathName", "parentID")}
            summary = f"Delete the empty category {cid} '{path}' (no items, matrices or subcategories)."
            return Plan("delete_category", summary,
                        [PlannedRequest(f"delete category {cid} '{path}'", "DELETE", f"Category/{cid}.json", None,
                                        before=before)],
                        ["Lightspeed deletes a category for good; its name and place are saved in the write log."])

        return self._run("delete_category", dry_run, confirm_code, build)

    # ---- 20. inventory counts (reconciling stays a click in Lightspeed) -------------------------------------------

    def create_inventory_count(self, name: str | None, items: list[dict], count_id: int | None = None,
                               dry_run: bool = True, confirm_code: str | None = None) -> dict:
        """Lightspeed's count flow: create the count, add each counted item with its quantity, then
        reconcile. Stock only changes on reconcile, and that is never sent from here."""

        def build() -> Plan:
            if not items:
                raise RuleViolation("Refused: give at least one counted item.")
            if len(items) > MAX_REQUESTS_PER_CALL:
                raise RuleViolation(f"Refused: {len(items)} items; the limit is {MAX_REQUESTS_PER_CALL} per call.")
            ids = [str(int(entry.get("item_id"))) for entry in items]
            if len(set(ids)) != len(ids):
                raise RuleViolation("Refused: the same item is listed twice. Lightspeed adds repeated entries "
                                    "together, so give each item once with its total.")
            requests: list[PlannedRequest] = []
            if count_id is not None:
                count = self._get_record(f"InventoryCount/{int(count_id)}.json", "InventoryCount")
                if count is None or fmt.flag(count.get("archived")):
                    raise RuleViolation(f"Refused: inventory count {count_id} was not found (or is archived).")
                # Live values: awaiting_reconciliation, reconciliation_success, reconciliation_done.
                if (fmt.text(count.get("reconcileStatus")) or "awaiting_reconciliation") != "awaiting_reconciliation":
                    raise RuleViolation(f"Refused: inventory count {count_id} is already reconciled "
                                        f"({count.get('reconcileStatus')}); start a new count instead.")
                already, _ = self.client.get_all("InventoryCountItem.json", {"inventoryCountID": str(int(count_id))})
                counted = {str(r.get("itemID")) for r in already}
                again = [i for i in ids if i in counted]
                if again:
                    raise RuleViolation(f"Refused: item(s) {', '.join(again)} are already in count {count_id}; another "
                                        "entry would be added to the first.")
                count_ref, count_text = str(int(count_id)), f"inventory count {count_id}"
            else:
                count_name = rules.check_record_name(name, "inventory count")
                requests.append(PlannedRequest(f"create inventory count '{count_name}'", "POST", "InventoryCount.json",
                                               {"name": count_name, "shopID": self._single_shop()}, creates="count"))
                count_ref, count_text = new_ref("count"), f"the new inventory count '{count_name}'"
            found = self._items_by_id(ids)
            lines = []
            for entry, item_id in zip(items, ids):
                item = found.get(item_id)
                if item is None:
                    raise RuleViolation(f"Refused: item {item_id} was not found.")
                if fmt.flag(item.get("archived")):
                    raise RuleViolation(f"Refused: {item_label(item)} is archived.")
                try:
                    qty = int(entry.get("counted"))
                except (TypeError, ValueError):
                    raise RuleViolation(f"Refused: the count for {item_label(item)} is not a whole number.") from None
                if qty < 0 or qty != float(entry.get("counted")):
                    raise RuleViolation(f"Refused: the count for {item_label(item)} must be a whole number of 0 or more.")
                qoh = fmt.total_qoh(item) or 0
                lines.append(f"{item_label(item)}: on hand {qoh}, counted {qty} ({qty - qoh:+d})")
                requests.append(PlannedRequest(f"count {qty} x {item_label(item)} (on hand now {qoh})", "POST",
                                               "InventoryCountItem.json",
                                               {"inventoryCountID": count_ref, "itemID": item_id, "qty": str(qty),
                                                "employeeID": COUNT_EMPLOYEE_ID}))
            change = sum(int(r.body["qty"]) for r in requests if r.path == "InventoryCountItem.json") - sum(
                fmt.total_qoh(found[i]) or 0 for i in ids)
            summary = (f"Record {len(ids)} counted item(s) on {count_text} (employee {COUNT_EMPLOYEE_ID}). If reconciled "
                       f"as is, stock would change by {change:+d} unit(s) in total. Nothing changes stock now.")
            after = ("Stock does NOT change until the count is reconciled. In Lightspeed, open the count, check the "
                     "differences, and reconcile it there. Items not in the count are not adjusted. Reconciling can't "
                     "be undone except by adjusting each item.")
            return Plan("create_inventory_count", summary, requests, lines, after)

        return self._run("create_inventory_count", dry_run, confirm_code, build)
