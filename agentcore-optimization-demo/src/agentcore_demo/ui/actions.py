"""Live actions for the Streamlit front end: approve / reject a promotion.

These are the *only* functions in the UI that mutate state or touch AWS. They reuse
exactly the same code path as ``demo approve`` / ``demo reject`` (the
:class:`PromotionGate` plus the persisted :class:`PromotionDecision`), so approving
from the browser performs the same real AgentCore rollout the CLI would:

    gate.approve(request_id, decider="ui", promotion_decision=<loaded decision>)
    -> client.stop_ab_test(...) + client.promote_variant(...)   # LIVE

Because approve/reject are irreversible against live resources, the app guards these
behind an explicit "live actions" toggle and a typed confirmation; this module simply
executes the decision once the app has confirmed it.
"""

from __future__ import annotations

import dataclasses

from ..agentcore_client import AgentCoreClient
from ..config import load_config
from ..optimization.promotion import PromotionGate
from ..orchestrator import load_promotion_decision
from ..state import StateStore


@dataclasses.dataclass
class ActionResult:
    ok: bool
    message: str


def _gate(config_path: str | None) -> tuple[PromotionGate, StateStore]:
    config = load_config(config_path)
    store = StateStore(config.artifacts_dir)
    client = AgentCoreClient(config)
    return PromotionGate(client, store), store


def approve(request_id: str, *, config_path: str | None = None, decider: str = "ui") -> ActionResult:
    """Approve a pending promotion and roll out the winner (real AgentCore call).

    Mirrors ``demo approve``: loads the saved PromotionDecision for this request and
    hands it to :meth:`PromotionGate.approve`. Returns a structured result the app can
    surface instead of raising.
    """
    gate, store = _gate(config_path)
    decision = load_promotion_decision(store, request_id)
    if decision is None:
        return ActionResult(
            ok=False,
            message=(
                f"No saved promotion decision for {request_id}. Run the promotion-gate "
                "stage (or `demo` run) that created this approval so the rollout target "
                "is recorded before approving."
            ),
        )
    try:
        req = gate.approve(request_id, decider=decider, promotion_decision=decision)
    except Exception as exc:  # noqa: BLE001 - surface any AWS/state error to the UI
        return ActionResult(ok=False, message=f"Approve failed: {exc}")
    return ActionResult(
        ok=True,
        message=f"Approved {request_id}: promoted variant {req.winning_variant} (status {req.status.value}).",
    )


def reject(request_id: str, *, config_path: str | None = None, decider: str = "ui") -> ActionResult:
    """Reject a pending promotion (stops the test, leaves the baseline unchanged)."""
    gate, _store = _gate(config_path)
    try:
        req = gate.reject(request_id, decider=decider)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(ok=False, message=f"Reject failed: {exc}")
    return ActionResult(
        ok=True,
        message=f"Rejected {request_id}: baseline unchanged (status {req.status.value}).",
    )


@dataclasses.dataclass
class ChatResult:
    ok: bool
    text: str
    session_id: str | None = None
    error: str | None = None


def chat_once(prompt: str, *, session_id: str | None = None, config_path: str | None = None) -> ChatResult:
    """Send one turn to the deployed agent and return its response (real runtime call).

    Reuses the same ``AgentCoreClient.send_session`` path as the loop. Passing a stable
    ``session_id`` across turns preserves conversation history (the runtime keys history
    by runtimeSessionId). The agent's SSE-encoded response is decoded for display by the
    caller (``clean_sse_text``). Returns a structured result rather than raising.
    """
    from ..agentcore_client import AgentCoreClient
    from ..config import load_config
    from ..orchestrator import load_agent
    from ..state import StateStore

    config = load_config(config_path)
    store = StateStore(config.artifacts_dir)
    agent = load_agent(store)
    if agent is None:
        return ChatResult(ok=False, text="", error="No deployed agent found. Run the `deploy` stage first.")
    client = AgentCoreClient(config)
    try:
        result = client.send_session(agent, [prompt], session_id=session_id)
    except Exception as exc:  # noqa: BLE001 - surface AWS/creds/runtime errors to the UI
        return ChatResult(ok=False, text="", session_id=session_id, error=f"{type(exc).__name__}: {exc}")
    return ChatResult(ok=True, text=result.final_response, session_id=result.session_id)


def fetch_session_tools(session_id: str, *, config_path: str | None = None) -> list[str]:
    """Reconstruct the ordered tool calls for a chat session from CloudWatch traces.

    Best-effort: trace ingestion lags a bit (~30-90s), so this may return an empty list
    right after a turn even when tools were used. Never raises — returns [] on any error.
    """
    from ..agentcore_client import AgentCoreClient
    from ..config import load_config
    from ..orchestrator import load_agent
    from ..state import StateStore

    try:
        config = load_config(config_path)
        store = StateStore(config.artifacts_dir)
        agent = load_agent(store)
        if agent is None:
            return []
        client = AgentCoreClient(config)
        return client.fetch_session_tool_calls(session_id, agent.spans_log_group)
    except Exception:  # noqa: BLE001 - trace reconstruction degrades gracefully
        return []


