"""Trim Lightspeed's verbose JSON down to small, readable dicts.

Lightspeed sends almost every value as a string ("true", "12.50", "0"), uses
"0" to mean "no ID", returns one record as an object but several as a list,
and includes many fields that do not matter for inventory work. These helpers
keep the fields that matter, convert types, and always include IDs.

Field shapes marked "verify" are from the docs and are checked against the
live account (see NOTES.md).
"""

from __future__ import annotations

from collections import Counter

from .client import as_list

# ---- value converters -------------------------------------------------------


def text(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def number(value) -> float | None:
    try:
        return round(float(value), 4) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def integer(value) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def ref_id(value) -> int | None:
    """A foreign key. Lightspeed uses 0 for "none"."""
    n = integer(value)
    return n or None


def flag(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    return str(value).lower() == "true"


def date(value) -> str | None:
    """Keep timestamps as Lightspeed's ISO strings; drop empty ones."""
    value = text(value)
    if not value or value.startswith("1970-01-01"):
        return None
    return value


def prune(record: dict, keep: tuple[str, ...] = ()) -> dict:
    """Drop None and empty values, except keys listed in `keep` (shown as null on purpose)."""
    return {k: v for k, v in record.items() if k in keep or v not in (None, "", [], {})}


# ---- items ---------------------------------------------------------------------

# Always shown (as null when blank), so a missing SKU is visible.
SKU_FIELDS = ("custom_sku", "upc", "manufacturer_sku")


def prices(item: dict) -> dict:
    """{"Default": 299.99, "MSRP": 349.0, ...} from Prices.ItemPrice."""
    result = {}
    for price in as_list((item.get("Prices") or {}).get("ItemPrice")):
        label = text(price.get("useType")) or f"useType{price.get('useTypeID')}"
        amount = number(price.get("amount"))
        if amount is not None:
            result[label] = amount
    return result


def stock(item: dict, shop_names: dict[str, str] | None = None) -> list[dict]:
    """One row per real shop.

    Lightspeed also sends a shopID 0 row: the all-shops total. On this
    one-store account it always equals shop 1, so it is left out here and
    reported once as the item's qoh and average_cost instead.
    """
    rows = []
    for shop in as_list((item.get("ItemShops") or {}).get("ItemShop")):
        shop_id = str(shop.get("shopID"))
        if shop_id == "0":
            continue
        rows.append(prune({
            "shop_id": integer(shop_id),
            "shop": (shop_names or {}).get(shop_id),
            "qoh": integer(shop.get("qoh")),
            "sellable": integer(shop.get("sellable")),
            "backorder": integer(shop.get("backorder")),
            "reorder_point": integer(shop.get("reorderPoint")),
            "reorder_level": integer(shop.get("reorderLevel")),
            "item_shop_id": integer(shop.get("itemShopID")),  # needed to change reorder levels later
        }, keep=("qoh",)))
    return rows


def average_cost(item: dict) -> float | None:
    """Weighted average cost. Only the shopID 0 (total) row carries it; per-shop rows show 0."""
    shops = as_list((item.get("ItemShops") or {}).get("ItemShop"))
    for shop in shops:
        if str(shop.get("shopID")) == "0":
            return number(shop.get("averageCost"))
    return number(shops[0].get("averageCost")) if shops else None


def total_qoh(item: dict) -> int | None:
    """Quantity on hand across shops: the shopID 0 total row if present, else the sum."""
    shops = as_list((item.get("ItemShops") or {}).get("ItemShop"))
    if not shops:
        return None
    for shop in shops:
        if str(shop.get("shopID")) == "0":
            return integer(shop.get("qoh"))
    return sum(integer(s.get("qoh")) or 0 for s in shops)


def attribute_slots(attribute_set: dict | None) -> dict[int, str]:
    """{1: "Color", 2: "Size", 3: "Fit"}, keeping positions.

    Positions matter: the built-in "Size" set (ID 2) has a blank attributeName1
    and puts Size in attributeName2, so its items carry the size in attribute2.
    """
    if not attribute_set:
        return {}
    slots = {i: text(attribute_set.get(f"attributeName{i}")) for i in (1, 2, 3)}
    return {i: name for i, name in slots.items() if name}


def attribute_names(attribute_set: dict | None) -> list[str]:
    """["Color", "Size", "Fit"] from attributeName1..3 (blank names skipped)."""
    return list(attribute_slots(attribute_set).values())


def attribute_values(item: dict, attribute_sets: dict[str, dict]) -> dict | None:
    """{"Color": "Navy", "Size": "40R", "Fit": "Slim"} for an item in a matrix."""
    attrs = item.get("ItemAttributes")
    if not isinstance(attrs, dict):
        return None
    set_id = str(attrs.get("itemAttributeSetID") or "")
    attr_set = attrs.get("ItemAttributeSet") or attribute_sets.get(set_id)
    values = {}
    for i in (1, 2, 3):
        value = text(attrs.get(f"attribute{i}"))
        if value is None:
            continue
        name = text(attr_set.get(f"attributeName{i}")) if attr_set else None
        values[name or f"attribute{i}"] = value
    return values or None


def item_summary(item: dict, attribute_sets: dict[str, dict], vendor_names: dict[str, str]) -> dict:
    """One line per item: for search results and matrix child lists."""
    vendor_id = ref_id(item.get("defaultVendorID"))
    return prune({
        "item_id": integer(item.get("itemID")),
        "description": text(item.get("description")),
        "custom_sku": text(item.get("customSku")),
        "upc": text(item.get("upc")),
        "manufacturer_sku": text(item.get("manufacturerSku")),
        "system_sku": text(item.get("systemSku")),
        "matrix_id": ref_id(item.get("itemMatrixID")),
        "attributes": attribute_values(item, attribute_sets),
        "vendor": vendor_names.get(str(vendor_id)) if vendor_id else None,
        "vendor_id": vendor_id,
        "category_id": ref_id(item.get("categoryID")),
        "qoh": total_qoh(item),
        "price": prices(item).get("Default"),
        "default_cost": number(item.get("defaultCost")),
        "archived": True if flag(item.get("archived")) else None,
    }, keep=SKU_FIELDS)


def item_detail(
    item: dict,
    attribute_sets: dict[str, dict],
    vendor_names: dict[str, str],
    shop_names: dict[str, str],
    matrix: dict | None,
) -> dict:
    """Everything needed to judge one item: identity, matrix, attributes, vendor, cost, stock."""
    vendor_id = ref_id(item.get("defaultVendorID"))
    category = item.get("Category") if isinstance(item.get("Category"), dict) else None
    manufacturer = item.get("Manufacturer") if isinstance(item.get("Manufacturer"), dict) else None
    attrs = item.get("ItemAttributes") if isinstance(item.get("ItemAttributes"), dict) else {}
    set_id = ref_id(attrs.get("itemAttributeSetID"))
    vendor_numbers = [
        prune({"vendor_id": ref_id(v.get("vendorID")), "vendor": vendor_names.get(str(v.get("vendorID"))),
               "number": text(v.get("value")), "cost": number(v.get("cost"))})
        for v in as_list((item.get("ItemVendorNums") or {}).get("ItemVendorNum"))
    ]
    return prune({
        "item_id": integer(item.get("itemID")),
        "description": text(item.get("description")),
        "custom_sku": text(item.get("customSku")),
        "upc": text(item.get("upc")),
        "ean": text(item.get("ean")),
        "manufacturer_sku": text(item.get("manufacturerSku")),
        "system_sku": text(item.get("systemSku")),
        "item_type": text(item.get("itemType")),
        "archived": flag(item.get("archived")),
        "matrix": matrix,
        "attribute_set": prune({
            "attribute_set_id": set_id,
            "name": text((attribute_sets.get(str(set_id)) or {}).get("name")) if set_id else None,
        }),
        "attributes": attribute_values(item, attribute_sets),
        "vendor": prune({"vendor_id": vendor_id, "name": vendor_names.get(str(vendor_id))}) if vendor_id else None,
        "vendor_numbers": vendor_numbers,
        "category": prune({
            "category_id": ref_id(item.get("categoryID")),
            "path": text(category.get("fullPathName")) if category else None,
        }),
        "manufacturer": prune({
            "manufacturer_id": ref_id(item.get("manufacturerID")),
            "name": text(manufacturer.get("name")) if manufacturer else None,
        }),
        "qoh": total_qoh(item),
        "default_cost": number(item.get("defaultCost")),
        "average_cost": average_cost(item),
        "prices": prices(item),
        "stock": stock(item, shop_names),
        "note": text((item.get("Note") or {}).get("note")) if isinstance(item.get("Note"), dict) else None,
        "created": date(item.get("createTime")),
        "updated": date(item.get("timeStamp")),
    }, keep=SKU_FIELDS)


# ---- matrices and attribute sets -------------------------------------------------


def matrix_attribute_values(matrix: dict, index: int) -> list[str]:
    """The values offered by matrix attribute `index` (attribute1Values etc.; shape verify)."""
    raw = matrix.get(f"attribute{index}Values")
    if isinstance(raw, dict):  # e.g. {"attribute1Value": [...]}
        raw = next(iter(raw.values()), None)
    return [v for v in (text(x) for x in as_list(raw)) if v]


def matrix_summary(matrix: dict, attribute_sets: dict[str, dict], vendor_names: dict[str, str]) -> dict:
    """From the ItemMatrix record itself (needs a scope that can read ItemMatrix; see NOTES.md)."""
    set_id = ref_id(matrix.get("itemAttributeSetID"))
    attr_set = matrix.get("ItemAttributeSet") if isinstance(matrix.get("ItemAttributeSet"), dict) else None
    attr_set = attr_set or attribute_sets.get(str(set_id))
    slots = attribute_slots(attr_set)
    category = matrix.get("Category") if isinstance(matrix.get("Category"), dict) else None
    vendor_id = ref_id(matrix.get("defaultVendorID"))
    return prune({
        "matrix_id": integer(matrix.get("itemMatrixID")),
        "description": text(matrix.get("description")),
        "attribute_set": prune({
            "attribute_set_id": set_id,
            "name": text(attr_set.get("name")) if attr_set else None,
            "attributes": list(slots.values()),
        }),
        "attribute_values": prune({
            slots.get(i, f"attribute{i}"): matrix_attribute_values(matrix, i) for i in (1, 2, 3)
        }),
        "vendor": prune({"vendor_id": vendor_id, "name": vendor_names.get(str(vendor_id))}) if vendor_id else None,
        "category": prune({
            "category_id": ref_id(matrix.get("categoryID")),
            "path": text(category.get("fullPathName")) if category else None,
        }),
        "manufacturer_id": ref_id(matrix.get("manufacturerID")),
        "default_cost": number(matrix.get("defaultCost")),
        "prices": prices(matrix),
        "item_type": text(matrix.get("itemType")),
        "archived": flag(matrix.get("archived")),
        "updated": date(matrix.get("timeStamp")),
    })


def _most_common(values) -> int | None:
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else None


def matrix_from_children(matrix_id: int, children: list[dict], attribute_sets: dict[str, dict],
                         vendor_names: dict[str, str]) -> dict:
    """What can be said about a matrix from its child items alone.

    Used when the ItemMatrix record itself is not readable (HTTP 403 with
    employee:inventory_read). The set, vendor and category are the ones most
    children use; the matrix's own description is unknown.
    """
    set_id = _most_common(ref_id((c.get("ItemAttributes") or {}).get("itemAttributeSetID")) for c in children)
    attr_set = attribute_sets.get(str(set_id))
    slots = attribute_slots(attr_set)
    vendor_id = _most_common(ref_id(c.get("defaultVendorID")) for c in children)
    values = {}
    for i in (1, 2, 3):
        seen = {text((c.get("ItemAttributes") or {}).get(f"attribute{i}")) for c in children} - {None}
        if seen:
            values[slots.get(i, f"attribute{i}")] = sorted(seen)
    return prune({
        "matrix_id": matrix_id,
        "attribute_set": prune({
            "attribute_set_id": set_id,
            "name": text(attr_set.get("name")) if attr_set else None,
            "attributes": list(slots.values()),
        }),
        "attribute_values": values,
        "vendor": prune({"vendor_id": vendor_id, "name": vendor_names.get(str(vendor_id))}) if vendor_id else None,
        "category_id": _most_common(ref_id(c.get("categoryID")) for c in children),
        "manufacturer_id": _most_common(ref_id(c.get("manufacturerID")) for c in children),
    })


def matrix_child_table(children: list[dict], attribute_sets: dict[str, dict], include_archived: bool) -> dict:
    """Child items as a compact table: {"columns": [...], "rows": [[...], ...]}.

    A matrix can have hundreds of children (the Caravelli suit matrix has 399),
    so one row of values per item is far smaller than one object per item.
    """
    set_ids = [ref_id((c.get("ItemAttributes") or {}).get("itemAttributeSetID")) for c in children]
    slots = attribute_slots(attribute_sets.get(str(_most_common(set_ids))))
    attr_columns = [(i, slots.get(i, f"attribute{i}")) for i in (1, 2, 3)
                    if any(text((c.get("ItemAttributes") or {}).get(f"attribute{i}")) for c in children)]
    columns = ["item_id", "custom_sku", "upc", "manufacturer_sku", *[name for _, name in attr_columns],
               "qoh", "price", "default_cost"]
    if include_archived:
        columns.append("archived")
    rows = []
    for c in children:
        attrs = c.get("ItemAttributes") or {}
        row = [integer(c.get("itemID")), text(c.get("customSku")), text(c.get("upc")), text(c.get("manufacturerSku")),
               *[text(attrs.get(f"attribute{i}")) for i, _ in attr_columns],
               total_qoh(c), prices(c).get("Default"), number(c.get("defaultCost"))]
        if include_archived:
            row.append(bool(flag(c.get("archived"))))
        rows.append(row)
    return {"columns": columns, "rows": rows}


def attribute_set_summary(attr_set: dict) -> dict:
    return prune({
        "attribute_set_id": integer(attr_set.get("itemAttributeSetID")),
        "name": text(attr_set.get("name")),
        "attributes": attribute_names(attr_set),
        "system": flag(attr_set.get("system")),
        "archived": True if flag(attr_set.get("archived")) else None,
    })


# ---- vendors and categories --------------------------------------------------------


def vendor_summary(vendor: dict) -> dict:
    return prune({
        "vendor_id": integer(vendor.get("vendorID")),
        "name": text(vendor.get("name")),
        "account_number": text(vendor.get("accountNumber")),
        "archived": True if flag(vendor.get("archived")) else None,
    })


def category_summary(category: dict) -> dict:
    return prune({
        "category_id": integer(category.get("categoryID")),
        "name": text(category.get("name")),
        "path": text(category.get("fullPathName")),
        "parent_id": ref_id(category.get("parentID")),
        "depth": integer(category.get("nodeDepth")),
        "archived": True if flag(category.get("archived")) else None,
    })


# ---- purchase orders ---------------------------------------------------------------


def order_status(order: dict) -> str:
    """Our reading of where a PO is (verify against what the Lightspeed screen shows)."""
    if flag(order.get("complete")):
        return "finished"
    if date(order.get("receivedDate")):
        return "checking_in"
    if date(order.get("orderedDate")):
        return "ordered"
    return "open"


def order_summary(order: dict, vendor_names: dict[str, str]) -> dict:
    vendor_id = ref_id(order.get("vendorID"))
    return prune({
        "order_id": integer(order.get("orderID")),
        "status": order_status(order),
        "vendor": vendor_names.get(str(vendor_id)) if vendor_id else None,
        "vendor_id": vendor_id,
        "ref_num": text(order.get("refNum")),
        "ordered": date(order.get("orderedDate")),
        "arrival": date(order.get("arrivalDate")),
        "received": date(order.get("receivedDate")),
        "total_quantity": integer(order.get("totalQuantity")),
        # subTotalCost and totalCost are not in the docs' field list but are in real responses.
        "subtotal_cost": number(order.get("subTotalCost")),
        "ship_cost": number(order.get("shipCost")),
        "other_cost": number(order.get("otherCost")),
        "total_cost": number(order.get("totalCost")),
        "shop_id": ref_id(order.get("shopID")),
        "created": date(order.get("createTime")),
        "archived": True if flag(order.get("archived")) else None,
    })


def order_shipment(shipment: dict) -> dict:
    """One receipt against a PO (the "Shipment History" tab in Lightspeed)."""
    return prune({
        "shipment_id": integer(shipment.get("orderShipmentID")),
        "sequence": integer(shipment.get("sequenceNumber")),
        "status": text(shipment.get("status")),
        "received": date(shipment.get("receptionDate")) or date(shipment.get("createTime")),
        "quantity_received": integer(shipment.get("totalQtyReceived")),
        "total_cost": number(shipment.get("totalCost")),
        "total_vendor_cost": number(shipment.get("totalVendorCost")),
        "packing_ref": text(shipment.get("shipmentPackingRefNum")),
        "payment_due": date(shipment.get("paymentDueDate")),
    })


def order_line(line: dict, item: dict | None) -> dict:
    """`item` is fetched separately: OrderLine refuses load_relations=["Item"] (HTTP 400)."""
    item = item or {}
    return prune({
        "order_line_id": integer(line.get("orderLineID")),
        "item_id": ref_id(line.get("itemID")),
        "description": text(item.get("description")),
        "custom_sku": text(item.get("customSku")),
        "upc": text(item.get("upc")),
        "manufacturer_sku": text(item.get("manufacturerSku")),
        "matrix_id": ref_id(item.get("itemMatrixID")),
        "quantity": integer(line.get("quantity")),
        "checked_in": integer(line.get("checkedIn")),
        "received_not_checked_in": integer(line.get("numReceived")),
        "unit_cost": number(line.get("price")),
        "original_unit_cost": number(line.get("originalPrice")),
        "vendor_unit_cost": number(line.get("vendorCost")),
        "total": number(line.get("total")),
    })


# ---- small item reference, used inside other records ---------------------------------


def item_ref(item: dict | None, attribute_sets: dict[str, dict] | None = None) -> dict:
    """Just enough to recognise an item: ID, description, custom SKU, matrix and attributes."""
    item = item or {}
    return prune({
        "item_id": integer(item.get("itemID")),
        "description": text(item.get("description")),
        "custom_sku": text(item.get("customSku")),
        "matrix_id": ref_id(item.get("itemMatrixID")),
        "attributes": attribute_values(item, attribute_sets or {}),
    })


# ---- matrix and manufacturer lists ---------------------------------------------------


def matrix_list_entry(matrix: dict, attribute_sets: dict[str, dict], vendor_names: dict[str, str],
                      category_paths: dict[str, str]) -> dict:
    set_id = ref_id(matrix.get("itemAttributeSetID"))
    vendor_id = ref_id(matrix.get("defaultVendorID"))
    return prune({
        "matrix_id": integer(matrix.get("itemMatrixID")),
        "description": text(matrix.get("description")),
        "attribute_set": text((attribute_sets.get(str(set_id)) or {}).get("name")) if set_id else None,
        "attribute_set_id": set_id,
        "vendor": vendor_names.get(str(vendor_id)) if vendor_id else None,
        "vendor_id": vendor_id,
        "category": category_paths.get(str(ref_id(matrix.get("categoryID")))),
        "category_id": ref_id(matrix.get("categoryID")),
        "price": prices(matrix).get("Default"),
        "default_cost": number(matrix.get("defaultCost")),
        "archived": True if flag(matrix.get("archived")) else None,
    })


def manufacturer_summary(manufacturer: dict) -> dict:
    return prune({"manufacturer_id": integer(manufacturer.get("manufacturerID")), "name": text(manufacturer.get("name"))})


# ---- stock history (InventoryLog) ----------------------------------------------------


def inventory_log_entry(log: dict) -> dict:
    """One change to an item's quantity on hand, with what caused it."""
    return prune({
        "date": date(log.get("createTime")),
        "qoh_change": integer(log.get("qohChange")),
        "reason": text(log.get("reason")),
        "unit_cost": number(log.get("costChange")),  # docs: "the updated unit cost for the item"
        "caused_negative": True if flag(log.get("causedNegative")) else None,
        "automated": flag(log.get("automated")),
        "order_id": ref_id(log.get("orderID")),
        "sale_id": ref_id(log.get("saleID")),
        "inventory_count_id": ref_id(log.get("inventoryCountID")),
        "vendor_return_id": ref_id(log.get("vendorReturnID")),
        "transfer_id": ref_id(log.get("transferID")),
        "item_import_id": ref_id(log.get("itemImportID")),
        "employee_id": ref_id(log.get("employeeID")),
        "log_id": integer(log.get("inventoryLogID")),
    })


# ---- inventory counts ------------------------------------------------------------------


def inventory_count_summary(count: dict) -> dict:
    return prune({
        "count_id": integer(count.get("inventoryCountID")),
        "name": text(count.get("name")),
        "shop_id": ref_id(count.get("shopID")),
        "updated": date(count.get("timeStamp")),
        "archived": True if flag(count.get("archived")) else None,
    })


# ---- special orders -------------------------------------------------------------------


def special_order_summary(order: dict, attribute_sets: dict[str, dict]) -> dict:
    """A customer special order. The item is reached through its sale line."""
    sale_line = order.get("SaleLine") if isinstance(order.get("SaleLine"), dict) else {}
    item = sale_line.get("Item") if isinstance(sale_line.get("Item"), dict) else None
    return prune({
        "special_order_id": integer(order.get("specialOrderID")),
        "status": text(order.get("status")),  # e.g. "Not Ordered", "Ready For Pickup" (not in the field docs)
        "quantity": integer(order.get("unitQuantity")),
        "completed": flag(order.get("completed")),
        "customer_contacted": flag(order.get("contacted")),
        "customer_id": ref_id(order.get("customerID")),
        "item": item_ref(item, attribute_sets) if item else None,
        "item_id": None if item else ref_id(sale_line.get("itemID")),
        "unit_price": number(sale_line.get("unitPrice")),
        "sale_id": ref_id(sale_line.get("saleID")),
        "sale_line_id": ref_id(order.get("saleLineID")),
        "order_line_id": ref_id(order.get("orderLineID")),  # the PO line that orders it from the vendor
        "created": date(sale_line.get("createTime")),
        "updated": date(order.get("timeStamp")),
    })


# ---- vendor returns -------------------------------------------------------------------


def vendor_return_summary(vendor_return: dict, vendor_names: dict[str, str]) -> dict:
    vendor_id = ref_id(vendor_return.get("vendorID"))
    return prune({
        "vendor_return_id": integer(vendor_return.get("vendorReturnID")),
        "status": text(vendor_return.get("status")),
        "vendor": vendor_names.get(str(vendor_id)) if vendor_id else None,
        "vendor_id": vendor_id,
        "ref_num": text(vendor_return.get("refNum")),
        "sent": date(vendor_return.get("sentDate")),
        "subtotal": number(vendor_return.get("subtotal")),
        "ship_cost": number(vendor_return.get("shipCost")),
        "other_cost": number(vendor_return.get("otherCost")),
        "total": number(vendor_return.get("total")),
        "updated": date(vendor_return.get("timeStamp")),
        "archived": True if flag(vendor_return.get("archived")) else None,
    })


def vendor_return_line(line: dict, attribute_sets: dict[str, dict]) -> dict:
    item = line.get("Item") if isinstance(line.get("Item"), dict) else None
    reason = line.get("VendorReturnItemReason") if isinstance(line.get("VendorReturnItemReason"), dict) else {}
    return prune({
        "vendor_return_item_id": integer(line.get("vendorReturnItemID")),
        "item": item_ref(item, attribute_sets) if item else None,
        "item_id": None if item else ref_id(line.get("itemID")),
        "quantity": integer(line.get("quantity")),
        "unit_cost": number(line.get("cost")),
        "subtotal": number(line.get("subtotal")),
        "reason": text(reason.get("name")) or text(reason.get("reason")),
        "from_order_id": ref_id(line.get("orderID")),
    })


# ---- vendor catalogs -------------------------------------------------------------------


def catalog_item(item: dict) -> dict:
    """An item in a vendor catalog (not yet in this store's inventory)."""
    return prune({
        "catalog_item_id": integer(item.get("catalogVendorItemID")),
        "catalog_vendor_id": ref_id(item.get("catalogVendorID")),
        "brand": text(item.get("brand")),
        "description": text(item.get("description")),
        "color": text(item.get("colorName")),
        "size": text(item.get("sizeName")),
        "vendor_number": text(item.get("vendorNumber")),
        "manufacturer_number": text(item.get("manufacturerNumber")),
        "upc": text(item.get("upc")),
        "ean": text(item.get("ean")),
        "cost": number(item.get("cost")),
        "msrp": number(item.get("msrp")),
        "archived": True if flag(item.get("archived")) else None,
    })
