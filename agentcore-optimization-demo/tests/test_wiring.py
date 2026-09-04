"""End-to-end wiring test (task 17.1).

Verifies that build_stages produces the full, correctly-ordered set of stages wired to
the real modules (without executing any AWS calls) and that the default CLI builder is
importable/callable. Live execution is covered by the opt-in smoke test (task 17.3).
Requirements: 7.1, 7.3
"""

from pathlib import Path

from agentcore_demo import demo
from agentcore_demo.config import DEFAULT_CONFIG_PATH, load_config
from agentcore_demo.orchestrator import STAGE_SPECS, build_stages, make_context
from agentcore_demo.state import StateStore


def test_build_stages_matches_specs(tmp_path):
    config = load_config(DEFAULT_CONFIG_PATH)
    ctx = make_context(config, StateStore(tmp_path / "artifacts"))
    stages = build_stages(ctx)
    assert [s.name for s in stages] == [s.name for s in STAGE_SPECS]
    assert [s.provisions for s in stages] == [s.provisions for s in STAGE_SPECS]
    assert all(callable(s.run) for s in stages)


def test_default_builder_is_callable():
    # The concrete builder exists and is wired (executed only in live runs).
    assert callable(demo._default_builder)


def test_promotion_decision_roundtrip(tmp_path):
    from agentcore_demo.agentcore_client import PromotionDecision
    from agentcore_demo.models import BundleConfig
    from agentcore_demo.orchestrator import load_promotion_decision, save_promotion_decision

    store = StateStore(tmp_path / "artifacts")
    decision = PromotionDecision(
        strategy="config_bundle", ab_test_id="ab-1", bundle_id="b1", agent_arn="arn:agent",
        config=BundleConfig(system_prompt="winner", tool_descriptions={"t": "d"}), commit_message="promote",
    )
    save_promotion_decision(store, "appr-1", decision)
    loaded = load_promotion_decision(store, "appr-1")
    assert loaded.strategy == "config_bundle" and loaded.bundle_id == "b1"
    assert loaded.config.system_prompt == "winner" and loaded.config.tool_descriptions == {"t": "d"}
    assert load_promotion_decision(store, "missing") is None


def test_agent_state_roundtrip(tmp_path):
    from agentcore_demo.agentcore_client import AgentHandle
    from agentcore_demo.orchestrator import load_agent, save_agent

    store = StateStore(tmp_path / "artifacts")
    assert load_agent(store) is None
    agent = AgentHandle("PatientSupport", "arn:rt", "rt-1", "/lg", "PatientSupport.DEFAULT", "arn:role", "us-east-1")
    save_agent(store, agent)
    loaded = load_agent(store)
    assert loaded.runtime_id == "rt-1" and loaded.service_name == "PatientSupport.DEFAULT"


def test_config_draft_roundtrip_preserves_prompt_model_and_tools(tmp_path):
    from agentcore_demo.models import BundleConfig
    from agentcore_demo.orchestrator import (
        config_drafts_exist,
        load_config_draft,
        save_config_draft,
    )

    store = StateStore(tmp_path / "artifacts")
    cfg = BundleConfig(
        system_prompt="line one\nline two\nline three",
        model_id="us.anthropic.claude-sonnet-5",
        tool_descriptions={"get_x": "desc x", "do_y": "desc y"},
    )
    assert not config_drafts_exist(store)
    save_config_draft(store, "control", cfg)
    save_config_draft(store, "treatment", cfg)
    assert config_drafts_exist(store)

    loaded = load_config_draft(store, "control")
    assert loaded is not None
    assert loaded.system_prompt == cfg.system_prompt  # multi-line preserved
    assert loaded.model_id == cfg.model_id
    assert loaded.tool_descriptions == cfg.tool_descriptions  # carried through, not lost


def test_load_config_draft_missing_returns_none(tmp_path):
    from agentcore_demo.orchestrator import load_config_draft

    store = StateStore(tmp_path / "artifacts")
    assert load_config_draft(store, "treatment") is None


def test_edited_draft_is_what_build_reads(tmp_path):
    """An edit to the draft (e.g. model id) is what a later load returns."""
    from agentcore_demo.models import BundleConfig
    from agentcore_demo.orchestrator import load_config_draft, save_config_draft

    store = StateStore(tmp_path / "artifacts")
    save_config_draft(store, "treatment", BundleConfig(system_prompt="orig", model_id="model-a"))
    # Simulate a UI edit: change model + prompt, keep tools.
    save_config_draft(store, "treatment", BundleConfig(system_prompt="edited prompt", model_id="model-b"))
    loaded = load_config_draft(store, "treatment")
    assert loaded.model_id == "model-b"
    assert loaded.system_prompt == "edited prompt"
