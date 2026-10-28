# Notes: the Lightspeed API and the store's data

What the docs say, what the live Suits 4 Men account actually does, and what
that means for the tools. When the two disagree, observed behaviour wins.
Live checks were run on 2026-10-26 against account 304581 ("Suits 4 Men Inc"),
GET only. **No write has ever been sent to Lightspeed** from this project
(Phase 2 was built and checked with dry runs and mocked tests only).

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
| **6** | **Color/Size/Width** | Color / Size / Width | **no** (created 2026-10-27 for shoes, MT2; see "Fourth real write") |

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

## Phase 2: writes (2026-10-26)

### What the docs say about write requests

From developers.lightspeedhq.com/retail (endpoint pages ItemMatrix, Item,
Order, OrderLine, Shipment, the scopes page, the rate-limit page, and the
ItemMatrix and inventory tutorials). **None of this has been tried on the live
account yet**: the first real write (with the owner's approval) is the first test.

| Request | Body (bare JSON object, not wrapped in `{"Item": …}`) |
|---|---|
| `POST ItemMatrix.json` | Only `description` and `itemAttributeSetID` are required. Also `itemType`, `tax`, `taxClassID`, `categoryID`, `manufacturerID`, `defaultVendorID`, `defaultCost`, `Prices: {ItemPrice: [{amount, useTypeID, useType}]}` |
| `POST Item.json` (matrix child) | `itemMatrixID` plus `ItemAttributes: {itemAttributeSetID, attribute1, attribute2, attribute3}`; `customSku`, `description`, `defaultCost`, `Prices`, IDs as above |
| `PUT Item/{id}.json` | **Merges**: omitted fields keep their values, empty or null blanks a field. Send only what changes |
| `POST OrderLine.json` | `orderID`, `itemID`, `quantity`, `price` (unit cost), `originalPrice`, `numReceived`, `vendorCost` |
| `PUT OrderLine/{id}.json` | Same fields. **`checkedIn` is not writable** |

- **Matrix attribute values are not writable.** `attribute1Values` etc. fill in
  automatically when a child Item with those values is created. So a matrix is
  created empty, and each color/size/fit is created as its own Item. Matrices 8
  and 11 (values but no items) were probably set up in the Lightspeed UI.
- A matrix child's description is `<matrix description> <attr1> <attr2> <attr3>`
  (docs). The tools send it that way. Whether Lightspeed overwrites a different
  description is **unconfirmed**.
- **DELETE on ItemMatrix archives the matrix and all of its items.** No route
  touches an existing matrix; archiving matrices stays manual (owner's decision).
- Price propagation from a matrix to its children is **not documented**.
  No tool updates a matrix, so it can't happen.
- Whether Lightspeed rejects a duplicate `customSku` is **not documented**. The
  tools check every item, archived ones too, before sending.
- Values: endpoint samples send strings (`"20"`), the tutorials send numbers. The
  tools send strings, matching what GET returns.
- Success status codes aren't documented; 400, 403 (missing scope), 409, 422
  (validation) and 429 are. POST and PUT return the full record under its key.
- **Rate cost:** since 2026-03-17 every request costs 1 drip (writes used to
  cost 10), plus 0.5 per nested record in the body. The write client budgets 2.

### Receiving against a purchase order

- `numReceived` = received but **not** checked in (not in stock yet).
  `checkedIn` = added to inventory. A PUT sets `numReceived` to an absolute
  value; it is not a delta.
- **There is no documented API call that checks stock in.** The inventory
  tutorial sets `numReceived` and says the PO "must now be finished in Retail
  POS", i.e. the **Add Received To Inventory** button on the PO, then Finished.
- Order `receivedDate` / `orderedDate` only change the PO's status
  (Check-In / Ordered); `complete` is not in the writable list.
- Shipment (`OrderShipment`) records are read-only receipts that check-in creates.
- Stock could be set directly (`PUT Item` with `ItemShops.ItemShop.qoh`, or a
  count + reconcile), but that skips PO and cost history. **No tool does that**,
  and the write path refuses `ItemShops` in any body.
- So `receive_po_lines` only sets `numReceived`, and says that stock changes
  only when someone finishes the check-in in Lightspeed. Whether `numReceived`
  goes back to 0 after check-in (moving into `checkedIn`) is **unconfirmed**;
  the over-receipt check counts `checkedIn + numReceived`.

### Scopes

- `SCOPES` now has `employee:inventory` (view, create, update and archive items)
  in place of `employee:inventory_read`. Bootstrap was re-run at 20:43 on
  2026-10-26; the token's `scope` claim lists all 9, `employee:inventory`
  included. After that, all 26 read calls and all 5 write-tool dry runs passed
  live (`check_tools.py`, `--dry-runs`).
- `employee:product_edit` exists ("create, update and archive products") but
  is **not requested**: the ItemMatrix page names only `employee:product_cost`
  (held). If the first matrix creation answers 403, add it and re-run bootstrap.
- The Item page's scope list is odd (cfd, register, product_cost, reports,
  workbench, customers, admin) and looks incomplete. `employee:inventory_base`
  appears on the ItemAttributeSet page only.
- The approving employee's rights also cap the token: run bootstrap as the owner.

### How the write path is built

See README.md ("Write tools" and "Turning writes on and off") and the comments
in `lightspeed_mcp/write_path.py`. In short: separate WriteClient that only
the write tools hold; master switch file `state/WRITES_ENABLED` (shipped
absent = off); one route per tool with a field allowlist (one DELETE, for one item; no matrix
update, prices only on new records, never `ItemShops`); dry run by default;
real calls need the dry run's confirm code (a hash of the exact requests);
append-only log `state/write_log.jsonl`; 5xx and network errors are never
retried, since a write might have gone through.

### To confirm on the first real writes (each approved by the owner)

1. ~~Status code of a successful POST/PUT (200 or 201?) and the response shape.~~ **PUT: 200**, record
   under its key (`{"Item": {...}}`), see "First real write" below. **POST: 200** too, record under
   its key (`{"ItemAttributeSet": {...}}`), see "Fourth real write" below.
2. Creating a matrix with `employee:product_cost` + `employee:inventory`, without `product_edit`.
3. A new child sent with only a Default price: what MSRP and Online become.
4. Whether a child's description is kept as sent.
5. Whether a duplicate `customSku` is refused by Lightspeed itself.
6. What `numReceived` and `checkedIn` show after "Add Received To Inventory".
7. The real request cost of a write (`x-ls-api-request-cost`).

Take a snapshot (`backup_lightspeed.py`) before and after, and compare.

### First real write (2026-10-26 22:06, approved by the owner)

- The owner turned writes on by hand, approved the dry run in chat, and turned
  writes off again right after.
- `update_item_fields`: `PUT /API/V3/Account/304581/Item/2321.json` with
  `{"customSku": "013471"}` (confirm code `ae0bdb8e3b`). **HTTP 200**; the reply
  held the item under `Item`. Write log: one `sending` and one `done` line
  (write_id `ad4251f81bb3`).
- **A PUT merges, confirmed live:** snapshots 2026-10-26_220504 (before) and
  2026-10-26_220635 (after) differ in exactly one record and two fields: item
  2321's `customSku` ("" → "013471") and `timeStamp`. Prices, cost, stock,
  category and every other record were untouched.
- 56 active items are now without a custom SKU; the highest SKU in use is 013471.

### Second real write: first matrix update (2026-10-26 22:12, approved by the owner)

- `update_matrix`: `PUT /API/V3/Account/304581/ItemMatrix/8.json` with
  `{"description": "Giorgio Fiorelli Suit"}` (confirm code `10817dec52`), on a
  matrix with **no items** (archived included), so nothing could cascade.
  **HTTP 200**, reply under `ItemMatrix`.
- **A matrix PUT merges too, confirmed live:** snapshots 2026-10-26_221043 and
  2026-10-26_221219 differ in exactly matrix 8's `description` and `timeStamp`.
  Its attribute set, attribute value lists, prices (549.95 / MSRP 795 / Online 0),
  cost, vendor and category were untouched.
- So the current scopes (`employee:inventory` + `employee:product_cost`, no
  `product_edit`) can **update** a matrix. Creating one is still untested.
- Still unconfirmed: what a matrix update does to a matrix's *items* (needs a
  matrix with items; matrix 8 had none).

