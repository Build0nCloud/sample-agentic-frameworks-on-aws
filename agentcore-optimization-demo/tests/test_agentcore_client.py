"""Tests for agentcore_client (tasks 5.3, 5.4).

Property 10 (session stickiness) plus unit tests of request shaping, response
parsing, retry/backoff, and promotion — all against lightweight fakes (no boto3,
no real AWS).
Requirements: 1.6, 2.3, 4.3, 5.1, 5.4, 5.7, 5.8, 6.4, 8.2
"""

import json
import types

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agentcore_demo import agentcore_client as ac
from agentcore_demo.agentcore_client import (
    ABTestHandle,
    ABTestSpec,
    ABTestVariant,
    AgentCoreClient,
    AgentHandle,
    BundleRef,
    EvalTarget,
    PromotionDecision,
    assign_variant,
    build_gateway_invoke,
    make_config_bundle_baggage,
    parse_ab_test_result,
    parse_batch_scores,
    retry_call,
)
from agentcore_demo.models import BundleConfig


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class _Body:
    def __init__(self, text: str):
        self._text = text

    def read(self):
        return self._text.encode("utf-8")


class FakeAws:
    """Records calls and returns programmed responses keyed by method name."""

    def __init__(self, **responses):
        self.calls: list[tuple[str, dict]] = []
        self._responses = responses

    def __getattr__(self, name):
        def method(**kwargs):
            self.calls.append((name, kwargs))
            r = self._responses.get(name)
            return r(**kwargs) if callable(r) else ({} if r is None else r)

        return method

    def last(self, name: str) -> dict:
        for n, kw in reversed(self.calls):
            if n == name:
                return kw
        raise AssertionError(f"{name} was not called")

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


def _client(dp=None, ctrl=None):
    cfg = types.SimpleNamespace(region="us-east-1")
    return AgentCoreClient(cfg, dp=dp or FakeAws(), ctrl=ctrl or FakeAws(), sleep=lambda _s: None)


def _agent():
    return AgentHandle(
        runtime_name="PatientSupport",
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/rt-1",
        runtime_id="rt-1",
        log_group="/aws/bedrock-agentcore/runtimes/rt-1-DEFAULT",
        service_name="PatientSupport.DEFAULT",
        role_arn="arn:aws:iam::1:role/PatientSupportRole",
        region="us-east-1",
    )


# --------------------------------------------------------------------------
# Property 10: Session stickiness
# Validates: Requirements 5.4
# --------------------------------------------------------------------------
_VARIANTS = [("C", 50), ("T1", 50)]


@given(sid=st.text(min_size=1, max_size=40))
def test_property_assign_variant_is_sticky(sid):
    first = assign_variant(sid, _VARIANTS)
    for _ in range(5):
        assert assign_variant(sid, _VARIANTS) == first
    assert first in {"C", "T1"}


@given(sid=st.text(min_size=1, max_size=40))
def test_property_zero_weight_variant_never_chosen(sid):
    assert assign_variant(sid, [("C", 100), ("T1", 0)]) == "C"


def test_assign_variant_requires_positive_weight():
    with pytest.raises(ValueError):
        assign_variant("s", [("C", 0), ("T1", 0)])


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
def test_make_config_bundle_baggage():
    b = make_config_bundle_baggage("arn:bundle", "v3")
    assert b == "aws.agentcore.configbundle_arn=arn:bundle,aws.agentcore.configbundle_version=v3"


def test_build_gateway_invoke_shapes_request():
    url, headers, body = build_gateway_invoke("https://gw.example.com/", "PatientAgentV1", "sess-1", "hi")
    assert url == "https://gw.example.com/PatientAgentV1/invocations"
    assert headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"] == "sess-1"
    assert json.loads(body)["prompt"] == "hi"


def test_parse_batch_scores():
    result = {"evaluationResults": {"evaluatorSummaries": [
        {"evaluatorId": "Builtin.Helpfulness", "statistics": {"averageScore": 0.82}},
        {"evaluatorId": "Builtin.Correctness", "statistics": {"averageScore": 0.9}},
        {"evaluatorId": "NoScore", "statistics": {}},
    ]}}
    assert parse_batch_scores(result) == {"Builtin.Helpfulness": 0.82, "Builtin.Correctness": 0.9}


