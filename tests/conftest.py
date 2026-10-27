from __future__ import annotations

import httpx
import pytest

from lightspeed_mcp.client import BucketLimiter, LightspeedClient
from lightspeed_mcp.secret_store import AppConfig, SecretStore, Tokens
from lightspeed_mcp.tokens import TokenManager
from lightspeed_mcp.write_path import WRITE_LOG_PATH, WRITE_SWITCH_PATH, WriteClient, WriteLog, WriteSwitch
from tests.fakes import FakeTokenServer, MemoryKeyring, long_token

ACCOUNT_ID = "12345"
BASE = f"https://api.lightspeedapp.com/API/V3/Account/{ACCOUNT_ID}/"


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def keyring_backend():
    return MemoryKeyring()


@pytest.fixture
def store(keyring_backend):
    s = SecretStore(backend=keyring_backend, prefix="test")
    s.save_app(AppConfig("test-client-id", "test-client-secret", ACCOUNT_ID, "Suits 4 Men"))
    return s


def seed_tokens(store: SecretStore, server: FakeTokenServer, expires_at: float) -> Tokens:
    """Save generation-0 tokens and tell the fake server that refresh-0 is the valid one."""
    tokens = Tokens(long_token("access", 0), long_token("refresh", 0), expires_at)
    store.save_tokens(tokens)
    server.reset(tokens.refresh_token)
    return tokens


@pytest.fixture
def token_server(tmp_path):
    return FakeTokenServer(tmp_path / "token-server")


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "state" / "token.lock"


@pytest.fixture
def make_manager(store, token_server, lock_path, clock):
    def make(handler=None, **kwargs) -> TokenManager:
        transport = httpx.MockTransport(handler or token_server.handler)
        return TokenManager(store, httpx.Client(transport=transport), lock_path=lock_path, now=clock, **kwargs)
    return make


class FakeApi:
    """Mock Lightspeed data API. `routes` maps a path (after the account base) to a handler."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.handler = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


@pytest.fixture
def fake_api():
    return FakeApi()


@pytest.fixture
def make_client(fake_api, clock):
    def make(tokens: TokenManager, **kwargs) -> tuple[LightspeedClient, list[float]]:
        sleeps: list[float] = []

        def sleep(seconds):
            sleeps.append(round(seconds, 3))
            clock.advance(seconds)

        client = LightspeedClient(ACCOUNT_ID, tokens, transport=httpx.MockTransport(fake_api),
                                  sleep=sleep, now=clock, **kwargs)
        return client, sleeps
    return make


# ---- the write path, always in a temporary folder ----------------------------------------


@pytest.fixture(autouse=True)
def real_write_switch_untouched():
    """No test may create, remove or use the real write switch or write log in state/."""
    switch_before = WRITE_SWITCH_PATH.exists()
    log_before = WRITE_LOG_PATH.stat().st_size if WRITE_LOG_PATH.exists() else None
    yield
    assert WRITE_SWITCH_PATH.exists() == switch_before, "a test changed the real write switch"
    assert (WRITE_LOG_PATH.stat().st_size if WRITE_LOG_PATH.exists() else None) == log_before, \
        "a test wrote to the real write log"


@pytest.fixture
def write_switch(tmp_path) -> WriteSwitch:
    """A write switch in a temporary folder. OFF until a test calls turn_on()."""
    return WriteSwitch(tmp_path / "state" / "WRITES_ENABLED")


def turn_on(switch: WriteSwitch) -> None:
    switch.path.parent.mkdir(parents=True, exist_ok=True)
    switch.path.write_text("")


@pytest.fixture
def write_log(tmp_path) -> WriteLog:
    return WriteLog(tmp_path / "state" / "write_log.jsonl")


@pytest.fixture
def make_write_client(store, token_server, make_manager, fake_api, clock, write_switch, write_log):
    """A WriteClient whose requests go to fake_api (a mock), never to Lightspeed."""
    def make() -> WriteClient:
        seed_tokens(store, token_server, expires_at=clock() + 3600)

        def sleep(seconds):
            clock.advance(seconds)

        return WriteClient(ACCOUNT_ID, make_manager(), BucketLimiter(sleep=sleep, now=clock), switch=write_switch,
                           write_log=write_log, transport=httpx.MockTransport(fake_api), sleep=sleep)
    return make
