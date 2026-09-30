# AgentCore Optimization Demo

An end-to-end, runnable demonstration of the **Amazon Bedrock AgentCore optimization loop**
for a healthcare **patient-support agent** — taking a deployed agent from a measured quality
baseline, through a data-driven improvement, to a safe rollout gated by human approval:

```
deploy → offline baseline → online evaluation → recommendation → generate config
→ build bundles → offline check → A/B test → promotion (human approval) → new baseline
```

It builds on the AWS sample vendored under [`reference/`](reference/) and adds the two
capabilities that sample lacks: a **human-approval promotion gate** and a **unified,
observable orchestrator** with both a CLI and a Streamlit web UI.

> ⚠️ **This demo always runs against real Amazon Bedrock AgentCore** (Runtime, Gateway,
> Evaluations, Optimization). There is no local/simulated mode. Running it provisions
> **billable AWS resources** — see [Teardown](#teardown).

This README is self-contained (overview + quickstart + architecture + demo instructions).
Two companion docs go deeper where noted:
- **[QUICKSTART.md](QUICKSTART.md)** — the condensed fresh-account checklist.
- **[MANUAL_DEPLOYMENT.md](MANUAL_DEPLOYMENT.md)** — the same loop as raw AWS API calls
  (no CLI/UI), for production / infrastructure-as-code.

---

## Contents

1. [What it demonstrates](#1-what-it-demonstrates)
2. [Quickstart (fresh AWS account)](#2-quickstart-fresh-aws-account)
3. [Architecture](#3-architecture)
   - [High-level flow](#31-high-level-flow)
   - [The 9 stages](#32-the-9-stages)
   - [Data flow across the loop](#33-data-flow-across-the-loop)
   - [Components](#34-components)
   - [Runtime artifacts](#35-runtime-artifacts)
   - [Configuration](#36-configuration)
   - [How promotion decisions are made](#37-how-promotion-decisions-are-made)
4. [Running the demo — Web UI](#4-running-the-demo--web-ui)
5. [Running the demo — CLI](#5-running-the-demo--cli)
6. [Testing](#6-testing)
7. [Teardown](#teardown)
8. [Repository layout](#repository-layout)

---

## 1. What it demonstrates

- **Offline (batch) evaluation** of a curated, multi-turn dataset with a named baseline,
  a compiled report, and optional email delivery (local-outbox fallback).
- **Online evaluation** of the deployed agent, driven by scripted multi-turn traffic.
- **Recommendations** — AI-generated system-prompt improvements from production traces.
- **Configuration bundles** — versioned, immutable config snapshots (system prompt + model +
  tool descriptions) with lineage, injected into the runtime per request (no redeploy).
- **A/B testing** — control vs treatment through an AgentCore Gateway, scored by built-in
  evaluators **and** a custom code-based (Lambda) evaluator, with statistical significance.
- **Promotion with human approval** — a significant winner opens a *pending approval*; a human
  `approve`/`reject` decision gates the 100% rollout, and every decision is audited.

The agent is a **healthcare patient-support assistant** (synthetic, non-PHI data; no clinical
advice) with 8 deterministic tools and two designed trajectories (prescription refill;
appointment scheduling) for tool-call and trajectory evaluation.

**Two ways to drive the loop, over the same code and artifacts:**
- a **Streamlit web UI** (`demo-ui`) — the primary demo surface; and
- a **CLI** (`demo`) — the scriptable equivalent.

---

## 2. Quickstart (fresh AWS account)

Everything is account-agnostic — names and IDs are derived from your account at runtime;
nothing is hardcoded. (Condensed version: [QUICKSTART.md](QUICKSTART.md).)

### 2.1 Prerequisites

**a. AgentCore available in your region** — confirm reachability:
```bash
export AWS_REGION=us-east-1     # or your region (AgentCore must be available there)
aws bedrock-agentcore-control list-agent-runtimes --region "$AWS_REGION" >/dev/null \
  && echo "AgentCore reachable"
```

**b. Model access** — enable the model in **Bedrock → Model access** (default is Claude
Sonnet 5), then verify:
```bash
aws bedrock-runtime converse --region "$AWS_REGION" \
  --model-id us.anthropic.claude-sonnet-5 \
  --messages '[{"role":"user","content":[{"text":"say ok"}]}]' \
  --inference-config '{"maxTokens":10}' \
  --query 'output.message.content[0].text' --output text        # → ok
```

**c. Credentials** — standard AWS chain, with permission to create IAM roles, S3 buckets,
Lambda functions, and AgentCore resources:
```bash
aws sts get-caller-identity --query Account --output text
```

**d. Local tooling** — Python **3.10–3.13** (3.13 recommended; 3.14 lacks wheels for the
AWS/agent deps), a `pip` that can fetch prebuilt wheels for `manylinux2014_aarch64` (runtime
package) and `manylinux2014_x86_64` (Lambda evaluator), plus `git` and `zip`.

### 2.2 Install

```bash
git clone <this-repo-url> agentcore-optimization-demo
cd agentcore-optimization-demo

python3 -m venv .venv && source .venv/bin/activate     # use a 3.10–3.13 interpreter
pip install -e ".[dev,ui]"        # dev = tests; ui = the Streamlit front end
pip install pip                   # ensure pip is inside the venv (deploy packages deps with it)

pytest -q                         # optional: unit + property tests, no AWS, no cost
```

> The vendored sample under `reference/` is **not** required to run the loop (the demo code
> doesn't import it). It's only used by the cleanup shortcut; re-fetch with
> `bash scripts/fetch_reference.sh` if needed.

### 2.3 Configure (optional)

All settings live in [`config/demo_config.yaml`](config/demo_config.yaml) (see
[§3.6](#36-configuration)). Defaults work out of the box. Credentials are **never** stored
there — they come from the AWS credential chain. Region resolves from
`AWS_REGION`/`AWS_DEFAULT_REGION` or the YAML (default `us-east-1`).

### 2.4 Run

- **UI:** `demo-ui` → open the printed `http://localhost:8501` → enable **live actions** →
  **🚀 Run demo** → **▶ Run all stages** → approve on the **✅ Approvals** tab. See [§4](#4-running-the-demo--web-ui).
- **CLI:** `demo --yes run-all`, then `demo approve <id>`. See [§5](#5-running-the-demo--cli).

A full run is ~25–40 min (the A/B online evaluation aggregates asynchronously, ~10–15 min).
Individual stages are much faster; state persists under `artifacts/` between runs.

When you're done, **[tear it down](#teardown)** to stop costs.

---

## 3. Architecture

### 3.1 High-level flow

```
     ui/ (Streamlit web app)                     demo.py (CLI entry point)
     app · data · actions · runner · launch      subcommands: <stage> | run-all
     11 tabs; runs stages in a bg thread;         | promotion-status | approve/reject
     chat; edit configs; approve                        │
            │      └──────────────┬───────────────────┘
            │  (both build the same orchestrator + StageContext)
            ▼                     ▼
                                 orchestrator.py
                    (ordered Stage runner; per-run Manifest; before/after)
 ┌────────┬──────────────┬────────┬─────────┬───────────────┬─────────────┬───────────┬──────────┬─────────────┐
 ▼        ▼              ▼        ▼         ▼               ▼             ▼           ▼          ▼             ▼
deploy offline-baseline online recommend generate-config build-bundles offline-check ab-test  promotion-gate  approve/reject
                                        │  all stages call ↓ (never boto3 directly)
                                        ▼
                              agentcore_client.py
        (single service layer over real AgentCore: data-plane + control-plane,
         logs / iam / s3 / lambda; retries, timing waits, response parsing)
                                        │
                                        ▼
                          Amazon Bedrock AgentCore (real)
     Runtime · Gateway + Targets · Configuration Bundles · Evaluations (batch/online)
     · Recommendations · A/B tests · (custom code-based evaluator Lambda)

   state.py ──► artifacts/  (baselines, bundles, ab-results, approvals, audit,
                             reports, outbox, config_drafts, per-run manifest, loop_state)
   config.py ──► config/demo_config.yaml  +  AWS credential chain
```

**Two decoupling ideas make the whole thing work:**

1. **One AWS seam.** Every stage talks to `AgentCoreClient`; nothing else imports `boto3`.
   Credentials, retries, timing waits, and response parsing live in one testable place.
2. **State on disk, not just memory.** Because each `demo <stage>` is a separate OS process,
   stages persist outputs to `artifacts/` (`agent_state.json`, `loop_state.json`, approvals,
   audit). Later stages/commands reload from there, so the loop can run stage-by-stage across
   separate invocations — and the UI reads the same tree.

### 3.2 The 9 stages

Legend: **[P]** creates billable AWS resources (guarded by the confirmation gate).

| # | Stage | Reads | Does | Writes / AWS effect |
|---|---|---|---|---|
| 1 | **deploy** **[P]** | agent code, config | Package agent → S3 → create Runtime + IAM role; poll READY | `AgentHandle` → `artifacts/agent_state.json`; Runtime, role, S3 object |
| 2 | **offline-baseline** | `offline_multiturn.jsonl`, agent | Replay cases → reconstruct tool calls from traces → score (custom + built-in batch) → build report → email/outbox | `Baseline` + `report.{json,md,html}` (`baseline_pre`) |
| 3 | **online** **[P]** | `traffic_sessions.jsonl`, agent | Create online-eval config; drive multi-turn traffic; score the sessions | Online-eval config; `online_result` → `loop_state.json`; CloudWatch traces |
| 4 | **recommend** | baseline prompt, traces | Ask the Recommendations API for an improved prompt targeting GoalSuccessRate | `recommendation` (incl. target metric) → `loop_state.json` |
| 5 | **generate-config** | recommendation, base config | Compute **control** (weak baseline) + **treatment** (recommended) configs and write **editable YAML drafts** — no AWS | `config_drafts/{control,treatment}.yaml` |
| 6 | **build-bundles** **[P]** | the drafts (or computed configs) | Create control + treatment configuration bundle versions from the (possibly edited) drafts | Bundle versions + refs → `loop_state.json` |
| 7 | **offline-check** | control + treatment bundles, dataset | Batch-evaluate **both** bundles on the same cases to catch regressions before going live | `baseline_control` + `baseline_treatment` reports |
| 8 | **ab-test** **[P]** | control/treatment refs | Create gateway + target + online-eval (incl. the custom trajectory evaluator when enabled); start A/B test; drive gateway traffic; poll for significance | `ABTestResult` + handle → `loop_state.json`; gateway/target/online-eval/A-B test, evaluator Lambda |
| 9 | **promotion-gate** | `ab_result` | If a significant winner exists, open a `PENDING_APPROVAL` (no rollout) | `ApprovalRequest` → `artifacts/approvals/`; promotion decision persisted |
| — | **approve / reject** | approval id | Human decision: stop A/B, promote winner into baseline bundle + audit, or keep baseline + audit | Updated approval, `artifacts/audit/audit-log.jsonl`, bundle update |

> **generate-config + build-bundles** split the old single `bundle` stage so you can **edit**
> the control/treatment configs (model id + system prompt) between drafting and building — the
> UI's **Bundles** tab reads/writes these YAML drafts, and **Build & run A/B** rebuilds from the
> edits. `run-all` runs both straight through (no pause).

### 3.3 Data flow across the loop

```
 datasets/offline_multiturn.jsonl ─┐
                                   ▼
 (2) offline-baseline ── send sessions ──► AgentCore Runtime (Claude Sonnet 5)
        │                                        │ OTel spans
        │  reconstruct tool calls  ◄────────── CloudWatch aws/spans
        │  custom + built-in scores
        ▼
   Baseline + Report ──► artifacts/reports + emailer (SES/outbox)

 (3) online ── create online-eval; drive traffic ──► Runtime ──► CloudWatch (scored async)

 (4) recommend ── traces + baseline prompt ──► Recommendations API ──► improved prompt
 (5) generate-config ── control(weak) & treatment(recommended) ──► editable YAML drafts
                                                    │  (optionally edited in the UI Bundles tab)
 (6) build-bundles ── drafts ──► Configuration Bundles ── control_ref / treatment_ref
 (7) offline-check ── control + treatment bundles ──► batch eval ──► baseline_control / baseline_treatment

 (8) ab-test ── Gateway ─┬─ 50% ─► control bundle  ─┐
                         └─ 50% ─► treatment bundle ─┤──► Runtime ──► CloudWatch
                Online eval (built-ins + TrajectoryQuality Lambda) scores each session
                                                     ▼
                              ABTestResult (per-variant means, p-values, significance)
                                                     │
 (9) promotion-gate ── significant winner? ──► ApprovalRequest (PENDING_APPROVAL)  ⟵ rollout BLOCKED
                                                     │
   approve <id> ── stop A/B ─► promote treatment config into control bundle ─► new baseline
                             └─► audit-log.jsonl
```

### 3.4 Components

| Module | Responsibility |
|---|---|
| **`demo.py`** | CLI entry point. Lists stages; runs one stage / `run-all` / `promotion-status` / `approve`/`reject`. Prints a cost notice + requires confirmation before provisioning (`y/N` or `--yes`). |
| **`orchestrator.py`** | The stage engine: runs the 9 stages in order, records a per-run manifest, halts on failure. `build_stages()` wires stages to modules; helpers persist/reload `loop_state.json`, `agent_state.json`, config drafts, and the promotion decision. |
| **`agentcore_client.py`** | **The only module that touches AWS.** Wraps the AgentCore data-plane + control-plane (plus logs/iam/s3/lambda): credential resolution, retry/backoff, timing waits, SigV4 gateway invokes, CloudWatch-spans → tool-trajectory reconstruction, response parsing. Injectable clients so tests use fakes. |
| **`deploy.py` / `lambda_deploy.py`** | Package + deploy the agent to Runtime (ARM64) and the custom evaluator Lambda (x86_64); create/reuse IAM roles; grant AgentCore invoke permission. |
| **`agent/patient_support_agent.py`** | The deployed Strands agent (Claude Sonnet 5), 8 deterministic tools, synthetic non-PHI data, declines clinical advice. A **config-bundle hook** reads the injected bundle each call and overrides the system prompt + tool descriptions — this is what lets A/B variants change behavior without redeploying code. |
| **`evaluation/evaluators.py`** | References the **built-in** evaluators (GoalSuccessRate, Helpfulness, Correctness — LLM judges, server-side) and defines **custom local** evaluators: `ToolCall`, `Trajectory`, `ClinicalSafety`. |
| **`evaluation/offline.py` / `online.py`** | The batch-evaluation pipeline (+ report builder) and the online-eval lifecycle + traffic driver. |
| **`optimization/`** | `recommendations.py` (prompt recommendations), `bundles.py` (versioned config bundles + offline check), `abtest.py` (A/B lifecycle + Welch's t-test + winner logic), `promotion.py` (**the promotion gate** — the capability the sample lacks: approval, rollout, audit). |
| **`lambdas/trajectory_evaluator/`** | A SESSION-level **code-based** evaluator (Lambda) that AgentCore online eval invokes; scores tool-trajectory quality without ground truth so the A/B can measure it on live traffic. |
| **`ui/`** | The Streamlit app: `app.py` (tabs), `data.py` (read-only view over `artifacts/`), `actions.py` (chat / approve / reject / refresh — the only live-AWS UI code), `runner.py` (runs stages in a background thread), `launch.py` (`demo-ui` entry). |
| **`config.py` / `state.py` / `models.py`** | Config load/validate; the `artifacts/` store + run manifest; shared dataclasses. |
| **`reporting/emailer.py`** | Delivers the offline report via SES/SMTP or a local outbox; best-effort (never aborts a run). |

### 3.5 Runtime artifacts

Everything generated lands under `artifacts/` (gitignored):

| Path | Written by | Contents |
|---|---|---|
| `agent_state.json` | deploy | The `AgentHandle` (runtime id/arn, log group, service name, role, region) |
| `loop_state.json` | online/recommend/build-bundles/ab-test | Cross-stage state: recommendation, `online_result`, control/treatment configs + bundle refs, A/B handle + result, evaluator id |
| `config_drafts/{control,treatment}.yaml` | generate-config | Editable draft configs (model id + system prompt) |
| `baselines/<name>.json` · `reports/<name>/report.{json,md,html}` | offline eval | Named baselines + full reports (`baseline_pre`, `baseline_control`, `baseline_treatment`) |
| `approvals/<id>.json` · `bundles/decision-<id>.json` | promotion-gate | Pending/decided approval + the persisted promotion decision |
| `audit/audit-log.jsonl` | approve/reject | Append-only audit entries |
| `runs/<run-id>.json` | orchestrator | Per-run manifest |

### 3.6 Configuration

All settings live in [`config/demo_config.yaml`](config/demo_config.yaml):

| Key | Meaning |
|---|---|
| `aws.region` | Region for all AgentCore calls (env `AWS_REGION` overrides) |
| `agent.runtime_name` / `gateway_name` | Resource name prefixes |
| `agent.model_id` | Foundation model (default `us.anthropic.claude-sonnet-5`) |
| `agent.baseline_system_prompt` | Optional **deliberately weak** control prompt so the recommended treatment has real headroom to win |
| `evaluators` | Built-in evaluators applied by offline/online evaluation |
| `datasets.offline` / `datasets.traffic` | Dataset paths |
| `online_eval.sampling_percentage` | % of sessions scored online |
| `ab_testing.*_weight` | Control/treatment traffic split (50/50 config-bundle; 90/10 canary) |
| `enable_trajectory_evaluator` | Deploy + include the custom trajectory Lambda in the A/B online eval |
| `email.{delivery,target,sender,...}` | Report delivery (`ses`/`smtp`/`outbox`); optional; secrets from env only |
| `ui.display_timezone` | Timezone for UI timestamps (shown alongside UTC): an IANA name or `local` |

Credentials are resolved via the standard AWS chain and are never stored in config.

**Report email is optional.** With `email.delivery: outbox` (default) the report is written to
`artifacts/outbox/`; `ses`/`smtp` send it (SMTP creds come from `SMTP_USERNAME`/`SMTP_PASSWORD`
env vars). The full report is always viewable in the UI's **Reports** tab regardless.

### 3.7 How promotion decisions are made

The gate promotes only on a **statistically significant winner**:

- **`determine_winner`** — treatment wins if significantly better (p < 0.05) *and* improved on
  a chosen **primary** evaluator.
- **`determine_winner_any`** — treatment wins if significantly better on **≥1** evaluator and
  **not significantly worse** on any (decisive, no-regression). Used when the headline
  improvement can't yield a p-value on its own.

Observed live (weak control vs recommended treatment):

| Evaluator | Control | Treatment | p-value | significant |
|---|---|---|---|---|
| **TrajectoryQuality** (custom Lambda) | 0.00 | 1.00 | none* | — |
| Builtin.GoalSuccessRate | 0.00 | 0.70 | 0.061 | no |
| Builtin.Helpfulness | 0.60 | 0.88 | 0.0005 | ✅ |
| Builtin.Correctness | 0.80 | 1.00 | 0.24 | no |

\* A *perfect* 0→1 separation has zero within-group variance, so a t-test can't produce a
p-value. Promotion fired via `determine_winner_any`: the treatment was significantly better on
**Helpfulness** with no regressions, and the trajectory metric corroborated the win.

---

## 4. Running the demo — Web UI

The Streamlit front end is the primary demo surface. It reads the same `artifacts/` tree the
CLI writes, and its approve/reject buttons drive the **same** `PromotionGate` code path as the
CLI (a real AgentCore rollout).

```bash
pip install -e ".[ui]"     # if you didn't install the [ui] extra already
demo-ui                     # opens http://localhost:8501  (demo-ui --server.port 8502 to change)
```

**Sidebar:** **🔄 Refresh artifacts** reloads from disk (use it whenever a stage finishes and the
UI looks stale); **Enable live actions** is required for chatting, running stages, and approvals
(needs valid AWS creds). Timestamps show in a configurable timezone + UTC.

**The eleven tabs, ordered to follow the loop:**

- **🚀 Run demo** — run stages (all or a subset) from the browser. Runs in a background thread and
  streams per-stage status + a live log; `build-bundles` + `offline-check` are grouped; it stops
  at `promotion-gate`, which opens a pending approval. One run at a time.
- **💬 Chat** — talk to the **live** deployed agent (multi-turn, session memory). Proof it's real,
  not a recording. Each reply has an on-demand **Show tools called** button that reconstructs the
  tool trajectory from CloudWatch traces (traces lag ~1–2 min; **Retry** if empty).
- **📚 Dataset** — the evaluation inputs: the offline ground-truth cases (with expected tool
  trajectories + assertions) and the online/A-B traffic sessions, plus the JSONL file format so
  you can build your own.
- **📋 Reports** — the offline evaluation report in the browser: aggregate scores + a per-case
  breakdown (tools called, each evaluator's pass/score/explanation, and the agent's response).
  No email required.
- **📡 Online** — the `online` stage results: sessions driven, sampling, the online-eval config,
  aggregate scores, and per-session tool trajectories.
- **💡 Recommendation** — the `recommend` output: the **target metric** it optimized for, plus the
  proposed system prompt shown **before → after** against the baseline prompt.
- **🧾 Bundles** — view and **edit** the control and treatment configs (model id + system prompt)
  drafted by `generate-config`; **💾 Save** writes the draft (no AWS); **▶ Build & run A/B** rebuilds
  the bundles from your edits and runs `build-bundles → offline-check → ab-test`.
- **🧪 Candidate** — the offline-check regression view: control → candidate scores with deltas, so
  you confirm no regressions before spending live A/B traffic.
- **📊 A/B Results** — per-evaluator control-vs-treatment table + chart, the winner, and
  significance. **🔄 Refresh A/B result** re-polls the live test (online eval aggregates ~10–15 min).
- **✅ Approvals** — pending + decided requests with the metrics at request time. **The human
  moment:** enable live actions, expand the pending `appr-…`, type its id to confirm, and click
  **Approve & promote** (or **↻ Reopen pending approval** if none is pending).
- **📜 Audit trail** — every recorded decision (decider, decision, metrics at decision, outcome).

**Edit-and-A/B-test-your-own-values workflow:** run through `generate-config` → on **🧾 Bundles**
edit the model id and/or system prompt for control/treatment and **Save** → **▶ Build & run A/B**.
`run-all` does *not* pause for edits (it uses the generated configs straight through).

> Running the loop and approving/rejecting are live actions that provision and mutate real
> AgentCore resources and **incur AWS costs**, so they're guarded behind the **Enable live actions**
> toggle (plus a typed confirmation for approvals). A full run is ~25–40 min; individual stages are
> faster and more robust with short-lived credentials.

---

## 5. Running the demo — CLI

The `demo` CLI (installed by `pip install -e .`) is the scriptable equivalent — one command per
stage, plus approve/reject. Each stage persists to `artifacts/`, so you can run them one at a time
across separate invocations.

```bash
demo                          # list the 9 stages (no AWS, no cost)
demo --yes run-all            # run deploy → … → promotion-gate (prompts without --yes)
demo --yes <stage>            # run a single stage, e.g. demo --yes deploy
demo generate-config          # draft editable configs (no AWS); edit the YAML, then:
demo --yes build-bundles      # build bundles from the (edited) drafts
demo promotion-status         # list pending approvals
demo approve <appr-id>        # human decision: promote the winning variant
demo reject  <appr-id>        # human decision: keep the current baseline
```

**Stage order:** `deploy → offline-baseline → online → recommend → generate-config →
build-bundles → offline-check → ab-test → promotion-gate`. The `promotion-gate` stage opens a
**pending approval** when the A/B test has a statistically significant winner; nothing is rolled
out until you `approve`/`reject`.

**Timing (⏱️ approx; `[P]` = billable):**
```
deploy [P] ~2-3m → offline-baseline ~8-12m → online [P] ~4-6m → recommend ~3-5m
→ generate-config [seconds] → build-bundles [P] ~30s → offline-check ~5-8m
→ ab-test [P] ~6m drive + ~10-15m to reach significance → promotion-gate [seconds]
```

The A/B poll log shows "no winner" for the first ~13 polls, then flips once online eval
aggregates — that's the aggregation lag, not a failure. If a run finishes before significance,
re-run `ab-test` or use the UI's **🔄 Refresh A/B result**.

---

## 6. Testing

Unit and property-based tests run entirely against stubbed/fake AWS clients — no AWS, no cost:

```bash
pytest                      # unit + property tests (live tests excluded by default)
pytest tests/property       # the consolidated Correctness Properties suite (P1-P10)
```

The only test that touches real AWS is an opt-in live smoke test, guarded by both the `live`
marker and the `RUN_LIVE` env var so it never runs by default:

```bash
RUN_LIVE=1 pytest -m live tests/test_live_smoke.py
```

---

## Teardown

The live demo creates real AWS resources (runtime, gateway + target, online-eval configs,
configuration bundles, A/B tests, the trajectory-evaluator Lambda, IAM roles, an S3 object).
Tear them down with the vendored sample's cleanup script (adjust `--name` to your
`agent.runtime_name`):

```bash
bash scripts/fetch_reference.sh   # if you haven't fetched reference/ yet
python reference/amazon-bedrock-agentcore-samples/01-features/06-observe-evaluate-optimize-your-agent/03-optimize/cleanup.py --name PatientSupport
```

That script does not remove the trajectory-evaluator Lambda, the registered code evaluator, or
the Lambda execution role — delete those separately. For the full per-resource teardown commands
(and the exact API calls), see **[MANUAL_DEPLOYMENT.md](MANUAL_DEPLOYMENT.md) → Cleanup**.

---

## Repository layout

```
src/agentcore_demo/ ... orchestration library:
  agentcore_client.py .. single service layer over real AgentCore (the only AWS seam)
  deploy.py / lambda_deploy.py  runtime + evaluator-Lambda packaging/deploy
  agent/ ............... deployable patient-support agent + 8-tool suite
  evaluation/ .......... offline (batch) + online evaluation + evaluators
  optimization/ ........ recommendations, bundles, A/B testing, promotion gate
  reporting/ ........... evaluation-report email delivery (+ outbox fallback)
  ui/ .................. optional Streamlit front end (app/data/actions/runner/launch)
  orchestrator.py ...... stage runner + build_stages() wiring
  demo.py .............. CLI entry point
  config.py, state.py, models.py
config/ ............... demo_config.yaml (all tunable settings)
datasets/ ............. curated multi-turn evaluation + traffic datasets
lambdas/ .............. trajectory_evaluator (custom code-based evaluator)
artifacts/ ............ generated outputs (gitignored)
reference/ ............ read-only AWS sample this adapts from (gitignored; re-fetchable)
tests/ ................ unit + property tests (stubbed AWS) + opt-in live smoke test
```

Companion docs: **[QUICKSTART.md](QUICKSTART.md)** (condensed fresh-account checklist) ·
**[ARCHITECTURE.md](ARCHITECTURE.md)** (deep dive) ·
**[MANUAL_DEPLOYMENT.md](MANUAL_DEPLOYMENT.md)** (production, API-only).
