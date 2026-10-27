# Suits 4 Men – Lightspeed MCP server

A local MCP server that lets Claude Desktop read the Suits 4 Men Lightspeed
Retail (R-Series) account (items, matrices, attribute sets, vendors,
categories, purchase orders and stock) and, with your approval, make five
narrow kinds of change.

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
saved token has all 9 scopes. Re-authorizing changes no inventory data. Run it
from a normal PowerShell window: from the Claude app's Terminal panel, open one with
`Start-Process powershell -WorkingDirectory "C:\Development\Suits 4 Men Inventory"`.

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

### Write tools

| Tool | What it sends | What it never does |
|---|---|---|
| `create_matrix` | `POST ItemMatrix.json`: a new, empty matrix under attribute set 5 Color/Size/Fit | Update or archive an existing matrix |
| `add_matrix_variants` | `POST Item.json` per new child item (color, size, fit, own custom SKU, the price and cost you give) | Touch the matrix or its existing items |
| `update_item_fields` | `PUT Item/<id>.json` with only the changed fields among custom SKU, default cost, description | Change prices, stock or anything else |
| `add_po_lines` | `POST OrderLine.json` per new line on an open PO | Receive anything |
| `receive_po_lines` | `PUT OrderLine/<id>.json` with the line's new `numReceived` total | Change stock (see below) |

`receive_po_lines` marks goods as received on the PO. Stock on hand changes
only when someone presses **Add Received To Inventory** on that PO in
Lightspeed: the API has no call for that step.

The tools refuse (and say which rule) anything that breaks the store's rules:

- a matrix outside attribute set 5;
- a matrix description that names a color, lacks the garment type, or holds a vendor code;
- 3-piece or boys' items;
- pants sizes that aren't waist-only;
- a custom SKU that isn't 6 digits or is already used (archived items included);
- a duplicate variant;
- receiving more than was ordered.

Color and fit values are sent exactly as given, never re-cased. Prices are
only ever set on new records, and only to the price you give. Matrix 7's own
price (61.21, equal to its cost) is never copied. The patterns behind the text
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
