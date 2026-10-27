"""Read-only snapshots of the store's inventory data, and comparing two of them.

A snapshot is one folder under backups/, named by date and time
(e.g. 2026-10-26_202204), with one JSON file per record type. manifest.json
is written last, so a folder without it is incomplete: the snapshot is still
running, or it stopped (then FAILED.txt says why). Snapshots only use the
GET-only read client, so taking one cannot change anything in Lightspeed.

What a snapshot is for: Lightspeed has no "restore from file", so this is not
a one-click undo. It is an exact record of every value at that moment (SKUs,
costs, prices, descriptions, attributes, stock, purchase orders), so anything
changed later can be checked against it and, if needed, put back by an
approved write built from it.

Customer, employee and sales records are not saved. backups/ is not committed
to git, because the files hold costs.

Used by backup_lightspeed.py (command line) and by the take_snapshot and
compare_snapshots tools in server.py.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from .client import LightspeedClient
from .config import PROJECT_DIR

log = logging.getLogger(__name__)

BACKUP_DIR = PROJECT_DIR / "backups"
SNAPSHOT_NAME = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}")

# (record type, query parameters, primary key, JSON key the records come under).
# Everything a write tool can change, plus the reference lists those records
# point at. Add a line to back up another inventory endpoint.
ITEM_RELATIONS = json.dumps(["ItemShops", "ItemAttributes", "ItemVendorNums", "Note"])
ENDPOINTS = [
    ("Item", {"archived": "true", "load_relations": ITEM_RELATIONS}, "itemID", "Item"),
    ("ItemMatrix", {"archived": "true"}, "itemMatrixID", "ItemMatrix"),
    ("ItemAttributeSet", {"archived": "true"}, "itemAttributeSetID", "ItemAttributeSet"),
    ("Category", {"archived": "true"}, "categoryID", "Category"),
    ("Manufacturer", {}, "manufacturerID", "Manufacturer"),
    ("Vendor", {"archived": "true"}, "vendorID", "Vendor"),
    ("Shop", {}, "shopID", "Shop"),
    ("TaxClass", {}, "taxClassID", "TaxClass"),
    ("Order", {"archived": "true", "load_relations": json.dumps(["Note"])}, "orderID", "Order"),
    ("OrderLine", {}, "orderLineID", "OrderLine"),
    # Receipts against POs. Shipment.json answers under "OrderShipment" (NOTES.md).
    ("Shipment", {}, "orderShipmentID", "OrderShipment"),
]

# Fields that make a changed record recognisable in a comparison.
LABEL_FIELDS = ("description", "name", "refNum", "customSku")


# ---- taking a snapshot ------------------------------------------------------------------------


def new_snapshot_folder(backup_dir: Path = BACKUP_DIR, now=datetime.now) -> Path:
    return Path(backup_dir) / now().strftime("%Y-%m-%d_%H%M%S")


def take_snapshot(client: LightspeedClient, folder: Path, progress=None) -> Path:
    """Save every record type in ENDPOINTS to `folder`. GET only.

    `progress(name, count, seconds)` is called after each record type, if given.
    On failure, FAILED.txt is left in the folder and the error is raised.
    """
    folder = Path(folder)
    folder.mkdir(parents=True)
    manifest = {"account_id": client.account_id, "started": datetime.now().astimezone().isoformat(), "counts": {}}
    try:
        for name, params, key, record_key in ENDPOINTS:
            started = time.perf_counter()
            records, _ = client.get_all(f"{name}.json", params, record_key=record_key)
            (folder / f"{name}.json").write_text(
                json.dumps({"key": key, "records": records}, indent=1, ensure_ascii=False), encoding="utf-8")
            manifest["counts"][name] = len(records)
            if progress:
                progress(name, len(records), time.perf_counter() - started)
    except Exception as e:
        (folder / "FAILED.txt").write_text(f"{type(e).__name__}: {e}\n", encoding="utf-8")
        raise
    manifest["finished"] = datetime.now().astimezone().isoformat()
    # Written last: its presence means the snapshot is complete.
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return folder


class SnapshotRunner:
    """Takes snapshots in a background thread, so a tool call can return at once (a snapshot takes about 75 s)."""

    def __init__(self, client: LightspeedClient, backup_dir: Path = BACKUP_DIR):
        self.client = client
        self.backup_dir = Path(backup_dir)
        self._thread: threading.Thread | None = None
        self._folder: Path | None = None
        self._lock = threading.Lock()

    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"started": False, "snapshot": self._folder.name,
                        "note": "A snapshot is already being taken. Wait for it to finish (compare_snapshots shows it "
                                "as complete), then start another."}
            folder = new_snapshot_folder(self.backup_dir)
            if folder.exists():
                return {"started": False, "snapshot": folder.name, "note": "A snapshot was started this second; wait a moment."}
            self._folder = folder
            self._thread = threading.Thread(target=self._run, args=(folder,), name="snapshot", daemon=True)
            self._thread.start()
        return {"started": True, "snapshot": folder.name, "folder": str(folder),
                "note": "Reading every inventory record from Lightspeed (GET only); this takes about 75 seconds. "
                        "compare_snapshots lists it as complete when done."}

    def _run(self, folder: Path) -> None:
        try:
            take_snapshot(self.client, folder)
            log.info("Snapshot %s complete.", folder.name)
        except Exception:
            log.exception("Snapshot %s failed.", folder.name)  # FAILED.txt is in the folder

    def wait(self, timeout: float | None = None) -> None:
        """For tests and scripts: block until the running snapshot (if any) finishes."""
        if self._thread:
            self._thread.join(timeout)


# ---- listing and comparing ----------------------------------------------------------------------


def list_snapshots(backup_dir: Path = BACKUP_DIR) -> list[dict]:
    """Every snapshot, newest first, with whether it is complete and its record counts."""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return []
    rows = []
    for folder in sorted((f for f in backup_dir.iterdir() if f.is_dir() and SNAPSHOT_NAME.fullmatch(f.name)),
                         key=lambda f: f.name, reverse=True):
        row: dict = {"snapshot": folder.name}
        manifest = folder / "manifest.json"
        failed = folder / "FAILED.txt"
        if manifest.exists():
            row["status"] = "complete"
            row["counts"] = json.loads(manifest.read_text(encoding="utf-8")).get("counts", {})
        elif failed.exists():
            row["status"] = "failed: " + failed.read_text(encoding="utf-8").strip()
        else:
            row["status"] = "incomplete (still running, or stopped)"
        rows.append(row)
    return rows


def snapshot_folder(name: str, backup_dir: Path = BACKUP_DIR) -> Path:
    """A complete snapshot's folder by name. Only names like 2026-10-26_202204 inside backup_dir are accepted."""
    name = (name or "").strip()
    if not SNAPSHOT_NAME.fullmatch(name):
        raise ValueError(f"'{name}' is not a snapshot name; they look like 2026-10-26_202204.")
    folder = Path(backup_dir) / name
    if not (folder / "manifest.json").exists():
        raise ValueError(f"Snapshot {name} does not exist or is not complete.")
    return folder


