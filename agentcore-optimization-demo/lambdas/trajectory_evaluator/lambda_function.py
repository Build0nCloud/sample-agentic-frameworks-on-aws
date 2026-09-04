"""TrajectoryQuality — AgentCore code-based (Lambda) evaluator, SESSION level.

Scores the *tool-call trajectory* of a session deterministically, without ground truth,
so it can run in AgentCore ONLINE evaluation over live/A-B traffic. It rewards using
tools and following the domain's required orderings (preconditions), which is exactly
the dimension the recommended (strong) prompt improves over a weak, tool-avoiding one:

  * used_at_least_one_tool
  * refill only after get_medications           (refill precondition)
  * schedule only after find_provider + check_coverage  (scheduling precondition)

The scoring logic is a pure function (``score_trajectory``) so it is unit-testable
without the AgentCore SDK. The Lambda handler parses session spans into an ordered list
of tool names and delegates to it.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Pure logic (importable/testable without the bedrock_agentcore SDK)
# ---------------------------------------------------------------------------
REFILL = "request_prescription_refill"
GET_MEDS = "get_medications"
SCHEDULE = "schedule_appointment"
FIND_PROVIDER = "find_provider"
CHECK_COVERAGE = "check_coverage"


def extract_tool_names(session_spans: list) -> list[str]:
    """Return the ordered tool names from a session's OTel spans."""
    calls = []
    for span in session_spans or []:
        attrs = span.get("attributes", {}) if isinstance(span, dict) else {}
        if attrs.get("gen_ai.operation.name") == "execute_tool":
            name = attrs.get("gen_ai.tool.name")
            if name:
                calls.append(name)
    return calls


def _before(tools: list[str], prereq: str, action_index: int) -> bool:
    return prereq in tools[:action_index]


def score_trajectory(tool_names: list[str]) -> tuple[float, str, str]:
    """Score tool-trajectory quality in [0,1] with a label and explanation.

    No ground truth required: checks tool usage and required orderings.
    """
    checks: list[str] = []
    passed: list[str] = []
    failed: list[str] = []

    # 1. Used at least one tool (a weak, tool-avoiding agent fails this).
    checks.append("used_at_least_one_tool")
    if tool_names:
        passed.append("used_at_least_one_tool")
    else:
        failed.append("used_at_least_one_tool: no tools were called")

    # 2. Refill precondition: get_medications before the first refill.
    if REFILL in tool_names:
        checks.append("refill_after_get_medications")
        idx = tool_names.index(REFILL)
        if _before(tool_names, GET_MEDS, idx):
            passed.append("refill_after_get_medications")
        else:
            failed.append("refill_after_get_medications: refilled without listing medications first")

    # 3. Scheduling precondition: find_provider AND check_coverage before scheduling.
    if SCHEDULE in tool_names:
        checks.append("schedule_after_provider_and_coverage")
        idx = tool_names.index(SCHEDULE)
        if _before(tool_names, FIND_PROVIDER, idx) and _before(tool_names, CHECK_COVERAGE, idx):
            passed.append("schedule_after_provider_and_coverage")
        else:
            failed.append("schedule_after_provider_and_coverage: scheduled without verifying provider/coverage first")

    total = len(checks)
    value = round(len(passed) / total, 3) if total else 1.0
    label = "PASS" if value == 1.0 else ("PARTIAL" if value >= 0.5 else "FAIL")
    parts = [f"{len(passed)}/{total} trajectory checks passed", f"tools={tool_names or ['none']}"]
    if failed:
        parts.append("failed=" + "; ".join(failed))
    return value, label, " | ".join(parts)


# ---------------------------------------------------------------------------
# Lambda handler (uses the AgentCore SDK contract when deployed)
# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised only in the deployed Lambda
    from bedrock_agentcore.evaluation import (  # type: ignore
        EvaluatorInput,
        EvaluatorOutput,
        custom_code_based_evaluator,
    )

    @custom_code_based_evaluator()
    def lambda_handler(evaluator_input: "EvaluatorInput", _context) -> "EvaluatorOutput":
        tool_names = extract_tool_names(getattr(evaluator_input, "session_spans", []) or [])
        value, label, explanation = score_trajectory(tool_names)
        return EvaluatorOutput(value=value, label=label, explanation=explanation)

except Exception:  # noqa: BLE001 - SDK not present (e.g. local unit tests)
    def lambda_handler(event, _context):  # type: ignore[misc]
        spans = (event or {}).get("session_spans", [])
        value, label, explanation = score_trajectory(extract_tool_names(spans))
        return {"value": value, "label": label, "explanation": explanation}
