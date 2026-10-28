"""Save a read-only snapshot of the store's inventory data, or compare two snapshots.

    .venv\\Scripts\\python.exe backup_lightspeed.py                  # take a snapshot
    .venv\\Scripts\\python.exe backup_lightspeed.py --list           # list snapshots
    .venv\\Scripts\\python.exe backup_lightspeed.py --compare A B    # what changed between two snapshots

Snapshots go to backups/ (one folder per snapshot, named by date and time).
Taking one only sends GET requests, so it cannot change anything in
Lightspeed. The logic lives in lightspeed_mcp/snapshots.py, which the
take_snapshot and compare_snapshots tools in Claude Desktop use too.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


from lightspeed_mcp import snapshots
from lightspeed_mcp.client import LightspeedClient, make_http
from lightspeed_mcp.secret_store import SecretStore
from lightspeed_mcp.tokens import TokenManager


def make_client() -> LightspeedClient:
    """The same GET-only client the MCP server uses."""
    store = SecretStore()
    app = store.load_app()
    if app is None or not app.account_id:
        sys.exit("This computer has not been connected to Lightspeed yet. Run bootstrap.py first.")
    tokens = TokenManager(store, make_http())
    return LightspeedClient(app.account_id, tokens)


def print_comparison(result: dict) -> None:
    print(f"{result['older']} -> {result['newer']}: {result['differences']} difference(s)")
    for name, summary in result["record_types"].items():
        if "note" in summary:
            print(f"{name}: {summary['note']}")
            continue
        print(f"{name}: {summary['added']} added, {summary['removed']} removed, {summary['changed']} changed")
        for r in summary.get("added_records", []):
            print(f"  + {r['id']} {r['label'] or ''}")
        for r in summary.get("removed_records", []):
            print(f"  - {r['id']} {r['label'] or ''}")
        for r in summary.get("changed_records", []):
            detail = "; ".join(f"{f}: {old!r} -> {new!r}" for f, (old, new) in r["changes"].items())
            print(f"  ~ {r['id']} {r['label'] or ''}: {detail}")
        if summary.get("more_not_listed"):
            print("  ... and more")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--compare", nargs=2, metavar=("OLD", "NEW"), help="two snapshot names to compare")
    parser.add_argument("--list", action="store_true", help="list snapshots")
    args = parser.parse_args()

    if args.list:
        for row in snapshots.list_snapshots():
            print(f"{row['snapshot']}  {row['status']}")
        return 0
    if args.compare:
        old, new = (snapshots.snapshot_folder(Path(name).name) for name in args.compare)
        print_comparison(snapshots.compare(old, new))
        return 0

    folder = snapshots.new_snapshot_folder()
    snapshots.take_snapshot(make_client(), folder,
                            progress=lambda name, n, s: print(f"{name:<18} {n:>6} records  {s:5.1f}s"))
    print(f"\nSaved to {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
