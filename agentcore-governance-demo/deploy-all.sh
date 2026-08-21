#!/usr/bin/env bash
# ============================================================================
# One-command deployment of the AgentCore Governance Demo into a fresh account.
#
#   cp config.env.example config.env   # fill in Tavily key + GitHub App
#   ./deploy-all.sh
#
# Each step is idempotent and records generated ids in .deployed-state.json, so
# re-running after a failure resumes cleanly. You can also run any single step
# from scripts/ on its own.
# ============================================================================
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source lib/common.sh

STEPS=(
  00_preflight
  05_github_secret
  10_infra
  20_dynamodb
  30_mcp_runtimes
  40_gateway
  45_targets
  50_guardrails
  55_interceptor
  60_policies
  65_attach_gateway
  68_agent_image
  70_personas
)

START=$(date +%s)
for step in "${STEPS[@]}"; do
  bash "scripts/${step}.sh"
done
END=$(date +%s)

section "Deployment complete in $(( (END-START)/60 ))m $(( (END-START)%60 ))s"
cat <<EOF

Gateway URL : $(state_get gateway_url)
Policy engine: $(state_get policy_engine_id)
Personas    : clinician / dev / auditor / public

Warm up each persona (first call loads the tool catalog):
  for p in clinician dev auditor public; do
    python connect_persona.py --persona \$p --cmd "echo ready"
  done

Try the demo (see DEMO-CHEATSHEET in the parent project for the full script):
  python connect_persona.py --persona clinician --prompt "Pull up patient PAT-001's full chart including SSN and phone."
  python connect_persona.py --persona dev       --prompt "List your MCP tools."
  python connect_persona.py --persona auditor   --prompt "Get patient PAT-001."
  python connect_persona.py --persona public    --prompt "Search the web for AWS AgentCore pricing."
EOF
