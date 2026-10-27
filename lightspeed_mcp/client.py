"""HTTP client for the Lightspeed R-Series V3 API. Phase 1: GET requests only.

Three things happen here:

  * GET-only guard. LightspeedClient has no method that can send anything but
    GET, and the underlying httpx client has a request hook that refuses any
    other method before it leaves the computer. Both have to be removed on
    purpose before a write is possible.
  * Rate limiting. Lightspeed uses a "leaky bucket": each request adds drips
    (1 for a simple GET, more with load_relations), the bucket drains at a
    steady rate (1 drip/second for a one-register store), and when it is full
    you get 429. Every response reports the level in X-LS-API-Bucket-Level as
    "used/size" (e.g. "12.5/90"). We track that and pause before overrunning,
    and on a 429 wait for Retry-After and try again.
  * Pagination. V3 pages are cursor based: each response has
    @attributes.next, a full URL for the next page (empty string on the last
    page). Pages hold at most 100 records. There is no `offset` in V3.
"""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlsplit

import httpx

from .config import API_ROOT, HTTP_TIMEOUT_SECONDS, USER_AGENT
from .errors import ApiError, AuthError, RateLimitedError, WriteBlockedError
from .tokens import TokenManager

log = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100


def _refuse_non_get(request: httpx.Request) -> None:
    """httpx request hook: the last line of defence for read-only Phase 1."""
    if request.method != "GET":
        raise WriteBlockedError(f"Blocked a {request.method} request: this server is read-only (Phase 1).")


