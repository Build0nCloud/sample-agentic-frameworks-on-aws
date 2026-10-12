# AgentCore Governance Demo — Fresh-Account Deployment

This project deploys the **Amazon Bedrock AgentCore governance demo** end-to-end
into a brand-new AWS account with a single command. It reproduces the full
governance stack described in the parent project's `DEMO-ARCHITECTURE.md` and
`DEMO-CHEATSHEET.md`:

- **One governed MCP Gateway** (IAM auth) fronting **4 tool targets**:
  `GitHubMCP`, `JiraMock`, `MedicalRecords` (DynamoDB, with PII), and
  `TavilyWebSearch` (hosted).
- **Cedar policy engine** (default-deny) with 7 per-persona policies for
  deterministic tool-access control.
- **Two Bedrock Guardrails** (clinician-permissive, restrictive) applied by a
  **Lambda response interceptor** that routes on the caller's IAM identity.
- **4 Claude Code persona runtimes** (clinician / dev / auditor / public), each
  with its own IAM role, on a private **VPC** with **S3 Files**.

Everything is parameterized — there are **no hardcoded account ids, ARNs, or
resource ids**. Generated ids are resolved at run time and stored in
`.deployed-state.json`.

## Governance outcomes

| Persona | Medical (PII) | GitHub | Jira | Tavily | Enforced by |
|---------|:---:|:---:|:---:|:---:|---|
| **Clinician** | ✅ PII visible | ✅ All | 🚫 denied | ✅ All | Cedar (forbid Jira) + permissive guardrail |
| **Dev** | 🚫 tool hidden | ✅ All | ✅ All | ✅ All | Cedar (no medical) |
| **Auditor** | 🛡️ PII stripped | ✅ read | ✅ read | ✅ search | Cedar read-only + restrictive guardrail |
| **Public** | 🚫 tool hidden | 🚫 | 🚫 | ✅ search only | Cedar (tavily_search only) |

## Prerequisites

- **AWS CLI v2**, configured with admin credentials for the **fresh account**.
  Default region `us-west-2` (override with `AWS_REGION`). Claude models must be
  enabled in Bedrock for the region.
