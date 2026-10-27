"""Access-token handling, including the refresh-token rotation rules.

How Lightspeed's tokens behave (developers.lightspeedhq.com/retail, Refresh Token Grant):

  * An access token lasts about an hour (the token response says exactly how
    long in `expires_in`; the docs show both 3600 and 1800, so we never assume).
  * Every refresh returns a NEW access token AND a NEW refresh token.
  * The OLD refresh token is revoked the first time the NEW access token is used.
  * A refresh token that goes unused for 30 days expires.

So a single mistake can brick the connection: if two processes refresh at the
same time, or if we use a new access token before the new refresh token is
safely saved, the only refresh token we still have may be a revoked one.

The rules this module follows:

  1. Only one refresh at a time, across every process on this computer
     (Claude Desktop can run several tool calls at once, and bootstrap.py may
     run while the server is up). A file lock in the project's state/ folder
     enforces this; a threading lock covers threads inside one process.
  2. After taking the lock, re-read the saved tokens. If another process
     refreshed while we waited, use its token instead of refreshing again.
  3. Save the new pair to Credential Manager and read it back BEFORE returning
     the new access token to anyone. If saving fails, the new access token is
     never used, so the old refresh token is not revoked.
  4. Refresh only when the token is expired or close to it, or after a 401.
  5. If Lightspeed says invalid_grant, mark the saved tokens as dead and tell
     the user to re-run bootstrap.py. Never retry that in a loop.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
from filelock import FileLock, Timeout

from .config import REFRESH_MARGIN_SECONDS, TOKEN_LOCK_PATH, TOKEN_LOCK_TIMEOUT_SECONDS, TOKEN_URL
from .errors import NotBootstrappedError, ReauthorizationRequired, TokenRefreshError
from .secret_store import SecretStore, Tokens

log = logging.getLogger(__name__)

# Used when a token response has no expires_in. The shorter of the two
# documented values, so we refresh too early rather than too late.
DEFAULT_EXPIRES_IN = 1800


def token_lock(lock_path: Path = TOKEN_LOCK_PATH, timeout: float = TOKEN_LOCK_TIMEOUT_SECONDS) -> FileLock:
    """The cross-process lock that guards every write of the saved tokens."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(lock_path), timeout=timeout)


def tokens_from_response(body: dict, issued_at: float, previous_refresh_token: str | None = None) -> Tokens:
    """Build a Tokens record from a successful token-endpoint response."""
    expires_in = int(body.get("expires_in") or DEFAULT_EXPIRES_IN)
    refresh_token = body.get("refresh_token")
    if not refresh_token:
        # Lightspeed documents rotation, so this should not happen. If it does,
        # the old refresh token is presumably still the valid one.
        if not previous_refresh_token:
            raise TokenRefreshError("Lightspeed's token response did not include a refresh token.")
        log.warning("Token response had no refresh_token; keeping the previous one.")
        refresh_token = previous_refresh_token
    return Tokens(
        access_token=body["access_token"],
        refresh_token=refresh_token,
        expires_at=issued_at + expires_in,
    )


