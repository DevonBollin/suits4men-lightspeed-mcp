"""Matching an invoice's or packing slip's lines to the store's items. GET only.

Claude reads the document and passes each line as a dict:

    quantity, unit_cost            what arrived and what it cost (required)
    item_id                        if the item is already known
    vendor_number                  the vendor's style / item number printed on the invoice
    matrix_id, color, size, fit    a variant of a Color/Size/Fit matrix
    description                    the invoice's own text (used to suggest candidates)
    price, custom_sku              for a variant that doesn't exist yet (its selling price; SKU optional)
    new_item                       {description, category_id, price, custom_sku?, manufacturer_id?}
                                   for an item outside any matrix that doesn't exist yet

Each line is resolved, in this order, to:
  * "existing": an active item, by item_id; or by vendor_number (the store's saved vendor
    item numbers for this vendor) plus color/size/fit when the number covers several items;
    or by matrix_id + color/size/fit;
  * "new_variant": matrix_id + color/size/fit that the matrix doesn't have yet (needs price);
  * "new_item": a new item outside any matrix (needs new_item);
  * "unresolved": none of the above; the reason says what is missing, with candidates.

match() never refuses: it reports. The intake tool (writer.py) refuses while any line is unresolved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import format as fmt
from .reader import InventoryReader, relations
from .rules import clean


@dataclass
class LineMatch:
    number: int  # 1-based position on the invoice
    line: dict  # the line as given
    status: str  # existing / new_variant / new_item / unresolved
    item: dict | None = None  # the existing item
    matrix: dict | None = None  # for new_variant
    values: tuple[str, str, str] | None = None  # by slot: attribute1-3 (for set 5: color, size, fit)
    slots: dict[int, str] = field(default_factory=dict)  # the matrix's attribute names, e.g. {1: "Color", ...}
    reason: str = ""
    candidates: list[dict] = field(default_factory=list)
    vendor_number_known: bool = False  # the item already has this vendor number saved


LEGACY_FIELDS = {1: "color", 2: "size", 3: "fit"}  # set 5's names; for any set they mean slots 1, 2, 3


def resolve_values(entry: dict, slots: dict[int, str]) -> tuple[tuple[str, str, str], list[str], list[str]]:
    """An entry's attribute values by slot (1-3) for a set with these slots ({1: "Color", ...}).

    Values come from entry["values"] by attribute name, ignoring case ({"Neck": "15.5"}), or from
    color / size / fit for slots 1 / 2 / 3. Returns (values, required names left empty, names or
    fields that don't fit the set). Every named slot is required except one called "Fit".
    Values are trimmed, never re-cased (vendor naming).
    """
    by_name = {str(k).strip().lower(): clean(v) for k, v in (entry.get("values") or {}).items()}
    known = {name.lower() for name in slots.values()}
    unexpected = [k for k in by_name if k not in known]
    values, missing = [], []
    for i in (1, 2, 3):
        name = slots.get(i)
        legacy = clean(entry.get(LEGACY_FIELDS[i]))
        value = (by_name.get(name.lower()) if name else "") or legacy
        if not name:
            if value:
                unexpected.append(LEGACY_FIELDS[i])
            value = ""
        elif not value and name.lower() != "fit":
            missing.append(name)
        values.append(value)
    return (values[0], values[1], values[2]), missing, unexpected


def vendor_numbers(item: dict) -> list[dict]:
    return fmt.as_list((item.get("ItemVendorNums") or {}).get("ItemVendorNum"))


def attribute_values(item: dict) -> tuple[str, str, str]:
    attrs = item.get("ItemAttributes") or {}
    return tuple((fmt.text(attrs.get(f"attribute{i}")) or "") for i in (1, 2, 3))


def item_summary(item: dict) -> dict:
    return fmt.prune({"item_id": fmt.integer(item.get("itemID")), "description": fmt.text(item.get("description")),
                      "custom_sku": fmt.text(item.get("customSku")), "matrix_id": fmt.ref_id(item.get("itemMatrixID")),
                      "qoh": fmt.total_qoh(item)})


class InvoiceMatcher:
    """Loads every item once (with vendor numbers and attributes) and matches lines against them."""

    def __init__(self, reader: InventoryReader):
        self.reader = reader
        items, _ = reader.client.get_all("Item.json", {
            "archived": "true", "load_relations": relations("ItemVendorNums", "ItemAttributes", "ItemShops")})
        self.items = items
        self.by_id = {str(i.get("itemID")): i for i in items}
        self._matrices: dict[str, dict | None] = {}

    def matrix(self, matrix_id) -> dict | None:
        key = str(int(matrix_id))
        if key not in self._matrices:
            self._matrices[key] = self.reader._matrix_record(int(matrix_id))
        return self._matrices[key]

    def children(self, matrix_id) -> list[dict]:
        return [i for i in self.items if str(i.get("itemMatrixID")) == str(int(matrix_id))]

    def slots(self, attribute_set_id) -> dict[int, str]:
        """A set's attribute names by slot, e.g. {1: "Color", 2: "Size", 3: "Fit"}."""
        return fmt.attribute_slots(self.reader.attribute_sets().get(str(attribute_set_id)))

    def with_vendor_number(self, vendor_id, number: str) -> list[dict]:
        number = number.lower()
        return [i for i in self.items if not fmt.flag(i.get("archived")) and any(
            str(v.get("vendorID")) == str(vendor_id) and (fmt.text(v.get("value")) or "").lower() == number
            for v in vendor_numbers(i))]

    def candidates(self, text: str, limit: int = 5) -> list[dict]:
        """Active items whose description shares the most words with `text`."""
        words = {w for w in re.findall(r"[a-z0-9.]+", text.lower()) if len(w) > 1}
        if not words:
            return []
        scored = []
        for i in self.items:
            if fmt.flag(i.get("archived")):
                continue
            desc_words = set(re.findall(r"[a-z0-9.]+", (fmt.text(i.get("description")) or "").lower()))
            score = len(words & desc_words)
            if score:
                scored.append((score, i))
        scored.sort(key=lambda s: (-s[0], fmt.text(s[1].get("description")) or ""))
        return [item_summary(i) for _, i in scored[:limit]]

    def match(self, vendor_id, lines: list[dict]) -> list[LineMatch]:
        return [self._match_one(vendor_id, n, line) for n, line in enumerate(lines, 1)]

    def _match_one(self, vendor_id, number: int, line: dict) -> LineMatch:
        given = any(clean(line.get(k)) for k in LEGACY_FIELDS.values()) or bool(line.get("values"))
        vendor_number = clean(line.get("vendor_number"))

        def found(item: dict) -> LineMatch:
            if fmt.flag(item.get("archived")):
                return LineMatch(number, line, "unresolved", item=item,
                                 reason=f"Item {item.get('itemID')} '{fmt.text(item.get('description'))}' is archived.")
            known = bool(vendor_number) and any(
                str(v.get("vendorID")) == str(vendor_id) and (fmt.text(v.get("value")) or "").lower() == vendor_number.lower()
                for v in vendor_numbers(item))
            return LineMatch(number, line, "existing", item=item, vendor_number_known=known)

        if line.get("item_id") is not None:
            item = self.by_id.get(str(int(line["item_id"])))
            if item is None:
                return LineMatch(number, line, "unresolved", reason=f"Item {line['item_id']} was not found.")
            return found(item)

        spans: list[int] = []  # the matrices a shared vendor number is used in, when more than one
        if vendor_number:
            by_number = self.with_vendor_number(vendor_id, vendor_number)
            if len(by_number) == 1 and not given:
                return found(by_number[0])
            if by_number and given:
                set_id = (by_number[0].get("ItemAttributes") or {}).get("itemAttributeSetID")
                values, missing, _ = resolve_values(line, self.slots(set_id))
                same = [i for i in by_number if tuple(v.lower() for v in attribute_values(i)) ==
                        tuple(v.lower() for v in values)]
                if len(same) == 1 and not missing:
                    return found(same[0])
                if line.get("matrix_id") is None:
                    matrix_ids = {fmt.ref_id(i.get("itemMatrixID")) for i in by_number} - {None}
                    if len(matrix_ids) == 1:  # the number belongs to one matrix: look for the variant there
                        line = {**line, "matrix_id": matrix_ids.pop()}
                    else:  # one fabric code on suits, vests, pants...: the garment type decides
                        spans = sorted(matrix_ids)

        if line.get("matrix_id") is not None:
            matrix = self.matrix(line["matrix_id"])
            if matrix is None:
                return LineMatch(number, line, "unresolved", reason=f"Matrix {line['matrix_id']} was not found.")
            slots = self.slots(matrix.get("itemAttributeSetID"))
            values, missing, unexpected = resolve_values(line, slots)
            names = " / ".join(slots.values()) or "no attributes"
            if missing or unexpected:
                return LineMatch(number, line, "unresolved", matrix=matrix, slots=slots,
                                 reason=f"Matrix {matrix.get('itemMatrixID')} is {names}: "
                                        + (f"give {', '.join(missing)}. " if missing else "")
                                        + (f"It has no {', '.join(unexpected)}." if unexpected else ""))
            wanted = tuple(v.lower() for v in values)
            for child in self.children(line["matrix_id"]):
                if tuple(v.lower() for v in attribute_values(child)) == wanted:
                    return found(child)
            return LineMatch(number, line, "new_variant", matrix=matrix, values=values, slots=slots)

        if line.get("new_item"):
            return LineMatch(number, line, "new_item")

        given_values = [clean(v) for v in (line.get("values") or {}).values()]
        text = " ".join(p for p in (clean(line.get("description")), vendor_number,
                                    *(clean(line.get(k)) for k in LEGACY_FIELDS.values()), *given_values) if p)
        if len(spans) > 1:
            names = ", ".join(f"{mid} '{fmt.text((self.matrix(mid) or {}).get('description'))}'" for mid in spans)
            return LineMatch(number, line, "unresolved", candidates=self.candidates(text),
                             reason=f"Vendor number {vendor_number} is used in matrices {names}: give the matrix_id "
                                    "for this line's garment type, with its attribute values.")
        return LineMatch(number, line, "unresolved", candidates=self.candidates(text),
                         reason="Not matched: give item_id, a matrix_id with its attribute values (e.g. "
                                "color/size/fit), or new_item. Candidates by description are listed.")


def match_report(matches: list[LineMatch]) -> dict:
    """For the match_invoice_lines tool: one row per invoice line."""
    rows = []
    for m in matches:
        row = {"line": m.number, "status": m.status, "quantity": m.line.get("quantity"),
               "unit_cost": m.line.get("unit_cost"), "invoice_text": m.line.get("description"),
               "vendor_number": m.line.get("vendor_number")}
        if m.item:
            row["item"] = item_summary(m.item)
            if m.line.get("vendor_number") and m.status == "existing":
                row["vendor_number_saved"] = m.vendor_number_known
        if m.matrix:
            row["matrix"] = {"matrix_id": fmt.integer(m.matrix.get("itemMatrixID")),
                             "description": fmt.text(m.matrix.get("description"))}
        if m.values:
            row["attributes"] = {m.slots.get(i, f"attribute{i}"): v for i, v in enumerate(m.values, 1) if v}
        if m.reason:
            row["reason"] = m.reason
        if m.candidates:
            row["candidates"] = m.candidates
        rows.append(fmt.prune(row))
    counts: dict[str, int] = {}
    for m in matches:
        counts[m.status] = counts.get(m.status, 0) + 1
    return {"lines": len(matches), "counts": counts, "matches": rows}
