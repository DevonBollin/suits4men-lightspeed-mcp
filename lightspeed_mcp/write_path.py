"""The write path: the only code in this project that can change Lightspeed.

The read client (client.py) only sends GET, and every read tool and api_get
use it. Writes go through WriteClient, defined here, and only the write tools
in writer.py hold one. Before anything leaves the computer, these checks must pass:

  1. The master write switch is ON. The switch is a file, state/WRITES_ENABLED,
     that only the store owner creates, by hand. While it is missing (as
     shipped), every write is refused. It is checked on every request, so
     deleting the file turns writes off at once, without a restart.
  2. The request is one that the calling write tool is allowed to make
     (WRITE_ROUTES). For example update_item_fields can only PUT
     Item/<id>.json. The only DELETE is archive_item's, on one Item/<id>.json
     (Lightspeed archives an item on DELETE). No route can update or archive a
     matrix: archiving a matrix archives every item in it, so that stays manual.
  3. The JSON body holds only the fields that route allows (a DELETE has no
     body). Prices can only be set when creating a matrix or a new item; stock
     levels (ItemShops) never.
  4. An httpx request hook repeats checks 1 and 2 as the last line of defence,
     so code that reaches past WriteClient still cannot send anything else.

The write tools add two more checks on top (writer.py): each write must first
be shown as a dry run, and the real call must carry that dry run's confirm code.

Every write that is sent is recorded in state/write_log.jsonl: one JSON object
per line, only ever appended. A "sending" line is written just before the
request goes out and a "done" or "failed" line when the answer comes back, so a
crash in between still leaves a record. Tokens are never logged.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from filelock import FileLock

from .client import BucketLimiter, _error_message
from .config import API_ROOT, HTTP_TIMEOUT_SECONDS, STATE_DIR, USER_AGENT
from .errors import (ApiError, AuthError, LightspeedError, RateLimitedError, WriteBlockedError,
                     WriteOutcomeUnknown, WritesDisabledError)
from .tokens import TokenManager

log = logging.getLogger(__name__)

# The master write switch. Writes are possible only while this file exists.
WRITE_SWITCH_PATH = STATE_DIR / "WRITES_ENABLED"
WRITE_LOG_PATH = STATE_DIR / "write_log.jsonl"

WRITES_OFF_MESSAGE = (
    "Writes to Lightspeed are turned OFF on this computer, so nothing was sent and nothing changed. "
    "Only the store owner can turn writes on, by hand (README.md, 'Turning writes on and off'). "
    "Dry runs (dry_run=true) still work."
)

# Lightspeed charges 1 drip per request plus 0.5 per nested record in the body
# (developers.lightspeedhq.com/retail/introduction/ratelimits). 2 is a safe
# estimate; the real level comes back in every response's headers.
WRITE_COST = 2.0


@dataclass(frozen=True)
class Route:
    """One kind of request a write tool may send."""

    method: str
    path: re.Pattern  # relative to the account, e.g. Item/123.json
    fields: frozenset[str]  # top-level JSON fields the body may contain


ITEM_FIELDS = {"description", "itemType", "tax", "taxClassID", "categoryID", "manufacturerID", "defaultVendorID",
               "defaultCost", "Prices"}

# Tool name -> the one kind of request it may send. Nothing else can be written.
WRITE_ROUTES: dict[str, Route] = {
    "create_matrix": Route("POST", re.compile(r"ItemMatrix\.json"),
                           frozenset(ITEM_FIELDS | {"itemAttributeSetID"})),
    "add_matrix_variants": Route("POST", re.compile(r"Item\.json"),
                                 frozenset(ITEM_FIELDS | {"itemMatrixID", "customSku", "ItemAttributes"})),
    "update_item_fields": Route("PUT", re.compile(r"Item/\d+\.json"),
                                frozenset({"customSku", "defaultCost", "description"})),  # never prices or stock
    "add_po_lines": Route("POST", re.compile(r"OrderLine\.json"),
                          frozenset({"orderID", "itemID", "quantity", "price", "originalPrice", "numReceived"})),
    "receive_po_lines": Route("PUT", re.compile(r"OrderLine/\d+\.json"), frozenset({"numReceived"})),
    # Put an existing item into a matrix: its matrix, Color/Size/Fit, and (optionally) the
    # matrix's description pattern, category, vendor and manufacturer. Never prices, cost or stock.
    "move_items_into_matrix": Route("PUT", re.compile(r"Item/\d+\.json"),
                                    frozenset({"itemMatrixID", "ItemAttributes", "description", "categoryID",
                                               "manufacturerID", "defaultVendorID"})),
    "assign_custom_skus": Route("PUT", re.compile(r"Item/\d+\.json"), frozenset({"customSku"})),
    # Lightspeed archives an item on DELETE (items are never really deleted). Items only, never a matrix.
    "archive_item": Route("DELETE", re.compile(r"Item/\d+\.json"), frozenset()),
}


class WriteSwitch:
    """The master switch: ON only while the switch file exists. Nothing in this project creates it."""

    def __init__(self, path: Path = WRITE_SWITCH_PATH):
        self.path = Path(path)

    def is_on(self) -> bool:
        return self.path.is_file()

    def require_on(self) -> None:
        if not self.is_on():
            raise WritesDisabledError(WRITES_OFF_MESSAGE)


class WriteLog:
    """Append-only record of every write sent (state/write_log.jsonl)."""

    def __init__(self, path: Path = WRITE_LOG_PATH):
        self.path = Path(path)

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"time": datetime.now().astimezone().isoformat(timespec="seconds"), **entry},
                          ensure_ascii=False)
        # The lock keeps lines from two processes (e.g. two Claude windows) from interleaving.
        with FileLock(str(self.path) + ".lock", timeout=30):
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def entries(self) -> list[dict]:
        """Every line, oldest first. A line that isn't valid JSON (e.g. cut short by a crash) is skipped."""
        if not self.path.exists():
            return []
        found = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                found.append(json.loads(line))
            except ValueError:
                if line.strip():
                    log.warning("Skipped an unreadable line in %s.", self.path)
        return found


