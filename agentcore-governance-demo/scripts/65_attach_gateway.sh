#!/usr/bin/env bash
# Attach the Cedar policy engine (ENFORCE) and the guardrail response interceptor
# to the gateway. This is what turns on the governance: tool discovery/calls are
# authorized by Cedar, and tool responses are filtered by the per-persona guardrail.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

section "Attach policy engine + interceptor to gateway"

GATEWAY_ID="$(state_require gateway_id)"
GW_ROLE_ARN="$(state_require gateway_role_arn)"
PE_ARN="$(state_require policy_engine_arn 'Run 60_policies.sh first.')"
LAMBDA_ARN="$(state_require interceptor_lambda_arn 'Run 55_interceptor.sh first.')"

POLICY_ENGINE_CONFIG="$(jq -n --arg arn "$PE_ARN" '{arn:$arn, mode:"ENFORCE"}')"
INTERCEPTOR_CONFIG="$(jq -n --arg arn "$LAMBDA_ARN" \
  '[{interceptor:{lambda:{arn:$arn}}, interceptionPoints:["RESPONSE"], inputConfiguration:{passRequestHeaders:true}}]')"

log "update-gateway"
aws bedrock-agentcore-control update-gateway --gateway-identifier "$GATEWAY_ID" --region "$AWS_REGION" \
  --name "${GATEWAY_NAME}" --role-arn "$GW_ROLE_ARN" \
  --protocol-type MCP --authorizer-type AWS_IAM --exception-level DEBUG \
  --policy-engine-configuration "$POLICY_ENGINE_CONFIG" \
  --interceptor-configurations "$INTERCEPTOR_CONFIG" >/dev/null

ok "Gateway now enforcing Cedar policies + guardrail interceptor"