def test_parse_ab_test_result():
    resp = {"results": {"analysisTimestamp": "2026-01-01T00:00:00Z", "evaluatorMetrics": [
        {
            "evaluatorArn": "arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate",
            "controlStats": {"mean": 0.7},
            "variantResults": [
                {"name": "T1", "mean": 0.84, "pValue": 0.01, "percentChange": 20.0, "isSignificant": True,
                 "confidenceInterval": {"lower": 0.8, "upper": 0.88}},
            ],
        }
    ]}}
    result = parse_ab_test_result("ab-1", resp)
    assert result.ab_test_id == "ab-1"
    assert result.significant is True
    assert result.per_variant["control"]["Builtin.GoalSuccessRate"].mean == 0.7
    t1 = result.per_variant["T1"]["Builtin.GoalSuccessRate"]
    assert t1.p_value == 0.01 and t1.significant is True and t1.pct_change == 20.0


# --------------------------------------------------------------------------
# retry/backoff
# --------------------------------------------------------------------------
def _client_error(code):
    exc = Exception("boom")
    exc.response = {"Error": {"Code": code}}  # type: ignore[attr-defined]
    return exc


def test_retry_call_retries_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _client_error("ThrottlingException")
        return "ok"

    assert retry_call(flaky, sleep=lambda _s: None) == "ok"
    assert calls["n"] == 3


def test_retry_call_reraises_non_retryable_immediately():
    calls = {"n": 0}

    def bad():
        calls["n"] += 1
        raise _client_error("ValidationException")

    with pytest.raises(Exception):
        retry_call(bad, sleep=lambda _s: None)
    assert calls["n"] == 1


# --------------------------------------------------------------------------
# invoke / multi-turn session
# --------------------------------------------------------------------------
def test_invoke_once_shapes_payload_and_decodes():
    dp = FakeAws(invoke_agent_runtime=lambda **kw: {"response": _Body("hello")})
    client = _client(dp=dp)
    text = client.invoke_once("arn:rt", "sess-1", "hi", baggage="b=1")
    assert text == "hello"
    kw = dp.last("invoke_agent_runtime")
    assert kw["agentRuntimeArn"] == "arn:rt"
    assert kw["runtimeSessionId"] == "sess-1"
    assert json.loads(kw["payload"])["prompt"] == "hi"
    assert kw["baggage"] == "b=1"


def test_send_session_multi_turn_uses_one_session_id():
    dp = FakeAws(invoke_agent_runtime=lambda **kw: {"response": _Body("resp:" + json.loads(kw["payload"])["prompt"])})
    client = _client(dp=dp)
    result = client.send_session(_agent(), ["turn1", "turn2", "turn3"], session_id="sess-9")
    assert dp.count("invoke_agent_runtime") == 3
    session_ids = {kw["runtimeSessionId"] for _, kw in dp.calls}
    assert session_ids == {"sess-9"}  # all turns share one session -> multi-turn history
    assert result.final_response == "resp:turn3"


def test_send_session_injects_bundle_baggage():
    dp = FakeAws(invoke_agent_runtime=lambda **kw: {"response": _Body("ok")})
    client = _client(dp=dp)
    client.send_session(_agent(), ["hi"], session_id="s", bundle=BundleRef("arn:bundle", "v2"))
    assert dp.last("invoke_agent_runtime")["baggage"] == make_config_bundle_baggage("arn:bundle", "v2")


# --------------------------------------------------------------------------
# batch evaluation
# --------------------------------------------------------------------------
def test_run_batch_evaluation_shapes_request_and_parses_scores():
    dp = FakeAws(
        start_batch_evaluation=lambda **kw: {"batchEvaluationId": "be-1"},
        get_batch_evaluation=lambda **kw: {
            "status": "COMPLETED",
            "evaluationResults": {"evaluatorSummaries": [
                {"evaluatorId": "Builtin.GoalSuccessRate", "statistics": {"averageScore": 0.75}},
            ]},
        },
    )
    client = _client(dp=dp)
    target = EvalTarget(name="baseline", service_name="PatientSupport.DEFAULT",
                        log_groups=["aws/spans", "/aws/x"], session_ids=["s1", "s2"])
    out = client.run_batch_evaluation(target, ["Builtin.GoalSuccessRate"])
    assert out["status"] == "COMPLETED"
    assert out["scores"] == {"Builtin.GoalSuccessRate": 0.75}
    start_kw = dp.last("start_batch_evaluation")
    assert start_kw["evaluators"] == [{"evaluatorId": "Builtin.GoalSuccessRate"}]
    cw = start_kw["dataSourceConfig"]["cloudWatchLogs"]
    assert cw["serviceNames"] == ["PatientSupport.DEFAULT"]
    assert cw["filterConfig"]["sessionIds"] == ["s1", "s2"]


