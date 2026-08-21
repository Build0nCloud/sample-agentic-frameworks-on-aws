#!/usr/bin/env bash
# Create the AgentCore Gateway (IAM auth, MCP) and its execution role.
# The role is granted everything the gateway needs for the full governance stack:
#   - invoke the MCP server runtimes
#   - read/authorize against the Cedar policy engine
#   - run Bedrock guardrail checks
#   - invoke the response-interceptor Lambda
# (Policy engine + interceptor are attached later, in 65_attach_gateway.sh.)
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

section "Gateway: ${GATEWAY_NAME}"

INTERCEPTOR_ARN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${INTERCEPTOR_LAMBDA_NAME}"

GW_TRUST="$(cat <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
"Principal":{"Service":"bedrock-agentcore.amazonaws.com"},"Action":"sts:AssumeRole",
"Condition":{"StringEquals":{"aws:SourceAccount":"${AWS_ACCOUNT_ID}"},
"ArnLike":{"aws:SourceArn":"arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:*"}}}]}
JSON
)"

GW_POLICY="$(cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"InvokeRuntime","Effect":"Allow","Action":["bedrock-agentcore:InvokeAgentRuntime"],
  "Resource":["arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:runtime/*"]},
 {"Sid":"PolicyEngineConfig","Effect":"Allow","Action":["bedrock-agentcore:GetPolicyEngine"],
  "Resource":["arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:policy-engine/*"]},
 {"Sid":"PolicyEngineAuth","Effect":"Allow",
  "Action":["bedrock-agentcore:AuthorizeAction","bedrock-agentcore:PartiallyAuthorizeActions"],
  "Resource":["arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:policy-engine/*",
              "arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:gateway/*"]},
 {"Sid":"GuardrailChecks","Effect":"Allow","Action":["bedrock:InvokeGuardrailChecks"],"Resource":"*"},
 {"Sid":"LambdaInvoke","Effect":"Allow","Action":["lambda:InvokeFunction"],
  "Resource":["${INTERCEPTOR_ARN}"]}
]}
JSON
)"

log "Gateway IAM role ${GATEWAY_ROLE_NAME}"
if aws iam get-role --role-name "${GATEWAY_ROLE_NAME}" >/dev/null 2>&1; then
  GW_ROLE_ARN="$(aws iam get-role --role-name "${GATEWAY_ROLE_NAME}" --query Role.Arn --output text)"
else
  GW_ROLE_ARN="$(aws iam create-role --role-name "${GATEWAY_ROLE_NAME}" \
    --assume-role-policy-document "${GW_TRUST}" --query Role.Arn --output text)"
  sleep 8
fi
aws iam put-role-policy --role-name "${GATEWAY_ROLE_NAME}" \
  --policy-name "AgentCoreGatewayExecution" --policy-document "${GW_POLICY}"
state_set gateway_role_arn "${GW_ROLE_ARN}"
ok "Gateway role: ${GW_ROLE_ARN}"

log "Create gateway"
GATEWAY_ID="$(state_get gateway_id)"
if [[ -z "$GATEWAY_ID" ]]; then
  GATEWAY_ID="$(aws bedrock-agentcore-control list-gateways --region "$AWS_REGION" \
    --query "items[?name=='${GATEWAY_NAME}'].gatewayId | [0]" --output text 2>/dev/null || true)"
  [[ "$GATEWAY_ID" == "None" ]] && GATEWAY_ID=""
fi

if [[ -n "$GATEWAY_ID" ]] && aws bedrock-agentcore-control get-gateway --gateway-identifier "$GATEWAY_ID" --region "$AWS_REGION" >/dev/null 2>&1; then
  ok "Gateway already exists: ${GATEWAY_ID}"
  GW="$(aws bedrock-agentcore-control get-gateway --gateway-identifier "$GATEWAY_ID" --region "$AWS_REGION")"
else
  GW="$(aws bedrock-agentcore-control create-gateway --name "${GATEWAY_NAME}" --region "$AWS_REGION" \
    --description "Governed MCP gateway (IAM auth, Cedar policy engine, guardrail interceptor)" \
    --role-arn "${GW_ROLE_ARN}" --protocol-type MCP --authorizer-type AWS_IAM \
    --exception-level DEBUG)"
  GATEWAY_ID="$(echo "$GW" | jq -r '.gatewayId')"
fi

GATEWAY_ARN="$(echo "$GW" | jq -r '.gatewayArn')"
GATEWAY_URL_BASE="$(echo "$GW" | jq -r '.gatewayUrl')"
# MCP calls use the /mcp path.
GATEWAY_URL="${GATEWAY_URL_BASE%/}/mcp"

state_set gateway_id "${GATEWAY_ID}"
state_set gateway_arn "${GATEWAY_ARN}"
state_set gateway_url "${GATEWAY_URL}"
ok "Gateway ${GATEWAY_ID}"
ok "URL ${GATEWAY_URL}"
