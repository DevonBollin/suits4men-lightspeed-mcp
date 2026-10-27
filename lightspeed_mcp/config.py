"""Fixed settings for the Suits 4 Men Lightspeed MCP server.

Nothing in this file is secret. Secrets (client ID/secret, tokens) live only in
Windows Credential Manager; see secret_store.py.
"""

from pathlib import Path

# Project folder (the one containing server.py and bootstrap.py).
PROJECT_DIR = Path(__file__).resolve().parent.parent

# Local, non-secret runtime files. The token lock lives here rather than in
# %APPDATA% because Claude Desktop is a packaged (MSIX) app, and Windows can
# redirect a packaged app's AppData writes to a private copy. A lock that the
# server and bootstrap.py saw in two different places would not be a lock.
STATE_DIR = PROJECT_DIR / "state"
TOKEN_LOCK_PATH = STATE_DIR / "token.lock"

# Lightspeed endpoints (verified against developers.lightspeedhq.com/retail, Oct 2026).
AUTHORIZE_URL = "https://cloud.lightspeedapp.com/auth/oauth/authorize"
TOKEN_URL = "https://cloud.lightspeedapp.com/auth/oauth/token"
API_ROOT = "https://api.lightspeedapp.com/API/V3/"

# Phase 1 scopes: everything inventory management needs to READ.
#
# Only inventory_read and register_read are read-only scopes. Lightspeed has
# no read-only version of the others, so they also allow writes; the HTTP
# client's GET-only guard is what keeps Phase 1 read-only.
#
# Phase 2 swaps inventory_read for employee:inventory (and adds
# employee:product_edit if creating matrices needs it), then bootstrap.py is
# re-run. Changing this list always means re-running bootstrap.py.
SCOPES = [
    "employee:inventory_read",   # items, stock (ItemShop), attribute sets
    "employee:product_cost",     # ItemMatrix (403 without it), InventoryLog, vendor catalogs
    "employee:purchase_orders",  # purchase orders, lines, received shipments
    "employee:vendors",          # vendors
    "employee:categories",       # categories
    "employee:inventory_counts", # inventory counts and their discrepancies (not reconcile)
    "employee:register_read",    # sales history, for sell-through (read-only scope)
    "employee:special_orders",   # customer special orders
    "employee:vendor_returns",   # returns to vendors
]

# Prefix for every Windows Credential Manager entry this project creates.
KEYRING_PREFIX = "suits4men-lightspeed"

# Refresh the access token when it has less than this many seconds left.
REFRESH_MARGIN_SECONDS = 120

# How long one process waits for another to finish refreshing tokens.
TOKEN_LOCK_TIMEOUT_SECONDS = 60

HTTP_TIMEOUT_SECONDS = 30
USER_AGENT = "suits4men-lightspeed-mcp/0.1"
