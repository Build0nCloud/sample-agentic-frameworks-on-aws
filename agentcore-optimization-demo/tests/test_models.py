"""Unit tests for agentcore_demo.models (task 3.1).

Verifies construction, JSON serialization via state.to_jsonable (enums -> string
values), and from_dict round-trips for the models later stages persist/reload.
Requirements: 2.6, 2.10, 4.4, 5.6, 6.2, 6.6
"""

from agentcore_demo import models as m
from agentcore_demo.state import to_jsonable


def test_conversation_case_roundtrip():
    case = m.ConversationCase(
        case_id="refill-1",
        turns=["What meds am I on?", "Refill the second one"],
        ground_truth=m.GroundTruth(
            expected_response=None,
            assertions=["mentions refill confirmation"],
            expected_trajectory=["get_patient_profile", "get_medications", "request_prescription_refill"],
        ),
    )
    data = to_jsonable(case)
    back = m.ConversationCase.from_dict(data)
    assert back == case
    assert back.ground_truth.expected_trajectory[-1] == "request_prescription_refill"


def test_abtest_result_nested_metric_roundtrip():
    metric = m.EvalMetric(mean=0.9, abs_change=0.1, pct_change=12.5, p_value=0.01, ci_low=0.85, ci_high=0.95, significant=True)
    result = m.ABTestResult(
        ab_test_id="ab-123",
        per_variant={"treatment": {"Builtin.GoalSuccessRate": metric}},
        winner="treatment",
        significant=True,
    )
    data = to_jsonable(result)
    back = m.ABTestResult.from_dict(data)
    assert back == result
    assert back.per_variant["treatment"]["Builtin.GoalSuccessRate"].significant is True


def test_approval_request_enum_serializes_to_value():
    req = m.ApprovalRequest(
        request_id="r1",
        ab_test_id="ab-123",
        winning_variant="treatment",
        metrics={"Builtin.GoalSuccessRate": m.EvalMetric(mean=0.9)},
        agent_ref="PatientSupport",
        status=m.ApprovalStatus.PENDING_APPROVAL,
        created_at="2026-01-01T00:00:00+00:00",
    )
    data = to_jsonable(req)
    assert data["status"] == "PENDING_APPROVAL"  # enum serialized to its string value
    back = m.ApprovalRequest.from_dict(data)
    assert back == req
    assert isinstance(back.status, m.ApprovalStatus)


def test_audit_entry_roundtrip():
    entry = m.AuditEntry(
        timestamp="2026-01-01T00:00:00+00:00",
        request_id="r1",
        decider="cli",
        decision=m.Decision.APPROVED,
        metrics_at_decision={"Builtin.Helpfulness": m.EvalMetric(mean=0.7)},
        outcome="promoted treatment",
    )
    data = to_jsonable(entry)
    assert data["decision"] == "APPROVED"
    assert m.AuditEntry.from_dict(data) == entry


def test_evaluation_report_and_bundle_and_baseline_roundtrip():
    report = m.EvaluationReport(
        baseline_name="baseline_v1",
        generated_at="2026-01-01T00:00:00+00:00",
        aggregate_scores={"Builtin.Correctness": 0.8},
        case_count=12,
        per_case=[{"case_id": "c1", "passed": True}],
        rendered_path="artifacts/reports/baseline_v1/report.html",
        delivery="outbox",
    )
    assert m.EvaluationReport.from_dict(to_jsonable(report)) == report

    bundle = m.BundleVersion(
        bundle_id="b1",
        version_id="v1",
        parent_version_ids=["v0"],
        commit_message="baseline",
        config=m.BundleConfig(system_prompt="hi", model_id="nova", tool_descriptions={"t": "d"}),
    )
    assert m.BundleVersion.from_dict(to_jsonable(bundle)) == bundle

    baseline = m.Baseline(name="baseline_v1", config_ref="v1", scores={"g": 0.8}, case_count=12, created_at="2026-01-01T00:00:00+00:00")
    assert m.Baseline.from_dict(to_jsonable(baseline)) == baseline
