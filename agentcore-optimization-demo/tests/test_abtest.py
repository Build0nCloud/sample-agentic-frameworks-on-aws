"""A/B testing tests (tasks 13.4, 13.5).

Property 4 (significance gating) plus Welch's t-test statistics, winner detection, and
the A/B lifecycle + traffic driver (all against fakes).
Requirements: 5.1, 5.2, 5.3, 5.4, 5.6, 5.7, 5.8, 6.1
"""

import types

from hypothesis import given
from hypothesis import strategies as st

from agentcore_demo.agentcore_client import AgentCoreClient, AgentHandle, BundleRef
from agentcore_demo.models import EvalMetric
from agentcore_demo.optimization import abtest
from agentcore_demo.optimization.abtest import (
    ABTestRunner,
    compute_ab_result,
    compute_metric,
    t_ppf_975,
    two_sided_p,
    welch_t_test,
)


class FakeAws:
    def __init__(self, **responses):
        self.calls = []
        self._responses = responses

    def __getattr__(self, name):
        def method(**kwargs):
            self.calls.append((name, kwargs))
            r = self._responses.get(name)
            return r(**kwargs) if callable(r) else ({} if r is None else r)

        return method

    def last(self, name):
        for n, kw in reversed(self.calls):
            if n == name:
                return kw
        raise AssertionError(f"{name} not called")


def _client(dp=None):
    return AgentCoreClient(types.SimpleNamespace(region="us-east-1"), dp=dp or FakeAws(), ctrl=FakeAws(), sleep=lambda _s: None)


def _agent():
    return AgentHandle("PatientSupport", "arn:rt", "rt-1", "/lg", "PatientSupport.DEFAULT", "arn:role", "us-east-1")


# ==========================================================================
# Statistics (13.5)
# ==========================================================================
def test_t_ppf_975_known_values():
    assert abs(t_ppf_975(10) - 2.228) < 0.02
    assert abs(t_ppf_975(1_000_000) - 1.96) < 0.02


def test_welch_similar_samples_not_significant():
    c = [0.50, 0.55, 0.45, 0.50, 0.52]
    t = [0.51, 0.49, 0.53, 0.50, 0.48]
    _t, _df, p, _se = welch_t_test(c, t)
    assert p > 0.05


def test_welch_separated_samples_significant():
    c = [0.40, 0.45, 0.50, 0.42, 0.48]
    t = [0.85, 0.90, 0.88, 0.86, 0.90]
    _t, _df, p, _se = welch_t_test(c, t)
    assert p < 0.05


def test_two_sided_p_symmetric_and_bounded():
    assert two_sided_p(0.0, 10) == 1.0
    assert 0.0 <= two_sided_p(3.0, 10) <= 1.0


def test_compute_metric_significant_improvement():
    c = [0.40, 0.45, 0.50, 0.42, 0.48]
    t = [0.85, 0.90, 0.88, 0.86, 0.90]
    m = compute_metric(c, t)
    assert m.significant is True and m.pct_change > 0 and m.p_value < 0.05
    assert abs(m.mean - sum(t) / len(t)) < 1e-9


def test_compute_metric_regression_not_significant_winner():
    c = [0.85, 0.90, 0.88, 0.86, 0.90]
    t = [0.40, 0.45, 0.50, 0.42, 0.48]
    m = compute_metric(c, t)
    assert m.significant is False  # treatment is worse -> not a significant improvement


# ==========================================================================
# Property 4: Significance gating
# Validates: Requirements 6.1, 5.6
# ==========================================================================
_samples = st.lists(st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False), min_size=2, max_size=15)


@given(control=_samples, treatment=_samples)
def test_property_significance_gating(control, treatment):
    result = compute_ab_result("ab", {"g": control}, {"g": treatment})
    # A winner exists iff the result is significant.
    assert (result.winner is not None) == result.significant
    if result.significant:
        m = result.per_variant["treatment"]["g"]
        assert m.p_value < 0.05 and m.pct_change > 0
    else:
        assert result.winner is None


def test_compute_ab_result_clear_winner():
    control = {"g": [0.40, 0.45, 0.50, 0.42, 0.48, 0.44]}
    treatment = {"g": [0.85, 0.90, 0.88, 0.86, 0.90, 0.87]}
    result = compute_ab_result("ab-1", control, treatment)
    assert result.significant is True and result.winner == "treatment"


def test_determine_winner_any_promotes_on_one_significant_metric_no_regression():
    from agentcore_demo.optimization.abtest import determine_winner_any
    per_variant = {
        "control": {"Helpfulness": EvalMetric(mean=0.60), "Traj": EvalMetric(mean=0.0)},
        "T1": {
            "Helpfulness": EvalMetric(mean=0.88, pct_change=46.0, p_value=0.0005, significant=True),
            "Traj": EvalMetric(mean=1.0, pct_change=100.0, p_value=1.0, significant=False),  # perfect split, no p-value
        },
    }
    winner, sig = determine_winner_any(per_variant)
    assert winner == "T1" and sig is True


