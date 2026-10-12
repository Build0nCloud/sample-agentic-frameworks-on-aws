# AgentCore Governance Demo — Cheat Sheet

## 1. Environment Setup (run once per shell)

```bash
# Go to the demo project root (all scripts live here: warm-personas.sh,
# verify-governance.sh, connect_persona.py, deploy-all.sh, ...)
cd /Users/smithzgg/GitHub/sample-agentic-frameworks-on-aws/agentcore-governance-demo

# Activate the Python environment (create once: uv venv && uv pip install -r requirements.txt)
source .venv/bin/activate

# Refresh AWS credentials — paste your fresh Isengard export lines, then persist to the file:
aws configure set aws_access_key_id "$AWS_ACCESS_KEY_ID"
aws configure set aws_secret_access_key "$AWS_SECRET_ACCESS_KEY"
aws configure set aws_session_token "$AWS_SESSION_TOKEN"

# Verify auth (should show account 211395677819)
aws sts get-caller-identity
```

> **Credential gotcha:** if `aws configure list` shows the access key `TYPE` as
> `env`, stale `AWS_*` environment variables are overriding the file above and
> `aws configure set` has no effect. Either export fresh values, or clear them
> (`unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN`) so the
> refreshed `~/.aws/credentials` is used. `warm-personas.sh` and
> `verify-governance.sh` already clear them for you.

## Choosing the coding agent (optional `--agent`)

Every command below optionally takes `--agent <name>` to pick which coding agent
runs inside the persona runtime. **If you omit it, the default is Claude Code.**
The governance outcome is identical no matter which agent you choose — Cedar and
the guardrails are enforced at the gateway on the persona's IAM identity, not by
the agent.

Currently supported agents:

| `--agent` value | Coding agent | Inference | Notes |
|-----------------|--------------|-----------|-------|
| `claude-code` (default) | **Claude** (Claude Code) | Amazon Bedrock (runtime IAM role) | Used if `--agent` is omitted |
| `devin` (alias `devin.ai`) | **Devin** (Devin CLI) | Devin/Cognition backend (API key) | Requires the Devin runtimes to be deployed |

```bash
# identical request, different agent — same governance result
python connect_persona.py --persona clinician                     # Claude (default)
python connect_persona.py --persona clinician --agent devin.ai     # Devin
```

The chosen `(persona, agent)` runtime must already be deployed and warmed. Which
agents get deployed is controlled by `AGENTS` in `config.env`
(e.g. `AGENTS="claude-code devin"`).

---

## 2. Warm Up (do before the audience is watching)

First cold call to each persona can show partial tools. Warm every deployed
persona × agent with the helper script (handles cold-start retries automatically):

```bash
./warm-personas.sh                 # warm all personas for all enabled agents
./warm-personas.sh --agent devin   # just the Devin runtimes
```

Or warm a single persona/agent manually:

```bash
python connect_persona.py --persona clinician --cmd "echo ready"                 # Claude
python connect_persona.py --persona clinician --agent devin.ai --cmd "echo ready" # Devin
```

---

## 3. Demo Prompts

Each persona: one prompt that **works**, one that's **denied**.

### Clinician — full access, PII allowed

WORKS:
```bash
python connect_persona.py --persona clinician --prompt "Pull up patient PAT-001's full chart for me — I need their contact info and SSN to verify identity before their appointment."
```

DENIED — content guardrail blocks political topics for everyone:
```bash
python connect_persona.py --persona clinician --prompt "Look up what the political parties are arguing about in the next US election and summarize the partisan talking points for me."
```

