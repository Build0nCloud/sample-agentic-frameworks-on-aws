"""Offline evaluation + report tests (tasks 8.3, 8.4).

Property 5 (per-case isolation) plus report-compilation and persistence tests.
Requirements: 2.5, 2.6, 2.8, 2.9, 2.10
"""

from hypothesis import given
from hypothesis import strategies as st

from agentcore_demo.evaluation import offline
from agentcore_demo.evaluation.evaluators import EvaluatorResult
from agentcore_demo.evaluation.offline import (
    CaseResult,
    aggregate_custom_scores,
    build_report,
    render_html,
    render_markdown,
    run_offline_evaluation,
)
from agentcore_demo.models import ConversationCase, GroundTruth, SessionResult
from agentcore_demo.state import StateStore


def _cases(n):
    return [
        ConversationCase(case_id=f"c{i}", turns=["hi"], ground_truth=GroundTruth(expected_trajectory=["a"], assertions=["x"]))
        for i in range(n)
    ]


# --------------------------------------------------------------------------
# Property 5: Offline per-case isolation
# Validates: Requirements 2.9
# --------------------------------------------------------------------------
@given(flags=st.lists(st.booleans(), min_size=1, max_size=15))
def test_property_per_case_isolation(flags):
    cases = _cases(len(flags))

    def runner(case):
        idx = int(case.case_id[1:])
        if flags[idx]:
            raise RuntimeError("boom")
        return SessionResult(session_id=f"s{idx}", final_response="ok", tool_calls=["a"])

    result = run_offline_evaluation("baseline", cases, runner)
    # Reported case count equals total, failures recorded (not dropped).
    assert result.baseline.case_count == len(flags)
    assert len(result.case_results) == len(flags)
    assert sum(1 for c in result.case_results if c.error is not None) == sum(flags)
    # Every non-failed case was scored.
    for c in result.case_results:
        if c.error is None:
            assert c.evaluators


def test_all_cases_failing_still_reports_total_count():
    cases = _cases(4)

    def runner(_case):
        raise RuntimeError("always fails")

    result = run_offline_evaluation("b", cases, runner)
    assert result.baseline.case_count == 4
    assert len(result.failed_cases) == 4
    assert result.baseline.scores == {}  # nothing to aggregate


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------
def test_aggregate_custom_scores_means_over_applicable_no_error():
    results = [
        CaseResult("c0", "s0", evaluators={"E": EvaluatorResult("E", True, True, 1.0, "")}),
        CaseResult("c1", "s1", evaluators={"E": EvaluatorResult("E", True, False, 0.0, "")}),
        CaseResult("c2", "s2", error="boom", evaluators={}),  # excluded
        CaseResult("c3", "s3", evaluators={"E": EvaluatorResult("E", False, None, None, "n/a")}),  # not applicable, excluded
    ]
    assert aggregate_custom_scores(results) == {"E": 0.5}


# --------------------------------------------------------------------------
# End-to-end with a fake runner + persistence
# --------------------------------------------------------------------------
def _passing_runner(case):
    # tool_calls match the expected trajectory -> trajectory & tool-call pass.
    traj = case.ground_truth.expected_trajectory or []
    return SessionResult(session_id=f"s-{case.case_id}", final_response="Your request is complete.", tool_calls=list(traj))


def test_end_to_end_scores_and_persists(tmp_path):
    store = StateStore(tmp_path / "artifacts")
    cases = _cases(3)
    result = run_offline_evaluation("baseline_v1", cases, _passing_runner, store=store, config_ref="agent:v1")

    # All three custom evaluators applicable & passing -> mean 1.0.
    assert result.baseline.scores["Custom.Trajectory"] == 1.0
    assert result.baseline.scores["Custom.ToolCall"] == 1.0
    assert result.baseline.scores["Custom.ClinicalSafety"] == 1.0

    # Baseline + report artifacts persisted.
    assert store.exists("baselines", "baseline_v1")
    report_dir = store.subdir("reports") / "baseline_v1"
    assert (report_dir / "report.json").exists()
    assert (report_dir / "report.md").exists()
    assert (report_dir / "report.html").exists()
    assert result.report.rendered_path.endswith("report.html")


def test_batch_scorer_merged_into_scores():
    cases = _cases(2)

    def batch_scorer(session_ids):
        assert len(session_ids) == 2
        return {"Builtin.GoalSuccessRate": 0.8}

    result = run_offline_evaluation("b", cases, _passing_runner, batch_scorer=batch_scorer)
    assert result.baseline.scores["Builtin.GoalSuccessRate"] == 0.8
    assert "Custom.Trajectory" in result.baseline.scores


# --------------------------------------------------------------------------
# Report rendering
# --------------------------------------------------------------------------
def test_report_rendering_contains_scores_and_cases():
    cases = _cases(2)
    result = run_offline_evaluation("rep", cases, _passing_runner)
    md = render_markdown(result.report)
    assert "Offline Evaluation Report" in md and "Custom.Trajectory" in md and "c0" in md
    html = render_html(result.report)
    assert "<html>" in html and "Custom.ClinicalSafety" in html and "c1" in html


def test_build_report_case_count_and_per_case():
    cases = _cases(3)
    result = run_offline_evaluation("rep", cases, _passing_runner)
    report = build_report(result.baseline, result.case_results)
    assert report.case_count == 3
    assert len(report.per_case) == 3
    assert report.per_case[0]["case_id"] == "c0"


def test_load_cases_reads_shipped_dataset():
    from agentcore_demo import config as cfg
    c = cfg.load_config(cfg.DEFAULT_CONFIG_PATH)
    cases = offline.load_cases(c.offline_dataset)
    assert len(cases) >= 10 and all(isinstance(x, ConversationCase) for x in cases)