def test_determine_winner_any_blocks_on_significant_regression():
    from agentcore_demo.optimization.abtest import determine_winner_any
    per_variant = {
        "control": {"A": EvalMetric(mean=0.5), "B": EvalMetric(mean=0.9)},
        "T1": {
            "A": EvalMetric(mean=0.8, pct_change=60.0, p_value=0.01, significant=True),   # better
            "B": EvalMetric(mean=0.6, pct_change=-33.0, p_value=0.01, significant=True),  # significantly worse
        },
    }
    winner, sig = determine_winner_any(per_variant)
    assert winner is None and sig is False


def test_compute_ab_result_no_winner_when_equal():
    control = {"g": [0.5, 0.52, 0.48, 0.51, 0.49]}
    treatment = {"g": [0.5, 0.49, 0.51, 0.5, 0.5]}
    result = compute_ab_result("ab-2", control, treatment)
    assert result.significant is False and result.winner is None


# ==========================================================================
# Lifecycle (13.1)
# ==========================================================================
def test_start_config_bundle_test():
    dp = FakeAws(create_ab_test=lambda **kw: {"abTestId": "ab-1"})
    runner = ABTestRunner(_client(dp))
    handle = runner.start_config_bundle_test(
        "BundleAB", gateway_arn="arn:gw", role_arn="arn:role", online_eval_arn="arn:oe",
        control=BundleRef("arn:c", "v1"), treatment=BundleRef("arn:t", "v1"),
    )
    assert handle.ab_test_id == "ab-1"
    variants = dp.last("create_ab_test")["variants"]
    assert [v["name"] for v in variants] == ["C", "T1"]  # AgentCore requires (C|T1)
    assert variants[0]["variantConfiguration"]["configurationBundle"]["bundleArn"] == "arn:c"


def test_start_target_based_test_canary_weights():
    dp = FakeAws(create_ab_test=lambda **kw: {"abTestId": "ab-2"})
    runner = ABTestRunner(_client(dp))
    runner.start_target_based_test(
        "TargetAB", gateway_arn="arn:gw", role_arn="arn:role", online_eval_arn="arn:oe",
        control_target="v1", treatment_target="v2",
    )
    variants = dp.last("create_ab_test")["variants"]
    assert variants[0]["weight"] == 90 and variants[1]["weight"] == 10
    assert variants[1]["variantConfiguration"]["gatewayTarget"]["targetName"] == "v2"


def test_poll_and_stop():
    dp = FakeAws(
        get_ab_test=lambda **kw: {"results": {"analysisTimestamp": "t", "evaluatorMetrics": [
            {"evaluatorArn": "arn:.../Builtin.GoalSuccessRate", "controlStats": {"mean": 0.7},
             "variantResults": [{"name": "T1", "mean": 0.85, "pValue": 0.01, "isSignificant": True}]}
        ]}},
        update_ab_test=lambda **kw: {},
    )
    runner = ABTestRunner(_client(dp))
    from agentcore_demo.agentcore_client import ABTestHandle
    handle = ABTestHandle("ab-1", "BundleAB")
    result = runner.poll(handle)
    assert result.significant is True
    runner.stop(handle)
    assert dp.last("update_ab_test")["executionStatus"] == "STOPPED"


# ==========================================================================
# Traffic driver (13.3) — sticky assignment
# ==========================================================================
class _SendClient:
    def __init__(self):
        self.sent = []  # (session_id, bundle)

    def send_session(self, agent, turns, session_id=None, bundle=None):
        self.sent.append((session_id, bundle))
        from agentcore_demo.models import SessionResult
        return SessionResult(session_id or "s", "ok", [])


def test_split_and_drive_assigns_all_sessions_stickily():
    client = _SendClient()
    runner = ABTestRunner(client)
    sessions = [{"name": f"s{i}", "turns": ["hi"]} for i in range(10)]
    refs = {"control": BundleRef("arn:c", "v1"), "treatment": BundleRef("arn:t", "v2")}
    weights = [("control", 50), ("treatment", 50)]
    assignments = runner.split_and_drive(_agent(), sessions, refs, weights, loops=2)

    total = sum(len(v) for v in assignments.values())
    assert total == 20  # 10 sessions x 2 loops
    assert len(client.sent) == 20
    # Every session was sent with the bundle of its assigned variant, and assignment
    # is consistent with the sticky hash.
    sent_by_id = {sid: bundle for sid, bundle in client.sent}
    for variant, sids in assignments.items():
        for sid in sids:
            assert sent_by_id[sid] is refs[variant]
            assert abtest.assign_variant(sid, weights) == variant  # sticky/deterministic
