# AgentCore Governance Demo — Detailed Architecture

## Overview

This demo showcases Amazon Bedrock AgentCore's full governance stack: a Claude Code coding agent accessing enterprise tools (GitHub, Jira, DynamoDB, Web Search) through a centralized MCP Gateway, with layered access control enforced by Cedar Policy, Bedrock Guardrails, and network-level security.

Four personas demonstrate different levels of access — from full PII visibility (clinician) to minimal tool access (public agent) — all using the same infrastructure with governance applied per-principal.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                              Local Machine (Demo Operator)                               │
│                                                                                         │
│   python connect_persona.py --persona clinician|dev|auditor|public                      │
│   ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐                       │
│   │ Clinician  │  │    Dev     │  │  Auditor   │  │   Public   │                       │
│   │  connect   │  │  connect   │  │  connect   │  │  connect   │                       │
│   └─────┬──────┘  └─────┬──────┘  └─────┬──────┘  └─────┬──────┘                       │
│         │                │                │                │                             │
│         └────────────────┴────────────────┴────────────────┘                             │
│                          │ WebSocket Shell (SigV4-signed)                                │
└──────────────────────────┼──────────────────────────────────────────────────────────────┘
                           │
┌──────────────────────────┼──────────────────────────────────────────────────────────────┐
│                          ▼         AWS Account (211395677819)                            │
│                                                                                         │
│  ┌────────────────────────────────────────────────────────────────────────────────┐     │
│  │                    Amazon Bedrock AgentCore Runtime                             │     │
│  │                                                                                │     │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  ┌───────────┐ │     │
│  │  │ Claude Code     │  │ Claude Code     │  │ Claude Code     │  │ Claude    │ │     │
│  │  │ (Clinician)     │  │ (Dev)           │  │ (Auditor)       │  │ (Public)  │ │     │
│  │  │                 │  │                 │  │                 │  │           │ │     │
│  │  │ Role:           │  │ Role:           │  │ Role:           │  │ Role:     │ │     │
│  │  │ persona-        │  │ persona-dev     │  │ persona-        │  │ persona-  │ │     │
│  │  │ clinician       │  │                 │  │ auditor         │  │ public    │ │     │
│  │  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘  └─────┬─────┘ │     │
│  │           │                    │                    │                  │        │     │
│  │           └────────────────────┴────────────────────┴──────────────────┘        │     │
│  │                                │                                               │     │
│  │                    MCP Gateway Proxy (index.js)                                 │     │
│  │                    /mnt/s3files/mcp/ (S3 Files mount)                           │     │
│  │                    SigV4-signs requests to Gateway                              │     │
│  └────────────────────────────────┼───────────────────────────────────────────────┘     │
│                                   │                                                     │
│                                   │ HTTPS (SigV4) via VPC Endpoint                      │
│                                   ▼                                                     │
│  ┌────────────────────────────────────────────────────────────────────────────────┐     │
│  │                    AgentCore Gateway (IAM Auth)                                 │     │
│  │                    github-mcp-gateway-50rpwklbtt                                │     │
│  │                                                                                │     │
│  │  ┌──────────────┐                                                              │     │
│  │  │ Cedar Policy │  ← Policy Engine (GatewayDemoPolicy)                         │     │
│  │  │ Engine       │    DEFAULT-DENY: Only explicitly permitted actions allowed    │     │
│  │  │              │                                                              │     │
│  │  │ Policies:    │    • ClinicianFullAccess: permit all tools                   │     │
│  │  │              │    • DevGitHub/Jira/Tavily: permit 3 targets, deny medical   │     │
│  │  │              │    • AuditorReadTools: permit get/list/search only            │     │
│  │  │              │    • PublicSearchOnly: permit tavily_search only              │     │
│  │  └──────────────┘                                                              │     │
│  │                                                                                │     │
│  │  ┌──────────────┐                                                              │     │
│  │  │ Lambda       │  ← Response Interceptor                                     │     │
│  │  │ Interceptor  │    Reads caller identity from context.identity.awsPrincipalArn│    │
│  │  │              │    Routes to per-persona Bedrock Guardrail:                   │     │
│  │  │              │    • Clinician → permissive (hate/politics only)             │     │
│  │  │              │    • All others → restrictive (hate/politics + PII + medical)│     │
│  │  └──────────────┘                                                              │     │
│  │                                                                                │     │
│  │  ┌────────────────────────────────────────────────────────────────────────┐    │     │
│  │  │                        Gateway Targets (4)                             │    │     │
│  │  │                                                                        │    │     │
│  │  │  ┌──────────┐  ┌──────────────┐  ┌────────────┐  ┌────────────────┐   │    │     │
│  │  │  │ GitHub   │  │ Jira Mock    │  │ DynamoDB   │  │ Tavily Web     │   │    │     │
│  │  │  │ MCP      │  │ MCP          │  │ MCP        │  │ Search         │   │    │     │
│  │  │  │ (13)     │  │ (8 tools)    │  │ (7 tools)  │  │ (5 tools)      │   │    │     │
│  │  │  │ tools)   │  │              │  │            │  │                │   │    │     │
│  │  │  └────┬─────┘  └──────┬───────┘  └─────┬──────┘  └───────┬────────┘   │    │     │
│  │  └───────┼────────────────┼────────────────┼─────────────────┼────────────┘    │     │
│  └──────────┼────────────────┼────────────────┼─────────────────┼─────────────────┘     │
│             │                │                │                 │                        │
│             ▼                ▼                ▼                 ▼                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐            │
│  │ AgentCore    │  │ AgentCore    │  │ AgentCore    │  │ Tavily Hosted    │            │
│  │ Runtime      │  │ Runtime      │  │ Runtime      │  │ MCP Server       │            │
│  │ (FastMCP)    │  │ (FastMCP)    │  │ (FastMCP)    │  │ (External HTTPS) │            │
│  │              │  │              │  │              │  │                  │            │
│  │ → GitHub API │  │ → Mock Data  │  │ → DynamoDB   │  │ → Web Search API │            │
│  │   (via App)  │  │              │  │   Table      │  │                  │            │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────────┘            │
│                                             │                                           │
│                                             ▼                                           │
│                                    ┌──────────────────┐                                 │
│                                    │   DynamoDB       │                                 │
│                                    │   medical-       │                                 │
│                                    │   records-demo   │                                 │
│                                    │                  │                                 │
│                                    │ 7 patient records│                                 │
│                                    │ (3 with PII,     │                                 │
│                                    │  4 without)      │                                 │
│                                    └──────────────────┘                                 │
│                                                                                         │
│  ┌────────────────────────────────────────────────────────────────────────────────┐     │
│  │                         Network Security                                       │     │
│  │                                                                                │     │
│  │  VPC (10.0.0.0/16) with Private Subnets                                       │     │
│  │  ├── VPC Endpoint: com.amazonaws.us-west-2.bedrock-agentcore.gateway           │     │
│  │  ├── VPC Endpoint: com.amazonaws.us-west-2.bedrock-runtime                     │     │
│  │  ├── VPC Endpoint: com.amazonaws.us-west-2.bedrock-agentcore                   │     │
│  │  ├── S3 Files Mount Targets (NFS port 2049)                                    │     │
│  │  └── NAT Gateway (outbound internet for MCP targets)                           │     │
│  │                                                                                │     │
│  │  Security Groups:                                                              │     │
│  │  ├── Agent SG: Outbound restricted to VPC endpoints + NFS + DNS               │     │
│  │  └── VPCE SG: Inbound HTTPS from Agent SG only                                │     │
│  └────────────────────────────────────────────────────────────────────────────────┘     │
│                                                                                         │
│  ┌────────────────────────────────────────────────────────────────────────────────┐     │
│  │                         Bedrock (Model Inference)                               │     │
│  │  Accessed via VPC Endpoint (PrivateLink) — no internet traversal               │     │
│  │  Claude Sonnet 4.5 (default model for Claude Code)                             │     │
│  └────────────────────────────────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Components

