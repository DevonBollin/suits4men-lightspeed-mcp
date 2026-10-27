"""Test doubles: fake keyrings and a fake Lightspeed token endpoint.

Nothing here touches the real Windows Credential Manager or the network.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from filelock import FileLock


class MemoryKeyring:
    """keyring-compatible, in memory, with the real 1280-character limit."""

    MAX_LENGTH = 1280

    def __init__(self):
        self.entries: dict[tuple[str, str], str] = {}
        self.fail_writes = False

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def set_password(self, service, username, password):
        if self.fail_writes:
            raise OSError("simulated Credential Manager failure")
        if len(password) > self.MAX_LENGTH:
            raise OSError("(1783, 'CredWrite', 'The stub received bad data')")
        self.entries[(service, username)] = password

    def delete_password(self, service, username):
        self.entries.pop((service, username), None)


class FileKeyring:
    """keyring-compatible, stored in a JSON file, so several processes can share it."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = FileLock(str(self.path) + ".lock")

    def _load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def get_password(self, service, username):
        with self.lock:
            return self._load().get(f"{service}|{username}")

    def set_password(self, service, username, password):
        with self.lock:
            data = self._load()
            data[f"{service}|{username}"] = password
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)

    def delete_password(self, service, username):
        with self.lock:
            data = self._load()
            data.pop(f"{service}|{username}", None)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)


def long_token(prefix: str, n: int) -> str:
    """A JWT-sized token (long enough to need chunking in Credential Manager)."""
    return f"{prefix}-{n}-" + "x" * 1400


class FakeTokenServer:
    """Behaves like Lightspeed's token endpoint, including refresh-token rotation.

    State is kept in a folder so that several processes can share one fake server.
      valid.txt     the one refresh token that currently works
      calls.txt     one line per refresh request
      busy          exists while a refresh is being handled (detects overlap)
    Presenting any other refresh token gets 400 invalid_grant, which is what
    Lightspeed does after rotation.
    """

    def __init__(self, folder: str | Path, delay: float = 0.0):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self._lock = FileLock(str(self.folder / "server.lock"))

    def reset(self, valid_refresh_token: str) -> None:
        (self.folder / "valid.txt").write_text(valid_refresh_token)
        (self.folder / "calls.txt").write_text("")
        (self.folder / "counter.txt").write_text("0")

    @property
    def calls(self) -> list[str]:
        return [line for line in (self.folder / "calls.txt").read_text().splitlines() if line]

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        busy = self.folder / "busy"
        try:
            fd = os.open(busy, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            with open(self.folder / "calls.txt", "a") as f:
                f.write("OVERLAP\n")
            return httpx.Response(500, json={"error": "overlap"})
        try:
            time.sleep(self.delay)  # widen the window in which a race could happen
            with self._lock:
                valid = (self.folder / "valid.txt").read_text()
                with open(self.folder / "calls.txt", "a") as f:
                    f.write(f"{form.get('grant_type')} pid={os.getpid()} thread={threading.get_ident()}\n")
                if form.get("refresh_token") != valid:
                    return httpx.Response(400, json={"error": "invalid_grant",
                                                     "error_description": "The refresh token is invalid."})
                n = int((self.folder / "counter.txt").read_text()) + 1
                (self.folder / "counter.txt").write_text(str(n))
                new_refresh = long_token("refresh", n)
                (self.folder / "valid.txt").write_text(new_refresh)
            return httpx.Response(200, json={
                "access_token": long_token("access", n),
                "refresh_token": new_refresh,
                "expires_in": 3600,
                "token_type": "Bearer",
            })
        finally:
            os.close(fd)
            os.remove(busy)