### Third real write: first move into a matrix (2026-10-26 22:16, approved by the owner)

- `move_items_into_matrix`: `PUT /API/V3/Account/304581/Item/2321.json` with
  `{"itemMatrixID": "7", "ItemAttributes": {"itemAttributeSetID": "5",
  "attribute1": "Burgundy", "attribute2": "58L", "attribute3": "Slim"},
  "categoryID": "83", "defaultVendorID": "1"}` (confirm code `b5c3466162`).
  **HTTP 200**; the reply already showed `itemMatrixID` 7.
- **Moving an existing item into a matrix by PUT works** (undocumented until now).
  Snapshots 2026-10-26_221419 and 2026-10-26_221615 differ in exactly 2 records:
  - item 2321: `itemMatrixID` 0 → 7, `ItemAttributes` set, `categoryID` 82 → 83,
    `defaultVendorID` 0 → 1, `timeStamp`. **Its prices (599.95 / 799.95 / 549.95),
    cost, SKU and description did not change**: joining a matrix does not apply
    the matrix's own price (61.21) to the item.
  - matrix 7: `attribute1Values` gained "Burgundy" (Lightspeed adds a new value
    by itself, as documented for new items), and `timeStamp`. Its price and
    every other item were untouched.
- The write log now holds a completed move, so `move_items_into_matrix` allows
  up to 100 items per call from here on (each call still needs approval).
