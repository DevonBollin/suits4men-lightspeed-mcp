from __future__ import annotations

import httpx
import pytest

from lightspeed_mcp.client import LightspeedClient
from lightspeed_mcp.secret_store import AppConfig, SecretStore, Tokens
from lightspeed_mcp.tokens import TokenManager
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
