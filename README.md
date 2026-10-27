# Suits 4 Men – Lightspeed MCP server

A local MCP server that lets Claude Desktop read the Suits 4 Men Lightspeed
Retail (R-Series) account (items, matrices, attribute sets, vendors,
categories, purchase orders and stock) and, with your approval, make narrow,
task-shaped changes: items and matrices, SKUs, vendor numbers, purchase orders
and their lines, and whole invoices or packing slips in one approval.
Matrices can never be archived through it: that stays manual in Lightspeed.

- **Read tools** (Phase 1) use a client that refuses every request except GET.
  They can never change Lightspeed.
- **Write tools** (Phase 2) default to dry runs: they show exactly what they
  would send and send nothing. A real write needs **all** of the following:
  you have turned writes on by hand, you have seen that write's dry run and
  said yes in chat, and the call carries that dry run's confirm code.
  **Writes ship turned off.**

Secrets (client ID, client secret, tokens) are kept only in Windows Credential
Manager, under entries named `suits4men-lightspeed/...`. They are not in any
file in this folder, and not in the Claude Desktop config.

## Files

| File | What it is |
|---|---|
| `bootstrap.py` | One-time authorization (and re-authorization) |
| `server.py` | The MCP server Claude Desktop runs |
| `install_claude_desktop.py` | Adds the server to Claude Desktop's config |
| `check_tools.py` | Calls the read tools against the live account; `--dry-runs` calls the write tools as dry runs |
| `backup_lightspeed.py` | Saves a read-only snapshot of the inventory data; `--compare` diffs two snapshots |
| `update.ps1` | Updates another computer (the store's) to the newest released version, safely |
| `lightspeed_mcp/client.py`, `reader.py` | GET-only client and the read tools' logic |
| `lightspeed_mcp/write_path.py` | The only code that can write: master switch, allowed requests, write log |
| `lightspeed_mcp/writer.py`, `rules.py` | The write tools' logic and the store's business rules |
| `tests/` | Unit tests against a mocked Lightspeed (nothing reaches the real one) |
| `state/` | Token-refresh lock, the write switch (when on) and the write log. Not in git |
| `backups/` | Snapshots from `backup_lightspeed.py`. Not in git (they contain costs) |
| `NOTES.md` | How the real API and the store's data behave |

The folder is a git repository (branch `main`, nothing pushed anywhere).
`.venv/`, `state/` and `backups/` are never committed, and no secrets are in any file.

## Install

You need [uv](https://docs.astral.sh/uv/), which is already on the store computer.
Open PowerShell in the project folder:

```powershell
uv sync
.venv\Scripts\python.exe install_claude_desktop.py
```

`install_claude_desktop.py` backs up `%APPDATA%\Claude\claude_desktop_config.json`
and adds a `lightspeed` entry to it. Other servers and settings are left alone.
Re-run it if you move this folder.

After installing, **fully quit Claude Desktop** (right-click the tray icon, then
Quit) and start it again.

## Bootstrap (first-time authorization)

Run this in a normal PowerShell or Command Prompt **window**. In the Claude
app's built-in Terminal panel and in Git Bash, the hidden client ID and secret
prompts don't receive what you type.

```powershell
.venv\Scripts\python.exe bootstrap.py
```

1. Enter the client ID and secret from the Lightspeed developer portal. Typing is hidden.
2. A Lightspeed page opens. Sign in as the **owner or an admin** and approve.
   The token can only do what the approving employee is allowed to do.
3. The browser then shows a connection error for `https://localhost/callback`.
   That is expected. Copy the **whole address** from the address bar and paste
   it into the terminal **right away**: the code inside it expires after 60 seconds.
4. The script saves the tokens and prints the account ID and shop name.

You don't need to restart Claude Desktop after bootstrap. The server reads the
saved tokens on every call.

## Re-authorize

Run `bootstrap.py` again, the same way. Press Enter at the client ID and secret
prompts to keep the saved ones. Re-authorize when:

- a tool says Lightspeed rejected the refresh token (`invalid_grant`), or says to run bootstrap.py;
- access was revoked in Lightspeed;
- the server wasn't used for **30 days**, which is how long Lightspeed lets an unused refresh token live;
- the scopes change (`SCOPES` in `lightspeed_mcp/config.py`).

Phase 2 changed the scopes (`employee:inventory` replaces
`employee:inventory_read`), and bootstrap was re-run on 2026-10-26: the
saved token has those 9 scopes.

**Pending:** creating or renaming brands needs `employee:manufacturers`, added
afterwards. Run bootstrap once more before the first brand change (everything
else, vendors and categories included, works with the current token). Re-authorizing changes no inventory data. Run it
from a normal PowerShell window: from the Claude app's Terminal panel, open one with
`Start-Process powershell -WorkingDirectory "C:\Development\Suits 4 Men Inventory"`.

## Two computers: developing here, running at the store

The code lives in a private GitHub repository:
`https://github.com/DevonBollin/suits4men-lightspeed-mcp`.

- The **developer's computer** works on the `main` branch.
- The **store's computer** runs only released versions: the `stable` branch.
- Each computer has its **own** Lightspeed login, write switch, write log and
  snapshots. None of those are in git, so nothing that matters to one computer
  travels to the other.

Both computers work on the same live Lightspeed account. Dry runs, the write
switch and per-write approval are what keep testing safe.

### Setting up another computer (once)

1. **Install** [Claude Desktop](https://claude.ai/download),
   [Git for Windows](https://git-scm.com/download/win) and
   [uv](https://docs.astral.sh/uv/getting-started/installation/), then open a new
   PowerShell window so they are found.
2. **Access to the repository:** that computer needs a GitHub login that can
   read it. Either add its owner's GitHub account as a collaborator (repository
   Settings > Collaborators), or create a fine-grained token with read-only
   access to this one repository. The first `git clone` asks for it in a browser
   window (Git Credential Manager).
3. **Get the released version and its packages:**

   ```powershell
   git clone -b stable https://github.com/DevonBollin/suits4men-lightspeed-mcp.git "C:\Suits 4 Men\Lightspeed"
   cd "C:\Suits 4 Men\Lightspeed"
   uv sync
   .venv\Scripts\python.exe -m pytest -q
   .venv\Scripts\python.exe install_claude_desktop.py
   ```

4. **A Lightspeed login of its own.** In Lightspeed's developer portal, create a
   second API client for this computer, with the redirect URI
   `https://localhost/callback`. Use that client's ID and secret here, so each
   computer can be revoked without affecting the other. Then run the bootstrap
   steps above (`bootstrap.py`, in a normal PowerShell window), signing in as the
   owner or an admin.
5. **Restart Claude Desktop** (Quit from the tray icon). Ask Claude for
   `server_info`: it should show branch `stable`, the account, the login's
   permissions, and `writes_enabled: false`.

Writes stay **off** on that computer until someone there creates
`state\WRITES_ENABLED` by hand (see "Turning writes on and off").

### Releasing a new version (developer's computer)

1. Work on `main`, run the tests (`.venv\Scripts\python.exe -m pytest`) and, for
   anything that touches Lightspeed, `check_tools.py` and `check_tools.py --dry-runs`.
2. Raise `version` in `pyproject.toml` and run `uv lock`, so `server_info` shows
   which release a computer runs. Commit.
3. Publish `main`, then release it to `stable`:

   ```powershell
   git push origin main
   git push origin main:stable
   ```

### Updating the store's computer

In PowerShell, in its project folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\update.ps1
```

`-ExecutionPolicy Bypass` lets Windows run this one script and changes no
setting. The script:

1. refuses to run if files were edited on that computer;
2. moves to the newest `stable` (fast-forward only);
3. runs `uv sync`;
4. runs the tests, and if any fail, goes back to the version it started from;
5. tells you to restart Claude Desktop.

It never touches `state\` (the switch and the log), `backups\` or the saved login.
Updating is always a deliberate step, never automatic, because these tools can
change the live inventory.

## Revoke access

1. In Lightspeed Retail, open **Settings > Client API Access**, find this
   application and revoke it. Both tokens stop working immediately.
2. Remove the saved secrets from this computer:

   ```powershell
   .venv\Scripts\python.exe bootstrap.py --forget
   ```

3. Optionally, remove the `lightspeed` entry from
   `%APPDATA%\Claude\claude_desktop_config.json`.

## How tokens are handled

Access tokens last about an hour. Each refresh returns a new access token *and*
a new refresh token, and the old refresh token dies once the new access token is
used. To avoid breaking the connection, the server:

- refreshes only when the token is close to expiry, or after a 401 (then retries once);
- saves the new pair to Credential Manager and reads it back *before* using it;
- allows one refresh at a time across all processes, using a lock file in `state/`;
- stops and asks for bootstrap if Lightspeed answers `invalid_grant`, instead of retrying.

Windows Credential Manager caps an entry at 1280 characters, so tokens are split
across several entries and switched over atomically. See
`lightspeed_mcp/secret_store.py`.

## Tools

| Tool | Use |
|---|---|
| `search_items` | Find items by description words, UPC, custom SKU, manufacturer SKU, vendor or category |
| `get_item` | One item in full: matrix, attributes, vendor, cost, prices, stock |
| `search_matrices`, `get_matrix` | Find matrices; one matrix with its attribute values and every child item |
| `list_attribute_sets` | Matrix types and their attribute names |
| `list_vendors`, `list_manufacturers`, `list_categories` | Reference lists |
| `list_purchase_orders`, `get_purchase_order` | POs with their lines and receipts |
| `get_stock_history` | Every stock change for an item, a matrix or the whole store, by reason |
| `get_sales_history` | Units sold and net sales; for a matrix, by color, size and fit; store top sellers |
| `find_stock_issues` | Negative stock, missing or duplicate SKUs, below reorder point, no cost or price, not in a matrix |
| `inventory_counts` | Counts, and expected vs counted per item |
| `vendor_returns`, `list_special_orders` | Returns to vendors; customer special orders |
| `search_vendor_catalog` | Exact UPC or style-number lookups in Lightspeed's vendor catalogs |
| `api_get` | Read-only GET of an inventory endpoint, raw JSON. Customer, employee and payment data are refused |
| `suggest_custom_skus` | The next unused 6-digit SKUs, after the highest in use (archived items included, so old numbers are never reused) |
| `show_write_log` | Whether writes are on, and every write sent from this computer: payload, outcome, status, affected IDs |
| `take_snapshot` | Saves a snapshot to `backups\` in the background (about 75 s). GET only; it adds files on this computer |
| `compare_snapshots` | Lists snapshots and compares two (by default the two newest), field by field |
| `match_invoice_lines` | Matches an invoice's or packing slip's lines to items: existing, new variant, new item, or unresolved (with candidates) |
| `server_info` | What runs on this computer: version (branch and commit), computer, account, login permissions, write switch, writes logged, newest snapshot |

### Write tools

| Tool | What it sends | What it never does |
|---|---|---|
| `create_matrix` | `POST ItemMatrix.json`: a new, empty matrix under attribute set 5 Color/Size/Fit | Update or archive an existing matrix |
| `add_matrix_variants` | `POST Item.json` per new child item (color, size, fit, own custom SKU, the price and cost you give) | Touch the matrix or its existing items |
| `update_item_fields` | `PUT Item/<id>.json` with only the changed fields among custom SKU, default cost, description | Change prices, stock or anything else |
| `add_po_lines` | `POST OrderLine.json` per new line on an open PO | Receive anything |
| `receive_po_lines` | `PUT OrderLine/<id>.json` with the line's new `numReceived` total | Change stock (see below) |
| `move_items_into_matrix` | `PUT Item/<id>.json` per item: its matrix and Color/Size/Fit, plus (unless told not to) the matrix's description pattern, category, manufacturer and vendor | Send prices, cost, SKU or stock |
| `assign_custom_skus` | `PUT Item/<id>.json` per item with only `customSku`, for items that have none; empty ones get the next free SKU | Replace an existing SKU |
| `archive_item` | `DELETE Item/<id>.json` for **one** item with stock exactly 0 and on no open PO (Lightspeed archives on DELETE) | Archive a matrix, or an item with stock |
| `create_items` | `POST Item.json` per new item outside any matrix (shirts, ties, shoes), own SKU, cost, price | Create pants (they go in a pants matrix) |
| `create_purchase_order` | `POST Order.json`: a new, empty PO with the vendor's reference and shipping/other costs | Reuse a reference the vendor already has on a PO |
| `update_purchase_order` | `PUT Order/<id>.json`: reference, costs, ordered date (→ Ordered), received date (→ Check In) | Mark a PO finished or archive it |
| `update_po_lines` | `PUT OrderLine/<id>.json`: quantity ordered and/or unit cost | Go below what was received, or re-cost what was checked in |
| `remove_po_lines` | `DELETE OrderLine/<id>.json` for lines with nothing received (Lightspeed deletes them for good; logged first) | Remove a line with anything received |
| `set_vendor_numbers` | `PUT Item/<id>.json` with the vendor's own number or style code (Lightspeed's Vendor ID), keeping the item's other vendor numbers | Give an item a second number for the same vendor |
| `intake_invoice` | A whole invoice or packing slip in one approval: PO, new items, vendor numbers, lines with what was received, Check In | Add the goods to stock (a click in Lightspeed) |
| `update_matrix` | `PUT ItemMatrix/<id>.json`: description, category, brand, vendor, default cost, Default price, only what changes | Change the attribute set or values, or archive |
| `create_vendor` | `POST Vendor.json`: name (as the vendor writes it) and account number | Create a duplicate or near-duplicate (see below); update or archive a vendor |
| `create_brand` | `POST Manufacturer.json`: the brand's name | Create a duplicate or near-duplicate; Lightspeed can't delete a brand |
| `update_item_prices` | `PUT Item/<id>.json` with all three price levels, only the asked-for ones changed | Change anything but prices; it is the only tool that changes an existing item's price |
| `set_reorder_levels` | `PUT ItemShop/<id>.json`: reorder point and level for the shop | Send stock (no route can) |
| `archive_purchase_order` | `DELETE Order/<id>.json` (Lightspeed archives) for a PO where nothing was received; logged first | Archive a PO with goods received, or a finished one |
| `correct_vendor_numbers` | `PUT Item/<id>.json`: an existing vendor number's new value (its ID and cost kept) | Remove a vendor number (not documented) |
| `update_vendor` / `archive_vendor` | `PUT Vendor/<id>.json` (name, account number) / `DELETE` (archives) | Archive a vendor still used by an active item or an open PO |
| `rename_brand` | `PUT Manufacturer/<id>.json`: name | Take another brand's name |
| `create_category` / `rename_category` / `delete_category` | `POST`/`PUT Category` (name, path, parent) / `DELETE` (for good) | Rename one with subcategories; delete one with items, matrices or subcategories |
| `move_category` | `PUT Category/<id>.json`: new parent and path; its items and matrices move with it | Move one with subcategories (rebuild a branch leaf by leaf), or rename it |
| `create_inventory_count` | `POST InventoryCount.json` + one `POST InventoryCountItem.json` per counted item | **Reconcile** (the step that changes stock stays a click in Lightspeed) |
| `create_attribute_set` / `update_attribute_set` | `POST`/`PUT ItemAttributeSet`: a set's name and its 1-3 attribute names (one is always Color) | Touch Lightspeed's built-in sets 1-4; remove, swap or move Color in a set that matrices use |
| `reassign_items` | `PUT Item/<id>.json` per item: default vendor, brand and/or category, only what changes | Touch an item in a matrix (`update_matrix` does the whole matrix), or prices, cost, stock and vendor numbers |
| `set_upcs` | `PUT Item/<id>.json` per item with only `upc`: the barcode number from its box or tag | Save a UPC that fails its check digit or is already on another item (archived ones too) |

`receive_po_lines` marks goods as received on the PO. Stock on hand changes
only when someone presses **Add Received To Inventory** on that PO in
Lightspeed: the API has no call for that step.

`move_items_into_matrix` is for items that belong in a matrix but aren't in
one (e.g. 2321 "Caravelli Burgundy 58L Slim" outside matrix 7), and for moving
the items of a Color-only matrix (9, 10) into a new Color/Size/Fit one: create
the new matrix, move the items, then archive the empty old matrix by hand.
Lightspeed's docs don't describe moving existing items, so the tool moves
**one item per call until a move has been done and checked**.

`archive_item` refuses any item with stock (positive or negative):
Lightspeed zeroes an archived item's stock and that can't be undone. It saves
the item's values in the write log first, because Lightspeed's docs show
archiving also switching the item to non-inventory and not taxable.
Unarchiving may be possible but is not built.

#### Entering an invoice or packing slip

1. Give Claude the document (a PDF or a photo in the chat). It reads each line.
2. Claude runs `match_invoice_lines`: each line comes back as an existing
   item, a new size/color for a matrix, a new item outside any matrix, or
   unresolved with the closest candidates. Unresolved lines need a decision
   (which item, or that it is new and at what price).
3. Claude runs `intake_invoice` as a dry run: one plan for the whole
   document. It creates the PO (or uses the one you name), creates missing items
   with their own SKUs, saves vendor numbers, adds the lines at the invoice's
   unit cost with what was received, sets the ordered date and puts the PO in
   **Check In**. A vendor's invoice number that is already on a PO is refused,
   so one invoice can't be entered twice.
4. You approve it once, as for any write.
5. In Lightspeed, open the PO, check the received quantities against the
   goods, click **Add Received To Inventory**, then **Finished**. Lightspeed's API
   has no call for that step, so stock only changes when you click it.

A vendor Lightspeed doesn't have yet (`new_vendor`), or a new brand on a new
item (`new_brand`), is created first in the same plan, so a new supplier
doesn't block the intake.

New vendors and brands: a name an existing one already has is refused,
ignoring case, punctuation and Inc/LLC/Ltd, archived vendors included. A
near-match is refused too, unless you confirm it really is a different company
(e.g. "Tomy Hilfiger" next to "Tommy Hilfiger" and "Tommmy Hilfiger"). Checking
a new vendor reads all ~680 vendors, so that dry run takes about 20 seconds.

Vendor numbers: vendors print one code for a style and color (Renoir
`201-19`) on every size and fit, and often on the suit, vest and pants cut from
one fabric. That code is saved on each of those items (one number per vendor
per item). Invoice lines are matched by matrix and color/size/fit, not by the
code alone: when a code is used in several matrices, the line needs the matrix
for its garment type. The trade-off: Lightspeed's own lookups by Vendor ID
(its purchase-order import) find several items for such a code.

UPCs (barcode numbers, e.g. on shoe boxes): a line with a UPC matches the item
that has it. A new item or variant created by the intake gets the line's UPC,
and `add_matrix_variants`, `create_items` and `set_upcs` take one too. A UPC
must be 11-14 digits (Lightspeed's rule), pass its check digit, and belong to
one item only.

#### Inventory counts

`create_inventory_count` records counted quantities (a new count, or more items
on one still awaiting reconciliation); the dry run shows on-hand vs counted for
each item and the total change. **Stock only changes when you reconcile the count
in Lightspeed**, and only for the items in it. Reconciling can't be undone except by
adjusting each item, so it stays your click. Entries are recorded under employee
`COUNT_EMPLOYEE_ID` in `lightspeed_mcp/config.py` (1, the employee all past counts used).

Not available through Lightspeed's API at all: creating or changing vendor
returns (read-only), and special orders other than as a customer sale line (kept
out, like all customer data).

#### Updating a matrix

Lightspeed applies a matrix's description, category, brand and vendor to
**every item in it** ("Shared values"). A new description renames all of them
('Caravelli Suit Black 42R Slim'). The dry run says how many items each change
reaches and previews the new names. Price and default cost are defaults for new
items ("Default values"); Lightspeed's help says existing items keep their own
unless someone clicks Reset Items To Defaults, which is not confirmed for the
API, so the dry run also shows what the items sell at today. Take a snapshot
before the first matrix update and compare after.

#### Attribute sets (matrix types)

An attribute set names the 1–3 dimensions a matrix's items differ by: set 5
is Color / Size / Fit. **Suits, tuxedos, vests, pants and sport coats always use
set 5.** Other garments (shirts, shoes) may use a set the store makes with
`create_attribute_set`, e.g. Color / Neck / Sleeve, never one of Lightspeed's
built-in sets 1–4, and **every set has Color**, so a matrix never splits by color.
`create_matrix` takes `attribute_set_id` (5 by default); variants and moves take
their values by the matrix's attribute names (`{"Neck": "15.5"}`), or
color/size/fit for set 5.

While matrices use a set, `update_attribute_set` may rename it or its attributes
(every matrix on it shows the new names; the dry run lists them) or add one, but
refuses removing an attribute (Lightspeed is reported to refuse item updates
afterwards), moving Color, or swapping names between slots (items' values would
show under the wrong names). Lightspeed's docs on writing sets contradict
themselves, so the first one may be refused.

The tools refuse (and say which rule) anything that breaks the store's rules:

- suiting outside attribute set 5; a built-in set, or a set without Color, for any matrix;
- a matrix description that names a color, lacks the garment type, or holds a vendor code;
- 3-piece or boys' items;
- pants sizes that aren't waist-only;
- a custom SKU that isn't 6 digits or is already used (archived items included);
- a duplicate variant;
- receiving more than was ordered;
- archiving an item with stock or on an open PO;
- pants outside a pants matrix; a new item whose description an active item already has;
- an invoice number the vendor already has on a PO.

Color and fit values are sent exactly as given, never re-cased. Prices are
only ever set on new records and as a matrix's default, and only to the price
you give. Matrix 7's own price (61.21, equal to its cost) is never copied. The patterns behind the text
rules are at the top of `lightspeed_mcp/rules.py`.

#### Making a change

1. Ask Claude for the change. It runs the tool as a dry run. The result shows the
   summary, warnings, each request's method, path and JSON body, and a confirm code.
2. Read it. If it is right, say so in chat. A yes covers that one change only.
3. Claude calls the tool again with `dry_run=false` and the confirm code. If
   anything changed in between (your arguments or the data in Lightspeed), the code
   no longer matches and the call is refused until a fresh dry run is approved.

It is a good habit to take a snapshot before and after a real write: ask
Claude to take one (`take_snapshot`) and, afterwards, to compare the two
(`compare_snapshots`). Or run `.venv\Scripts\python.exe backup_lightspeed.py`.
`show_write_log` lists what was actually sent.

#### Turning writes on and off

Writes are **off** while the file `state\WRITES_ENABLED` does not exist. Nothing
in this project creates it. It is checked on every request, so no restart is needed.

Turn writes on (PowerShell, in the project folder):

```powershell
New-Item state\WRITES_ENABLED -ItemType File
```

Turn writes off again:

```powershell
Remove-Item state\WRITES_ENABLED
```

Turning writes on does not approve anything. Each write still needs its own
dry run and your yes in chat. Leaving writes off between jobs is the safe default.

#### Write log

Every write that is sent is appended to `state\write_log.jsonl`, one JSON
object per line, and the file is never rewritten. A `sending` line (tool, method,
path, payload, confirm code) is written just before the request goes out. A
`done` line (HTTP status, affected IDs) or a `failed` line (status, error) follows.
Tokens are never logged.

If a write ends in a network error or a 5xx, it is **not retried**: it may have
gone through. Check the record in Lightspeed first.

### Read access

The token can technically read customer and employee records: they come with
one of the scopes. No tool returns them, and `api_get` only accepts the
inventory endpoints listed in `API_GET_ENDPOINTS` (in `lightspeed_mcp/reader.py`).
It refuses any `load_relations` that would pull in customers, employees or
payments. To allow another inventory endpoint, add its name to that list.

The history tools read Lightspeed's whole log or sales for the period. A
store-wide month takes about 5–25 s, and a year takes longer.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest
```

The tests never touch the real write switch or write log, and fail if
anything does. To check every read tool against the live account (GET only),
the way Claude Desktop calls them:

```powershell
.venv\Scripts\python.exe check_tools.py
```

To see each write tool's dry run on real records (GET only, nothing is sent):

```powershell
.venv\Scripts\python.exe check_tools.py --dry-runs
```

## Backups

```powershell
.venv\Scripts\python.exe backup_lightspeed.py
.venv\Scripts\python.exe backup_lightspeed.py --list
.venv\Scripts\python.exe backup_lightspeed.py --compare 2026-10-26_202204 2026-10-26_203912
```

The first command saves every item (with stock, prices, costs, SKUs and
attributes), matrix, attribute set, category, manufacturer, vendor, tax class,
purchase order, PO line and receipt to `backups\<date_time>\` as JSON. It takes
about 75 seconds and sends GET only. `--list` shows the snapshots, and `--compare`
lists what was added, removed or changed between two of them, field by field.
In Claude Desktop, `take_snapshot` and `compare_snapshots` do the same
(the logic is in `lightspeed_mcp/snapshots.py`). A snapshot folder is complete
once it has `manifest.json`; if one stopped, `FAILED.txt` in it says why.
Normal store activity (sales change stock and timestamps) shows up in a comparison too.

Lightspeed has no "restore from file", so a snapshot is not a one-click undo.
It is an exact record of every value, to check changes against and, if needed,
to build an approved write that puts a value back.

## Troubleshooting

- Server log: `%APPDATA%\Claude\logs\mcp-server-lightspeed.log`
- After updating the code, **fully quit and restart Claude Desktop** so it sees new or changed tools.
- "Blocked a POST request" or "not one of the write tools' requests": something tried to send a request the
  server never sends. This is by design.
- "Writes to Lightspeed are turned OFF": the master switch is off (see "Turning writes on and off").
- "confirm_code … does not match": the arguments or the data changed since the dry run. Ask for a new dry run.
- "Timed out waiting for another process to finish refreshing": a refresh got stuck. Restart Claude Desktop.