def write_log_report(switch: WriteSwitch, write_log: WriteLog, limit: int = 20) -> dict:
    """The switch state and the logged writes, one row per write, newest first."""
    writes: dict[str, dict] = {}
    for e in write_log.entries():
        w = writes.setdefault(e.get("write_id", "?"), {
            "write_id": e.get("write_id"), "tool": e.get("tool"), "method": e.get("method"), "path": e.get("path"),
            "confirm_code": e.get("confirm_code")})
        if e.get("event") == "sending":
            w["sent"] = e.get("time")
            w["payload"] = e.get("payload")
            if e.get("before"):
                w["before"] = e["before"]
        elif e.get("event") == "done":
            w.update(outcome="done", status=e.get("status"), affected_ids=e.get("affected_ids"))
        elif e.get("event") == "failed":
            w.update(outcome="failed", status=e.get("status"), error=e.get("error"))
    for w in writes.values():
        # A "sending" line with no answer: the process stopped while the request was out.
        w.setdefault("outcome", "unknown: no answer was recorded; check the record in Lightspeed")
    rows = list(reversed(list(writes.values())))
    limit = max(1, min(int(limit), 500))
    result = {"writes_enabled": switch.is_on(), "log_file": str(write_log.path), "writes_logged": len(rows),
              "writes": rows[:limit]}
    if not rows:
        result["note"] = "No write has ever been sent from this computer."
    return result


def route_allows(route: Route, method: str, relative_path: str) -> bool:
    return route.method == method.upper() and route.path.fullmatch(relative_path) is not None


