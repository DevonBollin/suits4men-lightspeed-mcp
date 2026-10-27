"""Runs in a separate process for the cross-process refresh test (test_tokens.py)."""

from __future__ import annotations

from pathlib import Path

import httpx

from lightspeed_mcp.secret_store import SecretStore
from lightspeed_mcp.tokens import TokenManager
from tests.fakes import FakeTokenServer, FileKeyring


def refresh_in_process(store_path, lock_path, server_folder, barrier, results) -> None:
    store = SecretStore(backend=FileKeyring(store_path), prefix="test")
    server = FakeTokenServer(server_folder, delay=0.3)
    manager = TokenManager(store, httpx.Client(transport=httpx.MockTransport(server.handler)), lock_path=Path(lock_path))
    barrier.wait()  # all processes ask for a token at the same moment
    try:
        results.put(("ok", manager.access_token()))
    except Exception as e:  # report, don't crash the test runner
        results.put(("error", repr(e)))