- **finch** (container builds — Docker-compatible CLI)
- **jq**, **python3**, **node**/**npm**, **zip**
- A **Tavily API key** — https://app.tavily.com/
- A **GitHub App** (see below)

Install the local Python client deps (use a venv):

```bash
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt
```

### GitHub App

The `GitHubMCP` target authenticates to GitHub via a GitHub App installation token.
Create one at https://github.com/settings/apps:

1. Repository permissions: **Contents** R/W, **Issues** R/W, **Pull requests** R/W, **Metadata** R.
2. Install it on a repo; note the **App ID**, **Installation ID**, and download the **private key** `.pem`.

## Deploy

```bash
cp config.env.example config.env      # fill in TAVILY_API_KEY + GitHub App values
./deploy-all.sh
```

`deploy-all.sh` runs these idempotent steps (also runnable individually from `scripts/`):

| # | Step | Creates |
|---|------|---------|
| 00 | preflight | tooling / credential / config checks (incl. enabled agents) |
| 05 | github_secret | GitHub App secret in Secrets Manager |
| 07 | agent_secrets | per-agent API keys in Secrets Manager (e.g. Devin) |
| 10 | infra | S3 bucket, VPC + S3 Files (CloudFormation), MCP proxy upload |
| 20 | dynamodb | `medical-records-demo` table + 7 seeded patients |
| 30 | mcp_runtimes | GitHub / Jira / DynamoDB MCP server images + runtimes |
| 40 | gateway | Gateway + IAM role (invoke/policy/guardrail/lambda perms) |
| 45 | targets | 4 gateway targets |
| 50 | guardrails | 2 Bedrock guardrails (published as version 1) |
| 55 | interceptor | guardrail-routing Lambda + role |
| 60 | policies | Cedar policy engine + 7 policies |
| 65 | attach_gateway | wire policy engine (ENFORCE) + interceptor onto gateway |
| 68 | agent_image | container image for each enabled agent (Claude Code, Devin, …) |
| 70 | personas | 4 shared persona IAM roles + a VPC runtime per persona × agent |

Re-running after a failure resumes from state; each step is safe to re-run.

## Coding agents (pluggable)

The agent that runs inside the persona runtimes is pluggable. Governance is
enforced at the gateway on the persona's IAM identity, so the allow/deny outcome
is identical regardless of which agent runs.

- **Claude** (Claude Code) — `--agent claude-code` (default); inference via Amazon Bedrock.
- **Devin** (Devin CLI) — `--agent devin` (alias `devin.ai`); inference via Devin's backend (needs `DEVIN_API_KEY`).

Enable which agents get built/deployed with `AGENTS` in `config.env`
(e.g. `AGENTS="claude-code devin"`). Each agent is a self-contained image under
`agents/<slug>/` that honors one contract: expose `/app/run.sh` and reach tools
only through the stdio MCP proxy. Adding another agent (e.g. Cursor) is a new
`agents/<slug>/` directory plus a registry entry in `lib/common.sh` — no changes
to the gateway, policies, or guardrails.

## Run the demo

```bash
# Warm every deployed persona x agent (handles cold-start):
./warm-personas.sh

# Clinician sees full PII (default agent = Claude Code):
python connect_persona.py --persona clinician \
  --prompt "Pull up patient PAT-001's full chart including SSN and phone."

# Same persona, run with Devin instead — same governance result:
python connect_persona.py --persona clinician --agent devin.ai \
  --prompt "Pull up patient PAT-001's full chart including SSN and phone."

# Dev has no medical tool at all (Cedar hides it):
python connect_persona.py --persona dev --prompt "List your MCP tools."

# Auditor can call the tool but PII is stripped by the restrictive guardrail:
python connect_persona.py --persona auditor --prompt "Get patient PAT-001."

# Public only has web search:
python connect_persona.py --persona public --prompt "Search the web for AWS AgentCore pricing."

# Verify the whole allow/deny matrix (all enabled agents x personas):
./verify-governance.sh
```

Interactive terminal (PTY) into a persona's agent (add `--agent devin.ai` for Devin):

```bash
python connect_persona.py --persona clinician
```

## Teardown

```bash
./cleanup-all.sh                     # prompts; removes all demo resources
./cleanup-all.sh --yes --delete-bucket   # non-interactive, also removes the S3 bucket
```

## Layout

```
governance-demo/
├── config.env.example        # your parameters (copy to config.env)
├── deploy-all.sh             # orchestrator
├── cleanup-all.sh            # teardown
├── warm-personas.sh          # warm persona×agent runtimes (cold-start handling)
├── verify-governance.sh      # allow/deny matrix across agents × personas
├── connect_persona.py        # client (--persona, --agent; reads per-account state)
├── lib/common.sh             # config, naming, state helpers, agent registry
├── infra/cfn-vpc.yaml        # VPC + S3 Files
├── mcp-proxy/                # stdio↔gateway SigV4 proxy (mounted via S3 Files)
├── mcp-servers/{github,jira-mock,dynamodb}/   # the 3 self-hosted MCP servers
├── interceptor/guardrails_interceptor.py      # per-persona guardrail routing
├── agents/<slug>/            # one dir per coding agent (claude-code, devin, …)
└── scripts/                  # numbered deploy steps
```

## Notes

- **Cost / blast radius:** this creates a NAT gateway, VPC, S3 Files, ECR repos,
  a DynamoDB table, guardrails, a Lambda, and AgentCore runtimes — 3 MCP servers
  plus one persona runtime per persona × enabled agent (e.g. 8 with Claude + Devin).
  Run `./cleanup-all.sh` when finished.
- **Secrets:** `config.env`, `.deployed-state.json`, and `*.pem` are git-ignored.
  The Tavily key is embedded in the `TavilyWebSearch` target endpoint (as in the
  original demo); treat the deployed target config as sensitive.
- **Container tooling:** all image builds use `finch` per project conventions.
```
