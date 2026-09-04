"""Artifact and run-manifest persistence for the demo (Requirements 7.3, 7.5).

The :class:`StateStore` owns the ``artifacts/`` tree so every stage can persist its
outputs (baselines, bundles, A/B results, approvals, audit log, reports, outbox) and
be reviewed after a run, and so stages can run independently. A per-run
:class:`Manifest` records which stages ran, their status, and the artifacts they
produced.

This module depends only on the standard library so it stays trivially testable and
never pulls in AWS SDKs.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterator

# Subdirectories of the artifacts root, one per artifact kind.
SUBDIRS = (
    "baselines",
    "bundles",
    "ab-results",
    "approvals",
    "audit",
    "reports",
    "outbox",
    "runs",
)

AUDIT_LOG = "audit-log.jsonl"


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses / Paths / datetimes / enums to JSON-safe values.

    Lets stages persist their dataclasses (defined in ``models.py``) without the store
    needing to import them.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    return obj


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (write to a temp file, then replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class StateStore:
    """Read/write access to the ``artifacts/`` tree."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        self.ensure_dirs()

    def ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # --- path helpers -----------------------------------------------------
    def subdir(self, name: str) -> Path:
        if name not in SUBDIRS:
            raise ValueError(f"unknown artifact subdir {name!r}; expected one of {SUBDIRS}")
        return self.root / name

    def path(self, subdir: str, filename: str) -> Path:
        return self.subdir(subdir) / filename

    # --- JSON documents ---------------------------------------------------
    def write_json(self, subdir: str, name: str, obj: Any) -> Path:
        """Persist ``obj`` as ``<subdir>/<name>.json`` and return the path."""
        filename = name if name.endswith(".json") else f"{name}.json"
        dest = self.path(subdir, filename)
        _atomic_write(dest, json.dumps(to_jsonable(obj), indent=2, sort_keys=False))
        return dest

    def read_json(self, subdir: str, name: str) -> Any:
        filename = name if name.endswith(".json") else f"{name}.json"
        return json.loads(self.path(subdir, filename).read_text(encoding="utf-8"))

    def list_json(self, subdir: str) -> list[str]:
        """Return the stems (no extension) of JSON documents in ``subdir``, sorted."""
        return sorted(p.stem for p in self.subdir(subdir).glob("*.json"))

    def exists(self, subdir: str, name: str) -> bool:
        filename = name if name.endswith(".json") else f"{name}.json"
        return self.path(subdir, filename).exists()

    # --- append-only logs -------------------------------------------------
    def append_jsonl(self, subdir: str, filename: str, obj: Any) -> Path:
        """Append one JSON object as a line to ``<subdir>/<filename>``."""
        dest = self.path(subdir, filename)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(to_jsonable(obj), sort_keys=False) + "\n")
        return dest

    def read_jsonl(self, subdir: str, filename: str) -> list[Any]:
        dest = self.path(subdir, filename)
        if not dest.exists():
            return []
        return [json.loads(line) for line in dest.read_text(encoding="utf-8").splitlines() if line.strip()]

    def append_audit(self, entry: Any) -> Path:
        """Append an audit entry to the append-only audit log (Requirement 6.6)."""
        return self.append_jsonl("audit", AUDIT_LOG, entry)

    def read_audit(self) -> list[Any]:
        return self.read_jsonl("audit", AUDIT_LOG)

    # --- outbox (email fallback) -----------------------------------------
    def write_outbox(self, filename: str, content: str) -> Path:
        """Write a rendered report into the local outbox (email fallback, R2.14)."""
        dest = self.subdir("outbox") / filename
        _atomic_write(dest, content)
        return dest

    # --- run manifests ----------------------------------------------------
    def start_run(self, run_id: str | None = None) -> "Manifest":
        run_id = run_id or _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:6]
        manifest = Manifest(run_id=run_id, created_at=_utcnow_iso())
        self.save_manifest(manifest)
        return manifest

    def save_manifest(self, manifest: "Manifest") -> Path:
        manifest.updated_at = _utcnow_iso()
        return self.write_json("runs", manifest.run_id, manifest)

    def load_manifest(self, run_id: str) -> "Manifest":
        return Manifest.from_dict(self.read_json("runs", run_id))

    def list_runs(self) -> list[str]:
        return self.list_json("runs")


@dataclasses.dataclass
class StageRecord:
    name: str
    status: str = "started"  # started | ok | failed | skipped
    started_at: str = dataclasses.field(default_factory=_utcnow_iso)
    ended_at: str | None = None
    artifacts: list[str] = dataclasses.field(default_factory=list)
    note: str | None = None


@dataclasses.dataclass
class Manifest:
    """Per-run record of stages and the artifacts they produced (Requirements 7.3, 7.5)."""

    run_id: str
    created_at: str
    updated_at: str | None = None
    stages: list[StageRecord] = dataclasses.field(default_factory=list)

    def stage(self, name: str) -> StageRecord:
        """Return the existing stage record for ``name`` or create/append a new one."""
        for s in self.stages:
            if s.name == name:
                return s
        record = StageRecord(name=name)
        self.stages.append(record)
        return record

    def record(
        self,
        name: str,
        status: str,
        artifacts: list[str] | None = None,
        note: str | None = None,
    ) -> StageRecord:
        rec = self.stage(name)
        rec.status = status
        rec.ended_at = _utcnow_iso()
        if artifacts:
            rec.artifacts.extend(str(a) for a in artifacts)
        if note is not None:
            rec.note = note
        return rec

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        stages = [StageRecord(**s) for s in data.get("stages", [])]
        return cls(
            run_id=data["run_id"],
            created_at=data["created_at"],
            updated_at=data.get("updated_at"),
            stages=stages,
        )

    def iter_stages(self) -> Iterator[StageRecord]:
        return iter(self.stages)