def make_api_http(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """An httpx client for api.lightspeedapp.com that can only send GET requests.

    Redirects are not followed, so the Authorization header can never be sent
    to some other host.
    """
    return httpx.Client(
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        event_hooks={"request": [_refuse_non_get]},
        follow_redirects=False,
        transport=transport,
    )


def as_list(value) -> list:
    """Lightspeed returns a single record as an object and several as a list. Always give a list."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


class BucketLimiter:
    """Our running estimate of Lightspeed's leaky bucket for this account.

    Shared by all threads in one process. Several processes (e.g. Claude
    Desktop and a test script) each keep their own estimate; the 429 handling
    in LightspeedClient covers the gap.
    """

    def __init__(self, sleep=time.sleep, now=time.monotonic, headroom: float = 10.0, max_wait: float = 60.0):
        self.sleep = sleep
        self.now = now
        self.headroom = headroom  # keep this many drips free for other callers
        self.max_wait = max_wait  # never pause longer than this in one go
        self.level = 0.0
        self.size = 90.0  # documented base size; corrected by the first response
        self.drip_rate = 1.0  # drips leaked per second; corrected by headers
        self.seen_at: float | None = None
        self._lock = threading.Lock()

    def _estimate(self) -> float:
        if self.seen_at is None:
            return 0.0
        return max(0.0, self.level - (self.now() - self.seen_at) * self.drip_rate)

    def wait_for_room(self, cost: float = 1.0) -> float:
        """Pause if this request would push the bucket past (size - headroom). Returns seconds slept."""
        with self._lock:
            level = self._estimate()
            over = level + cost - (self.size - self.headroom)
            delay = min(max(0.0, over / self.drip_rate), self.max_wait)
            # Reserve our drips now, so a concurrent caller queues up behind us.
            self.level = level + cost
            self.seen_at = self.now()
        if delay > 0:
            log.info("Rate limit: pausing %.1fs (bucket about %.1f/%.0f).", delay, level, self.size)
            self.sleep(delay)
        return delay

    def update(self, headers: httpx.Headers) -> None:
        """Take the real bucket level from a response's headers."""
        level_header = headers.get("X-LS-API-Bucket-Level")  # e.g. "12.5/90"
        drip_header = headers.get("X-LS-API-Drip-Rate")  # e.g. "1"
        with self._lock:
            if level_header and "/" in level_header:
                used, size = level_header.split("/", 1)
                try:
                    self.level, self.size = float(used), float(size)
                    self.seen_at = self.now()
                except ValueError:
                    pass
            if drip_header:
                try:
                    self.drip_rate = max(float(drip_header), 0.1)
                except ValueError:
                    pass

    def retry_delay(self, headers: httpx.Headers) -> float:
        """How long to wait after a 429."""
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                return min(max(float(retry_after), 1.0), self.max_wait)
            except ValueError:
                pass
        if headers.get("X-LS-API-RateLimit-Type") == "api-burst-limiter":
            return 1.0  # docs: back off at least 1 second after a burst-limit 429
        # No Retry-After: wait for the bucket to drain below the headroom line.
        with self._lock:
            over = self._estimate() - (self.size - self.headroom)
        return min(max(over / self.drip_rate, 2.0), self.max_wait)


class LightspeedClient:
    """Read-only access to one Lightspeed account."""

    def __init__(
        self,
        account_id: str,
        tokens: TokenManager,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
        now=time.monotonic,
        max_rate_limit_retries: int = 5,
        max_server_error_retries: int = 2,
    ):
        self.account_id = str(account_id)
        self.base_url = f"{API_ROOT}Account/{self.account_id}/"
        self.tokens = tokens
        self.http = make_api_http(transport)
        self.sleep = sleep
        self.limiter = BucketLimiter(sleep=sleep, now=now)
        self.max_rate_limit_retries = max_rate_limit_retries
        self.max_server_error_retries = max_server_error_retries

    # ---- public API: GET only ---------------------------------------------

    def request(self, method: str, path: str, params: dict | None = None) -> dict:
        """The single entry point. Refuses every method except GET."""
        if method.upper() != "GET":
            raise WriteBlockedError(f"Refused {method.upper()} {path}: this server is read-only (Phase 1).")
        return self._get_url(self.url_for(path), params)

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET one page (or one record). `path` is relative to the account, e.g. "Item/123.json"."""
        return self.request("GET", path, params)

    def get_all(self, path: str, params: dict | None = None, max_records: int | None = None) -> tuple[list[dict], bool]:
        """GET every page of a list endpoint.

        Returns (records, truncated). `truncated` is True when more records
        exist beyond `max_records`.
        """
        record_key = self.record_key(path)
        page_params = dict(params or {})
        page_size = MAX_PAGE_SIZE if max_records is None else max(1, min(MAX_PAGE_SIZE, max_records))
        page_params.setdefault("limit", str(page_size))

        records: list[dict] = []
        url = self.url_for(path)
        while True:
            body = self._get_url(url, page_params)
            records.extend(as_list(body.get(record_key)))
            next_url = (body.get("@attributes") or {}).get("next") or ""
            if max_records is not None and len(records) >= max_records:
                return records[:max_records], bool(next_url) or len(records) > max_records
            if not next_url:
                return records, False
            # The next URL already carries every query parameter plus the cursor.
            url, page_params = self.check_next_url(next_url), None

    # ---- path and URL handling ---------------------------------------------

    def url_for(self, path: str) -> str:
        """Turn "Item", "Item.json" or "Item/123.json" into a full URL for this account."""
        path = (path or "").strip().replace("\\", "/").lstrip("/")
        # Accept paths copied from docs or from a Lightspeed URL.
        for prefix in ("API/V3/", f"Account/{self.account_id}/"):
            if path.startswith(prefix):
                path = path[len(prefix) :]
        if not path or "://" in path or ".." in path or "?" in path or "#" in path:
            raise ValueError(
                f"Bad API path {path!r}: give a path relative to the account such as 'Item.json' "
                "or 'Item/123.json', and put query parameters in params."
            )
        if path.startswith("Account"):
            raise ValueError("Only this store's account is reachable; give a path like 'Item.json'.")
        if not path.endswith(".json"):
            path += ".json"
        return self.base_url + path

    @staticmethod
    def record_key(path: str) -> str:
        """The JSON key that holds the records: "Item.json" -> "Item", "Item/123.json" -> "Item"."""
        name = path.strip().replace("\\", "/").lstrip("/").split("?")[0]
        name = name.removesuffix(".json")
        parts = [p for p in name.split("/") if p and not p.isdigit()]
        return parts[-1] if parts else name

    def check_next_url(self, url: str) -> str:
        """Only follow pagination links back into this account's API (never leak the token elsewhere)."""
        parts = urlsplit(url)
        if parts.scheme != "https" or not url.startswith(self.base_url):
            raise ApiError(0, f"Refusing to follow a pagination link outside this account's API: {parts.netloc}{parts.path}")
        return url

    # ---- the request loop ------------------------------------------------------

    def _get_url(self, url: str, params: dict | None) -> dict:
        """GET a full URL that has already been validated, with auth, rate-limit and retry handling."""
        retried_auth = False
        rate_limit_retries = 0
        server_error_retries = 0

        while True:
            self.limiter.wait_for_room()
            token = self.tokens.access_token()
            try:
                response = self.http.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            except httpx.TransportError as e:
                if server_error_retries < self.max_server_error_retries:
                    server_error_retries += 1
                    self.sleep(2 * server_error_retries)
                    continue
                raise ApiError(0, f"Network error talking to Lightspeed: {type(e).__name__}") from None

            self.limiter.update(response.headers)
            status = response.status_code

            if status == 401:
                if retried_auth:
                    raise AuthError(
                        "Lightspeed rejected a freshly refreshed access token (401). Access may have been "
                        "revoked in Lightspeed, or the authorizing employee was locked. Re-run bootstrap.py."
                    )
                # Retry exactly once with a new token.
                retried_auth = True
                self.tokens.refresh_after_401(token)
                continue

            if status == 429:
                rate_limit_retries += 1
                if rate_limit_retries > self.max_rate_limit_retries:
                    raise RateLimitedError(
                        f"Lightspeed is still rate limiting after {self.max_rate_limit_retries} retries. "
                        "Wait a minute and try again."
                    )
                delay = self.limiter.retry_delay(response.headers)
                log.info("429 from Lightspeed; retrying in %.1fs.", delay)
                self.sleep(delay)
                continue

            if status in (500, 502, 503, 504) and server_error_retries < self.max_server_error_retries:
                server_error_retries += 1
                self.sleep(2 * server_error_retries)
                continue

            if status >= 400:
                raise ApiError(status, _error_message(response))

            try:
                return response.json()
            except ValueError:
                raise ApiError(status, "Lightspeed returned a response that is not JSON.") from None


def _error_message(response: httpx.Response) -> str:
    """Lightspeed errors look like {"httpCode": "404", "httpMessage": "Not Found", "message": "..."}."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    message = body.get("message") if isinstance(body, dict) else None
    hint = ""
    if response.status_code == 403:
        hint = " The token's scopes or the authorizing employee's rights do not allow this."
    return f"Lightspeed returned HTTP {response.status_code}: {message or response.reason_phrase}.{hint}"
