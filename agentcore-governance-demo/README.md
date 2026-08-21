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
| 00 | preflight | tooling / credential / config checks |
| 05 | github_secret | GitHub App secret in Secrets Manager |
| 10 | infra | S3 bucket, VPC + S3 Files (CloudFormation), MCP proxy upload |
| 20 | dynamodb | `medical-records-demo` table + 7 seeded patients |
| 30 | mcp_runtimes | GitHub / Jira / DynamoDB MCP server images + runtimes |
| 40 | gateway | Gateway + IAM role (invoke/policy/guardrail/lambda perms) |
| 45 | targets | 4 gateway targets |
| 50 | guardrails | 2 Bedrock guardrails (published as version 1) |
| 55 | interceptor | guardrail-routing Lambda + role |
| 60 | policies | Cedar policy engine + 7 policies |
| 65 | attach_gateway | wire policy engine (ENFORCE) + interceptor onto gateway |
| 68 | agent_image | Claude Code agent container image |
| 70 | personas | 4 persona IAM roles + 4 VPC persona runtimes |

Re-running after a failure resumes from state; each step is safe to re-run.

## Run the demo

```bash
# Warm each persona once (loads the tool catalog):
for p in clinician dev auditor public; do
  python connect_persona.py --persona $p --cmd "echo ready"
done

# Clinician sees full PII:
python connect_persona.py --persona clinician \
  --prompt "Pull up patient PAT-001's full chart including SSN and phone."

# Dev has no medical tool at all (Cedar hides it):
python connect_persona.py --persona dev --prompt "List your MCP tools."

# Auditor can call the tool but PII is stripped by the restrictive guardrail:
python connect_persona.py --persona auditor --prompt "Get patient PAT-001."

# Public only has web search:
python connect_persona.py --persona public --prompt "Search the web for AWS AgentCore pricing."
```

Interactive terminal (PTY) into a persona's Claude Code:

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
├── connect_persona.py        # client (reads .deployed-state.json)
├── lib/common.sh             # config, naming, state helpers
├── infra/cfn-vpc.yaml        # VPC + S3 Files
├── mcp-proxy/                # stdio↔gateway SigV4 proxy (mounted via S3 Files)
├── mcp-servers/{github,jira-mock,dynamodb}/   # the 3 self-hosted MCP servers
├── interceptor/guardrails_interceptor.py      # per-persona guardrail routing
├── agent/                    # Claude Code container (shared by all personas)
└── scripts/                  # numbered deploy steps
```

## Notes

- **Cost / blast radius:** this creates a NAT gateway, VPC, S3 Files, 7 AgentCore
  runtimes, ECR repos, a DynamoDB table, guardrails, and a Lambda. Run
  `./cleanup-all.sh` when finished.
- **Secrets:** `config.env`, `.deployed-state.json`, and `*.pem` are git-ignored.
  The Tavily key is embedded in the `TavilyWebSearch` target endpoint (as in the
  original demo); treat the deployed target config as sensitive.
- **Container tooling:** all image builds use `finch` per project conventions.
```
