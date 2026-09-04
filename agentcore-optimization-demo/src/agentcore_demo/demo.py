"""Demo CLI entry point.

Single documented entry point for the AgentCore optimization demo. With no arguments
it lists the available stages; subcommands run individual stages or the full loop, and
approve/reject a pending promotion. Because every run uses real AgentCore, any command
that provisions billable AWS resources prints a cost notice and requires explicit
confirmation (interactive ``y/N`` or the non-interactive ``--yes`` flag).

Requirements: 1.7, 1.8, 7.5, 8.3, 8.4
"""

from __future__ import annotations

import argparse
from typing import Callable

from .orchestrator import RUN_ALL_STAGES, STAGE_SPECS, requires_provisioning

# A builder turns parsed args into a ready (orchestrator, context) pair. Injected in
# tests; the default (real AWS wiring) is provided by build_stages in task 17.1.
Builder = Callable[[argparse.Namespace], tuple]

COST_NOTICE = (
    "This will create and use REAL Amazon Bedrock AgentCore resources "
    "(runtime, gateway, evaluations, bundles, A/B tests) and will incur AWS costs."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="demo", description="Amazon Bedrock AgentCore optimization demo")
    parser.add_argument("--config", default=None, help="Path to demo_config.yaml (defaults to config/demo_config.yaml)")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip the interactive confirmation before provisioning resources")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="List the available demo stages")
    sub.add_parser("run-all", help="Run the full optimization loop (up to the approval gate)")
    for spec in STAGE_SPECS:
        sub.add_parser(spec.name, help=spec.description)
    sub.add_parser("promotion-status", help="Show pending promotion approvals")
    ap = sub.add_parser("approve", help="Approve a pending promotion")
    ap.add_argument("request_id")
    rj = sub.add_parser("reject", help="Reject a pending promotion")
    rj.add_argument("request_id")
    return parser


def list_stages_text() -> str:
    lines = ["Demo stages (run in this order):", ""]
    for i, spec in enumerate(STAGE_SPECS, start=1):
        flag = "  [creates AWS resources]" if spec.provisions else ""
        lines.append(f"  {i}. {spec.name:<16} {spec.description}{flag}")
    lines += [
        "",
        "Commands:",
        "  run-all             Run all stages above (up to the approval gate)",
        "  <stage-name>        Run a single stage",
        "  promotion-status    Show pending promotion approvals",
        "  approve <id>        Approve a pending promotion (human decision)",
        "  reject <id>         Reject a pending promotion (human decision)",
        "",
        "Use --yes to skip the confirmation prompt before provisioning resources.",
    ]
    return "\n".join(lines)


def confirm_provisioning(assume_yes: bool, emit: Callable[[str], None], input_fn: Callable[[str], str]) -> bool:
    """Return True if provisioning is confirmed (R8.3, R8.4)."""
    emit(COST_NOTICE)
    if assume_yes:
        emit("Proceeding (--yes).")
        return True
    answer = input_fn("Proceed and create real AWS resources? [y/N]: ")
    return answer.strip().lower() in ("y", "yes")


def _selected_stages(command: str) -> list[str]:
    if command == "run-all":
        return list(RUN_ALL_STAGES)
    return [command]  # a single stage name


def main(argv: list[str] | None = None, *, builder: Builder | None = None, emit: Callable[[str], None] = print, input_fn: Callable[[str], str] = input) -> int:
    args = build_parser().parse_args(argv)
    command = args.command

    # No command, or explicit `list`: show the stages (no AWS, no confirmation).
    if command is None or command == "list":
        emit(list_stages_text())
        return 0

    if builder is None:
        builder = _default_builder

    stage_names = {s.name for s in STAGE_SPECS}

    if command in stage_names or command == "run-all":
        selected = _selected_stages(command)
        if requires_provisioning(selected) and not confirm_provisioning(args.yes, emit, input_fn):
            emit("Aborted: provisioning not confirmed. No AWS resources were created.")
            return 2
        orchestrator, context = builder(args)
        try:
            orchestrator.run(context, only=selected)
        except Exception as exc:  # OrchestratorError or setup failure
            emit(f"Run failed: {exc}")
            return 1
        if command == "run-all":
            from .orchestrator import maybe_emit_before_after

            maybe_emit_before_after(context)
        return 0

    if command in ("approve", "reject", "promotion-status"):
        orchestrator, context = builder(args)
        gate = context.bag.get("promotion_gate")
        if gate is None:
            emit("No promotion gate is available in this context.")
            return 1
        if command == "promotion-status":
            pending = gate.pending()
            if not pending:
                emit("No pending promotion approvals.")
            for req in pending:
                emit(f"{req.request_id}  variant={req.winning_variant}  ab_test={req.ab_test_id}  status={req.status.value}")
            return 0
        # approve / reject
        try:
            if command == "approve":
                decision = context.bag.get("promotion_decision")
                gate.approve(args.request_id, decider="cli", promotion_decision=decision)
                emit(f"Approved and promoted: {args.request_id}")
            else:
                gate.reject(args.request_id, decider="cli")
                emit(f"Rejected: {args.request_id}")
        except Exception as exc:  # noqa: BLE001
            emit(f"{command} failed: {exc}")
            return 1
        return 0

    emit(list_stages_text())
    return 0


def _default_builder(args: argparse.Namespace) -> tuple:
    """Build the real (orchestrator, context) wired to AgentCore."""
    from .agentcore_client import AgentCoreClient
    from .config import load_config
    from .optimization.promotion import PromotionGate
    from .orchestrator import Orchestrator, build_stages, load_agent, load_promotion_decision, make_context
    from .state import StateStore

    config = load_config(args.config)
    store = StateStore(config.artifacts_dir)
    client = AgentCoreClient(config)
    agent = load_agent(store)
    context = make_context(config, store, client=client, agent=agent, emit=print, assume_yes=args.yes)
    context.bag["promotion_gate"] = PromotionGate(client, store)

    request_id = getattr(args, "request_id", None)
    if request_id:
        decision = load_promotion_decision(store, request_id)
        if decision is not None:
            context.bag["promotion_decision"] = decision

    return Orchestrator(build_stages(context)), context


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