### 1. Claude Code Agent (on AgentCore Runtime)

| Property | Value |
|----------|-------|
| Container | `coding-agents-claude-code:latest` (node:22-slim + Claude Code CLI) |
| Protocol | HTTP (WebSocket Shell for interactive PTY) |
| Model | Claude Sonnet 4.5 via Bedrock (IAM role, no API key) |
| MCP Access | Gateway Proxy at `/mnt/s3files/mcp/index.js` (mounted via S3 Files) |
| Instances | 4 runtimes (one per persona), same image, different IAM roles |

**Persona Runtimes:**

| Persona | Runtime ID | IAM Role |
|---------|-----------|----------|
| Clinician | `claude_code_persona_clinician-YT8ors5RRK` | `persona-clinician` |
| Dev | `claude_code_persona_dev-tJRLrV8Q7E` | `persona-dev` |
| Auditor | `claude_code_persona_auditor-i6V0XOHbKZ` | `persona-auditor` |
| Public | `claude_code_persona_public-3037yFHZig` | `persona-public` |

---

### 2. AgentCore Gateway

| Property | Value |
|----------|-------|
| Gateway ID | `github-mcp-gateway-50rpwklbtt` |
| URL | `https://github-mcp-gateway-50rpwklbtt.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp` |
| Auth | AWS IAM (SigV4) |
| Protocol | MCP |
| Policy Engine | `GatewayDemoPolicy-95hqf9ixlz` (ENFORCE mode) |
| Interceptor | Lambda response interceptor for guardrails routing |
| Total Tools | 30 (across 4 targets) |

