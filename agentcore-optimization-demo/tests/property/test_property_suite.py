"""Consolidated property-based test suite (task 17.2).

One place that exercises every design Correctness Property (P1-P10) against
stubbed/fake AWS clients (never real AWS), each at >=100 iterations. The behavioral
detail of each property is also covered in the per-module test files; this suite is the
single runner that guarantees all ten are enforced together.
"""

import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agentcore_demo.agentcore_client import PromotionDecision, assign_variant
from agentcore_demo.agent import patient_support_agent as agent
from agentcore_demo.evaluation.evaluators import ToolCallEvaluator, TrajectoryEvaluator
from agentcore_demo.evaluation.offline import run_offline_evaluation
from agentcore_demo.models import (
    ABTestResult,
    ApprovalStatus,
    BundleConfig,
    BundleVersion,
    ConversationCase,
    EvalMetric,
    GroundTruth,
    SessionResult,
)
from agentcore_demo.optimization.abtest import compute_ab_result
from agentcore_demo.optimization.bundles import BundleManager
from agentcore_demo.optimization.promotion import PromotionGate
from agentcore_demo.reporting.emailer import deliver_report
from agentcore_demo.config import EmailConfig
from agentcore_demo.state import StateStore

HUNDRED = settings(max_examples=100)

_tool_lists = st.lists(st.sampled_from(list("abcd")), max_size=6)
_score_samples = st.lists(st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False), min_size=2, max_size=15)


def _store(tmp):
    return StateStore(Path(tmp) / "artifacts")


def _significant_result(sig, winner="treatment"):
    return ABTestResult(
        "ab-1",
        {"control": {"g": EvalMetric(mean=0.7)}, "treatment": {"g": EvalMetric(mean=0.85, pct_change=21.0, p_value=0.01, significant=sig)}},
        winner=winner,
        significant=sig,
    )


def _decision():
    return PromotionDecision(strategy="config_bundle", bundle_id="b1", agent_arn="arn", config=BundleConfig(system_prompt="w"))


class _PromoClient:
    def __init__(self):
        self.promote_calls = 0

    def stop_ab_test(self, handle):
        pass

    def promote_variant(self, decision):
        self.promote_calls += 1


class _BundleClient:
    def __init__(self):
        self.n = 0

    def create_bundle_version(self, bundle_name, agent_arn, config, commit_message, description=""):
        self.n += 1
        return BundleVersion("b1", f"v{self.n}", [], commit_message, config, bundle_arn="arn:b1")

    def update_bundle_version(self, bundle_id, agent_arn, config, parent_version_ids, commit_message):
        self.n += 1
        return BundleVersion(bundle_id, f"v{self.n}", list(parent_version_ids), commit_message, config, bundle_arn="arn:b1")


# --- P1: Promotion safety --------------------------------------------------
@HUNDRED
@given(sig=st.booleans())
def test_p1_promotion_safety(sig):
    with tempfile.TemporaryDirectory() as d:
        client = _PromoClient()
        gate = PromotionGate(client, _store(d))
        gate.evaluate_for_promotion(_significant_result(sig), agent_ref="a")
        assert client.promote_calls == 0  # never promote during detection / while pending


# --- P2: Approval state machine -------------------------------------------
@HUNDRED
@given(approve_first=st.booleans())
def test_p2_state_machine(approve_first):
    from agentcore_demo.optimization.promotion import InvalidTransition

    with tempfile.TemporaryDirectory() as d:
        gate = PromotionGate(_PromoClient(), _store(d))
        req = gate.evaluate_for_promotion(_significant_result(True), agent_ref="a")
        if approve_first:
            out = gate.approve(req.request_id, "cli", _decision())
            assert out.status == ApprovalStatus.PROMOTED
            with pytest.raises(InvalidTransition):
                gate.reject(req.request_id, "cli")
        else:
            out = gate.reject(req.request_id, "cli")
            assert out.status == ApprovalStatus.REJECTED
            with pytest.raises(InvalidTransition):
                gate.approve(req.request_id, "cli", _decision())


# --- P3: Audit completeness -----------------------------------------------
@HUNDRED
@given(approve=st.booleans())
def test_p3_audit_completeness(approve):
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        gate = PromotionGate(_PromoClient(), store)
        req = gate.evaluate_for_promotion(_significant_result(True), agent_ref="a")
        if approve:
            gate.approve(req.request_id, "cli", _decision())
        else:
            gate.reject(req.request_id, "cli")
        assert len(store.read_audit()) == 1