# --------------------------------------------------------------------------
# bundles
# --------------------------------------------------------------------------
def test_create_bundle_version_shapes_components():
    ctrl = FakeAws(create_configuration_bundle=lambda **kw: {"bundleId": "b1", "bundleArn": "arn:b1", "versionId": "v1"})
    client = _client(ctrl=ctrl)
    cfg = BundleConfig(system_prompt="SP", model_id="nova", tool_descriptions={"t": "d"})
    bv = client.create_bundle_version("Baseline", "arn:agent", cfg, "initial")
    assert bv.bundle_id == "b1" and bv.version_id == "v1"
    comp = ctrl.last("create_configuration_bundle")["components"]["arn:agent"]["configuration"]
    assert comp["system_prompt"] == "SP" and comp["tool_descriptions"] == {"t": "d"} and comp["model_id"] == "nova"


def test_update_bundle_version_records_parent_lineage():
    ctrl = FakeAws(update_configuration_bundle=lambda **kw: {"versionId": "v2"})
    client = _client(ctrl=ctrl)
    cfg = BundleConfig(system_prompt="SP2")
    bv = client.update_bundle_version("b1", "arn:agent", cfg, ["v1"], "promote")
    assert bv.version_id == "v2" and bv.parent_version_ids == ["v1"]
    assert ctrl.last("update_configuration_bundle")["parentVersionIds"] == ["v1"]


# --------------------------------------------------------------------------
# A/B testing
# --------------------------------------------------------------------------
def test_start_ab_test_config_bundle_variants():
    dp = FakeAws(create_ab_test=lambda **kw: {"abTestId": "ab-1"})
    client = _client(dp=dp)
    spec = ABTestSpec(
        name="BundleAB", gateway_arn="arn:gw", role_arn="arn:role", online_eval_arn="arn:oe",
        variants=[
            ABTestVariant("C", 50, bundle=BundleRef("arn:c", "v1")),
            ABTestVariant("T1", 50, bundle=BundleRef("arn:t", "v1")),
        ],
    )
    handle = client.start_ab_test(spec)
    assert handle.ab_test_id == "ab-1"
    variants = dp.last("create_ab_test")["variants"]
    assert variants[0]["variantConfiguration"]["configurationBundle"]["bundleArn"] == "arn:c"
    assert variants[1]["weight"] == 50


def test_start_ab_test_target_based_variants():
    dp = FakeAws(create_ab_test=lambda **kw: {"abTestId": "ab-2"})
    client = _client(dp=dp)
    spec = ABTestSpec(
        name="TargetAB", gateway_arn="arn:gw", role_arn="arn:role", online_eval_arn="arn:oe",
        variants=[ABTestVariant("v1", 90, target_endpoint="PatientV1"), ABTestVariant("v2", 10, target_endpoint="PatientV2")],
    )
    client.start_ab_test(spec)
    variants = dp.last("create_ab_test")["variants"]
    assert variants[0]["variantConfiguration"]["gatewayTarget"]["targetName"] == "PatientV1"


def test_variant_requires_bundle_or_target():
    with pytest.raises(ValueError):
        AgentCoreClient._variant_payload(ABTestVariant("x", 50))


def test_stop_ab_test_sets_stopped():
    dp = FakeAws(update_ab_test=lambda **kw: {})
    client = _client(dp=dp)
    client.stop_ab_test(ABTestHandle("ab-1", "BundleAB"))
    assert dp.last("update_ab_test")["executionStatus"] == "STOPPED"


# --------------------------------------------------------------------------
# promotion primitive
# --------------------------------------------------------------------------
def test_promote_variant_config_bundle_updates_with_parent():
    ctrl = FakeAws(
        get_configuration_bundle=lambda **kw: {"versionId": "v1"},
        update_configuration_bundle=lambda **kw: {"versionId": "v2"},
    )
    client = _client(ctrl=ctrl)
    decision = PromotionDecision(
        strategy="config_bundle", bundle_id="b1", agent_arn="arn:agent",
        config=BundleConfig(system_prompt="winner"), commit_message="promote",
    )
    client.promote_variant(decision)
    kw = ctrl.last("update_configuration_bundle")
    assert kw["bundleId"] == "b1"
    assert kw["parentVersionIds"] == ["v1"]  # looked up current version for lineage
    assert kw["components"]["arn:agent"]["configuration"]["system_prompt"] == "winner"


def test_promote_variant_target_based_stops_test():
    dp = FakeAws(update_ab_test=lambda **kw: {})
    client = _client(dp=dp)
    client.promote_variant(PromotionDecision(strategy="target_based", ab_test_id="ab-9"))
    assert dp.last("update_ab_test") == {"abTestId": "ab-9", "executionStatus": "STOPPED"}


def test_promote_variant_rejects_unknown_strategy():
    with pytest.raises(ValueError):
        _client().promote_variant(PromotionDecision(strategy="magic"))
