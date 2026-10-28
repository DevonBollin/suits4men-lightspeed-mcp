<!-- Copy of the owner's draft, published at https://claude.ai/artifact/1DcijHbP8xyUsLLHcUzrqS (version 1792627047-7b15), saved 2026-10-27. -->

# Suits 4 Men — Lightspeed R-Series Inventory Structure (Draft)

Status: working sketch. Items marked **TBD** or **Confirm** are open.

---

## 1. Settled decisions

1. One store, Lightspeed R-Series. Every sellable unit is its own matrix item with its own system SKU.
2. Every matrix uses one of the store's **matrix types** (attribute sets, §2). Each garment type is assigned exactly one matrix type in its definition (§4).
3. **Color** and **Fit** values use the vendor's own naming. No normalization to store terms.
4. **Size** values use a fixed store format per garment type (§4). Size is the one attribute with a store-defined grammar.
5. **No 3-piece items.** Every 3-piece suit or tuxedo is stored as a 2pc item plus a vest item, regardless of vendor or brand. "3-piece" exists only at the register, as two items sold together.
   - Vendor sells the vest separately → received as 2pc and vest, as invoiced.
   - Vendor sells 3pc as one SKU → the slip line is expanded at intake into one 2pc item and one vest item, with the invoiced cost split between them (§11.2a).
   - Vests are sized by jacket size (`40R`) and use the same Color and Fit values as the line's suits, so a suit and its vest match one to one.
   - No assembly matrices.
6. Pants are separate items with their own matrices. All pants are stocked uncut; pant size is waist only.
7. **Matrix boundary:** one matrix = one vendor line (fabric family) × one garment type. Each matrix therefore has exactly one brand, one vendor, one category, one default price, and one default cost. Shoes are the exception in form: one matrix per style (§7).
8. **Variants are created on receipt, not as a full grid.** A matrix holds only combinations that have been stocked at least once.
9. **Categories hold garment type only** (§3). Fabric, finish, and construction go in tags. Price tier is expressed by the matrix. Brand is the Brand field.
10. **Matching key at intake:** vendor + vendor code + garment type + fit + size. Garment type is supplied by the matrix's category. Claude matches on matrix plus attributes, not on Vendor ID alone, because vendors reuse one fabric code across suits, tuxes, blazers, and pants.
11. No boys' items.
12. Accessories are deferred to last (§9).
13. **Intake runs through the Lightspeed API via a custom connector.** Claude reads the packing slip or invoice, proposes, and on approval creates variants, creates a purchase order, and receives it (§11). The approval step is the proposal, before anything is written to Lightspeed.
14. **"Update From Catalog" is off for every vendor**, so that no catalog reimport overwrites plain-language descriptions, prices, or costs.

---

## 2. Lightspeed mechanisms and fields

### Division of labor

| Mechanism | Job | Exclusive? |
|---|---|---|
| Category | Garment type | Yes — one per matrix |
| Matrix | Vendor line within a garment type | Yes |
| Brand | Label | Yes |
| Tags | Fabric, finish, construction, season (e.g. `wool`, `super-150s`, `non-iron`, `stretch`, `half-canvas`) | No — any number |

### Matrix types

A matrix type is a Lightspeed matrix attribute set. Lightspeed allows up to three attributes per set and provides Color, Size, and Color/Size as built-in sets; the others are custom sets created once under Inventory > Matrix Attribute Sets.

| Matrix type | Attributes (in order) | Lightspeed set | Used by |
|---|---|---|---|
| **MT1 — Tailored** | Color, Size, Fit | Custom `Color/Size/Fit` (exists) | Suits, tuxedos, tux & dinner jackets, sport coats, suit and tux vests, top coats, dress pants, dress and tux shirts |
| **MT2 — Footwear** | Color, Size, Width | Custom `Color/Size/Width` (to create) | Shoes |
| **MT3 — Sized** | Color, Size | Built-in `Color/Size` | Garment types with no fit dimension: casual jackets, casual vests, casual pants and denim, sweaters; belts (deferred) |
| **MT4 — Color only** | Color | Built-in `Color` | One-size items tracked by color: ties, bow ties, pocket squares, suspenders, scarves (deferred) |
| **Single item** | — (no matrix) | — | Price-point items, services, and anything not tracked by variant |

Rules:

1. Each garment type has exactly one matrix type (§4). All matrices of that garment type use it.
2. The matrix type is chosen when the matrix is created. Changing it later means building a new matrix and migrating its items. **Confirm** whether Lightspeed allows changing the attribute set of a matrix that already has items.
3. Attribute order is always the order shown above, so generated item names read the same way across the store.
4. **Single-value fallback:** when a vendor line has only one fit and the vendor gives it no name, Fit is `Standard`. Width has no fallback; shoe widths are always printed.
5. A garment type that needs a dimension its matrix type lacks is a new matrix type, added to this table, not an overloaded attribute.

### Matrix-level fields (shared by all items in the matrix)

| Lightspeed field | Holds | Example |
|---|---|---|
| Description | Plain-language name (§2 Naming) | `Renoir Suit` |
| Attribute set | Color / Size / Fit | — |
| Brand (Manufacturer) | Brand on the label | Renoir |
| Default Vendor | Distributor invoiced by | Renoir Fashions |
| Category | Leaf garment-type category (§3) | Suits |
| Default price / cost | Line price point | — |
| Tags | Fabric / finish / construction | `wool`, `super-140s` |

### Item-level fields

| Lightspeed field | Holds | Example |
|---|---|---|
| Attribute 1 — Color | Vendor color name | New Navy |
| Attribute 2 — Size | Store size format (§4) | 40R |
| Attribute 3 — Fit (or Width for shoes) | Vendor fit / style name, or width code; absent in two-attribute matrix types | Slim |
| Vendor ID | Vendor's code for the item, as printed on slips. Not required to be unique per item; Claude matches on matrix + attributes | `201-19` |
| UPC | Manufacturer barcode where one exists (shoes) | — |
| Manufacturer SKU | Unused | — |
| Custom SKU | Existing legacy numeric codes; leave as-is | — |

Vendor ID and Manufacturer SKU are empty across the current inventory, so both are free for this scheme.

### Naming

Lightspeed generates item descriptions as `Matrix description + Attr1 + Attr2 + Attr3`. Individual matrix item names cannot be edited directly; they change only through the matrix description or the attribute values. Item names are therefore controlled by writing the matrix description in plain language.

**Matrix description rules**

1. Format: `{Brand} {Line name} {Garment}`.
2. **Line name** is a plain-language word for the line (Wool, Linen, Stretch, Premium Wool), never the vendor code. The vendor code is stored in Vendor ID.
3. Line name is included only when the brand has more than one line in the same garment type. A brand with one suit line is `{Brand} Suit`.
4. **Garment** is written out: Suit, Tuxedo, Peak Lapel Tuxedo, Sport Coat, Blazer, Vest, Dress Pants, Dress Shirt, Tuxedo Shirt.
5. Lapel or model is written where it distinguishes matrices (Notch Lapel, Peak Lapel, Shawl, Double-Breasted, Arlan).
6. Shoes: `{Brand} {Style}` (e.g. `Stacy Adams Dickinson`). The style name stands in for the garment word.
7. Every matrix description must be distinct across the store, since it is the only part of an item name that distinguishes two matrices.

With the attribute order fixed as Color, Size, Fit:

`Renoir Suit New Navy 40R Slim`
`Mantoni Wool Suit Navy 42L Euro Slim`
`Mantoni Vest Navy 42L Euro Slim`
`Marquis Dress Shirt White 15.5 34/35 Slim`

Attribute order is set per matrix and must be applied consistently when matrices are created.

---

## 3. Category tree

Every matrix sits in exactly one leaf category, and the leaf is the garment type. The leaf category identifies the garment type, whose definition (§4) gives the matrix type and the size and fit grammar, so intake goes: slip line → garment type → category → expected grammar.

| Department | Leaf categories |
|---|---|
| Suits | (single leaf) |
| Tuxedos | Tuxedos, Tux & Dinner Jackets |
| Sport Coats & Blazers | (single leaf) |
| Outerwear | Top Coats, Jackets |
| Vests | Suit Vests, Tux Vests, Casual Vests |
| Pants | Dress Pants, Casual Pants & Denim |
| Shirts | Dress Shirts, Tux Shirts |
| Sweaters | (single leaf) |
| Shoes | Dress, Boots, Casual |
| Accessories | Deferred (§9) |
| Services | Alteration, Shipping, Special Order |

### Changes from the current tree

- Tuxedos moves out from under Suits; tux jackets, vests, and shirts did not fit under Suits.
- "Coats" splits into Sport Coats & Blazers and Outerwear.
- "Dress Shirts, Sweaters, & Vests" splits into Shirts, Sweaters, and Vests.
- Dress shirt subcategories (100% Cotton, Dress Shirt, Non Iron, Standard, Ultra Stretch) are removed. They classified fabric in an exclusive slot. Fabric and finish move to tags. These nodes hold 4 items today, so removal costs nothing.
- Vests > Specialty / Standard are replaced by garment-type leaves.

---

## 4. Garment type definitions

A garment type is defined once, here. Every matrix belongs to one garment type and inherits its matrix type, size format, fit meaning, matrix boundary, naming, and tags. Adding a garment type means filling in the template in §4.0; adding a line or vendor within an existing garment type requires no change to this section.

Size ranges are the store's full carried range. A given matrix uses only the subset its vendor makes, and only the variants actually received (§1.8).

### 4.0 Template

| Field | Content |
|---|---|
| Category leaf(s) | Leaf category or categories in §3 |
| Matrix type | MT1–MT4 or Single item (§2) |
| Size format and range | Store format for the Size value, with the carried range |
| Fit / Width meaning | What the third attribute holds; `—` for two-attribute types |
| Matrix boundary | What one matrix contains (normally vendor line × garment type) |
| Garment word | Word(s) used in matrix descriptions (§2 Naming) |
| Standard tags | Tags applied at matrix level |
| Related types | Garment types whose items must share attribute values with this one |
| Notes | Exceptions, vendor-specific rules |

### 4.1 Suits

| Field | Content |
|---|---|
| Category leaf | Suits |
| Matrix type | MT1 — Color, Size, Fit |
| Size format and range | `{chest}{length}`, e.g. `40R`. S 32–50, R 34–64, L 38–64, XL 40–60. Portly: Short 40–66, Regular 42–68 |
| Fit meaning | Vendor fit name (Classic, Slim, Euro Slim, Savvy Slim, Modern, …) |
| Matrix boundary | Vendor line (fabric family) × garment type; lapel and double-breasted models are separate matrices |
| Garment word | Suit; Double-Breasted Suit; Peak Lapel Suit |
| Standard tags | Fabric (`wool`, `poly-viscose`, `linen`, …), grade (`super-140s`), `stretch` |
| Related types | Suit Vests (§4.5): same Color, Size, Fit values per line |
| Notes | 3-piece suits are suit + vest (§1.5). Even chest sizes are the norm; Eisenberg carries odd sizes (41L, 43L). **TBD** Portly encoding: Fit value (`44R`, Fit = Portly) or length code (`44PS`, `44PR`) |

### 4.2 Tuxedos

| Field | Content |
|---|---|
| Category leaf | Tuxedos > Tuxedos |
| Matrix type | MT1 |
| Size format and range | As Suits (§4.1) |
| Fit meaning | Vendor fit name |
| Matrix boundary | Vendor line × model (notch, peak, shawl, double-breasted, tailcoat, named models such as Arlan or Oscar) |
| Garment word | Tuxedo, with lapel or model: Notch Lapel Tuxedo, Peak Lapel Tuxedo, Shawl Tuxedo, Double-Breasted Tuxedo, Tailcoat, Arlan Tuxedo |
| Standard tags | Fabric, `stretch` |
| Related types | Tux Vests (§4.5) |
| Notes | Tux + vest SKUs (e.g. Oscar Tux+Vest) are expanded at intake (§1.5) |

### 4.3 Tux & Dinner Jackets

