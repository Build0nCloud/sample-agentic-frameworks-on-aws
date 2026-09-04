"""Unit tests for agentcore_demo.state (task 2.3).

Covers artifact round-trip read/write, JSON-ability of dataclasses/paths/datetimes,
append-only audit log, outbox writes, and run-manifest lifecycle.
Requirements: 7.3, 7.5
"""

import dataclasses
import datetime as dt
import enum
from pathlib import Path

from agentcore_demo.state import Manifest, StateStore, to_jsonable


def test_ensure_dirs_creates_subdirs(tmp_path):
    store = StateStore(tmp_path / "artifacts")
    for sub in ("baselines", "bundles", "ab-results", "approvals", "audit", "reports", "outbox", "runs"):
        assert (store.root / sub).is_dir()


def test_json_roundtrip(tmp_path):
    store = StateStore(tmp_path / "artifacts")
    store.write_json("baselines", "baseline_v1", {"name": "baseline_v1", "scores": {"g": 0.8}})
    assert store.exists("baselines", "baseline_v1")
    loaded = store.read_json("baselines", "baseline_v1")
    assert loaded["scores"]["g"] == 0.8
    assert store.list_json("baselines") == ["baseline_v1"]


class _Color(enum.Enum):
    RED = "red"


@dataclasses.dataclass
class _Sample:
    name: str
    when: dt.datetime
    where: Path
    color: _Color


def test_to_jsonable_handles_dataclass_path_datetime_enum():
    sample = _Sample("x", dt.datetime(2026, 1, 1, 12, 0, 0), Path("/tmp/a"), _Color.RED)
    out = to_jsonable(sample)
    assert out["name"] == "x"
    assert out["when"] == "2026-01-01T12:00:00"
    assert out["where"] == "/tmp/a"
    assert out["color"] == "red"


def test_write_json_accepts_dataclass(tmp_path):
    store = StateStore(tmp_path / "artifacts")
    store.write_json("reports", "r1", _Sample("x", dt.datetime(2026, 1, 1), Path("/tmp/a"), _Color.RED))
    assert store.read_json("reports", "r1")["color"] == "red"


def test_audit_append_and_read(tmp_path):
    store = StateStore(tmp_path / "artifacts")
    store.append_audit({"decision": "APPROVED", "request_id": "r1"})
    store.append_audit({"decision": "REJECTED", "request_id": "r2"})
    entries = store.read_audit()
    assert [e["decision"] for e in entries] == ["APPROVED", "REJECTED"]


def test_outbox_write(tmp_path):
    store = StateStore(tmp_path / "artifacts")
    dest = store.write_outbox("report.html", "<h1>hi</h1>")
    assert dest.read_text() == "<h1>hi</h1>"
    assert dest.parent.name == "outbox"


def test_manifest_lifecycle(tmp_path):
    store = StateStore(tmp_path / "artifacts")
    manifest = store.start_run("run-123")
    manifest.record("offline-baseline", "ok", artifacts=["baselines/baseline_v1.json"])
    manifest.record("ab-test", "failed", note="timed out")
    store.save_manifest(manifest)

    reloaded = store.load_manifest("run-123")
    assert reloaded.run_id == "run-123"
    names = {s.name: s for s in reloaded.iter_stages()}
    assert names["offline-baseline"].status == "ok"
    assert names["offline-baseline"].artifacts == ["baselines/baseline_v1.json"]
    assert names["ab-test"].status == "failed"
    assert names["ab-test"].note == "timed out"
    assert "run-123" in store.list_runs()


def test_manifest_stage_is_idempotent(tmp_path):
    store = StateStore(tmp_path / "artifacts")
    m = store.start_run("run-x")
    m.record("s1", "started")
    m.record("s1", "ok")
    assert len([s for s in m.iter_stages() if s.name == "s1"]) == 1
