#!/usr/bin/env bash
# Create the Cedar policy engine and the 7 per-persona policies (default-deny).
# Principals are the persona assumed-role ARNs; the resource is the gateway.
# Action ids are "<TargetName>" (whole target) or "<TargetName>___<tool>".
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

section "Cedar policy engine + policies"

GATEWAY_ARN="$(state_require gateway_arn 'Run 40_gateway.sh first.')"

arn_for() { echo "arn:aws:sts::${AWS_ACCOUNT_ID}:assumed-role/${PERSONA_ROLE_PREFIX}$1"; }
CLIN="$(arn_for clinician)"; DEV="$(arn_for dev)"; AUD="$(arn_for auditor)"; PUB="$(arn_for public)"

# --- Policy engine ----------------------------------------------------------
PE_ID="$(state_get policy_engine_id)"
if [[ -z "$PE_ID" ]]; then
  PE_ID="$(aws bedrock-agentcore-control list-policy-engines --region "$AWS_REGION" \
    --query "policyEngines[?name=='${POLICY_ENGINE_NAME}'].policyEngineId | [0]" --output text 2>/dev/null || true)"
  [[ "$PE_ID" == "None" ]] && PE_ID=""
fi
if [[ -z "$PE_ID" ]]; then
  log "Creating policy engine ${POLICY_ENGINE_NAME}"
  PE_ID="$(aws bedrock-agentcore-control create-policy-engine --region "$AWS_REGION" \
    --name "${POLICY_ENGINE_NAME}" \
    --description "Cedar policy engine for persona-based access control demo" \
    --query policyEngineId --output text)"
else
  ok "Policy engine exists: ${PE_ID}"
fi
PE_ARN="$(aws bedrock-agentcore-control get-policy-engine --policy-engine-id "$PE_ID" \
  --region "$AWS_REGION" --query policyEngineArn --output text)"
state_set policy_engine_id "${PE_ID}"
state_set policy_engine_arn "${PE_ARN}"

# A freshly-created policy engine is CREATING for a few seconds; CreatePolicy
# fails with ConflictException until it reaches ACTIVE. Wait for it.
log "Waiting for policy engine to become ACTIVE"
for _ in $(seq 1 30); do
  pe_status="$(aws bedrock-agentcore-control get-policy-engine --policy-engine-id "$PE_ID" \
    --region "$AWS_REGION" --query status --output text 2>/dev/null || echo CREATING)"
  case "$pe_status" in
    ACTIVE) break ;;
    CREATE_FAILED|FAILED) die "Policy engine $PE_ID entered $pe_status" ;;
  esac
  sleep 5
done
[[ "$pe_status" == "ACTIVE" ]] || die "Timed out waiting for policy engine $PE_ID to become ACTIVE"
ok "Policy engine ${PE_ID} (ACTIVE)"

# --- Policies ---------------------------------------------------------------
# create_policy NAME DESC CEDAR_STATEMENT
create_policy() {
  local name="$1" desc="$2" statement="$3"
  local existing
  existing="$(aws bedrock-agentcore-control list-policies --policy-engine-id "$PE_ID" --region "$AWS_REGION" \
    --query "policies[?name=='${name}'].policyId | [0]" --output text 2>/dev/null || true)"
  [[ "$existing" == "None" ]] && existing=""
  local definition; definition="$(jq -n --arg s "$statement" '{cedar:{statement:$s}}')"
  if [[ -n "$existing" ]]; then
    ok "[$name] exists ($existing)"
    return
  fi
  log "[$name] create"
  aws bedrock-agentcore-control create-policy --policy-engine-id "$PE_ID" --region "$AWS_REGION" \
    --name "$name" --description "$desc" --definition "$definition" >/dev/null
  ok "[$name] created"
}

G="$GATEWAY_ARN"

create_policy "ClinicianFullAccess" \
  "Clinician: full access to all tools including medical records" \
  "permit(principal == AgentCore::IamEntity::\"${CLIN}\", action, resource == AgentCore::Gateway::\"${G}\");"

create_policy "ClinicianDenyJira" \
  "Deny clinician access to all Jira tools (forbid overrides ClinicianFullAccess)" \
  "forbid(principal == AgentCore::IamEntity::\"${CLIN}\", action in AgentCore::Action::\"${TARGET_JIRA}\", resource == AgentCore::Gateway::\"${G}\");"

create_policy "DevGitHub" \
  "Dev: allow all GitHub tools" \
  "permit(principal == AgentCore::IamEntity::\"${DEV}\", action in AgentCore::Action::\"${TARGET_GITHUB}\", resource == AgentCore::Gateway::\"${G}\");"

create_policy "DevJira" \
  "Dev: allow all Jira tools" \
  "permit(principal == AgentCore::IamEntity::\"${DEV}\", action in AgentCore::Action::\"${TARGET_JIRA}\", resource == AgentCore::Gateway::\"${G}\");"

create_policy "DevTavily" \
  "Dev: allow all Tavily tools" \
  "permit(principal == AgentCore::IamEntity::\"${DEV}\", action in AgentCore::Action::\"${TARGET_TAVILY}\", resource == AgentCore::Gateway::\"${G}\");"

create_policy "AuditorReadTools" \
  "Auditor: read-only access - only get/list/search tools across all targets" \
  "permit(principal == AgentCore::IamEntity::\"${AUD}\", action in [AgentCore::Action::\"${TARGET_GITHUB}___get_file\", AgentCore::Action::\"${TARGET_GITHUB}___get_issue\", AgentCore::Action::\"${TARGET_GITHUB}___list_files\", AgentCore::Action::\"${TARGET_GITHUB}___list_issue_comments\", AgentCore::Action::\"${TARGET_JIRA}___get_issue\", AgentCore::Action::\"${TARGET_JIRA}___list_projects\", AgentCore::Action::\"${TARGET_JIRA}___search_issues\", AgentCore::Action::\"${TARGET_MEDICAL}___get_patient\", AgentCore::Action::\"${TARGET_MEDICAL}___list_patients\", AgentCore::Action::\"${TARGET_MEDICAL}___search_patients\", AgentCore::Action::\"${TARGET_TAVILY}___tavily_search\"], resource == AgentCore::Gateway::\"${G}\");"

create_policy "PublicSearchOnly" \
  "Public: only Tavily web search allowed" \
  "permit(principal == AgentCore::IamEntity::\"${PUB}\", action == AgentCore::Action::\"${TARGET_TAVILY}___tavily_search\", resource == AgentCore::Gateway::\"${G}\");"

ok "Cedar policies created"
