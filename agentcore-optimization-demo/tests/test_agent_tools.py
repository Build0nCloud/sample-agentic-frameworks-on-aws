"""Tool-suite tests for the patient-support agent (tasks 4.2, 4.3).

Covers deterministic tool behavior, the two designed trajectories, and
Property 7 (tool robustness) with property-based testing.
Requirements: 9.1, 9.2, 9.3, 9.8
"""

from hypothesis import given
from hypothesis import strategies as st

from agentcore_demo.agent import patient_support_agent as ag

_KNOWN_PATIENTS = set(ag._PATIENTS)


def _call_tool(name, s, flag=True):
    """Invoke a tool by name using ``s`` for string params (arbitrary arity)."""
    fn = ag.TOOL_IMPLS[name]
    if name in ("get_patient_profile", "get_medications", "lookup_health_policy"):
        return fn(s)
    if name == "find_provider":
        return fn(s, flag)
    if name in ("check_coverage", "get_lab_results", "request_prescription_refill"):
        return fn(s, s)
    if name == "schedule_appointment":
        return fn(s, s, s)
    raise AssertionError(f"unhandled tool {name}")


def test_tool_suite_has_at_least_five_tools():
    # Requirement 9.1
    assert len(ag.TOOL_NAMES) >= 5
    assert set(ag.TOOL_IMPLS) == set(ag.DEFAULT_TOOL_DESCRIPTIONS)


# --- Property 7: Tool robustness --------------------------------------------
# Validates: Requirements 9.8
@given(s=st.text(max_size=24), flag=st.booleans())
def test_property_tools_never_raise_and_return_dict(s, flag):
    """Any tool invoked with arbitrary input returns a structured dict, never raises."""
    for name in ag.TOOL_NAMES:
        result = _call_tool(name, s, flag)
        assert isinstance(result, dict)


@given(pid=st.text(max_size=24).filter(lambda x: x not in _KNOWN_PATIENTS))
def test_property_unknown_patient_returns_structured_error(pid):
    """Patient-keyed tools return a structured error (not an exception) for unknown ids."""
    assert "error" in ag._get_patient_profile(pid)
    assert "error" in ag._get_medications(pid)
    assert "error" in ag._check_coverage(pid, "OFFICE_VISIT")
    assert "error" in ag._get_lab_results(pid, "lipid")
    assert "error" in ag._schedule_appointment(pid, "PRV-201", "2026-01-01")
    assert "error" in ag._request_prescription_refill(pid, "MED-01")


# --- Deterministic behavior + trajectories ----------------------------------
def test_refill_trajectory_happy_path():
    assert ag._get_patient_profile("PT-1001")["plan_id"] == "PLN-GOLD"
    meds = ag._get_medications("PT-1001")
    assert meds["count"] == 2 and meds["medications"][0]["medication_id"] == "MED-01"
    refill = ag._request_prescription_refill("PT-1001", "MED-01")
    assert refill["status"] == "SUBMITTED" and refill["refill_id"].startswith("RX-")


def test_scheduling_trajectory_happy_path():
    providers = ag._find_provider("cardiology", in_network_only=True)
    ids = [p["provider_id"] for p in providers["providers"]]
    assert ids == ["PRV-201"]  # out-of-network PRV-202 excluded
    coverage = ag._check_coverage("PT-1001", "SPECIALIST_VISIT")
    assert coverage["covered"] is True
    appt = ag._schedule_appointment("PT-1001", "PRV-201", "2026-03-01")
    assert appt["status"] == "SCHEDULED"


def test_refill_precondition_unknown_medication_errors():
    res = ag._request_prescription_refill("PT-1001", "MED-DOES-NOT-EXIST")
    assert "error" in res and "on file" in res["error"]


def test_refill_without_refills_needs_provider_authorization():
    res = ag._request_prescription_refill("PT-1002", "MED-03")  # refills_remaining == 0
    assert res["status"] == "PENDING_PROVIDER_AUTHORIZATION"


def test_schedule_out_of_network_provider_is_blocked():
    res = ag._schedule_appointment("PT-1001", "PRV-202", "2026-03-01")  # out of network
    assert "error" in res and "out of network" in res["error"]


def test_find_provider_unknown_specialty_returns_empty_structured():
    res = ag._find_provider("astrology", in_network_only=True)
    assert res["count"] == 0 and res["providers"] == [] and res["note"]


def test_check_coverage_unknown_service_code_lists_available():
    res = ag._check_coverage("PT-1001", "TELEPORT")
    assert "error" in res and "OFFICE_VISIT" in res["available_service_codes"]


def test_lookup_policy_unknown_lists_available_topics():
    res = ag._lookup_health_policy("time_travel")
    assert "error" in res and "billing" in res["available_topics"]
