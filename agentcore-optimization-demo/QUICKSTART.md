# Quickstart — Reproduce on a Fresh AWS Account

This is the shortest path from a clean AWS account to a running demo of the Amazon Bedrock
AgentCore optimization loop. Everything is account-agnostic: names and IDs are derived from
your own account at runtime (nothing is hardcoded).

> ⚠️ **This provisions real, billable AgentCore resources** (runtime, gateway, evaluations,
> configuration bundles, A/B tests, a Lambda evaluator, an S3 object, IAM roles). See
> [Teardown](#7-teardown) when you're done.

Two ways to run it:
- **This guide (the `demo` CLI / Streamlit UI)** — the automated path. Recommended.
- **[MANUAL_DEPLOYMENT.md](MANUAL_DEPLOYMENT.md)** — the same loop as raw AWS API calls
  (no CLI, no UI), for production/infra-as-code.

---

## 1. Prerequisites (the two you can't script)

**a. Amazon Bedrock AgentCore must be available/enabled** in your account and target region.
Confirm reachability:
```bash
export AWS_REGION=us-east-1     # or your region (AgentCore must be available there)
aws bedrock-agentcore-control list-agent-runtimes --region "$AWS_REGION" >/dev/null \
  && echo "AgentCore reachable"
```

**b. Model access.** Enable the foundation model in **Bedrock → Model access** for that
region (the demo defaults to Claude Sonnet 5), then verify:
```bash
aws bedrock-runtime converse --region "$AWS_REGION" \
  --model-id us.anthropic.claude-sonnet-5 \
  --messages '[{"role":"user","content":[{"text":"say ok"}]}]' \
  --inference-config '{"maxTokens":10}' \
  --query 'output.message.content[0].text' --output text        # → ok
```
> To use a different model, set `agent.model_id` in `config/demo_config.yaml` (and enable
> that model in Bedrock).

**c. Credentials & identity.** Standard AWS credential chain, with permission to create IAM
roles, S3 buckets, Lambda functions, and AgentCore resources:
```bash
aws sts get-caller-identity --query Account --output text       # your account id
```

**d. Local tooling.**
- **Python 3.10–3.13** (3.13 recommended; 3.14 lacks wheels for the AWS/agent deps).
- **`pip` able to fetch prebuilt wheels** for `manylinux2014_aarch64` (the runtime package)
  and `manylinux2014_x86_64` (the Lambda evaluator). Standard pip on macOS/Linux does this.
- `git`, and `zip`.

---

## 2. Get the code and install

```bash
git clone <this-repo-url> agentcore-optimization-demo
cd agentcore-optimization-demo

python3 -m venv .venv && source .venv/bin/activate     # use a 3.10–3.13 interpreter
pip install -e ".[dev,ui]"        # dev = tests; ui = the Streamlit front end
pip install pip                   # ensure pip is inside the venv (deploy packages deps with it)
```

Sanity-check the code with no AWS calls (free):
```bash
pytest -q                         # unit + property tests; the live test is skipped by default
```

> The read-only vendored AWS sample under `reference/` is **not** required to run the loop
> (the demo code doesn't import it). It's only used by the cleanup shortcut; re-fetch it with
> `bash scripts/fetch_reference.sh` if you want it.

---

## 3. Configure (optional)

All settings live in [`config/demo_config.yaml`](config/demo_config.yaml): region, resource
name prefixes, model id, evaluators, dataset paths, sampling, A/B weights, and the optional
weak-baseline prompt. Credentials are **never** stored here — they come from the AWS chain.

Defaults work out of the box. Region resolves from `AWS_REGION`/`AWS_DEFAULT_REGION` or the
YAML (default `us-east-1`).

---

## 4. Run the loop

Everything is named/derived from your account at runtime — no per-account edits needed.

### Option A — the UI (recommended)
```bash
demo-ui                           # opens http://localhost:8501
```
In the sidebar, toggle **Enable live actions**, then on the **🚀 Run demo** tab click
**▶ Run all stages**. Progress streams live; the loop stops at `promotion-gate`, which opens a
pending approval. Watch results populate on the Online, Recommendation, Candidate, A/B Results,
and Reports tabs.

### Option B — the CLI
```bash
demo                              # list the stages
demo --yes run-all                # run deploy → … → promotion-gate (prompts without --yes)
```

Either way, a full run is ~25–40 min (the A/B online evaluation aggregates asynchronously,
~10–15 min). Individual stages are much faster and can be run one at a time; state persists
under `artifacts/` between invocations.

---

## 5. Approve the winner (human-in-the-loop)

When the A/B test finds a statistically significant winner, `promotion-gate` opens a pending
approval — **nothing is promoted until a human decides.**

- **UI:** on the **✅ Approvals** tab (live actions enabled), expand the pending `appr-…`, type
  its id to confirm, and click **Approve & promote**.
- **CLI:**
  ```bash
  demo promotion-status           # list pending approvals → note the appr-… id
  demo approve <appr-id>          # promote the winner (or: demo reject <appr-id>)
  ```

Approving stops the A/B test, promotes the winning configuration to 100%, and writes an
append-only audit entry.

> If the A/B result hasn't reached significance yet (online eval is still aggregating), use the
> UI's **🔄 Refresh A/B result** button (or re-run the `ab-test` stage) a few minutes later.

---

## 6. Where the outputs go

All generated artifacts are written under `artifacts/` (gitignored): baselines, the offline
evaluation reports, configuration-bundle refs, the A/B result, approvals, the audit log, and
`loop_state.json` (the cross-stage state). The UI reads from here; nothing is required to be
committed.

---

## 7. Teardown

Stop ongoing costs when done. The demo creates: an Agent Runtime, a Gateway + target,
online-evaluation configs, configuration bundles, A/B tests, a trajectory-evaluator Lambda,
IAM roles, and an S3 object.

Fastest path (vendored sample's cleanup script; adjust `--name` to your `agent.runtime_name`):
```bash
bash scripts/fetch_reference.sh   # if you haven't already fetched reference/
python reference/amazon-bedrock-agentcore-samples/01-features/06-observe-evaluate-optimize-your-agent/03-optimize/cleanup.py --name PatientSupport
```
That script does not remove the trajectory evaluator Lambda, the registered code evaluator, or
the Lambda execution role — delete those separately. For the full, per-resource teardown
commands (and the exact API calls), see **[MANUAL_DEPLOYMENT.md](MANUAL_DEPLOYMENT.md) → Cleanup**.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `list-agent-runtimes` fails / AccessDenied | AgentCore may not be enabled for your account/region (see §1a), or your principal lacks permission. |
| Agent invocation fails with a model/access error | Enable the model in Bedrock → Model access for the region (see §1b), or point `agent.model_id` at an enabled model. |
| `pip install` build errors on Python 3.14 | Use a Python 3.10–3.13 interpreter for the venv. |
| Deploy can't build the package | Ensure pip can fetch `manylinux2014_aarch64` wheels (Python 3.13); `pip install pip` into the venv. |
| A/B shows "no significant winner" | Online eval aggregates ~10–15 min; refresh the A/B result (UI button) or re-run `ab-test`. |
| Credentials expired mid-run | Refresh creds and re-run remaining stages — state persists under `artifacts/`; the A/B test keeps running server-side. |

---

## More docs

- **[README.md](README.md)** — overview and layout.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — how it works: the flow, every artifact, and each component.
- **[MANUAL_DEPLOYMENT.md](MANUAL_DEPLOYMENT.md)** — production, API-only deployment on a fresh account.
