"""Snapshots (taken with a canned read client into a temporary folder), comparing them,
the next-SKU suggestions and the write-log report. Nothing reaches Lightspeed."""

from __future__ import annotations

import copy
import json

import httpx
import pytest

from lightspeed_mcp import snapshots
from lightspeed_mcp.client import LightspeedClient
from lightspeed_mcp.reader import InventoryReader
from lightspeed_mcp.write_path import WriteLog, write_log_report
from tests.conftest import BASE, seed_tokens, turn_on

RECORDS = {
    "Item": [{"itemID": "1345", "description": "Caravelli Beige 50S Classic", "customSku": "012911", "archived": "false",
              "ItemShops": {"ItemShop": [{"shopID": "0", "qoh": "0"}]}},
             {"itemID": "1700", "description": "Old item", "customSku": "013470", "archived": "true"},
             {"itemID": "2321", "description": "Caravelli Burgundy 58L Slim", "customSku": "", "archived": "false"},
             {"itemID": "2400", "description": "Odd SKU", "customSku": "A-17", "archived": "false"}],
    "ItemMatrix": [{"itemMatrixID": "7", "description": "Caravelli"}],
    "OrderShipment": [{"orderShipmentID": "1", "orderID": "853"}],  # Shipment.json's records
}


class CannedReads:
    """Answers get_all like the live account. Has no way to send anything but a read."""

    account_id = "12345"
    record_key = staticmethod(LightspeedClient.record_key)

    def __init__(self, fail_on: str | None = None):
        self.data = copy.deepcopy(RECORDS)
        self.calls = []
        self.fail_on = fail_on

    def get_all(self, path, params=None, max_records=None, record_key=None):
        self.calls.append((path, dict(params or {}), record_key))
        key = record_key or self.record_key(path)
        if key == self.fail_on:
            raise RuntimeError("simulated network failure")
        return copy.deepcopy(self.data.get(key, [])), False


def snap(reads, backup_dir, name):
    return snapshots.take_snapshot(reads, backup_dir / name)


# ---- taking snapshots --------------------------------------------------------------------------


def test_snapshot_saves_every_record_type_and_the_manifest(tmp_path):
    reads = CannedReads()
    folder = snap(reads, tmp_path, "2026-10-27_090000")
    for name, _, key, _ in snapshots.ENDPOINTS:
        saved = json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))
        assert saved["key"] == key
    assert json.loads((folder / "Item.json").read_text(encoding="utf-8"))["records"] == RECORDS["Item"]
    assert json.loads((folder / "Shipment.json").read_text(encoding="utf-8"))["records"] == RECORDS["OrderShipment"]
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["Item"] == 4 and manifest["account_id"] == "12345"
    # Archived records are included where Lightspeed hides them by default.
    assert ("Item.json", {"archived": "true", "load_relations": snapshots.ITEM_RELATIONS}, "Item") in reads.calls


def test_failed_snapshot_is_marked_and_not_complete(tmp_path):
    with pytest.raises(RuntimeError):
        snap(CannedReads(fail_on="Order"), tmp_path, "2026-10-27_090000")
    folder = tmp_path / "2026-10-27_090000"
    assert not (folder / "manifest.json").exists()
    assert "simulated network failure" in (folder / "FAILED.txt").read_text(encoding="utf-8")
    [row] = snapshots.list_snapshots(tmp_path)
    assert row["status"].startswith("failed")


def test_runner_works_in_the_background_one_at_a_time(tmp_path):
    runner = snapshots.SnapshotRunner(CannedReads(), tmp_path)
    first = runner.start()
    assert first["started"] is True
    runner.wait(10)
    assert snapshots.list_snapshots(tmp_path)[0] == {
        "snapshot": first["snapshot"], "status": "complete",
        "counts": json.loads((tmp_path / first["snapshot"] / "manifest.json").read_text())["counts"]}


