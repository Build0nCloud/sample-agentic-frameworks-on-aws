"""Runtime-helper tests for the patient-support agent (task 4.4).

Covers the multi-turn session cache and the config-bundle hook helpers, which are
importable without the strands / bedrock_agentcore packages.
Requirements: 1.3, 1.5, 4.6, 9.7
"""

from agentcore_demo.agent import patient_support_agent as ag


class _FakeAgent:
    """Minimal stateful stand-in that accumulates turns (proxy for chat history)."""

    def __init__(self):
        self.turns: list[str] = []
        self.system_prompt = None

    def __call__(self, turn: str) -> str:
        self.turns.append(turn)
        return f"echo:{turn}"


def test_session_cache_preserves_same_agent_across_turns():
    cache = ag.SessionAgentCache()
    a1, created1 = cache.get_or_create("s1", _FakeAgent)
    assert created1 is True
    a1("first turn")

    a2, created2 = cache.get_or_create("s1", _FakeAgent)
    assert created2 is False
    assert a2 is a1  # same instance -> conversation history is retained
    assert a2.turns == ["first turn"]


def test_session_cache_distinct_sessions_get_distinct_agents():
    cache = ag.SessionAgentCache()
    a, _ = cache.get_or_create("s1", _FakeAgent)
    b, created = cache.get_or_create("s2", _FakeAgent)
    assert created is True and b is not a
    assert "s1" in cache and "s2" in cache and len(cache) == 2


def test_session_cache_does_not_cache_sessionless_calls():
    cache = ag.SessionAgentCache()
    c1, _ = cache.get_or_create(None, _FakeAgent)
    c2, _ = cache.get_or_create(None, _FakeAgent)
    assert c1 is not c2
    assert len(cache) == 0


def test_resolve_bundle_config_defaults_when_absent():
    sp, td = ag.resolve_bundle_config(None)
    assert sp == ag.DEFAULT_SYSTEM_PROMPT and td == {}
    sp2, td2 = ag.resolve_bundle_config({})
    assert sp2 == ag.DEFAULT_SYSTEM_PROMPT and td2 == {}


def test_resolve_bundle_config_overrides_from_bundle():
    bundle = {"system_prompt": "CUSTOM", "tool_descriptions": {"get_medications": "new desc"}}
    sp, td = ag.resolve_bundle_config(bundle)
    assert sp == "CUSTOM"
    assert td["get_medications"] == "new desc"


def test_apply_tool_descriptions_overrides_known_ignores_unknown():
    specs = {n: {"name": n, "description": ag.DEFAULT_TOOL_DESCRIPTIONS[n]} for n in ag.TOOL_NAMES}
    ag.apply_tool_descriptions(specs, {"get_medications": "NEW", "not_a_tool": "x"})
    assert specs["get_medications"]["description"] == "NEW"
    assert "not_a_tool" not in specs
    # untouched tool keeps its default description
    assert specs["find_provider"]["description"] == ag.DEFAULT_TOOL_DESCRIPTIONS["find_provider"]


def test_apply_tool_descriptions_skips_specs_without_description():
    specs = {"t": {"name": "t"}}  # no description field
    ag.apply_tool_descriptions(specs, {"t": "x"})
    assert "description" not in specs["t"]
