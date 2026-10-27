"""Token rotation, the refresh lock, and invalid_grant handling."""

from __future__ import annotations

import multiprocessing
import threading

import httpx
import pytest

from lightspeed_mcp.errors import ReauthorizationRequired, TokenRefreshError
from lightspeed_mcp.secret_store import AppConfig, SecretStore
from lightspeed_mcp.tokens import TokenManager
from tests.conftest import seed_tokens
from tests.fakes import FakeTokenServer, FileKeyring, long_token
from tests.mp_worker import refresh_in_process


def ok_json(body=None):
    return httpx.Response(200, json=body or {"Item": []})


def test_fresh_token_is_used_without_refreshing(store, token_server, make_manager, clock):
    seed_tokens(store, token_server, expires_at=clock() + 3600)
    assert make_manager().access_token() == long_token("access", 0)
    assert token_server.calls == []


def test_near_expiry_refreshes_once_and_saves_the_new_pair(store, token_server, make_manager, clock):
    seed_tokens(store, token_server, expires_at=clock() + 60)  # inside the 120s margin
    manager = make_manager()

    assert manager.access_token() == long_token("access", 1)
    saved = store.load_tokens()
    assert saved.refresh_token == long_token("refresh", 1)
    assert saved.expires_at == clock() + 3600
    # A second call uses the saved token; no second refresh.
    assert manager.access_token() == long_token("access", 1)
    assert len(token_server.calls) == 1


def test_new_pair_is_saved_before_the_new_access_token_is_used(store, token_server, make_manager, make_client, fake_api, clock):
    seed_tokens(store, token_server, expires_at=clock() - 1)  # expired

    seen = []

    def api(request):
        # The moment Lightspeed first sees the new access token, the old refresh
        # token is revoked. By then the new refresh token must already be saved.
        token = request.headers["Authorization"].removeprefix("Bearer ")
        saved = store.load_tokens()
        seen.append((token == long_token("access", 1), saved.refresh_token == long_token("refresh", 1),
                     saved.access_token == token))
        return ok_json()

    fake_api.handler = api
    client, _ = make_client(make_manager())
    client.get("Item.json")
    assert seen == [(True, True, True)]


def test_if_saving_fails_the_new_access_token_is_never_used(store, keyring_backend, token_server, make_manager, make_client, fake_api, clock):
    seed_tokens(store, token_server, expires_at=clock() - 1)

    def token_endpoint_then_break_keyring(request):
        response = token_server.handler(request)
        keyring_backend.fail_writes = True  # Credential Manager fails right after Lightspeed answers
        return response

    fake_api.handler = lambda request: ok_json()
    client, _ = make_client(make_manager(token_endpoint_then_break_keyring))

    with pytest.raises(TokenRefreshError, match="not used"):
        client.get("Item.json")
    assert fake_api.requests == []  # the API never saw the new token
    assert store.load_tokens().refresh_token == long_token("refresh", 0)


def test_invalid_grant_asks_for_bootstrap_and_stops_calling_the_token_endpoint(store, token_server, make_manager, clock):
    seed_tokens(store, token_server, expires_at=clock() - 1)
    token_server.reset("some-other-refresh-token")  # ours has been revoked
    manager = make_manager()

    with pytest.raises(ReauthorizationRequired, match="bootstrap.py") as first:
        manager.access_token()
    assert "invalid_grant" in str(first.value)
    assert store.load_tokens().reauth_required is True

    # Later calls fail fast without hitting Lightspeed again.
    with pytest.raises(ReauthorizationRequired, match="bootstrap.py"):
        manager.access_token()
    with pytest.raises(ReauthorizationRequired):
        manager.refresh_after_401(long_token("access", 0))
    assert len(token_server.calls) == 1


def test_refresh_after_401_reuses_a_token_another_process_already_refreshed(store, token_server, make_manager, clock):
    seed_tokens(store, token_server, expires_at=clock() + 3600)
    manager = make_manager()
    rejected = manager.access_token()

    # Meanwhile another process refreshed and saved a new pair.
    other = store.load_tokens()
    other.access_token, other.refresh_token = long_token("access", 99), long_token("refresh", 99)
    store.save_tokens(other)

    assert manager.refresh_after_401(rejected) == long_token("access", 99)
    assert token_server.calls == []


def test_refresh_keeps_working_across_several_rotations(store, token_server, make_manager, clock):
    seed_tokens(store, token_server, expires_at=clock() - 1)
    manager = make_manager()
    for n in (1, 2, 3):
        assert manager.access_token() == long_token("access", n)
        clock.advance(3600)  # let it expire again
    assert len(token_server.calls) == 3


@pytest.mark.parametrize("separate_managers", [False, True], ids=["one-manager", "manager-per-thread"])
def test_concurrent_refresh_in_threads_happens_once(store, tmp_path, lock_path, clock, separate_managers):
    """Eight simultaneous callers with an expired token: exactly one refresh.

    "manager-per-thread" gives each thread its own TokenManager and FileLock
    object, which is how separate processes look to the lock file.
    """
    server = FakeTokenServer(tmp_path / "server", delay=0.2)
    seed_tokens(store, server, expires_at=clock() - 1)

    def make():
        return TokenManager(store, httpx.Client(transport=httpx.MockTransport(server.handler)),
                            lock_path=lock_path, now=clock)

    shared = make()
    barrier = threading.Barrier(8)
    results, errors = [], []

    def worker():
        manager = make() if separate_managers else shared
        barrier.wait()
        try:
            results.append(manager.access_token())
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(server.calls) == 1, server.calls
    assert set(results) == {long_token("access", 1)}


def test_concurrent_refresh_across_processes_happens_once(tmp_path):
    """Four real processes, one shared (file-backed) keyring and lock file."""
    store_path = tmp_path / "keyring.json"
    lock_path = tmp_path / "state" / "token.lock"
    server_folder = tmp_path / "server"

    store = SecretStore(backend=FileKeyring(store_path), prefix="test")
    store.save_app(AppConfig("test-client-id", "test-client-secret", "12345", "Suits 4 Men"))
    server = FakeTokenServer(server_folder)
    seed_tokens(store, server, expires_at=0)  # long expired

    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(4), ctx.Queue()
    procs = [ctx.Process(target=refresh_in_process,
                         args=(str(store_path), str(lock_path), str(server_folder), barrier, results))
             for _ in range(4)]
    for p in procs:
        p.start()
    outcomes = [results.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=30)

    assert all(kind == "ok" for kind, _ in outcomes), outcomes
    assert {token for _, token in outcomes} == {long_token("access", 1)}
    assert len(server.calls) == 1, server.calls  # no OVERLAP lines, no second refresh
    assert store.load_tokens().refresh_token == long_token("refresh", 1)


def test_network_error_during_refresh_changes_nothing(store, token_server, make_manager, clock):
    seed_tokens(store, token_server, expires_at=clock() - 1)

    def down(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(TokenRefreshError, match="Could not reach"):
        make_manager(down).access_token()
    saved = store.load_tokens()
    assert saved.refresh_token == long_token("refresh", 0)
    assert saved.reauth_required is False
