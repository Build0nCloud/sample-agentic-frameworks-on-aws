"""Demo CLI tests (task 16.3).

Covers stage listing, the cost/confirmation gate, dispatch, and halt handling — all
with a fake builder (no AWS).
Requirements: 1.8, 7.6, 8.3, 8.4
"""

import types

from agentcore_demo import demo


class FakeOrch:
    def __init__(self, raises=None):
        self.raises = raises
        self.runs = []

    def run(self, context, only=None):
        self.runs.append(list(only) if only is not None else None)
        if self.raises:
            raise self.raises


def _builder(orch=None, bag=None):
    orch = orch or FakeOrch()
    ctx = types.SimpleNamespace(bag=bag or {})
    calls = {"n": 0}

    def builder(args):
        calls["n"] += 1
        return orch, ctx

    builder.calls = calls  # type: ignore[attr-defined]
    builder.orch = orch     # type: ignore[attr-defined]
    return builder


def _run(argv, builder, inputs="", ):
    out = []
    rc = demo.main(argv, builder=builder, emit=out.append, input_fn=lambda _p: inputs)
    return rc, "\n".join(out)


# --------------------------------------------------------------------------
# Listing (R1.8)
# --------------------------------------------------------------------------
def test_no_command_lists_stages():
    b = _builder()
    rc, out = _run([], b)
    assert rc == 0
    assert "Demo stages" in out and "offline-baseline" in out and "promotion-gate" in out
    assert b.calls["n"] == 0  # listing does not build/provision


def test_list_command():
    rc, out = _run(["list"], _builder())
    assert rc == 0 and "run-all" in out


# --------------------------------------------------------------------------
# Confirmation gate (R8.3, R8.4)
# --------------------------------------------------------------------------
def test_provisioning_stage_aborts_without_confirmation():
    b = _builder()
    rc, out = _run(["deploy"], b, inputs="n")
    assert rc == 2
    assert "REAL Amazon Bedrock AgentCore" in out and "Aborted" in out
    assert b.calls["n"] == 0  # never built -> no resources created


def test_provisioning_stage_proceeds_with_yes_flag():
    b = _builder()
    rc, out = _run(["--yes", "deploy"], b)
    assert rc == 0
    assert b.calls["n"] == 1
    assert b.orch.runs == [["deploy"]]


def test_provisioning_stage_proceeds_with_interactive_yes():
    b = _builder()
    rc, _ = _run(["deploy"], b, inputs="y")
    assert rc == 0 and b.orch.runs == [["deploy"]]


def test_non_provisioning_stage_needs_no_confirmation():
    b = _builder()
    rc, out = _run(["recommend"], b)  # recommend does not provision
    assert rc == 0
    assert "REAL Amazon Bedrock AgentCore" not in out
    assert b.orch.runs == [["recommend"]]


def test_run_all_confirmed_runs_full_loop():
    b = _builder()
    rc, _ = _run(["--yes", "run-all"], b)
    assert rc == 0
    assert b.orch.runs[0] == list(demo.RUN_ALL_STAGES)


def test_run_all_reports_failure_as_exit_1():
    orch = FakeOrch(raises=RuntimeError("stage blew up"))
    b = _builder(orch=orch)
    rc, out = _run(["--yes", "run-all"], b)
    assert rc == 1 and "Run failed" in out


# --------------------------------------------------------------------------
# Approve / reject / status
# --------------------------------------------------------------------------
class FakeGate:
    def __init__(self):
        self.approved = []
        self.rejected = []
        self._pending = []

    def approve(self, request_id, decider, promotion_decision):
        self.approved.append((request_id, decider, promotion_decision))

    def reject(self, request_id, decider):
        self.rejected.append((request_id, decider))

    def pending(self):
        return self._pending


def test_approve_invokes_gate():
    gate = FakeGate()
    b = _builder(bag={"promotion_gate": gate, "promotion_decision": "DECISION"})
    rc, out = _run(["approve", "appr-1"], b)
    assert rc == 0 and gate.approved == [("appr-1", "cli", "DECISION")]
    assert "Approved" in out


def test_reject_invokes_gate():
    gate = FakeGate()
    b = _builder(bag={"promotion_gate": gate})
    rc, out = _run(["reject", "appr-2"], b)
    assert rc == 0 and gate.rejected == [("appr-2", "cli")]


def test_promotion_status_reports_no_pending():
    b = _builder(bag={"promotion_gate": FakeGate()})
    rc, out = _run(["promotion-status"], b)
    assert rc == 0 and "No pending" in out
