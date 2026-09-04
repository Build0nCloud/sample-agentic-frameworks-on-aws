"""Background stage runner for the Streamlit front end.

Streamlit reruns the whole script on every interaction and can't block its main thread
for the ~25-40 minutes a full optimization loop takes. This module runs the loop in a
**daemon thread**, capturing every ``emit()`` progress line and per-stage status into a
thread-safe, process-global :class:`RunState` that the Streamlit main thread polls and
re-renders.

It reuses the exact CLI wiring (:class:`AgentCoreClient`, :func:`build_stages`,
:class:`PromotionGate`) so "run from the browser" drives the same real AgentCore loop as
``demo run-all`` / ``demo <stage>`` — only the ``emit`` sink differs (a queue instead of
``print``).

A single run at a time is allowed (the loop mutates shared ``artifacts/`` state); the
runner refuses to start a second concurrent run.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import threading
from typing import Sequence

from ..orchestrator import (
    OrchestratorError,
    Orchestrator,
    StageContext,
    build_stages,
    load_agent,
    make_context,
)


def _now() -> str:
    return _dt.datetime.now().strftime("%H:%M:%S")


@dataclasses.dataclass
class RunState:
    """Thread-safe snapshot of an in-flight or finished run.

    All access goes through the module lock; :meth:`snapshot` returns a plain,
    detached copy so the UI never reads a half-updated structure.
    """

    running: bool = False
    stages: list[str] = dataclasses.field(default_factory=list)
    current_stage: str | None = None
    stage_status: dict[str, str] = dataclasses.field(default_factory=dict)  # stage -> pending|running|ok|failed
    log: list[str] = dataclasses.field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    ok: bool | None = None  # None while running; True/False when done


_LOCK = threading.RLock()
_STATE = RunState()
_THREAD: threading.Thread | None = None


# ---------------------------------------------------------------------------
# Read side (Streamlit main thread)
# ---------------------------------------------------------------------------
def snapshot() -> RunState:
    with _LOCK:
        return RunState(
            running=_STATE.running,
            stages=list(_STATE.stages),
            current_stage=_STATE.current_stage,
            stage_status=dict(_STATE.stage_status),
            log=list(_STATE.log),
            started_at=_STATE.started_at,
            finished_at=_STATE.finished_at,
            error=_STATE.error,
            ok=_STATE.ok,
        )


def is_running() -> bool:
    with _LOCK:
        return _STATE.running


# ---------------------------------------------------------------------------
# Write side (helpers used by the worker thread)
# ---------------------------------------------------------------------------
def _append_log(line: str) -> None:
    with _LOCK:
        _STATE.log.append(f"[{_now()}] {line}")


def _set_stage_status(stage: str, status: str) -> None:
    with _LOCK:
        _STATE.stage_status[stage] = status
        if status == "running":
            _STATE.current_stage = stage


def reset() -> None:
    """Clear the last run's state (only when nothing is running)."""
    with _LOCK:
        if _STATE.running:
            return
        globals()["_STATE"] = RunState()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def start_run(stages: Sequence[str], *, config_path: str | None = None) -> tuple[bool, str]:
    """Start a background run of ``stages``. Returns ``(started, message)``.

    Refuses to start if a run is already in flight (one run mutates shared state).
    """
    global _THREAD
    with _LOCK:
        if _STATE.running:
            return False, "A run is already in progress."
        stages = list(stages)
        if not stages:
            return False, "No stages selected."
        # Fresh state for this run.
        globals()["_STATE"] = RunState(
            running=True,
            stages=stages,
            stage_status={s: "pending" for s in stages},
            started_at=_now(),
        )

    _THREAD = threading.Thread(
        target=_worker,
        args=(stages, config_path),
        name="agentcore-demo-runner",
        daemon=True,
    )
    _THREAD.start()
    return True, f"Started run of {len(stages)} stage(s)."


def _build(config_path: str | None) -> tuple[Orchestrator, StageContext]:
    """Build the real (orchestrator, context) wired to AgentCore with a queue emit.

    Mirrors ``demo._default_builder`` but routes ``emit`` into the run log instead of
    ``print`` and marks the promotion-gate context so approve can find its decision.
    """
    from ..agentcore_client import AgentCoreClient
    from ..config import load_config
    from ..optimization.promotion import PromotionGate
    from ..state import StateStore

    config = load_config(config_path)
    store = StateStore(config.artifacts_dir)
    client = AgentCoreClient(config)
    agent = load_agent(store)
    context = make_context(config, store, client=client, agent=agent, emit=_emit_line, assume_yes=True)
    context.bag["promotion_gate"] = PromotionGate(client, store)
    return Orchestrator(build_stages(context)), context


# Progress lines from the orchestrator look like "▶ deploy: ...", "✓ deploy: ...",
# "✗ deploy failed: ...". Parse the stage marker so the checklist tracks status.
def _emit_line(line: str) -> None:
    text = str(line)
    _append_log(text)
    stripped = text.strip()
    if stripped.startswith("\u25b6"):  # ▶ stage started
        stage = _stage_from(stripped)
        if stage:
            _set_stage_status(stage, "running")
    elif stripped.startswith("\u2713"):  # ✓ stage ok
        stage = _stage_from(stripped)
        if stage:
            _set_stage_status(stage, "ok")
    elif stripped.startswith("\u2717"):  # ✗ stage failed
        stage = _stage_from(stripped)
        if stage:
            _set_stage_status(stage, "failed")


def _stage_from(marker_line: str) -> str | None:
    # "▶ deploy: description" -> "deploy"; "✗ deploy failed: ..." -> "deploy"
    rest = marker_line[1:].strip()
    token = rest.split(":", 1)[0].strip()
    token = token.replace(" failed", "").strip()
    return token or None


def _worker(stages: list[str], config_path: str | None) -> None:
    try:
        orchestrator, context = _build(config_path)
    except Exception as exc:  # noqa: BLE001 - build/config/creds failure before any stage
        with _LOCK:
            _STATE.running = False
            _STATE.ok = False
            _STATE.error = f"Setup failed: {type(exc).__name__}: {exc}"
            _STATE.finished_at = _now()
        _append_log(f"\u2717 setup failed: {exc}")
        return

    try:
        orchestrator.run(context, only=stages)
        with _LOCK:
            _STATE.ok = True
    except OrchestratorError as exc:
        with _LOCK:
            _STATE.ok = False
            _STATE.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - unexpected
        with _LOCK:
            _STATE.ok = False
            _STATE.error = f"{type(exc).__name__}: {exc}"
    finally:
        with _LOCK:
            _STATE.running = False
            _STATE.current_stage = None
            _STATE.finished_at = _now()
