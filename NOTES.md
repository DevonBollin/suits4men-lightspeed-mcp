# Notes for Phase 2

What the docs say, what the live Suits 4 Men account actually does, and what
that means for the write tools. When the two disagree, observed behaviour wins.
Live checks were run on 2026-10-26 against account 304581 ("Suits 4 Men Inc"),
GET only.

## Summary of surprises

| Assumed (from docs) | Observed | What changed |
|---|---|---|
| `employee:inventory_read` covers items and matrices | **ItemMatrix is 403** without `employee:product_cost` (the scope its docs page names). Readable once it was added | `get_matrix` uses the matrix record, and falls back to child items if a 403 ever comes back |
| Item relation for the matrix | `load_relations=["ItemMatrix"]` is 400 "not allowed" | The matrix description is fetched separately |
| InventoryLog filters by `itemID` | **Every filter except `createTime` and `inventoryLogID` is silently ignored** (`itemID`, `IN`, `saleID`, `timeStamp`). You get the whole store's log | `get_stock_history` reads the period's log and filters locally |
| OrderShipment at `OrderShipment.json` | **404**. `Shipment.json` works but its records are under the key `OrderShipment`. `Order` accepts `load_relations=["OrderShipments"]` | `get_purchase_order` uses the relation |
| CatalogVendorItem is searchable | Exact-field lookups take 0.5 s; **a LIKE search on description timed out (30 s)** | `search_vendor_catalog` does exact matches only |
| OrderLine supports `load_relations=["Item"]` | **400** "relations that are not allowed: Item" | `get_purchase_order` fetches line items with `Item.json?itemID=IN,[…]` |
| Bucket 90, drip 1/s, burst 20 | **Bucket 100, drip 1.5/s, burst 25** (the account must have 2 registers) | Nothing; the limiter reads both values from the headers |
| Shop names readable | Shop.json was **403** with the first 4 scopes and is readable with the current 9 (which scope unlocked it is unknown) | Stock rows show the shop name |
| Order fields per docs | Responses also have `subTotalCost` and `totalCost` | Added to PO summaries |
| `or=` combines clauses | `or=description=~,%Beige%\|description=~,%Cobalt%` returned 58 items (Cobalt only) rather than 52 + 58, with the pipe either literal or `%7C` | Not used by any tool. Don't rely on `or=`; run separate queries |
| Credential Manager holds any length | **1280 characters max** per entry | Records are split into chunks with an atomic switch |

## Phase 1 scopes

First authorization: `employee:inventory_read employee:purchase_orders employee:vendors employee:categories`.

