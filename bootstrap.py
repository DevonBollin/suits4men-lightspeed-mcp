"""Authorize this computer to read the Suits 4 Men Lightspeed account.

Run it once to set up, and again whenever you need to re-authorize (for
example after revoking access in Lightspeed, or if the server says the refresh
token was rejected). From the project folder, in PowerShell or Command Prompt:

    .venv\\Scripts\\python.exe bootstrap.py            set up or re-authorize
    .venv\\Scripts\\python.exe bootstrap.py --forget   delete the saved secrets from this computer

What it does:
  1. Asks for the client ID and secret (typing is hidden). Press Enter to reuse saved ones.
  2. Prints the Lightspeed authorize URL and opens it in your browser.
  3. You approve. The browser then fails to load https://localhost/callback
     (nothing listens there; that is expected). Copy the whole address from
     the address bar and paste it here. The code inside it is valid for only
     60 seconds, so paste it right away.
  4. Exchanges the code for tokens and saves them to Windows Credential Manager
     immediately, before using them.
  5. Looks up the account ID and shop name and prints those. Secrets are never printed.
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
import time
import webbrowser
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from lightspeed_mcp.client import as_list, make_api_http, make_http
from lightspeed_mcp.config import API_ROOT, AUTHORIZE_URL, SCOPES, TOKEN_URL
from lightspeed_mcp.errors import LightspeedError
from lightspeed_mcp.secret_store import AppConfig, SecretStore
from lightspeed_mcp.tokens import token_lock, tokens_from_response


class BootstrapError(Exception):
    pass


def build_authorize_url(client_id: str, state: str) -> str:
    """Matches the documented example: scopes joined by a literal '+', colons left as-is."""
    scope = "+".join(quote(s, safe=":") for s in SCOPES)
    return f"{AUTHORIZE_URL}?response_type=code&client_id={quote(client_id)}&scope={scope}&state={quote(state)}"


def extract_code(pasted: str, expected_state: str) -> str:
    """Accept the full redirect URL (preferred, lets us check `state`) or a bare code."""
    pasted = pasted.strip().strip('"').strip("'")
    if not pasted:
        raise BootstrapError("Nothing was pasted.")

    if "://" in pasted or pasted.startswith("localhost") or "?" in pasted:
        query = parse_qs(urlsplit(pasted if "://" in pasted else "https://" + pasted).query)
        if "error" in query:
            detail = query.get("error_description", query["error"])[0]
            raise BootstrapError(f"Lightspeed did not authorize: {detail}")
        if "code" not in query:
            raise BootstrapError("That URL has no 'code=' in it. Copy the whole address bar after approving.")
        returned_state = query.get("state", [None])[0]
        if returned_state is not None and returned_state != expected_state:
            raise BootstrapError(
                "The 'state' in that URL does not match this run. It is probably from an older attempt. "
                "Start bootstrap again and use the new link."
            )
        if returned_state is None:
            print("  (No 'state' in the URL, so it could not be checked.)")
        return query["code"][0]

    if " " in pasted:
        raise BootstrapError("That does not look like a code or a URL.")
    print("  (Bare code pasted, so the 'state' check was skipped.)")
    return pasted


def exchange_code(http: httpx.Client, client_id: str, client_secret: str, code: str):
    issued_at = time.time()
    response = http.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
        },
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not body.get("access_token"):
        error = body.get("error") or f"HTTP {response.status_code}"
        detail = body.get("error_description") or body.get("message")
        message = f"Token exchange failed: {error}" + (f" ({detail})" if detail else "") + "."
        if error == "invalid_client":
            message += " Check the client ID and secret."
        elif error == "invalid_grant":
            message += " The code may have expired (60 seconds) or already been used. Run bootstrap again."
        raise BootstrapError(message)
    return tokens_from_response(body, issued_at), body


def fetch_account(access_token: str) -> tuple[str, str]:
    """GET /API/V3/Account.json -> (accountID, name). Asks if more than one account comes back."""
    with make_api_http() as api:
        response = api.get(f"{API_ROOT}Account.json", headers={"Authorization": f"Bearer {access_token}"})
    if response.status_code != 200:
        raise BootstrapError(f"Account lookup failed: HTTP {response.status_code}.")
    accounts = as_list(response.json().get("Account"))
    if not accounts:
        raise BootstrapError("Lightspeed returned no account for this authorization.")
    if len(accounts) == 1:
        chosen = accounts[0]
    else:
        print("\nThis login can see more than one account:")
        for i, acct in enumerate(accounts, 1):
            print(f"  {i}. {acct.get('name')} (account {acct.get('accountID')})")
        try:
            chosen = accounts[int(input("Which one is Suits 4 Men? Enter the number: ").strip()) - 1]
        except (ValueError, IndexError):
            raise BootstrapError("Not one of the listed numbers.") from None
    return str(chosen["accountID"]), str(chosen.get("name") or "")


def fetch_shop_names(access_token: str, account_id: str) -> list[str]:
    """Best effort: the shop endpoint may need a scope we did not request."""
    try:
        with make_api_http() as api:
            response = api.get(
                f"{API_ROOT}Account/{account_id}/Shop.json",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code != 200:
            return [f"(not readable with the current scopes: HTTP {response.status_code})"]
        return [f"{s.get('name')} (shopID {s.get('shopID')})" for s in as_list(response.json().get("Shop"))]
    except httpx.HTTPError as e:
        return [f"(lookup failed: {type(e).__name__})"]


def ask_for_client_credentials(saved: AppConfig | None) -> tuple[str, str]:
    if saved:
        print("A saved client ID and secret were found. Press Enter at each prompt to keep them.")
    client_id = getpass.getpass("Client ID (typing is hidden): ").strip()
    client_secret = getpass.getpass("Client secret (typing is hidden): ").strip()
    if saved:
        client_id = client_id or saved.client_id
        client_secret = client_secret or saved.client_secret
    if not client_id or not client_secret:
        raise BootstrapError("Both a client ID and a client secret are needed.")
    return client_id, client_secret


def forget(store: SecretStore) -> None:
    with token_lock():
        store.delete("tokens")
        store.delete("app")
    print("Deleted the saved Lightspeed secrets from Windows Credential Manager.")
    print("To also cut off access on Lightspeed's side, see 'Revoke access' in README.md.")


def run(store: SecretStore) -> None:
    saved = store.load_app()
    client_id, client_secret = ask_for_client_credentials(saved)

    state = secrets.token_urlsafe(24)
    url = build_authorize_url(client_id, state)
    print("\nStep 1. Open this link and approve access. Sign in as the store owner or an admin")
    print("        (the token can only do what that employee is allowed to do):\n")
    print(f"  {url}\n")
    print("Step 2. The browser will then show a connection error for https://localhost/callback.")
    print("        That is expected. Copy the WHOLE address from the address bar and paste it")
    print("        below right away (the code expires after 60 seconds).\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass  # the printed link is enough

    pasted = input("Paste the address (or just the code) and press Enter: ")
    code = extract_code(pasted, state)

    with make_http() as auth_http:
        tokens, body = exchange_code(auth_http, client_id, client_secret, code)

    # Save first, use second. Hold the token lock so a running server cannot
    # refresh (and overwrite) at the same moment.
    with token_lock():
        store.save_app(AppConfig(client_id, client_secret,
                                 saved.account_id if saved else None,
                                 saved.account_name if saved else None))
        store.save_tokens(tokens)

    account_id, account_name = fetch_account(tokens.access_token)
    store.save_app(AppConfig(client_id, client_secret, account_id, account_name))
    shops = fetch_shop_names(tokens.access_token, account_id)

    if saved and saved.account_id and saved.account_id != account_id:
        print(f"\nNote: the account changed from {saved.account_id} to {account_id}. Restart Claude Desktop.")

    print("\nConnected.")
    print(f"  Account: {account_name} (account ID {account_id})")
    for shop in shops:
        print(f"  Shop:    {shop}")
    print(f"  Scopes requested: {' '.join(SCOPES)}")
    print(f"  Access token renews automatically (this one lasts {int(body.get('expires_in', 0)) // 60} min).")
    print("  Secrets are saved in Windows Credential Manager under 'suits4men-lightspeed/...'.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Authorize this computer for the Suits 4 Men Lightspeed account.")
    parser.add_argument("--forget", action="store_true", help="delete the saved secrets from this computer")
    args = parser.parse_args()

    store = SecretStore()
    try:
        if args.forget:
            forget(store)
        else:
            run(store)
    except (BootstrapError, LightspeedError) as e:
        print(f"\nStopped: {e}", file=sys.stderr)
        return 1
    except httpx.HTTPError as e:
        print(f"\nStopped: network error talking to Lightspeed ({type(e).__name__}). Try again.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