| Field | Content |
|---|---|
| Category leaf | Tuxedos > Tux & Dinner Jackets |
| Matrix type | MT1 |
| Size format and range | As Suits (§4.1) |
| Fit meaning | Vendor fit name |
| Matrix boundary | Vendor line × model |
| Garment word | Tuxedo Jacket; Dinner Jacket; Velvet Tuxedo Jacket |
| Standard tags | Fabric |
| Related types | — |
| Notes | Jacket only; no pants |

### 4.4 Sport Coats & Blazers

| Field | Content |
|---|---|
| Category leaf | Sport Coats & Blazers |
| Matrix type | MT1 |
| Size format and range | As Suits (§4.1) |
| Fit meaning | Vendor fit name; `Standard` if none |
| Matrix boundary | Vendor line × garment word |
| Garment word | Sport Coat; Blazer; Soft Jacket; specialty jackets by material (Velvet Jacket, Vegan Leather Jacket, Vegan Suede Jacket) |
| Standard tags | Fabric |
| Related types | — |
| Notes | Where leftover jackets from broken suits go is **TBD** (§12.1, §13) |

### 4.5 Suit Vests and Tux Vests

| Field | Content |
|---|---|
| Category leaf | Vests > Suit Vests; Vests > Tux Vests |
| Matrix type | MT1 |
| Size format and range | Jacket size, as Suits (§4.1) |
| Fit meaning | The related suit or tuxedo line's Fit values |
| Matrix boundary | One vest matrix per suit or tuxedo line that has vests |
| Garment word | Vest; Tuxedo Vest; Satin Vest |
| Standard tags | As the related line |
| Related types | Suits (§4.1), Tuxedos (§4.2): identical Color, Size, Fit values so a suit and its vest match one to one |
| Notes | Vests from vendor 3pc SKUs and vests sold separately go to the same matrix |

### 4.6 Casual Vests

| Field | Content |
|---|---|
| Category leaf | Vests > Casual Vests |
| Matrix type | MT3 — Color, Size |
| Size format and range | Alpha: `S`, `M`, `L`, `XL`, `2XL`, … |
| Fit meaning | — |
| Matrix boundary | Vendor × model |
| Garment word | Puffer Vest; Vest |
| Standard tags | Fabric |
| Related types | — |
| Notes | e.g. Pellagio Puffer Vest |

### 4.7 Top Coats

| Field | Content |
|---|---|
| Category leaf | Outerwear > Top Coats |
| Matrix type | MT1 |
| Size format and range | `{chest}{length}`, as Suits (§4.1); vendor ranges vary (Pellagio Pioggia S38–44, R36–54, L42–50) |
| Fit meaning | Vendor fit name; `Standard` if none |
| Matrix boundary | Vendor × model |
| Garment word | Topcoat; Car Coat; Raincoat |
| Standard tags | Fabric (`wool-cashmere`, …) |
| Related types | — |
| Notes | Top coats sold in alpha sizes belong under Jackets (§4.8) |

### 4.8 Jackets (outerwear)

| Field | Content |
|---|---|
| Category leaf | Outerwear > Jackets |
| Matrix type | MT3 — Color, Size |
| Size format and range | Alpha: `S`–`4XL` |
| Fit meaning | — |
| Matrix boundary | Vendor × model (Enzo Tovare Ace, Kami, Play, …) |
| Garment word | `{Model} Jacket` |
| Standard tags | Fabric, `reversible`, `quilted` |
| Related types | — |
| Notes | — |

### 4.9 Dress Pants

| Field | Content |
|---|---|
| Category leaf | Pants > Dress Pants |
| Matrix type | MT1 |
| Size format and range | Waist only, e.g. `34`. Slim 28–50, Classic 30–60. Stocked uncut |
| Fit meaning | Vendor pant style, which may combine cut and pleat (Gruppo: Non Pltd, 1 Pltd, Slim) |
| Matrix boundary | Vendor line × garment type |
| Garment word | Dress Pants; Linen Pants; Cotton Pants |
| Standard tags | Fabric |
| Related types | Suits (§4.1): leftover pants from a broken suit go to the line's Dress Pants matrix (§12.1) |
| Notes | **Confirm** even waists only or every inch |

### 4.10 Casual Pants & Denim

| Field | Content |
|---|---|
| Category leaf | Pants > Casual Pants & Denim |
| Matrix type | MT3 — Color, Size |
| Size format and range | Waist only, e.g. `34`; 30–46 for Enzo Tovare |
| Fit meaning | — |
| Matrix boundary | Vendor × model (Albert, Alpha, Berlin, Beta, Milan, Tokyo, …) |
| Garment word | Denim; Stretch Pants; model name |
| Standard tags | `denim`, `stretch`, `5-pocket` |
| Related types | — |
| Notes | Exception to check: Pellagio stretch pants are cataloged in Short / Regular / Long; if they ship hemmed, Size is `34S` / `34R` / `34L` |

### 4.11 Dress Shirts

| Field | Content |
|---|---|
| Category leaf | Shirts > Dress Shirts |
| Matrix type | MT1 |
| Size format and range | `{neck} {sleeve}`, decimal necks and slash sleeves, e.g. `15.5 34/35`. Grids per vendor below |
| Fit meaning | Vendor style options for the code, including cuff or collar options listed under one code (Mantoni `M20001-1`: Classic, Slim, French Cuff) |
| Matrix boundary | Vendor line × shirt type |
| Garment word | Dress Shirt; Cotton Dress Shirt |
| Standard tags | `100-cotton`, `non-iron`, `stretch`, `easy-care` |
| Related types | — |
| Notes | Collar shapes sold under separate codes are separate matrices. Size strings are comparable only within a vendor |

**Marquis — Slim**

| Sleeve | Necks |
|---|---|
| 32/33 | 14.5, 15.5, 16.5, 17.5, 18.5 |
| 34/35 | 15.5, 16.5, 17.5, 18.5 |
| 36/37 | 15.5, 16.5, 17.5, 18.5 |

**Marquis — Classic**

| Sleeve | Necks |
|---|---|
| 32/33 | 14.5, 15.5, 16.5, 17.5 |
| 34/35 | 15.5, 16.5, 17.5, 18.5, 19.5, 20.5 |
| 36/37 | 16.5, 17.5, 18.5, 19.5, 20.5, 22, 24 |
| 38/39 | 18.5, 19.5, 20.5, 22, 24 |

**Vitarelli** (fit **TBD**)

| Sleeve | Necks |
|---|---|
| 32/33 | 14.5, 15.5, 16.5, 17.5, 18.5 |
| 34/35 | 14.5, 15.5, 16.5, 17.5, 18.5, 19, 20, 22, 24 |
| 36/37 | 14.5, 15.5, 16.5, 17.5, 18.5, 19, 20, 22, 24 |

### 4.12 Tux Shirts

| Field | Content |
|---|---|
| Category leaf | Shirts > Tux Shirts |
| Matrix type | MT1 |
| Size format and range | As Dress Shirts (§4.11) |
| Fit meaning | Vendor style options, including collar (Slim, Classic, Wing Tip) and bib (Ribbed Bib, Pique Bib) |
| Matrix boundary | Vendor line × shirt type |
| Garment word | Tuxedo Shirt |
| Standard tags | Fabric |
| Related types | — |
| Notes | Usually white only; Color still recorded |

### 4.13 Sweaters

| Field | Content |
|---|---|
| Category leaf | Sweaters |
| Matrix type | MT3 — Color, Size |
| Size format and range | Alpha: `S`–`3XL` — **Confirm** |
| Fit meaning | — |
| Matrix boundary | Vendor × model |
| Garment word | Sweater; Quarter-Zip; Cardigan |
| Standard tags | Fabric |
| Related types | — |
| Notes | No active sweater line identified (DeNiko is disabled) |

### 4.14 Shoes

| Field | Content |
|---|---|
| Category leaf | Shoes > Dress; Shoes > Boots; Shoes > Casual |
| Matrix type | MT2 — Color, Size, Width |
| Size format and range | Decimal, e.g. `10.5`. Medium 7–16, Wide 8–16. Per style: the vendor's printed run |
| Width meaning | Vendor width code as printed. Stacy Adams: `M`, `W`; Classics line `D`, `3E` |
| Matrix boundary | One matrix per style |
| Garment word | Style name stands in (`Stacy Adams Dickinson`) |
| Standard tags | Material (`leather`, `suede`, `patent`) |
| Related types | — |
| Notes | Leaf by the catalog's style description: *Boot* → Boots; *Sandal*, *Sneaker*, *Driving Moc*, *Perfed* → Casual; otherwise Dress. Stacy Adams runs: half sizes within the hyphenated range, whole sizes after; sandals whole sizes only. Florsheim and Johnston & Murphy runs and widths: **TBD** |

### 4.15 Accessories (deferred, provisional)

| Group | Matrix type | Size format | Notes |
|---|---|---|---|
| Ties, bow ties, pocket squares, suspenders, scarves | MT4 (Color) where tracked by color; otherwise Single item | — | Decide per group when accessories are designed (§9) |
| Belts | MT3 (Color, Size) | Waist, e.g. `36` |   |
| Cufflinks, stays, pins, collar extenders, shoe horns, garment bags | Single item | — | Price-point items |

---

## 5. Vendors and brands

Reconstructed from the Lightspeed vendor list, the current inventory export, and the catalogs on hand.

**Status** is confirmed: every vendor in §5.1 and §5.2 is active. Vendors marked *Active — lines TBD* have no current lines in the inventory data; their brands and lines still need identifying (§13).

### 5.1 Garment and shoe vendors

| Vendor (canonical) | Lightspeed record(s) | Brands | Status | Cleanup |
|---|---|---|---|---|
| San Malone | `San Malone` | Caravelli, Cianni | Active | Also on unbranded "Vest" and "J60313 Sport Coat" |
| Gruppo Bravo | `Gruppo Bravo Menswear`, `GRUPPO BRAVO` (both enabled); `gruppo brave`, `grup`, `MANTO` (disabled) | Mantoni, Giorgio Fiorelli, Enzo Tovare, Dray Scott, Manchester & Tailor (catalog also: Carlo Lusso, Woolston, Bertolini) | Active | Merge into one record. Brand "Enzo" → "Enzo Tovare"; "Manchester" → "Manchester & Tailor". Recorded as vendor on 7 Stacy Adams shoe items (Brayden, Bryant, Sync Black, Karcher) — **Confirm** |
| Renoir Fashions (Novara Fashion Inc) | `Renoir Fashions`; `RENOI` (disabled) | Renoir, Rivelino, Alessandro Vitello, Pellagio | Active | Brand "Renoir Wool" is a tier, not a brand (§13 Q1) |
| Weyco Group | `Weyco Group Inc.`, `Stacy Adams` (both enabled) | Stacy Adams, Florsheim | Active | Merge `Stacy Adams` record into Weyco unless ordering through a separate account. Johnston & Murphy "Upton Tan" wrongly on Weyco |
| Johnston & Murphy | `Johnston & Murphy` | Johnston & Murphy | Active | — |
| Peerless Clothing | `Peerless Clothing`; `PEERL` (disabled) | Calvin Klein, DKNY, Kenneth Cole, Michael Kors, Ralph Lauren, Tallia, Tommy Hilfiger, Marc New York, TailoRED | Active | "Tommmy Hilfiger" typo; Calvin Klein Tuxedo has no brand; "Designer Outerwear" branded Enzo under Peerless. **Confirm** which brands are still bought |
| Eisenberg | `Eisenberg` | Eisenberg | Active | Duplicate items (§6) |
| Paris By Night | `Paris By Night ` (trailing space) | Retro Paris (Luciano), Sharp (Capucci), Venezia | Active | Remove trailing space in record name |
| Retro Paris | `Retro Paris` | Retro Paris | Active | Separate phone number from Paris By Night; **Confirm** whether same company |
| Marina Imports | `Marina Imports` | Giorgio Sanetti, Mundo, Tweed | Active | "Tweed" is a fabric, not a brand |
| Tiglio | `Tiglio` | Tiglio, Canaletto | Active | — |
| Cosani | `Cosani` | Angelo Rossi | Active | — |
| The Suit Co. | `The Suit Co.` | Vitarelli (suits, pants) | Active | — |
| Jade Eastern Trading | `Marquis / Jade Eastern Trading Inc.` (used), `Marquis /Jade Eastern Trading Inc` (unused) | Marquis, Vitarelli (shirts) | Active | Merge records. Split combined brand "Marquis / Vitarelli" |
| Harmony Clothes | `Harmony Clothes` | Harmony | Active | — |
| PB Aristo | `PB Aristo Inc. - Paul Betenly` (used), `PB Aristo Inc - Paul Betenly` (unused); `Natsun America Co - Paul Betenly` (disabled) | Paul Betenly | Active | Merge records |
| Desen Suiting Company | `Desen Suiting Company` | Desen | Active | — |
| L.A. Gibbs Resources | `L.A.Gibbs Resources Inc.` (used), `L.A. Gibbs Resources Inc`, `L.A. Gibbs Resources Inc.` (unused) | L.A. Gibbs, Ascott Brown | Active | Merge three records |
| Angelo | `Angelo` | Marco Valentino | Active | "Marco Valetino" / "Marco Valenino" typos |
| Christopher Lena Shirt Co | `Christopher Lena Shirt Co` | Christopher Lena | Active | Brand name typed into Vendor ID |
| MM Fashion & Design | `MM Fashion & Design Inc.` (used), `MM Fashion & Design Inc` (unused) | — | Active — lines TBD | Legacy items only |
| Braveman | `Braveman Enterprise` (used), `USA Braveman Garment Inc` (unused) | — | Active — lines TBD | Legacy item only |
| Nevima International | `Nevima International` | — | Active — lines TBD | Legacy item only |
| Coppley | `Coppley Corp` | — | Active — lines TBD | Enabled, no items |
| Forsyth of Canada | `Forsyth of Canada` | — | Active — lines TBD | Enabled, no items |
| Dazi | `Dazi` | — | Active — lines TBD | Enabled, no items |
| TBD | — | LaMilano | Active — lines TBD | Shoe item with no vendor |