Expanded on 2026-10-26 to every read inventory management needs (all 9 confirmed in the token's `scope` claim):

`employee:inventory_read employee:product_cost employee:purchase_orders employee:vendors employee:categories employee:inventory_counts employee:register_read employee:special_orders employee:vendor_returns`

- Only `inventory_read` and `register_read` are read-only scopes. The rest
  allow writes, and the GET-only guard is what holds Phase 1 read-only.
- Not requested: `inventory_counts_reconcile`, `transfers` (one store),
  `customers_read` (special orders show customer IDs only), `tags`,
  `manufacturers` (Manufacturer is readable without it), `reports`, and any `admin*`.
- The per-endpoint docs mention `employee:all_read` and
  `employee:inventory_log`, which are not on the scopes page. The authorize
  endpoint accepts any scope string before sign-in (even a made-up one), so they
  can't be tested without a full authorization. Untested.

## Auth and tokens

- The authorize URL and token endpoint work exactly as documented. With the
  scopes joined by a literal `+`, the token's `scope` claim lists all four
  Phase 1 scopes.
- `expires_in` is **3600** for both the authorization-code grant and the refresh grant.
- The access token is a JWT (claims `acct aud exp iat iss jti nbf scope sub`).
  With 4 scopes it was 957 characters and the refresh token 1034. With 9 scopes
  they are **1114 and 1292**: the refresh token alone is now over Credential
  Manager's 1280 limit. The saved record is split into 3 chunks.
- **Rotation confirmed live:** one forced refresh returned a new access token
  *and* a new refresh token. The pair was saved before first use, the next GET
  worked, and a second TokenManager reused the saved token without refreshing.
- The `acct` JWT claim is the account ID. Account.json returned a single account.
- bootstrap's hidden prompts (`getpass`) **do not receive keystrokes in the
  Claude app's Terminal panel**. Run bootstrap in a normal PowerShell window
  (README says so).

## Rate limits (observed headers)

- `x-ls-api-bucket-level: 3.65/100`, `x-ls-api-drip-rate: 1.5`, `x-ls-api-burst-level: 3/25`, `x-ls-api-request-cost: 1.6`.
- Request cost: a plain GET is 1. With `["ItemShops","ItemAttributes"]` it is 1.6.
  With 6 item relations it is 1.8, and with `["Note"]` on Order it is 1.1. So
  relations cost roughly 0.1–0.3 each, not the documented 0.5 (that may apply
  to writes).
- 400 responses (bad relation) carry no rate-limit headers.
- Writes will cost more. Budget for them in Phase 2: a bulk job creating
  hundreds of variants will hit the bucket, and the existing limiter and 429
  handling cover that.

## Pagination

- `@attributes.next` is a full URL on our account base, e.g.
  `https://api.lightspeedapp.com/API/V3/Account/304581/Vendor.json?limit=2&sort=vendorID&after=WzJd`.
  The server adds `sort=<primary key>` itself, and `next` is `""` on the last page.
- One record comes back as an object, several as a list (as expected).
- `sort=-orderID` works on Order. `count=1` works and returned 790 active items.
- LIKE (`~`) is **case-insensitive**. Spaces in values are sent as `+` and match fine.

## Data model as it exists in this store

### Attribute sets

| ID | Name | attributeName1/2/3 | system |
|---|---|---|---|
| 1 | Color/Size | Color / Size / – | yes |
| 2 | Size | **– / Size / –** | yes |
| 3 | Color | Color / – / – | yes |
| 4 | 3 Attributes | Attribute 1 / Attribute 2 / Attribute 3 | yes |
| **5** | **Color/Size/Fit** | Color / Size / Fit | **no (the store's own)** |

Positions matter: the built-in "Size" set stores size in **attribute2**, not
attribute1. `format.attribute_slots()` keeps positions for that reason. For
Phase 2, new suit matrices go under set **5**. Pants sized by waist only
could use set 2 (size in attribute2), or set 5 with color.

### Items and ItemAttributes

- Item has `ItemAttributes: {attribute1, attribute2, attribute3, itemAttributeSetID}`.
  It only appears for matrix children, and the set ID is on the item (as a string).
- 790 active items. 410 are in matrices and 380 are not.
  - Matrix **7**: 399 children, set 5, vendor San Malone (1), manufacturer
    Caravelli (1), category Suits/Suit (83). Colors: Beige, Black, Cobalt,
    Light Grey, Medium Grey, Midnight Blue. Sizes like 34S–64R incl. L/XL
    (chest+length in one value). Fit: Classic, Slim. Child descriptions look
    like "Caravelli Black 42R Slim", i.e. matrix description + attribute values.
  - Matrices **9** (7 children) and **10** (4 children) use set 3 (Color).
- Category 83 "Suits/Suit" holds 582 of the 790 items.
- **SKUs:** `customSku` is a 6-digit number (e.g. 012911). **57 items have no
  custom SKU.** No item has a UPC or a manufacturer SKU. `systemSku` is
  always set (2100000xxxxx).
- **Negative stock:** 255 items have negative quantity on hand, −5,561 in
  total. Matrix 7 alone sums to −518. Catch-all items exist, e.g. 1186
  "Caravelli Suits" (qoh −16) and 1485 "Caravelli Pants", not in a matrix but
  used on POs. Phase 2 has to decide how these map onto real variants before
  receiving against them.
- Some non-matrix descriptions carry vendor codes, e.g. 1163 "320-0S2 Suit Grey 62R Classic" (vendor Cosani).
  This goes against the "plain-language descriptions" rule.
- `ItemVendorNums`: none of the first 100 items have any.
- `Prices.ItemPrice` is always present with useTypes Default (1), MSRP (2), Online (3).

### ItemShops (stock)

- Two rows per item: **shopID 0 is the all-shops total** and shopID 1 is the
  store. qoh matched in every one of the 790 items.
- `averageCost` is populated only on the shopID 0 row (shop 1 shows 0), so
  `get_item` reports average cost from the total row.
- **Cost fields are visible without `employee:product_cost`** (`defaultCost`,
  `avgCost`, `averageCost`). Whether *writing* cost needs it is for Phase 2 to find out.
- `itemShopID` is kept in the stock rows; it is what a reorder-point update would target.

### Vendors and categories

- **646 active vendors** (677 including archived). Vendor names are fetched
  per ID on demand; `list_vendors` caps at 200 by default and suggests
  `name_contains`.
- Manufacturer is a separate entity (Caravelli = manufacturer 1, sold through
  vendor San Malone). Manufacturer.json is readable with these scopes.
- 32 categories. `fullPathName` uses `/` (e.g. "Suits/Suit"); `nodeDepth`, `leftNode`/`rightNode` and `parentID` are present.

### Purchase orders

- An Order with no `orderedDate`/`receivedDate` simply omits those keys. All
  11 not-complete POs have no `orderedDate`, so they show as status "open".
- `complete` is the finished flag. `subTotalCost` and `totalCost` exist but are undocumented.
- `Note` relation: `{noteID, note, isPublic, timeStamp}`.
- OrderLine fields match the docs: `quantity`, `price` (unit cost), `originalPrice`,
  `vendorCost` (0 on the lines seen), `checkedIn`, `numReceived`, `total`, `itemID`.
  `OrderLine.json?orderID=` filters correctly.

## Expanded reads (2026-10-26, with the 9 scopes)

### Matrices

| ID | Description | Set | Children (incl. archived) | Matrix Default price / cost |
|---|---|---|---|---|
| 6 | Caravelli | 3 Attributes | (archived matrix) | |
| 7 | Caravelli | Color/Size/Fit | 399 | **61.21 / 61.21** (children sell at 599.95) |
| 8 | Giorgio Fiorelli | Color/Size/Fit | **0** (18 colors defined) | 549.95 / 63.15 |
| 9 | Angelo Rossi | Color | 7 | 549.95 / 68.14 |
| 10 | Caravelli Tux | Color | 4 | 549.95 / 67.7 |
| 11 | Giorgio Fiorelli Tuxedo | Color/Size/Fit | **0** | 499.95 / 73.34 |

- `attribute1Values` etc. are plain lists in the matrix's own order (not sorted).
- **Matrix 7's matrix-level price equals its cost.** In Lightspeed, saving a
  matrix price can push it to every child. Any Phase 2 matrix update must
  leave prices alone unless explicitly asked.
- Matrices 8 and 11 have attribute values but no child items, so their
  variants were never generated.
- Matrices 9 and 10 split by color only and carry no size. They are candidates
  for re-homing under set 5.

### Stock history (InventoryLog)

- Only the `createTime` (e.g. `>=,2026-09-27T00:00:00+00:00`) and
  `inventoryLogID` filters work, and sorting by `-inventoryLogID` works.
- About 1,200–1,300 entries per 30 days (4–5 s to read), about 11 s for 90 days.
- Reasons seen in 30 days: `removeInventoryForTransaction` 823–920,
  **`autoResolvedNegativeInventory` 266**, `checkinPurchaseOrderInventory` 76,
  `putBackForTransaction`, `removeInventoryForLayaway`, `undoAdjustmentForLayaway`.
- **About half of all changes push stock negative** (631 of 1,198 in the latest 30 days).
  Item 1644 (Caravelli Black 42R Slim) sits at qoh −27.

### Sales (SaleLine / Sale)

- The `itemID` filter (incl. `IN`) and the `createTime` filter work on SaleLine.
- Only lines on Sales with `completed=true` and `voided=false` count.
  Layaway lines on completed sales count as sold; some lines are $0 add-ons.
- **Net sales of a line = `calcTotal − calcTax1 − calcTax2`**, which equals
  `displayableSubtotal`. `calcSubtotal` is before discounts.
- About 3,600 sale lines per 90 days. A store-wide 30-day report takes about 26 s;
  a matrix for 90 days about 11 s.
- Last 30 days: 1,114 net units and $105k net sales. **The top sellers are
  catch-all items**: "Alteration" (222), "Neck Ties - Standard" (146, qoh
  −1,052), "Dress Shirts (Standard)" (143, qoh −422). These are sold without
  stock being received, and they cause most of the negative stock.
- Matrix 7, last 90 days: 186 net units. Sizes 42R > 44R > 46R > 40R > 38R;
  colors Black > Beige > Midnight Blue > Medium Grey > Light Grey > Cobalt.

### Counts, special orders, vendor returns, catalogs

- InventoryCount: 21 counts. Most are used to **write off stock** (donations,
  damaged goods). Responses include undocumented `reconcileStatus` and `jobID`.
  `InventoryCountCalc.json?inventoryCountID=` filters correctly and has calcQoh vs countedQoh.
- SpecialOrder: only 8, all from 2023, status "Completed". The item is reached
  via `SaleLine.Item`, as documented.
- VendorReturn: actively used (29 so far; e.g. Peerless Clothing $4,185).
  Statuses seen: `open`, `done`. `closedDate` is undocumented. The return reason is
  `VendorReturnItemReason.reason` (e.g. "Defective").
- CatalogVendorItem: real catalogs are attached (IDs around 171k) and include
  `brand`, `colorName`, `sizeName`, `vendorNumber`, `upc`, `cost` and `msrp`.
  This could seed Phase 2 item creation if a suit vendor's catalog is there.

### Access map with the 9 scopes (GET, 2026-10-26)

- **Readable, used by tools:** Item, ItemShop, ItemMatrix, ItemAttributeSet,
  Category, Manufacturer, Vendor, Order, OrderLine, Shipment, InventoryLog,
  InventoryCount, InventoryCountItem, InventoryCountCalc, Sale, SaleLine,
  SpecialOrder, VendorReturn, CatalogVendorItem, Shop.
- **Readable, not used:** Item/CustomField (empty), Image and ItemMatrix/Image (empty), Serialized (empty),
  TagGroup (empty), TaxClass, Discount, Register, PaymentType, and Tag (4 tags that label people; none on any item).
- **Readable without asking for it:** **Customer and Employee**. One of the
  9 scopes (probably register_read or special_orders) includes them. No tool
  returns them. Since 2026-10-26, `api_get` accepts only the inventory
  endpoints in `API_GET_ENDPOINTS` and refuses `load_relations` mentioning
  customer, employee or payment records (tests in `tests/test_api_get_guard.py`).
  Sale and SaleLine records themselves carry only customer and employee IDs.
- **403:** PriceLevel, Season, Transfer, Quote, Workorder, CustomerType, ShipTo, CreditAccount.
  None matter for inventory: there are no seasons, one store, and price
  levels already come through on each item's Prices.
- ItemFee answers 422 "custom print template SaleReceipt needs an update", which is unrelated.
- **Paging is complete:** `count=1` matched the paged totals for Item (790
  active, 830 incl. archived), Order (828) and Vendor (677 incl. archived).

## Phase 2 scope plan

The current 9 scopes, with `employee:inventory_read` replaced by `employee:inventory`.

- If creating a matrix fails with that, add `employee:product_edit`.
- `get_matrix` already prefers the matrix record when it's readable.

## Tooling

- `check_tools.py` calls every tool over stdio against the live account (GET only, 26 calls, about 90 s).
- Unit tests: `.venv\Scripts\python.exe -m pytest` (106 tests).
