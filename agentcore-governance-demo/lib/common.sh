#!/usr/bin/env bash
# ============================================================================
# Shared library for the AgentCore Governance Demo deployment.
#
# Every deploy/cleanup script sources this file. It provides:
#   - config loading (config.env) + AWS account/region resolution
#   - stable resource naming (overridable via config.env / environment)
#   - a JSON state file (.deployed-state.json) with get/set helpers
#   - logging helpers
#
# Nothing here is account-specific: the account id and all generated resource
# ids (gateway id, runtime ids, guardrail ids, arns) are resolved at run time
# and persisted to the state file. This is what makes the project portable to
# a fresh AWS account.
# ============================================================================
set -euo pipefail

# --- Paths -----------------------------------------------------------------
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${LIB_DIR}/.." && pwd)"
export PROJECT_ROOT
export STATE_FILE="${PROJECT_ROOT}/.deployed-state.json"

# --- Load user config (config.env) -----------------------------------------
# config.env is created by the user from config.env.example and is git-ignored.
if [[ -f "${PROJECT_ROOT}/config.env" ]]; then
  # shellcheck disable=SC1091
  set -a; source "${PROJECT_ROOT}/config.env"; set +a
fi

# --- AWS account / region ---------------------------------------------------
export AWS_PAGER=""   # never page CLI output in scripts
export AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo "us-west-2")}"
export AWS_DEFAULT_REGION="${AWS_REGION}"

resolve_account_id() {
  if [[ -z "${AWS_ACCOUNT_ID:-}" ]]; then
    AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
    export AWS_ACCOUNT_ID
  fi
}
resolve_account_id

# --- Stable resource names (override in config.env if desired) --------------
# Target names and persona role names are referenced by Cedar action ids and by
# the agent's baked-in settings.json, so keep them stable unless you rebuild the
# agent image and rewrite the Cedar policies to match.
export GATEWAY_NAME="${GATEWAY_NAME:-governance-mcp-gateway}"
export GATEWAY_ROLE_NAME="${GATEWAY_ROLE_NAME:-governance-mcp-gateway-role}"
export POLICY_ENGINE_NAME="${POLICY_ENGINE_NAME:-GatewayDemoPolicy}"

export TARGET_GITHUB="${TARGET_GITHUB:-GitHubMCP}"
export TARGET_JIRA="${TARGET_JIRA:-JiraMock}"
export TARGET_MEDICAL="${TARGET_MEDICAL:-MedicalRecords}"
export TARGET_TAVILY="${TARGET_TAVILY:-TavilyWebSearch}"

export GITHUB_RUNTIME_NAME="${GITHUB_RUNTIME_NAME:-github_mcp_runtime}"
export JIRA_RUNTIME_NAME="${JIRA_RUNTIME_NAME:-jira_mock_mcp_runtime}"
export MEDICAL_RUNTIME_NAME="${MEDICAL_RUNTIME_NAME:-dynamodb_mcp_runtime}"

export GITHUB_ECR_REPO="${GITHUB_ECR_REPO:-github-mcp}"
export JIRA_ECR_REPO="${JIRA_ECR_REPO:-jira-mock-mcp}"
export MEDICAL_ECR_REPO="${MEDICAL_ECR_REPO:-dynamodb-mcp}"
export AGENT_ECR_REPO="${AGENT_ECR_REPO:-coding-agents-claude-code}"

export MEDICAL_TABLE_NAME="${MEDICAL_TABLE_NAME:-medical-records-demo}"

export GITHUB_SECRET_NAME="${GITHUB_SECRET_NAME:-agentcore/github-mcp/github-app}"

export CLINICIAN_GUARDRAIL_NAME="${CLINICIAN_GUARDRAIL_NAME:-clinician-guardrail}"
export RESTRICTIVE_GUARDRAIL_NAME="${RESTRICTIVE_GUARDRAIL_NAME:-restrictive-guardrail}"

export INTERCEPTOR_LAMBDA_NAME="${INTERCEPTOR_LAMBDA_NAME:-governance-guardrails-interceptor}"
export INTERCEPTOR_ROLE_NAME="${INTERCEPTOR_ROLE_NAME:-gateway-interceptor-lambda-role}"

# Personas: keep names stable (Cedar principals reference persona-<name>)
export PERSONAS=(clinician dev auditor public)
export PERSONA_ROLE_PREFIX="${PERSONA_ROLE_PREFIX:-persona-}"
export PERSONA_RUNTIME_PREFIX="${PERSONA_RUNTIME_PREFIX:-claude_code_persona_}"

# Infra (shared VPC + S3 Files)
export INFRA_STACK_NAME="${INFRA_STACK_NAME:-coding-agents-infra}"
export INFRA_BUCKET="${INFRA_BUCKET:-coding-agents-${AWS_ACCOUNT_ID}-${AWS_REGION}}"

# MCP runtime tuning
export MCP_RUNTIME_IDLE_TIMEOUT="${MCP_RUNTIME_IDLE_TIMEOUT:-600}"
export MCP_RUNTIME_MAX_LIFETIME="${MCP_RUNTIME_MAX_LIFETIME:-3300}"
# Persona runtime tuning (longer sessions for live interactive demo)
export PERSONA_RUNTIME_IDLE_TIMEOUT="${PERSONA_RUNTIME_IDLE_TIMEOUT:-900}"
export PERSONA_RUNTIME_MAX_LIFETIME="${PERSONA_RUNTIME_MAX_LIFETIME:-28800}"

export AGENT_MODEL="${AGENT_MODEL:-us.anthropic.claude-opus-4-6-v1}"

# --- Derived values ---------------------------------------------------------
export ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
export GATEWAY_ARN_PREFIX="arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}"

# --- Logging ----------------------------------------------------------------
log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; }
die()  { err "$*"; exit 1; }
section() { printf '\n\033[1;36m==== %s ====\033[0m\n' "$*"; }

# --- State file helpers -----------------------------------------------------
_state_init() { [[ -f "$STATE_FILE" ]] || echo '{}' > "$STATE_FILE"; }

state_get() {
  # state_get KEY -> prints value or empty
  _state_init
  jq -r --arg k "$1" '.[$k] // empty' "$STATE_FILE"
}

state_set() {
  # state_set KEY VALUE
  _state_init
  local tmp; tmp="$(jq --arg k "$1" --arg v "$2" '.[$k]=$v' "$STATE_FILE")"
  printf '%s\n' "$tmp" > "$STATE_FILE"
}

state_require() {
  # state_require KEY human-friendly-hint
  local v; v="$(state_get "$1")"
  [[ -n "$v" ]] || die "Missing '$1' in state. ${2:-Run the prerequisite step first.}"
  printf '%s' "$v"
}

# --- Small utilities --------------------------------------------------------
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"; }

# Wait for an AgentCore runtime to reach READY.
wait_runtime_ready() {
  local rid="$1" tries="${2:-60}" i status
  for ((i=1; i<=tries; i++)); do
    status="$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$rid" \
                --region "$AWS_REGION" --query status --output text 2>/dev/null || echo PENDING)"
    case "$status" in
      READY) return 0 ;;
      CREATE_FAILED|UPDATE_FAILED) die "Runtime $rid entered $status" ;;
    esac
    sleep 10
  done
  die "Timed out waiting for runtime $rid to become READY"
}