def compare_report(older: str | None = None, newer: str | None = None, record_type: str | None = None,
                   limit: int = 20, backup_dir: Path = BACKUP_DIR) -> dict:
    """For the compare_snapshots tool: compare two snapshots (by default the two newest complete ones)."""
    available = list_snapshots(backup_dir)
    listing = available[:10]
    complete = [s["snapshot"] for s in available if s["status"] == "complete"]
    if older is None and newer is None:
        if len(complete) < 2:
            return {"snapshots": listing, "note": f"{len(complete)} complete snapshot(s); at least 2 are needed. "
                                                  "take_snapshot makes one."}
        newer, older = complete[0], complete[1]
    elif older is None or newer is None:
        raise ValueError("Give both older and newer, or neither (to compare the two newest).")
    limit = max(1, min(int(limit), 200))
    result = compare(snapshot_folder(older, backup_dir), snapshot_folder(newer, backup_dir), record_type, limit)
    result["snapshots"] = listing
    return result


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


def _id_order(record_id: str):
    return (0, int(record_id)) if record_id.isdigit() else (1, record_id)


def _label(record: dict) -> str | None:
    return next((str(record[f]) for f in LABEL_FIELDS if record.get(f)), None)


def compare(old_folder: Path, new_folder: Path, record_type: str | None = None, limit: int = 20) -> dict:
    """What was added, removed or changed (field by field) between two snapshots.

    Counts always cover everything; at most `limit` records per type are listed.
    """
    types = [e for e in ENDPOINTS if record_type in (None, "", e[0])]
    if not types:
        raise ValueError(f"record_type must be one of: {', '.join(e[0] for e in ENDPOINTS)}.")
    result: dict = {"older": Path(old_folder).name, "newer": Path(new_folder).name, "differences": 0, "record_types": {}}
    for name, _, _, _ in types:
        old_file, new_file = Path(old_folder) / f"{name}.json", Path(new_folder) / f"{name}.json"
        if not old_file.exists() or not new_file.exists():
            result["record_types"][name] = {"note": "missing from one snapshot"}
            continue
        old, new = (json.loads(f.read_text(encoding="utf-8")) for f in (old_file, new_file))
        key = old["key"]
        before = {str(r.get(key)): r for r in old["records"]}
        after = {str(r.get(key)): r for r in new["records"]}
        added = sorted(after.keys() - before.keys(), key=_id_order)
        removed = sorted(before.keys() - after.keys(), key=_id_order)
        changed = []
        for record_id in sorted(before.keys() & after.keys(), key=_id_order):
            a, b = flatten(before[record_id]), flatten(after[record_id])
            fields = sorted(f for f in a.keys() | b.keys() if a.get(f) != b.get(f))
            if fields:
                changed.append({"id": record_id, "label": _label(after[record_id]),
                                "changes": {f: [a.get(f), b.get(f)] for f in fields[:10]},
                                **({"more_fields_changed": len(fields) - 10} if len(fields) > 10 else {})})

        result["differences"] += len(added) + len(removed) + len(changed)
        summary: dict = {"added": len(added), "removed": len(removed), "changed": len(changed)}
        if added:
            summary["added_records"] = [{"id": i, "label": _label(after[i])} for i in added[:limit]]
        if removed:
            summary["removed_records"] = [{"id": i, "label": _label(before[i])} for i in removed[:limit]]
        if changed:
            summary["changed_records"] = changed[:limit]
        if max(len(added), len(removed), len(changed)) > limit:
            summary["more_not_listed"] = True
        result["record_types"][name] = summary
    return result
