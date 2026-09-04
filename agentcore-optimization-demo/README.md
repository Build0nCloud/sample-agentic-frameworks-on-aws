# AgentCore Optimization Demo

An end-to-end, runnable demonstration of the **Amazon Bedrock AgentCore optimization
loop** for a healthcare **patient-support agent**:

> deploy → offline baseline → online evaluation → recommendation → generate config →
> build bundles → offline check → A/B test → **promotion with human approval** → new baseline

It builds on the AWS sample vendored under [`reference/`](reference/README.md)
(`awslabs/amazon-bedrock-agentcore-samples` → `06-observe-evaluate-optimize-your-agent`)
and adds the two capabilities that sample lacks: a **human-approval promotion gate** and
a **unified, observable orchestrator/CLI**.

> ⚠️ **This demo always runs against real Amazon Bedrock AgentCore** (Runtime, Gateway,
> Evaluations, Optimization). There is no local/simulated mode. Running it provisions
> billable AWS resources — the CLI prints a cost notice and requires explicit
> confirmation before creating anything.

## What it demonstrates

- **Offline (batch) evaluation** of a curated, multi-turn dataset with a named baseline,
  a compiled report, and email delivery (with a local-outbox fallback).
- **Online evaluation** of the deployed agent, driven by scripted multi-turn traffic.
- **Configuration bundles** — versioned, immutable config snapshots (system prompt +
  tool descriptions) with lineage.