### 5.2 Accessory vendors

| Vendor | Lightspeed record(s) | Status | Cleanup |
|---|---|---|---|
| Brand Q | `Brand Q` | Active | Also used as a brand |
| Jimmy Sales Corp | `Jimmy Sales Corp` | Active | — |
| TW & Company | `TW & Company` | Active | — |
| CKL | `CKL` | Active | — |
| Garment Group | `Garment Group Inc` | Active | — |
| Imani Uomo | `Imani Uomo Inc` | Active | — |
| D&L Apparel | `D&L APPAREL` | Active | — |
| Ties, Boxers & Socks | `Ties, Boxers & Socks` (disabled, used by items), `Ties, Boxers, and Socks` (enabled, unused) | Active | Repoint items to the enabled record |
| Daniel Elissa | `Daniel Elissa` (disabled) | Inactive | Item still points at it |
| Pacific Silk | `Pacific Silk` | Active — lines TBD | Enabled, no items |
| Trans Pacific Wholesale | `Trans Pacific Wholesale` | Active — lines TBD | Enabled, no items |
| Top Circle Hosiery Mills | `Top Circle Hosiery Mills` | Active — lines TBD | Enabled, no items |

### 5.3 Inactive vendors

Disabled in Lightspeed **with** current items (archive the items): Actex International, Ballin, DeNiko, Importex, Lanier Clothes, Marcraft Apparel Group (Jones New York), P&L Import Group, Phita International.

Disabled **without** items (no action): Alain Dupetit, Atica Brand, Bernardi of California, Karl Knox (2 records), Klotho, Natsun America, Perry Ellis – Savane, PVH Corporation, Shanghai Shenda America, Statement Clothing, The Sun's Group, Unico Fashion Group, and fragment records `MAQ`, `MANTO`, `grup`, `gruppo brave`, `RENOI`, `PEERL`.

### 5.4 Lightspeed industry catalog vendors

603 of the 677 vendor records are Lightspeed vendor-catalog vendors (Billabong, Patagonia, Carhartt, etc.), plus a `TEST` record. They were added when product industries were enabled in account settings; enabling an industry adds all its vendors and their catalogs to the account. None are store vendors.

- **Removal:** turn off the unneeded product industries rather than disabling 603 records individually. **Confirm** that disabling an industry removes vendors already added before doing anything by hand.
- **Vendor catalogs worth checking:** Lightspeed's catalog library and the NuORDER catalog integration (products with images, descriptions, and UPCs) may include Stacy Adams, Florsheim, or Johnston & Murphy. If so, shoe matrices and UPCs can be built from that data. Suit vendors are unlikely to be present.
- **Update From Catalog:** off for every vendor (§1.14).

### 5.5 Catalog notes

- Novara Four Seasons catalog: Rivelino = `RHC100` line; Alessandro Vitello = `508`, `508B`; unbranded pages (`201`, `202`, `203`, `207`, `2106`, `2110`, `290`, `561`, `611`) assumed Renoir — **Confirm**.
- Pellagio: "A Division of Novara Fashion."
- Not yet reviewed: Stacy Adams catalog; separate Renoir catalog.
- No catalog yet: San Malone (Caravelli), Peerless, Jade Eastern Trading, and all other current vendors.

---

## 6. Current state

### Existing matrices

| Matrix | Category | Matrix default price | Observed state |
|---|---|---|---|
| Caravelli | Suits / Suit | $61.21 | Working Color/Size/Fit matrix, ~470 items at $599.95. **Default price is wrong** (looks like a cost); "Reset Items To Defaults" would reprice every item. Fit values: Classic, Slim. Holds 2pc and 3pc mixed with no distinction in item names. Under §1.5 the suit items stay as Caravelli Suit; vests from 3pc stock are counted into Caravelli Vest (§7.5). Navy (11) and Midnight Blue (76) are both genuine, distinct San Malone colors. "Navy-Str" (1 item) belongs in a separate stripe line. "Caravelli Navy 42R Slim" exists twice (Custom SKU 2740, 5919). |
| Giorgio Fiorelli | Suits / Suit | $549.95 | No items visible in export; empty or archived |
| Angelo Rossi | Suits / Suit | $549.95 | Appears color-only (no size/fit) |
| Caravelli Tux | Suits / Tuxedos | $549.95 | Appears color-only; items at $599.95 |
| Giorgio Fiorelli Tuxedo | Suits / Tuxedos | $499.95 | Appears color-only |

### Inventory models in use

1. **Itemized matrix** — Caravelli only. This is the target structure.
2. **Price-point placeholders** — one item per brand per garment type (e.g. "Mantoni Suit," "Renoir Wool Suit," "Tallia Pants"), with negative quantities. Most non-Caravelli inventory. Converting these into model 1 is the bulk of the project.
3. **Legacy one-off items** — style codes in names ("C602F Suit Navy 56R Classic"), mostly from inactive vendors. Archive during cleanup.

The item-by-item migration from these models to the target structure is in Appendix C.

### Existing price tiers (inform matrix boundaries)

| Brand / line | Suit retail |
|---|---|
| Caravelli | $599.95 |
| Renoir Suit | $599.95 (likely `201` line) |
| Renoir Wool Suit | $795.95 (likely `508` line) |
| Renoir Premium Wool Suit | $1,199.95 (likely `RHC100` Rivelino) |
| Giorgio Fiorelli, Manchester & Tailor | $599.95 |
| Mantoni | $795.95 |
| Enzo Tovare | $1,099.95 |
| Dray Scott | $1,495.95 |

---

## 7. Matrices by category

Matrix descriptions follow the naming rules in §2. Vendor codes are listed for reference and are stored in Vendor ID, not in the description. Lists are candidates drawn from catalogs and current inventory; only lines actually stocked get matrices. Current retail by brand and garment, for default prices: Appendix A.

### 7.1 Suits

| Matrix description | Brand | Vendor | Vendor codes | Fits (vendor names) |
|---|---|---|---|---|
| Caravelli Suit | Caravelli | San Malone | TBD (no catalog) | Classic, Slim — existing matrix; vests from its 3pc stock move to Caravelli Vest |
| Rivelino Suit | Rivelino | Renoir Fashions | `RHC100-{1,2,3,4,19}` | Classic (`CL`), Modern (`MD`) |
| Alessandro Vitello Wool Suit | Alessandro Vitello | Renoir Fashions | `508-*`, `555-3` | Classic, Slim |
| Alessandro Vitello Wool Blend Suit | Alessandro Vitello | Renoir Fashions | `557-1` | Slim only (70/30 wool-poly) |
| Alessandro Vitello Wool Stretch Suit | Alessandro Vitello | Renoir Fashions | `508B-*`, `555B-3` | Classic, New Slim |
| Renoir Suit | Renoir | Renoir Fashions | `201-*`, `202-*`, `203-*`, `207-*`, `2106-3` | Classic, Slim, New Slim — **Confirm** one price point |
| Renoir Stretch Suit | Renoir | Renoir Fashions | `2110-*` | Slim |
| Renoir Linen Suit | Renoir | Renoir Fashions | `611-*` | New Slim |
| Mantoni Wool Suit | Mantoni | Gruppo Bravo | `M40901-*`, `M46306-*`, `M46500/01-*`, `M78200-*`, `M87xxx-*` | 2BV No P, Euro Slim, Savvy Slim, Portly |
| Mantoni Wool Double-Breasted Suit | Mantoni | Gruppo Bravo | `M40901-*`, `M46306-*`, `M87184-*`, … (DB model) | — |
| Mantoni Stretch Suit | Mantoni | Gruppo Bravo | `MS4000-*` | — |
| Mantoni Tweed Suit | Mantoni | Gruppo Bravo | `M87905-*` | — |
| Enzo Tovare Wool Suit | Enzo Tovare | Gruppo Bravo | `E59663-*`, `E846xx-*` | Savvy Semi Slim, … |
| Enzo Tovare Linen Suit | Enzo Tovare | Gruppo Bravo | `E58302-*`, `E85302-*` | — |
| Dray Scott Suit | Dray Scott | Gruppo Bravo | `D300xx-*` | Savvy Slim, Savvy Semi Slim |
| Giorgio Fiorelli Suit | Giorgio Fiorelli | Gruppo Bravo | `G47815-*`, `G49412-*`, `G45001-*`, `G797xx-*`, `G66046-*` | Euro Slim, Portly, … |
| Giorgio Fiorelli Stretch Suit | Giorgio Fiorelli | Gruppo Bravo | `G49500-*`, `GS797xx-*` | Euro Slim |
| Carlo Lusso Suit | Carlo Lusso | Gruppo Bravo | `C67901-*` | Euro Slim |
| Woolston Suit | Woolston | Gruppo Bravo | `WS110xx-*` | Euro Slim |
| Bertolini Suit | Bertolini | Gruppo Bravo | `B6605x-*` | Euro Slim |
| Manchester & Tailor Suit | Manchester & Tailor | Gruppo Bravo | `T30110-*` | Manchester Slim |
| Pellagio Traveller Suit | Pellagio | Renoir Fashions | `PS25-16`, `PF24-15/16`, `PF20-17/18` | — (drawstring pant; paired jacket/pant sizes, e.g. `R40/34`) — **TBD** size format |
| Eisenberg Suit | Eisenberg | Eisenberg | TBD | TBD; odd sizes |
| Angelo Rossi Suit | Angelo Rossi | Cosani | TBD | Existing color-only matrix to rebuild |
| `{Brand} Suit` per Peerless brand | various | Peerless Clothing | TBD | **TBD** which are active |
| `{Brand} Suit` for other current brands (Tiglio, Harmony, Paul Betenly, Retro Paris, Vitarelli, etc.) | various | various | TBD | **TBD** which are active |

Open:

- Gruppo price points within a brand (solids vs. patterns) — **Confirm** whether one matrix per brand-fabric-tier or finer. If finer, line names distinguish them (e.g. `Mantoni Wool Suit` / `Mantoni Wool Pattern Suit`).
- Peak Lapel suits (Gruppo model) — separate matrices if stocked (`Mantoni Wool Peak Lapel Suit`). Portly per §4.1 (Notes).

### 7.2 Vendors that ship 3pc as one SKU