def make_write_http(base_url: str, switch: WriteSwitch, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """An httpx client that can only send the write tools' own requests, and only while the switch is on."""

    def check_request(request: httpx.Request) -> None:
        # Last line of defence: runs on every request, just before it is sent.
        if not switch.is_on():
            raise WritesDisabledError(WRITES_OFF_MESSAGE)
        url = str(request.url)
        relative = url[len(base_url):] if url.startswith(base_url) else None
        if relative is None or request.url.query or not any(
                route_allows(r, request.method, relative) for r in WRITE_ROUTES.values()):
            raise WriteBlockedError(f"Blocked {request.method} {request.url.path}: not one of the write tools' requests.")

    return httpx.Client(
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        event_hooks={"request": [check_request]},
        follow_redirects=False,
        transport=transport,
    )


# The ID fields worth recording from a response record.
ID_FIELDS = ("itemMatrixID", "itemID", "orderID", "orderLineID")


def affected_ids(body: dict) -> dict:
    """{"Item": {"itemID": "2001", "itemMatrixID": "8"}} from a response like {"Item": {...}}."""
    found = {}
    for key, record in (body or {}).items():
        if key != "@attributes" and isinstance(record, dict):
            found[key] = {f: record[f] for f in ID_FIELDS if record.get(f) not in (None, "", "0")}
    return found


class WriteClient:
    """Sends the write tools' requests. Held only by InventoryWriter (writer.py)."""

    def __init__(
        self,
        account_id: str,
        tokens: TokenManager,
        limiter: BucketLimiter,
        switch: WriteSwitch | None = None,
        write_log: WriteLog | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
        max_rate_limit_retries: int = 5,
    ):
        self.account_id = str(account_id)
        self.base_url = f"{API_ROOT}Account/{self.account_id}/"
        self.tokens = tokens
        self.limiter = limiter  # shared with the read client: one bucket per account
        self.switch = switch or WriteSwitch()
        self.write_log = write_log or WriteLog()
        self.http = make_write_http(self.base_url, self.switch, transport)
        self.sleep = sleep
        self.max_rate_limit_retries = max_rate_limit_retries

    def full_path(self, path: str) -> str:
        """What a dry run shows: the path exactly as it would be requested."""
        return f"/API/V3/Account/{self.account_id}/{path}"

    def check(self, tool: str, method: str, path: str, body: dict | None) -> None:
        """Refuse anything but the tool's own kind of request with allowed fields. Sends nothing.

        Dry runs call this too, so every payload a dry run shows is one the write path would accept.
        """
        route = WRITE_ROUTES.get(tool)
        if route is None:
            raise WriteBlockedError(f"'{tool}' is not a write tool; only {', '.join(WRITE_ROUTES)} can write.")
        if not route_allows(route, method, path):
            raise WriteBlockedError(f"{tool} may only send {route.method} {route.path.pattern}, not {method} {path}.")
        if route.method == "DELETE":
            if body:
                raise WriteBlockedError(f"{tool}: a DELETE has no body.")
            return
        if not isinstance(body, dict) or not body:
            raise WriteBlockedError(f"{tool}: the request body must be a non-empty JSON object.")
        extra = sorted(set(body) - route.fields)
        if extra:
            raise WriteBlockedError(f"{tool} may not send the field(s) {', '.join(extra)}.")

    def send(self, tool: str, method: str, path: str, body: dict | None, confirm_code: str,
             before: dict | None = None) -> dict:
        """Send one write. Returns {"status", "affected_ids", "record"}; raises on any refusal or failure.

        `before` is the record as it was, logged with the request so a change
        (e.g. archiving) can be put back by hand later.
        """
        self.switch.require_on()
        self.check(tool, method, path, body)
        write_id = uuid.uuid4().hex[:12]
        entry = {"write_id": write_id, "tool": tool, "confirm_code": confirm_code, "method": method,
                 "path": self.full_path(path)}
        sending = {**entry, "event": "sending", "payload": body}
        if before:
            sending["before"] = before
        self.write_log.append(sending)
        try:
            status, response_body = self._send(method, path, body)
        except LightspeedError as e:
            self.write_log.append({**entry, "event": "failed", "status": getattr(e, "status_code", None),
                                   "error": str(e)})
            raise
        ids = affected_ids(response_body)
        self.write_log.append({**entry, "event": "done", "status": status, "affected_ids": ids})
        record = next((v for k, v in response_body.items() if k != "@attributes" and isinstance(v, dict)), {})
        return {"status": status, "affected_ids": ids, "record": record}

    def _send(self, method: str, path: str, body: dict | None) -> tuple[int, dict]:
        """Auth, rate limit and error handling for one write.

        Retried: 401 (once, with a fresh token) and 429, because Lightspeed did not
        act on those. Never retried: network errors and 5xx, because the write
        may have gone through; those raise WriteOutcomeUnknown.
        """
        url = self.base_url + path
        retried_auth = False
        rate_limit_retries = 0
        while True:
            self.limiter.wait_for_room(WRITE_COST)
            token = self.tokens.access_token()
            try:
                response = self.http.request(method, url, json=body or None,
                                             headers={"Authorization": f"Bearer {token}"})
            except httpx.TransportError as e:
                raise WriteOutcomeUnknown(
                    f"Network error during {method} {path} ({type(e).__name__}). Lightspeed may or may not have made "
                    "this change: check the record in Lightspeed before trying again."
                ) from None

            self.limiter.update(response.headers)
            status = response.status_code
            if status == 401 and not retried_auth:
                retried_auth = True
                self.tokens.refresh_after_401(token)
                continue
            if status == 401:
                raise AuthError("Lightspeed rejected a freshly refreshed access token (401). Re-run bootstrap.py.")
            if status == 429:
                rate_limit_retries += 1
                if rate_limit_retries > self.max_rate_limit_retries:
                    raise RateLimitedError("Lightspeed is still rate limiting this write. Nothing was changed; try again later.")
                self.sleep(self.limiter.retry_delay(response.headers))
                continue
            if status >= 500:
                raise WriteOutcomeUnknown(
                    f"Lightspeed answered HTTP {status} to {method} {path}. It may or may not have made this change: "
                    "check the record in Lightspeed before trying again."
                )
            if status >= 400:
                raise ApiError(status, _error_message(response))
            try:
                return status, response.json()
            except ValueError:
                raise WriteOutcomeUnknown(
                    f"Lightspeed answered HTTP {status} to {method} {path} without JSON. Check the record in Lightspeed."
                ) from None
