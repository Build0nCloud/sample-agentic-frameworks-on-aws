#!/usr/bin/env bash
# ============================================================================
# Tear down everything the demo created, in reverse dependency order.
# Best-effort: each deletion is guarded so a partial deploy still cleans up.
# Reads resource ids from .deployed-state.json.
#
#   ./cleanup-all.sh            # prompts for confirmation
#   ./cleanup-all.sh --yes      # no prompt
#   ./cleanup-all.sh --yes --delete-bucket   # also empty+delete the S3 bucket
# ============================================================================
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source lib/common.sh

ASSUME_YES=0; DELETE_BUCKET=0
for a in "$@"; do
  case "$a" in
    --yes) ASSUME_YES=1 ;;
    --delete-bucket) DELETE_BUCKET=1 ;;
  esac
done

section "Governance Demo teardown (account ${AWS_ACCOUNT_ID}, region ${AWS_REGION})"
if [[ "$ASSUME_YES" != 1 ]]; then
  read -r -p "This will DELETE the demo's gateway, runtimes, guardrails, policies, roles, table, etc. Continue? [y/N] " ans
  [[ "$ans" == [yY] ]] || { echo "Aborted."; exit 0; }
fi

R="--region $AWS_REGION"

delete_role() {
  local role="$1"
  aws iam get-role --role-name "$role" >/dev/null 2>&1 || return 0
  for p in $(aws iam list-role-policies --role-name "$role" --query 'PolicyNames[]' --output text 2>/dev/null); do
    aws iam delete-role-policy --role-name "$role" --policy-name "$p" >/dev/null 2>&1 || true
  done
  for arn in $(aws iam list-attached-role-policies --role-name "$role" --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
    aws iam detach-role-policy --role-name "$role" --policy-arn "$arn" >/dev/null 2>&1 || true
  done
  aws iam delete-role --role-name "$role" >/dev/null 2>&1 && ok "deleted role $role" || true
}

# --- Personas: runtimes + roles ---------------------------------------------
section "Personas"
for persona in "${PERSONAS[@]}"; do
  rid="$(state_get "persona_runtime_${persona}_id")"
  [[ -n "$rid" ]] && aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$rid" $R >/dev/null 2>&1 \
    && ok "deleted runtime ${persona} ($rid)" || true
  delete_role "${PERSONA_ROLE_PREFIX}${persona}"
done

# --- Gateway: targets, gateway, role ----------------------------------------
section "Gateway"
GATEWAY_ID="$(state_get gateway_id)"
if [[ -n "$GATEWAY_ID" ]]; then
  for tid in $(aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$GATEWAY_ID" $R \
                --query 'items[].targetId' --output text 2>/dev/null); do
    aws bedrock-agentcore-control delete-gateway-target --gateway-identifier "$GATEWAY_ID" --target-id "$tid" $R >/dev/null 2>&1 \
      && ok "deleted target $tid" || true
  done
  aws bedrock-agentcore-control delete-gateway --gateway-identifier "$GATEWAY_ID" $R >/dev/null 2>&1 \
    && ok "deleted gateway $GATEWAY_ID" || true
fi
delete_role "${GATEWAY_ROLE_NAME}"

# --- Policy engine + policies -----------------------------------------------
section "Cedar policy engine"
PE_ID="$(state_get policy_engine_id)"
if [[ -n "$PE_ID" ]]; then
  for pid in $(aws bedrock-agentcore-control list-policies --policy-engine-id "$PE_ID" $R \
                --query 'policies[].policyId' --output text 2>/dev/null); do
    aws bedrock-agentcore-control delete-policy --policy-engine-id "$PE_ID" --policy-id "$pid" $R >/dev/null 2>&1 \
      && ok "deleted policy $pid" || true
  done
  aws bedrock-agentcore-control delete-policy-engine --policy-engine-id "$PE_ID" $R >/dev/null 2>&1 \
    && ok "deleted policy engine $PE_ID" || true
fi

# --- Interceptor Lambda + role ----------------------------------------------
section "Interceptor Lambda"
aws lambda delete-function --function-name "${INTERCEPTOR_LAMBDA_NAME}" $R >/dev/null 2>&1 \
  && ok "deleted lambda ${INTERCEPTOR_LAMBDA_NAME}" || true
delete_role "${INTERCEPTOR_ROLE_NAME}"

# --- Guardrails -------------------------------------------------------------
section "Guardrails"
for key in clinician_guardrail_id restrictive_guardrail_id; do
  gid="$(state_get "$key")"
  [[ -n "$gid" ]] && aws bedrock delete-guardrail --guardrail-identifier "$gid" $R >/dev/null 2>&1 \
    && ok "deleted guardrail $gid" || true
done

# --- MCP server runtimes + roles + ECR --------------------------------------
section "MCP server runtimes"
declare -A MCP=( [github_runtime_id]="agentcore-github-mcp-role:${GITHUB_ECR_REPO}"
                 [jira_runtime_id]="agentcore-jira-mock-role:${JIRA_ECR_REPO}"
                 [medical_runtime_id]="agentcore-dynamodb-mcp-role:${MEDICAL_ECR_REPO}" )
for key in "${!MCP[@]}"; do
  rid="$(state_get "$key")"
  role="${MCP[$key]%%:*}"; repo="${MCP[$key]##*:}"
  [[ -n "$rid" ]] && aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$rid" $R >/dev/null 2>&1 \
    && ok "deleted runtime $rid" || true
  delete_role "$role"
  aws ecr delete-repository --repository-name "$repo" --force $R >/dev/null 2>&1 && ok "deleted ECR $repo" || true
done
aws ecr delete-repository --repository-name "${AGENT_ECR_REPO}" --force $R >/dev/null 2>&1 && ok "deleted ECR ${AGENT_ECR_REPO}" || true

# --- DynamoDB table ---------------------------------------------------------
section "DynamoDB"
aws dynamodb delete-table --table-name "${MEDICAL_TABLE_NAME}" $R >/dev/null 2>&1 \
  && ok "deleted table ${MEDICAL_TABLE_NAME}" || true

# --- GitHub secret ----------------------------------------------------------
section "Secrets Manager"
aws secretsmanager delete-secret --secret-id "${GITHUB_SECRET_NAME}" --force-delete-without-recovery $R >/dev/null 2>&1 \
  && ok "deleted secret ${GITHUB_SECRET_NAME}" || true

# --- Infra (CloudFormation) -------------------------------------------------
section "Infrastructure"
if [[ "$DELETE_BUCKET" == 1 ]]; then
  aws s3 rm "s3://${INFRA_BUCKET}" --recursive $R >/dev/null 2>&1 || true
  aws s3api delete-bucket --bucket "${INFRA_BUCKET}" $R >/dev/null 2>&1 && ok "deleted bucket ${INFRA_BUCKET}" || true
else
  warn "Keeping S3 bucket ${INFRA_BUCKET} (pass --delete-bucket to remove)."
fi
aws cloudformation delete-stack --stack-name "${INFRA_STACK_NAME}" $R >/dev/null 2>&1 \
  && ok "requested delete of stack ${INFRA_STACK_NAME}" || true

rm -f "${STATE_FILE}"
ok "Teardown complete (state file removed)."