These are not matrices. Under §1.5, each vendor 3pc SKU is expanded at intake into the line's suit matrix and its vest matrix (§7.5).

| Vendor 3pc SKU | Suit matrix | Vest matrix |
|---|---|---|
| Mantoni `M40901-*`, `M46306-*` ("2BV No P+Vest") | Mantoni Wool Suit | Mantoni Vest |
| Mantoni `M87905-*` (tweed, "Lapel Vest (Optional)") | Mantoni Tweed Suit | Mantoni Tweed Vest |
| Giorgio Fiorelli `G47815-*`, `G49412-*`, `G45001-*`, `G79773-2`, `G79762-1` ("2BV No P+Vest") | Giorgio Fiorelli Suit | Giorgio Fiorelli Vest |
| Carlo Lusso `C67901-*` ("2BV No P+Vest") | Carlo Lusso Suit | Carlo Lusso Vest |
| Bertolini `B66050-*`, `B66051-1A` ("2BV No P+Vest") | Bertolini Suit | Bertolini Vest |
| Giorgio Fiorelli Oscar Tux+Vest `G47815-*`, `G45001-1`, `G79505-*` | Giorgio Fiorelli Oscar Tuxedo | Giorgio Fiorelli Tuxedo Vest |
| Caravelli 3pc (San Malone; also sells vest separately) | Caravelli Suit | Caravelli Vest |

The suit component must be the same garment as the vendor's 2pc in that fabric; **Confirm** per vendor (§13).

### 7.3 Tuxedos

| Matrix description | Category | Vendor | Vendor codes | Notes |
|---|---|---|---|---|
| Caravelli Tuxedo | Tuxedos | San Malone | TBD | Existing color-only matrix to rebuild with size/fit |
| Renoir Notch Lapel Tuxedo | Tuxedos | Renoir Fashions | `201-1 Notch` | Classic, Slim |
| Renoir Peak Lapel Tuxedo | Tuxedos | Renoir Fashions | `201-1 Peak`, `201-4 Peak`, `201-19` | New Slim |
| Renoir Shawl Tuxedo | Tuxedos | Renoir Fashions | `201-{1,3,8,9,16,17,19}`, `202-2` Shawl | Slim |
| Renoir Double-Breasted Tuxedo | Tuxedos | Renoir Fashions | `201-1 DB` | New Slim |
| Renoir Tailcoat | Tuxedos | Renoir Fashions | `201-1 FD` | Classic (full dress) |
| Alessandro Vitello Notch Lapel Tuxedo | Tuxedos | Renoir Fashions | `508-1`, `508B-1` | Wool and wool stretch; split if priced differently |
| Alessandro Vitello Peak Lapel Tuxedo | Tuxedos | Renoir Fashions | `508B-1` | — |
| Renoir Tuxedo Jacket | Tux & Dinner Jackets | Renoir Fashions | `290-15`–`22` | Jacket only |
| Giorgio Fiorelli Arlan Tuxedo | Tuxedos | Gruppo Bravo | `G47815-*`, `G45001-1`, `G79696-*`, `G79505-*` | Existing color-only matrix ("Giorgio Fiorelli Tuxedo") to rebuild |
| Giorgio Fiorelli Shawl Tuxedo | Tuxedos | Gruppo Bravo | `G47815-*`, `G45001-1`, `G79696-*`, `G79505-2` |   |
| Giorgio Fiorelli Oscar Tuxedo | Tuxedos | Gruppo Bravo | `G47815-*`, `G45001-1`, `G79505-*` | Ships with vest; vest goes to Giorgio Fiorelli Tuxedo Vest (§7.2) |
| Giorgio Fiorelli Tuxedo | Tuxedos | Gruppo Bravo | `G47815-1` (2B Adj. / Slim) |   |
| Giorgio Fiorelli Dinner Jacket | Tux & Dinner Jackets | Gruppo Bravo | `G47815-{5,5A,7}` |   |
| Enzo Tovare Tuxedo | Tuxedos | Gruppo Bravo | `E59663-1` | 2BV Tux |
| Enzo Tovare Peak Lapel Tuxedo | Tuxedos | Gruppo Bravo | `E59663-1` | 1B Peak |
| Enzo Tovare Shawl Tuxedo | Tuxedos | Gruppo Bravo | `E59663-1` |   |
| Mantoni Tuxedo, Mantoni Peak Lapel Tuxedo, Mantoni Shawl Tuxedo, Mantoni Double-Breasted Tuxedo | Tuxedos | Gruppo Bravo | `M40901-1` | One matrix per model stocked (catalog: 2BV Tux, Euro Slim Tux, 1B Peak, Shawl, DB) |
| Mantoni Velvet Tuxedo Jacket | Tux & Dinner Jackets | Gruppo Bravo | `M42000-1` | Pants optional |

The Renoir suffix (`FD`, `DB`, `Notch`, `Peak`, `Shawl`) identifies the garment type and is part of the Vendor ID.

### 7.4 Sport Coats & Blazers

| Matrix description | Vendor | Vendor codes |
|---|---|---|
| Alessandro Vitello Wool Blazer | Renoir Fashions | `561-{5,6,7}` |
| Renoir Blazer | Renoir Fashions | `201-1`, `201-19` |
| Pellagio Riserva Wool Sport Coat | Renoir Fashions | `PF26-2W`, `PF26-4W`, `PF25-{17,18,20,21}W` |
| Pellagio Riserva Sport Coat | Renoir Fashions | `PF26-10`–`18`, `PF25-*`, `PS25-*`, `PF24-*`, `PS24-8`, `PF23-*` — **Confirm** price tiers |
| Pellagio Hooded Sport Coat | Renoir Fashions | `PF26-7`, `PF26-8`, `PF26-9` |
| Mantoni Wool Blazer | Gruppo Bravo | `M40901-1`, `M40901-2B` |
| Mantoni Waffle Weave Sport Coat | Gruppo Bravo | `M41000-*` |
| Mantoni Sport Coat | Gruppo Bravo | `M7879x-*` |
| Giorgio Fiorelli Blazer | Gruppo Bravo | `G47815-1`, `G47815-2B` |
| Enzo Tovare Soft Jacket | Gruppo Bravo | `E950xx-*` |
| Enzo Tovare Sport Coat | Gruppo Bravo | `E880xx-*`, `E8604x-*` |
| Dray Scott Sport Coat | Gruppo Bravo | `D30033-*` |
| Enzo Tovare Velvet Jacket | Gruppo Bravo | `M42000-{2,3}` |
| Enzo Tovare Vegan Leather Jacket | Gruppo Bravo | `E85100-*` |
| Enzo Tovare Vegan Suede Jacket | Gruppo Bravo | `E85000-*` |

### 7.5 Vests

| Matrix description | Category | Vendor | Vendor codes | Notes |
|---|---|---|---|---|
| Alessandro Vitello Vest | Suit Vests | Renoir Fashions | `508-*` | Classic fit; Regular and Long on some colors |
| Alessandro Vitello Stretch Vest | Suit Vests | Renoir Fashions | `508B-*` | Classic fit |
| Renoir Vest | Suit Vests | Renoir Fashions | `201-*`, `202-*`, `203-*`, `207-*` | Classic fit |
| Caravelli Vest | Suit Vests | San Malone | TBD | From 3pc SKUs and vests sold separately |
| Renoir Satin Vest | Tux Vests | Renoir Fashions | `Satin-1` |   |
| Pellagio Puffer Vest | Casual Vests | Renoir Fashions | `PF21-12` | Alpha S–2XL |
| Mantoni Vest | Suit Vests | Gruppo Bravo | `M40901-*`, `M46306-*` | From 3pc SKUs and "Suit Vest" |
| Mantoni Tweed Vest | Suit Vests | Gruppo Bravo | `M87905-*` |   |
| Giorgio Fiorelli Vest | Suit Vests | Gruppo Bravo | `G47815-*`, `G49412-*`, `G45001-*`, … | From 3pc SKUs and "Suit Vest" |
| Carlo Lusso Vest | Suit Vests | Gruppo Bravo | `C67901-*` | From 3pc SKUs |
| Bertolini Vest | Suit Vests | Gruppo Bravo | `B66050-*`, `B66051-1A` | From 3pc SKUs |
| Giorgio Fiorelli Tuxedo Vest | Tux Vests | Gruppo Bravo | `G47815-*`, `G45001-1`, `G79505-*` | From Oscar Tux+Vest; also Arlan Vest |
| `{Brand} Vest` (other lines) | Suit Vests | various | — | One per suit line that has vests in stock |

### 7.6 Outerwear

| Matrix description | Category | Vendor | Vendor codes | Size format |
|---|---|---|---|---|
| Pellagio Raincoat | Top Coats | Renoir Fashions | `PS26-14/15/16` (Pioggia) | Chest + length |
| Enzo Tovare Topcoat | Top Coats | Gruppo Bravo | `E54805-*` | Chest + length |
| Enzo Tovare Car Coat | Top Coats | Gruppo Bravo | `E54805-*` (Car Coat model) | Chest + length |
| `Enzo Tovare {Model} Jacket` (Ace, Kami, Play, etc.) | Jackets | Gruppo Bravo | `E54879-*`, `E54801-*` | Alpha S–4XL; one matrix per model |

### 7.7 Pants

| Matrix description | Category | Vendor | Vendor codes | Fits |
|---|---|---|---|---|
| Alessandro Vitello Dress Pants | Dress Pants | Renoir Fashions | `508-*` | Classic, Slim |
| Renoir Dress Pants | Dress Pants | Renoir Fashions | `201-*`, `202-*`, … | Classic, Slim |
| Mantoni Dress Pants | Dress Pants | Gruppo Bravo | `M40901-*`, `M46306-*` | Non Pltd, 1 Pltd, Slim |
| Giorgio Fiorelli Dress Pants | Dress Pants | Gruppo Bravo | `G47815-*`, `G49412-*`, `G45001-*` | No Pltd, 1 Pltd, Slim |
| Carlo Lusso Dress Pants | Dress Pants | Gruppo Bravo | `C67901-*` | Non Pleated |
| Enzo Tovare Linen Pants | Dress Pants | Gruppo Bravo | `E58302-*` | Non Pleated |
| Enzo Tovare Cotton Pants | Dress Pants | Gruppo Bravo | `E58304-*` | Non Pleated |
| Enzo Tovare Albert Pants; Enzo Tovare Alpha Denim; Enzo Tovare Berlin Pants; Enzo Tovare Beta Skinny Denim | Casual Pants & Denim | Gruppo Bravo | `Albert-*`, `Alpha-*`, `Berlin-*`, `Beta-*` | MT3; one matrix per model (§4.10); waist 30–46 |
| Enzo Tovare Milan Stretch Pants; Enzo Tovare Tokyo Stretch Pants | Casual Pants & Denim | Gruppo Bravo | `Milan-*`, `Tokyo-*` | MT3; one matrix per model; waist 30–46 |
| Pellagio Stretch Pants | Casual Pants & Denim | Renoir Fashions | `PF26-20`–`24` | See §4.10 Notes |
| `{Brand} Dress Pants` for current brands (Tallia, Calvin Klein, Tiglio, Harmony, Paul Betenly, Vitarelli, etc.) | Dress Pants | various | TBD | **TBD** which are active |

---

## 8. Shirts and shoes

### 8.1 Shirts

A dress shirt varies by color, neck, sleeve, fit, collar, cuff, and fabric/finish. Placement:

| Dimension | Where it goes |
|---|---|
| Color | Attribute 1 (vendor name) |
| Neck + sleeve | Attribute 2, combined (`15.5 34/35`) |
| Fit, and cuff/collar options the vendor lists under one code | Attribute 3 (vendor name) |
| Fabric / finish (100% cotton, non-iron, stretch) | Tags |
| Collar shape sold as a separate code | Separate matrix |

Matrix boundary: vendor line × shirt type (Dress Shirts, Tux Shirts).

Current price points: "Dress Shirts (Standard)" $44.95 (Marquis / Vitarelli) and "Premium Dress Shirts" $59.50 (Christopher Lena).

