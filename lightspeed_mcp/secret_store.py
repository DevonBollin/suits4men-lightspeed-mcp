"""Secrets in Windows Credential Manager, via the `keyring` library.

Two records are stored, each a small JSON object:

    "app"     client_id, client_secret, account_id, account_name
    "tokens"  access_token, refresh_token, expires_at, reauth_required

Why records are split into chunks
---------------------------------
Windows Credential Manager refuses any single entry longer than 1280
characters (measured on this machine: CredWrite fails at 1281). A Lightspeed
access token is a JWT, and together with the refresh token it can exceed that.

So a record is written like this:

  1. Its JSON is cut into chunks of at most 1000 characters, stored under a new
     "generation" number:  suits4men-lightspeed/tokens/g7/0, .../g7/1, ...
  2. One small "pointer" entry, suits4men-lightspeed/tokens, is overwritten to
     say "generation 7, 2 chunks". This single write is the commit.
  3. The generation before the previous one (g5) is deleted.

A reader reads the pointer and then that generation's chunks, so it sees
either the whole old record or the whole new one, never a mix. The previous
generation (g6) is kept so a reader that read the old pointer a moment ago can
still finish. Each entry has its own service name, so keyring's Windows
backend never falls back to its "username@service" naming.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import keyring
import keyring.errors

from .config import KEYRING_PREFIX
from .errors import SecretStoreError

CHUNK_SIZE = 1000  # characters; well under the 1280 limit


@dataclass
class AppConfig:
    client_id: str
    client_secret: str
    account_id: str | None = None
    account_name: str | None = None


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float  # Unix time (seconds) when the access token expires
    # Set when Lightspeed rejects the refresh token (invalid_grant). Once set,
    # the server stops calling the token endpoint until bootstrap.py runs again.
    reauth_required: bool = False


class SecretStore:
    """Reads and writes the "app" and "tokens" records.

    `backend` is anything with keyring's get_password / set_password /
    delete_password functions. The real one is the keyring module itself
    (Windows Credential Manager); tests pass an in-memory fake.
    """

    def __init__(self, backend=keyring, prefix: str = KEYRING_PREFIX):
        self.backend = backend
        self.prefix = prefix

    # ---- typed helpers -------------------------------------------------

    def load_app(self) -> AppConfig | None:
        data = self.read("app")
        return AppConfig(**data) if data else None

    def save_app(self, app: AppConfig) -> None:
        self.write("app", asdict(app))

    def load_tokens(self) -> Tokens | None:
        data = self.read("tokens")
        return Tokens(**data) if data else None

    def save_tokens(self, tokens: Tokens) -> None:
        self.write("tokens", asdict(tokens))

    # ---- generic chunked records ----------------------------------------

    def read(self, record: str) -> dict | None:
        """Return the record as a dict, or None if it was never written."""
        # Retry a couple of times: a writer may delete an old generation
        # between our pointer read and our chunk reads.
        for _attempt in range(3):
            pointer = self._read_pointer(record)
            if pointer is None:
                return None
            parts = [
                self._get(self._chunk_name(record, pointer["gen"], i), record)
                for i in range(pointer["chunks"])
            ]
            if all(part is not None for part in parts):
                try:
                    return json.loads("".join(parts))
                except json.JSONDecodeError as e:
                    raise SecretStoreError(f"Saved '{record}' record is corrupt: {e}") from None
        raise SecretStoreError(f"Could not read a consistent '{record}' record from Windows Credential Manager.")

    def write(self, record: str, data: dict) -> None:
        """Replace the record. Readers never see a half-written record."""
        text = json.dumps(data, separators=(",", ":"))
        chunks = [text[i : i + CHUNK_SIZE] for i in range(0, len(text), CHUNK_SIZE)]

        old = self._read_pointer(record)
        gen = old["gen"] + 1 if old else 1

        for i, chunk in enumerate(chunks):
            self._set(self._chunk_name(record, gen, i), record, chunk)

        # The commit: one small write switches every reader to the new generation.
        pointer = {"gen": gen, "chunks": len(chunks)}
        if old:
            pointer["prev"] = {"gen": old["gen"], "chunks": old["chunks"]}
        self._set(self._pointer_name(record), record, json.dumps(pointer))

        # Clean up the generation before the previous one (best effort).
        if old and "prev" in old:
            self._delete_generation(record, old["prev"]["gen"], old["prev"]["chunks"])

    def delete(self, record: str) -> None:
        """Remove the record and all of its chunks."""
        pointer = self._read_pointer(record)
        if pointer is None:
            return
        self._delete_generation(record, pointer["gen"], pointer["chunks"])
        if "prev" in pointer:
            self._delete_generation(record, pointer["prev"]["gen"], pointer["prev"]["chunks"])
        self._delete(self._pointer_name(record), record)

    # ---- internals ---------------------------------------------------------

    def _pointer_name(self, record: str) -> str:
        return f"{self.prefix}/{record}"

    def _chunk_name(self, record: str, gen: int, index: int) -> str:
        return f"{self.prefix}/{record}/g{gen}/{index}"

    def _read_pointer(self, record: str) -> dict | None:
        raw = self._get(self._pointer_name(record), record)
        if raw is None:
            return None
        try:
            pointer = json.loads(raw)
            int(pointer["gen"]), int(pointer["chunks"])
            return pointer
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise SecretStoreError(f"Saved '{record}' pointer entry is corrupt.") from None

    def _delete_generation(self, record: str, gen: int, chunks: int) -> None:
        for i in range(chunks):
            self._delete(self._chunk_name(record, gen, i), record)

    def _get(self, service: str, username: str) -> str | None:
        try:
            return self.backend.get_password(service, username)
        except Exception as e:  # keyring errors and pywintypes.error (not a KeyringError)
            raise SecretStoreError(f"Windows Credential Manager read failed: {e}") from None

    def _set(self, service: str, username: str, value: str) -> None:
        try:
            self.backend.set_password(service, username, value)
        except Exception as e:  # pywintypes.error is not a KeyringError
            raise SecretStoreError(f"Windows Credential Manager write failed: {e}") from None

    def _delete(self, service: str, username: str) -> None:
        try:
            self.backend.delete_password(service, username)
        except keyring.errors.PasswordDeleteError:
            pass  # already gone
        except Exception:
            pass  # cleanup is best effort; a leftover old chunk is harmless
