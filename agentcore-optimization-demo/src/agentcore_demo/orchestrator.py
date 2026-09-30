"""Stage orchestration engine for the demo.

Runs the optimization loop as ordered, observable stages. Each stage produces a
:class:`StageOutcome`; the orchestrator records status in the run manifest, emits
human-readable progress, and halts (without proceeding to dependent stages) on failure.

This module owns the generic engine and the ordered stage *metadata*
(:data:`STAGE_SPECS`). The concrete stage implementations (which drive real AgentCore)
are wired in :func:`build_stages` (task 17.1).

Requirements: 7.1, 7.2, 7.3, 7.4, 7.6
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from .models import Baseline
from .state import Manifest, StateStore


# ---------------------------------------------------------------------------
# Ordered stage metadata (used for listing and the cost/confirmation gate)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StageSpec:
    name: str
    description: str
    provisions: bool  # True if the stage creates billable AWS resources


STAGE_SPECS: tuple[StageSpec, ...] = (
    StageSpec("deploy", "Deploy the patient-support agent to AgentCore Runtime", True),
    StageSpec("offline-baseline", "Batch-evaluate the current config on the curated dataset; email the report", False),
    StageSpec("online", "Create online evaluation and drive multi-turn traffic against the agent", True),
    StageSpec("recommend", "Generate a system-prompt / tool-description recommendation from traces", False),
    StageSpec("generate-config", "Draft editable control + treatment configs (model + system prompt)", False),
    StageSpec("build-bundles", "Build control and treatment configuration bundles from the drafts", True),
    StageSpec("offline-check", "Batch-evaluate the treatment bundle to catch regressions", False),
    StageSpec("ab-test", "A/B test control vs treatment on live traffic and compute significance", True),
    StageSpec("promotion-gate", "Detect a statistically significant winner and open a pending approval", False),
)

# Stages executed by `run-all` (the human approve/reject step is intentionally separate).
RUN_ALL_STAGES: tuple[str, ...] = tuple(s.name for s in STAGE_SPECS)


def stage_spec(name: str) -> StageSpec:
    for s in STAGE_SPECS:
        if s.name == name:
            return s
    raise KeyError(f"unknown stage: {name}")


def requires_provisioning(stage_names: Sequence[str]) -> bool:
    """True if any of the named stages creates billable AWS resources (R8.3)."""
    return any(stage_spec(n).provisions for n in stage_names)


# ---------------------------------------------------------------------------
# Engine types
# ---------------------------------------------------------------------------
@dataclass
class StageOutcome:
    status: str = "ok"                      # ok | failed | skipped
    artifacts: list[str] = field(default_factory=list)
    note: str | None = None
    data: dict = field(default_factory=dict)


@dataclass
class StageContext:
    """Shared state threaded through the stages of a run."""

    config: object
    store: StateStore
    manifest: Manifest
    client: object | None = None
    agent: object | None = None
    bag: dict = field(default_factory=dict)          # inter-stage artifacts (baselines, bundles, ...)
    emit: Callable[[str], None] = print
    assume_yes: bool = False


@dataclass
class Stage:
    name: str
    description: str
    run: Callable[[StageContext], StageOutcome]
    provisions: bool = False


class OrchestratorError(Exception):
    """Raised when a stage fails; carries the stage name and underlying cause."""

    def __init__(self, stage: str, cause: BaseException):
        super().__init__(f"stage '{stage}' failed: {cause}")
        self.stage = stage
        self.cause = cause


class Orchestrator:
    """Runs stages in order, records the manifest, and halts on failure."""

    def __init__(self, stages: Sequence[Stage]):
        self.stages = list(stages)
        self._by_name = {s.name: s for s in self.stages}

    def list_stages(self) -> list[tuple[str, str]]:
        return [(s.name, s.description) for s in self.stages]

    def _select(self, only: Sequence[str] | None) -> list[Stage]:
        if only is None:
            return list(self.stages)
        missing = [n for n in only if n not in self._by_name]
        if missing:
            raise KeyError(f"unknown stage(s): {missing}")
        # Preserve the canonical stage order.
        return [s for s in self.stages if s.name in set(only)]

    def run(self, context: StageContext, only: Sequence[str] | None = None) -> Manifest:
        for stage in self._select(only):
            context.emit(f"\u25b6 {stage.name}: {stage.description}")
            context.manifest.stage(stage.name).status = "started"
            context.store.save_manifest(context.manifest)
            try:
                outcome = stage.run(context)
            except Exception as exc:  # noqa: BLE001 - record + halt (R7.6)
                context.manifest.record(stage.name, "failed", note=f"{type(exc).__name__}: {exc}")
                context.store.save_manifest(context.manifest)
                context.emit(f"\u2717 {stage.name} failed: {exc}")
                raise OrchestratorError(stage.name, exc) from exc

            context.manifest.record(stage.name, outcome.status, artifacts=outcome.artifacts, note=outcome.note)
            context.store.save_manifest(context.manifest)
            context.emit(f"\u2713 {stage.name}: {outcome.note or outcome.status}")
            if outcome.status == "failed":
                err = RuntimeError(outcome.note or "stage reported failure")
                context.emit(f"\u2717 {stage.name} failed: {err}")
                raise OrchestratorError(stage.name, err)
        return context.manifest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_context(config, store: StateStore, *, client=None, agent=None, emit: Callable[[str], None] = print, assume_yes: bool = False, run_id: str | None = None) -> StageContext:
    manifest = store.start_run(run_id)
    return StageContext(config=config, store=store, manifest=manifest, client=client, agent=agent, emit=emit, assume_yes=assume_yes)


def compare_baselines(before: Baseline, after: Baseline) -> dict[str, dict[str, float]]:
    """Per-evaluator before/after/delta comparison (Requirement 7.4)."""
    evaluators = sorted(set(before.scores) | set(after.scores))
    out: dict[str, dict[str, float]] = {}
    for ev in evaluators:
        b = before.scores.get(ev, 0.0)
        a = after.scores.get(ev, 0.0)
        out[ev] = {"before": b, "after": a, "delta": a - b}
    return out


def format_before_after(before: Baseline, after: Baseline) -> str:
    comp = compare_baselines(before, after)
    lines = [
        f"Before/after comparison: {before.name} -> {after.name}",
        f"{'Evaluator':<28}{'Before':>10}{'After':>10}{'Delta':>10}",
        "-" * 58,
    ]
    for ev, vals in comp.items():
        lines.append(f"{ev:<28}{vals['before']:>10.3f}{vals['after']:>10.3f}{vals['delta']:>+10.3f}")
    return "\n".join(lines)


def maybe_emit_before_after(context: StageContext, *, before_key: str = "original_baseline", after_key: str = "candidate_baseline") -> None:
    """Emit a before/after comparison if both baselines are present in the bag."""
    before = context.bag.get(before_key)
    after = context.bag.get(after_key)
    if isinstance(before, Baseline) and isinstance(after, Baseline):
        context.emit("\n" + format_before_after(before, after))


# ---------------------------------------------------------------------------
# Small cross-invocation persistence helpers (agent handle, promotion decision)
# ---------------------------------------------------------------------------
def save_agent(store: StateStore, agent) -> None:
    (store.root / "agent_state.json").write_text(__import__("json").dumps(agent.to_dict(), indent=2), encoding="utf-8")


def load_agent(store: StateStore):
    from .agentcore_client import AgentHandle

    path = store.root / "agent_state.json"
    if not path.exists():
        return None
    return AgentHandle.from_dict(__import__("json").loads(path.read_text(encoding="utf-8")))


def save_promotion_decision(store: StateStore, request_id: str, decision) -> None:
    from .state import to_jsonable

    store.write_json("bundles", f"decision-{request_id}", to_jsonable(decision))


def load_promotion_decision(store: StateStore, request_id: str):
    from .agentcore_client import PromotionDecision
    from .models import BundleConfig

    if not store.exists("bundles", f"decision-{request_id}"):
        return None
    d = store.read_json("bundles", f"decision-{request_id}")
    cfg = d.get("config")
    return PromotionDecision(
        strategy=d["strategy"],
        ab_test_id=d.get("ab_test_id"),
        bundle_id=d.get("bundle_id"),
        agent_arn=d.get("agent_arn"),
        config=BundleConfig.from_dict(cfg) if cfg else None,
        parent_version_ids=list(d.get("parent_version_ids", []) or []),
        commit_message=d.get("commit_message", "Promote treatment (A/B validated)"),
    )


def _log_group_arn(agent) -> str:
    return f"arn:aws:logs:{agent.region}:{agent.account_id}:log-group:{agent.log_group}"


# ---------------------------------------------------------------------------
# Cross-stage state (so stages can be run in separate processes) — Requirement 7.5
# ---------------------------------------------------------------------------
_LOOP_STATE = "loop_state.json"


def load_loop_state(store: StateStore) -> dict:
    import json

    path = store.root / _LOOP_STATE
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_loop_state(store: StateStore, **updates) -> None:
    import json

    from .state import to_jsonable

    state = load_loop_state(store)
    state.update({k: to_jsonable(v) for k, v in updates.items()})
    (store.root / _LOOP_STATE).write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Editable bundle-config drafts (generate-config writes them; build-bundles + the UI
# read them). Each draft exposes only the editable surface — model_id + system_prompt —
# as YAML so the multi-line prompt stays hand-editable. Tool descriptions are carried
# through internally (not exposed for editing) and merged back at build time.
# ---------------------------------------------------------------------------
_CONFIG_DRAFTS_DIR = "config_drafts"
DRAFT_SIDES = ("control", "treatment")


def _drafts_dir(store: StateStore):
    d = store.root / _CONFIG_DRAFTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def draft_path(store: StateStore, side: str):
    if side not in DRAFT_SIDES:
        raise ValueError(f"unknown draft side {side!r}; expected one of {DRAFT_SIDES}")
    return _drafts_dir(store) / f"{side}.yaml"


def save_config_draft(store: StateStore, side: str, config) -> None:
    """Write a BundleConfig (or dict) as an editable YAML draft (model_id + system_prompt).

    Tool descriptions are preserved in a non-editable trailer key so build-bundles can
    reconstruct the full config without exposing them for editing.
    """
    import yaml

    if hasattr(config, "system_prompt"):
        model_id = getattr(config, "model_id", None)
        system_prompt = config.system_prompt
        tool_descriptions = dict(getattr(config, "tool_descriptions", {}) or {})
    else:
        model_id = config.get("model_id")
        system_prompt = config.get("system_prompt", "")
        tool_descriptions = dict(config.get("tool_descriptions", {}) or {})

    data = {
        "model_id": model_id,
        "system_prompt": system_prompt,
        # Preserved but not meant for editing (kept out of the way at the bottom).
        "_tool_descriptions": tool_descriptions,
    }

    class _Dumper(yaml.SafeDumper):
        pass

    def _str_presenter(dumper, value):
        style = "|" if "\n" in value else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    _Dumper.add_representer(str, _str_presenter)
    draft_path(store, side).write_text(
        yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def load_config_draft(store: StateStore, side: str):
    """Load a YAML draft into a BundleConfig, or return None if the draft doesn't exist."""
    import yaml

    from .models import BundleConfig

    path = draft_path(store, side)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return BundleConfig(
        system_prompt=data.get("system_prompt", "") or "",
        model_id=data.get("model_id"),
        tool_descriptions=dict(data.get("_tool_descriptions", {}) or {}),
    )


