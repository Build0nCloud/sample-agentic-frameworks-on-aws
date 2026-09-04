"""Dataset-integrity tests (task 6).

Validates that the shipped datasets parse into the ConversationCase schema, use only
real tool names in expected trajectories, and cover the required scenarios: both
designed trajectories, failure modes, and out-of-scope clinical declines.
Requirements: 2.1, 2.2, 3.6, 5.9, 9.6, 10.4
"""

import json
from pathlib import Path

from agentcore_demo import config as cfg
from agentcore_demo.agent import patient_support_agent as ag
from agentcore_demo.models import ConversationCase

ROOT = Path(__file__).resolve().parent.parent
OFFLINE = ROOT / "datasets" / "offline_multiturn.jsonl"
TRAFFIC = ROOT / "datasets" / "traffic_sessions.jsonl"

_VALID_TOOLS = set(ag.TOOL_NAMES)


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_offline_dataset_parses_into_conversation_cases():
    rows = _read_jsonl(OFFLINE)
    assert len(rows) >= 10
    ids = set()
    for row in rows:
        case = ConversationCase.from_dict(row)
        assert case.case_id and case.case_id not in ids, "case_id must be present and unique"
        ids.add(case.case_id)
        assert case.turns and all(isinstance(t, str) and t for t in case.turns)
        gt = case.ground_truth
        assert gt is not None and gt.assertions, "each case needs ground-truth assertions"
        if gt.expected_trajectory is not None:
            for tool in gt.expected_trajectory:
                assert tool in _VALID_TOOLS, f"unknown tool in trajectory: {tool}"


def test_offline_dataset_has_multiturn_cases():
    rows = _read_jsonl(OFFLINE)
    multiturn = [r for r in rows if len(r["turns"]) >= 2]
    assert len(multiturn) >= 3  # Requirement 2.2


def test_offline_dataset_covers_designed_trajectories():
    trajectories = [tuple(r["ground_truth"].get("expected_trajectory") or []) for r in _read_jsonl(OFFLINE)]
    assert tuple(ag.TRAJECTORY_REFILL) in trajectories
    assert tuple(ag.TRAJECTORY_SCHEDULING) in trajectories


def test_offline_dataset_has_out_of_scope_clinical_case():
    # Requirement 10.4: at least one clinical request the agent must decline
    # (encoded as an empty expected trajectory = no tool calls).
    rows = _read_jsonl(OFFLINE)
    declines = [r for r in rows if r["ground_truth"].get("expected_trajectory") == []]
    assert declines, "expected at least one out-of-scope clinical decline case"
    joined = " ".join(a.lower() for r in declines for a in r["ground_truth"]["assertions"])
    assert "clinician" in joined or "911" in joined or "emergency" in joined


def test_offline_dataset_has_failure_mode_cases():
    ids = {r["case_id"] for r in _read_jsonl(OFFLINE)}
    for expected in ("fail-unknown-patient", "fail-out-of-network", "fail-med-not-on-file", "fail-unavailable-lab-panel"):
        assert expected in ids, f"missing failure-mode case: {expected}"


def test_traffic_dataset_parses_and_is_multiturn_capable():
    rows = _read_jsonl(TRAFFIC)
    assert len(rows) >= 20  # enough volume (with replay) to reach an A/B result
    names = set()
    multiturn = 0
    for row in rows:
        assert row["name"] and row["name"] not in names
        names.add(row["name"])
        assert row["turns"] and all(isinstance(t, str) and t for t in row["turns"])
        if len(row["turns"]) >= 2:
            multiturn += 1
    assert multiturn >= 5


def test_shipped_config_points_at_existing_datasets():
    c = cfg.load_config(cfg.DEFAULT_CONFIG_PATH)
    assert c.offline_dataset == OFFLINE and c.offline_dataset.exists()
    assert c.traffic_dataset == TRAFFIC and c.traffic_dataset.exists()
