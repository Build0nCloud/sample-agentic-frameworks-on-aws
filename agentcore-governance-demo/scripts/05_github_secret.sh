#!/usr/bin/env bash
# Store GitHub App credentials in Secrets Manager for the GitHub MCP server.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

section "GitHub App credential -> Secrets Manager (${GITHUB_SECRET_NAME})"

[[ -f "${GITHUB_APP_PRIVATE_KEY_FILE}" ]] || die "Private key file not found: ${GITHUB_APP_PRIVATE_KEY_FILE}"

SECRET_JSON="$(jq -n \
  --arg app_id "${GITHUB_APP_ID}" \
  --arg private_key "$(cat "${GITHUB_APP_PRIVATE_KEY_FILE}")" \
  --arg installation_id "${GITHUB_APP_INSTALLATION_ID}" \
  '{app_id:$app_id, private_key:$private_key, installation_id:$installation_id}')"

if aws secretsmanager describe-secret --secret-id "${GITHUB_SECRET_NAME}" --region "$AWS_REGION" >/dev/null 2>&1; then
  log "Secret exists — updating value"
  aws secretsmanager put-secret-value --secret-id "${GITHUB_SECRET_NAME}" \
    --secret-string "${SECRET_JSON}" --region "$AWS_REGION" >/dev/null
  SECRET_ARN="$(aws secretsmanager describe-secret --secret-id "${GITHUB_SECRET_NAME}" \
    --region "$AWS_REGION" --query ARN --output text)"
else
  log "Creating secret"
  SECRET_ARN="$(aws secretsmanager create-secret --name "${GITHUB_SECRET_NAME}" \
    --description "GitHub App credentials for AgentCore GitHub MCP server" \
    --secret-string "${SECRET_JSON}" --region "$AWS_REGION" --query ARN --output text)"
fi

state_set github_app_secret_arn "${SECRET_ARN}"
ok "Secret ARN: ${SECRET_ARN}"
