# AgentCore Optimization Demo — Architecture

An end-to-end, runnable demonstration of the **Amazon Bedrock AgentCore optimization
loop** for a healthcare **patient-support agent**. It takes a deployed agent from a
measured quality baseline, through a data-driven improvement, to a safe rollout gated by
human approval:

```
deploy → offline baseline → online evaluation → recommendation → generate config
→ build bundles → offline check → A/B test → promotion (human approval) → new baseline
```

The demo always runs against **real** Amazon Bedrock AgentCore services (Runtime,
Gateway, Evaluations, Optimization). There is no local/simulated mode; the automated
tests exercise the pure logic against stubbed AWS clients.

There are two ways to drive the loop, over the same code and artifacts:

- a **Streamlit web UI** (`demo-ui`) — the primary demo surface: run stages, chat with
  the live agent, view every intermediate result, edit bundle configs, and make the human
  approval; and
- a **CLI** (`demo`) — the scriptable fallback: one command per stage, plus
  `approve`/`reject`.

---

## 1. High-level flow

```
     ui/ (Streamlit web app)                     demo.py (CLI entry point)
     app · data · actions · runner · launch      subcommands: <stage> | run-all
     10 tabs; runs stages in a bg thread;         | promotion-status | approve/reject
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

1. **One AWS seam.** Every stage talks to `AgentCoreClient`; nothing else imports
   `boto3`. This keeps stages focused on *what* to do and puts credentials, retries,
   timing waits, and response parsing in one testable place.
2. **State on disk, not just memory.** Because each `demo <stage>` is a separate OS
   process, stages persist their outputs to `artifacts/` (`agent_state.json`,
   `loop_state.json`, approvals, audit). Later stages/commands reload from there, so the
   loop can be run stage-by-stage across separate invocations.

---

## 2. Repository layout (every artifact)

```
agentcore-optimization-demo/
├── demo.py entry point ....... src/agentcore_demo/demo.py         # CLI (installed as `demo`)
├── config/
│   └── demo_config.yaml                                           # all tunable settings
├── datasets/
│   ├── offline_multiturn.jsonl                                    # curated eval cases + ground truth
│   └── traffic_sessions.jsonl                                     # scripted multi-turn traffic
├── lambdas/
│   └── trajectory_evaluator/lambda_function.py                    # custom code-based evaluator (Phase 2)
├── src/agentcore_demo/
│   ├── demo.py                                                    # CLI + cost/confirmation gate
│   ├── orchestrator.py                                            # Stage engine + build_stages() wiring
│   ├── agentcore_client.py                                        # single service layer over AgentCore
│   ├── deploy.py                                                  # package + deploy agent to Runtime
│   ├── lambda_deploy.py                                           # package + deploy the evaluator Lambda
│   ├── config.py                                                  # load/validate demo_config.yaml
│   ├── state.py                                                   # artifacts/ + run manifest persistence
│   ├── models.py                                                  # shared dataclasses
│   ├── agent/patient_support_agent.py                             # the deployed agent + tool suite
│   ├── evaluation/
│   │   ├── evaluators.py                                          # built-in refs + custom evaluators
│   │   ├── offline.py                                             # batch eval pipeline + report builder
│   │   └── online.py                                              # online-eval lifecycle + traffic driver
│   ├── optimization/
│   │   ├── recommendations.py                                     # prompt/tool-description recommendations
│   │   ├── bundles.py                                             # configuration bundle manager
│   │   ├── abtest.py                                              # A/B lifecycle + statistics + winner
│   │   └── promotion.py                                           # promotion gate (approval + audit)
│   ├── reporting/emailer.py                                       # report delivery (SES/SMTP + outbox)
│   └── ui/                                                        # Streamlit web app (optional `ui` extra)
│       ├── app.py                                                 # tabs + rendering (the demo UI)
│       ├── data.py                                                # read-only view over artifacts/
│       ├── actions.py                                             # live actions (chat, approve/reject, refresh)
│       ├── runner.py                                              # runs stages in a background thread
│       └── launch.py                                              # `demo-ui` entry point (streamlit run)
├── artifacts/                                                     # generated at runtime (see §6)
├── reference/                                                     # read-only AWS sample this builds on
└── tests/                                                         # unit + property tests (stubbed AWS)
```

---

## 3. Components — what each thing does

### CLI & orchestration

| Artifact | Responsibility |
|---|---|
| **`demo.py`** | The single entry point. With no args it lists stages; subcommands run one stage, `run-all`, `promotion-status`, or `approve`/`reject <id>`. Before any stage that creates billable resources it prints a cost notice and requires confirmation (`y/N` or `--yes`). Builds the orchestrator + a `StageContext` via `_default_builder`. |
| **`orchestrator.py`** | The stage engine. `Stage`/`StageOutcome`/`StageContext` types; `Orchestrator.run()` executes stages in canonical order, records each in a per-run `Manifest`, emits ✓/✗ progress, and **halts on failure** (no dependent stages run). `build_stages()` is the concrete wiring of the 9 stages to the modules. `STAGE_SPECS` is the ordered metadata (name/description/`provisions`). Helpers persist/reload cross-stage state (`loop_state.json`), the agent handle (`agent_state.json`), the editable config drafts (`config_drafts/*.yaml`), and the promotion decision. `format_before_after()` renders the quality comparison. |

### The web UI (optional `ui` extra)

| Artifact | Responsibility |
|---|---|
| **`ui/app.py`** | The Streamlit app — the primary demo surface. Ten tabs (loop order): **Run demo, Chat, Online, Recommendation, Bundles, Candidate, A/B Results, Reports, Approvals, Audit trail**. Presentation only; reads through `data.py`, mutates only through `actions.py`/`runner.py`. Timestamps show in a configurable timezone + UTC. |
| **`ui/data.py`** | Read-only facade over `artifacts/` for the UI: loads loop state, A/B result, reports, approvals, audit, and the editable config drafts; decodes the agent's SSE responses; resolves the display timezone. No AWS, no mutation. |
| **`ui/actions.py`** | The only UI code that mutates state or calls AWS: `chat_once` (invoke the live agent), `fetch_session_tools` (reconstruct a chat turn's tool trajectory from traces), `approve`/`reject` (drive `PromotionGate`), `reopen_approval` (open a fresh pending approval from the last significant result), and `refresh_ab_result` (re-poll + re-persist the A/B result). |
| **`ui/runner.py`** | Runs orchestrator stages in a **background thread** (Streamlit can't block for a ~25-40 min run), streaming each `emit()` line and per-stage status into a thread-safe snapshot the UI polls. Reuses the same wiring as the CLI; one run at a time. |
| **`ui/launch.py`** | The `demo-ui` console entry point — shells out to `streamlit run app.py`. |

### The AWS seam

| Artifact | Responsibility |
|---|---|
| **`agentcore_client.py`** | The only module that touches AWS. Wraps the AgentCore **data-plane** (`bedrock-agentcore`: invoke, batch-evaluation, recommendations, A/B tests) and **control-plane** (`bedrock-agentcore-control`: configuration bundles, online-eval configs, gateways/targets, runtimes, evaluators), plus `logs`/`iam`/`s3`/`lambda`. Owns credential resolution (standard chain), retry/backoff on throttling, configurable timing waits, SigV4-signed gateway invokes, CloudWatch-spans → tool-trajectory reconstruction, and parsing of batch/A-B responses into typed models. All clients are injectable so tests use fakes. |
| **`deploy.py`** | Packages the agent (`patient_support_agent.py` as `main.py`) with ARM64 deps, uploads to S3, creates the AgentCore Runtime + IAM role, polls to READY, and returns an `AgentHandle`. Adapted from the AWS sample; also builds a `v2` variant for target-based A/B tests. |
| **`lambda_deploy.py`** | Packages the custom evaluator Lambda (source + `bedrock-agentcore` SDK + deps built for the Lambda platform), creates/updates the function, and grants `bedrock-agentcore` permission to invoke it. |

### The agent (deployed to Runtime)

| Artifact | Responsibility |
|---|---|
| **`agent/patient_support_agent.py`** | A Strands agent on `BedrockAgentCoreApp`, deployed to AgentCore Runtime, running on **Claude Sonnet 5** (`us.anthropic.claude-sonnet-5`). Healthcare patient-support assistant using **synthetic, non-PHI** data; scoped to administrative/informational tasks and instructed to decline clinical advice. Keeps a per-session agent cache so **multi-turn** history carries across turns. A **config-bundle hook** reads `BedrockAgentCoreContext.get_config_bundle()` on each call and overrides the system prompt + tool descriptions — this is what lets A/B variants change behavior without redeploying code. |
| — tool suite (8 tools) | `get_patient_profile`, `find_provider`, `check_coverage`, `schedule_appointment`, `get_medications`, `request_prescription_refill`, `get_lab_results`, `lookup_health_policy`. Deterministic mock data; return **structured errors** (not exceptions) for bad input. Two designed trajectories: **refill** (`get_patient_profile → get_medications → request_prescription_refill`) and **scheduling** (`find_provider → check_coverage → schedule_appointment`). |

### Evaluation

| Artifact | Responsibility |
|---|---|
| **`evaluation/evaluators.py`** | References the **built-in** AgentCore evaluators (`GoalSuccessRate`, `Helpfulness`, `Correctness`, scored server-side by an LLM judge) and defines **custom** local evaluators for offline scoring: `ToolCall` (required tools were called), `Trajectory` (ordered tool calls match the expected trajectory), and `ClinicalSafety` (flags clinical advice without a referral). Matching logic is pure/testable. |
| **`evaluation/offline.py`** | The batch-evaluation pipeline. Loads the multi-turn dataset, replays each case in one session (send-all → wait for CloudWatch ingestion → reconstruct tool calls → score), aggregates per-evaluator means, persists a named **baseline**, and builds a full **report** (JSON + Markdown + HTML). **Per-case isolation:** a failing case is recorded and the run continues; the reported case count always equals the total. |
| **`evaluation/online.py`** | Manages the online-evaluation config lifecycle (start/stop/teardown), drives **real traffic** by invoking the deployed agent with scripted multi-turn sessions, and provides views over per-session scores (aggregate, quality trend over time, low-scoring sessions). |

### Optimization

| Artifact | Responsibility |
|---|---|
| **`optimization/recommendations.py`** | Calls the AgentCore Recommendations API to produce an improved **system prompt** (or tool descriptions) targeting an evaluator, from production traces. Surfaces the before/after change and can apply it to a bundle config. |
| **`optimization/bundles.py`** | Manages **configuration bundles** — versioned, immutable snapshots of `{system_prompt, model_id, tool_descriptions}`. Creates/lists versions with parent lineage, tracks the current baseline version, and yields a `BundleRef` for serving a version as an A/B variant. Also runs an offline check of a candidate bundle before online testing. |
| **`optimization/abtest.py`** | The A/B lifecycle (config-bundle or target-based variants), the **traffic driver** (sticky session→variant assignment), and the **statistics**: a dependency-free Welch's t-test (incomplete-beta p-values, t-quantile CIs). `determine_winner` (significant + improved on a primary metric) and `determine_winner_any` (significantly better on ≥1 metric, no significant regression) decide the winner. |
| **`optimization/promotion.py`** | **The promotion gate** — the capability the AWS sample lacks. On a significant winner it opens a `PENDING_APPROVAL` request and **blocks rollout**. A human `approve` stops the A/B test, promotes the winning config into the baseline bundle, and writes an audit entry; `reject` leaves the baseline unchanged and audits. Enforces the state machine (`PENDING_APPROVAL → APPROVED → PROMOTING → PROMOTED` or `→ REJECTED`). |

### Custom evaluator (Phase 2)

| Artifact | Responsibility |
|---|---|
| **`lambdas/trajectory_evaluator/lambda_function.py`** | A SESSION-level **code-based** evaluator that AgentCore online evaluation invokes. It extracts the ordered tool calls from the session's OTel spans and scores **trajectory quality without ground truth** — rewarding tool use and correct orderings (refill after `get_medications`; schedule after `find_provider`+`check_coverage`). This lets the *online A/B* score the tool-trajectory dimension (not just the built-in LLM judges). Pure scoring logic is unit-tested; the handler uses the `@custom_code_based_evaluator()` SDK contract. |

### Supporting

| Artifact | Responsibility |
|---|---|
| **`config.py`** | Loads/validates `demo_config.yaml` into typed `Config`/`EmailConfig`. Region/names/model/evaluators/dataset paths/sampling/weights, the optional **weak baseline prompt** (demo scenario), the **email** settings, and the **`enable_trajectory_evaluator`** flag. Credentials come only from the AWS chain — never config. |
| **`state.py`** | Owns the `artifacts/` tree with atomic writes: named JSON docs, the append-only audit log, the email outbox, and the per-run `Manifest` (stage statuses + produced artifacts). `to_jsonable` serializes dataclasses/enums/paths. |
| **`models.py`** | Shared dataclasses: `ConversationCase`/`GroundTruth`, `SessionResult` (ordered `tool_calls`), `Baseline`, `BundleConfig`/`BundleVersion`, `EvalMetric`/`ABTestResult`, `ApprovalRequest`/`AuditEntry` (+ `ApprovalStatus`/`Decision` enums), `EvaluationReport`. |
| **`reporting/emailer.py`** | Delivers the offline report via SES or SMTP, or writes it to a local **outbox** when email isn't configured. **Best-effort:** a send failure never aborts the run and the report is always kept locally. |

---

## 4. The 9 stages — inputs, actions, outputs

Legend: **[P]** creates billable AWS resources (guarded by the confirmation gate).

| # | Stage | Reads | Does | Writes / AWS effect |
|---|---|---|---|---|
| 1 | **deploy** **[P]** | agent code, config | Package agent → S3 → create Runtime + IAM role; poll READY | `AgentHandle` → `artifacts/agent_state.json`; Runtime, role, S3 object |
| 2 | **offline-baseline** | `offline_multiturn.jsonl`, deployed agent | Replay cases → reconstruct tool calls from traces → score (custom + built-in batch) → build report → email/outbox | `Baseline` + `report.{json,md,html}` (name `baseline_pre`); SES send or `artifacts/outbox/` |
| 3 | **online** **[P]** | `traffic_sessions.jsonl`, agent | Create online-eval config; drive multi-turn traffic; score the driven sessions | Online-eval config; `online_result` (scores + per-session trajectories) → `loop_state.json`; CloudWatch traces |
| 4 | **recommend** | baseline prompt, traces | Ask Recommendations API for an improved prompt targeting GoalSuccessRate | `recommendation` → `loop_state.json` |
| 5 | **generate-config** | recommendation, base config | Compute **control** (weak baseline) + **treatment** (recommended) configs and write **editable YAML drafts** — no AWS | `config_drafts/{control,treatment}.yaml` |
| 6 | **build-bundles** **[P]** | the drafts (or computed configs) | Create **control** + **treatment** configuration bundle versions from the (possibly edited) drafts | Bundle versions + refs; `control_config`/`treatment_config` → `loop_state.json` |
| 7 | **offline-check** | control + treatment bundles, dataset | Batch-evaluate **both** bundles on the same cases to catch regressions before going live | `baseline_control` + `baseline_treatment` reports (for before/after) |
| 8 | **ab-test** **[P]** | control/treatment refs | Create gateway + target + online-eval (incl. the custom trajectory evaluator when enabled); start A/B test; drive gateway traffic; poll for significance (~20×45s) | `ABTestResult` + handle → `loop_state.json`; gateway/target/online-eval/A-B test, evaluator Lambda |
| 9 | **promotion-gate** | `ab_result` | If a significant winner exists, open a `PENDING_APPROVAL` (no rollout) | `ApprovalRequest` → `artifacts/approvals/`; promotion decision persisted |
| — | **approve / reject** | approval id | Human decision: stop A/B, promote winner into baseline bundle + audit, or keep baseline + audit | Updated approval (`PROMOTED`/`REJECTED`), `artifacts/audit/audit-log.jsonl`, bundle update |

> **generate-config + build-bundles** replace the old single `bundle` stage. Splitting it
> lets you **edit** the control/treatment configs (model id + system prompt) between drafting
> and building — the Web UI's **Bundles** tab reads/writes these YAML drafts, and
> **Build & run A/B** rebuilds from the edits. `run-all` runs both straight through (no pause).

---

## 5. Data / control flow across the loop

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
                                                                        │
 (5) generate-config ── control(weak) & treatment(recommended) ──► editable YAML drafts
                                                    │  (optionally edited in the UI Bundles tab)
 (6) build-bundles ── drafts ──► Configuration Bundles
                                                                        │  control_ref / treatment_ref
 (7) offline-check ── control + treatment bundles ──► batch eval ──► baseline_control / baseline_treatment (before/after)

 (8) ab-test ── Gateway ─┬─ 50% ─► control bundle  ─┐
                         └─ 50% ─► treatment bundle ─┤──► Runtime ──► CloudWatch
                                                     │
                Online eval (built-ins + TrajectoryQuality Lambda) scores each session
                                                     ▼
                              ABTestResult (per-variant means, p-values, significance)
                                                     │
 (9) promotion-gate ── significant winner? ──► ApprovalRequest (PENDING_APPROVAL)  ⟵ rollout BLOCKED
                                                     │
   demo approve <id> ── stop A/B ─► promote treatment config into control bundle ─► new baseline
                                   └─► audit-log.jsonl
```

---

## 6. Runtime artifacts (`artifacts/`)

| Path | Written by | Contents |
|---|---|---|
| `agent_state.json` | deploy | The `AgentHandle` (runtime id/arn, log group, service name, role, region) |
| `loop_state.json` | online/recommend/build-bundles/ab-test | Cross-stage state: recommendation, `online_result`, control/treatment configs + bundle refs, A/B handle + result, trajectory-evaluator id |
| `config_drafts/{control,treatment}.yaml` | generate-config | Editable draft configs (model id + system prompt); read by build-bundles and the UI Bundles tab |
| `baselines/<name>.json` | offline eval | A named baseline (per-evaluator means, case count, config ref) — e.g. `baseline_pre`, `baseline_control`, `baseline_treatment` |
| `reports/<name>/report.{json,md,html}` | offline report builder | The full evaluation report (aggregate scores + per-case results) |
| `outbox/<name>.html` | emailer | Rendered report when email isn't sent (fallback) |
| `bundles/decision-<id>.json` | promotion-gate | The persisted promotion decision for a later `approve` |
| `approvals/<id>.json` | promotion gate | An `ApprovalRequest` (winner, metrics-at-detection, status, timestamps) |
| `audit/audit-log.jsonl` | approve/reject | Append-only audit entries (decider, decision, metrics, outcome) |
| `runs/<run-id>.json` | orchestrator | Per-run manifest (stage statuses + artifacts produced) |

---

## 7. Configuration (`config/demo_config.yaml`)

| Key | Meaning |
|---|---|
| `aws.region` | Region for all AgentCore calls (env `AWS_REGION` overrides) |
| `agent.runtime_name` / `gateway_name` | Resource name prefixes |
| `agent.model_id` | Foundation model — **`us.anthropic.claude-sonnet-5`** |
| `agent.baseline_system_prompt` | Optional **deliberately weak** control prompt (demo scenario) so the recommended treatment has real headroom to win |
| `evaluators` | Built-in evaluators applied by offline/online evaluation |
| `datasets.offline` / `datasets.traffic` | Dataset paths |
| `online_eval.sampling_percentage` | % of sessions scored online |
| `ab_testing.*_weight` | Control/treatment traffic split (50/50 config-bundle; 90/10 canary) |
| `enable_trajectory_evaluator` | Deploy + include the custom trajectory Lambda in the A/B online eval (Phase 2) |
| `email.{delivery,target,sender,...}` | Report delivery (`ses`/`smtp`/`outbox`); optional; secrets from env only |
| `ui.display_timezone` | Timezone for UI timestamps (shown alongside UTC): an IANA name or `local` |

Credentials are resolved via the standard AWS chain and are never stored in config.

---

## 8. How promotion decisions are made (and the Phase-2 nuance)

The gate promotes only on a **statistically significant winner**:

- **`determine_winner`** — treatment wins if it is significantly better (p < 0.05) *and*
  improved on a chosen **primary** evaluator.
- **`determine_winner_any`** — treatment wins if it is significantly better on **at least
  one** evaluator and **not significantly worse** on any (a decisive, no-regression win).
  Used when the headline improvement can't yield a p-value on its own.

Observed live (weak control vs recommended treatment):

| Evaluator | Control | Treatment | p-value | significant |
|---|---|---|---|---|
| **TrajectoryQuality** (custom Lambda) | 0.00 | 1.00 | none* | — |
| Builtin.GoalSuccessRate | 0.00 | 0.70 | 0.061 | no |
| Builtin.Helpfulness | 0.60 | 0.88 | 0.0005 | ✅ |
| Builtin.Correctness | 0.80 | 1.00 | 0.24 | no |

\* The custom trajectory evaluator **ran and measured** the improvement (0.0 → 1.0), but a
*perfect* separation has zero within-group variance, so AgentCore can't compute a t-test
p-value for it. The promotion therefore fired via `determine_winner_any`: the treatment
was significantly better on **Helpfulness** with no regressions, and the trajectory metric
corroborated the win. To make the trajectory metric itself the significance driver would
require some within-group variance (more varied traffic) or a proportion/Fisher test in
our own stats layer.

---

## 9. Testing model

- **Unit + property tests** run against stubbed/fake AWS clients — no AWS, no cost —
  covering the pure logic: promotion state machine, A/B statistics, evaluators, offline
  per-case isolation, bundle lineage, report delivery, and the trajectory scorer.
- **Property suite** enforces the design's Correctness Properties (P1–P10) at ≥100
  iterations each.
- The only test that touches real AWS is an opt-in `live` smoke test (excluded by
  default). All AWS-touching stage bodies are validated by live runs, not unit tests.

---

## 10. External dependencies

- **Amazon Bedrock AgentCore** (preview): Runtime, Gateway, Evaluations (batch/online),
  Optimization (recommendations, configuration bundles, A/B testing), custom code-based
  evaluators.
- **Amazon Bedrock** model access: Claude Sonnet 5.
- **AWS**: IAM, S3, CloudWatch Logs (OTel spans), Lambda, optionally SES.
- **Python**: `boto3`, `strands-agents`, `bedrock-agentcore`, `PyYAML`, `requests`;
  `pytest` + `hypothesis` for tests (`dev` extra); `streamlit` for the web UI (`ui` extra).
- Built on the read-only AWS sample under `reference/`
  (`awslabs/amazon-bedrock-agentcore-samples` → `06-observe-evaluate-optimize-your-agent`).
```
