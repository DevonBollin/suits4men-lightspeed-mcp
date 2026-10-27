"""Save a read-only snapshot of the store's inventory data, or compare two snapshots.

    .venv\\Scripts\\python.exe backup_lightspeed.py                  # take a snapshot
    .venv\\Scripts\\python.exe backup_lightspeed.py --compare A B    # what changed between two snapshots

A snapshot is one folder under backups/ (named by date and time) with one
JSON file per record type, plus manifest.json with the record counts. It
uses the same GET-only client as the MCP server, so taking one cannot change
anything in Lightspeed.

What a snapshot is for: Lightspeed has no "restore from file", so this is not
a one-click undo. It is an exact record of every value at that moment (SKUs,
costs, prices, descriptions, attributes, stock, purchase orders), so anything
changed later can be checked against it and, if needed, put back by an
approved write built from it.

Customer, employee and sales records are not saved. backups/ is not committed
to git, because the files hold costs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

from lightspeed_mcp.client import LightspeedClient
from lightspeed_mcp.config import HTTP_TIMEOUT_SECONDS, PROJECT_DIR
from lightspeed_mcp.secret_store import SecretStore
from lightspeed_mcp.tokens import TokenManager

BACKUP_DIR = PROJECT_DIR / "backups"

# (record type, query parameters, primary key). Everything a Phase 2 write tool
# can change, plus the reference lists those records point at. Add a line to
# back up another inventory endpoint.
ITEM_RELATIONS = json.dumps(["ItemShops", "ItemAttributes", "ItemVendorNums", "Note"])
ENDPOINTS = [
    ("Item", {"archived": "true", "load_relations": ITEM_RELATIONS}, "itemID"),
    ("ItemMatrix", {"archived": "true"}, "itemMatrixID"),
    ("ItemAttributeSet", {"archived": "true"}, "itemAttributeSetID"),
    ("Category", {"archived": "true"}, "categoryID"),
    ("Manufacturer", {}, "manufacturerID"),
    ("Vendor", {"archived": "true"}, "vendorID"),
    ("Shop", {}, "shopID"),
    ("TaxClass", {}, "taxClassID"),
    ("Order", {"archived": "true", "load_relations": json.dumps(["Note"])}, "orderID"),
    ("OrderLine", {}, "orderLineID"),
    ("Shipment", {}, "orderShipmentID"),  # receipts against POs; records come back under "OrderShipment"
]


def make_client() -> LightspeedClient:
    """The same GET-only client the MCP server uses."""
    store = SecretStore()
    app = store.load_app()
    if app is None or not app.account_id:
        sys.exit("This computer has not been connected to Lightspeed yet. Run bootstrap.py first.")
    tokens = TokenManager(store, httpx.Client(timeout=HTTP_TIMEOUT_SECONDS))
    return LightspeedClient(app.account_id, tokens)


def records_of(client: LightspeedClient, name: str, params: dict) -> list[dict]:
    """Every record of one type. Shipment.json answers under the key "OrderShipment" (see NOTES.md)."""
    if name != "Shipment":
        records, _ = client.get_all(f"{name}.json", params)
        return records
    records: list[dict] = []
    url, page_params = client.url_for("Shipment.json"), {**params, "limit": "100"}
    while True:
        body = client._get_url(url, page_params)  # same GET-only request loop that get_all uses
        value = body.get("OrderShipment")
        records.extend(value if isinstance(value, list) else [value] if value else [])
        next_url = (body.get("@attributes") or {}).get("next") or ""
        if not next_url:
            return records
        url, page_params = client.check_next_url(next_url), None


def take_snapshot() -> Path:
    client = make_client()
    folder = BACKUP_DIR / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    folder.mkdir(parents=True)
    manifest = {"account_id": client.account_id, "started": datetime.now().astimezone().isoformat(), "counts": {}}

    for name, params, key in ENDPOINTS:
        started = time.perf_counter()
        records = records_of(client, name, params)
        (folder / f"{name}.json").write_text(
            json.dumps({"key": key, "records": records}, indent=1, ensure_ascii=False), encoding="utf-8")
        manifest["counts"][name] = len(records)
        print(f"{name:<18} {len(records):>6} records  {time.perf_counter() - started:5.1f}s")

    manifest["finished"] = datetime.now().astimezone().isoformat()
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"\nSaved to {folder}")
    return folder


# ---- comparing two snapshots ----------------------------------------------------------------


def flatten(value, prefix: str = "") -> dict[str, object]:
    """{"Prices": {"ItemPrice": [{"amount": "1"}]}} -> {"Prices.ItemPrice.0.amount": "1"}."""
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for k, v in value.items():
            out.update(flatten(v, f"{prefix}{k}."))
        return out
    if isinstance(value, list):
        out = {}
        for i, v in enumerate(value):
            out.update(flatten(v, f"{prefix}{i}."))
        return out
    return {prefix.rstrip("."): value}


def compare(old_folder: Path, new_folder: Path, show: int = 20) -> int:
    """Print what was added, removed or changed between two snapshots. Returns the number of differences."""
    differences = 0
    for name, _, _ in ENDPOINTS:
        old_file, new_file = old_folder / f"{name}.json", new_folder / f"{name}.json"
        if not old_file.exists() or not new_file.exists():
            print(f"{name}: missing from one snapshot, skipped")
            continue
        old, new = (json.loads(f.read_text(encoding="utf-8")) for f in (old_file, new_file))
        key = old["key"]
        before = {str(r.get(key)): r for r in old["records"]}
        after = {str(r.get(key)): r for r in new["records"]}
        added = sorted(after.keys() - before.keys(), key=lambda k: int(k) if k.isdigit() else 0)
        removed = sorted(before.keys() - after.keys(), key=lambda k: int(k) if k.isdigit() else 0)
        changed = []
        for record_id in sorted(before.keys() & after.keys(), key=lambda k: int(k) if k.isdigit() else 0):
            a, b = flatten(before[record_id]), flatten(after[record_id])
            fields = sorted(f for f in a.keys() | b.keys() if a.get(f) != b.get(f))
            if fields:
                changed.append((record_id, fields, a, b))

        differences += len(added) + len(removed) + len(changed)
        print(f"{name}: {len(added)} added, {len(removed)} removed, {len(changed)} changed")
        for record_id in added[:show]:
            print(f"  + {key} {record_id}")
        for record_id in removed[:show]:
            print(f"  - {key} {record_id}")
        for record_id, fields, a, b in changed[:show]:
            detail = "; ".join(f"{f}: {a.get(f)!r} -> {b.get(f)!r}" for f in fields[:6])
            print(f"  ~ {key} {record_id}: {detail}{' ...' if len(fields) > 6 else ''}")
        hidden = max(0, len(added) - show) + max(0, len(removed) - show) + max(0, len(changed) - show)
        if hidden:
            print(f"  ... and {hidden} more")
    return differences


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--compare", nargs=2, metavar=("OLD", "NEW"), help="two snapshot folders to compare")
    args = parser.parse_args()
    if args.compare:
        old, new = (Path(p) if Path(p).is_absolute() else BACKUP_DIR / p for p in args.compare)
        compare(old, new)
        return 0
    take_snapshot()
    return 0


if __name__ == "__main__":
    sys.exit(main())