| Matrix description | Category | Vendor | Vendor codes | Fits |
|---|---|---|---|---|
| Marquis Dress Shirt | Dress Shirts | Jade Eastern Trading | TBD | Slim, Classic |
| Vitarelli Dress Shirt | Dress Shirts | Jade Eastern Trading | TBD | TBD |
| Christopher Lena Dress Shirt | Dress Shirts | Christopher Lena Shirt Co | TBD | TBD |
| Renoir Cotton Dress Shirt | Dress Shirts | Renoir Fashions | `SS21x`, `CS022x` | Slim, Classic |
| Renoir Dress Shirt | Dress Shirts | Renoir Fashions | `TC*` (poly-cotton) | Classic, Slim |
| Renoir Tuxedo Shirt | Tux Shirts | Renoir Fashions | `TUX-01-SL` | Slim |
| Mantoni Dress Shirt | Dress Shirts | Gruppo Bravo | `M2000x-*`, `M200xx-*` | Classic, Slim, Modern, French Cuff |
| Mantoni Tuxedo Shirt | Tux Shirts | Gruppo Bravo | `M20028-1`, `M20029-*` | Slim, Classic, Wing Tip |
| Giorgio Fiorelli Dress Shirt | Dress Shirts | Gruppo Bravo | `G26xxx-*` | Classic, Slim |
| Giorgio Fiorelli Tuxedo Shirt | Tux Shirts | Gruppo Bravo | `G1266` | Slim, Classic, Wing Tip |
| Enzo Tovare Dress Shirt | Dress Shirts | Gruppo Bravo | `E61101-1`, `E61102-1` | Classic, Slim, French Cuff |
| Enzo Tovare Tuxedo Shirt | Tux Shirts | Gruppo Bravo | `E71402-1` | — |

Volume: one shirt line in 20 colors × ~25 neck/sleeve combinations × 2 fits is ~1,000 possible variants. Variants are created on receipt only (§1.8).

Neck/sleeve grids for Renoir, Mantoni, Giorgio Fiorelli, Enzo Tovare, Christopher Lena: **TBD** from catalogs.

### 8.2 Shoes

Dimensions: style, color, size, width. With Color / Size / Width as the attributes, **style is the matrix**: one matrix per style (e.g. `Stacy Adams Dickinson`).

- **Vendor codes:** Stacy Adams codes encode style and color: `25066-221` = style 25066 (Dickinson), color 221 (Cognac); `25066-001` = Black. The full code goes in Vendor ID.
- **UPCs:** shoe boxes are expected to carry a UPC per style, color, size, and width (**Confirm** on a Stacy Adams, Florsheim, and Johnston & Murphy box). If so:
  - Intake for shoes is a box scan, not slip parsing. Store the UPC on the item at first receipt; later receipts match exactly.
  - The register can scan the box, so shoes need no printed labels.
- **New matrices are routine.** Styles turn over every season, so creating a new shoe matrix is the common case, not the exception. **TBD:** allow Claude to create shoe matrices from a fixed template (brand, vendor, category, price from slip) on approval, or keep new styles manual.
- **Current data:** 115 shoe items, one per style and color, with no size or width. Each style becomes one matrix; existing colors become Color values.

Candidate shoe matrices, from current inventory: Appendix B.

---

## 9. Accessories (deferred)

Last in sequence. Current accessories are price-point items (e.g. "Neck Ties - Standard" $22.95, "Neck Tie - Premium" $49.50) under several small vendors. Proposed default, to revisit: keep low-value accessories as single price-point items without variant tracking; matrix only where size or color tracking is worth it (e.g. belts, Color / Size).

---

## 10. Vendor adapters

An adapter is everything Claude needs to read one vendor's documents and translate them into the structure in §2–§4. Adding a vendor means filling in the template in §10.0. An adapter is complete when every field is filled from a catalog and at least one real packing slip or invoice.

### 10.0 Template

| Field | Content |
|---|---|
| Lightspeed vendor record | Exact record name (§5) |
| Brands | Brands supplied |
| Garment types | Garment types supplied (§4) |
| Documents with goods | Packing slip, invoice, or both; whether costs appear |
| Code grammar | How the vendor's item code is built, and what goes in Vendor ID |
| Garment type on slip | Where garment type or model appears (code suffix, description, column) |
| Fit on slip | Where fit appears, and the vendor's fit vocabulary |
| Color on slip | Color code, color name, or both |
| Size on slip | How size is printed, and conversion to the store format (§4) |
| 3-piece handling | Vest sold separately, 3pc as one SKU (expanded, §1.5), or both; cost-split fallback (§11.2a) |
| UPCs | Whether items or boxes carry UPCs |
| Catalog | Catalog on hand, and season |
| Status | Complete, or the fields still **TBD** |

### 10.1 Renoir Fashions (Novara Fashion Inc)

| Field | Content |
|---|---|
| Lightspeed vendor record | `Renoir Fashions` |
| Brands | Renoir, Rivelino, Alessandro Vitello, Pellagio |
| Garment types | Suits, Tuxedos, Tux & Dinner Jackets, Sport Coats & Blazers, Suit and Tux Vests, Casual Vests, Top Coats, Dress Pants, Casual Pants & Denim, Dress and Tux Shirts |
| Documents with goods | **TBD** |
| Code grammar | `{line}-{color}` (e.g. `201-19`, `508B-3`); Pellagio `{PF\|PS}{yy}-{n}[W]` (e.g. `PF26-2W`). Vendor ID = code plus any fit or garment suffix |
| Garment type on slip | Tux suffix `FD` / `DB` / `Notch` / `Peak` / `Shawl`; otherwise **TBD** (same fabric code spans suit, pant, vest, tux, blazer) |
| Fit on slip | Rivelino suffix `CL` / `MD`; otherwise **TBD**. Vocabulary: Classic, Slim, New Slim, Modern |
| Color on slip | Code suffix (`-19`); names from catalog (e.g. 19 = New Navy) |
| Size on slip | **TBD** |
| 3-piece handling | Vests sold separately; no expansion |
| UPCs | **TBD** |
| Catalog | Four Seasons (Novara); Pellagio Fall/Winter 2026; separate Renoir catalog not yet reviewed |
| Status | Code grammar complete; slip layout **TBD** |

### 10.2 Gruppo Bravo

| Field | Content |
|---|---|
| Lightspeed vendor record | `Gruppo Bravo Menswear` (after merge, §5) |
| Brands | Mantoni, Giorgio Fiorelli, Enzo Tovare, Dray Scott, Manchester & Tailor, Carlo Lusso, Woolston, Bertolini |
| Garment types | Suits, Tuxedos, Tux & Dinner Jackets, Sport Coats & Blazers, Suit and Tux Vests, Top Coats, Jackets, Dress Pants, Casual Pants & Denim, Dress and Tux Shirts |
| Documents with goods | **TBD** |
| Code grammar | `{brand letter(s)}{fabric}-{color}` (M, E, D, G, GS, C, WS, B, T), some colors with letter suffixes (`-2A`, `-2B`, `-4A`, `-5A`, `-1A`); denim and stretch pants by model (`Alpha-510`, `Milan-1`); outerwear by model and code (`E54879-72`) |
| Garment type on slip | Model strings: 2BV No P, 2BV No P+Vest, DB, Peak Lapel, Arlan Tux, Shawl Tux, Oscar Tux+Vest, Blazer, Dinner Jacket — **Confirm** they appear on slips verbatim |
| Fit on slip | Model strings: Euro Slim, Savvy Slim, Savvy Semi Slim, Portly, Slim; pants Non Pltd, 1 Pltd, Slim |
| Color on slip | Code suffix; names from catalog |
| Size on slip | **TBD** |
| 3-piece handling | 3pc as one SKU ("2BV No P+Vest," "Oscar Tux+Vest") — expanded (§7.2); cost-split fallback **TBD** |
| UPCs | **TBD** |
| Catalog | Fall-Winter 2026 |
| Status | Code grammar complete; slip layout and cost split **TBD**. Recorded as vendor on some Stacy Adams items — **Confirm** (§13) |

### 10.3 San Malone

| Field | Content |
|---|---|
| Lightspeed vendor record | `San Malone` |
| Brands | Caravelli, Cianni |
| Garment types | Suits, Tuxedos, Sport Coats & Blazers, Suit Vests, Dress Pants |
| Documents with goods | **TBD** |
| Code grammar | **TBD** |
| Garment type on slip | **TBD** |
| Fit on slip | **TBD**; current Fit values Classic, Slim |
| Color on slip | **TBD**; current colors include Midnight Blue, Navy, Medium Grey, Light Grey, Cobalt, Beige, Burgundy, Powder Blue, Black |
| Size on slip | **TBD** |
| 3-piece handling | Both: 3pc as one SKU (expanded) and vest sold separately; cost-split fallback **TBD** |
| UPCs | **TBD** |
| Catalog | None on hand |
| Status | **TBD** — needs catalog and a slip |

### 10.4 Weyco Group (Stacy Adams, Florsheim)

| Field | Content |
|---|---|
| Lightspeed vendor record | `Weyco Group Inc.` (absorbing `Stacy Adams` record unless ordered separately, §13) |
| Brands | Stacy Adams, Florsheim |
| Garment types | Shoes |
| Documents with goods | **TBD** |
| Code grammar | Stacy Adams `{style}-{color code}` (e.g. `25066-221`), used as Vendor ID; Florsheim similar (`15216-001`) — **Confirm** |
| Garment type on slip | Not needed; all shoes. Leaf by style description (§4.14) |
| Fit on slip | Width code: `M`, `W` (Classics `D`, `3E`) |
| Color on slip | Color code; names from catalog (001 Black, 221 Cognac, 226 Brandy, …) |
| Size on slip | **TBD** |
| 3-piece handling | Not applicable |
| UPCs | Expected per style, color, size, width on boxes — **Confirm**; primary match if present |
| Catalog | Stacy Adams Fall 2026 (styles in Appendix B.1); Florsheim none |
| Status | Stacy Adams codes, colors, widths, size runs complete; slip layout and UPCs **TBD**; Florsheim **TBD** |

### 10.5 Johnston & Murphy

| Field | Content |
|---|---|
| Lightspeed vendor record | `Johnston & Murphy` |
| Brands | Johnston & Murphy |
| Garment types | Shoes |
| Status | All fields **TBD** — no catalog |

### 10.6 Other active vendors

Adapters not started: Peerless Clothing, Eisenberg, Paris By Night, Retro Paris, Marina Imports, Tiglio, Cosani, The Suit Co., Jade Eastern Trading, Harmony Clothes, PB Aristo, Desen Suiting Company, L.A. Gibbs Resources, Angelo, Christopher Lena Shirt Co, MM Fashion & Design, Braveman, Nevima International, Coppley, Forsyth of Canada, Dazi, and accessory vendors (deferred with accessories). Until a vendor's adapter is complete, its intake is proposal-only with every line reviewed.

---

## 11. Intake workflow

### 11.1 Sequence

1. **Shipment arrives.** Staff give Claude the packing slip or invoice (photo or PDF).
2. **Claude parses** the document with the vendor's adapter (§10): vendor, code, garment type, color, fit, size, quantity, cost.
3. **Claude expands and matches.** A vendor 3pc SKU becomes two lines, suit and vest (§1.5, §7.2). Each line is matched to an existing item by matrix (vendor line + garment type) and attributes (Color, Size, Fit).
4. **Claude proposes**, one row per slip line:
   - the slip's wording beside the matched Lightspeed item;
   - lines needing a new variant in an existing matrix;
   - lines needing a new matrix (§11.3);
   - lines it could not parse or match;
   - quantity and cost per line, with cost source (invoice or default cost). The proposal is formatted for checking against the boxes on the counter.
5. **Staff approve**, correcting any line.
6. **Claude executes** through the API:
   - creates missing variants in their matrices;
   - creates a purchase order for the vendor with the approved lines, quantities, and costs;
   - receives the purchase order, which adds stock and records cost.
7. **Claude reports** what was written, with the purchase order number.

Nothing is written to Lightspeed before step 5.

### 11.2 Cost source

