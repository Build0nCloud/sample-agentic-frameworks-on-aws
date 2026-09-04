"""Streamlit front end for the AgentCore optimization demo.

A presentation layer over the ``artifacts/`` tree with two jobs:

1. **Show results** — the A/B test comparison (control vs treatment across evaluators),
   the AI recommendation that produced the treatment, and the append-only audit trail.
2. **Human approval** — surface pending promotion approvals and let a human
   approve/reject them. Approve/reject drive the same :class:`PromotionGate` code path
   as ``demo approve``/``demo reject`` and perform a *real* AgentCore rollout, so they
   are guarded behind an explicit "Enable live actions" toggle plus a typed
   confirmation.

Run with::

    pip install -e ".[ui]"
    demo-ui                 # or: streamlit run src/agentcore_demo/ui/app.py

All artifact parsing lives in ``data.py``; all live mutations live in ``actions.py``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as st_components

# Streamlit runs this file as a top-level script (module name "__main__"), so relative
# imports (from ..models import ...) would fail with "attempted relative import with no
# known parent package". Make the installed/src package importable, then use absolute
# imports so the app works via `demo-ui`, `streamlit run app.py`, or a direct run.
if __package__ in (None, ""):
    _SRC_ROOT = Path(__file__).resolve().parents[2]  # .../src
    if str(_SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(_SRC_ROOT))

from agentcore_demo.models import ApprovalStatus
from agentcore_demo.orchestrator import RUN_ALL_STAGES, STAGE_SPECS
from agentcore_demo.ui import actions, runner
from agentcore_demo.ui.data import ABView, DemoData, clean_sse_text

CONFIG_PATH = os.environ.get("AGENTCORE_DEMO_CONFIG") or None

STATUS_ICON = {
    ApprovalStatus.PENDING_APPROVAL: "🟡",
    ApprovalStatus.APPROVED: "🟢",
    ApprovalStatus.PROMOTING: "🔵",
    ApprovalStatus.PROMOTED: "✅",
    ApprovalStatus.REJECTED: "🔴",
}

STAGE_STATUS_ICON = {
    "pending": "⚪",
    "running": "🔄",
    "ok": "✅",
    "failed": "❌",
}

# How often (seconds) the Run tab re-polls the background runner while a run is active.
RUN_POLL_SECONDS = 2

# UI-only grouping: present some orchestrator stages as a single combined step in the
# Run tab. The underlying stages still run separately (and in canonical order); this
# only affects how the controls and checklist are labeled. Any stage not listed here is
# its own group. `bundle` + `offline-check` are combined into "Build & check candidate".
STAGE_GROUPS: dict[str, list[str]] = {
    "build-bundles + offline-check": ["build-bundles", "offline-check"],
}


def _stage_to_group() -> dict[str, str]:
    """Reverse map: underlying stage name -> its group label."""
    out: dict[str, str] = {}
    for label, members in STAGE_GROUPS.items():
        for m in members:
            out[m] = label
    return out


def _group_labels_in_order() -> list[str]:
    """Group labels in canonical stage order (a group appears at its first member)."""
    to_group = _stage_to_group()
    labels: list[str] = []
    for spec in STAGE_SPECS:
        label = to_group.get(spec.name, spec.name)
        if label not in labels:
            labels.append(label)
    return labels


def _expand_groups(labels: list[str]) -> list[str]:
    """Expand selected group labels back to underlying stage names, in canonical order."""
    wanted: set[str] = set()
    for label in labels:
        wanted.update(STAGE_GROUPS.get(label, [label]))
    return [s.name for s in STAGE_SPECS if s.name in wanted]


def _group_status(members: list[str], stage_status: dict[str, str]) -> str:
    """Combined status for a group: failed > running > pending(any) > ok."""
    statuses = [stage_status.get(m, "pending") for m in members]
    if "failed" in statuses:
        return "failed"
    if "running" in statuses:
        return "running"
    if any(s == "pending" for s in statuses):
        # partially done but none running -> treat as running-in-spirit if some ok
        return "running" if any(s == "ok" for s in statuses) else "pending"
    return "ok"


# ---------------------------------------------------------------------------
# Data loading (cached so reruns don't re-read the disk every keystroke).
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_data() -> DemoData:
    return DemoData(CONFIG_PATH)


def _refresh() -> None:
    """Drop caches so the next render reads fresh artifacts (after an action)."""
    st.cache_resource.clear()
    st.cache_data.clear()


def _fmt(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _fmt_p(p: float) -> str:
    if p is None:
        return "—"
    if p < 0.001:
        return "<0.001"
    return f"{p:.4f}"


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
def render_header(data: DemoData) -> None:
    st.title("AgentCore Optimization Demo")
    st.caption("Healthcare patient-support agent · offline + online eval → A/B test → human-approved promotion")
    c1, c2, c3 = st.columns(3)
    c1.metric("Region", data.region or "—")
    c2.metric("Runtime", data.runtime_name or "—")
    c3.metric("Model", data.model_id or "—")


def _render_ab_refresh(data: DemoData) -> None:
    """Re-poll the live A/B test and re-persist (online eval aggregates asynchronously)."""
    live_enabled = st.session_state.get("live_enabled", False)
    has_test = bool((data.loop_state().get("ab_handle") or {}).get("ab_test_id"))
    if not has_test:
        return
    cols = st.columns([1, 3])
    if cols[0].button("🔄 Refresh A/B result", disabled=not live_enabled, use_container_width=True):
        with st.spinner("Re-polling the live A/B test…"):
            result = actions.refresh_ab_result(config_path=CONFIG_PATH)
        _report(result)
    cols[1].caption(
        "Online evaluation aggregates asynchronously (~10-15 min). If the stage finished "
        "before a winner appeared, refresh to re-fetch and re-persist the latest result."
        + ("" if live_enabled else " Enable live actions in the sidebar to use this.")
    )


def render_results(data: DemoData) -> None:
    view = data.ab_view()
    if view is None:
        st.info("No A/B result found yet. Run the loop through the `ab-test` stage to populate results.")
        _render_ab_refresh(data)
        return

    st.subheader("A/B test result")
    top = st.columns(3)
    top[0].metric("A/B test", view.ab_test_id)
    top[1].metric("Winner", view.winner or "—")
    top[2].metric("Significant", "Yes" if view.significant else "No")

    _render_ab_refresh(data)

    sig = view.significant_evaluators
    if view.significant:
        drivers = ", ".join(sig) if sig else "—"
        st.success(f"Treatment **{view.treatment_variant}** beat control **{view.control_variant}** "
                   f"— significant on: {drivers}")
    else:
        st.warning("No statistically significant winner recorded.")

    _render_metric_table(view)
    st.caption("↩ See the **Recommendation** tab for the prompt change that produced the treatment.")


def _render_metric_table(view: ABView) -> None:
    st.markdown(f"**Per-evaluator comparison** (control `{view.control_variant}` vs treatment `{view.treatment_variant}`)")
    rows = []
    for r in view.rows:
        rows.append(
            {
                "Evaluator": r.evaluator,
                "Control": _fmt(r.control_mean),
                "Treatment": _fmt(r.treatment_mean),
                "Δ (abs)": f"{r.abs_change:+.3f}",
                "Δ (%)": f"{r.pct_change:+.1f}%",
                "p-value": _fmt_p(r.p_value),
                "Significant": "✅" if r.significant else "",
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)

    # A simple visual: treatment vs control means per evaluator.
    chart_data = {
        r.evaluator: {"control": r.control_mean or 0.0, "treatment": r.treatment_mean or 0.0}
        for r in view.rows
    }
    if chart_data:
        st.bar_chart(chart_data, stack=False)


def _render_reopen_control(data: DemoData) -> None:
    """Offer to open a fresh pending approval from the last A/B result (demo helper).

    Only useful when there's a significant A/B result but no request is currently
    pending (e.g. a previous approval was already promoted). Reopening writes a new
    approval locally and does not touch AWS — it just lets you demo the approve click
    again without re-running the loop.
    """
    ab = data.ab_result()
    has_pending = bool(data.pending_approvals())
    can_reopen = ab is not None and ab.significant and ab.winner and not has_pending
    if not can_reopen:
        return
    with st.container(border=True):
        st.markdown("**Demo helper — reopen an approval**")
        st.caption(
            "No approval is currently pending, but the last A/B result has a significant "
            f"winner (**{ab.winner}**). Open a fresh pending approval to demo the "
            "approve flow again."
        )
        if st.button("↻ Reopen pending approval", use_container_width=False):
            result = actions.reopen_approval(config_path=CONFIG_PATH)
            _report(result)


def render_approvals(data: DemoData) -> None:
    st.subheader("Promotion approvals")

    live_enabled = st.session_state.get("live_enabled", False)
    if not live_enabled:
        st.info(
            "Approve promotes the winner (stops the A/B test and routes 100% of traffic "
            "to it); reject keeps the current baseline. Enable live actions in the sidebar "
            "to make a decision."
        )

    _render_reopen_control(data)

    approvals = data.approvals()
    if not approvals:
        st.info("No approval requests yet. The `promotion-gate` stage opens one when the A/B test has a significant winner.")
        return

    pending = [a for a in approvals if a.status == ApprovalStatus.PENDING_APPROVAL]
    st.caption(f"{len(approvals)} total · {len(pending)} pending decision")

    for req in approvals:
        icon = STATUS_ICON.get(req.status, "•")
        is_pending = req.status == ApprovalStatus.PENDING_APPROVAL
        with st.expander(f"{icon} {req.request_id} · {req.status.value} · winner {req.winning_variant}", expanded=is_pending):
            meta = st.columns(3)
            meta[0].write(f"**A/B test:** `{req.ab_test_id}`")
            meta[1].write(f"**Agent:** `{req.agent_ref}`")
            meta[2].write(f"**Created:** {data.fmt_ts(req.created_at)}")
            if req.decided_at:
                st.write(f"**Decided:** {data.fmt_ts(req.decided_at)}")

            if req.metrics:
                st.markdown("**Metrics at request**")
                st.dataframe(
                    [
                        {
                            "Evaluator": name,
                            "Mean": _fmt(m.mean),
                            "Δ (abs)": f"{m.abs_change:+.3f}",
                            "p-value": _fmt_p(m.p_value),
                            "Significant": "✅" if m.significant else "",
                        }
                        for name, m in req.metrics.items()
                    ],
                    use_container_width=True,
                    hide_index=True,
                )

            if is_pending:
                _render_decision_controls(data, req.request_id, live_enabled)
            else:
                st.caption("This request has already been decided — no further action.")


def _render_decision_controls(data: DemoData, request_id: str, live_enabled: bool) -> None:
    if not data.has_promotion_decision(request_id):
        st.warning(
            "No saved promotion decision for this request, so approve can't roll out a "
            "target. Re-run the loop's `promotion-gate` stage to record it."
        )

    st.markdown("**Human decision**")
    confirm = st.text_input(
        "Type the request id to confirm a decision",
        key=f"confirm_{request_id}",
        placeholder=request_id,
        disabled=not live_enabled,
    )
    confirmed = confirm.strip() == request_id
    cols = st.columns(2)
    approve_clicked = cols[0].button(
        "Approve & promote",
        key=f"approve_{request_id}",
        type="primary",
        disabled=not (live_enabled and confirmed),
        use_container_width=True,
    )
    reject_clicked = cols[1].button(
        "Reject",
        key=f"reject_{request_id}",
        disabled=not (live_enabled and confirmed),
        use_container_width=True,
    )

    if approve_clicked:
        with st.spinner("Promoting winner via AgentCore…"):
            result = actions.approve(request_id, config_path=CONFIG_PATH)
        _report(result)
    elif reject_clicked:
        with st.spinner("Rejecting and stopping the A/B test…"):
            result = actions.reject(request_id, config_path=CONFIG_PATH)
        _report(result)


def _report(result: actions.ActionResult) -> None:
    if result.ok:
        st.success(result.message)
        _refresh()
        st.rerun()
    else:
        st.error(result.message)


def render_audit(data: DemoData) -> None:
    st.subheader("Audit trail")
    entries = data.audit_entries()
    if not entries:
        st.info("No audit entries yet. Each approve/reject appends one immutable record.")
        return
    st.caption(f"{len(entries)} decision(s), newest first")
    st.dataframe(
        [
            {
                "Timestamp": data.fmt_ts(e.timestamp),
                "Request": e.request_id,
                "Decision": e.decision.value,
                "Decider": e.decider,
                "Outcome": e.outcome,
            }
            for e in entries
        ],
        use_container_width=True,
        hide_index=True,
    )


def render_run(data: DemoData) -> None:
    st.subheader("Run the optimization loop")
    snap = runner.snapshot()
    live_enabled = st.session_state.get("live_enabled", False)

    st.caption(
        "Runs the loop stages in the background and streams progress here. The loop stops "
        "at `promotion-gate`, which opens a pending approval — you then decide it on the "
        "**Approvals** tab. A full run takes ~25-40 min; individual stages are much faster."
    )

    # --- launch controls (hidden/disabled while a run is active) ---------
    if not snap.running:
        _render_run_controls(data, live_enabled)
    else:
        st.info(f"Run in progress since {snap.started_at}. Controls are disabled until it finishes.")

    # --- stage checklist (grouped) ---------------------------------------
    if snap.stages:
        st.markdown("**Stages**")
        to_group = _stage_to_group()
        shown: set[str] = set()
        for name in snap.stages:
            label = to_group.get(name, name)
            if label in shown:
                continue
            shown.add(label)
            members = STAGE_GROUPS.get(label, [name])
            # Only consider members that are actually part of this run.
            members = [m for m in members if m in snap.stages]
            status = _group_status(members, snap.stage_status) if len(members) > 1 else snap.stage_status.get(name, "pending")
            icon = STAGE_STATUS_ICON.get(status, "⚪")
            line = f"{icon} `{label}` — {status}"
            if status == "running":
                line += "  ⏳"
            st.write(line)

    # --- result banner ---------------------------------------------------
    if snap.finished_at and not snap.running:
        if snap.ok:
            st.success(f"Run finished at {snap.finished_at}. All selected stages completed.")
        else:
            st.error(f"Run stopped at {snap.finished_at}: {snap.error or 'unknown error'}")

    # --- live log --------------------------------------------------------
    if snap.log:
        st.markdown("**Progress log**")
        st.code("\n".join(snap.log), language="text")

    # --- auto-refresh while running --------------------------------------
    if snap.running:
        import time as _t

        _t.sleep(RUN_POLL_SECONDS)
        st.rerun()


def _render_run_controls(data: DemoData, live_enabled: bool) -> None:
    group_labels = _group_labels_in_order()
    selected_labels = st.multiselect(
        "Steps to run (in order)",
        options=group_labels,
        default=group_labels,
        help=(
            "Deselect to run a subset. Steps always execute in the canonical order. "
            "'bundle + offline-check' runs both stages together (package the candidate, "
            "then validate it offline)."
        ),
    )
    selected = _expand_groups(selected_labels)
    can_run = live_enabled and bool(selected)
    if not live_enabled:
        st.info("Enable **live actions** in the sidebar to start a run (requires valid AWS credentials).")

    cols = st.columns([1, 1, 2])
    run_all_clicked = cols[0].button(
        "▶ Run all stages",
        type="primary",
        disabled=not live_enabled,
        use_container_width=True,
    )
    run_sel_clicked = cols[1].button(
        "▶ Run selected",
        disabled=not can_run,
        use_container_width=True,
    )
    if runner.snapshot().finished_at:
        if cols[2].button("Clear last run", use_container_width=True):
            runner.reset()
            st.rerun()

    stages_to_run = list(RUN_ALL_STAGES) if run_all_clicked else (selected if run_sel_clicked else None)
    if stages_to_run:
        started, msg = runner.start_run(stages_to_run, config_path=CONFIG_PATH)
        if started:
            st.success(msg)
            st.rerun()
        else:
            st.error(msg)


CHAT_SUGGESTIONS = [
    "I'm patient PT-1001. What medications am I on?",
    "Refill my Lisinopril.",
    "I'm PT-1001 — can you book me a cardiology appointment?",
    "My blood pressure is 150/95, should I double my dose?",
]


def render_chat(data: DemoData) -> None:
    st.subheader("Chat with the agent")
    st.caption(
        "Talk to the **live** deployed agent in real time — proof it's a real runtime, not a "
        "recording. Messages in one conversation share a session, so the agent remembers "
        "context across turns."
    )

    live_enabled = st.session_state.get("live_enabled", False)
    agent_name = data.runtime_name or "—"
    st.caption(f"Runtime: `{agent_name}` · region `{data.region or '—'}`")

    if not live_enabled:
        st.info("Enable **live actions** in the sidebar to chat with the agent (requires valid AWS credentials).")

    # Conversation state: a stable session id preserves history across turns.
    history = st.session_state.setdefault("chat_history", [])  # list[(role, text)]
    cols = st.columns([1, 1, 3])
    if cols[0].button("🆕 New conversation", use_container_width=True):
        st.session_state["chat_history"] = []
        st.session_state.pop("chat_session_id", None)
        st.session_state.pop("chat_tools", None)
        st.session_state.pop("chat_tools_empty", None)
        st.rerun()
    if st.session_state.get("chat_session_id"):
        cols[1].caption(f"session `{st.session_state['chat_session_id'][:8]}…`")

    with st.expander("Example prompts (click to copy into the box)"):
        for s in CHAT_SUGGESTIONS:
            st.code(s, language="text")

    # Render the transcript so far. Assistant turns carry the session id + a turn index so
    # we can reconstruct the tools they called on demand.
    for i, entry in enumerate(history):
        role, text = entry[0], entry[1]
        with st.chat_message("user" if role == "user" else "assistant"):
            st.markdown(text)
            if role == "assistant":
                _render_chat_tools(i, entry[2] if len(entry) > 2 else None)

    prompt = st.chat_input("Message the agent…", disabled=not live_enabled)
    if prompt:
        history.append(("user", prompt))
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Agent is responding…"):
                result = actions.chat_once(
                    prompt,
                    session_id=st.session_state.get("chat_session_id"),
                    config_path=CONFIG_PATH,
                )
            if result.ok:
                if result.session_id:
                    st.session_state["chat_session_id"] = result.session_id
                answer = clean_sse_text(result.text) or "_(empty response)_"
                st.markdown(answer)
                history.append(("assistant", answer, result.session_id))
            else:
                st.error(result.error or "Chat failed.")
                # Drop the unanswered user turn so retry is clean.
                if history and history[-1][0] == "user":
                    history.pop()


def _render_chat_tools(turn_index: int, session_id: str | None) -> None:
    """On-demand 'tools called' reconstruction for one assistant turn.

    Fetched lazily (not after every message) because trace ingestion lags ~1-2 min — so we
    only pay the cost when the presenter clicks. Only **non-empty** results are cached; an
    empty result (traces not yet ingested) is not cached so the button stays clickable and
    can be retried once the spans land.
    """
    if not session_id:
        return
    cache = st.session_state.setdefault("chat_tools", {})  # turn_index -> list[str] (non-empty only)
    key = str(turn_index)
    if cache.get(key):
        tools = cache[key]
        st.caption("🛠️ Tools called: " + " → ".join(f"`{t}`" for t in tools))
        return

    # Track whether the last attempt for this turn came back empty, to show a hint.
    empty_flag = st.session_state.setdefault("chat_tools_empty", set())
    label = "🔁 Retry — no tools detected yet" if key in empty_flag else "🛠️ Show tools called"
    if st.button(label, key=f"tools_btn_{turn_index}"):
        with st.spinner("Reconstructing tool trajectory from traces…"):
            tools = actions.fetch_session_tools(session_id, config_path=CONFIG_PATH)
        if tools:
            cache[key] = tools
            empty_flag.discard(key)
        else:
            empty_flag.add(key)
        st.rerun()
    if key in empty_flag:
        st.caption(
            "_No tools detected yet — traces take ~1-2 min to reach CloudWatch. "
            "Wait a moment and retry._"
        )


def render_online(data: DemoData) -> None:
    st.subheader("Online evaluation")
    st.caption(
        "Results from the `online` stage: it enables an online-evaluation config and drives "
        "scripted multi-turn traffic against the deployed agent, then scores those sessions."
    )
    result = data.online_result()
    if not result:
        st.info(
            "No online results yet. Run the `online` stage (Run demo tab, or `demo online`) "
            "to drive traffic and score it."
        )
        return

    top = st.columns(3)
    top[0].metric("Sessions driven", result.get("session_count", 0))
    top[1].metric("Sampling", f"{result.get('sampling_percentage', 0):.0f}%")
    top[2].metric("Config", result.get("config_name") or result.get("config_id") or "—")
    if result.get("generated_at"):
        st.caption(f"🕒 Run: {data.fmt_ts(result['generated_at'])}")

    scores = {k: v for k, v in (result.get("aggregate_scores") or {}).items() if not k.startswith("_")}
    st.markdown("**Aggregate scores**")
    if scores:
        cols = st.columns(min(len(scores), 4))
        for i, (nm, mean) in enumerate(sorted(scores.items())):
            cols[i % len(cols)].metric(nm, f"{mean:.3f}")
        st.bar_chart(scores)
    else:
        st.warning(
            "Scores are still aggregating. Online/batch evaluation runs asynchronously "
            "(~10-15 min); re-run the `online` stage or refresh once they populate."
        )

    sessions = result.get("sessions") or []
    if sessions:
        st.markdown(f"**Driven sessions** ({len(sessions)})")
        st.dataframe(
            [
                {
                    "Session": s.get("session_id", "")[:8] + "…",
                    "Tools called": " → ".join(s.get("tool_calls", [])) or "—",
                }
                for s in sessions
            ],
            use_container_width=True,
            hide_index=True,
        )


def _full_text(text: str) -> None:
    """Render the full text with no fixed-height scroll box.

    ``st.code`` caps its height and adds an inner scrollbar for long content; instead we
    render the complete text in a bordered container as a Markdown blockquote so the whole
    prompt is visible and flows with the page. Markdown special characters are escaped and
    newlines become hard line breaks so the prompt shows verbatim.
    """
    escaped = (
        str(text)
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("#", "\\#")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )
    quoted = "\n".join(f"> {line}" if line else ">" for line in escaped.splitlines())
    with st.container(border=True):
        st.markdown(quoted)


def render_recommendation(data: DemoData) -> None:
    st.subheader("Optimization recommendation")
    st.caption(
        "Output of the `recommend` stage: AgentCore analyzes production traces and proposes "
        "an improved system prompt (and/or tool descriptions) targeting a weak metric."
    )
    rec = data.recommendation()
    if not rec:
        st.info(
            "No recommendation yet. Run the `recommend` stage (Run demo tab, or "
            "`demo recommend`) to generate one."
        )
        return

    st.write(f"**Kind:** `{rec.get('kind', 'system_prompt')}`")

    before = data.baseline_prompt()
    after = rec.get("recommended_system_prompt")

    if after:
        if before:
            st.markdown("### System prompt — before → after")
            st.markdown("#### Before (baseline / control)")
            _full_text(before)
            st.divider()
            st.markdown("#### After (recommended / treatment)")
            _full_text(after)
        else:
            st.markdown("### Recommended system prompt")
            _full_text(after)

    tools = rec.get("recommended_tool_descriptions")
    if tools:
        st.markdown("**Recommended tool descriptions**")
        st.json(tools)


def render_bundles(data: DemoData) -> None:
    st.subheader("Bundle configs — view & edit")
    st.caption(
        "The `generate-config` step drafts editable configs for both the **control** and "
        "**treatment** bundles. Edit the model id and system prompt here, save, then use "
        "**Build & run A/B** to rebuild the bundles from your edits and run the A/B test. "
        "Tool descriptions are carried through unchanged and aren't editable here."
    )

    if not data.drafts_exist():
        st.info(
            "No config drafts yet. Run the **generate-config** step (Run demo tab, or "
            "`demo generate-config`) to create editable `control` and `treatment` drafts."
        )
        return

    live_enabled = st.session_state.get("live_enabled", False)
    from agentcore_demo.ui.data import KNOWN_MODEL_IDS

    for side in data.draft_sides:
        draft = data.draft_config(side) or {"model_id": None, "system_prompt": ""}
        with st.container(border=True):
            st.markdown(f"### {side.capitalize()}")
            st.caption(f"`{data.draft_file(side)}`")

            # Model id: dropdown of known-good ids + a custom option.
            current_model = draft.get("model_id") or ""
            options = list(KNOWN_MODEL_IDS)
            custom_label = "Custom…"
            if current_model and current_model not in options:
                options.append(current_model)
            options.append(custom_label)
            default_index = options.index(current_model) if current_model in options else len(options) - 1
            choice = st.selectbox("Model id", options=options, index=default_index, key=f"model_sel_{side}")
            if choice == custom_label:
                model_id = st.text_input("Custom model id", value=current_model, key=f"model_custom_{side}")
            else:
                model_id = choice
            if model_id and model_id not in KNOWN_MODEL_IDS:
                st.caption("⚠️ Not a known-good id — it must be enabled in your region to run.")

            # System prompt: full-height editable area.
            system_prompt = st.text_area(
                "System prompt",
                value=draft.get("system_prompt", ""),
                height=280,
                key=f"prompt_{side}",
            )

            if st.button(f"💾 Save {side} draft", key=f"save_{side}"):
                if not (system_prompt or "").strip():
                    st.error("System prompt can't be empty.")
                else:
                    data.save_draft_config(side, model_id=model_id.strip() or None, system_prompt=system_prompt)
                    _refresh()
                    st.success(f"Saved {side} draft.")
                    st.rerun()

    st.divider()
    _render_build_and_run(data, live_enabled)


def _render_build_and_run(data: DemoData, live_enabled: bool) -> None:
    st.markdown("**Build & run A/B on the saved drafts**")
    st.caption(
        "Runs `build-bundles → offline-check → ab-test` on the current drafts (a live run "
        "that provisions bundles and drives A/B traffic)."
    )
    if runner.is_running():
        st.info("A run is already in progress — see the **Run demo** tab.")
        return
    if st.button("▶ Build & run A/B", type="primary", disabled=not live_enabled, use_container_width=False):
        started, msg = runner.start_run(["build-bundles", "offline-check", "ab-test"], config_path=CONFIG_PATH)
        if started:
            st.success(msg + " Watch progress on the Run demo tab.")
            st.rerun()
        else:
            st.error(msg)
    if not live_enabled:
        st.caption("Enable **live actions** in the sidebar to build & run.")


def render_candidate(data: DemoData) -> None:
    st.subheader("Candidate: build & offline check")
    st.caption(
        "The `bundle` step packages the recommended configuration into a candidate "
        "(treatment) bundle; the `offline-check` step batch-evaluates that candidate on the "
        "curated dataset to catch regressions **before** any live A/B traffic."
    )

    cfg = data.treatment_config()
    check = data.report("baseline_treatment")
    # Prefer the control evaluated in the SAME offline-check run (true apples-to-apples
    # before); fall back to the older baseline_pre for runs that predate this.
    control_check = data.report("baseline_control")
    baseline = control_check or data.report("baseline_pre")
    baseline_is_control = control_check is not None

    if not cfg and not check:
        st.info(
            "No candidate yet. Run the **bundle + offline-check** step (Run demo tab) to "
            "package the treatment configuration and validate it offline."
        )
        return

    # --- offline-check regression scores (before -> after) ---------------
    st.markdown("**Offline check — control vs candidate**")
    if check is None:
        st.info("The candidate bundle was built, but no offline-check report is present yet.")
    else:
        if check.generated_at:
            st.caption(f"🕒 Checked: {data.fmt_ts(check.generated_at)} · {check.case_count} cases")
        before = {k: v for k, v in (baseline.aggregate_scores.items() if baseline else []) if not k.startswith("_")}
        after = {k: v for k, v in check.aggregate_scores.items() if not k.startswith("_")}
        before_label = "Control" if baseline_is_control else "Baseline"
        evaluators = sorted(set(before) | set(after))
        rows = []
        for ev in evaluators:
            b = before.get(ev)
            a = after.get(ev)
            delta = (a - b) if (a is not None and b is not None) else None
            rows.append(
                {
                    "Evaluator": ev,
                    before_label: ("—" if b is None else f"{b:.3f}"),
                    "Candidate": ("—" if a is None else f"{a:.3f}"),
                    "Δ": ("—" if delta is None else f"{delta:+.3f}"),
                    "": ("" if delta is None else ("⬆️" if delta > 0 else ("⬇️" if delta < 0 else "➖"))),
                }
            )
        st.dataframe(rows, use_container_width=True, hide_index=True)
        if baseline_is_control:
            st.caption(
                "Control = `baseline_control` and Candidate = `baseline_treatment`, both "
                "evaluated in the **same** offline-check run on the same cases (apples-to-apples). "
                "Offline-check is a regression guardrail on a small, non-deterministic sample — "
                "expect minor run-to-run variance; the live A/B test is the statistical comparison."
            )
        else:
            st.caption(
                "Baseline = `baseline_pre` (offline-baseline) · Candidate = `baseline_treatment` "
                "(offline-check). Re-run **bundle + offline-check** to compare against the candidate's "
                "own control (`baseline_control`) for an apples-to-apples check."
            )

    # --- the candidate configuration -------------------------------------
    if cfg:
        st.markdown("**Candidate configuration**")
        if cfg.get("model_id"):
            st.write(f"**Model:** `{cfg['model_id']}`")
        prompt = cfg.get("system_prompt")
        if prompt:
            with st.expander("System prompt", expanded=False):
                st.code(prompt, language="text")
        tools = cfg.get("tool_descriptions")
        if tools:
            with st.expander("Tool descriptions", expanded=False):
                st.json(tools)


def render_reports(data: DemoData) -> None:
    st.subheader("Offline evaluation report")
    names = data.report_names()
    if not names:
        st.info(
            "No offline reports yet. Run the `offline-baseline` (or `offline-check`) stage "
            "to generate one under `artifacts/reports/`."
        )
        return

    st.caption(
        "The report the loop generates is shown here directly — no email needed. "
        "(Email/outbox delivery still runs as a separate demo of that capability.)"
    )
    name = st.selectbox("Report", options=names, index=0)
    report = data.report(name)
    if report is None:
        st.error(f"Could not parse report `{name}`.")
        return

    top = st.columns(2)
    top[0].metric("Baseline", report.baseline_name)
    top[1].metric("Cases", report.case_count)
    st.caption(f"🕒 Generated: {data.fmt_ts(report.generated_at)}")

    _render_delivery_note(report)
    _render_aggregate_scores(report)
    _render_per_case(report)
    _render_raw_html(data, name)


def _render_delivery_note(report) -> None:
    if report.delivery == "outbox":
        st.caption("📥 Report was written to the local outbox (email not configured / not sent).")
    elif report.delivered_to:
        st.caption(f"📧 Report was emailed via {report.delivery} to {report.delivered_to}.")
    else:
        st.caption(f"Delivery: {report.delivery} (no recipient recorded).")


def _render_aggregate_scores(report) -> None:
    st.markdown("**Aggregate scores**")
    # Separate real evaluator means from bookkeeping keys (e.g. _batch_error).
    scores = {k: v for k, v in report.aggregate_scores.items() if not k.startswith("_")}
    diagnostics = {k: v for k, v in report.aggregate_scores.items() if k.startswith("_")}

    if scores:
        cols = st.columns(min(len(scores), 4))
        for i, (name, mean) in enumerate(sorted(scores.items())):
            cols[i % len(cols)].metric(name, f"{mean:.3f}")
        st.bar_chart(scores)
    if diagnostics.get("_batch_error"):
        st.warning(
            f"Batch evaluation reported errors (`_batch_error` = {diagnostics['_batch_error']:.2f}). "
            "Some built-in evaluator means may be missing for this report."
        )


def _render_per_case(report) -> None:
    st.markdown(f"**Per-case results** ({len(report.per_case)} cases)")
    for case in report.per_case:
        case_id = case.get("case_id", "?")
        evaluators = case.get("evaluators", {}) or {}
        # Summarize pass/fail across applicable evaluators for the header.
        applicable = [e for e in evaluators.values() if e.get("applicable")]
        passed = sum(1 for e in applicable if e.get("passed"))
        err = case.get("error")
        if err:
            head = f"❌ {case_id} — error"
        elif applicable:
            mark = "✅" if passed == len(applicable) else ("⚠️" if passed else "❌")
            head = f"{mark} {case_id} — {passed}/{len(applicable)} evaluators passed"
        else:
            head = f"• {case_id}"

        with st.expander(head):
            if err:
                st.error(f"Case error: {err}")
            tools = case.get("tool_calls") or []
            st.write("**Tools called:** " + (" → ".join(f"`{t}`" for t in tools) if tools else "_none_"))

            if evaluators:
                st.dataframe(
                    [
                        {
                            "Evaluator": ev_name,
                            "Applicable": "yes" if ev.get("applicable") else "no",
                            "Passed": ("✅" if ev.get("passed") else "❌") if ev.get("applicable") else "—",
                            "Score": (f"{ev['score']:.2f}" if ev.get("score") is not None else "—"),
                            "Explanation": ev.get("explanation", ""),
                        }
                        for ev_name, ev in evaluators.items()
                    ],
                    use_container_width=True,
                    hide_index=True,
                )

            response = clean_sse_text(case.get("final_response"))
            if response:
                st.markdown("**Agent response**")
                st.markdown(f"> {response.strip().replace(chr(10), chr(10) + '> ')}")


def _render_raw_html(data: DemoData, name: str) -> None:
    html = data.report_html(name)
    if not html:
        return
    with st.expander("Rendered HTML report (as emailed)"):
        st_components.html(html, height=320, scrolling=True)


def render_sidebar(data: DemoData) -> None:
    with st.sidebar:
        st.header("Controls")
        if st.button("🔄 Refresh artifacts", use_container_width=True):
            _refresh()
            st.rerun()

        st.divider()
        st.subheader("Live actions")
        st.caption(
            "Enables running the loop and approving/rejecting promotions. Requires valid "
            "AWS credentials in the environment running this app."
        )
        st.session_state["live_enabled"] = st.toggle(
            "Enable live actions",
            value=st.session_state.get("live_enabled", False),
        )

        st.divider()
        st.caption(f"Artifacts: `{data.artifacts_dir}`")
        from agentcore_demo.ui.data import tz_abbrev

        st.caption(
            f"Times shown in **{tz_abbrev(data.display_tz)}** + UTC "
            f"(set `display_timezone` in config to change)."
        )
        gw = data.gateway()
        if gw and gw.get("gateway_id"):
            st.caption(f"Gateway: `{gw['gateway_id']}`")
        tev = data.trajectory_evaluator_id()
        if tev:
            st.caption(f"Trajectory evaluator: `{tev}`")


def main() -> None:
    st.set_page_config(page_title="AgentCore Optimization Demo", page_icon="🩺", layout="wide")
    data = get_data()
    render_sidebar(data)
    render_header(data)

    tabs = st.tabs(
        [
            "🚀 Run demo",
            "💬 Chat",
            "📡 Online",
            "💡 Recommendation",
            "🧾 Bundles",
            "🧪 Candidate",
            "📊 A/B Results",
            "📋 Reports",
            "✅ Approvals",
            "📜 Audit trail",
        ]
    )
    with tabs[0]:
        render_run(data)
    with tabs[1]:
        render_chat(data)
    with tabs[2]:
        render_online(data)
    with tabs[3]:
        render_recommendation(data)
    with tabs[4]:
        render_bundles(data)
    with tabs[5]:
        render_candidate(data)
    with tabs[6]:
        render_results(data)
    with tabs[7]:
        render_reports(data)
    with tabs[8]:
        render_approvals(data)
    with tabs[9]:
        render_audit(data)


# Streamlit executes this module top-to-bottom, so call main() at import time.
main()
