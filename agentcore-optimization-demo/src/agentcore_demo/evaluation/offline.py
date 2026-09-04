"""Offline (batch) evaluation and report builder.

Replays each curated multi-turn case in a single session against the deployed agent,
scores it with the custom evaluators (locally) and optionally the built-in evaluators
(service-side batch evaluation), aggregates per-evaluator scores, persists a named
baseline, and compiles a full evaluation report.

Per-case isolation (Requirement 2.9 / Property 5): a case that errors is recorded as a
failure and evaluation continues; the reported case count always equals the total
number of cases.

The session runner is injected so the pipeline is unit-testable without AWS: in
production it is built from :class:`AgentCoreClient`; in tests it is a fake.

Requirements: 2.3, 2.4, 2.5, 2.6, 2.8, 2.9, 2.10, 4.7
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from ..models import Baseline, ConversationCase, EvaluationReport, SessionResult
from ..state import StateStore, to_jsonable
from .evaluators import CUSTOM_EVALUATORS, EvaluatorResult

# A session runner turns a case into a SessionResult (may raise on failure).
SessionRunner = Callable[[ConversationCase], SessionResult]
# A batch scorer turns a list of session ids into built-in aggregate scores.
BatchScorer = Callable[[Sequence[str]], dict[str, float]]


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------
def load_cases(path: str | Path) -> list[ConversationCase]:
    """Load conversation cases from a JSONL dataset."""
    p = Path(path)
    cases: list[ConversationCase] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(ConversationCase.from_dict(json.loads(line)))
    return cases


# ---------------------------------------------------------------------------
# Per-case results
# ---------------------------------------------------------------------------
@dataclass
class CaseResult:
    case_id: str
    session_id: str | None
    error: str | None = None
    final_response: str = ""
    tool_calls: list[str] = field(default_factory=list)
    evaluators: dict[str, EvaluatorResult] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "session_id": self.session_id,
            "error": self.error,
            "final_response": self.final_response[:500],
            "tool_calls": list(self.tool_calls),
            "evaluators": {
                name: {"applicable": r.applicable, "passed": r.passed, "score": r.score, "explanation": r.explanation}
                for name, r in self.evaluators.items()
            },
        }


@dataclass
class OfflineResult:
    baseline: Baseline
    report: EvaluationReport
    case_results: list[CaseResult]

    @property
    def failed_cases(self) -> list[CaseResult]:
        return [c for c in self.case_results if c.error is not None]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def aggregate_custom_scores(case_results: Sequence[CaseResult]) -> dict[str, float]:
    """Mean score per custom evaluator over cases where it applied (no error)."""
    totals: dict[str, list[float]] = {}
    for cr in case_results:
        if cr.error is not None:
            continue
        for name, r in cr.evaluators.items():
            if r.applicable and r.score is not None:
                totals.setdefault(name, []).append(r.score)
    return {name: sum(scores) / len(scores) for name, scores in totals.items() if scores}


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------
def run_offline_evaluation(
    name: str,
    cases: Iterable[ConversationCase],
    runner: SessionRunner,
    *,
    custom_evaluators: Sequence = CUSTOM_EVALUATORS,
    batch_scorer: BatchScorer | None = None,
    config_ref: str = "agent:v1",
    store: StateStore | None = None,
    delivery: str = "outbox",
    tool_call_resolver: Callable[[str], list[str]] | None = None,
    ingestion_wait: float = 0.0,
    sleep: Callable[[float], None] = __import__("time").sleep,
) -> OfflineResult:
    """Run offline evaluation over ``cases`` and produce a named baseline + report.

    Args:
        name: baseline/report name.
        cases: conversation cases to evaluate.
        runner: turns a case into a SessionResult (may raise; failures are isolated).
        custom_evaluators: local evaluators run on each SessionResult.
        batch_scorer: optional callable returning built-in service-side scores for the
            produced session ids (merged into the baseline scores).
        config_ref: identifier of the configuration under test (bundle version/agent).
        store: if provided, the baseline and report artifacts are persisted.
        delivery: delivery mechanism recorded on the report.
    """
    cases = list(cases)
    case_results: list[CaseResult] = []
    session_ids: list[str] = []

    # Phase 1: run all sessions (isolating per-case failures, Property 5).
    ran: list[tuple[ConversationCase, SessionResult]] = []
    for case in cases:
        try:
            session = runner(case)
        except Exception as exc:  # noqa: BLE001 - isolate per-case failures (Property 5)
            case_results.append(CaseResult(case_id=case.case_id, session_id=None, error=str(exc)))
            continue
        session_ids.append(session.session_id)
        ran.append((case, session))

    # Phase 2 (optional): after CloudWatch ingestion, reconstruct ordered tool calls
    # from traces so trajectory/tool-call evaluators have data to score.
    if tool_call_resolver is not None and ran:
        if ingestion_wait > 0:
            sleep(ingestion_wait)
        for _case, session in ran:
            if not session.tool_calls:
                session.tool_calls = tool_call_resolver(session.session_id)

    # Phase 3: score each session with the custom evaluators.
    for case, session in ran:
        results = {ev.name: ev.evaluate(case.ground_truth, session) for ev in custom_evaluators}
        case_results.append(
            CaseResult(
                case_id=case.case_id,
                session_id=session.session_id,
                error=None,
                final_response=session.final_response,
                tool_calls=list(session.tool_calls),
                evaluators=results,
            )
        )

    # Restore original case order (failures were appended first in phase 1).
    order = {c.case_id: i for i, c in enumerate(cases)}
    case_results.sort(key=lambda cr: order.get(cr.case_id, len(order)))

    scores = aggregate_custom_scores(case_results)
    if batch_scorer is not None and session_ids:
        try:
            scores.update(batch_scorer(session_ids))
        except Exception as exc:  # noqa: BLE001 - built-in scoring is best-effort
            scores["_batch_error"] = 0.0
            _ = exc

    baseline = Baseline(
        name=name,
        config_ref=config_ref,
        scores=scores,
        case_count=len(cases),  # includes failures (Property 5)
        created_at=_utcnow_iso(),
    )
    report = build_report(baseline, case_results, delivery=delivery)

    if store is not None:
        persist(store, baseline, report, case_results)

    return OfflineResult(baseline=baseline, report=report, case_results=case_results)


# ---------------------------------------------------------------------------
# Report building + rendering (Requirement 2.10)
# ---------------------------------------------------------------------------
def build_report(baseline: Baseline, case_results: Sequence[CaseResult], delivery: str = "outbox") -> EvaluationReport:
    return EvaluationReport(
        baseline_name=baseline.name,
        generated_at=_utcnow_iso(),
        aggregate_scores=dict(baseline.scores),
        case_count=baseline.case_count,
        per_case=[cr.to_dict() for cr in case_results],
        rendered_path=None,
        delivery=delivery,
        delivered_to=None,
    )


def render_markdown(report: EvaluationReport) -> str:
    lines = [
        f"# Offline Evaluation Report — {report.baseline_name}",
        "",
        f"- Generated: {report.generated_at}",
        f"- Cases: {report.case_count}",
        "",
        "## Aggregate scores",
        "",
        "| Evaluator | Mean score |",
        "| --- | --- |",
    ]
    for name, score in sorted(report.aggregate_scores.items()):
        lines.append(f"| {name} | {score:.3f} |")
    lines += ["", "## Per-case results", "", "| Case | Session | Status | Tools called |", "| --- | --- | --- | --- |"]
    for c in report.per_case:
        status = "ERROR" if c.get("error") else "ok"
        tools = ", ".join(c.get("tool_calls", [])) or "-"
        lines.append(f"| {c['case_id']} | {c.get('session_id') or '-'} | {status} | {tools} |")
    return "\n".join(lines) + "\n"


def render_html(report: EvaluationReport) -> str:
    def esc(x) -> str:
        return _html.escape(str(x))

    rows_scores = "".join(
        f"<tr><td>{esc(n)}</td><td>{s:.3f}</td></tr>" for n, s in sorted(report.aggregate_scores.items())
    )
    rows_cases = ""
    for c in report.per_case:
        status = "ERROR" if c.get("error") else "ok"
        tools = ", ".join(c.get("tool_calls", [])) or "-"
        rows_cases += (
            f"<tr><td>{esc(c['case_id'])}</td><td>{esc(c.get('session_id') or '-')}</td>"
            f"<td>{esc(status)}</td><td>{esc(tools)}</td></tr>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Offline Evaluation — {esc(report.baseline_name)}</title></head><body>"
        f"<h1>Offline Evaluation Report — {esc(report.baseline_name)}</h1>"
        f"<p>Generated: {esc(report.generated_at)}<br>Cases: {esc(report.case_count)}</p>"
        f"<h2>Aggregate scores</h2><table border='1'><tr><th>Evaluator</th><th>Mean score</th></tr>{rows_scores}</table>"
        f"<h2>Per-case results</h2><table border='1'>"
        f"<tr><th>Case</th><th>Session</th><th>Status</th><th>Tools called</th></tr>{rows_cases}</table>"
        "</body></html>"
    )


def persist(store: StateStore, baseline: Baseline, report: EvaluationReport, case_results: Sequence[CaseResult]) -> Path:
    """Persist the baseline and the rendered report artifacts; returns the report dir."""
    store.write_json("baselines", baseline.name, baseline)
    report_dir = store.subdir("reports") / baseline.name
    report_dir.mkdir(parents=True, exist_ok=True)
    html_path = report_dir / "report.html"
    report.rendered_path = str(html_path)
    (report_dir / "report.json").write_text(json.dumps(to_jsonable(report), indent=2), encoding="utf-8")
    (report_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return report_dir


# ---------------------------------------------------------------------------
# Production runner factory (built from the AgentCore client; not unit-tested)
# ---------------------------------------------------------------------------
def make_client_runner(client, agent, bundle=None) -> SessionRunner:
    """Build a SessionRunner that drives the real agent via the AgentCore client.

    Tool-call reconstruction is done as a post-pass by ``run_offline_evaluation`` (after
    CloudWatch ingestion), not inline, so pass a ``tool_call_resolver`` there.
    """

    def runner(case: ConversationCase) -> SessionResult:
        return client.send_session(agent, case.turns, session_id=str(uuid.uuid4()), bundle=bundle)

    return runner