- Still unconfirmed for moves: what happens to the *old* matrix's attribute
  lists when an item leaves a matrix (2321 was in none).

### Fourth real write: first POST, the shoe attribute set (2026-10-27 20:16, approved by the owner)

The first step of the migration in `docs/inventory-structure.md` (Appendix C.2
step 1, §13 "Immediate fixes": create `Color/Size/Width` for shoes).

- `create_attribute_set`: `POST /API/V3/Account/304581/ItemAttributeSet.json` with
  `{"name": "Color/Size/Width", "attributeName1": "Color", "attributeName2": "Size",
  "attributeName3": "Width"}` (confirm code `811137a6a0`). **HTTP 200**, reply under
  `ItemAttributeSet`. New set **6**, `system: false`, `archived: false`.
- **Creating an attribute set works** with the current scopes; `employee:inventory_base`
  (named on the docs page) isn't needed, and the docs' "only allowed names" rule
  isn't enforced. The docs' "GET only" for this endpoint is wrong.
- Snapshots 2026-10-27_201433 and 2026-10-27_201704 differ in exactly one record:
  ItemAttributeSet 6 added. No item, matrix or other record changed.
- Two tool gaps it showed (both fixed the same day):
  - The write log's `affected_ids` came back `{"ItemAttributeSet": {}}`:
    `write_path.ID_FIELDS` had no `itemAttributeSetID`, so the new ID wasn't recorded
    (`CREATED_ID_FIELDS` in writer.py lacked it too). Both have it now.
  - `list_attribute_sets` didn't show set 6 right after: the reader caches attribute
    sets and categories for 10 minutes (`CACHE_SECONDS`), and nothing cleared the cache
    after a write. The writer's checks read the same cache (e.g. a category made
    moments ago, `writer.py` `reader.categories()`), so a tool run right after a
    create could miss the new record. Now every real write ends with
    `reader.forget_cached()` (attribute sets, categories, shops, vendor names).
- Also fixed then: `update_matrix`'s price/cost warning named only the Default price,
  though the PUT re-sends every level. It now lists each value sent beside what the
  items hold today. Live for matrix 7 at 599.95: MSRP 995 differs on 235 of 400 items
  (213 at 695, 18 at 795, …), Online 0 on all 400 (347 at 549.95, 53 at 599.95), so a
  cascade would matter even though Default wouldn't change.

### Data findings (2026-10-26, from the snapshot)

