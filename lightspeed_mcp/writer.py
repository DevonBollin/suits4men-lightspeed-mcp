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
from dataclasses import dataclass, field

from . import format as fmt
from . import rules
from .errors import ApiError, LightspeedError, WriteRefused
from .reader import InventoryReader, batches, id_filter, relations
from .rules import RuleViolation
from .write_path import WriteClient

MAX_REQUESTS_PER_CALL = 100  # variants or PO lines in one call
MAX_LINE_QUANTITY = 1000  # a sanity limit for one PO line


# ---- the plan: exactly what would be sent ---------------------------------------------------


@dataclass
class PlannedRequest:
    label: str  # what this one request does, in a few words
    method: str
    path: str  # relative to the account, e.g. "Item.json"
    body: dict | None  # None for a DELETE
    before: dict | None = None  # the record as it is now, saved in the write log (for archiving)


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


def default_price(amount: str) -> dict:
    """The Prices block for a Default price only. MSRP and Online are never sent."""
    return {"ItemPrice": [{"amount": amount, "useTypeID": "1", "useType": "Default"}]}


def item_label(item: dict) -> str:
    sku = fmt.text(item.get("customSku"))
    return f"item {item.get('itemID')} '{fmt.text(item.get('description'))}'" + (f" (SKU {sku})" if sku else " (no SKU)")


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
        if len(plan.requests) > MAX_REQUESTS_PER_CALL:
            raise RuleViolation(f"Refused: {len(plan.requests)} requests in one call; the limit is {MAX_REQUESTS_PER_CALL}.")
        for r in plan.requests:
            self.writes.check(tool, r.method, r.path, r.body)
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
        for index, r in enumerate(plan.requests):
            try:
                sent = self.writes.send(plan.tool, r.method, r.path, r.body, code, before=r.before)
            except LightspeedError as e:
                return fmt.prune({
                    "dry_run": False,
                    "finished": False,
                    "summary": plan.summary,
                    "done": done,
                    "failed": {"does": r.label, "method": r.method, "path": self.writes.full_path(r.path), "error": str(e)},
                    "not_sent": [p.label for p in plan.requests[index + 1:]],
                    "note": "Stopped at the first failure. Requests under 'done' were made in Lightspeed; nothing after "
                            "the failure was sent. Check Lightspeed before trying anything again.",
                    "write_log": str(self.writes.write_log.path),
                })
            done.append({"does": r.label, "status": sent["status"], "affected_ids": sent["affected_ids"]})
        return fmt.prune({"dry_run": False, "finished": True, "summary": plan.summary, "done": done,
                          "after": plan.after, "write_log": str(self.writes.write_log.path)})

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

    def _items_by_id(self, item_ids: list[str]) -> dict[str, dict]:
        items: dict[str, dict] = {}
        for batch in batches(sorted(set(item_ids), key=int)):
            rows, _ = self.client.get_all("Item.json", {"itemID": id_filter(batch), "archived": "true",
                                                        "load_relations": relations("ItemShops", "ItemAttributes")})
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
                description = " ".join(p for p in (matrix_desc, color, size, fit) if p)
                attributes = {"itemAttributeSetID": str(rules.STORE_ATTRIBUTE_SET_ID), "attribute1": color, "attribute2": size}
                if fit:
                    attributes["attribute3"] = fit
                body = {
                    "description": description,
                    "itemMatrixID": str(int(matrix_id)),
                    "itemType": fmt.text(matrix.get("itemType")) or "default",
                    "customSku": sku,
                    "defaultCost": cost_text,
                    "ItemAttributes": attributes,
                    "Prices": default_price(price_text),
                }
                # Category, vendor, manufacturer and tax come from the matrix.
                for key in ("categoryID", "manufacturerID", "defaultVendorID", "taxClassID"):
                    if fmt.ref_id(matrix.get(key)):
                        body[key] = str(matrix.get(key))
                if matrix.get("tax") not in (None, ""):
                    body["tax"] = str(matrix.get("tax"))
                requests.append(PlannedRequest(f"create '{description}' (SKU {sku})", "POST", "Item.json", body))

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

            # One pass over every item (archived too): who holds which SKU, and the highest in use.
            everything, _ = self.client.get_all("Item.json", {"archived": "true"})
            by_id = {str(i.get("itemID")): i for i in everything}
            holders: dict[str, dict] = {}
            for i in everything:
                if fmt.text(i.get("customSku")):
                    holders.setdefault(fmt.text(i.get("customSku")), i)
            six_digit = sorted(s for s in holders if rules.CUSTOM_SKU_PATTERN.match(s))
            highest = six_digit[-1] if six_digit else None
            next_free = int(highest) + 1 if highest else 1

            given = [rules.check_custom_sku_format(entry["custom_sku"]) for entry in items if entry.get("custom_sku")]
            rules.check_skus_unique_in_request(given)
            for sku in given:
                if sku in holders:
                    raise RuleViolation(f"Refused: custom SKU {sku} is already used by {item_label(holders[sku])}.")
            reserved = set(given)

            requests, picked = [], 0
            for entry, item_id in zip(items, ids):
                item = by_id.get(item_id)
                if item is None:
                    raise RuleViolation(f"Refused: item {item_id} was not found.")
                if fmt.flag(item.get("archived")):
                    raise RuleViolation(f"Refused: {item_label(item)} is archived.")
                if fmt.text(item.get("customSku")):
                    raise RuleViolation(f"Refused: {item_label(item)} already has a SKU. This tool only fills in "
                                        "missing ones; update_item_fields changes an existing SKU.")
                if entry.get("custom_sku"):
                    sku = rules.clean(entry["custom_sku"])
                else:  # the next number nobody holds or is about to get
                    while f"{next_free:06d}" in holders or f"{next_free:06d}" in reserved:
                        next_free += 1
                    if next_free > 999_999:
                        raise RuleViolation("Refused: the 6-digit SKU range is used up.")
                    sku = f"{next_free:06d}"
                    reserved.add(sku)
                    picked += 1
                requests.append(PlannedRequest(f"{item_label(item)} -> SKU {sku}", "PUT", f"Item/{item_id}.json",
                                               {"customSku": sku}))

            summary = (f"Give {len(requests)} item(s) their own custom SKU"
                       + (f" ({picked} picked automatically, continuing after the highest in use, {highest})"
                          if picked else "")
                       + ". Only customSku is sent: nothing else on the items changes. Each SKU is checked against "
                         "every item, archived ones included.")
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