| Document received | Cost used | Status |
|---|---|---|
| Invoice, or slip with invoice | Invoiced cost per line | Final |
| Packing slip without costs | Item's default cost | Flagged **cost pending**; reconciled when the invoice arrives |

Which documents each vendor ships with the goods: **TBD** (§13).

### 11.2a Cost split for vendor 3pc SKUs

The vendor invoices one cost for a 3pc; Lightspeed needs a cost on the suit item and the vest item. The two always sum to the invoiced cost. Allocation, in order of preference:

1. Suit = the vendor's own 2pc cost for that fabric; vest = the remainder.
2. If the vendor has no 2pc price for that fabric: vest = the vendor's separate vest price; suit = the remainder.
3. Otherwise: a fixed ratio set per vendor in its adapter (§10). **TBD** per vendor.

Retail for a 3-piece sale is set manually at the register (§12.2).

### 11.3 Claude's authority

| Case | Claude may |
|---|---|
| Existing item | Receive on approval |
| New variant in an existing matrix | Create and receive on approval |
| New shoe matrix (new style) | **TBD** (§8.2): create from template on approval, or refer to staff |
| New matrix, any other garment | Propose only; staff create or explicitly approve the matrix description, brand, vendor, category, price, and tags |
| Unparseable or unmatched line | Refer to staff |

### 11.4 Fallback

If receiving through the API does not update stock and cost the way receiving in the Lightspeed UI does, step 6 stops after creating the purchase order, and staff receive it in Lightspeed.

### 11.5 Connector requirements to verify

- Creating an item inside an existing matrix with specified attribute values. Everything depends on this write; test it first.
- Creating a purchase order with lines and costs.
- Receiving a purchase order through the API, and confirming stock and cost update as in the UI.
- Rate limits: Lightspeed throttles API calls; a shipment that creates many variants needs pacing.

### 11.6 Alternative: purchase order file import

Lightspeed's purchase order import accepts a file of up to 500 lines, matches existing items by Vendor ID, and creates unmatched items. It is not the primary path, because Vendor ID is not unique per item in this scheme (§1.10), and because it is untested whether imported new items land inside existing matrices with correct attributes.

---

## 12. Exceptions and register practices

### 12.1 Breaking a suit

Suit size is the jacket size; pant size is not tracked on the suit item. Since a 3-piece is always a suit item plus a vest item (§1.5), selling a suit without its vest, or a vest alone, is an ordinary sale with no adjustment. The remaining cases are inventory adjustments on items that already exist; Claude performs them when staff describe what happened.

| Case | Adjustment |
|---|---|
| Pants swapped between two suits to fit a customer | None. Both jackets keep their sizes, so counts stay correct. The suit left on the rack carries non-standard pants. |
| Jacket or pants sold alone from a 2pc | Suit item −1; the leftover piece +1 in its line's separates matrix (e.g. pants into `{Brand} Dress Pants` at its waist). |

Requirements and limits:

- A leftover jacket needs a separates matrix for its line. Where none exists: **TBD** (§13).
- The leftover piece takes the suit item's cost. Accepted as a small distortion.

### 12.2 Packages

Package deals (suit + shirt + tie; 3-piece as suit + vest) are priced manually at the register with a discount. No package items and no assembly matrices.

### 12.3 Rentals

The store does not rent. No rental tracking.

---

## 13. Open questions (consolidated)

1. The store currently sells "Renoir Wool Suit" and "Renoir Premium Wool Suit." The catalog labels these lines Alessandro Vitello (`508`) and Rivelino (`RHC100`). Which brand is on the garment label? This decides both the Brand field and the matrix description (`Alessandro Vitello Wool Suit` vs. `Renoir Wool Suit`). **Action:** check the label on a garment in stock.
2. Active vendors with no current lines in the data — MM Fashion & Design, Braveman, Nevima International, Coppley, Forsyth of Canada, Dazi (garments); Pacific Silk, Trans Pacific Wholesale, Top Circle Hosiery Mills (accessories): what brands and lines does the store buy from each?
3. Does Gruppo Bravo supply Stacy Adams? (7 shoe items list Gruppo Bravo as vendor.)
4. Are Paris By Night and Retro Paris the same company? Is Stacy Adams ordered through Weyco or a separate account?
5. Portly encoding (§4.1 Notes).
6. Every-inch vs. even waists for pants; half sizes and width codes for Florsheim and Johnston & Murphy (Stacy Adams resolved in §4.14).
7. Stacy Adams styles not in the Fall 2026 catalog (Appendix B.2): does the store still order any of them? Default is Sell through.
8. Price tiers within lines (Renoir 201 family, Gruppo brands, Pellagio Riserva).
9. Shoe box UPCs — present per size and width?
10. Shoe matrix creation: Claude on approval, or manual.
11. Per vendor that ships 3pc as one SKU (§7.2): is the suit component the same garment as its 2pc in that fabric, and what cost-split fallback applies (§11.2a)?
12. One real packing slip per vendor, to confirm that garment type and fit are readable, and which documents (slip, invoice, both) arrive with the goods.
13. Are Stacy Adams, Florsheim, or Johnston & Murphy in Lightspeed's vendor catalogs or NuORDER? (§5.4)
14. Lightspeed tests: duplicate attribute combination behavior; custom field API support; connector requirements in §11.5.
15. Unmatched sales: a garment sold before intake, untagged, or with no existing item. Proposed: one "unmatched sale" item per category, priced at the register and reconciled by Claude against stock afterward.
16. Size-based pricing: upcharges for XL lengths and large chest sizes (Eisenberg XL is +$100), and color-based price differences (J&M Lancaster). Per-item price overrides by a stated rule, or separate extended-size matrices?
17. Markdowns and clearance: item price change, register discount, or a Lightspeed price level?
18. Closeouts and one-off buys ("Designer Sport Coat," "800 Suit"): one closeout matrix per vendor and garment type, or individual matrices?
19. Special orders: how deposits and the link between customer and incoming item are recorded.
20. Labels: print Lightspeed labels at intake (§11) and during the migration count; confirm label stock and printer.
21. Leftover jackets from broken suits in lines with no separates matrix (§12.1).
22. Can Lightspeed change the attribute set of a matrix that already has items (§2 Matrix types, rule 2)? Determines whether the existing Color-only matrices (Angelo Rossi, Caravelli Tux, Giorgio Fiorelli Tuxedo) can be converted in place or must be rebuilt.

### Immediate fixes (independent of design)

- Create the custom matrix attribute set `Color/Size/Width` for shoes (§2 Matrix types).
- Caravelli matrix default price: $61.21 → $599.95.
- Caravelli: count the vests in current 3pc stock into Caravelli Vest; the suit items remain in the Caravelli matrix (renamed Caravelli Suit).
- Merge duplicate items (Caravelli Navy 42R Slim; Eisenberg Black 40S ×3, 40XL, 48XL, 52XL).
- Vendor and brand name cleanup per §5: merge duplicate vendor records (Gruppo Bravo, L.A. Gibbs, PB Aristo, Jade Eastern Trading, MM Fashion); remove the trailing space in `Paris By Night `; repoint items on disabled records.
- Remove the 603 vendor-catalog vendors by turning off unneeded product industries, and disable the `TEST` record (§5.4).
- Confirm "Update From Catalog" is off on every vendor.

---

## Appendix A. Current retail price points by brand and garment

