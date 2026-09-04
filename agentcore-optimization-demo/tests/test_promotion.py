"""Promotion-gate tests (tasks 15.4-15.7).

Property 1 (promotion safety), Property 2 (state machine), Property 3 (audit
completeness), and Property 4 (significance-gated approval creation).
Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.8
"""

import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agentcore_demo.agentcore_client import PromotionDecision
from agentcore_demo.models import ABTestResult, ApprovalStatus, BundleConfig, Decision, EvalMetric
from agentcore_demo.optimization.promotion import InvalidTransition, PromotionGate
from agentcore_demo.state import StateStore


class FakePromoClient:
    def __init__(self):
        self.stop_calls = 0
        self.promote_calls = 0

    def stop_ab_test(self, handle):
        self.stop_calls += 1

    def promote_variant(self, decision):
        self.promote_calls += 1


def _store(tmp) -> StateStore:
    return StateStore(Path(tmp) / "artifacts")


def _result(significant: bool, winner: str | None = "treatment") -> ABTestResult:
    per_variant = {
        "control": {"g": EvalMetric(mean=0.70)},
        "treatment": {"g": EvalMetric(mean=0.85, abs_change=0.15, pct_change=21.4, p_value=0.01, significant=significant)},
    }
    return ABTestResult(ab_test_id="ab-1", per_variant=per_variant, winner=winner, significant=significant)


def _decision() -> PromotionDecision:
    return PromotionDecision(strategy="config_bundle", bundle_id="b1", agent_arn="arn:agent", config=BundleConfig(system_prompt="winner"))


# ==========================================================================
# Property 4: Significance gating (approval creation)
# Validates: Requirements 6.1, 5.6
# ==========================================================================
@given(significant=st.booleans(), has_winner=st.booleans())
def test_property_pending_created_iff_significant_and_winner(significant, has_winner):
    with tempfile.TemporaryDirectory() as d:
        gate = PromotionGate(FakePromoClient(), _store(d))
        winner = "treatment" if has_winner else None
        req = gate.evaluate_for_promotion(_result(significant, winner), agent_ref="PatientSupport")
        created = req is not None
        assert created == (significant and winner is not None)
        # No approval is persisted when not created.
        assert bool(gate.approvals.ids()) == created


# ==========================================================================
# Property 1: Promotion safety
# Validates: Requirements 6.3
# ==========================================================================
@given(significant=st.booleans())
def test_property_no_promotion_while_pending(significant):
    with tempfile.TemporaryDirectory() as d:
        client = FakePromoClient()
        gate = PromotionGate(client, _store(d))
        req = gate.evaluate_for_promotion(_result(significant), agent_ref="a")
        # Detection never promotes; a created request is PENDING and traffic is not routed.
        assert client.promote_calls == 0
        if req is not None:
            assert req.status == ApprovalStatus.PENDING_APPROVAL
        assert client.promote_calls == 0


def test_promotion_only_happens_on_approve():
    with tempfile.TemporaryDirectory() as d:
        client = FakePromoClient()
        gate = PromotionGate(client, _store(d))
        req = gate.evaluate_for_promotion(_result(True), agent_ref="a")
        assert client.promote_calls == 0  # pending -> not promoted
        gate.approve(req.request_id, "cli", _decision())
        assert client.promote_calls == 1  # only after approval


# ==========================================================================
# Property 2: Approval state machine
# Validates: Requirements 6.4, 6.5, 6.8
# ==========================================================================
def test_approve_transitions_to_promoted():
    with tempfile.TemporaryDirectory() as d:
        client = FakePromoClient()
        gate = PromotionGate(client, _store(d))
        req = gate.evaluate_for_promotion(_result(True), agent_ref="a")
        out = gate.approve(req.request_id, "cli", _decision())
        assert out.status == ApprovalStatus.PROMOTED
        assert client.stop_calls == 1 and client.promote_calls == 1
        assert out.decided_at is not None


