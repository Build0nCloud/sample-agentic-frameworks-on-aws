"""Orchestrator engine tests (task 16.3).

Covers stage ordering, halt-on-failure, subset selection, and the before/after
comparison.
Requirements: 7.1, 7.4, 7.6
"""

from pathlib import Path

import pytest

from agentcore_demo.models import Baseline
from agentcore_demo.orchestrator import (
    Orchestrator,
    OrchestratorError,
    Stage,
    StageOutcome,
    compare_baselines,
    format_before_after,
    make_context,
    maybe_emit_before_after,
)
from agentcore_demo.state import StateStore


def _ctx(tmp, emit=None):
    store = StateStore(Path(tmp) / "artifacts")
    return make_context(config=None, store=store, emit=emit or (lambda _m: None))


def _stage(name, order, *, outcome=None, raises=None):
    def run(context):
        order.append(name)
        if raises:
            raise raises
        return outcome or StageOutcome(status="ok", note=f"{name} done")

    return Stage(name=name, description=name, run=run)


def test_runs_stages_in_order(tmp_path):
    order = []
    stages = [_stage("a", order), _stage("b", order), _stage("c", order)]
    ctx = _ctx(tmp_path)
    manifest = Orchestrator(stages).run(ctx)
    assert order == ["a", "b", "c"]
    assert [s.status for s in manifest.stages] == ["ok", "ok", "ok"]


def test_halts_on_exception_and_does_not_proceed(tmp_path):
    order = []
    stages = [_stage("a", order), _stage("b", order, raises=RuntimeError("boom")), _stage("c", order)]
    ctx = _ctx(tmp_path)
    with pytest.raises(OrchestratorError) as exc:
        Orchestrator(stages).run(ctx)
    assert exc.value.stage == "b"
    assert order == ["a", "b"]  # c never ran (R7.6)
    statuses = {s.name: s.status for s in ctx.manifest.stages}
    assert statuses["a"] == "ok" and statuses["b"] == "failed"


def test_halts_on_failed_outcome(tmp_path):
    order = []
    stages = [_stage("a", order), _stage("b", order, outcome=StageOutcome(status="failed", note="bad")), _stage("c", order)]
    with pytest.raises(OrchestratorError):
        Orchestrator(stages).run(_ctx(tmp_path))
    assert order == ["a", "b"]


def test_only_runs_selected_subset_in_canonical_order(tmp_path):
    order = []
    stages = [_stage("a", order), _stage("b", order), _stage("c", order)]
    Orchestrator(stages).run(_ctx(tmp_path), only=["c", "a"])  # order preserved as a, c
    assert order == ["a", "c"]


def test_unknown_stage_raises(tmp_path):
    with pytest.raises(KeyError):
        Orchestrator([_stage("a", [])]).run(_ctx(tmp_path), only=["nope"])


def test_emits_progress(tmp_path):
    msgs = []
    Orchestrator([_stage("a", [])]).run(_ctx(tmp_path, emit=msgs.append))
    joined = "\n".join(msgs)
    assert "a" in joined and any("\u2713" in m for m in msgs)  # check-mark on success


# --------------------------------------------------------------------------
# Before/after comparison (R7.4)
# --------------------------------------------------------------------------
def _baseline(name, scores):
    return Baseline(name=name, config_ref="ref", scores=scores, case_count=10, created_at="2026-01-01T00:00:00Z")


def test_compare_baselines_deltas():
    before = _baseline("pre", {"g": 0.6, "h": 0.7})
    after = _baseline("post", {"g": 0.8, "h": 0.65})
    comp = compare_baselines(before, after)
    assert comp["g"] == {"before": 0.6, "after": 0.8, "delta": pytest.approx(0.2)}
    assert comp["h"]["delta"] == pytest.approx(-0.05)


def test_format_before_after_contains_values():
    text = format_before_after(_baseline("pre", {"g": 0.6}), _baseline("post", {"g": 0.8}))
    assert "Before/after" in text and "g" in text and "0.600" in text and "0.800" in text


def test_maybe_emit_before_after_emits_only_when_both_present(tmp_path):
    msgs = []
    ctx = _ctx(tmp_path, emit=msgs.append)
    maybe_emit_before_after(ctx)  # nothing in bag
    assert msgs == []
    ctx.bag["original_baseline"] = _baseline("pre", {"g": 0.6})
    ctx.bag["candidate_baseline"] = _baseline("post", {"g": 0.8})
    maybe_emit_before_after(ctx)
    assert any("Before/after" in m for m in msgs)