class TokenManager:
    """Hands out a valid access token, refreshing it safely when needed.

    The saved tokens are re-read from Credential Manager on every call and
    never cached in memory, because another process may have rotated them.
    """

    def __init__(
        self,
        store: SecretStore,
        http: httpx.Client,
        lock_path: Path = TOKEN_LOCK_PATH,
        now=time.time,
        margin_seconds: float = REFRESH_MARGIN_SECONDS,
        lock_timeout: float = TOKEN_LOCK_TIMEOUT_SECONDS,
    ):
        self.store = store
        self.http = http  # used only for POSTs to the token endpoint
        self.lock_path = lock_path
        self.now = now
        self.margin_seconds = margin_seconds
        self.lock_timeout = lock_timeout
        self._thread_lock = threading.Lock()

    def access_token(self) -> str:
        """Return an access token that is good for at least `margin_seconds`."""
        tokens = self._load()
        if not self._near_expiry(tokens):
            return tokens.access_token
        return self._refresh(stale_access_token=tokens.access_token)

    def refresh_after_401(self, rejected_access_token: str) -> str:
        """Lightspeed rejected `rejected_access_token`; get a new one (once)."""
        return self._refresh(stale_access_token=rejected_access_token)

    # ---- internals ---------------------------------------------------------

    def _load(self) -> Tokens:
        tokens = self.store.load_tokens()
        if tokens is None:
            raise NotBootstrappedError()
        if tokens.reauth_required:
            raise ReauthorizationRequired("Lightspeed rejected the saved refresh token earlier (invalid_grant).")
        return tokens

    def _near_expiry(self, tokens: Tokens) -> bool:
        return tokens.expires_at - self.margin_seconds <= self.now()

    def _refresh(self, stale_access_token: str) -> str:
        try:
            with self._thread_lock, token_lock(self.lock_path, self.lock_timeout):
                # Rule 2: someone else may have refreshed while we waited for the lock.
                tokens = self._load()
                if tokens.access_token != stale_access_token and not self._near_expiry(tokens):
                    log.info("Another caller already refreshed the access token; using theirs.")
                    return tokens.access_token
                return self._refresh_locked(tokens)
        except Timeout:
            raise TokenRefreshError(
                f"Timed out after {self.lock_timeout:.0f}s waiting for another process to finish refreshing "
                "the Lightspeed token. Try again; if it keeps happening, restart Claude Desktop."
            ) from None

    def _refresh_locked(self, tokens: Tokens) -> str:
        """Call the token endpoint and save the result. Caller holds the lock."""
        app = self.store.load_app()
        if app is None:
            raise NotBootstrappedError("The saved client ID and secret are missing.")

        issued_at = self.now()
        try:
            response = self.http.post(
                TOKEN_URL,
                data={
                    "client_id": app.client_id,
                    "client_secret": app.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": tokens.refresh_token,
                },
            )
        except httpx.HTTPError as e:
            # Nothing changed on Lightspeed's side that we know of; safe to retry later.
            raise TokenRefreshError(
                f"Could not reach Lightspeed to refresh the access token ({type(e).__name__}). Try again."
            ) from None

        body = _json_or_empty(response)
        if response.status_code == 200 and body.get("access_token"):
            new_tokens = tokens_from_response(body, issued_at, previous_refresh_token=tokens.refresh_token)
            self._save_and_confirm(new_tokens)
            log.info("Refreshed the Lightspeed access token; it expires in %ss.", body.get("expires_in"))
            return new_tokens.access_token

        error = body.get("error", "")
        if error == "invalid_grant":
            # Rule 5: mark the tokens dead so later calls fail fast without
            # hammering the token endpoint. We hold the lock and just re-read
            # the record, so this cannot overwrite tokens from a newer bootstrap.
            self.store.save_tokens(replace(tokens, reauth_required=True))
            raise ReauthorizationRequired("Lightspeed rejected the saved refresh token (invalid_grant).")
        if error == "invalid_client":
            raise ReauthorizationRequired(
                "Lightspeed rejected the saved client ID or secret (invalid_client). "
                "If the secret was changed in the Lightspeed developer portal, enter the new one."
            )
        detail = " ".join(part for part in (error, body.get("error_description") or body.get("message")) if part)
        raise TokenRefreshError(f"Lightspeed refused the token refresh (HTTP {response.status_code}: {detail or 'no detail'}).")

    def _save_and_confirm(self, new_tokens: Tokens) -> None:
        """Rule 3: the new pair must be safely stored before the access token is used."""
        try:
            self.store.save_tokens(new_tokens)
            saved = self.store.load_tokens()
        except Exception as e:
            raise TokenRefreshError(
                "Lightspeed issued new tokens but they could not be saved to Windows Credential Manager "
                f"({e}). The new access token was not used, so the saved refresh token should still work. "
                "Try again."
            ) from None
        if saved is None or saved.refresh_token != new_tokens.refresh_token or saved.access_token != new_tokens.access_token:
            raise TokenRefreshError(
                "New tokens did not read back correctly from Windows Credential Manager. "
                "The new access token was not used. Try again."
            )


def _json_or_empty(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
