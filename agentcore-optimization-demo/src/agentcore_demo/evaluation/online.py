"""Online evaluation and the real-traffic driver.

Online evaluation continuously scores sampled production sessions. This module:

* manages the online-eval config lifecycle (start / stop / teardown) via the AgentCore
  client (Requirements 3.1, 3.2, 3.7);
* drives *real* traffic against the deployed agent by replaying scripted multi-turn
  sessions, so online evaluation can be demonstrated without waiting for organic users
  (Requirement 3.6);
* aggregates per-evaluator scores, computes a quality trend over time, and surfaces
  low-scoring sessions for inspection (Requirements 3.3, 3.4, 3.5).

Reading per-session scores from AgentCore/CloudWatch is provided by an injectable
"score provider" so the aggregation/inspection logic is unit-testable without AWS.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from ..agentcore_client import AgentCoreClient, AgentHandle, BundleRef, OnlineEvalConfig, OnlineEvalHandle
from ..models import SessionResult


# ---------------------------------------------------------------------------
# Per-session online score (as surfaced by the score provider)
# ---------------------------------------------------------------------------
@dataclass
class SessionScore:
    session_id: str
    timestamp: str                       # ISO-8601; used to order the trend
    scores: dict[str, float]             # evaluator -> score
    inputs: list[str] = field(default_factory=list)
    output: str = ""
    tool_calls: list[str] = field(default_factory=list)

    def min_score(self) -> float | None:
        return min(self.scores.values()) if self.scores else None


# ---------------------------------------------------------------------------
# Traffic driver (Requirement 3.6)
# ---------------------------------------------------------------------------
def load_traffic(path: str | Path) -> list[dict]:
    """Load traffic sessions ({"name", "turns"}) from a JSONL dataset."""
    p = Path(path)
    sessions: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            sessions.append(json.loads(line))
    return sessions


def drive_traffic(
    client: AgentCoreClient,
    agent: AgentHandle,
    sessions: Sequence[dict],
    *,
    loops: int = 1,
    bundle: BundleRef | None = None,
) -> list[SessionResult]:
    """Drive real multi-turn traffic against the deployed agent.

    Each session (and each loop) uses a fresh runtime session id so it counts as a
    distinct session; ``loops`` replays the dataset to reach enough volume for online
    evaluation / A-B results (Requirements 3.6, 5.9).
    """
    results: list[SessionResult] = []
    for loop in range(max(1, loops)):
        for spec in sessions:
            session_id = str(uuid.uuid4())
            results.append(client.send_session(agent, spec["turns"], session_id=session_id, bundle=bundle))
    return results


# ---------------------------------------------------------------------------
# Aggregation + inspection (Requirements 3.3, 3.4, 3.5)
# ---------------------------------------------------------------------------
def aggregate_online_scores(session_scores: Iterable[SessionScore]) -> dict[str, float]:
    """Mean score per evaluator across sessions that have it."""
    totals: dict[str, list[float]] = {}
    for ss in session_scores:
        for name, score in ss.scores.items():
            totals.setdefault(name, []).append(score)
    return {name: sum(v) / len(v) for name, v in totals.items() if v}


def quality_trend(session_scores: Iterable[SessionScore], evaluator: str) -> list[dict]:
    """Ordered trend for one evaluator over time, with a cumulative running mean."""
    points = sorted(
        ((ss.timestamp, ss.session_id, ss.scores[evaluator]) for ss in session_scores if evaluator in ss.scores),
        key=lambda t: t[0],
    )
    trend: list[dict] = []
    running_sum = 0.0
    for i, (ts, sid, score) in enumerate(points, start=1):
        running_sum += score
        trend.append({"timestamp": ts, "session_id": sid, "score": score, "running_mean": running_sum / i})
    return trend


def low_scoring_sessions(
    session_scores: Iterable[SessionScore],
    threshold: float,
    evaluator: str | None = None,
) -> list[SessionScore]:
    """Sessions at or below ``threshold`` (on ``evaluator`` or the min across evaluators).

    Returned ascending by the key score so the worst sessions come first (R3.5).
    """
    def key(ss: SessionScore) -> float | None:
        return ss.scores.get(evaluator) if evaluator else ss.min_score()

    flagged = [ss for ss in session_scores if (k := key(ss)) is not None and k <= threshold]
    return sorted(flagged, key=lambda ss: key(ss))  # type: ignore[arg-type,return-value]


# A score provider maps a set of session ids to their per-session online scores.
ScoreProvider = Callable[[Sequence[str]], list[SessionScore]]


# ---------------------------------------------------------------------------
# Online evaluation lifecycle (Requirements 3.1, 3.2, 3.7)
# ---------------------------------------------------------------------------
class OnlineEvaluation:
    """Manages an online-eval config and surfaces its results."""

    def __init__(self, client: AgentCoreClient, config: OnlineEvalConfig, score_provider: ScoreProvider | None = None):
        self.client = client
        self.config = config
        self.score_provider = score_provider
        self.handle: OnlineEvalHandle | None = None

    def start(self) -> OnlineEvalHandle:
        """Create and enable the online evaluation config."""
        self.handle = self.client.create_online_eval(self.config)
        return self.handle

    def stop(self) -> None:
        """Stop (disable) the online evaluation as a distinct stage (R3.7)."""
        if self.handle is not None:
            self.client.disable_online_eval(self.handle)

    def teardown(self) -> None:
        """Delete the online evaluation config."""
        if self.handle is not None:
            self.client.delete_online_eval(self.handle)

    def status(self) -> dict:
        if self.handle is None:
            return {}
        return self.client.get_online_scores(self.handle)

    # --- result views ---------------------------------------------------
    def scores_for(self, session_ids: Sequence[str]) -> list[SessionScore]:
        if self.score_provider is None:
            raise RuntimeError("no score_provider configured for this OnlineEvaluation")
        return self.score_provider(session_ids)

    def summary(self, session_ids: Sequence[str], low_threshold: float = 0.5, evaluator: str | None = None) -> dict:
        """Aggregate scores, quality trend, and low-scoring sessions for inspection."""
        scores = self.scores_for(session_ids)
        aggregates = aggregate_online_scores(scores)
        trend_evaluator = evaluator or (next(iter(aggregates)) if aggregates else None)
        return {
            "aggregate_scores": aggregates,
            "session_count": len(scores),
            "trend": quality_trend(scores, trend_evaluator) if trend_evaluator else [],
            "low_scoring": [
                {
                    "session_id": ss.session_id,
                    "scores": ss.scores,
                    "inputs": ss.inputs,
                    "output": ss.output[:300],
                    "tool_calls": ss.tool_calls,
                }
                for ss in low_scoring_sessions(scores, low_threshold, evaluator)
            ],
        }
