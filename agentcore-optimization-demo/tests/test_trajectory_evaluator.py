"""Tests for the custom code-based trajectory evaluator Lambda (Phase 2).

Covers the pure scoring logic and span parsing (no AWS, no SDK required). The scoring
is ground-truth-free so it can run in AgentCore online evaluation.
"""

import importlib.util
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

# Load the Lambda module directly from the lambdas/ tree (not part of the package).
_LAMBDA = Path(__file__).resolve().parent.parent / "lambdas" / "trajectory_evaluator" / "lambda_function.py"
_spec = importlib.util.spec_from_file_location("trajectory_lambda", _LAMBDA)
traj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(traj)


def _span(tool):
    return {"attributes": {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool}}


def test_extract_tool_names_orders_execute_tool_spans():
    spans = [_span("get_medications"), {"attributes": {"gen_ai.operation.name": "chat"}}, _span("request_prescription_refill")]
    assert traj.extract_tool_names(spans) == ["get_medications", "request_prescription_refill"]


def test_no_tools_scores_zero():
    value, label, _ = traj.score_trajectory([])
    assert value == 0.0 and label == "FAIL"


def test_good_refill_trajectory_scores_one():
    value, label, _ = traj.score_trajectory(["get_patient_profile", "get_medications", "request_prescription_refill"])
    assert value == 1.0 and label == "PASS"


def test_refill_without_meds_partial():
    # used a tool (pass) but refilled without listing meds first (fail) -> 1/2
    value, label, _ = traj.score_trajectory(["request_prescription_refill"])
    assert value == 0.5 and label == "PARTIAL"


def test_good_scheduling_trajectory_scores_one():
    value, _, _ = traj.score_trajectory(["find_provider", "check_coverage", "schedule_appointment"])
    assert value == 1.0


def test_schedule_missing_coverage_partial():
    value, _, _ = traj.score_trajectory(["find_provider", "schedule_appointment"])
    assert value == 0.5  # used_tools pass, precondition fail


def test_readonly_tool_only_passes():
    value, label, _ = traj.score_trajectory(["lookup_health_policy"])
    assert value == 1.0 and label == "PASS"


def test_handler_composition_from_spans():
    # The handler composes extract_tool_names -> score_trajectory; verify that path
    # (independent of whether the AgentCore SDK decorator is installed).
    spans = [_span("get_medications"), _span("request_prescription_refill")]
    value, label, _ = traj.score_trajectory(traj.extract_tool_names(spans))
    assert value == 1.0 and label == "PASS"


# --- property: weak (no tools) always < strong (well-ordered) --------------
@given(extra=st.lists(st.sampled_from(["get_patient_profile", "check_coverage", "find_provider", "get_medications"]), max_size=4))
def test_property_no_tools_never_beats_tool_use(extra):
    weak, _, _ = traj.score_trajectory([])
    strong, _, _ = traj.score_trajectory(extra + ["get_medications", "request_prescription_refill"])
    assert weak == 0.0
    assert strong >= weak