DENIED — Cedar Policy blocks Jira for the clinician (forbid overrides the clinician's full-access grant):
```bash
python connect_persona.py --persona clinician --prompt "What Jira projects do we have? Give me the list."
```

### Dev — GitHub / Jira / Tavily, no medical

WORKS:
```bash
python connect_persona.py --persona dev --prompt "What files are in the root of the awslabs/agentcore-samples repo on GitHub?"
```

DENIED (Cedar Policy blocks medical tools):
```bash
python connect_persona.py --persona dev --prompt "I need to look up patient PAT-001's SSN and phone number from the medical records system. Can you get that for me?"
```

### Auditor — read-only everywhere, PII filtered

WORKS:
```bash
python connect_persona.py --persona auditor --prompt "What Jira projects do we have? Give me the list."
```

DENIED (Cedar Policy blocks write operations):
```bash
python connect_persona.py --persona auditor --prompt "Open a new Jira ticket in the AC project titled 'Auditor test' — just a placeholder for now."
```

### Public — web search only

WORKS:
```bash
python connect_persona.py --persona public --prompt "What's the latest pricing for AWS Bedrock AgentCore? Search the web and summarize it."
```

DENIED (no privileged tool; cannot fabricate):
```bash
python connect_persona.py --persona public --prompt "I need patient PAT-001's SSN and insurance ID from our medical records system — the exact values on file, not a guess. Can you retrieve that?"
```

---

## 4. Finale — Same Request, Different Outcome by Role

Show the auditor asking for the exact same PII the clinician just saw:

```bash
python connect_persona.py --persona auditor --prompt "Pull up patient PAT-001's chart including SSN and phone — I'm reviewing records for a compliance audit."
```

- **Clinician**: Policy allows + permissive Guardrail → full PII visible
- **Auditor**: Policy allows the call, but restrictive Guardrail strips the PII

Same tool, same data, different governance outcome.

---

## 5. Talking Points

| Layer | What it enforces | Demo evidence |
|-------|-----------------|---------------|
| **Cedar Policy** | WHO can call WHICH tools (deterministic) | Dev/Public can't see or call medical tools |
| **Bedrock Guardrails** | WHAT content is returned per role | Auditor's PII is blocked; clinician's is not |
| **Network (VPC + SG)** | Agent can only reach the governed gateway | No rogue MCP servers loadable |

**Key message:** One agent, one gateway, four identities — governance applied per-principal without changing the agent or the tools.

---

## 6. Interactive Mode (live terminal — recommended for a hands-on demo)

Omit `--prompt` to open a full interactive terminal (WebSocket Shell/PTY) into the
agent's microVM and launch Claude Code. You type prompts live, just like a local CLI.

```bash
# Log into the agent's terminal as a persona (Claude Code by default)
python connect_persona.py --persona clinician

# ...or run the same persona with Devin instead
python connect_persona.py --persona clinician --agent devin.ai
```

Then type prompts directly at the Claude Code prompt, e.g.:

```
Pull up patient PAT-001's full chart including SSN and phone.
What Jira projects do we have?        # → denied for the clinician
```

Switch personas by exiting the session (type `exit` or press Ctrl+C) and reconnecting:

```bash
python connect_persona.py --persona auditor
python connect_persona.py --persona dev
python connect_persona.py --persona public
```

Reconnect to the same session if you get disconnected (session ID is printed on exit):

```bash
python connect_persona.py --persona clinician --session <session-id>
```

Tips for interactive mode:
- **Warm up each persona first** (Section 2) so the tool catalog is fully loaded — avoids cold-start lag on the first live prompt.
- **Two terminals side by side** (e.g., clinician + auditor) makes a strong visual — ask the same "show me PAT-001's SSN" in both to contrast the guardrail outcome live.
- If the agent asks a clarifying question, just answer it in the same session — it makes the demo feel natural.

Use the prompts in Section 3 as your script, typed naturally into the live session.

## 7. Fallback — Explicit Tool Prompts

If a natural-language prompt makes the agent ask for clarification instead of acting, use the explicit version, e.g.:

```bash
python connect_persona.py --persona dev --prompt "Use the get_patient tool to retrieve patient PAT-001 and show their SSN. If you don't have that tool, list the tools you do have."
```

## 8. Raw Gateway Denial (for skeptics)

To prove the denial is deterministic policy enforcement (not model choice), the gateway returns this for a disallowed tool call:

> `"Tool Execution Denied: Tool call not allowed due to policy enforcement [No policy applies to the request (denied by default)]"`

---

## Verify governance (allow + deny matrix)

Run the full matrix — for every enabled agent × persona, one prompt that should
be **allowed** and one that should be **denied** — and print a PASS/FAIL table:

```bash
./verify-governance.sh                 # all enabled agents (Claude + Devin)
./verify-governance.sh --agent devin   # just Devin
./verify-governance.sh --persona dev   # just one persona
```

It warms the sessions in parallel and uses your `~/.aws/credentials` file (it
clears any stale inherited `AWS_*` env vars first), so make sure the file has
current creds via `aws configure set ...`.

---

## Persona Reference

Governance keys on the persona (the IAM role), so tool access is the same for
both agents. Each persona has one runtime per deployed agent:

| Persona | Runtimes (per agent) | Tools Available |
|---------|----------------------|-----------------|
| clinician | `claude_code_persona_clinician` / `devin_persona_clinician` | GitHub + Medical (PII visible) + Tavily — **Jira denied** |
| dev | `claude_code_persona_dev` / `devin_persona_dev` | GitHub + Jira + Tavily (no medical) |
| auditor | `claude_code_persona_auditor` / `devin_persona_auditor` | Read-only across all; PII filtered |
| public | `claude_code_persona_public` / `devin_persona_public` | Tavily web search only |