def test_runner_refuses_a_second_snapshot_while_one_runs(tmp_path):
    import threading
    gate = threading.Event()

    class Slow(CannedReads):
        def get_all(self, *args, **kwargs):
            gate.wait(10)
            return super().get_all(*args, **kwargs)

    runner = snapshots.SnapshotRunner(Slow(), tmp_path)
    assert runner.start()["started"] is True
    second = runner.start()
    gate.set()
    runner.wait(10)
    assert second["started"] is False and "already being taken" in second["note"]


def test_get_all_reads_records_under_another_key(make_client, make_manager, store, token_server, clock, fake_api):
    """Shipment.json answers under "OrderShipment"; snapshots ask get_all for that key."""
    seed_tokens(store, token_server, expires_at=clock() + 3600)
    client, _ = make_client(make_manager())
    fake_api.handler = lambda request: httpx.Response(200, json={"@attributes": {"next": ""},
                                                                 "OrderShipment": [{"orderShipmentID": "1"}]})
    assert client.get_all("Shipment.json", record_key="OrderShipment") == ([{"orderShipmentID": "1"}], False)
    assert fake_api.requests[0].method == "GET" and str(fake_api.requests[0].url).startswith(BASE + "Shipment.json")


# ---- comparing ------------------------------------------------------------------------------------


def test_compare_finds_added_removed_and_changed_fields(tmp_path):
    reads = CannedReads()
    old = snap(reads, tmp_path, "2026-10-27_090000")
    reads.data["Item"][0]["customSku"] = "012912"
    reads.data["Item"][0]["ItemShops"]["ItemShop"][0]["qoh"] = "-1"
    del reads.data["Item"][3]
    reads.data["Item"].append({"itemID": "2500", "description": "Caravelli Cobalt 58R Slim", "customSku": "013471"})
    new = snap(reads, tmp_path, "2026-10-27_100000")

    result = snapshots.compare(old, new)
    items = result["record_types"]["Item"]
    assert (items["added"], items["removed"], items["changed"]) == (1, 1, 1)
    assert items["added_records"] == [{"id": "2500", "label": "Caravelli Cobalt 58R Slim"}]
    assert items["removed_records"] == [{"id": "2400", "label": "Odd SKU"}]
    [changed] = items["changed_records"]
    assert changed["id"] == "1345" and changed["changes"] == {
        "ItemShops.ItemShop.0.qoh": ["0", "-1"], "customSku": ["012911", "012912"]}
    assert result["differences"] == 3
    assert result["record_types"]["ItemMatrix"] == {"added": 0, "removed": 0, "changed": 0}


def test_identical_snapshots_have_no_differences(tmp_path):
    reads = CannedReads()
    old, new = snap(reads, tmp_path, "2026-10-27_090000"), snap(reads, tmp_path, "2026-10-27_100000")
    assert snapshots.compare(old, new)["differences"] == 0


def test_compare_report_defaults_to_the_two_newest_complete(tmp_path):
    reads = CannedReads()
    snap(reads, tmp_path, "2026-10-27_080000")
    snap(reads, tmp_path, "2026-10-27_090000")
    reads.data["ItemMatrix"][0]["description"] = "Caravelli Suit"
    snap(reads, tmp_path, "2026-10-27_100000")
    (tmp_path / "2026-10-27_110000").mkdir()  # still running: ignored

    result = snapshots.compare_report(backup_dir=tmp_path)
    assert (result["older"], result["newer"]) == ("2026-10-27_090000", "2026-10-27_100000")
    assert result["record_types"]["ItemMatrix"]["changed"] == 1
    assert [s["snapshot"] for s in result["snapshots"]][0] == "2026-10-27_110000"
    assert result["snapshots"][0]["status"].startswith("incomplete")


def test_compare_report_needs_two_snapshots(tmp_path):
    snap(CannedReads(), tmp_path, "2026-10-27_090000")
    assert "at least 2" in snapshots.compare_report(backup_dir=tmp_path)["note"]


