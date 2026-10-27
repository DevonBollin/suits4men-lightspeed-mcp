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

# Scopes: everything inventory management needs to read, plus item writes.
#
# Phase 2 swapped employee:inventory_read for employee:inventory, which also
# lets the token create and update items. Holding a scope does not make a
# write possible on its own: the read client only sends GET, and writes go
# through write_path.py, which is off until the store owner turns it on.
#
# employee:product_edit is not requested: the ItemMatrix docs name only
# employee:product_cost. If creating a matrix ever answers 403, add it here.
#
# Changing this list only takes effect after re-running bootstrap.py.
SCOPES = [
    "employee:inventory",        # items, stock (ItemShop), attribute sets; create and update items
    "employee:product_cost",     # ItemMatrix (403 without it), InventoryLog, vendor catalogs, editing cost
    "employee:purchase_orders",  # purchase orders, lines, received shipments
    "employee:vendors",          # vendors (view, create, update)
    "employee:manufacturers",    # brands: create new ones (reading them needs no scope)
    "employee:categories",       # categories
    "employee:inventory_counts", # inventory counts and their discrepancies (not reconcile)
    "employee:register_read",    # sales history, for sell-through (read-only scope)
    "employee:special_orders",   # customer special orders
    "employee:vendor_returns",   # returns to vendors
]

# The employee that inventory-count entries are recorded under (Lightspeed requires one per
# counted item). Employee 1 counted every item in the store's counts and created its recent POs
# (checked 2026-10-26). Change it here if counts should be recorded under someone else.
COUNT_EMPLOYEE_ID = "1"

# Prefix for every Windows Credential Manager entry this project creates.
KEYRING_PREFIX = "suits4men-lightspeed"

# Refresh the access token when it has less than this many seconds left.
REFRESH_MARGIN_SECONDS = 120

# How long one process waits for another to finish refreshing tokens.
TOKEN_LOCK_TIMEOUT_SECONDS = 60

HTTP_TIMEOUT_SECONDS = 30
USER_AGENT = "suits4men-lightspeed-mcp/0.1"
