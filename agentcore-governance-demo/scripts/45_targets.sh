#!/usr/bin/env bash
# Create the 4 gateway targets:
#   GitHubMCP, JiraMock, MedicalRecords  -> AgentCore runtime endpoints
#   TavilyWebSearch                       -> external hosted MCP server
# All targets: listingMode DEFAULT, GATEWAY_IAM_ROLE credentials, and the
# policy-session-id request header allowed (needed by the Cedar policy engine).
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

section "Gateway targets"

GATEWAY_ID="$(state_require gateway_id 'Run 40_gateway.sh first.')"

CRED_CONFIG="$(cat <<JSON
[{"credentialProviderType":"GATEWAY_IAM_ROLE",
  "credentialProvider":{"iamCredentialProvider":{"service":"bedrock-agentcore","region":"${AWS_REGION}"}}}]
JSON
)"
# Note: the policy-session-id header (x-amzn-bedrock-agentcore-policy-session-id)
# is a client->gateway header consumed by the Cedar policy engine AT the gateway.
# It is NOT forwarded to targets, and X-Amzn-* headers are prohibited in a target's
# allowedRequestHeaders, so no metadataConfiguration is needed on the targets.

runtime_endpoint() {
  echo "https://bedrock-agentcore.${AWS_REGION}.amazonaws.com/runtimes/$1/invocations?qualifier=DEFAULT&accountId=${AWS_ACCOUNT_ID}"
}

# upsert_target NAME DESCRIPTION ENDPOINT
upsert_target() {
  local name="$1" desc="$2" endpoint="$3"
  local target_config
  target_config="$(jq -n --arg ep "$endpoint" \
    '{mcp:{mcpServer:{endpoint:$ep, listingMode:"DEFAULT"}}}')"

  local existing_id
  existing_id="$(aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$GATEWAY_ID" \
    --region "$AWS_REGION" --query "items[?name=='${name}'].targetId | [0]" --output text 2>/dev/null || true)"
  [[ "$existing_id" == "None" ]] && existing_id=""

  if [[ -n "$existing_id" ]]; then
    log "[$name] update ($existing_id)"
    aws bedrock-agentcore-control update-gateway-target --gateway-identifier "$GATEWAY_ID" \
      --target-id "$existing_id" --name "$name" --region "$AWS_REGION" \
      --target-configuration "$target_config" \
      --credential-provider-configurations "$CRED_CONFIG" >/dev/null
  else
    log "[$name] create"
    aws bedrock-agentcore-control create-gateway-target --gateway-identifier "$GATEWAY_ID" \
      --name "$name" --region "$AWS_REGION" --description "$desc" \
      --target-configuration "$target_config" \
      --credential-provider-configurations "$CRED_CONFIG" >/dev/null
  fi
  ok "[$name] ready"
}

GH_RID="$(state_require github_runtime_id 'Run 30_mcp_runtimes.sh first.')"
JIRA_RID="$(state_require jira_runtime_id 'Run 30_mcp_runtimes.sh first.')"
MED_RID="$(state_require medical_runtime_id 'Run 30_mcp_runtimes.sh first.')"
[[ -n "${TAVILY_API_KEY:-}" ]] || die "TAVILY_API_KEY not set (config.env)."

upsert_target "${TARGET_GITHUB}"  "GitHub MCP Server on AgentCore Runtime"          "$(runtime_endpoint "$GH_RID")"
upsert_target "${TARGET_JIRA}"    "Mock Jira MCP Server - project and issue management" "$(runtime_endpoint "$JIRA_RID")"
upsert_target "${TARGET_MEDICAL}" "DynamoDB MCP Server - medical patient records with PII" "$(runtime_endpoint "$MED_RID")"
upsert_target "${TARGET_TAVILY}"  "Tavily Web Search MCP - search and extract web content" \
  "https://mcp.tavily.com/mcp/?tavilyApiKey=${TAVILY_API_KEY}"

ok "All 4 targets configured"
