"""What is running on this computer: version, account, permissions, write switch. Local only.

For the server_info tool: with the server installed on two computers (the
developer's and the store's), it shows which version each one runs and how it
is set up. It reads files on this computer and Windows Credential Manager; it
makes no Lightspeed request, and never returns a token or the client secret.
"""

from __future__ import annotations

import base64
import json
import platform
import tomllib
from datetime import datetime
from pathlib import Path

from .config import PROJECT_DIR
from .secret_store import SecretStore
from .snapshots import BACKUP_DIR, list_snapshots
from .write_path import WriteLog, WriteSwitch, write_log_report


def git_version(project_dir: Path = PROJECT_DIR) -> dict:
    """The checked-out branch and commit, read straight from .git (no git program needed)."""
    git = Path(project_dir) / ".git"
    head_file = git / "HEAD"
    if not head_file.is_file():
        return {"note": "not a git checkout"}
    head = head_file.read_text(encoding="utf-8").strip()
    if not head.startswith("ref: "):
        return {"branch": None, "commit": head[:7], "note": "a specific version (detached), not a branch"}
    ref = head[5:]
    commit = None
    if (git / ref).is_file():
        commit = (git / ref).read_text(encoding="utf-8").strip()
    elif (git / "packed-refs").is_file():
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                commit = line.split()[0]
    return {"branch": ref.removeprefix("refs/heads/"), "commit": commit[:7] if commit else None}


def package_version(project_dir: Path = PROJECT_DIR) -> str | None:
    try:
        with open(Path(project_dir) / "pyproject.toml", "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return None


def token_summary(store: SecretStore) -> dict:
    """The saved token's permissions and expiry, read from its claims. The token itself is never returned."""
    tokens = store.load_tokens()
    if tokens is None:
        return {"connected": False, "note": "No saved login on this computer: run bootstrap.py."}
    result: dict = {"connected": True,
                    "access_token_expires": datetime.fromtimestamp(tokens.expires_at).astimezone().isoformat(
                        timespec="minutes")}
    try:
        payload = tokens.access_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        result["scopes"] = sorted(str(claims.get("scope", "")).split())
    except (IndexError, ValueError):
        result["scopes"] = None
    return result


def server_info(store: SecretStore, switch: WriteSwitch, write_log: WriteLog, backup_dir: Path = BACKUP_DIR,
                project_dir: Path = PROJECT_DIR) -> dict:
    app = store.load_app()
    snapshots = list_snapshots(backup_dir)
    log = write_log_report(switch, write_log, limit=1)
    return {
        "version": package_version(project_dir),
        "git": git_version(project_dir),
        "computer": platform.node(),
        "project_folder": str(project_dir),
        "lightspeed_account": ({"account_id": app.account_id, "name": app.account_name} if app else None),
        "login": token_summary(store),
        "writes_enabled": switch.is_on(),
        "writes_logged": log["writes_logged"],
        "last_write": (log["writes"][0].get("sent") if log["writes"] else None),
        "snapshots": len(snapshots),
        "latest_snapshot": snapshots[0] if snapshots else None,
    }
