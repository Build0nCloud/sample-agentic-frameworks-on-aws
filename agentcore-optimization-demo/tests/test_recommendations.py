"""Recommendation tests (task 12.4).

Covers request building, before/after presentation with explanations, and applying a
recommendation to a bundle config.
Requirements: 4.1, 4.2, 4.3
"""

from agentcore_demo.agentcore_client import Recommendation
from agentcore_demo.models import BundleConfig
from agentcore_demo.optimization.recommendations import RecommendationService, apply_to_config, present_change


class FakeRecClient:
    def __init__(self, rec):
        self.rec = rec
        self.requests = []

    def start_recommendation(self, req, wait=True):
        self.requests.append((req, wait))
        return self.rec


def _service(rec):
    client = FakeRecClient(rec)
    svc = RecommendationService(client, log_group_arns=["arn:lg"], service_names=["PatientSupport.DEFAULT"])
    return svc, client


# --------------------------------------------------------------------------
# Request building
# --------------------------------------------------------------------------
def test_recommend_system_prompt_builds_request():
    rec = Recommendation(kind="system_prompt", recommended_system_prompt="better")
    svc, client = _service(rec)
    out = svc.recommend_system_prompt("current prompt", target_evaluator="Builtin.GoalSuccessRate")
    assert out is rec
    req, wait = client.requests[0]
    assert req.kind == "system_prompt"
    assert req.current_system_prompt == "current prompt"
    assert req.target_evaluator_arn.endswith("Builtin.GoalSuccessRate")
    assert req.service_names == ["PatientSupport.DEFAULT"]


def test_recommend_tool_descriptions_builds_request():
    rec = Recommendation(kind="tool_description", recommended_tool_descriptions={"get_medications": "new"})
    svc, client = _service(rec)
    svc.recommend_tool_descriptions({"get_medications": "old"})
    req, _ = client.requests[0]
    assert req.kind == "tool_description"
    assert req.current_tool_descriptions == {"get_medications": "old"}


# --------------------------------------------------------------------------
# Presentation (what changed and why)
# --------------------------------------------------------------------------
def test_present_change_system_prompt_changed():
    rec = Recommendation(kind="system_prompt", recommended_system_prompt="new prompt")
    out = present_change(rec, current_system_prompt="old prompt", target_evaluator="Builtin.GoalSuccessRate")
    assert out["changed"] is True
    assert out["before"] == "old prompt" and out["after"] == "new prompt"
    assert "Builtin.GoalSuccessRate" in out["explanation"]


def test_present_change_system_prompt_unchanged():
    rec = Recommendation(kind="system_prompt", recommended_system_prompt="same")
    out = present_change(rec, current_system_prompt="same")
    assert out["changed"] is False


def test_present_change_reports_error():
    rec = Recommendation(kind="system_prompt", error_code="INSUFFICIENT_DATA", error_message="not enough traces")
    out = present_change(rec, current_system_prompt="x")
    assert out["changed"] is False and "INSUFFICIENT_DATA" in out["error"]


def test_present_change_tool_descriptions_lists_changed_tools():
    rec = Recommendation(kind="tool_description", recommended_tool_descriptions={"get_medications": "new", "find_provider": "same"})
    out = present_change(rec, current_tool_descriptions={"get_medications": "old", "find_provider": "same"})
    assert out["changed"] is True and out["changed_tools"] == ["get_medications"]


# --------------------------------------------------------------------------
# Applying to a bundle config
# --------------------------------------------------------------------------
def test_apply_system_prompt_recommendation_does_not_mutate_base():
    base = BundleConfig(system_prompt="old", tool_descriptions={"t": "d"})
    rec = Recommendation(kind="system_prompt", recommended_system_prompt="new")
    new = apply_to_config(rec, base)
    assert new.system_prompt == "new" and base.system_prompt == "old"  # base untouched


def test_apply_tool_description_recommendation_merges():
    base = BundleConfig(system_prompt="sp", tool_descriptions={"a": "1", "b": "2"})
    rec = Recommendation(kind="tool_description", recommended_tool_descriptions={"b": "22", "c": "3"})
    new = apply_to_config(rec, base)
    assert new.tool_descriptions == {"a": "1", "b": "22", "c": "3"}
    assert base.tool_descriptions == {"a": "1", "b": "2"}  # base untouched
