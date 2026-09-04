"""Online evaluation tests (task 11.3).

Covers sampling config, start/stop lifecycle, aggregation, quality trend, low-score
inspection, and the real-traffic driver — all against fakes (no AWS).
Requirements: 3.2, 3.3, 3.4, 3.5, 3.6, 3.7
"""

import json
import types

from agentcore_demo.agentcore_client import AgentCoreClient, AgentHandle, OnlineEvalConfig
from agentcore_demo.evaluation import online
from agentcore_demo.evaluation.online import (
    OnlineEvaluation,
    SessionScore,
    aggregate_online_scores,
    drive_traffic,
    low_scoring_sessions,
    quality_trend,
)


class _Body:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text.encode("utf-8")


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

    def count(self, name):
        return sum(1 for n, _ in self.calls if n == name)


def _client(dp=None, ctrl=None):
    return AgentCoreClient(types.SimpleNamespace(region="us-east-1"), dp=dp or FakeAws(), ctrl=ctrl or FakeAws(), sleep=lambda _s: None)


def _agent():
    return AgentHandle("PatientSupport", "arn:rt", "rt-1", "/lg", "PatientSupport.DEFAULT", "arn:role", "us-east-1")


def _cfg(sampling=100.0):
    return OnlineEvalConfig(
        name="OnlineEval", service_name="PatientSupport.DEFAULT", log_groups=["/lg"],
        evaluators=["Builtin.GoalSuccessRate"], role_arn="arn:role", sampling_percentage=sampling,
    )


# --------------------------------------------------------------------------
# Lifecycle + sampling (R3.2, R3.7)
# --------------------------------------------------------------------------
def test_start_passes_sampling_percentage():
    ctrl = FakeAws(create_online_evaluation_config=lambda **kw: {"onlineEvaluationConfigId": "oe-1", "onlineEvaluationConfigArn": "arn:oe"})
    oe = OnlineEvaluation(_client(ctrl=ctrl), _cfg(sampling=25))
    handle = oe.start()
    assert handle.config_id == "oe-1"
    rule = ctrl.last("create_online_evaluation_config")["rule"]
    assert rule["samplingConfig"]["samplingPercentage"] == 25.0


def test_stop_disables_config():
    ctrl = FakeAws(
        create_online_evaluation_config=lambda **kw: {"onlineEvaluationConfigId": "oe-1", "onlineEvaluationConfigArn": "arn:oe"},
        update_online_evaluation_config=lambda **kw: {},
    )
    oe = OnlineEvaluation(_client(ctrl=ctrl), _cfg())
    oe.start()
    oe.stop()
    assert ctrl.last("update_online_evaluation_config")["executionStatus"] == "DISABLED"


# --------------------------------------------------------------------------
# Aggregation / trend / inspection (R3.3, R3.4, R3.5)
# --------------------------------------------------------------------------
def _scores():
    return [
        SessionScore("s1", "2026-01-01T00:00:00Z", {"g": 0.9, "h": 0.8}, inputs=["hi"], output="ok", tool_calls=["a"]),
        SessionScore("s2", "2026-01-01T00:01:00Z", {"g": 0.5, "h": 0.4}, inputs=["yo"], output="meh", tool_calls=[]),
        SessionScore("s3", "2026-01-01T00:02:00Z", {"g": 0.7}, inputs=["hey"], output="fine"),
    ]


def test_aggregate_online_scores():
    agg = aggregate_online_scores(_scores())
    assert round(agg["g"], 3) == round((0.9 + 0.5 + 0.7) / 3, 3)
    assert round(agg["h"], 3) == round((0.8 + 0.4) / 2, 3)


def test_quality_trend_ordered_with_running_mean():
    trend = quality_trend(_scores(), "g")
    assert [p["session_id"] for p in trend] == ["s1", "s2", "s3"]  # time-ordered
    assert trend[0]["running_mean"] == 0.9
    assert round(trend[1]["running_mean"], 3) == 0.7  # (0.9+0.5)/2
    assert round(trend[2]["running_mean"], 3) == 0.7  # (0.9+0.5+0.7)/3


def test_low_scoring_sessions_by_evaluator_and_min():
    low_g = low_scoring_sessions(_scores(), 0.6, evaluator="g")
    assert [ss.session_id for ss in low_g] == ["s2"]
    low_min = low_scoring_sessions(_scores(), 0.6)  # min across evaluators
    assert [ss.session_id for ss in low_min] == ["s2"]  # s2 min 0.4 <= 0.6; s1 min 0.8; s3 min 0.7


def test_summary_composes_views():
    provider = lambda ids: _scores()
    oe = OnlineEvaluation(_client(), _cfg(), score_provider=provider)
    summary = oe.summary(["s1", "s2", "s3"], low_threshold=0.6, evaluator="g")
    assert summary["session_count"] == 3
    assert "g" in summary["aggregate_scores"]
    assert summary["low_scoring"][0]["session_id"] == "s2"
    assert summary["low_scoring"][0]["tool_calls"] == []


# --------------------------------------------------------------------------
# Real-traffic driver (R3.6)
# --------------------------------------------------------------------------
def test_drive_traffic_replays_with_unique_sessions():
    dp = FakeAws(invoke_agent_runtime=lambda **kw: {"response": _Body("resp")})
    client = _client(dp=dp)
    sessions = [{"name": "a", "turns": ["hi", "again"]}, {"name": "b", "turns": ["yo"]}]
    results = drive_traffic(client, _agent(), sessions, loops=2)
    assert len(results) == 4  # 2 sessions x 2 loops
    assert len({r.session_id for r in results}) == 4  # all unique
    # multi-turn session "a" -> 2 invokes per occurrence; total invokes = (2 + 1) * 2 = 6
    assert dp.count("invoke_agent_runtime") == 6


def test_load_traffic_reads_shipped_dataset():
    from agentcore_demo import config as cfg
    c = cfg.load_config(cfg.DEFAULT_CONFIG_PATH)
    sessions = online.load_traffic(c.traffic_dataset)
    assert len(sessions) >= 20 and all("turns" in s for s in sessions)
