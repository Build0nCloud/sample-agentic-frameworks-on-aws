# AgentCore Governance Demo — Cheat Sheet

## 1. Environment Setup (run once per shell)

```bash
# Go to project root
cd /Users/smithzgg/GitHub/agentcore-samples/01-features/02-host-your-agent/01-runtime/04-coding-agents/03-code-agents-competition-e2e

# Activate Python environment
source .venv/bin/activate

# Refresh AWS credentials — paste your Isengard export lines, then persist:
aws configure set aws_access_key_id $AWS_ACCESS_KEY_ID
aws configure set aws_secret_access_key $AWS_SECRET_ACCESS_KEY
aws configure set aws_session_token $AWS_SESSION_TOKEN

# Verify auth (should show account 211395677819)
aws sts get-caller-identity

# Move into the demo folder
cd coding_agents/claude-code
```

## 2. Warm Up (do before the audience is watching)

First cold call to each persona can show partial tools. Warm all four:

```bash
python connect_persona.py --persona clinician --cmd "echo ready"
python connect_persona.py --persona dev --cmd "echo ready"
python connect_persona.py --persona auditor --cmd "echo ready"
python connect_persona.py --persona public --cmd "echo ready"
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
# Log into the agent's terminal as a persona
python connect_persona.py --persona clinician
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

## Persona Reference

| Persona | Runtime | Tools Available |
|---------|---------|-----------------|
| clinician | `claude_code_persona_clinician` | GitHub + Medical (PII visible) + Tavily — **Jira denied** |
| dev | `claude_code_persona_dev` | GitHub + Jira + Tavily (no medical) |
| auditor | `claude_code_persona_auditor` | Read-only across all; PII filtered |
| public | `claude_code_persona_public` | Tavily web search only |