- **Recommendations** — AI-generated system-prompt / tool-description improvements.
- **A/B testing** — control vs treatment through AgentCore Gateway, with statistical
  significance (Welch's t-test).
- **Automatic promotion with human approval** — a significant winner opens a pending
  approval; a human `approve`/`reject` decision gates the 100% rollout, and every
  decision is audited.

The agent is a **healthcare patient-support assistant** (synthetic, non-PHI data; no
clinical advice) with 8 deterministic tools and two designed trajectories
(prescription refill; appointment scheduling) for tool-call and trajectory evaluation.

## Documentation

Alongside this README, four companion docs cover the demo in depth:

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the end-to-end flow, every artifact the loop
  produces, and a description of what each component does (both the UI and CLI entry points).
- **[DEMO_CHEATSHEET.md](DEMO_CHEATSHEET.md)** — the presenter runbook, **UI-first**: session
  setup, launching `demo-ui`, a tab-by-tab walkthrough, the edit-and-re-run workflow, plus
  troubleshooting and a CLI fallback section.
- **[demo-talk-track.md](demo-talk-track.md)** — a start-to-finish narration for presenting
  (what to say, what to click, when to flip to the AWS Console), including a 5-minute short
  version and a 20-25-minute pre-run version with no long waits.
- **[MANUAL_DEPLOYMENT.md](MANUAL_DEPLOYMENT.md)** — a **production, API-only** guide to stand
  up the whole loop in a fresh AWS account using raw AgentCore/AWS APIs (no UI, no CLI):
  account bootstrap, IAM, packaging, runtime, bundles, gateway, evaluations, custom
  evaluator, A/B, promotion, production hardening, and cleanup.

## Prerequisites

- Python 3.10–3.13 (3.13 recommended; heavy AWS/agent deps may lack 3.14 wheels).
- An AWS account with **Amazon Bedrock AgentCore** access, and the target foundation
  model (default `us.anthropic.claude-sonnet-5`, Claude Sonnet 5) enabled in your region.
- AWS credentials via the standard chain (`aws configure`, env vars, SSO, or a role).
- `pip` capable of building an ARM64 (`manylinux2014_aarch64`) wheel set for the agent
  deployment package.
- IAM permissions for `bedrock-agentcore*`, `bedrock:InvokeModel`, `iam`, `s3`, `logs`,
  `xray` (see the vendored sample's README for the detailed policy).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# (Re)fetch the read-only reference sample if needed:
bash scripts/fetch_reference.sh
```

## Configuration

All settings live in [`config/demo_config.yaml`](config/demo_config.yaml): AWS region,
runtime/gateway names, model id, evaluators, dataset paths, online-eval sampling, A/B
traffic weights, and optional report email settings. Credentials are **never** stored
there — they come from the standard AWS credential chain.

**Report email (optional).** Set `email.delivery` to `outbox` (default), `ses`, or
`smtp`:
- `outbox` — writes the rendered report to `artifacts/outbox/` (no email sent).
- `ses` — sends via Amazon SES using the AWS credential chain.
- `smtp` — sends via SMTP; credentials come from `SMTP_USERNAME` / `SMTP_PASSWORD`
  environment variables.
Delivery is best-effort: if it fails, the report is still kept locally. Email is
**optional** — the full report is always viewable in the Web UI's **Reports** tab and
persisted under `artifacts/reports/`, so you don't need SES/SMTP configured to see it.

## Running the demo

The single entry point is the `demo` CLI (installed by `pip install -e .`):

```bash
demo                       # list the stages (no AWS, no cost)
demo run-all               # run the full loop up to the approval gate (prompts to confirm)
demo run-all --yes         # non-interactive confirmation for scripted runs
demo offline-baseline      # run a single stage
demo promotion-status      # show pending promotion approvals
demo approve <request-id>  # human decision: promote the winning variant
demo reject  <request-id>  # human decision: keep the current baseline
```

Stage order: `deploy → offline-baseline → online → recommend → generate-config →
build-bundles → offline-check → ab-test → promotion-gate`. `generate-config` drafts the
editable control/treatment configs and `build-bundles` creates the bundles from them
(you can edit the drafts in the Web UI's Bundles tab in between). The `promotion-gate` stage opens a **pending approval** when
the A/B test has a statistically significant winner; nothing is rolled out until you run
`demo approve <id>` (or `demo reject <id>`). Artifacts (baselines, bundles, A/B results,
reports, approvals, and the audit log) are written under `artifacts/` for review.

## Web UI (Streamlit)

An optional Streamlit front end visualizes the A/B results and the audit trail and lets
a human approve/reject a pending promotion from the browser. It reads the same
`artifacts/` tree the CLI writes, and its approve/reject buttons drive the **same**
`PromotionGate` code path as `demo approve` / `demo reject` (a real AgentCore rollout).

```bash
pip install -e ".[ui]"     # install the optional UI extra (Streamlit)
demo-ui                     # launches Streamlit; open the printed http://localhost:8501
# extra args pass through to Streamlit, e.g.:
demo-ui --server.port 8502
```

The app has ten tabs, ordered to follow the loop:

- **Run demo** — run the optimization loop (all stages or a subset) directly from the
  browser. The loop runs in a background thread and streams progress here (per-stage
  status + a live log); it stops at `promotion-gate`, which opens a pending approval you
  then decide on the Approvals tab.
- **Chat** — talk to the **live** deployed agent in real time (multi-turn, with session
  memory). Proof the agent is a real runtime, not a recording. Each reply has an on-demand
  **Show tools called** button that reconstructs the tool trajectory from CloudWatch traces.
- **Online** — results from the `online` stage: sessions driven, sampling, the
  online-eval config, aggregate scores, and the per-session tool trajectories.
- **Recommendation** — the `recommend` stage output: the proposed system prompt shown
  **before → after** against the baseline (control) prompt, plus any tool-description
  changes.
- **Bundles** — view and **edit** the control and treatment bundle configs (model id +
  system prompt) drafted by the `generate-config` stage, then **Build & run A/B** to
  rebuild the bundles from your edits and run the A/B test on them.
- **Candidate** — the combined `build-bundles` + `offline-check` step: the packaged
  candidate (treatment) configuration and its offline-check regression scores shown
  before → after against the control, so you can confirm no regressions before going live.
- **A/B Results** — per-evaluator control-vs-treatment comparison, the winning variant,
  and significance.
- **Reports** — the offline evaluation report shown directly in the browser: aggregate
  scores and a per-case breakdown (tools called, each evaluator's pass/score/explanation,
  and the agent's response). No email required — email/outbox delivery still runs as a
  separate demonstration of that capability.
- **Approvals** — pending and decided requests with the metrics at request time.
- **Audit trail** — every recorded decision.

Running the loop and approving/rejecting are live actions that provision and mutate real
Amazon Bedrock AgentCore resources and **incur AWS costs**, so they are guarded behind an
**"Enable live actions"** toggle in the sidebar (plus a typed confirmation of the request
id for approvals) and require valid AWS credentials in the environment running the app. A
full run takes ~25-40 minutes; running individual stages is much faster and is more robust
with short-lived credentials.

> ⚠️ **Cost notice:** as with the CLI, running the loop from the UI creates billable
> AWS resources (runtime, gateway, online evaluations, configuration bundles, A/B tests,
> the evaluator Lambda, an S3 bucket). Tear them down when done (see **Teardown** below).
> The cost warnings are intentionally kept out of the demo UI itself and documented here.

## Teardown

The live demo creates real AWS resources (runtime, gateway + target, online-eval
configs, configuration bundles, IAM role, S3 objects). Tear them down with the vendored
sample's cleanup script, pointed at your runtime name, or delete them via the console:

```bash
python reference/amazon-bedrock-agentcore-samples/01-features/06-observe-evaluate-optimize-your-agent/03-optimize/cleanup.py --name PatientSupport
```

(Adjust `--name` to match `agent.runtime_name` in your config.)

## Testing

Unit and property-based tests run entirely against stubbed/fake AWS clients — no AWS, no
cost:

```bash
pytest                      # unit + property tests (live tests excluded by default)
pytest tests/property       # the consolidated Correctness Properties suite (P1-P10)
```

The single test that touches real AWS is the opt-in live smoke test, guarded by both the
`live` marker and the `RUN_LIVE` env var so it never runs by default:

```bash
RUN_LIVE=1 pytest -m live tests/test_live_smoke.py
```

## Layout

```
agent/ ................ deployable patient-support agent + tool suite (src/agentcore_demo/agent)
src/agentcore_demo/ ... orchestration library:
  agentcore_client.py .. single service layer over real AgentCore
  deploy.py ............ runtime deployment (adapts the sample deploy.py)
  evaluation/ .......... offline (batch) + online evaluation + evaluators
  optimization/ ........ recommendations, bundles, A/B testing, promotion gate
  reporting/ ........... evaluation-report email delivery (+ outbox fallback)
  orchestrator.py ...... stage runner + wiring
  demo.py .............. CLI entry point
  ui/ ................. optional Streamlit front end (data.py read layer, actions.py
                        approve/reject, app.py dashboard, launch.py `demo-ui` entry)
  config.py, state.py .. configuration and artifact/state persistence
config/ ............... demo_config.yaml
datasets/ ............. curated multi-turn evaluation + traffic datasets
artifacts/ ............ generated outputs (gitignored)
reference/ ............ read-only AWS sample this demo adapts from
tests/ ................ unit + property-based tests (stubbed AWS) + opt-in live smoke test
```