@pytest.mark.parametrize("name", ["../state", "..\\..\\Windows", "2026-10-27_120000", "latest", ""])
def test_only_existing_snapshot_names_are_accepted(tmp_path, name):
    snap(CannedReads(), tmp_path, "2026-10-27_090000")
    with pytest.raises(ValueError):
        snapshots.compare_report(name, "2026-10-27_090000", backup_dir=tmp_path)


def test_unknown_record_type_is_refused(tmp_path):
    reads = CannedReads()
    old, new = snap(reads, tmp_path, "2026-10-27_090000"), snap(reads, tmp_path, "2026-10-27_100000")
    with pytest.raises(ValueError, match="record_type"):
        snapshots.compare(old, new, record_type="Customer")


# ---- SKU suggestions ----------------------------------------------------------------------------


def test_suggestions_follow_the_highest_sku_archived_included():
    result = InventoryReader(CannedReads()).suggest_custom_skus(3)
    assert result["highest_in_use"] == "013470"  # held by an archived item
    assert result["suggested"] == ["013471", "013472", "013473"]
    assert result["active_items_without_sku"] == 1
    assert result["skus_not_6_digit"] == ["A-17"]


# ---- the write-log report ---------------------------------------------------------------------------


def test_write_log_report_when_nothing_was_written(write_switch, write_log):
    result = write_log_report(write_switch, write_log)
    assert result["writes_enabled"] is False and result["writes_logged"] == 0
    assert "No write has ever been sent" in result["note"]


def test_write_log_report_one_row_per_write_newest_first(write_switch, write_log):
    turn_on(write_switch)
    lines = [
        {"write_id": "a", "event": "sending", "tool": "update_item_fields", "method": "PUT", "path": "/x/Item/1.json",
         "payload": {"customSku": "013473"}, "time": "t1", "confirm_code": "c1"},
        {"write_id": "a", "event": "done", "status": 200, "affected_ids": {"Item": {"itemID": "1"}}, "time": "t2"},
        {"write_id": "b", "event": "sending", "tool": "add_po_lines", "method": "POST", "path": "/x/OrderLine.json",
         "payload": {"orderID": "853"}, "time": "t3"},
        {"write_id": "b", "event": "failed", "status": 422, "error": "Validation failed", "time": "t4"},
        {"write_id": "c", "event": "sending", "tool": "receive_po_lines", "method": "PUT",
         "path": "/x/OrderLine/1.json", "payload": {"numReceived": "1"}, "time": "t5"},
    ]
    write_log.path.parent.mkdir(parents=True, exist_ok=True)
    write_log.path.write_text("\n".join(json.dumps(line) for line in lines) + "\n{\"write_id\": \"d\", \"ev",
                              encoding="utf-8")  # last line cut short, as after a crash

    result = write_log_report(write_switch, write_log)
    assert result["writes_enabled"] is True and result["writes_logged"] == 3
    c, b, a = result["writes"]
    assert c["outcome"].startswith("unknown") and c["payload"] == {"numReceived": "1"}
    assert b["outcome"] == "failed" and b["status"] == 422 and b["error"] == "Validation failed"
    assert a["outcome"] == "done" and a["affected_ids"] == {"Item": {"itemID": "1"}} and a["sent"] == "t1"
    assert write_log_report(write_switch, write_log, limit=1)["writes"] == [c]


def test_write_log_report_never_touches_the_switch(write_switch, write_log):
    write_log_report(write_switch, write_log)
    assert not write_switch.path.exists()
    assert not write_log.path.exists()


def test_reading_the_log_skips_a_damaged_line(write_log):
    write_log.path.parent.mkdir(parents=True, exist_ok=True)
    write_log.path.write_text('{"write_id": "a", "event": "sending"}\nnot json\n', encoding="utf-8")
    assert WriteLog(write_log.path).entries() == [{"write_id": "a", "event": "sending"}]
