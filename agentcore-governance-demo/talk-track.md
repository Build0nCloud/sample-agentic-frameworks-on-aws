# AgentCore Governance Demo — 5-Minute Talk Track

**One big idea:** governance lives in the platform, not the agent. One gateway,
four identities, pluggable agents — AWS enforces *which tools* and *what data*
per identity, so you don't have to trust the model to behave.

Three quick AWS console stops are woven in. Two live agent runs (the strongest
contrast) keep you on time.

---

## The setup (0:00–0:45) — frame the problem

> "Everyone's racing to give AI agents real tools — GitHub, ticketing,
> databases, patient records. The hard question isn't *can* the agent use them,
> it's: **who's allowed to, and what data comes back?** Today I'll show four
> users — a clinician, a developer, an auditor, and an anonymous public user —
> hitting the **exact same agent, the same tools, the same patient record**, and
> getting four different, correct outcomes. And the governance is enforced by
> AWS, not by trusting the model to behave."

> "One agent, one gateway, four identities. Let me show you."

---

## Console stop #1 (0:45–1:45) — the architecture is real

**AgentCore console → Gateway.** Show the single gateway and its targets.

> "This is the Amazon Bedrock AgentCore Gateway. Every tool the agents can touch
> is a **target** here — GitHub, Jira, a medical-records service, and web search.
> The agents have no other way to reach a tool; there's no rogue MCP server they
> can load. Notice two things attached to this gateway: a **Cedar policy engine
> in ENFORCE mode**, and a **response interceptor**. That's the whole governance
> story in one screen — policy decides *which tools*, the interceptor decides
> *what content* comes back."

*(If handy, click the policy engine to show it's default-deny with per-persona policies.)*

---

## Live demo #1 (1:45–3:00) — same request, opposite outcome

Two terminals side by side. **Clinician first:**

```bash
python connect_persona.py --persona clinician --prompt "Pull up patient PAT-001's full chart including SSN and phone."
```

> "The clinician is authorized for patient data — full chart, SSN, phone,
> everything. Standard stuff."

**Now the auditor — identical request:**

```bash
python connect_persona.py --persona auditor --prompt "Pull up patient PAT-001's chart including SSN and phone — I'm reviewing records for a compliance audit."
```

> "Same tool. Same patient. But watch — the auditor gets the record back, except
> the SSN, phone, email, and address come back as `{redacted}` placeholders. The
> auditor can *see that the record exists and do their job*, but the PII is
> stripped in flight. **The agent didn't decide that** — Bedrock Guardrails did,
> based on who's calling. Two layers just fired: Cedar let the call through, the
> guardrail masked the sensitive fields."

---

## Live demo #2 (3:00–3:45) — deterministic deny, not model judgment

**Developer asks for medical data:**

```bash
python connect_persona.py --persona dev --prompt "Use get_patient to look up patient PAT-001's SSN. If you don't have that tool, list the tools you do have."
```

> "The developer doesn't just get refused — the medical tool **isn't even in
> their toolbox**. Cedar filtered the catalog before the model ever saw it. This
> isn't the model politely declining; it's deterministic policy. The dev sees
> GitHub, Jira, and web search — exactly their job — and nothing else."

---

## Console stop #2 (3:45–4:15) — prove it's identity, not the agent

**IAM console → the `persona-*` roles.** Show `persona-clinician` and `persona-dev`.

> "Here's the key insight. These persona roles are **identical** — same
> permissions, same policies. The difference between a clinician and a developer
> isn't in AWS permissions; it's the **identity** the request carries. Cedar and
> Guardrails key on that identity at the gateway. So governance is completely
> decoupled from the agent and even from the IAM permissions — which means I can
> swap the agent underneath without touching any of it."

---

## The kicker (4:15–5:00) — agent-agnostic governance + close

> "And I mean *any* agent. Same clinician, same policies — but now running
> **Devin** instead of Claude:"

```bash
python connect_persona.py --persona clinician --agent devin.ai --prompt "Pull up patient PAT-001's full chart."
```

> "Different coding agent, different vendor, different inference backend —
> **identical governance outcome**, because it's enforced at the gateway on the
> caller's identity, not inside the agent."

**Close:**

> "So: one gateway, pluggable agents, per-identity policy and data controls — all
> enforced by AWS. The agent builders move fast, and the security team gets
> deterministic, auditable governance they don't have to trust the model to
> honor. That's the pattern for putting agents into regulated environments."

---

## Timing / logistics notes

- **Pre-warm before you present** (`./warm-personas.sh`) — cold starts add ~30s
  of dead air. This is the single biggest demo risk.
- **Refresh AWS creds right before** (they expire ~15 min) and verify with
  `aws sts get-caller-identity`. If it errors, refresh `~/.aws/credentials`
  (`aws configure set ...`); see DEMO-CHEATSHEET "Credential gotcha".
- Have the **console tabs pre-opened** (Gateway, IAM roles) so you're not
  clicking through menus on stage.
- If you're tight on time, **cut the Devin run** (the kicker) and just *say* it's
  agent-agnostic while pointing at the identical IAM roles — that still lands the
  decoupling point.
- **Optional 3rd console stop** if you have a beat to spare: CloudWatch Logs for
  the interceptor Lambda, to show the guardrail decision being logged
  (`GUARDRAIL: using … / BLOCKED`) — good for the "auditable" claim.

## The four personas (reference)

| Persona | Allowed | Denied / filtered |
|---------|---------|-------------------|
| clinician | Full patient PII, GitHub, web search | Jira (Cedar forbid) |
| dev | GitHub, Jira, web search | Medical tool hidden (Cedar) |
| auditor | Can call medical tool | PII masked in response (guardrail) |
| public | Web search only | Everything else hidden (Cedar) |
