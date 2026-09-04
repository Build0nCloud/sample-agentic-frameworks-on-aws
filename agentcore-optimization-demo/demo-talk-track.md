# Demo Talk Track — AgentCore Optimization Loop

A start-to-finish narration for presenting the demo, with cues for when to speak, when
to click in the Streamlit UI, and when to flip to the AWS Console to show the real
Amazon Bedrock AgentCore artifacts behind each step.

**Format of this doc**
- 🎤 **Say** — what to tell the audience.
- 🖱️ **Do** — what to click in the demo UI.
- ☁️ **Console** — flip to the AWS Console to show the real resource.
- 💡 **Note** — presenter tip / what to watch for.

**Account/region for this run:** `211395677819` · `us-east-1`
(Resource ids below are from a real run; yours will have a different random suffix.)

**Which version to use:**
- **[5-minute short version](#five-minute-short-version)** — the elevator pitch + the one
  moment that lands: the human-approved promotion.
- **[20-25 minute detailed version (pre-run)](#twenty-five-minute-detailed-version-pre-run)**
  — the full loop with UI *and* AWS Console artifacts, using an already-completed run so
  there are **no long waits**. Only the approval is done live. **This is the recommended
  default.**
- **The full narration (Sections 0-10 below)** — the complete reference, including notes
  for running stages live. Both versions above pull their wording from these sections.

---

<a id="five-minute-short-version"></a>
## ⏱️ 5-Minute Short Version

Assumes a completed run is already loaded (artifacts populated) and the UI + AWS Console
are open. Goal: land the story and the one differentiator — human-gated promotion — fast.

**Prereqs (offstage):** UI running with a completed run; **one pending approval available**
(if all are decided, click **↻ Reopen pending approval** on the Approvals tab beforehand).

1. **Framing (45s).**
   🎤 "Building an agent is easy; *keeping it good in production* is hard. This is the Amazon
   Bedrock AgentCore optimization loop for a healthcare support agent — evaluate, observe,
   get an AI-suggested improvement, A/B test it on live traffic, and promote the winner
   **only after a human approves.** That approval gate is the part most teams are missing."

2. **The improvement (60s).**
   🖱️ **💡 Recommendation** tab → show the **before → after** prompt. 🎤 "The service
   analyzed real production traces and proposed a stronger, tool-using prompt — this wasn't
   hand-written."

3. **The proof (90s).**
   🖱️ **📊 A/B Results** tab → 🎤 "We A/B tested control vs treatment on live traffic.
   Treatment T1 is the significant winner — Helpfulness 0.56→1.0, a custom trajectory
   metric 0.0→0.8. Both built-in and *custom* evaluators."
   ☁️ Quick Console flip: **AgentCore → Configuration bundles** — "each variant is an
   immutable, versioned config bundle" — and **Evaluations → Evaluators** — "including our
   own Lambda-based trajectory evaluator."

4. **The payoff — do this live (90s).**
   🖱️ **✅ Approvals** tab → 🎤 "A significant winner does *not* auto-promote. It opens a
   pending approval." Toggle **Enable live actions**, type the `appr-…` id, click
   **Approve & promote**. 🎤 "That one click stopped the test, promoted the winner to 100%
   of traffic, and wrote an audit record."
   🖱️ **📜 Audit trail** tab → show the new entry.

5. **Close (15s).**
   🎤 "Measure → improve → A/B test → human-approved promotion, all on real AgentCore, all
   audited. That's how you keep an agent good in production — safely."

---

<a id="twenty-five-minute-detailed-version-pre-run"></a>
## 🎯 20-25 Minute Detailed Version (pre-run, no waiting)

The recommended default for a real audience. It shows the **entire loop with depth** — every
UI tab and the real AWS Console artifact behind it — but runs against an **already-completed
run**, so you never wait on a deploy, an eval, or the ~15-minute A/B aggregation. The only
thing done live is the human approval (instant and high-impact).

### Offstage prep (do this before the audience is watching)
- [ ] Run the **full loop once** ahead of time (or reuse a recent run) so all artifacts are
      populated: baselines/reports, online result, recommendation, bundles, A/B result with a
      significant winner.
- [ ] Leave **one approval pending** — either don't approve during the pre-run, or on the
      Approvals tab click **↻ Reopen pending approval** so there's a fresh `appr-…` to approve
      live.
- [ ] UI open at `http://localhost:8501`; hit **🔄 Refresh artifacts** so nothing is stale.
- [ ] AWS Console open in a second tab, `us-east-1`, on **Bedrock AgentCore**.
- [ ] Optional: have `artifacts/loop_state.json` handy to show "this is the state the loop
      persists between steps."

### Running order and timing (target ~22 min)

| # | Segment | UI tab | Console flip | ~min |
|---|---|---|---|---|
| 1 | Framing + scenario | header / tab strip | — | 1.5 |
| 1b | Prove it's real — live chat | 💬 Chat | — | 1.5 |
| 2 | Deployed agent (Runtime) | — | Runtime, IAM role, CloudWatch traces | 2 |
| 3 | Offline baseline | 📋 Reports (`baseline_pre`) | Batch evaluations | 3 |
| 4 | Online evaluation | 📡 Online | Online eval config + sampling | 2.5 |
| 5 | Recommendation | 💡 Recommendation | traces (input) | 2.5 |
| 6 | Bundles (+ optional edit) | 🧾 Bundles | Configuration bundles + versions | 3 |
| 7 | Candidate regression check | 🧪 Candidate | — | 1.5 |
| 8 | A/B test | 📊 A/B Results | Gateway + target, custom evaluator Lambda | 3 |
| 9 | Promotion gate + **live approval** | ✅ Approvals → 📜 Audit | stopped A/B test / promoted bundle | 2 |
| 10 | Close | — | — | 0.5 |

### How to run it
Present **Sections 1-10 below verbatim**, but everywhere a section says "run this stage,"
**skip the run** and instead **read the already-populated result** in the UI tab and show its
Console artifact. The narration in each section already assumes the result exists.

- **Do NOT click** Run demo / Build & run A/B / any stage button during this version — those
  trigger the long live runs you're trying to avoid.
- **The two live interactions are the Chat tab (Section 1b) and Section 9's Approve &
  promote.** Both are fast; everything else is read from the pre-run artifacts.
- Keep the AWS Console flips brief (10-20s each) — the point is "this is real infrastructure,"
  not a Console tour. Pre-open each Console page in tabs so switching is instant.

### Talking-point emphasis (what makes this land)
- **Section 5 (Recommendation):** "generated from real traces, not hand-written."
- **Section 6 (Bundles):** show that configs are **editable** — "teams can A/B test their own
  model or prompt." (Describe the Build & run A/B button; don't click it live.)
- **Section 8 (A/B):** the **custom trajectory evaluator** (your own Lambda metric) + the
  honest note that GoalSuccessRate's big jump isn't significant on its own at small n.
- **Section 9 (Approval):** the differentiator — **no auto-promotion; human-gated; audited.**

### If asked "can we run it live?"
🎤 "Absolutely — every step you're seeing was produced by a real run. I've pre-run it because
the A/B online evaluation aggregates asynchronously and takes ~10-15 minutes; in a working
session you'd kick off `run-all`, go get coffee, and come back to approve. Happy to start a
live run now and check back at the end."
💡 If you want to prove it live, kick off `generate-config` (seconds, free) or a full run at
the *start* and return to it during the close.

---

## 0. Before you start (offstage setup)

- [ ] `source .venv/bin/activate` and refresh AWS credentials (`aws configure set …`).
- [ ] `aws sts get-caller-identity` → confirm account `211395677819`.
- [ ] Launch the UI: `demo-ui` → open `http://localhost:8501`.
- [ ] Have the AWS Console open in a second tab, signed in to `us-east-1`, on the
      **Amazon Bedrock AgentCore** pages.
- [ ] Ideally pre-run the loop once so results are populated — then present against the
      existing artifacts and do the *approval* live. A full live run is ~25-40 min.

💡 If you want a live "run it now" moment without the full wait, run just the fast,
no-cost stages live (generate-config) and show the rest from the pre-run artifacts.

---

## 1. Framing (60-90 seconds, no screen action)

🎤 **Say:**
> "Teams can build an agent, but the hard part is *keeping it good* in production —
> measuring quality, improving it, and safely rolling out changes. This demo shows the
> full Amazon Bedrock AgentCore **optimization loop** for a healthcare patient-support
> agent: evaluate it offline, watch it in production, get an AI-generated improvement,
> A/B test the change on live traffic, and promote the winner — but only after a
> **human approves**. That human-in-the-loop promotion gate is the piece most teams are
> missing, and it's the heart of this demo."

🎤 **The scenario:**
> "Our agent helps patients with refills, appointments, coverage, and lab results. It has
> eight tools and a strict no-clinical-advice safety rule. To make the improvement
> obvious, we start the *control* with a deliberately weak prompt that forbids tool use,
> and let the loop discover a stronger, tool-using prompt as the *treatment*."

🖱️ **Do:** Show the top of the UI — Region, Runtime, Model header. Point out the tab
strip left-to-right: **Run demo → Chat → Online → Recommendation → Bundles → Candidate →
A/B Results → Reports → Approvals → Audit trail** — "the tabs follow the loop."

---

## 1b. Prove it's real — chat with the live agent (optional, high-impact opener)

🎤 **Say:**
> "Before we talk about *improving* the agent, let me prove it's real. This isn't a mockup
> or a recording — it's a live agent running on AgentCore. Let's ask it something."

🖱️ **Do:** Open the **💬 Chat** tab (enable **live actions** first). Send a couple of turns
in one conversation to show tool use and memory:
> 1. "I'm patient PT-1001. What medications am I on?" → it looks up the record and lists meds.
> 2. "Refill my Lisinopril." → it completes the refill (returns an RX id, refills remaining).
> 3. (Optional safety beat) "My blood pressure is 150/95, should I double my dose?" → it
>    **declines clinical advice** and points to a clinician.

🖱️ **Do (optional depth):** Under the refill reply, click **🛠️ Show tools called** — it
reconstructs the tool trajectory from CloudWatch traces (e.g. `get_medications →
request_prescription_refill`).

🎤 **Say:**
> "Same conversation, so it remembered I'm PT-1001 across turns. It used tools to actually do
> the work — I can even show the exact tool trajectory pulled from the traces — and it
> respected the safety rule: no clinical advice. That's the real agent; everything else in
> this demo is about measuring and improving *this*."

💡 The **Show tools called** button reads CloudWatch traces, which lag ~30-90s after a turn —
if it shows "none detected," wait a few seconds and try the next turn's button, or skip it.
Keep the chat beat to ~60-90s and 2-3 turns. If creds are expired or the runtime is cold, skip live
and describe it — don't fight a timeout on stage.

---

## 2. The deployed agent (Runtime)

🎤 **Say:**
> "That agent is deployed as an AgentCore **Runtime** — a managed, serverless place to run
> it, with built-in tracing to CloudWatch."

☁️ **Console — AgentCore Runtime:**
> Navigate to **Bedrock AgentCore → Agent Runtimes**. Open **`PatientSupport-mzD1WU2GZU`**.
> Point out:
> - Status **READY**, the container/artifact, and the **execution role**
>   (`PatientSupportRole`).
> - "This role is what lets the agent call Bedrock models, write traces, and — later —
>   invoke our custom evaluator Lambda."

☁️ **Console — CloudWatch:**
> Show the log group **`/aws/bedrock-agentcore/runtimes/PatientSupport-mzD1WU2GZU-DEFAULT`**
> and mention the `aws/spans` traces. "Every invocation emits OpenTelemetry spans — that's
> the raw material both evaluation and the recommendation engine feed on."

💡 If deploy isn't already done, this is the one stage worth *not* doing live (it builds
an ARM64 package and polls to READY — a few minutes).

---

## 3. Offline baseline — measure before you change anything

🎤 **Say:**
> "Before optimizing, we measure. The **offline-baseline** step replays a curated set of
> ~12 multi-turn cases against the agent and scores them — with built-in evaluators
> (GoalSuccessRate, Helpfulness, Correctness) and our own custom evaluators (tool-call
> correctness, tool trajectory, and clinical safety)."

🖱️ **Do:** Open the **📋 Reports** tab → select `baseline_pre`. Walk the aggregate scores,
then expand one case (e.g. `refill-happy`) to show the per-evaluator pass/score/explanation
and the agent's actual response.

🎤 **Say:**
> "Notice this is a real, compiled report. In production you'd email it; here it's shown
> right in the UI. Each case shows which tools were called and why each evaluator passed
> or failed — that per-case detail is what makes the score trustworthy."

☁️ **Console — Bedrock AgentCore Evaluations:**
> Show **Batch evaluations** — point out the batch evaluation job the offline step created,
> and that the built-in evaluators are managed AgentCore evaluators (not something we
> hand-rolled). "The custom evaluators run locally; the built-ins are a managed service."

---

## 4. Online evaluation — watch it in production

🎤 **Say:**
> "Offline is a lab test. **Online evaluation** scores real production traffic. This step
> stands up an online-evaluation config and drives scripted multi-turn traffic so we don't
> have to wait for organic users."

🖱️ **Do:** Open the **📡 Online** tab. Show sessions driven (30), sampling (100%), the
online-eval config id, the aggregate scores, and the per-session tool trajectories table.

☁️ **Console — AgentCore Evaluations → Online evaluations:**
> Show the online-evaluation config (`PatientSupportOnlineEval…`). Point out the
> **sampling percentage** and the evaluator list. "This is the knob for how much of live
> traffic gets continuously scored."

💡 **Watch for:** online eval aggregates asynchronously (~10-15 min). If scores look
empty, that's the lag, not a failure — say so and move on; the pre-run artifacts have it.

---

## 5. Recommendation — the AI proposes an improvement

🎤 **Say:**
> "Here's where it gets interesting. The **recommend** step calls the AgentCore
> Recommendations API. It analyzes the production traces we just generated and proposes a
> **better system prompt** targeting the weak metric — GoalSuccessRate."

🖱️ **Do:** Open the **💡 Recommendation** tab. Show the **before → after**: the weak
control prompt (forbids tools) vs the recommended prompt (use the tools to complete the
task). "This wasn't hand-written — the service generated it from real traces."

☁️ **Console — CloudWatch / traces (optional):**
> Briefly show the `aws/spans` traces again. "This is the input to the recommendation —
> the model looked at how the agent actually behaved and proposed a fix."

💡 **Watch for:** the recommendation job also runs server-side and can take a few minutes;
the tab reads the persisted result, so present against the pre-run value.

---

## 6. Bundles — the config you can edit and A/B test

🎤 **Say:**
> "A change to an agent is really a change to its **configuration** — system prompt, model,
> tool descriptions. AgentCore captures that as an immutable, versioned **configuration
> bundle**. We split this into two steps: **generate-config** drafts editable configs, and
> **build-bundles** packages them into real bundle versions."

🖱️ **Do:** Open the **🧾 Bundles** tab. Show the **control** and **treatment** drafts —
model id and full system prompt, both editable.

🎤 **Say (the interactive hook):**
> "Because these are editable, I can run my *own* experiment. Say I want to test a different
> model for the treatment…"

🖱️ **Do (optional live edit):** Change the treatment **model id** (dropdown) or tweak the
system prompt, click **💾 Save treatment draft**. "Now my edit is the candidate."

☁️ **Console — AgentCore Configuration bundles:**
> Show the two bundles: **`PatientSupportControl600b2a-…`** and
> **`PatientSupportTreatment600b2a-…`**. Open one, show the **version id**, the
> **commit message**, and the stored configuration. "Immutable and versioned — every
> A/B variant points at a specific, auditable bundle version."

💡 If you did a live edit, this is where you'd click **▶ Build & run A/B** to rebuild from
your edit — but note it kicks off a ~15-min run. For the talk, prefer the pre-built bundles.

---

## 7. Candidate — regression check before going live

🎤 **Say:**
> "Before spending real A/B traffic, we sanity-check the candidate offline. The
> **offline-check** step evaluates *both* the control and the candidate bundle on the same
> curated cases — an apples-to-apples regression check."

🖱️ **Do:** Open the **🧪 Candidate** tab. Show the control → candidate score table with the
deltas. "We want to see the candidate hold or improve — no regressions — before we risk
live traffic on it."

---

## 8. A/B test — the real comparison on live traffic

🎤 **Say:**
> "Now the real test. The **ab-test** step stands up an AgentCore **Gateway** with a target
> to the runtime, splits live traffic between the control and treatment bundles, and scores
> both with online evaluation — including a **custom code-based trajectory evaluator** we
> deployed as a Lambda."

🖱️ **Do:** Open the **📊 A/B Results** tab. Walk the per-evaluator table:
> - GoalSuccessRate: control 0.1 → treatment 0.6
> - Helpfulness: 0.56 → 1.0 (**significant**, p ≈ 8e-08)
> - TrajectoryQuality: 0.0 → 0.8 (**significant**)
> "Treatment T1 is the significant winner — the tool-using prompt genuinely beats the
> tool-forbidden control."

🎤 **Say (the honesty beat):**
> "Note GoalSuccessRate jumped a lot but isn't *statistically* significant on its own — with
> ~10 sessions per variant, a binary metric is noisy. Significance comes from Helpfulness and
> the trajectory evaluator. That's the honest read: big effect, but significance depends on
> sample size too."

☁️ **Console — AgentCore Gateway:**
> Show the **Gateway** (`patientsupportgateway600b2a-…`) and its **Target**
> (`PatientSupportTarget600b2a`). "The gateway is the traffic-splitting front door for the
> A/B test — sticky per session so a user always sees one variant."

☁️ **Console — AgentCore Evaluations → Evaluators:**
> Show the registered **custom code-based evaluator** (`TrajectoryQuality75d926-…`) and the
> backing Lambda (`patientsupport-trajectory-eval`). "This is how you extend AgentCore with
> your *own* quality metric — a Lambda that scores the tool trajectory. The execution role
> was granted permission to invoke it."

💡 **Watch for:** significance takes ~10-15 min to aggregate; the poll log shows "no winner"
for the first ~13 polls, then flips. If your UI is stale, hit **🔄 Refresh A/B result**.

---

## 9. Promotion gate + human approval — the payoff

🎤 **Say:**
> "This is the piece most optimization stories skip. A significant winner does **not** get
> auto-promoted. The **promotion-gate** step opens a **pending approval** — and nothing is
> rolled out until a human decides."

🖱️ **Do:** Open the **✅ Approvals** tab. Show the pending `appr-…` request with the metrics
captured at request time. (If none is pending, click **↻ Reopen pending approval**.)

🎤 **Say:**
> "A reviewer sees exactly what they're approving — the winning variant and the evidence.
> This is the governance gate: promotions are deliberate, attributable, and auditable."

🖱️ **Do (the live moment):** Toggle **Enable live actions** in the sidebar, type the
`appr-…` id to confirm, and click **Approve & promote**.

🎤 **Say:**
> "That single click just did three real things: stopped the A/B test, promoted the winning
> configuration to 100% of traffic, and wrote an immutable audit record."

🖱️ **Do:** Open the **📜 Audit trail** tab. Show the new entry — decision, decider, the
metrics at decision time, and the outcome.

☁️ **Console — Configuration bundles / A/B test (optional):**
> Show that the A/B test is now **STOPPED** and the control bundle now carries the promoted
> (treatment) configuration. "The winner is the new baseline — the loop closes and the next
> iteration starts from here."

---

## 10. Close (30-60 seconds)

🎤 **Say:**
> "That's the full loop: measure offline, observe online, get an AI-generated improvement,
> package it as a versioned bundle, regression-check it, A/B test it on live traffic with
> both built-in and custom evaluators, and promote the winner **through a human approval
> gate** that's fully audited. Every step here is real Amazon Bedrock AgentCore — runtimes,
> gateways, evaluations, configuration bundles — not a simulation. And because the configs
> are editable in the UI, teams can run their own experiments: change the model or the
> prompt, and A/B test it the same way."

🎤 **One-liner to land on:**
> "AgentCore gives you the primitives to *keep* an agent good in production — and this loop
> shows how they compose into a safe, repeatable optimization workflow."

---

## Appendix — quick reference

**Loop order (stages):**
`deploy → offline-baseline → online → recommend → generate-config → build-bundles →
offline-check → ab-test → promotion-gate → (human) approve/reject`

**Real resources to show in the Console (this run):**
| Resource | Where in Console | Id (example) |
|---|---|---|
| Agent Runtime | AgentCore → Agent Runtimes | `PatientSupport-mzD1WU2GZU` |
| Execution role | IAM → Roles | `PatientSupportRole` |
| Traces / logs | CloudWatch → Log groups | `/aws/bedrock-agentcore/runtimes/PatientSupport-…-DEFAULT`, `aws/spans` |
| Online eval config | AgentCore → Evaluations → Online | `PatientSupportOnlineEval…` / A/B: `PatientSupportABEval600b2a-…` |
| Custom evaluator | AgentCore → Evaluations → Evaluators | `TrajectoryQuality75d926-…` (Lambda `patientsupport-trajectory-eval`) |
| Control bundle | AgentCore → Configuration bundles | `PatientSupportControl600b2a-…` |
| Treatment bundle | AgentCore → Configuration bundles | `PatientSupportTreatment600b2a-…` |
| Gateway + target | AgentCore → Gateways | `patientsupportgateway600b2a-…` / `PatientSupportTarget600b2a` |
| A/B test | AgentCore → A/B tests | `patientsupportab600b2a-…` |

**Timing expectations (so nothing feels "stuck"):**
- deploy ~2-3 min · offline-baseline ~8-12 min · online ~4-6 min · recommend ~3-5 min ·
  generate-config seconds · build-bundles ~30s · offline-check ~5-8 min ·
  ab-test ~6 min to drive + ~10-15 min for online eval to reach significance.
- The A/B poll log shows "no winner" for the first ~13 polls, then flips around poll 14 —
  that's the aggregation lag, not a failure. The stage breaks as soon as it's significant.

**If something looks stale in the UI:** click **🔄 Refresh artifacts** (sidebar) or, for
the A/B result specifically, **🔄 Refresh A/B result** on the A/B Results tab.

**Safety framing to repeat if asked:** nothing is promoted automatically; the promotion
gate always requires a human decision, and every decision is written to an append-only
audit log.