---

### 3. Gateway Targets (MCP Servers)

#### GitHub MCP (13 tools)
| Property | Value |
|----------|-------|
| Type | AgentCore Runtime (FastMCP container) |
| Runtime ID | `github_mcp_runtime-Vjz9jVH2AY` |
| Auth to GitHub | GitHub App credentials (Secrets Manager) |
| Tools | get_issue, list_files, create_branch, put_file, create_pull_request, comment_on_issue, etc. |
| Listing Mode | DEFAULT (pre-indexed) |

#### Jira Mock (8 tools)
| Property | Value |
|----------|-------|
| Type | AgentCore Runtime (FastMCP container) |
| Runtime ID | `jira_mock_mcp_runtime-uSTKsoCvti` |
| Data | In-memory mock data (2 projects, 4 issues) |
| Tools | list_projects, get_issue, create_issue, update_issue, transition_issue, add_comment, search_issues, delete_issue |
| Listing Mode | DEFAULT (pre-indexed) |

#### Medical Records / DynamoDB (7 tools)
| Property | Value |
|----------|-------|
| Type | AgentCore Runtime (FastMCP container) |
| Runtime ID | `dynamodb_mcp_runtime-3X8e1D9RAW` |
| Data Source | DynamoDB table `medical-records-demo` |
| Records | 7 patients (3 with PII: SSN, phone, email, address; 4 without) |
| Tools | get_patient, list_patients, search_patients, put_patient, delete_patient, update_patient_status, add_clinical_note |
| Listing Mode | DEFAULT (pre-indexed) |

#### Tavily Web Search (5 tools)
| Property | Value |
|----------|-------|
| Type | External hosted MCP server (Streamable HTTP) |
| Endpoint | `https://mcp.tavily.com/mcp/?tavilyApiKey=...` |
| Tools | tavily_search, tavily_research, tavily_extract, tavily_crawl, tavily_map |
| Listing Mode | DEFAULT (pre-indexed via SSE handshake) |

---

### 4. Governance Layer 1: Cedar Policy (Deterministic Access Control)

**Default-deny model**: All actions are denied unless explicitly permitted by a Cedar policy.