- **Custom SKUs:** 771 of the 830 items (archived included) have one; all are 6 digits and
  none is duplicated. The highest is **013470**. 57 active items (and 2 archived) have none (e.g. 2321 "Caravelli
  Burgundy 58L Slim", 2322 "Bryant Black", 2331 "Ralph Lauren Pants").
- Item 2321 "Caravelli Burgundy 58L Slim" is **not in matrix 7** (itemMatrixID 0),
  and matrix 7 has no Burgundy color.
- Matrix 7's children all sell at 599.95 (Default), but their **MSRP varies (695 or 995)**,
  Online is 549.95 or 599.95, and **cost varies** (63.50, 65.94, 75.06, …). The matrix's
  own Default price and cost are both 61.21. Matrix 7's record changed on 2026-10-21.
- Each brand has a **catch-all pants item outside any matrix** (e.g. 1485
  "Caravelli Pants", 1479 "Angelo Rossi Pants", 1483 "BN-5851 Pants", 1475
  "320-PE0-1 Pants"), several with vendor codes in the description.
- Boys' items exist: "Giorgio Fiorelli Boys Shirt" and "Boy's Suit".
- Brand names can contain color words: manufacturer "Ascott Brown"; vendors
  "White Sierra", "Blue Generation", "Red Kap" and others. The color check
  ignores the matrix's own vendor and manufacturer names for this reason.
- Order.json with `archived=true` has **853** POs (828 without). OrderLine: 2,538. Shipment: 820.
- One tax class: 1 "Item". Every matrix is `tax: true`, `taxClassID: 1`.
- The 11 open POs have no `orderedDate`; PO 853 (San Malone, ref 112423) has 12
  lines, nothing received. Line 2692 orders catch-all item 1186 "Caravelli Suits".

### Snapshots

- `backup_lightspeed.py` saves every Item (with stock, attributes, vendor
  numbers, notes), ItemMatrix, ItemAttributeSet, Category, Manufacturer,
  Vendor, Shop, TaxClass, Order, OrderLine and Shipment record to
  `backups/<date_time>/` (about 5.5 MB, about 75 s, GET only). Not in git.
- `--compare A B` lists records added, removed or changed (field by field).
- Taken at 20:22 and 20:39 on 2026-10-26, before and after building Phase 2:
  **identical, 0 differences in every record type.**

## Moving items, batch SKUs, archiving (2026-10-26, evening)

Three more write tools, built on the owner's request: `move_items_into_matrix`,
`assign_custom_skus`, `archive_item`. Same rules as the others (dry run first,
switch, confirm code, write log). The owner decided: **matrices are never
archived through the API; that stays manual.**

From the docs (Item, ItemMatrix, ItemMatrix tutorial pages):

- **Archiving:** "Items can not be deleted, only archived. If you issue a
  delete on an item it will be archived." And: "If you archive an item with
  inventory, the inventory will be zeroed out. This can not be undone without
  individually re-entering the inventory records." So `archive_item` refuses
  any item whose stock isn't exactly 0 in every shop row, negative included.
- The DELETE sample response shows the archived item with `itemType:
  non_inventory` and `tax: false` (it was `default`). Not explained. The tool
  saves the item's values (`before`) in the write log.
- **Unarchiving:** `archived` "can only be set to false on update", so a PUT
  with `archived: false` looks like the way back. Not documented as such, and
  stock would not come back. Not built.
- **Moving an existing item into a matrix is not described anywhere.**
  `itemMatrixID` and `ItemAttributes` are in the Item PUT field list. The
  tutorial only creates new items in a matrix. Unconfirmed: whether a PUT
  moves an item, whether Lightspeed rewrites its description or prices, and
  what happens to the old matrix's attribute lists. So the tool allows one item
  per call until the write log has a completed move.
- Whether a matrix's `itemAttributeSetID` can change once it has items is not
  documented. Moving matrices 9 and 10 (Color only) to Color/Size/Fit is done
  by creating a new set-5 matrix and moving the items; the old matrix is then
  archived by hand.
- The `confirm_code` of a plan now also covers facts the request doesn't show
  (for archiving: the item's stock and timeStamp), so a change between the dry
  run and the approval is caught.

Live dry runs (GET only): moving 2321 into matrix 7 as Burgundy / 58L / Slim
sends `itemMatrixID`, `ItemAttributes`, `categoryID 82 -> 83` and
`defaultVendorID none -> 1` (2321 has no vendor). Batch SKUs for 2321–2325 got
013471–013475. Archiving "Boy's Suit" (2366, stock −1) and "Neck Ties -
Standard" (1387, stock −1,052; also on PO 20, which is not complete) was
refused for stock. "Giorgio Fiorelli Boys Shirt" (1471) has stock 7, so it
would be refused too.

## Matrix updates, purchase orders, invoice intake (2026-10-26, night)

Built on the owner's request ("hand Claude an invoice or packing slip and
update the inventory"): `update_matrix`, `create_items`, `create_purchase_order`,
`update_purchase_order`, `update_po_lines`, `remove_po_lines`,
`set_vendor_numbers`, `intake_invoice` (one approval per document, as the owner
chose) and the read-only `match_invoice_lines`. Owner's permissions: vendor
style numbers, standalone items, removing unreceived PO lines.

### Matrix updates (Retail help "Creating matrixes", imports help; not documented for the API)

- The matrix screen has **"Shared Values – Applied to all items in the
  matrix"** (description, category, brand, vendor, tax class, item type) and
  **"Default Values"** (price, cost, tags), which only reach existing items via
  the **Reset Items To Defaults** button.
- **A new matrix description renames every item** to `<description> <attr1> <attr2> <attr3>`.
  Renaming an attribute value in the UI renames its items too.
- API: a PUT merges (omitted fields keep their values; empty/null blanks them).
  Whether a PUT with `Prices`/`defaultCost` reaches the items is **unconfirmed**;
  the ItemMatrix tutorial's example child kept prices 0 while its matrix had 20.
- Not documented: changing `itemAttributeSetID` on a matrix with items; a
  third-party integration reports API item writes failing after a set's
  attribute count was reduced. So `update_matrix` never sends it, nor
  attribute values (read-only, built from the items), `archived`, `itemType`, tax.
- `update_matrix` sends only changed fields, re-sends every price level with
  only Default changed (an empty level could blank it), and its dry run counts
  the items each shared change reaches and previews renamed items.
- **The first real matrix update is the test**: snapshot before, compare after.
  (No test matrix may be created: the owner's rule.)

### Purchase orders and receiving (Order, OrderLine, Shipment docs; Retail help on POs)

- Still **no API call adds received goods to stock** for a PO: Order has
  only GET/POST/PUT/DELETE, Shipment records are read-only, `checkedIn` is not
  writable, and the changelog has no PO entry. (September 2026 added receive
  actions for *inventory transfers* only.) UI steps: open the PO → status
  **Check In** → # Received → **Add Received To Inventory** → **Finished**.
  With `numReceived` already set through the API it is one click plus Finished.
- Order writable: `orderedDate` (→ Ordered), `receivedDate` (→ Check In; both
  need at least one line), `arrivalDate`, `refNum`, `shipCost`, `otherCost`
  (negative = a discount), `discount` (%), instructions. `complete` and
  `archived` are not in the writable list; DELETE archives a PO. No route sends them.
- OrderLine: `originalPrice` = the vendor's unit cost; `price` = unit cost
  after shipping/discount spreading. **Live data: on finished PO 219 (shipping
  split "evenly") `price` 65.907 = `originalPrice` 63.50 + `shippingCost` 2.407.**
  On every open PO `price` equals `originalPrice`: spreading happens later, by
  the PO's `shippingCostMethod` (evenly 555 POs, total 295, proportionally 1).
  So new lines get price = originalPrice = the invoice's unit cost.
- No PO in the store uses a discount (0 of 853). 32 lines have a `vendorCost`.
- OrderLine DELETE removes a line for good; `remove_po_lines` logs it first.
- Lightspeed also offers a PO spreadsheet import (up to 500 items, no
  received quantities, no matrix columns) and vendor catalogs; NuORDER for some brands.

### Vendor numbers (ItemVendorNum)

- A relation on Item (`ItemVendorNums.ItemVendorNum`: `itemVendorNumID`,
  `vendorID`, `value`, `cost`), written through Item POST/PUT. Lightspeed allows
  **one per vendor per item**.
- **Live check (GET): `Item.json?ItemVendorNums.value=Christopher Lena` returned
  only item 1468**, so items can be found by vendor number.
- Lightspeed's PO screen can receive by Vendor ID, and that **fails when two
  items share a Vendor ID**. So a number goes on one item only; a style number
  printed on several invoice lines is not saved. `set_vendor_numbers` re-sends
  the item's existing vendor numbers with their IDs, so they are kept whether
  Lightspeed merges or replaces the list (which is unconfirmed).
- Only one item (1468) has a vendor number today, and none has a manufacturer SKU.

### Intake design

- Claude reads the document; `match_invoice_lines` resolves each line (item_id;
  vendor number; matrix + color/size/fit; or new); `intake_invoice` builds one
  plan: [new PO] → [new items] → [vendor numbers] → [PO lines, received] →
  [PO dates]. Later requests refer to records created earlier as `{new:order}`
  / `{new:item:N}`; the real IDs are filled in from Lightspeed's replies, and the
  confirm code covers the plan with those placeholders.
- A vendor + reference already on any PO is refused (no double entry). The same
  item twice on one document is refused (combine the lines). New variants need
  their selling price; new items get the next free SKUs.
- Against an existing PO: received quantities are added to `numReceived`; a line
  that receives more than ordered has its quantity raised (warned); a different
  invoice cost replaces the line's cost unless some was already checked in.

### Findings from the dry runs (GET only)

- Besides 2321, more Caravelli Burgundy suits sit outside matrix 7 without a
  matrix (e.g. 1187 "Caravelli Burgundy 34S Slim", 1188 36R Slim, 1189 36S
  Slim); matrix 7 has no Burgundy color.
- Renaming matrix 7 to "Caravelli Suit" would rename all 399 items
  ("Caravelli Beige 50S Classic" → "Caravelli Suit Beige 50S Classic").
- All 399 of matrix 7's items sell at 599.95; its own price is 61.21, MSRP 995, Online 0.

## New vendors and brands (2026-10-26, late)

Built on the owner's request, so an invoice from a new vendor or brand doesn't
block intake: `create_vendor`, `create_brand`, and `intake_invoice(new_vendor=…,
new_item.new_brand=…)`.

- Docs: Vendor POST/PUT fields `name`, `accountNumber`, `priceLevel`,
  `updatePrice`/`updateCost`/`updateDescription` (catalog settings),
  `shareSellThrough`, `b2bSellerUID`, `Contact`, `Reps`. No required field is
  marked; `archived` isn't writable; DELETE archives. Only `name` and
  `accountNumber` are sent. Manufacturer POST: `name` only (required); **DELETE
  is not allowed at all**.
- Scopes page: `employee:vendors` "View, create, update and archive vendors"
  (held); `employee:manufacturers` "View, create, update, and archive
  manufacturers" (**not held**: added to SCOPES; bootstrap must be re-run
  before the first brand is created). Neither endpoint page names its scope.
- Name checks (`rules.name_key`, `same_and_similar_names`): same name =
  same after lower-casing, dropping punctuation, "&" → "and", and a trailing
  Inc/LLC/Ltd/Corp/Co. Similar = difflib ratio ≥ 0.85, or one name is a whole
  word of the other. Same is refused (archived vendors included); similar is
  refused unless `allow_similar_name(s)=true`, which Claude passes only after
  the owner confirms.
- Live dry runs (GET only): "San Malone, Inc." → refused (vendor 1); "Tommy
  Hilfiger" → refused (brand 15); "Tomy Hilfiger" → refused as like brands 15
  and 34; new vendor "Bruno Piattelli" + brand + tie → one plan of 6 requests.
  A new-vendor check reads all 677 vendors: about 20 s.

### Findings (from the snapshot)

- Vendor names used twice: "Broder Brothers" and "Yellowman (YMX)".
- Brand names that look like typos of each other: "Tommmy Hilfiger" / "Tommy
  Hilfiger", "Canaletto" / "Canoletto", "Marco Valenino" / "Marco Valetino",
  "Retro Paris" / "retro partis", "Stacy Adams" / "stac", "Renoir" / "Renoir
  Wool", "Vitarelli" / "Marquis / Vitarelli".
- Only 3 of 677 vendors have an account number.

## Prices, reorder levels, PO/vendor archiving, categories, counts (2026-10-26, late)

Built on the owner's request (the rest of the CRUD table). Docs findings:

- **Prices:** no page says what a PUT with only some price levels does (every
  documented example sends all three). `update_item_prices` sends all three, only
  the asked-for ones changed. It is the only route that puts `Prices` on an
  existing item.
- **Reorder:** `PUT ItemShop/{itemShopID}.json` (changelog 2025.06.06) "allows you
  to update the reorder point and reorder level"; qoh isn't writable there. Used
  instead of Item PUT with ItemShops, so **no route carries `ItemShops` or `qoh`**.
  No item in the store has a reorder point or level today (0 of 790).
- **ItemVendorNum:** changing a value (keep `itemVendorNumID`) and removing one are
  not documented; a number without `cost` sets the vendor cost to 0, so new
  numbers carry the invoice's or item's cost. Removal isn't offered.
- **Order DELETE** archives ("Archived orders are not counted towards backorder
  totals"). What happens to the lines and whether it can be undone is unconfirmed.
- **Vendor DELETE** archives; unarchiving isn't documented. `archive_vendor`
  refuses a vendor that is any active item's default or has an unfinished PO.
- **Manufacturer:** PUT `name`; DELETE is not allowed at all.
- **Category:** POST/PUT `name`, `fullPathName`, `parentID` (nested set, the
  documented pattern sends the path too); POST/PUT/DELETE cost 10 drips; **DELETE
  is permanent**, refused by Lightspeed with subcategories, and items "will not
  be deleted" (where they go is unconfirmed). So `delete_category` needs a
  category with no items (archived too), no matrices, no subcategories, and
  `rename_category` refuses one with subcategories (whether their paths update
  is unconfirmed).
- **Inventory counts:** `POST InventoryCount` {name, shopID} → `POST
  InventoryCountItem` {inventoryCountID, itemID, qty, employeeID (required)} →
  `POST InventoryCountReconcile`. "The quantity on hand is not adjusted until the
  count is reconciled." Reconcile "can not be undone"; it has its own scope
  (`employee:inventory_counts_reconcile`, not held). **No route reconciles.**
  Repeated entries for an item add up, so each item is entered once.
  Live `reconcileStatus` values: `awaiting_reconciliation`, `reconciliation_success`,
  `reconciliation_done`. Every existing count item, and recent POs, use employee 1
  (`COUNT_EMPLOYEE_ID`); the token's `sub` claim (1434369) is not an employee ID.
- **Vendor returns are read-only in the API** (GET only, scope "View vendor
  returns"); no VendorReturnItem endpoint. **Special orders** can only be created as
  a customer SaleLine (`isSpecialOrder`, `customerID` required): not built, customer
  data stays out. **ItemAttributeSet** names are limited to Lightspeed's own; not
  built (the store uses set 5).

### Findings (dry runs and snapshot)

- The duplicate vendor names are each an archived original plus an active copy
  with no items and no POs: "Broder Brothers" 101 (archived) / 700, "Yellowman
  (YMX)" 102 (archived) / 701.
- Item 1468's only vendor number is "Christopher Lena": the vendor's name, not an
  item number.
- Empty categories (no items, matrices or subcategories): 114 ".../Dress
  Shirts/Dress Shirt", 115 ".../Non Iron", 119 ".../Ultra Stretch".
- 9 POs from 2024 (353, 389, 415, 438, 469, 470, 472, 476, 495) are unfinished
  with nothing received.
- Category 83 holds items with vendor codes in their description, e.g. 1158
  "S62KR Suit Heather-Grey 34R Modern".

## Attribute sets, and matrices on other sets (2026-10-26, late)

The owner asked for Claude to be able to add and edit attribute sets. **Rule
change (owner, 2026-10-26):** suits, tuxedos, vests, pants and sport coats stay
on set 5 Color/Size/Fit; other garments (shirts, shoes) may use a store-made
set; never a built-in set (1-4); every set has Color. `rules.py` holds it
(`SUITING_GARMENTS`, `check_matrix_attribute_set`, `check_attribute_names`).

- Docs (ItemAttributeSet page): "Allowed actions" lists GET only, yet POST and
  PUT sections exist; `name` "Color/Size, Size, Color, 3 Attributes. These are
  the only allowed values for now", contradicted by its own "Color/Size/Fabric"
  example and by this store's set 5; scope `employee:inventory_base` (not on the
  scopes page). Fields: name, attributeName1-3, archived, system. DELETE not
  documented. **The first create/update is the test**; a 403 would mean adding a
  scope (try `employee:inventory_base`) and re-running bootstrap. *Done 2026-10-27:
  create works with the current scopes (set 6, "Fourth real write"); update still untested.*
- A set used by matrices: removing an attribute is refused (a third-party
  integration reports item writes failing after a set's attribute count was
  reduced); moving Color or swapping names between slots is refused (items keep
  values by slot, so they would show under the wrong names).
- Matrix tools now read a matrix's set fresh and take values by slot:
  `{"values": {"Neck": "15.5"}}` by attribute name, or color/size/fit for slots
  1/2/3. Every named attribute is required except one called "Fit". Pants waist
  sizes are checked on the attribute called "Size".
- Live dry runs: creating "Color/Neck/Sleeve" plans one POST; a copy of set 5's
  attributes is refused; renaming set 5's "Fit" lists matrices 7, 8 and 11;
  removing "Fit" from set 5 and editing built-in set 1 are refused.

## Helper tools (2026-10-26, evening)

- `suggest_custom_skus`, `show_write_log`, `take_snapshot`, `compare_snapshots`.
  None can write to Lightspeed. `take_snapshot` adds a folder under backups/.
- Live: the next free SKUs are 013471 upward (highest in use 013470);
  830 items checked; 57 active items without a SKU.
- `take_snapshot` runs in a background thread of the server: the tool returns
  at once, and `manifest.json` (written last) marks the folder complete.
- A third snapshot (2026-10-26_211318) taken through the runner compared
  identical to 203912: still 0 differences, so nothing changed in Lightspeed.

## Items, vendor codes, UPCs, categories, stock, tags (2026-10-26/27)

Built against the draft inventory structure (the owner's artifact "Suits 4 Men
— Lightspeed R-Series Inventory Structure"), easiest and best documented first.
No real write was made.

### What the docs and the account say

- **Item fields**: the Item docs list `categoryID`, `manufacturerID`,
  `defaultVendorID`, `upc` and `ean` as writable on POST and PUT; `upc`/`ean`
  "Must be 11-14 digits". Item PUT merges (confirmed live earlier).
- **Archiving**: "If you archive an item with inventory, the inventory will be
  zeroed out." So archiving answers the structure draft's question; the tool
  still only archives items at 0 (adjust_stock settles them first).
- **Stock**: not in the current Item or ItemShop docs (ItemShop PUT takes only
  reorder settings). Lightspeed staff on the developer forum (2015): update qoh
  with `<Item><ItemShops><ItemShop><qoh>` naming the shop; "ItemShop Id 0 is
  read-only" (the all-shops total). The Item docs: "Either the itemShopID or
  shopID field must be provided when making updates." The Cost control help
  lists "quantity added through the API" among adjustments costed by that
  setting. Item history labels API changes. **Unconfirmed**: the inventory log
  reason Lightspeed records, and the cost it gives added stock.
- **Categories**: the Category PUT lists `name`, `fullPathName`, `parentID`.
  The Retail help moves a category by changing its Parent; moving a parent
  carries its subcategories. **Unconfirmed**: whether the API recomputes
  `fullPathName` (sent anyway) and subcategories' paths (hence leaf moves only).
  The real tree is 32 categories, depth 2; every move the structure draft needs
  (Tuxedos, Sport Coats, Top Coats, Sweaters, the vests' leaves) is a leaf.
- **Tags**: Item and ItemMatrix list `TagRelations` / `TagRelations.Tag` as
  relations only; the Tag endpoint renames or deletes a tag ("removed from all
  records"). No documented way to put a tag on an item; the scope is
  `employee:tags` ("View, create, update, and archive product tags").
  `load_relations ["Tags","TagRelations"]` is accepted on Item and ItemMatrix;
  no item or matrix 7 has tags. **Unconfirmed**: the write form (the older
  API's `{"Tags": {"tag": [...]}}` is used) and the read shape.
- **Vendors**: every one of the 677 vendors has `updatePrice`, `updateCost`,
  `updateDescription` and `shareSellThrough` false (snapshot 2026-10-26_221615),
  so "Update From Catalog" is already off everywhere.
- **UPCs**: none of the 830 items has a `upc`, `ean` or `manufacturerSku`. The
  `upc` filter is applied by the API (a `>,0` filter returned nothing, where an
  ignored filter would return every item).
- Inventory log `reason` values seen: `removeInventoryForTransaction`,
  `removeInventoryForLayaway` (sales and layaways).

### Decisions

- **Shared vendor codes**: the structure draft keeps a vendor's style-color
  code (Renoir `201-19`) on every size and fit and across garments of one
  fabric, matching by matrix + attributes. The one-item-per-number rule is gone;
  one number per vendor per item stays. Cost: Lightspeed's own Vendor ID
  lookups (PO import) find several items.
- **Stock** gets its own route (`ItemShops` only, one row by `itemShopID`, never
  row 0, whole-number qoh) and tool; the confirm code covers the stock read, so
  a sale after the dry run is never overwritten. One item per call until a
  first change is logged done.
- **Tags**: lowercase-hyphen names; the whole list is sent; one item per call
  until a first tag write is logged done.

### To confirm on the first real writes (each approved by the owner)

1. `adjust_stock` on one item: snapshot before/after, `get_stock_history`
   (reason recorded, cost change).
2. `move_category` (e.g. Suits/Tuxedos to the top level): its path, and its
   13 items and 2 matrices still in it.
3. `set_item_tags` on one item, after re-running bootstrap for `employee:tags`:
   the tags read back with `load_relations ["Tags","TagRelations"]`.
4. `set_upcs` / `reassign_items`: plain Item PUTs of documented fields.

## Rules from the draft, batch archiving, sibling tags (2026-10-27)

The store rules now follow the owner's draft inventory structure (§2 matrix types,
§3 category tree, §4 garment types); the owner asked for it after reviewing which
tools the restructure needs. No real write was made.

- **Garment table** (`rules.GARMENTS`): 16 types, each with a matrix type, a size
  format, the words a matrix description names it with, and the category names
  that hold it. Accessories (belt, accessory) are the draft's provisional §4.15.
- **Matrix types** are matched by attribute names in order, not set IDs: MT1
  Color/Size/Fit = set 5, MT2 Color/Size/Width = a store-made set (set 6, created
  2026-10-27; before that shoe matrices were refused), MT3 Color/Size = built-in 1, MT4
  Color = built-in 3. This replaces the owner's 2026-10-26 rule "never a built-in
  set": the draft uses them for MT3/MT4. Renaming a set's attributes changes its
  matrix type (the dry run says so).
- **Garment type of an existing matrix**: its category (nearest level that names
  a garment type), else its description (specific phrases first; a bare "jacket"
  decides nothing). Live: matrix 7 "Caravelli" (Suits/Suit) is a suit; 9 "Angelo
  Rossi" (Suits/Suit, built-in Color) and 10 "Caravelli Tux" (Suits/Tuxedos,
  built-in Color) are on the wrong type, so they take no new items (rebuild).
- **create_matrix** refuses a category that holds another garment type (the draft:
  "the leaf is the garment type"), and picks the set itself. In today's tree,
  tux jackets, outerwear jackets, casual vests, casual pants and tux shirts have
  no category yet: create them (create_category / move_category) first.
- **Sizes**: only new size values are checked (sizes a matrix already has are left
  alone). Portly is a Fit value ("Portly", Mantoni) until the draft decides; a
  length code like 44PS is refused for now.
- **Fit is never blank** on new items: "Standard" when the vendor names none (§2
  rule 4). Matching existing items still accepts a blank fit (legacy items).
- **archive_items**: archive_item's checks for up to 100 items; one per call until
  an archive (either tool) is logged done.
- **Sibling tags**: new variants get the tags every active item of the matrix has,
  only after a set_item_tags write is logged done (before that, a warning names
  them), so an unconfirmed tag format can't make an item POST fail.

## Tooling

- `check_tools.py` calls every read tool over stdio against the live account (GET only, 29 calls, about 90 s).
  `check_tools.py --dry-runs` calls every write tool on real records, always with `dry_run=true`.
- Unit tests: `.venv\Scripts\python.exe -m pytest` (715 tests).
