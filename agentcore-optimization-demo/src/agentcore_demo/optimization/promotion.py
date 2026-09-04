"""Promotion gate with human approval — the core capability the AWS sample lacks.

Turns the sample's manual "Decision Framework" into an automated, approval-gated
workflow:

    RUNNING --significant winner--> PENDING_APPROVAL
    PENDING_APPROVAL --approve--> APPROVED --> PROMOTING --> PROMOTED
    PENDING_APPROVAL --reject---> REJECTED           (baseline unchanged)
    PENDING_APPROVAL --(no decision)--> stays PENDING (baseline keeps serving)

Guarantees (design Correctness Properties):
* P1 Promotion safety: the gate never promotes (routes 100% traffic) while a request
  is PENDING_APPROVAL; promotion happens only inside ``approve``.
* P2 State machine: only the transitions above are legal; a rejected/undecided request
  never changes the baseline.
* P3 Audit completeness: every approve/reject writes exactly one audit entry.
* P4 Significance gating: a pending approval is created iff the A/B result is
  statistically significant and has a winner.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8
"""

from __future__ import annotations

import datetime as _dt
import uuid

from ..agentcore_client import ABTestHandle, PromotionDecision
from ..models import ABTestResult, ApprovalRequest, ApprovalStatus, AuditEntry, Decision
from ..state import StateStore


class InvalidTransition(Exception):
    """Raised when an approve/reject is attempted from a non-pending state (P2)."""


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class ApprovalStore:
    """Persistence for approval requests under ``artifacts/approvals/``."""

    def __init__(self, store: StateStore):
        self.store = store

    def save(self, req: ApprovalRequest) -> None:
        self.store.write_json("approvals", req.request_id, req)

    def get(self, request_id: str) -> ApprovalRequest:
        return ApprovalRequest.from_dict(self.store.read_json("approvals", request_id))

    def exists(self, request_id: str) -> bool:
        return self.store.exists("approvals", request_id)

    def ids(self) -> list[str]:
        return self.store.list_json("approvals")

    def pending(self) -> list[ApprovalRequest]:
        return [r for r in (self.get(i) for i in self.ids()) if r.status == ApprovalStatus.PENDING_APPROVAL]


class PromotionGate:
    """Detects significant winners, gates rollout behind human approval, and audits."""

    def __init__(self, client, store: StateStore):
        self.client = client
        self.store = store
        self.approvals = ApprovalStore(store)

    # --- detection (R6.1, R6.2, R6.3, P4) --------------------------------
    def evaluate_for_promotion(self, ab_result: ABTestResult, *, agent_ref: str, request_id: str | None = None) -> ApprovalRequest | None:
        """Create a PENDING approval iff the result is significant and has a winner.

        Never promotes here — rollout is blocked until a human approves (P1, R6.3).
        """
        if not (ab_result.significant and ab_result.winner):
            return None
        rid = request_id or f"appr-{uuid.uuid4().hex[:10]}"
        req = ApprovalRequest(
            request_id=rid,
            ab_test_id=ab_result.ab_test_id,
            winning_variant=ab_result.winner,
            metrics=dict(ab_result.per_variant.get(ab_result.winner, {})),
            agent_ref=agent_ref,
            status=ApprovalStatus.PENDING_APPROVAL,
            created_at=_utcnow_iso(),
        )
        self.approvals.save(req)
        return req

    # --- decisions (R6.4, R6.5, R6.6, R6.7, P1, P2, P3) ------------------
    def approve(self, request_id: str, decider: str, promotion_decision: PromotionDecision) -> ApprovalRequest:
        """Approve a pending promotion: stop the test, promote the winner, audit."""
        req = self.approvals.get(request_id)
        if req.status != ApprovalStatus.PENDING_APPROVAL:
            raise InvalidTransition(f"cannot approve request in state {req.status.value}")

        req.status = ApprovalStatus.APPROVED
        req.decided_at = _utcnow_iso()
        self.approvals.save(req)

        req.status = ApprovalStatus.PROMOTING
        self.approvals.save(req)

        # Roll out: stop the A/B test, then promote the winning variant (R6.4).
        self.client.stop_ab_test(ABTestHandle(ab_test_id=req.ab_test_id, name=req.ab_test_id))
        self.client.promote_variant(promotion_decision)

        req.status = ApprovalStatus.PROMOTED
        self.approvals.save(req)

        self._audit(req, decider, Decision.APPROVED, f"promoted winning variant '{req.winning_variant}'")
        return req

    def reject(self, request_id: str, decider: str) -> ApprovalRequest:
        """Reject a pending promotion: stop the test, leave the baseline unchanged, audit."""
        req = self.approvals.get(request_id)
        if req.status != ApprovalStatus.PENDING_APPROVAL:
            raise InvalidTransition(f"cannot reject request in state {req.status.value}")

        req.status = ApprovalStatus.REJECTED
        req.decided_at = _utcnow_iso()
        self.approvals.save(req)

        self.client.stop_ab_test(ABTestHandle(ab_test_id=req.ab_test_id, name=req.ab_test_id))
        self._audit(req, decider, Decision.REJECTED, "rejected; baseline unchanged")
        return req

    # --- audit (R6.6, P3) ------------------------------------------------
    def _audit(self, req: ApprovalRequest, decider: str, decision: Decision, outcome: str) -> None:
        entry = AuditEntry(
            timestamp=_utcnow_iso(),
            request_id=req.request_id,
            decider=decider,
            decision=decision,
            metrics_at_decision=dict(req.metrics),
            outcome=outcome,
        )
        self.store.append_audit(entry)

    # --- queries ---------------------------------------------------------
    def pending(self) -> list[ApprovalRequest]:
        return self.approvals.pending()

    def get(self, request_id: str) -> ApprovalRequest:
        return self.approvals.get(request_id)
