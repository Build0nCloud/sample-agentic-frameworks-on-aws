# Demo Cheat Sheet — AgentCore Optimization Loop

How to run the demo. The **Streamlit web UI (`demo-ui`) is the primary demo vehicle** — it
runs the whole loop, shows every intermediate result, lets you chat with the live agent,
edit configs, and make the human approval. The **CLI (`demo`) is the fallback** for
scripted/headless runs (see the [CLI fallback](#cli-fallback) section at the end).

> ⚠️ Running the loop hits **real** Amazon Bedrock AgentCore and incurs AWS cost. Live
> actions in the UI are gated behind an **Enable live actions** toggle; the CLI prompts
> before provisioning (`--yes` skips it).

**Companion docs:** [ARCHITECTURE.md](ARCHITECTURE.md) (how it works),
[demo-talk-track.md](demo-talk-track.md) (start-to-finish narration + a 5-min and a
20-25 min pre-run script), [MANUAL_DEPLOYMENT.md](MANUAL_DEPLOYMENT.md) (production, API-only).

---

## 1. Initialize a terminal session (every time)

A fresh terminal has no venv active and no AWS credentials loaded. Do these three things.

**1a. Go to the project and activate the existing virtualenv:**
```bash
cd /Users/smithzgg/agentcore-optimization-demo
source .venv/bin/activate
```
> The `.venv` is already built (Python 3.13) with all deps + Streamlit + the `demo` /
> `demo-ui` commands. You do **not** need to recreate it. Verify:
> ```bash
> which demo-ui && python --version      # → .../.venv/bin/demo-ui  and  Python 3.13.x
> ```
> (If `.venv` is missing, see [First-time setup](#first-time-setup) below.)

**1b. Load AWS credentials** (short-lived — refresh when something fails with
`ExpiredTokenException`):
```bash
aws configure set aws_access_key_id     "$AWS_ACCESS_KEY_ID"
aws configure set aws_secret_access_key "$AWS_SECRET_ACCESS_KEY"
aws configure set aws_session_token     "$AWS_SESSION_TOKEN"
```

**1c. Confirm the account:**
```bash
aws sts get-caller-identity --query Account --output text     # expect 211395677819
```
> Region defaults to `us-east-1` from `config/demo_config.yaml`; set `AWS_REGION` only to
> target a different region.

> 💡 **What needs credentials:** viewing already-generated results in the UI needs none.
> **Live actions** — chatting with the agent, running stages, approving — need valid creds
> **and** the sidebar **Enable live actions** toggle on.

---

## 2. Launch the UI

```bash
demo-ui                       # opens http://localhost:8501
# optional: demo-ui --server.port 8502
```

In the **sidebar**:
- **🔄 Refresh artifacts** — reload from disk (use it whenever a stage finishes and the
  UI looks stale).
- **Enable live actions** — required for chat, running stages, and approvals. Leave off
  for a pure view-only walkthrough.
- Shows the active display timezone (times are shown local + UTC; set `ui.display_timezone`
  in config to change).

---

## 3. Recommended demo prep (avoid long waits on stage)

A full live run is ~25-40 min (the A/B online eval alone takes ~10-15 min to reach
significance). For a smooth demo, **pre-run the loop once** so all results are populated,
then present against them and do the *approval* live.

- **Pre-run offstage:** enable live actions → **🚀 Run demo** tab → **▶ Run all stages**
  (or `demo --yes run-all` in a terminal). Let it finish.
- **Leave one approval pending:** don't approve during the pre-run, or on the **✅ Approvals**
  tab click **↻ Reopen pending approval** to open a fresh `appr-…` for the live moment.
- Before presenting, click **🔄 Refresh artifacts**.

The full pre-run + presentation scripts (5-min and 20-25-min) are in
[demo-talk-track.md](demo-talk-track.md).

---

## 4. The UI, tab by tab (the demo)

Tabs are ordered to follow the loop. For each: **what to show** and **what to watch**.

### 💬 Chat — prove the agent is real
Enable live actions, then send a couple of turns in one conversation:
> - "I'm patient PT-1001. What medications am I on?" → looks up the record, lists meds.
> - "Refill my Lisinopril." → completes the refill (returns an RX id).
> - "My BP is 150/95, should I double my dose?" → **declines clinical advice.**
>
> Same conversation = session memory across turns. Click **🛠️ Show tools called** under a
> reply to reconstruct its tool trajectory from traces. ⏳ Traces lag ~1-2 min, so if it
> says "none detected," wait and hit **Retry** (or use the next turn's button).

### 🚀 Run demo — run stages from the browser
> Pick steps (default all) and **▶ Run all stages** / **▶ Run selected**. `build-bundles`
> and `offline-check` are grouped as one step. Progress streams live (per-step status +
> log); it auto-refreshes and **stops at `promotion-gate`**, which opens a pending approval.
> One run at a time.

### 📡 Online — production-traffic evaluation
> Sessions driven, sampling %, the online-eval config, aggregate scores, and per-session
> tool trajectories. ⏳ Online scores aggregate asynchronously (~10-15 min); if empty, that's
> the lag, not a failure.

### 💡 Recommendation — the AI-proposed improvement
> The recommended system prompt shown **before → after** vs the baseline (control) prompt.
> Talking point: generated from real production traces, not hand-written.

### 🧾 Bundles — view & edit the configs
> View/edit **control** and **treatment** configs (model id + full system prompt) drafted by
> `generate-config`. **💾 Save** writes the draft (no AWS). **▶ Build & run A/B** rebuilds the
> bundles from your edits and runs `build-bundles → offline-check → ab-test` (live).
> Great "let me test my own values" moment.

### 🧪 Candidate — regression check before going live
> The offline-check comparison: **control → candidate** scores with deltas, so you confirm
> no regressions before spending live A/B traffic.

### 📊 A/B Results — the real comparison
> Per-evaluator control-vs-treatment table + chart, the winner, and significance.
> **🔄 Refresh A/B result** re-polls the live test and re-persists (use if the stage finished
> before the eval aggregated). Honest note: a big GoalSuccessRate jump may not be significant
> on its own at ~10 sessions/variant — significance comes from Helpfulness / TrajectoryQuality.

### 📋 Reports — offline evaluation report, in the browser
> Pick a report (`baseline_pre`, `baseline_control`, `baseline_treatment`): aggregate scores +
> per-case breakdown (tools called, each evaluator's pass/score/explanation, agent response).
> No email needed.

### ✅ Approvals — the human-in-the-loop payoff (do this live)
> Shows pending + decided requests with the metrics at request time. **This is the moment:**
> enable live actions, expand the pending `appr-…`, type its id to confirm, click
> **Approve & promote**. That stops the A/B test, promotes the winner to 100%, and audits it.
> If none is pending, click **↻ Reopen pending approval** first.

### 📜 Audit trail — the record
> Every recorded decision (decider, decision, metrics at decision, outcome), newest first.

---

## 5. Edit the configs and A/B test your own values

1. Run through **generate-config** (Run demo tab, or `demo generate-config`) → drafts land
   at `artifacts/config_drafts/{control,treatment}.yaml`.
2. On **🧾 Bundles**, edit the model id and/or system prompt for control and/or treatment,
   and **Save**.
3. Enable live actions → **▶ Build & run A/B** → watch on the Run demo tab; results on
   **A/B Results** (use **🔄 Refresh A/B result** as it aggregates).

`run-all` does **not** pause for edits — it uses the generated configs straight through. Use
the generate-config → edit → Build & run A/B path to test your own values.

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| UI shows stale data after a stage finishes | Click **🔄 Refresh artifacts** (sidebar) or reload the page. |
| A/B Results / Approvals shows "no significant winner" | Online eval aggregates ~10-15 min; the poll log shows "no winner" for the first ~13 polls, then flips. Click **🔄 Refresh A/B result**. |
| "Show tools called" says "none detected" | Traces lag ~1-2 min. Wait and **Retry**, or use the next turn's button. |
| Live action does nothing / errors | Toggle **Enable live actions** on; refresh AWS creds (`ExpiredTokenException`) and confirm account `211395677819`. |
| A `NameError` / stale behavior after a code change | Restart `demo-ui` (it loads the module once at start; a browser refresh isn't always enough). |
| `ExpiredTokenException` mid-run | Refresh creds and re-run the remaining stages — state persists to `artifacts/`. The A/B test keeps running server-side; just re-poll. |
| `ConflictException` (name exists) | A prior partial run created it; stages append a unique per-run suffix, so re-running makes fresh resources. |
| A/B never significant | Ensure the weak `baseline_system_prompt` is set so control underperforms; give online eval the full ~15 min and refresh. |
| Report not emailed | Email is optional — view it on the **📋 Reports** tab. To send, verify an SES identity or set `email.delivery: smtp`. |
| Python 3.14 install errors | Use Python 3.13 (3.14 lacks wheels for the AWS/agent deps). |

---

## 7. Cleanup (stop ongoing costs when done)

The loop creates real resources (runtime, gateway/targets, online-eval configs, A/B tests,
bundles, the evaluator Lambda, IAM roles, an S3 bucket). Tear down with the vendored sample's
cleanup script (adjust `--name` to your `agent.runtime_name`):

```bash
python reference/amazon-bedrock-agentcore-samples/01-features/06-observe-evaluate-optimize-your-agent/03-optimize/cleanup.py --name PatientSupport
```
> Not covered by that script (delete manually): the trajectory evaluator Lambda
> `patientsupport-trajectory-eval`, the registered code evaluator, and the
> `AgentCoreLambdaEvaluatorRole`. See [MANUAL_DEPLOYMENT.md](MANUAL_DEPLOYMENT.md) §Cleanup
> for the full per-resource commands.

---

<a id="cli-fallback"></a>
## CLI fallback (scripted / headless)

Everything the UI does is also available on the `demo` CLI. Useful for scripting, or when a
browser isn't available. Each stage persists to `artifacts/`, so you can run them one at a
time across separate invocations (robust with short-lived creds).

**See the stages:**
```bash
demo                          # lists the 9 stages in order
```

**Stage order & timing** (⏱️ approx; `[P]` creates billable resources):
```
deploy [P] ~2-3m → offline-baseline ~8-12m → online [P] ~4-6m → recommend ~3-5m
→ generate-config [seconds] → build-bundles [P] ~30s → offline-check ~5-8m
→ ab-test [P] ~6m drive + ~10-15m to reach significance → promotion-gate [seconds]
```

**Run it:**
```bash
demo --yes run-all            # full loop up to the approval gate (no pause)
demo --yes <stage>            # run one stage (e.g. demo --yes deploy)
demo generate-config          # draft editable configs (no AWS); edit the YAML, then:
demo --yes build-bundles      # build bundles from the (edited) drafts
demo promotion-status         # list pending approvals
demo approve <appr-id>        # human decision: promote the winner
demo reject  <appr-id>        # human decision: keep the baseline
```

**Quick agent check (multi-turn, uses tools):**
```bash
python - <<'PY'
import uuid
from agentcore_demo.config import load_config
from agentcore_demo.agentcore_client import AgentCoreClient
from agentcore_demo.orchestrator import load_agent
from agentcore_demo.state import StateStore
cfg=load_config(); c=AgentCoreClient(cfg); a=load_agent(StateStore(cfg.artifacts_dir))
r=c.send_session(a, ["I'm patient PT-1001. What meds am I on?", "Refill the Lisinopril."], session_id=str(uuid.uuid4()))
print(r.final_response[:400])
PY
```

**Re-poll a running A/B test and re-persist the result** (the CLI equivalent of the UI's
**🔄 Refresh A/B result** — online eval aggregates asynchronously, so the `ab-test` stage can
finish before a winner appears):
```bash
python - <<'PY'
from agentcore_demo.config import load_config
from agentcore_demo.agentcore_client import AgentCoreClient, ABTestHandle
from agentcore_demo.optimization.abtest import determine_winner_any
from agentcore_demo.orchestrator import load_loop_state, save_loop_state
from agentcore_demo.state import StateStore
cfg=load_config(); c=AgentCoreClient(cfg); s=StateStore(cfg.artifacts_dir)
abid=load_loop_state(s)["ab_handle"]["ab_test_id"]
r=c.get_ab_test(ABTestHandle(abid,abid))
r.winner,r.significant=determine_winner_any(r.per_variant)
save_loop_state(s, ab_result=r, ab_handle={"ab_test_id":abid,"name":abid})
print("winner:", r.winner, "significant:", r.significant)
PY
```
> Expected once aggregated: `winner: T1 significant: True`.

**What "success" looks like per stage:**

| Stage | Success signal |
|---|---|
| deploy | `runtime PatientSupport-…` READY; `artifacts/agent_state.json` |
| offline-baseline | baseline scores printed; `artifacts/reports/baseline_pre/`; report in SES or `outbox/` |
| online | `online eval started; drove 30 sessions`; `online_result` in `loop_state.json` |
| recommend | `recommend: changed` |
| generate-config | `artifacts/config_drafts/{control,treatment}.yaml` written |
| build-bundles | `control=… treatment=…`; treatment prompt uses tools |
| offline-check | `baseline_control` + `baseline_treatment` reports; treatment holds/improves |
| ab-test | treatment significantly better on ≥1 metric (e.g. Helpfulness p≈0.0005); TrajectoryQuality 0.0→1.0 |
| promotion-gate | `pending approval appr-…` (PENDING_APPROVAL) |
| approve | approval → `PROMOTED`; audit-log entry; control bundle now holds the treatment config |

---

<a id="first-time-setup"></a>
## First-time setup (fresh clone / no `.venv`)

Only needed if `.venv` is missing. **Use Python 3.13** — 3.14 lacks wheels for the AWS/agent
deps, and `python3` on this machine is 3.14.
```bash
cd /Users/smithzgg/agentcore-optimization-demo
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,ui]"     # dev = tests; ui = Streamlit front end
pip install pip                # ensure pip is in the venv (deploy packages deps with it)
```

**Preflight (optional):**
```bash
# Claude Sonnet 5 reachable?
python - <<'PY'
import boto3
r=boto3.client("bedrock-runtime","us-east-1").converse(
  modelId="us.anthropic.claude-sonnet-5",
  messages=[{"role":"user","content":[{"text":"say ok"}]}],
  inferenceConfig={"maxTokens":10})
print(r["output"]["message"]["content"][0]["text"])      # → ok
PY
```

**No-cost sanity of the code (no AWS):**
```bash
pytest -q                      # → 190 passed, 1 deselected (the live test is skipped)
```
