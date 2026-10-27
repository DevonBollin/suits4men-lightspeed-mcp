"""Chunked, atomically-switched records in (fake) Credential Manager."""

from __future__ import annotations

import pytest

from lightspeed_mcp.errors import SecretStoreError
from lightspeed_mcp.secret_store import SecretStore, Tokens
from tests.fakes import MemoryKeyring, long_token


def test_long_tokens_are_split_under_the_1280_character_limit():
    backend = MemoryKeyring()
    store = SecretStore(backend=backend, prefix="t")
    tokens = Tokens(long_token("access", 1), long_token("refresh", 1), 123.0)
    store.save_tokens(tokens)  # MemoryKeyring raises if any entry exceeds 1280

    assert store.load_tokens() == tokens
    assert max(len(v) for v in backend.entries.values()) <= 1000


def test_previous_generation_is_kept_and_older_ones_deleted():
    backend = MemoryKeyring()
    store = SecretStore(backend=backend, prefix="t")
    for n in (1, 2, 3):
        store.save_tokens(Tokens(long_token("access", n), long_token("refresh", n), n))

    services = {service for service, _ in backend.entries}
    assert not any("/g1/" in s for s in services)  # two generations old: deleted
    assert any("/g2/" in s for s in services)  # previous: kept for in-flight readers
    assert store.load_tokens().access_token == long_token("access", 3)


def test_failed_write_leaves_the_old_record_readable():
    backend = MemoryKeyring()
    store = SecretStore(backend=backend, prefix="t")
    old = Tokens(long_token("access", 1), long_token("refresh", 1), 1.0)
    store.save_tokens(old)

    real_set = backend.set_password
    writes = []

    def fail_after_first_chunk(service, username, password):
        writes.append(service)
        if len(writes) > 1:
            raise OSError("simulated crash mid-write")
        real_set(service, username, password)

    backend.set_password = fail_after_first_chunk
    with pytest.raises(SecretStoreError):
        store.save_tokens(Tokens(long_token("access", 2), long_token("refresh", 2), 2.0))

    assert store.load_tokens() == old  # pointer was never switched


def test_missing_record_reads_as_none():
    assert SecretStore(backend=MemoryKeyring(), prefix="t").load_tokens() is None


def test_delete_removes_every_entry():
    backend = MemoryKeyring()
    store = SecretStore(backend=backend, prefix="t")
    store.save_tokens(Tokens(long_token("access", 1), long_token("refresh", 1), 1.0))
    store.save_tokens(Tokens(long_token("access", 2), long_token("refresh", 2), 2.0))
    store.delete("tokens")
    assert backend.entries == {}