def config_drafts_exist(store: StateStore) -> bool:
    return all(draft_path(store, side).exists() for side in DRAFT_SIDES)


# ---------------------------------------------------------------------------
# Concrete stage wiring (drives real AgentCore) — Requirements 7.1, 7.3, 6.7
# ---------------------------------------------------------------------------
def build_stages(context: StageContext) -> list[Stage]:
    """Build the ordered, wired stages for the full optimization loop.

    Each stage reads/writes ``context.bag`` to pass artifacts forward. Stage bodies
    drive real AgentCore via ``context.client``; they are exercised by the opt-in live
    smoke test (task 17.3), not by unit tests.
    """
    from .agent.patient_support_agent import DEFAULT_SYSTEM_PROMPT, DEFAULT_TOOL_DESCRIPTIONS
    from .agentcore_client import BundleRef, OnlineEvalConfig, PromotionDecision
    from .evaluation.offline import load_cases, make_client_runner, run_offline_evaluation
    from .evaluation.online import OnlineEvaluation, load_traffic
    from .models import BundleConfig
    from .optimization.abtest import ABTestRunner
    from .optimization.bundles import BundleManager, offline_check_bundle
    from .optimization.promotion import PromotionGate
    from .optimization.recommendations import RecommendationService, apply_to_config, present_change
    from .reporting.emailer import deliver_report

    import datetime as _dt
    import time as _time
    import uuid as _uuid

    cfg = context.config
    # Seconds to wait for CloudWatch span ingestion before reconstructing tool calls.
    ingestion_wait = float(getattr(cfg, "ingestion_wait_seconds", 90))
    # Unique per-invocation suffix so replaying a stage never collides with resources a
    # prior run created (AgentCore resource names must be unique). Names are alphanumeric
    # (no hyphens) to satisfy the ^[A-Za-z][A-Za-z0-9_]{0,47}$ constraint.
    sfx = _uuid.uuid4().hex[:6]

    def _resolver(ctx):
        return lambda sid: ctx.client.fetch_session_tool_calls(sid, ctx.agent.spans_log_group)

    def _ensure_trajectory_evaluator(ctx: StageContext) -> str | None:
        """Deploy+register the custom trajectory evaluator once; reuse via loop_state (Phase 2)."""
        if not getattr(cfg, "enable_trajectory_evaluator", False):
            return None
        existing = load_loop_state(ctx.store).get("trajectory_evaluator_id")
        if existing:
            return existing
        from .config import REPO_ROOT

        source_dir = REPO_ROOT / "lambdas" / "trajectory_evaluator"
        ctx.emit("    deploying custom trajectory evaluator (Lambda)…")
        evaluator_id = ctx.client.deploy_code_evaluator(
            function_name=f"{cfg.runtime_name.lower()}-trajectory-eval",
            source_dir=source_dir,
            level="SESSION",
            timeout_s=60,
            evaluator_name=f"TrajectoryQuality{sfx}",
        )
        save_loop_state(ctx.store, trajectory_evaluator_id=evaluator_id)
        ctx.emit(f"    trajectory evaluator registered: {evaluator_id}")
        return evaluator_id

    def _batch_scorer(ctx):
        from .agentcore_client import EvalTarget

        def scorer(session_ids):
            target = EvalTarget(
                name=f"{cfg.runtime_name}Batch{sfx}",
                service_name=ctx.agent.service_name,
                log_groups=[ctx.agent.spans_log_group, ctx.agent.log_group],
                session_ids=list(session_ids),
            )
            return ctx.client.run_batch_evaluation(target, cfg.evaluators)["scores"]

        return scorer

    # -- deploy ----------------------------------------------------------
    def deploy(ctx: StageContext) -> StageOutcome:
        if ctx.agent is None:
            ctx.agent = ctx.client.deploy_agent(cfg.runtime_name, "v1")
            save_agent(ctx.store, ctx.agent)
        ctx.bag["agent"] = ctx.agent
        return StageOutcome(note=f"runtime {ctx.agent.runtime_id}", artifacts=[ctx.agent.runtime_arn])

    # -- offline baseline + report/email --------------------------------
    def offline_baseline(ctx: StageContext) -> StageOutcome:
        cases = load_cases(cfg.offline_dataset)
        baseline_ref = ctx.bag.get("current_baseline_ref")  # promoted config from a prior loop (R6.7)
        runner = make_client_runner(ctx.client, ctx.agent, bundle=baseline_ref)
        result = run_offline_evaluation(
            "baseline_pre", cases, runner, batch_scorer=_batch_scorer(ctx), store=ctx.store,
            config_ref=(baseline_ref.version_id if baseline_ref else "agent:v1"), delivery=cfg.email.delivery,
            tool_call_resolver=_resolver(ctx), ingestion_wait=ingestion_wait,
        )
        deliver_report(result.report, cfg.email, ctx.store)
        ctx.bag["original_baseline"] = result.baseline
        return StageOutcome(note=f"baseline scores={result.baseline.scores}", artifacts=[result.report.rendered_path or ""])

    # -- online evaluation + traffic ------------------------------------
    def online(ctx: StageContext) -> StageOutcome:
        import uuid as _uuid2

        oe = OnlineEvaluation(
            ctx.client,
            OnlineEvalConfig(
                name=f"{cfg.runtime_name}OnlineEval{sfx}", service_name=ctx.agent.service_name, log_groups=[ctx.agent.log_group],
                evaluators=cfg.evaluators, role_arn=ctx.agent.role_arn, sampling_percentage=cfg.sampling_percentage,
            ),
        )
        ctx.emit(f"    creating online-evaluation config (sampling {cfg.sampling_percentage:.0f}%, "
                 f"evaluators: {', '.join(cfg.evaluators)})…")
        handle = oe.start()
        ctx.emit(f"    online eval active: {getattr(handle, 'name', '') or getattr(handle, 'config_id', '')}")

        sessions = load_traffic(cfg.traffic_dataset)
        ctx.bag["online_eval"] = oe

        # Drive traffic inline (instead of drive_traffic) so we can emit per-session
        # progress — this is the longest part of the stage and otherwise looks frozen.
        total = len(sessions)
        ctx.emit(f"    driving {total} multi-turn session(s) against the live agent…")
        results = []
        for i, spec in enumerate(sessions, start=1):
            name = spec.get("name") or f"session {i}"
            turns = spec["turns"]
            sid = str(_uuid2.uuid4())
            r = ctx.client.send_session(ctx.agent, turns, session_id=sid)
            results.append(r)
            ctx.emit(f"    [{i}/{total}] {name}: {len(turns)} turn(s) sent  (session {sid[:8]}…)")
        ctx.emit(f"    ✓ drove {len(results)} session(s); the online evaluator scores them asynchronously")

        session_ids = [r.session_id for r in results]
        # Score the driven traffic (built-in evaluators) and reconstruct per-session tool
        # trajectories so the UI can show real online results, not just a count. Both are
        # best-effort: online/batch scoring aggregates asynchronously, so a miss here
        # persists an empty scores map rather than failing the stage.
        scores: dict[str, float] = {}
        session_details: list[dict] = []
        try:
            if session_ids:
                if ingestion_wait > 0:
                    ctx.emit(f"    waiting {int(ingestion_wait)}s for CloudWatch trace/log ingestion before scoring…")
                    _time.sleep(ingestion_wait)
                    ctx.emit("    ingestion wait complete; reconstructing per-session tool trajectories…")
                resolve = _resolver(ctx)
                for i, r in enumerate(results, start=1):
                    tools = list(r.tool_calls) or resolve(r.session_id)
                    session_details.append({"session_id": r.session_id, "tool_calls": tools})
                    ctx.emit(f"    [{i}/{total}] tools: {' → '.join(tools) if tools else '(none reconstructed yet)'}")
                ctx.emit(f"    running batch scoring over {len(session_ids)} session(s) "
                         f"(evaluators: {', '.join(cfg.evaluators)})…")
                scores = _batch_scorer(ctx)(session_ids)
                ctx.emit(f"    ✓ online scores: {scores}")
        except Exception as exc:  # noqa: BLE001 - scoring is best-effort (async aggregation)
            ctx.emit(f"    online scoring incomplete (will show as pending; refresh later): {exc}")

        save_loop_state(
            ctx.store,
            online_result={
                "config_id": getattr(handle, "config_id", None),
                "config_name": getattr(handle, "name", None),
                "sampling_percentage": cfg.sampling_percentage,
                "evaluators": list(cfg.evaluators),
                "session_count": len(session_ids),
                "aggregate_scores": scores,
                "sessions": session_details,
                "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            },
        )
        scored = "scored" if scores else "pending async aggregation"
        return StageOutcome(note=f"online eval started; drove {len(results)} sessions ({scored})")

    # The control/baseline prompt: a deliberately weak one when configured (demo
    # scenario), else the agent's normal strong prompt.
    baseline_prompt = getattr(cfg, "baseline_system_prompt", None) or DEFAULT_SYSTEM_PROMPT

    # -- recommendation --------------------------------------------------
    def recommend(ctx: StageContext) -> StageOutcome:
        svc = RecommendationService(ctx.client, log_group_arns=[_log_group_arn(ctx.agent)], service_names=[ctx.agent.service_name], name_prefix=f"PatientRec{sfx}")
        target_evaluator = "Builtin.GoalSuccessRate"
        ctx.emit(f"    starting recommendation job: analyzing production traces to improve the "
                 f"system prompt for {target_evaluator}…")
        ctx.emit("    (this is a server-side LLM analysis over the trace window — it can take "
                 "several minutes; polling every ~30s)")

        _t0 = _time.time()

        def _on_poll(attempt: int, max_attempts: int, status: str) -> None:
            elapsed = int(_time.time() - _t0)
            st = status or "IN_PROGRESS"
            ctx.emit(f"    poll {attempt}: status={st} (elapsed {elapsed}s)")

        rec = svc.recommend_system_prompt(baseline_prompt, target_evaluator=target_evaluator, on_poll=_on_poll)
        change = present_change(rec, current_system_prompt=baseline_prompt, target_evaluator=target_evaluator)
        ctx.bag["recommendation"] = rec
        save_loop_state(
            ctx.store,
            recommendation={
                "kind": rec.kind,
                "recommended_system_prompt": rec.recommended_system_prompt,
                "recommended_tool_descriptions": rec.recommended_tool_descriptions,
                "target_evaluator": target_evaluator,
            },
        )
        if change.get("changed"):
            preview = (rec.recommended_system_prompt or "").strip().replace("\n", " ")
            ctx.emit(f"    ✓ recommendation ready: {change['explanation']}")
            if preview:
                ctx.emit(f"    proposed prompt (preview): {preview[:160]}{'…' if len(preview) > 160 else ''}")
        else:
            ctx.emit(f"    recommendation returned no change: {change['explanation']}")
        return StageOutcome(note=("changed" if change.get("changed") else "no change"))

    def _recommendation(ctx: StageContext):
        """Recommendation from the bag, or reconstructed from loop_state (R7.5)."""
        from .agentcore_client import Recommendation

        rec = ctx.bag.get("recommendation")
        if rec is not None:
            return rec
        d = load_loop_state(ctx.store).get("recommendation")
        if not d:
            raise RuntimeError("no recommendation found; run the 'recommend' stage first")
        return Recommendation(
            kind=d.get("kind", "system_prompt"),
            recommended_system_prompt=d.get("recommended_system_prompt"),
            recommended_tool_descriptions=d.get("recommended_tool_descriptions"),
        )

    # -- config drafting + bundle building -------------------------------
    def _compute_configs(ctx: StageContext) -> tuple[BundleConfig, BundleConfig]:
        """Compute the proposed control (weak baseline) and treatment (recommended) configs.

        Control uses the (possibly weak) baseline prompt; treatment applies the recommended
        prompt, falling back to the agent's strong default if the recommendation didn't
        change the weak baseline (so the demonstrated improvement is real).
        """
        control_config = BundleConfig(system_prompt=baseline_prompt, model_id=cfg.model_id, tool_descriptions=dict(DEFAULT_TOOL_DESCRIPTIONS))
        treatment_config = apply_to_config(_recommendation(ctx), control_config)
        if treatment_config.system_prompt == baseline_prompt:
            treatment_config.system_prompt = DEFAULT_SYSTEM_PROMPT
        return control_config, treatment_config

    def generate_config(ctx: StageContext) -> StageOutcome:
        """Write editable YAML drafts for the control + treatment configs (no AWS)."""
        control_config, treatment_config = _compute_configs(ctx)
        save_config_draft(ctx.store, "control", control_config)
        save_config_draft(ctx.store, "treatment", treatment_config)
        ctx.bag.update(control_config=control_config, treatment_config=treatment_config)
        ctx.emit(
            "    wrote editable drafts: "
            f"{draft_path(ctx.store, 'control')}, {draft_path(ctx.store, 'treatment')}"
        )
        return StageOutcome(
            note="drafted control + treatment configs (edit them, then run build-bundles)",
            artifacts=[str(draft_path(ctx.store, "control")), str(draft_path(ctx.store, "treatment"))],
        )

    def _bundle_config_for(ctx: StageContext, side: str, computed: BundleConfig) -> BundleConfig:
        """Prefer an edited draft for ``side``; fall back to the computed config."""
        draft = load_config_draft(ctx.store, side)
        return draft if draft is not None else computed

    def build_bundles(ctx: StageContext) -> StageOutcome:
        """Create the AWS bundle versions from the (possibly edited) draft configs."""
        mgr = BundleManager(ctx.client, ctx.agent.runtime_arn)
        computed_control, computed_treatment = _compute_configs(ctx)
        control_config = _bundle_config_for(ctx, "control", computed_control)
        treatment_config = _bundle_config_for(ctx, "treatment", computed_treatment)

        control = mgr.create(f"{cfg.runtime_name}Control{sfx}", control_config, "control: baseline configuration")
        treatment = mgr.create(f"{cfg.runtime_name}Treatment{sfx}", treatment_config, "treatment: recommended configuration")
        control_ref, treatment_ref = mgr.ref(control.bundle_id), mgr.ref(treatment.bundle_id)
        ctx.bag.update(
            bundle_mgr=mgr, control_ref=control_ref, treatment_ref=treatment_ref,
            control_bundle_id=control.bundle_id, control_config=control_config, treatment_config=treatment_config,
        )
        save_loop_state(
            ctx.store,
            control_ref={"bundle_arn": control_ref.bundle_arn, "version_id": control_ref.version_id, "bundle_id": control_ref.bundle_id},
            treatment_ref={"bundle_arn": treatment_ref.bundle_arn, "version_id": treatment_ref.version_id, "bundle_id": treatment_ref.bundle_id},
            control_bundle_id=control.bundle_id,
            control_config={"system_prompt": control_config.system_prompt, "model_id": control_config.model_id, "tool_descriptions": control_config.tool_descriptions},
            treatment_config={"system_prompt": treatment_config.system_prompt, "model_id": treatment_config.model_id, "tool_descriptions": treatment_config.tool_descriptions},
        )
        return StageOutcome(note=f"control={control.version_id} treatment={treatment.version_id}")

    def _bundle_ref(ctx: StageContext, which: str):
        from .agentcore_client import BundleRef

        ref = ctx.bag.get(f"{which}_ref")
        if ref is not None:
            return ref
        d = load_loop_state(ctx.store).get(f"{which}_ref")
        if not d:
            raise RuntimeError(f"no {which} bundle found; run the 'build-bundles' stage first")
        return BundleRef(bundle_arn=d["bundle_arn"], version_id=d["version_id"], bundle_id=d.get("bundle_id"))

    # -- offline check of treatment bundle ------------------------------
    def offline_check(ctx: StageContext) -> StageOutcome:
        cases = load_cases(cfg.offline_dataset)
        # Evaluate BOTH bundles on the same cases so the candidate is compared against its
        # true control (the same config that ships as the A/B control), not a stale prior
        # baseline. This makes the Candidate tab's before/after an apples-to-apples check.
        control_bundle = _bundle_ref(ctx, "control")
        treatment_bundle = _bundle_ref(ctx, "treatment")
        control_result = offline_check_bundle(
            ctx.client, ctx.agent, control_bundle, cases, "baseline_control", store=ctx.store, ingestion_wait=ingestion_wait
        )
        treatment_result = offline_check_bundle(
            ctx.client, ctx.agent, treatment_bundle, cases, "baseline_treatment", store=ctx.store, ingestion_wait=ingestion_wait
        )
        ctx.bag["control_check_baseline"] = control_result.baseline
        ctx.bag["candidate_baseline"] = treatment_result.baseline
        return StageOutcome(
            note=f"control scores={control_result.baseline.scores}; treatment scores={treatment_result.baseline.scores}"
        )

    # -- A/B test --------------------------------------------------------
    def ab_test(ctx: StageContext) -> StageOutcome:
        gateway = ctx.client.create_gateway(f"{cfg.runtime_name}Gateway{sfx}", ctx.agent.role_arn)
        target = ctx.client.create_gateway_target(gateway.gateway_id, f"{cfg.runtime_name}Target{sfx}", ctx.agent.runtime_arn)

        # Phase 2: optionally include the custom code-based trajectory evaluator so the
        # A/B scores the tool-trajectory dimension and can drive promotion on it.
        eval_ids = list(cfg.evaluators)
        traj_id = _ensure_trajectory_evaluator(ctx)
        if traj_id:
            eval_ids.append(traj_id)

        oe = ctx.client.create_online_eval(
            OnlineEvalConfig(
                name=f"{cfg.runtime_name}ABEval{sfx}", service_name=ctx.agent.service_name, log_groups=[ctx.agent.log_group],
                evaluators=eval_ids, role_arn=ctx.agent.role_arn, sampling_percentage=cfg.sampling_percentage,
            )
        )
        runner = ABTestRunner(ctx.client)
        handle = runner.start_config_bundle_test(
            f"{cfg.runtime_name}AB{sfx}", gateway_arn=gateway.gateway_arn, role_arn=ctx.agent.role_arn, online_eval_arn=oe.config_arn,
            control=_bundle_ref(ctx, "control"), treatment=_bundle_ref(ctx, "treatment"), control_weight=cfg.control_weight, treatment_weight=cfg.treatment_weight,
        )
        # Drive gateway traffic (sticky by session id) to accumulate a sample.
        sessions = load_traffic(cfg.traffic_dataset)
        for spec in sessions:
            import uuid as _uuid

            sid = str(_uuid.uuid4())
            for turn in spec["turns"]:
                ctx.client.send_via_gateway(gateway.gateway_url, target.name, sid, turn)
        # Poll for a reportable result over a bounded window; online evaluation scores
        # sessions asynchronously, so significance can take ~10-15 min to appear.
        from .optimization.abtest import determine_winner, determine_winner_any

        # Drive promotion on the trajectory evaluator when enabled, else GoalSuccessRate.
        primary = traj_id or cfg.evaluators[0]
        # Default window ~15 min (20 x 45s) so we don't finish before online eval aggregates.
        attempts = int(getattr(cfg, "ab_poll_attempts", 20))
        interval = float(getattr(cfg, "ab_poll_interval", 45))
        result = runner.poll(handle)
        for i in range(attempts):
            result = runner.poll(handle)
            # A winner counts if it's significantly better on ANY metric with no regression
            # (matches how promotion is decided), falling back to the primary evaluator.
            winner, significant = determine_winner_any(result.per_variant)
            if not significant:
                winner, significant = determine_winner(result.per_variant, primary)
            result.winner, result.significant = winner, significant
            ctx.emit(f"    poll {i + 1}/{attempts}: significant={significant} winner={winner}")
            if significant:
                break
            if i < attempts - 1:
                _time.sleep(interval)

        ctx.bag.update(ab_result=result, ab_handle=handle, gateway=gateway, gateway_target=target, ab_online_eval=oe)
        # Never clobber a previously-persisted significant result with a non-significant one
        # (e.g. if this run timed out before aggregation). Keep the better of the two.
        prior = load_loop_state(ctx.store).get("ab_result") or {}
        prior_sig = bool(prior.get("significant")) and prior.get("ab_test_id") == handle.ab_test_id
        if result.significant or not prior_sig:
            save_loop_state(
                ctx.store, ab_result=result,
                ab_handle={"ab_test_id": handle.ab_test_id, "name": handle.name},
                gateway={"gateway_id": gateway.gateway_id, "gateway_arn": gateway.gateway_arn, "gateway_url": gateway.gateway_url},
                gateway_target={"target_id": target.target_id, "name": target.name},
                ab_online_eval={"config_id": oe.config_id, "config_arn": oe.config_arn, "name": oe.name},
            )
        else:
            ctx.emit("    keeping the previously-persisted significant result (this poll was not significant yet)")
        if not result.significant:
            ctx.emit("    tip: online eval may still be aggregating — use 'Refresh A/B result' in the UI (or re-run) in a few minutes")
        return StageOutcome(note=f"ab_test={handle.ab_test_id} significant={result.significant} winner={result.winner}")

    # -- promotion gate (open pending approval; no rollout) --------------
    def promotion_gate(ctx: StageContext) -> StageOutcome:
        from .models import ABTestResult, BundleConfig as _BundleConfig

        gate = ctx.bag["promotion_gate"]
        ab_result = ctx.bag.get("ab_result")
        loop = load_loop_state(ctx.store)
        if ab_result is None:
            if not loop.get("ab_result"):
                raise RuntimeError("no A/B result found; run the 'ab-test' stage first")
            ab_result = ABTestResult.from_dict(loop["ab_result"])
        req = gate.evaluate_for_promotion(ab_result, agent_ref=ctx.agent.runtime_name)
        if req is None:
            return StageOutcome(note="no statistically significant winner; nothing to promote")
        # Prepare (but do not execute) the promotion; persist for the approve command.
        control_bundle_id = ctx.bag.get("control_bundle_id") or loop.get("control_bundle_id")
        treatment_config = ctx.bag.get("treatment_config")
        if treatment_config is None and loop.get("treatment_config"):
            treatment_config = _BundleConfig.from_dict(loop["treatment_config"])
        decision = PromotionDecision(
            strategy="config_bundle",
            ab_test_id=req.ab_test_id,
            bundle_id=control_bundle_id,
            agent_arn=ctx.agent.runtime_arn,
            config=treatment_config,
            commit_message="Promote treatment: A/B validated recommended configuration",
        )
        save_promotion_decision(ctx.store, req.request_id, decision)
        ctx.bag["approval"] = req
        ctx.bag["promotion_decision"] = decision
        ctx.emit(f"    pending approval {req.request_id} — run 'demo approve {req.request_id}' to promote")
        return StageOutcome(note=f"pending approval {req.request_id}", artifacts=[f"approvals/{req.request_id}.json"])

    runners = {
        "deploy": deploy,
        "offline-baseline": offline_baseline,
        "online": online,
        "recommend": recommend,
        "generate-config": generate_config,
        "build-bundles": build_bundles,
        "offline-check": offline_check,
        "ab-test": ab_test,
        "promotion-gate": promotion_gate,
    }
    return [Stage(name=s.name, description=s.description, run=runners[s.name], provisions=s.provisions) for s in STAGE_SPECS]