def test_reject_leaves_baseline_unchanged():
    with tempfile.TemporaryDirectory() as d:
        client = FakePromoClient()
        gate = PromotionGate(client, _store(d))
        req = gate.evaluate_for_promotion(_result(True), agent_ref="a")
        out = gate.reject(req.request_id, "cli")
        assert out.status == ApprovalStatus.REJECTED
        assert client.stop_calls == 1
        assert client.promote_calls == 0  # never promoted -> baseline unchanged


def test_cannot_decide_twice():
    with tempfile.TemporaryDirectory() as d:
        gate = PromotionGate(FakePromoClient(), _store(d))
        req = gate.evaluate_for_promotion(_result(True), agent_ref="a")
        gate.approve(req.request_id, "cli", _decision())
        with pytest.raises(InvalidTransition):
            gate.approve(req.request_id, "cli", _decision())
        with pytest.raises(InvalidTransition):
            gate.reject(req.request_id, "cli")


def test_cannot_approve_after_reject():
    with tempfile.TemporaryDirectory() as d:
        gate = PromotionGate(FakePromoClient(), _store(d))
        req = gate.evaluate_for_promotion(_result(True), agent_ref="a")
        gate.reject(req.request_id, "cli")
        with pytest.raises(InvalidTransition):
            gate.approve(req.request_id, "cli", _decision())


def test_undecided_request_stays_pending():
    with tempfile.TemporaryDirectory() as d:
        gate = PromotionGate(FakePromoClient(), _store(d))
        req = gate.evaluate_for_promotion(_result(True), agent_ref="a")
        # No decision taken -> still pending and listed.
        pending = gate.pending()
        assert [p.request_id for p in pending] == [req.request_id]
        assert gate.get(req.request_id).status == ApprovalStatus.PENDING_APPROVAL


# ==========================================================================
# Property 3: Audit completeness
# Validates: Requirements 6.6
# ==========================================================================
def test_approve_writes_exactly_one_audit_entry():
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        gate = PromotionGate(FakePromoClient(), store)
        req = gate.evaluate_for_promotion(_result(True), agent_ref="a")
        gate.approve(req.request_id, "alice", _decision())
        audit = store.read_audit()
        assert len(audit) == 1
        entry = audit[0]
        assert entry["decision"] == "APPROVED"
        assert entry["decider"] == "alice"
        assert entry["request_id"] == req.request_id
        assert entry["metrics_at_decision"]["g"]["mean"] == 0.85  # metrics captured at decision


def test_reject_writes_exactly_one_audit_entry():
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        gate = PromotionGate(FakePromoClient(), store)
        req = gate.evaluate_for_promotion(_result(True), agent_ref="a")
        gate.reject(req.request_id, "bob")
        audit = store.read_audit()
        assert len(audit) == 1 and audit[0]["decision"] == "REJECTED" and audit[0]["decider"] == "bob"


def test_two_decisions_produce_two_audit_entries():
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        gate = PromotionGate(FakePromoClient(), store)
        r1 = gate.evaluate_for_promotion(_result(True), agent_ref="a", request_id="appr-1")
        r2 = gate.evaluate_for_promotion(_result(True), agent_ref="a", request_id="appr-2")
        gate.approve(r1.request_id, "cli", _decision())
        gate.reject(r2.request_id, "cli")
        assert len(store.read_audit()) == 2


# ==========================================================================
# Persistence
# ==========================================================================
def test_pending_request_is_persisted_and_reloadable():
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        gate = PromotionGate(FakePromoClient(), store)
        req = gate.evaluate_for_promotion(_result(True), agent_ref="PatientSupport", request_id="appr-x")
        # Reload via a fresh gate instance (simulates a separate CLI invocation).
        gate2 = PromotionGate(FakePromoClient(), store)
        loaded = gate2.get("appr-x")
        assert loaded.status == ApprovalStatus.PENDING_APPROVAL
        assert loaded.winning_variant == "treatment"
        assert loaded.metrics["g"].significant is True
