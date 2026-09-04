"""Configuration-bundle tests (task 12.3).

Property 8 (bundle immutability & lineage) plus baseline/ref/offline-check behavior.
Requirements: 4.4, 4.5, 4.6, 4.7
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agentcore_demo.models import BundleConfig, BundleVersion, SessionResult
from agentcore_demo.optimization.bundles import BundleManager, offline_check_bundle


class FakeBundleClient:
    """Returns immutable-looking versions with incrementing ids, echoing parents."""

    def __init__(self):
        self.n = 0

    def create_bundle_version(self, bundle_name, agent_arn, config, commit_message, description=""):
        self.n += 1
        return BundleVersion("b1", f"v{self.n}", [], commit_message, config, bundle_arn="arn:b1")

    def update_bundle_version(self, bundle_id, agent_arn, config, parent_version_ids, commit_message):
        self.n += 1
        return BundleVersion(bundle_id, f"v{self.n}", list(parent_version_ids), commit_message, config, bundle_arn="arn:b1")


# --------------------------------------------------------------------------
# Property 8: Bundle immutability & lineage
# Validates: Requirements 4.4
# --------------------------------------------------------------------------
@given(prompts=st.lists(st.text(min_size=1, max_size=12), min_size=1, max_size=8))
def test_property_bundle_immutability_and_lineage(prompts):
    mgr = BundleManager(FakeBundleClient(), "arn:agent")
    first = mgr.create("Bundle", BundleConfig(system_prompt=prompts[0]), "init")
    assert first.parent_version_ids == []  # first version has no parent
    prev = first.version_id

    for p in prompts[1:]:
        before = mgr.versions("b1")  # snapshot of existing versions
        bv = mgr.update("b1", BundleConfig(system_prompt=p), "update")
        # Lineage: the new version records the previous baseline as its parent.
        assert bv.parent_version_ids == [prev]
        prev = bv.version_id
        # Immutability: previously-existing versions are unchanged.
        after = mgr.versions("b1")
        assert after[: len(before)] == before

    assert len(mgr.versions("b1")) == len(prompts)
    assert mgr.current_version("b1") == prev  # baseline is the latest version


# --------------------------------------------------------------------------
# Baseline / ref / lineage
# --------------------------------------------------------------------------
def test_ref_returns_bundle_ref_for_current_or_specific_version():
    mgr = BundleManager(FakeBundleClient(), "arn:agent")
    mgr.create("Bundle", BundleConfig(system_prompt="a"), "init")
    v2 = mgr.update("b1", BundleConfig(system_prompt="b"), "update")
    ref = mgr.ref("b1")  # current baseline == v2
    assert ref.bundle_arn == "arn:b1" and ref.version_id == v2.version_id and ref.bundle_id == "b1"
    ref_v1 = mgr.ref("b1", "v1")
    assert ref_v1.version_id == "v1"


def test_set_baseline_rejects_unknown_version():
    mgr = BundleManager(FakeBundleClient(), "arn:agent")
    mgr.create("Bundle", BundleConfig(system_prompt="a"), "init")
    with pytest.raises(ValueError):
        mgr.set_baseline("b1", "v999")


def test_set_baseline_moves_current_version():
    mgr = BundleManager(FakeBundleClient(), "arn:agent")
    mgr.create("Bundle", BundleConfig(system_prompt="a"), "init")
    mgr.update("b1", BundleConfig(system_prompt="b"), "update")
    mgr.set_baseline("b1", "v1")
    assert mgr.current_version("b1") == "v1"


def test_lineage_reports_parents():
    mgr = BundleManager(FakeBundleClient(), "arn:agent")
    mgr.create("Bundle", BundleConfig(system_prompt="a"), "init")
    mgr.update("b1", BundleConfig(system_prompt="b"), "update")
    assert mgr.lineage("b1") == [("v1", []), ("v2", ["v1"])]


# --------------------------------------------------------------------------
# Offline check against a candidate bundle (R4.7)
# --------------------------------------------------------------------------
class _FakeSendClient:
    def send_session(self, agent, turns, session_id=None, bundle=None):
        # record the bundle so we can assert it was injected
        self.last_bundle = bundle
        return SessionResult(session_id=session_id or "s", final_response="done", tool_calls=["get_medications"])


def test_offline_check_bundle_runs_with_candidate_bundle():
    from agentcore_demo.agentcore_client import BundleRef
    from agentcore_demo.models import ConversationCase, GroundTruth

    client = _FakeSendClient()
    cases = [ConversationCase("c1", ["hi"], GroundTruth(expected_trajectory=["get_medications"], assertions=["x"]))]
    ref = BundleRef(bundle_arn="arn:b1", version_id="v2", bundle_id="b1")
    result = offline_check_bundle(client, agent=object(), bundle_ref=ref, cases=cases, name="candidate_check")
    assert result.baseline.case_count == 1
    assert result.baseline.config_ref == "bundle:v2"
    assert client.last_bundle is ref  # candidate bundle was injected into traffic