| Policy Name | Principal | Effect |
|-------------|-----------|--------|
| `ClinicianFullAccess` | persona-clinician | Permit ALL tools on this gateway |
| `DevGitHub` | persona-dev | Permit all GitHubMCP tools |
| `DevJira` | persona-dev | Permit all JiraMock tools |
| `DevTavily` | persona-dev | Permit all TavilyWebSearch tools |
| `AuditorReadTools` | persona-auditor | Permit only get_*, list_*, search_* tools |
| `PublicSearchOnly` | persona-public | Permit only `TavilyWebSearch___tavily_search` |

**Key behavior**: The gateway's `tools/list` response is automatically filtered per-principal. Agents only discover tools they're authorized to call.

---

### 5. Governance Layer 2: Bedrock Guardrails (Content Filtering)

Applied via a Lambda Response Interceptor that routes to different guardrails based on caller identity.

#### Clinician Guardrail (`vycrdl8gt25y`)
- Blocks: Hate speech, political topics, violence, sexual content
- Allows: PII, medical diagnoses (clinician needs full access)

#### Restrictive Guardrail (`ptrt23n92k5y`)
- Blocks: Hate speech, political topics, violence, sexual content
- Blocks: PII (SSN, phone, email, address, name — via `sensitiveInformationPolicy`)
- Blocks: Medical diagnoses (via topic filter)

#### Lambda Interceptor Logic
```
event["mcp"]["gatewayRequest"]["context"]["identity"]["awsPrincipalArn"]
  → if contains "persona-clinician" → apply clinician guardrail
  → otherwise → apply restrictive guardrail
```

---

### 6. Governance Layer 3: Network Security

| Control | Purpose |
|---------|---------|
| VPC Endpoint (Gateway) | Agent reaches gateway via PrivateLink only |
| VPC Endpoint (Bedrock Runtime) | Model inference stays on AWS backbone |
| VPC Endpoint (AgentCore Data Plane) | Runtime management traffic stays private |
| Security Group (Agent) | Outbound restricted: HTTPS to VPCEs, NFS, DNS only |
| Security Group (VPCE) | Inbound: only the agent SG can reach endpoints |
| Private Subnets | No direct internet access; NAT for outbound targets |

**Effect**: The agent cannot load rogue MCP servers from the internet. It can only reach the governed gateway.

---

## Governance Results Matrix

| Persona | Medical (PII) | GitHub | Jira | Tavily | Governance Layer |
|---------|:---:|:---:|:---:|:---:|---|
| **Clinician** | ✅ PII visible | ✅ All | ✅ All | ✅ All | Permissive guardrail |
| **Dev** | 🚫 Tool hidden | ✅ All | ✅ All | ✅ All | Policy: no medical |
| **Auditor** | 🛡️ PII blocked | ✅ Read | ✅ Read | ✅ Search | Policy: read-only + Guardrail: PII filter |
| **Public** | 🚫 Tool hidden | 🚫 Denied | 🚫 Denied | ✅ Search only | Policy: tavily_search only |

---

## Demo Script

```bash
# Clinician: sees full patient data including PII
python connect_persona.py --persona clinician --prompt "Get patient PAT-001. Show SSN and phone."

# Dev: medical tools are completely hidden
python connect_persona.py --persona dev --prompt "List your MCP tools."

# Auditor: can call medical tool but PII is blocked by guardrail
python connect_persona.py --persona auditor --prompt "Get patient PAT-001."

# Public: only has web search
python connect_persona.py --persona public --prompt "Search for AWS AgentCore pricing."
```

---

## Key Takeaways for Customers

1. **Cedar Policy is deterministic** — unlike guardrails (probabilistic), policy decisions are absolute and auditable
2. **Guardrails add defense-in-depth** — even if policy allows access, content filtering protects sensitive data
3. **Gateway is the single control point** — all tools go through one governed endpoint
4. **Network controls prevent bypass** — agents can't load unauthorized MCP servers
5. **Per-principal governance** — same infrastructure, different access levels based on IAM identity
6. **Tool discovery is filtered** — agents only see tools they're allowed to call (reduced attack surface)
