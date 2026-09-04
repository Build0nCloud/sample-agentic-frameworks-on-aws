"""Shared data models for the AgentCore optimization demo.

These dataclasses are the common vocabulary passed between the agent, the AgentCore
client layer, evaluation, optimization, the promotion gate, and reporting. They mirror
the "Data Models" section of the design and are JSON-serialized by
``state.to_jsonable`` (which handles dataclasses, enums, Paths, and datetimes).

Requirements: 2.6, 2.10, 4.4, 5.6, 6.2, 6.6
"""

from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Enums for the promotion workflow (used by ApprovalRequest / AuditEntry).
# String-valued so they serialize to the exact tokens used in the design.
# ---------------------------------------------------------------------------
class ApprovalStatus(str, enum.Enum):
    """Lifecycle states of a promotion approval request (Requirement 6)."""

    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    PROMOTING = "PROMOTING"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"


class Decision(str, enum.Enum):
    """A human decision on a pending promotion (Requirement 6.6)."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


DeliveryMechanism = str  # one of "ses" | "smtp" | "outbox" (validated in config)


# ---------------------------------------------------------------------------
# Evaluation datasets (offline, multi-turn) — Requirements 2.1, 2.2, 9.6
# ---------------------------------------------------------------------------
@dataclass
class GroundTruth:
    """Optional expected outcome for an evaluation case."""

    expected_response: str | None = None
    assertions: list[str] = field(default_factory=list)
    expected_trajectory: list[str] | None = None  # ordered tool names (R9.6)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "GroundTruth | None":
        if data is None:
            return None
        return cls(
            expected_response=data.get("expected_response"),
            assertions=list(data.get("assertions", []) or []),
            expected_trajectory=(list(data["expected_trajectory"]) if data.get("expected_trajectory") is not None else None),
        )


@dataclass
class ConversationCase:
    """A single (possibly multi-turn) offline evaluation case."""

    case_id: str
    turns: list[str]  # ordered user turns -> one session
    ground_truth: GroundTruth | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationCase":
        return cls(
            case_id=str(data["case_id"]),
            turns=list(data["turns"]),
            ground_truth=GroundTruth.from_dict(data.get("ground_truth")),
        )


# ---------------------------------------------------------------------------
# Agent invocation result — Requirement 9 (ordered tool_calls for trajectory eval)
# ---------------------------------------------------------------------------
@dataclass
class SessionResult:
    """Outcome of running a (multi-turn) session against the agent."""

    session_id: str
    final_response: str
    tool_calls: list[str] = field(default_factory=list)  # ordered tool names invoked


# ---------------------------------------------------------------------------
# Baselines — Requirement 2.6
# ---------------------------------------------------------------------------
@dataclass
class Baseline:
    """A named, stored evaluation result used for pre/post comparison."""

    name: str
    config_ref: str  # bundle version id or agent version
    scores: dict[str, float]  # evaluator -> mean
    case_count: int
    created_at: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Baseline":
        return cls(
            name=data["name"],
            config_ref=data["config_ref"],
            scores=dict(data.get("scores", {})),
            case_count=int(data.get("case_count", 0)),
            created_at=data["created_at"],
        )


# ---------------------------------------------------------------------------
# Configuration bundles — Requirement 4.4
# ---------------------------------------------------------------------------
@dataclass
class BundleConfig:
    """The overridable agent configuration carried by a bundle version."""

    system_prompt: str
    model_id: str | None = None
    tool_descriptions: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BundleConfig":
        return cls(
            system_prompt=data["system_prompt"],
            model_id=data.get("model_id"),
            tool_descriptions=dict(data.get("tool_descriptions", {}) or {}),
        )


@dataclass
class BundleVersion:
    """An immutable, versioned snapshot of agent configuration (Requirement 4.4)."""

    bundle_id: str
    version_id: str  # immutable snapshot id
    parent_version_ids: list[str] = field(default_factory=list)
    commit_message: str = ""
    config: BundleConfig | None = None
    bundle_arn: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BundleVersion":
        cfg = data.get("config")
        return cls(
            bundle_id=data["bundle_id"],
            version_id=data["version_id"],
            parent_version_ids=list(data.get("parent_version_ids", []) or []),
            commit_message=data.get("commit_message", ""),
            config=BundleConfig.from_dict(cfg) if cfg else None,
            bundle_arn=data.get("bundle_arn"),
        )


# ---------------------------------------------------------------------------
# A/B testing metrics — Requirement 5.6
# ---------------------------------------------------------------------------
@dataclass
class EvalMetric:
    """Per-evaluator statistics for one variant in an A/B test."""

    mean: float
    abs_change: float = 0.0
    pct_change: float = 0.0
    p_value: float = 1.0
    ci_low: float = 0.0
    ci_high: float = 0.0
    significant: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvalMetric":
        return cls(
            mean=float(data["mean"]),
            abs_change=float(data.get("abs_change", 0.0)),
            pct_change=float(data.get("pct_change", 0.0)),
            p_value=float(data.get("p_value", 1.0)),
            ci_low=float(data.get("ci_low", 0.0)),
            ci_high=float(data.get("ci_high", 0.0)),
            significant=bool(data.get("significant", False)),
        )


def _metrics_map_from_dict(data: dict[str, Any]) -> dict[str, EvalMetric]:
    return {name: EvalMetric.from_dict(m) for name, m in (data or {}).items()}


@dataclass
class ABTestResult:
    """Aggregated A/B test result across variants and evaluators (Requirement 5.6)."""

    ab_test_id: str
    per_variant: dict[str, dict[str, EvalMetric]] = field(default_factory=dict)  # variant -> evaluator -> metric
    winner: str | None = None
    significant: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ABTestResult":
        per_variant = {
            variant: _metrics_map_from_dict(metrics)
            for variant, metrics in (data.get("per_variant", {}) or {}).items()
        }
        return cls(
            ab_test_id=data["ab_test_id"],
            per_variant=per_variant,
            winner=data.get("winner"),
            significant=bool(data.get("significant", False)),
        )


# ---------------------------------------------------------------------------
# Promotion approval + audit — Requirements 6.2, 6.6
# ---------------------------------------------------------------------------
@dataclass
class ApprovalRequest:
    """A pending/decided promotion approval (Requirement 6.2)."""

    request_id: str
    ab_test_id: str
    winning_variant: str
    metrics: dict[str, EvalMetric]
    agent_ref: str
    status: ApprovalStatus = ApprovalStatus.PENDING_APPROVAL
    created_at: str = ""
    decided_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalRequest":
        return cls(
            request_id=data["request_id"],
            ab_test_id=data["ab_test_id"],
            winning_variant=data["winning_variant"],
            metrics=_metrics_map_from_dict(data.get("metrics", {})),
            agent_ref=data["agent_ref"],
            status=ApprovalStatus(data.get("status", ApprovalStatus.PENDING_APPROVAL.value)),
            created_at=data.get("created_at", ""),
            decided_at=data.get("decided_at"),
        )


@dataclass
class AuditEntry:
    """An append-only record of a promotion decision (Requirement 6.6)."""

    timestamp: str
    request_id: str
    decider: str
    decision: Decision
    metrics_at_decision: dict[str, EvalMetric] = field(default_factory=dict)
    outcome: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditEntry":
        return cls(
            timestamp=data["timestamp"],
            request_id=data["request_id"],
            decider=data["decider"],
            decision=Decision(data["decision"]),
            metrics_at_decision=_metrics_map_from_dict(data.get("metrics_at_decision", {})),
            outcome=data.get("outcome", ""),
        )


# ---------------------------------------------------------------------------
# Offline evaluation report — Requirements 2.10-2.14
# ---------------------------------------------------------------------------
@dataclass
class EvaluationReport:
    """A compiled offline-evaluation report (Requirement 2.10)."""

    baseline_name: str
    generated_at: str
    aggregate_scores: dict[str, float]  # evaluator -> mean
    case_count: int
    per_case: list[dict[str, Any]] = field(default_factory=list)
    rendered_path: str | None = None  # local artifact path (always persisted)
    delivery: DeliveryMechanism = "outbox"
    delivered_to: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvaluationReport":
        return cls(
            baseline_name=data["baseline_name"],
            generated_at=data["generated_at"],
            aggregate_scores=dict(data.get("aggregate_scores", {})),
            case_count=int(data.get("case_count", 0)),
            per_case=list(data.get("per_case", []) or []),
            rendered_path=data.get("rendered_path"),
            delivery=data.get("delivery", "outbox"),
            delivered_to=data.get("delivered_to"),
        )


__all__ = [
    "ApprovalStatus",
    "Decision",
    "GroundTruth",
    "ConversationCase",
    "SessionResult",
    "Baseline",
    "BundleConfig",
    "BundleVersion",
    "EvalMetric",
    "ABTestResult",
    "ApprovalRequest",
    "AuditEntry",
    "EvaluationReport",
]


def is_model(obj: Any) -> bool:
    """True if ``obj`` is one of this module's dataclass instances (not a class)."""
    return dataclasses.is_dataclass(obj) and not isinstance(obj, type)