From the current inventory export (placeholder items' retail prices). These are provisional matrix default prices until confirmed. Multiple values mean the brand has more than one tier or inconsistent pricing.

| Brand | Suit | Tuxedo | Sport Coat | Pants | Notes |
|---|---|---|---|---|---|
| Caravelli | $599.95 | $599.95 | $299.95 | $159.95 | One suit item at $699.95 ("Pronto Moda – 100% Wool") |
| Renoir | $599.95 / $795.95 / $1,199.95 | $599.95 / $795.95 | $449.95 / $599.95 ("Renoir Wool") | $159.95 / $249.95 ("Renoir Wool") | Tiers likely `201` / `508` / `RHC100` (§13) |
| Giorgio Fiorelli | $599.95 | $599.95 | $399.95 | $159.95 | Suit matrix default is $549.95; tux matrix default is $499.95 — **Confirm** |
| Manchester & Tailor | $599.95 | — | — | — |   |
| Mantoni | $795.95 ($749.95 on one 54XL) | $795.95 | $499.95 | $249.95 |   |
| Enzo Tovare | $1,099.95; linen $795.95 | $1,099.95 | $499.95 | $249.95; linen $199.95 |   |
| Dray Scott | $1,495.95 | — | — | — |   |
| Angelo Rossi | $549.95 | — | — | $89.95 |   |
| Eisenberg | $695.95 / $699.95; XL $799.95 | — | — | $149.95 | XL premium; one item at $175.00 (error) |
| Calvin Klein | $695.95–$799.95 | $799.95 | — | $159.95 |   |
| DKNY | $599.95 | — | — | $159.95 |   |
| Kenneth Cole | $549.95 | — | $399.95 | $99.95 |   |
| Michael Kors | $749.95 | — | — | $159.95 |   |
| Ralph Lauren | $749.95 | — | — | $249.95 |   |
| Tommy Hilfiger | $749.95 | — | — | $159.95 |   |
| Tallia | $849.95 | — | $499.95 | $249.95 |   |
| Marc New York | $599.95 | — | — | — |   |
| TailoRED | $1,895.95 | — | — | — |   |
| Tiglio | $899.95 | — | $499.95 | $249.95 |   |
| Canaletto | $1,195.95 | — | — | — |   |
| Harmony | $899.95 | — | $499.95 | $249.95 |   |
| Paul Betenly | $1,295.95; stretch $599.95 | $1,295.95 | $599.95 | $349.95 |   |
| Vitarelli | $649.95 | — | — | $159.95 |   |
| Desen | $599.95 | — | — | $89.95 |   |
| Giorgio Sanetti | $599.95 | — | — | — |   |
| Tweed (Marina Imports) | $699.95 | — | — | — |   |
| Mundo | $239.95 | — | — | — |   |
| Retro Paris | $499.95 / $549.95; Luciano $129.95 | — | — | — |   |
| Sharp (Capucci) | $499.95 | — | — | — |   |
| Venezia | $699.95 | — | — | — |   |
| Marco Valentino | $1,195.00 | — | — | — |   |
| L.A. Gibbs | — | $549.95 | $149.95 | — |   |
| Ascott Brown | — | — | — | $149.95 |   |
| Cianni | — | — | — | $99.95 |   |

Shirts: Marquis / Vitarelli $44.95; Christopher Lena $59.50.

---

## Appendix B. Candidate shoe matrices (from current inventory)

One matrix per style. Stacy Adams style numbers, catalog colors, categories, and size runs are from the Stacy Adams Fall 2026 catalog. Vendor ID per item is `{style}-{color code}` (e.g. `25066-221`); the Color attribute is the catalog color name.

**Category rule** (from the catalog's style description): name includes *Boot* → Boots; *Sandal*, *Sneaker*, *Driving Moc*, or *Perfed* → Casual; all other oxfords, laces, monk straps, and slip-ons → Dress.

### B.1 Stacy Adams — styles in the Fall 2026 catalog (Weyco Group)

| Matrix description | Style no. | Category | Catalog colors | Current colors (inventory) | Size run | Retail |
|---|---|---|---|---|---|---|
| Stacy Adams Bailey | 25750 | Dress | 001 Black, 226 Brandy | — | M 7–12, 13–16; W 9–11, 12–15 | $59.95 |
| Stacy Adams Benson | 25751 | Dress | 226 Brandy, 001 Black | 226 | M 7–12, 13–16; W 9–11, 12–15 | $119.95 |
| Stacy Adams Berkshire | 25798 | Boots | 226 Brandy, 001 Black | — | M 7–12, 13–16; W 9–11, 12–15 | $129.95 |
| Stacy Adams Boswell | 25779 | Dress | 226 Brandy, 001 Black | — | M 7–12, 13–16; W 9–11, 12–15 | $119.95 |
| Stacy Adams Brayden | 25635 | Dress | 200 Brown, 240 Tan, 001 Black | Black, Gray, Tan | M 7–12, 13–15; W 8–11, 12–14 | $129.95 |
| Stacy Adams Bryant | 25634 | Dress | 240 Tan, 200 Brown, 001 Black | Black, Brown, Tan | M 7–12, 13–15; W 8–11, 12–14 | $129.95 |
| Stacy Adams Conroy | 25702 | Casual | 240 Tan, 001 Black, 100 White | 221 | M 7–12, 13–15 | $59.99 |
| Stacy Adams Desmond | 25162 | Dress | 221 Cognac, 001 Black | Cognac | M 7–12, 13–16; W 8–11, 12–16 | $104.95 |
| Stacy Adams Dickinson | 25066 | Dress | 221 Cognac, 001 Black, 601 Burgundy | Black, Cognac | M 7–12, 13–16; W 8–11, 12–16 | $119.95 |
| Stacy Adams Dunbar | 25064 | Dress | 001 Black, 221 Cognac | — | M 7–12, 13–16; W 8–11, 12–16 | $119.95 |
| Stacy Adams Kallum | 25568 | Dress | 410 Navy, 001 Black, 221 Cognac | Black, Burgundy, Cognac | M 7–12, 13–15; W 8–11, 12–14 | $119.95 |
| Stacy Adams Kaylor | 25572 | Dress | 221 Cognac, 001 Black | Black, Cognac | M 7–12, 13–15; W 8–11, 12–14 | $119.95 |
| Stacy Adams Kinsley | 20180 | Dress | 001 Black | — | M 7–12, 13; W 9–11, 12–13 | $59.99 |
| Stacy Adams Langford | 25795 | Dress | 221 Cognac, 001 Black | — | M 7–12, 13–15 | $129.95 |
| Stacy Adams Macarthur | 25489 | Dress | 001 Black, 221 Cognac | Cognac | M 7–12, 13–15; W 9–11, 12–14 | $89.95 |
| Stacy Adams Maddox | 25488 | Dress | 202 Chocolate, 001 Black, 221 Cognac | — | M 7–12, 13–15; W 9–11, 12–15 | $109.95 |
| Stacy Adams Marlton | 25490 | Dress | 202 Chocolate, 001 Black, 221 Cognac | — | M 7–12, 13–15; W 9–11, 12–15 | $59.95 |
| Stacy Adams Pacer | 25754 | Dress | 221 Cognac, 001 Black | — | M 7–12, 13–15 | $79.95 |
| Stacy Adams Richmont | 21567 | Dress | 222 Cognac Tumbled, 007 Black Tumbled, 117 White Tumbled | — | M 7–12, 13–15 | $79.95 |
| Stacy Adams Sync | 25662 | Dress | 280 Natural, 215 Brown CH, 001 Black, 221 Cognac | Black, Cognac | M 7–12, 13–16; W 9–11, 12–16 | $59.95 |
| Stacy Adams Synchro | 25518 | Dress | 113 Cream, 410 Navy, 001 Black, 100 White | — | M 7–12, 13–15; W 9–11, 12–15 | $99.95 |
| Stacy Adams Synergy | 25418 | Dress | 221 Cognac, 001 Black | Black, Cognac | M 7–12, 13–14 | $119.95 |
| Stacy Adams Tinsley | 25092 | Dress | 240 Tan, 001 Black, 111 Black & White, 238 Tan & Brown, 468 Cobalt & Navy | Tan | M 7–12, 13–15 | $129.95 |
| Stacy Adams Wakefield | 20141 | Dress | 001 Black | Black | M 7–12, 13–14; W 7½–11, 12–14 | $89.95 |
| Stacy Adams Wescott | 25712 | Dress | 202 Chocolate, 001 Black, 215 Brown Crazy Horse | — | M 7–12, 13–15 | $59.95 |

Notes:

- Current colors not in the Fall 2026 catalog (Brayden Gray, Kallum Burgundy, Conroy 221) are prior-season colors. They get Color values only if stock exists.
- Retail is the current inventory price. Catalog prices were not reviewed.
- Bailey is in current inventory at $59.95, well below Benson and Boswell ($119.95) from the same catalog group; **Confirm** it is the same style.

### B.2 Stacy Adams — styles not in the Fall 2026 catalog

These 57 current items match no style in the Fall 2026 catalog (men's lines), so they are most likely discontinued. **Proposed disposition: Sell through** (Appendix C.8), unless the store still orders a style from an earlier catalog.

| Current item(s) | Style no. (from item name) | Retail | Notes |
|---|---|---|---|
| Altair Black |   | $119.95 |   |
| Barlow Black |   | $64.95 |   |
| Barnett Black, Barnett Scotch |   | $64.95 |   |
| Bingham Tan |   | $120.95 |   |
| Bradley Boot |   | $129.95 |   |
| Canton Black |   | $79.95 |   |
| Corbetta Black, Corbetta Cognac |   | $119.95 |   |
| Emery Cognac |   | $79.95 |   |
| Fairchild Black, Fairchild Scotch |   | $99.95 |   |
| Faulkner Black, Faulkner Cognac |   | $109.95 |   |
| Felton Black |   | $64.95 |   |
| Flemming Cognac |   | $109.95 |   |
| Flex |   | $119.95 |   |
| Forrest Black |   | $64.95 |   |
| Franchetti 25812-221 | 25812 | $129.95 |   |
| Garrett Cognac |   | $119.95 |   |
| Garrison Black, Garrison Cognac |   | $89.95 |   |
| Heath Cognac |   | $89.95 |   |
| Hendrick Black, Hendrick Cognac |   | $89.95 |   |
| Kaine Black, Kaine Cognac |   | $119.95 |   |
| Kalvin |   | $99.95 | Catalog has Kalvin only in the kids' line (43440) |
| Karcher |   | $99.95 | Vendor recorded as Gruppo Bravo |
| Karnes Black, Karnes Cognac |   | $59.95 |   |
| Kordell Black, Brown, Cognac |   | $89.95 |   |
| Lavine Tan |   | $129.95 |   |
| Logan Cognac |   | $79.95 |   |
| Maury Black, Maury Cognac |   | $129.95 |   |
| Nelson 21541 | 21541 | $79.95 |   |
| Newell |   | $79.95 |   |
| 21544 – Noble | 21544 | $79.95 |   |
| Novack – 21540 | 21540 | $79.95 |   |
| Oxford Black; Oxford (Plain Toe) Black |   | $79.95 | Generic names; style unknown |
| Powell Cognac, Powell Tan |   | $99.95 |   |
| Rooney Black, Brown, Tan |   | $109.95 |   |
| Rucci Cognac |   | $129.95 |   |
| Sanford Burg/Black |   | $119.95 |   |
| Score – 21538 | 21538 | $79.95 |   |
| Vogel – 21548 | 21548 | $79.95 |   |
| Wardell Black |   | $89.95 |   |
| Waverly Black |   | $64.95 |   |
| Patent Leather Shoes |   | $85.95 | Current patent styles in catalog: Hartcliffe 25784, Scottie 25701, Slayton 25710, Gala 24998, Phoenix 21011, Concorde II 25721 — **Confirm** which |
| Wide Black |   | $89.95 | Style unknown |

### B.3 Florsheim (Weyco Group)

No catalog; categories and style numbers **TBD**.

| Matrix description | Colors | Retail | Style no. |
|---|---|---|---|
| Florsheim Postino | Black, Cognac, Wine | $119.95 |   |
| Florsheim Zaffiro | Black (001) | $129.95 | 15216 |

### B.4 Johnston & Murphy

No catalog; categories and style numbers **TBD**.

| Matrix description | Colors | Retail | Notes |
|---|---|---|---|
| Johnston & Murphy Conard 2.0 | Mahogany, Tan | $155.00 |   |
| Johnston & Murphy Danridge | — | $59.95 |   |
| Johnston & Murphy Everett | — | $99.95 | Stacy Adams also has Everett Moc Toe Penny Slip On (25790); **Confirm** brand on this item |
| Johnston & Murphy Gibbons | — | $129.95 |   |
| Johnston & Murphy Holden | — | $119.95 | Plain toe |
| Johnston & Murphy Lancaster | Black, Tan | $59.95 / $79.95 | Price differs by color — set per item |
| Johnston & Murphy Lewis | — | $129.95 | Boots. Two current items ("LEWIS - SHOES," "Lewis Ankle Boot") — **Confirm** same style |
| Johnston & Murphy Parsons | — | $59.95 |   |
| Johnston & Murphy Penny | — | $119.95 |   |
| Johnston & Murphy Stockton | — | $59.95 |   |
| Johnston & Murphy Upton | Grey, Navy, Tan | $155.00 |   |

A generic "Johnston & Murphy" item at $159.95 also exists. LaMilano ($89.95, no vendor) and Importex IM-98213 (inactive vendor) are excluded.

---

## Appendix C. Migration of current items

Maps every current item to its target matrices and states what happens to it. Built from the current inventory export.

### C.1 Dispositions

| Disposition | Meaning |
|---|---|
| **Migrate** | Count physical stock into items in the target matrix (creating variants as found). Zero the old item's quantity with an inventory adjustment, then archive it. |
| **Rebuild** | Item is already in a matrix but the matrix lacks attributes (color-only). Recreate its stock as Color/Size/Fit items in the target matrix, then zero and archive the old items. |
| **Split** | One existing matrix becomes several target matrices; each item is reassigned by physical check. |
| **Sell through** | No longer reordered (inactive vendor, legacy style, or category not carried). Leave the item as-is until stock reaches zero, then archive. |
| **Keep** | Remains a single price-point item (accessories, services). |
| **TBD** | Depends on an open question; noted per row. |

Rules for every disposition:

- **Archive, never delete.** Sales history is attached to the old items and must stay reportable.
- **Zero before archive.** Placeholders carry large negative quantities (e.g. −1,027 on standard ties) that distort inventory value. **Confirm** whether Lightspeed allows archiving an item with nonzero stock.
- **Vendor status.** All vendors in §5.1–5.2 are confirmed active. "Sell through" rows for active vendors (MM Fashion, Braveman, Nevima, Peerless legacy styles, Marina SB2-21201, San Malone J60313) refer to discontinued styles, not discontinued vendors; those vendors' current lines get matrices once identified (§13).

### C.2 Order of operations

1. Immediate fixes (§13): Caravelli default price, vendor record cleanup, catalog vendors.
2. Create target matrices for active lines (§7, §8) with plain-language descriptions, default price and cost (Appendix A), category, and tags. No variants yet.
3. Physical count by section; Claude creates variants and receives counted stock into them.
4. Zero and archive the old items per this appendix.
5. Intake (§11) goes live per vendor once that vendor's matrices are migrated.

### C.3 Suits

| Current item(s) | Retail | Target matrices | Disposition |
|---|---|---|---|
| Caravelli `{Color} {Size} {Fit}` (~470 items, existing matrix) | $599.95 | Caravelli Suit (existing matrix, renamed); Caravelli Vest | **Split**: suit items stay; vests from 3pc stock counted into Caravelli Vest; fix default price first |
| Caravelli Navy 42R Slim ×2 (Custom SKU 2740, 5919) | $599.95 | Caravelli Suit | Merge into one item |
| Caravelli Suits (generic) | $599.95 | Caravelli Suit; Caravelli Vest | Migrate |
| Pronto Moda – 100% Wool (brand Caravelli) | $699.95 | **TBD** — separate San Malone wool line? | TBD |
| Angelo Rossi `{Color}` ×7, Angelo Rossi Promo (existing color-only matrix) | $549.95 | Angelo Rossi Suit | Rebuild |
| Calvin Klein Suits (generic); CK-FX2052, CK-FY0079, CK-FY0423 | $695.95–$799.95 | Calvin Klein Suit | Migrate |
| Canaletto Suit | $1,195.95 | Canaletto Suit | Migrate |
| DKNY Suits | $599.95 | DKNY Suit | Migrate |
| Desen Suit | $599.95 | Desen Suit | Migrate |
| Dray Scott – Italian Woven Wool Suit | $1,495.95 | Dray Scott Suit | Migrate |
| Eisenberg `{Color} {Size}` (31 items, not matrixed) | $695.95–$799.95 | Eisenberg Suit | Migrate; merge duplicates (Black 40S ×3; Black 40XL, 48XL, 52XL ×2); correct $175.00 item |
| Enzo Suits | $1,099.95 | Enzo Tovare Wool Suit | Migrate |
| Enzo Linen Suit | $795.95 | Enzo Tovare Linen Suit | Migrate |
| Giorgio Fiorelli Suits (generic); Giorgio Fiorelli matrix (empty) | $599.95 | Giorgio Fiorelli Suit; Giorgio Fiorelli Stretch Suit; Giorgio Fiorelli Vest | Migrate; reuse or archive the empty matrix |
| Boy's Suit (Giorgio Fiorelli) | $399.95 | — | Sell through (no boys' items) |
| Giorgio Sanetti Wool Suit | $599.95 | Giorgio Sanetti Suit | Migrate |
| Harmony Suit | $899.95 | Harmony Suit | Migrate |
| Jones New York Suit | $699.95 | — | Sell through (Marcraft disabled) |
| Kenneth Cole Suit | $549.95 | Kenneth Cole Suit | Migrate |
| Manchester Suits | $599.95 | Manchester & Tailor Suit | Migrate |
| Mantoni Suit; Mantoni 54XL Brown | $795.95; $749.95 | Mantoni Wool Suit; Mantoni Wool Double-Breasted Suit; Mantoni Stretch Suit; Mantoni Tweed Suit; Mantoni Vest (per stock) | Migrate |
| Marc New York Suit | $599.95 | Marc New York Suit | Migrate |
| Marco Valentino 40R Slim Suit | $1,195.00 | Marco Valentino Suit | Migrate |
| Michael Kors Suit | $749.95 | Michael Kors Suit | Migrate |
| Mundo Suits | $239.95 | Mundo Suit | Migrate |
| Paul Betenly Suit | $1,295.95 | Paul Betenly Suit | Migrate |
| Aristo – Stretch Suit (brand Paul Betenly) | $599.95 | Paul Betenly Stretch Suit | Migrate |
| Phita DB Suit | $549.95 | — | Sell through (Phita disabled) |
| Ralph Lauren Suit | $749.95 | Ralph Lauren Suit | Migrate |
| Renoir Suit | $599.95 | Renoir Suit; Renoir Stretch Suit; Renoir Linen Suit (per stock) | Migrate |
| Renoir Wool Suit | $795.95 | Alessandro Vitello Wool Suit (or Renoir Wool Suit, §13 Q1); Alessandro Vitello Wool Stretch Suit; Alessandro Vitello Wool Blend Suit (per stock) | Migrate |
| Renoir Premium Wool Suit | $1,199.95 | Rivelino Suit (or Renoir Premium Wool Suit, §13 Q1) | Migrate |
| Retro Paris Black; Retro Paris Light Grey Ultra Slim; Retro Paris Indigo Ultra Slim | $499.95–$549.95 | Retro Paris Suit | Migrate |
| Luciano Suit – Stretch (brand Retro Paris, vendor Paris By Night) | $129.95 | **TBD** — Luciano is a line or a separate brand | TBD |
| Capucci Suit – 4 Way Stretch (brand Sharp) | $499.95 | Sharp Stretch Suit | Migrate |
| TailoRED | $1,895.95 | TailoRED Suit | Migrate |
| Tallia Suits | $849.95 | Tallia Suit | Migrate |
| Tiglio Suit | $899.95 | Tiglio Suit | Migrate |
| Tommy Hilfiger Suit (brand "Tommmy Hilfiger") | $749.95 | Tommy Hilfiger Suit | Migrate; fix brand |
| Tweed Suits (brand "Tweed," Marina Imports) | $699.95 | **TBD** — actual brand on label | TBD |
| Venezia Suit Wool Stretch | $699.95 | Venezia Wool Stretch Suit | Migrate |
| Vitarelli Suit | $649.95 | Vitarelli Suit | Migrate |
| 800 Suit; 320-0S2 Suit Grey 62R Classic (Cosani) | $599.95; $399.95 | **TBD** — Angelo Rossi lines or other Cosani brand | TBD |
| LG2516 Suit (L.A. Gibbs) | $549.95 | L.A. Gibbs Suit | Migrate |
| SB2-21201 Suit Charcoal 42S (Marina Imports) | $495.95 | — | Sell through (legacy style) |
| AF-0467, AR-0201, AR-BA0054, BB-Z0041, SJ-Z0978 (Peerless legacy styles) | $395.95–$895.95 | — | Sell through |
| MM-1500 ×4, MM-19200 (MM Fashion) | $449.95–$499.95 | — | Sell through |
| BR-UOMO (Braveman), MI-7511 (Nevima) | $499.95; $549.95 | — | Sell through |
| C602F, C602FV, D62ETL, S62KR, T62LU ×2, US631 (Actex); GB-LM2431 ×3, DR-001 (Lanier); P&L-64001 | $299.95–$499.95 | — | Sell through (vendors disabled) |

### C.4 Tuxedos

| Current item(s) | Retail | Target matrices | Disposition |
|---|---|---|---|
| Caravelli Tux Black / Midnight Blue / Light Grey / Medium Grey (existing color-only matrix) | $599.95 | Caravelli Tuxedo | Rebuild; matrix default $549.95 ≠ item price — **Confirm** |
| Giorgio Fiorelli Tux; Giorgio Fiorelli Tuxedo matrix (color-only) | $599.95 | Giorgio Fiorelli Arlan / Shawl / Oscar Tuxedo / Tuxedo; Giorgio Fiorelli Tuxedo Vest (per stock) | Rebuild; matrix default $499.95 ≠ item price — **Confirm** |
| Enzo Tuxedo | $1,099.95 | Enzo Tovare Tuxedo / Peak Lapel Tuxedo / Shawl Tuxedo (per stock) | Migrate |
| Mantoni Tuxedo | $795.95 | Mantoni Tuxedo / Peak Lapel / Shawl / Double-Breasted (per stock) | Migrate |
| Paul Betenly Tuxedo | $1,295.95 | Paul Betenly Tuxedo | Migrate |
| Renoir Tuxedo | $599.95 | Renoir Notch Lapel / Peak Lapel / Shawl / Double-Breasted Tuxedo; Renoir Tailcoat (per stock) | Migrate |
| Renoir Luxury Wool Tuxedo | $795.95 | Alessandro Vitello Notch Lapel / Peak Lapel Tuxedo (per stock) | Migrate |
| L.A. Gibbs Tuxedo | $549.95 | L.A. Gibbs Tuxedo | Migrate |
| Calvin Klein Tuxedo (no brand) | $799.95 | Calvin Klein Tuxedo | Migrate; set brand |
| YS61W-Slim Tux (Actex) | $339.95 | — | Sell through |

### C.5 Sport coats, blazers, outerwear, vests

| Current item(s) | Retail | Target matrices | Disposition |
|---|---|---|---|
| Caravelli Sport Coat | $299.95 | Caravelli Sport Coat | Migrate |
| Caravelli Wool Gab Blazer; Caravelli Wool Gab Blazer XL | $299.95 | Caravelli Wool Blazer (XL is a size, not a separate item) | Migrate |
| Enzo Sport Coat ×2 (Custom SKU 13196, 13282) | $499.95 | Enzo Tovare Sport Coat; Enzo Tovare Soft Jacket (per stock) | Migrate |
| Giorgio Fiorelli Sport Coat; Giorgio Fiorelli Navy Blazer | $399.95 | Giorgio Fiorelli Sport Coat; Giorgio Fiorelli Blazer | Migrate |
| Mantoni Sport Coat | $499.95 | Mantoni Sport Coat; Mantoni Wool Blazer; Mantoni Waffle Weave Sport Coat (per stock) | Migrate |
| Renoir Sport Coat | $449.95 | Renoir Blazer; Pellagio Riserva Sport Coat lines (per stock) | Migrate |
| Renoir Luxury Wool Sportcoat/Blazer (brand "Renoir Wool") | $599.95 | Alessandro Vitello Wool Blazer | Migrate; fix brand |
| Harmony / Kenneth Cole / Tallia / Tiglio / Paul Betenly / L.A. Gibbs Sport Coat | $149.95–$599.95 | `{Brand} Sport Coat` | Migrate |
| Designer Sport Coat (Peerless, no brand) | $499.95 | **TBD** — brand on label | TBD |
| J60313 Sport Coat 44XL (San Malone); P&L-9006 Sport Coat Grey 42L | $129.95; $149.95 | — | Sell through |
| Designer Outerwear (brand Enzo, vendor Peerless) | $129.95 | **TBD** — brand and vendor conflict | TBD |
| Vest (San Malone) | $49.50 | Caravelli Vest | Migrate |

### C.6 Pants

| Current item(s) | Retail | Target matrices | Disposition |
|---|---|---|---|
| Caravelli Pants | $159.95 | Caravelli Dress Pants | Migrate |
| Renoir Pants | $159.95 | Renoir Dress Pants | Migrate |
| Renoir Wool Pants (brand "Renoir Wool") | $249.95 | Alessandro Vitello Dress Pants | Migrate; fix brand |
| Mantoni Pants | $249.95 | Mantoni Dress Pants | Migrate |
| Giorgio Fiorelli Pants | $159.95 | Giorgio Fiorelli Dress Pants | Migrate |
| Enzo Pants | $249.95 | **TBD** — an Enzo Tovare denim or stretch-pant model, or Cotton Pants (check stock) | TBD |
| Enzo Linen Pants | $199.95 | Enzo Tovare Linen Pants | Migrate |
| Eisenberg Singer Pants | $149.95 | Eisenberg Dress Pants | Migrate |
| Angelo Rossi Pants; 320-PE0-1 Pants (Cosani) | $89.95 | Angelo Rossi Dress Pants | Migrate |
| Calvin Klein / DKNY / Kenneth Cole / Michael Kors / Ralph Lauren / Tallia / Tommy Hilfiger Pants (Peerless) | $99.95–$249.95 | `{Brand} Dress Pants` | Migrate |
| Harmony / Tiglio / Paul Betenly / Vitarelli / Desen / Cianni / Ascott Brown Pants | $89.95–$349.95 | `{Brand} Dress Pants` | Migrate |
| BN-5851 Pants (Ballin) | $249.95 | — | Sell through (Ballin disabled) |

### C.7 Shirts and sweaters

| Current item(s) | Retail | Target matrices | Disposition |
|---|---|---|---|
| Dress Shirts (Standard) (brand "Marquis / Vitarelli") | $44.95 | Marquis Dress Shirt; Vitarelli Dress Shirt | Migrate; split brand |
| Premium Dress Shirts (Christopher Lena) | $59.50 | Christopher Lena Dress Shirt | Migrate |
| Giorgio Fiorelli Boys Shirt | $29.95 | — | Sell through (no boys' items) |
| DeNiko Sweaters | $19.95 | — | Sell through (DeNiko disabled) |

### C.8 Shoes

| Current item(s) | Target matrices | Disposition |
|---|---|---|
| Stacy Adams styles in the Fall 2026 catalog (Appendix B.1, 25 styles, 35 current items) | One matrix per style; existing colors become Color values | Migrate |
| Stacy Adams styles not in the Fall 2026 catalog (Appendix B.2, 57 items) | — | Sell through, unless still ordered (§13) |
| Florsheim and Johnston & Murphy style-color items | One matrix per style (Appendix B.3, B.4) | Migrate |
| Johnston & Murphy (generic, $159.95) | Appendix B styles | Migrate |
| LaMilano Shoes (no vendor) | **TBD** | TBD |
| IM-98213 Shoes (Importex) | — | Sell through (Importex disabled) |

### C.9 Accessories and services

| Current item(s) | Disposition |
|---|---|
| All accessories (ties, bow ties, pocket squares, belts, suspenders, cufflinks, stays, etc.) | Keep as price-point items until accessories are designed (§9) |
| Cumberbund (Daniel Elissa) | Keep; repoint to an active vendor or sell through |
| Alteration, Shipping, SPECIAL ORDER | Keep (Services) |
| Clearance (no category, $399.95) | **TBD** — keep as a non-inventory sale-time item or archive |
| Garment Bag (no vendor) | Keep |
