# Suits 4 Men – Lightspeed MCP server

A local MCP server that lets Claude Desktop **read** the Suits 4 Men Lightspeed
Retail (R-Series) account: items, matrices, attribute sets, vendors,
categories, purchase orders and stock.

Phase 1 is read-only. The HTTP client refuses every request except GET, so
nothing here can change Lightspeed.

Secrets (client ID, client secret, tokens) are kept only in Windows Credential
Manager, under entries named `suits4men-lightspeed/...`. They are not in any
file in this folder, and not in the Claude Desktop config.

## Files

| File | What it is |
|---|---|
| `bootstrap.py` | One-time authorization (and re-authorization) |
| `server.py` | The MCP server Claude Desktop runs |
| `install_claude_desktop.py` | Adds the server to Claude Desktop's config |
| `check_tools.py` | Calls every tool against the live account (read-only) |
| `lightspeed_mcp/` | Token handling, GET-only client, tool logic |
| `tests/` | Unit tests against a mocked Lightspeed |
| `state/` | Lock file used during token refresh (no secrets) |
| `NOTES.md` | Findings about the real API, for Phase 2 |

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
- the scopes change. Phase 2 needs write scopes, which means editing `SCOPES` in
  `lightspeed_mcp/config.py` and re-running bootstrap.

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

To check every tool against the live account (GET only), the way Claude Desktop calls them:

```powershell
.venv\Scripts\python.exe check_tools.py
```

## Troubleshooting

- Server log: `%APPDATA%\Claude\logs\mcp-server-lightspeed.log`
- A "Blocked a POST request" error means something tried to write. Phase 1 refuses all writes; this is by design.
- "Timed out waiting for another process to finish refreshing": a refresh got stuck. Restart Claude Desktop.