def refresh_ab_result(*, config_path: str | None = None) -> ActionResult:
    """Re-poll the live A/B test and re-persist its result to loop_state.

    Online evaluation aggregates asynchronously, so the `ab-test` stage can finish before
    a significant result appears. This re-fetches the current result for the persisted
    A/B test id, recomputes the winner (significant-on-any-metric, falling back to the
    primary evaluator), and saves it — the same thing the CLI snippet does. Reads AWS;
    does not promote anything.
    """
    from ..agentcore_client import AgentCoreClient, ABTestHandle
    from ..config import load_config
    from ..optimization.abtest import determine_winner, determine_winner_any
    from ..orchestrator import load_loop_state, save_loop_state
    from ..state import StateStore

    config = load_config(config_path)
    store = StateStore(config.artifacts_dir)
    loop = load_loop_state(store)
    handle_d = loop.get("ab_handle") or {}
    abid = handle_d.get("ab_test_id")
    if not abid:
        return ActionResult(ok=False, message="No A/B test id in loop_state. Run the `ab-test` stage first.")

    client = AgentCoreClient(config)
    try:
        result = client.get_ab_test(ABTestHandle(ab_test_id=abid, name=handle_d.get("name", abid)))
    except Exception as exc:  # noqa: BLE001 - surface AWS/creds errors to the UI
        return ActionResult(ok=False, message=f"Refresh failed: {exc}")

    winner, significant = determine_winner_any(result.per_variant)
    if not significant:
        primary = (config.evaluators[0] if getattr(config, "evaluators", None) else "Builtin.GoalSuccessRate")
        winner, significant = determine_winner(result.per_variant, primary)
    result.winner, result.significant = winner, significant
    save_loop_state(store, ab_result=result, ab_handle={"ab_test_id": abid, "name": handle_d.get("name", abid)})

    if significant:
        return ActionResult(ok=True, message=f"Refreshed: winner {winner} is significant. Open/reopen an approval to promote.")
    return ActionResult(
        ok=True,
        message="Refreshed, but no significant winner yet — online eval may still be aggregating (~10-15 min). Try again shortly.",
    )


def reopen_approval(*, config_path: str | None = None) -> ActionResult:
    """Open a **fresh** pending approval from the last persisted A/B result.

    Lets you demo the click-to-approve flow again without re-running the whole loop:
    it reuses the exact promotion-gate logic (``gate.evaluate_for_promotion`` +
    ``save_promotion_decision``) against the ``ab_result`` already in
    ``artifacts/loop_state.json``, reconstructing the rollout target from loop state and
    the saved agent handle.

    This touches **no** AWS — it only writes a new approval + decision under
    ``artifacts/`` — so it does not require live actions to be enabled. The subsequent
    ``approve`` is what performs the real rollout.
    """
    from ..agentcore_client import PromotionDecision
    from ..models import ABTestResult, BundleConfig
    from ..orchestrator import (
        load_agent,
        load_loop_state,
        save_promotion_decision,
    )

    gate, store = _gate(config_path)
    loop = load_loop_state(store)
    if not loop.get("ab_result"):
        return ActionResult(
            ok=False,
            message="No A/B result in loop_state.json. Run the loop through the `ab-test` stage first.",
        )

    ab_result = ABTestResult.from_dict(loop["ab_result"])
    if not (ab_result.significant and ab_result.winner):
        return ActionResult(
            ok=False,
            message=(
                "The persisted A/B result has no statistically significant winner, so "
                "there's nothing to promote."
            ),
        )

    agent = load_agent(store)
    agent_ref = getattr(agent, "runtime_name", None) or "PatientSupport"
    agent_arn = getattr(agent, "runtime_arn", None)

    req = gate.evaluate_for_promotion(ab_result, agent_ref=agent_ref)
    if req is None:  # defensive; the significance check above should prevent this
        return ActionResult(ok=False, message="No significant winner; nothing to promote.")

    treatment_config = (
        BundleConfig.from_dict(loop["treatment_config"]) if loop.get("treatment_config") else None
    )
    decision = PromotionDecision(
        strategy="config_bundle",
        ab_test_id=req.ab_test_id,
        bundle_id=loop.get("control_bundle_id"),
        agent_arn=agent_arn,
        config=treatment_config,
        commit_message="Promote treatment: A/B validated recommended configuration",
    )
    save_promotion_decision(store, req.request_id, decision)
    return ActionResult(
        ok=True,
        message=f"Opened fresh pending approval {req.request_id} (winner {req.winning_variant}). Approve it on the Approvals tab.",
    )
