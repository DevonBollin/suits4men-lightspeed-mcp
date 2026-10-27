"""server_info: version, account, permissions and switch, without ever showing a token."""

from __future__ import annotations

import base64
import json

from lightspeed_mcp import info
from lightspeed_mcp.secret_store import Tokens
from tests.conftest import turn_on


def fake_jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.signature-{'x' * 400}"


def make_git(folder, branch="stable", commit="a46eca1f00d", packed=False):
    git = folder / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    if packed:
        (git / "packed-refs").write_text(f"# pack-refs\n{commit} refs/heads/{branch}\n")
    else:
        (git / "refs" / "heads" / branch).write_text(commit + "\n")


def test_server_info_reports_version_account_scopes_and_switch(tmp_path, store, write_switch, write_log):
    make_git(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.0"\n')
    token = fake_jwt({"scope": "employee:inventory employee:vendors", "acct": "12345"})
    store.save_tokens(Tokens(token, "refresh-secret-" + "y" * 400, 2_000_000_000))
    turn_on(write_switch)

    result = info.server_info(store, write_switch, write_log, backup_dir=tmp_path / "backups", project_dir=tmp_path)

    assert result["version"] == "1.2.0"
    assert result["git"] == {"branch": "stable", "commit": "a46eca1"}
    assert result["lightspeed_account"] == {"account_id": "12345", "name": "Suits 4 Men"}
    assert result["login"]["scopes"] == ["employee:inventory", "employee:vendors"]
    assert result["writes_enabled"] is True and result["writes_logged"] == 0
    text = json.dumps(result)
    assert token not in text and "refresh-secret" not in text and "test-client-secret" not in text


def test_packed_refs_and_detached_head(tmp_path):
    make_git(tmp_path, packed=True)
    assert info.git_version(tmp_path) == {"branch": "stable", "commit": "a46eca1"}
    (tmp_path / ".git" / "HEAD").write_text("0123456789abcdef\n")
    assert info.git_version(tmp_path)["commit"] == "0123456"


def test_not_connected_and_not_a_checkout(tmp_path, keyring_backend, write_switch, write_log):
    from lightspeed_mcp.secret_store import SecretStore
    empty = SecretStore(backend=keyring_backend, prefix="empty")
    result = info.server_info(empty, write_switch, write_log, backup_dir=tmp_path / "b", project_dir=tmp_path)
    assert result["login"]["connected"] is False and result["lightspeed_account"] is None
    assert result["git"] == {"note": "not a git checkout"}