# --- P4: Significance gating -----------------------------------------------
@HUNDRED
@given(control=_score_samples, treatment=_score_samples)
def test_p4_significance_gating(control, treatment):
    result = compute_ab_result("ab", {"g": control}, {"g": treatment})
    assert (result.winner is not None) == result.significant
    if result.significant:
        assert result.per_variant["treatment"]["g"].p_value < 0.05


# --- P5: Offline per-case isolation ---------------------------------------
@HUNDRED
@given(flags=st.lists(st.booleans(), min_size=1, max_size=12))
def test_p5_offline_isolation(flags):
    cases = [ConversationCase(f"c{i}", ["hi"], GroundTruth(expected_trajectory=["a"], assertions=["x"])) for i in range(len(flags))]

    def runner(case):
        if flags[int(case.case_id[1:])]:
            raise RuntimeError("boom")
        return SessionResult(f"s{case.case_id}", "ok", ["a"])

    result = run_offline_evaluation("b", cases, runner)
    assert result.baseline.case_count == len(flags)
    assert len(result.case_results) == len(flags)


# --- P6: Trajectory & tool-call evaluator soundness -----------------------
@HUNDRED
@given(expected=_tool_lists, actual=_tool_lists)
def test_p6_evaluator_soundness(expected, actual):
    gt = GroundTruth(expected_trajectory=list(expected))
    session = SessionResult("s", "resp", list(actual))
    assert TrajectoryEvaluator().evaluate(gt, session).passed == (list(expected) == list(actual))
    assert ToolCallEvaluator().evaluate(gt, session).passed == set(expected).issubset(set(actual))


# --- P7: Tool robustness ---------------------------------------------------
@HUNDRED
@given(s=st.text(max_size=20), flag=st.booleans())
def test_p7_tool_robustness(s, flag):
    for name, fn in agent.TOOL_IMPLS.items():
        if name in ("get_patient_profile", "get_medications", "lookup_health_policy"):
            result = fn(s)
        elif name == "find_provider":
            result = fn(s, flag)
        elif name == "schedule_appointment":
            result = fn(s, s, s)
        else:
            result = fn(s, s)
        assert isinstance(result, dict)


# --- P8: Bundle immutability & lineage ------------------------------------
@HUNDRED
@given(prompts=st.lists(st.text(min_size=1, max_size=10), min_size=1, max_size=8))
def test_p8_bundle_lineage(prompts):
    mgr = BundleManager(_BundleClient(), "arn:agent")
    first = mgr.create("B", BundleConfig(system_prompt=prompts[0]), "init")
    assert first.parent_version_ids == []
    prev = first.version_id
    for p in prompts[1:]:
        before = mgr.versions("b1")
        bv = mgr.update("b1", BundleConfig(system_prompt=p), "u")
        assert bv.parent_version_ids == [prev]
        assert mgr.versions("b1")[: len(before)] == before  # prior versions unmutated
        prev = bv.version_id


# --- P9: Report delivery best-effort --------------------------------------
@HUNDRED
@given(mechanism=st.sampled_from(["ses", "smtp", "outbox"]), raises=st.booleans())
def test_p9_report_delivery_best_effort(mechanism, raises):
    from agentcore_demo.models import EvaluationReport

    report = EvaluationReport("b", "2026-01-01T00:00:00Z", {"g": 0.8}, 5, [])

    def sender(*_a):
        if raises:
            raise RuntimeError("fail")

    with tempfile.TemporaryDirectory() as d:
        email = EmailConfig(delivery=mechanism, target=None if mechanism == "outbox" else "t@example.com", sender="s@example.com", smtp_host="h")
        result = deliver_report(report, email, _store(d), ses_sender=sender, smtp_sender=sender)
        if result.delivered:
            assert result.error is None
        else:
            assert result.outbox_path and Path(result.outbox_path).exists()


# --- P10: Session stickiness ----------------------------------------------
@HUNDRED
@given(sid=st.text(min_size=1, max_size=40))
def test_p10_session_stickiness(sid):
    variants = [("C", 50), ("T1", 50)]
    first = assign_variant(sid, variants)
    assert all(assign_variant(sid, variants) == first for _ in range(5))
