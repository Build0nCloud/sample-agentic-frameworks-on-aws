#!/usr/bin/env bash
# Preflight: verify tooling, credentials, and required config before deploying.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

section "Preflight checks"

log "Required tooling"
require_cmd aws
require_cmd jq
require_cmd finch      # container builds use finch (Docker-compatible CLI)
require_cmd python3
require_cmd node
require_cmd npm
ok "aws, jq, finch, python3, node, npm present"

log "AWS identity"
CALLER="$(aws sts get-caller-identity --output json)"
echo "$CALLER" | jq -r '"    Account: \(.Account)\n    Arn:     \(.Arn)"'
ok "Region: ${AWS_REGION}  Account: ${AWS_ACCOUNT_ID}"

log "Required configuration (config.env)"
[[ -f "${PROJECT_ROOT}/config.env" ]] || die "config.env not found. Run: cp config.env.example config.env  and fill it in."

[[ -n "${TAVILY_API_KEY:-}" && "${TAVILY_API_KEY}" != tvly-xxxx* ]] \
  || die "TAVILY_API_KEY is not set in config.env."
ok "TAVILY_API_KEY set"

[[ -n "${GITHUB_APP_ID:-}" ]] || die "GITHUB_APP_ID not set in config.env."
[[ -n "${GITHUB_APP_INSTALLATION_ID:-}" ]] || die "GITHUB_APP_INSTALLATION_ID not set in config.env."
[[ -n "${GITHUB_APP_PRIVATE_KEY_FILE:-}" && -f "${GITHUB_APP_PRIVATE_KEY_FILE}" ]] \
  || die "GITHUB_APP_PRIVATE_KEY_FILE not set or file not found in config.env."
ok "GitHub App credentials present"

log "Bedrock model access (informational)"
if aws bedrock list-foundation-models --region "$AWS_REGION" \
      --query "modelSummaries[?contains(modelId,'claude')] | length(@)" --output text >/dev/null 2>&1; then
  ok "bedrock:ListFoundationModels reachable"
else
  warn "Could not list Bedrock models. Ensure Claude models are enabled in this region."
fi

ok "Preflight passed"
