#!/usr/bin/env bash
# Deploy the guardrail response-interceptor Lambda + its execution role.
# The interceptor reads the caller's IAM principal from the gateway request
# context and routes to the clinician (permissive) or restrictive guardrail.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

section "Guardrail interceptor Lambda: ${INTERCEPTOR_LAMBDA_NAME}"

CLIN_ID="$(state_require clinician_guardrail_id 'Run 50_guardrails.sh first.')"
CLIN_VER="$(state_require clinician_guardrail_version)"
REST_ID="$(state_require restrictive_guardrail_id)"
REST_VER="$(state_require restrictive_guardrail_version)"

# --- IAM role ---------------------------------------------------------------
log "Lambda role ${INTERCEPTOR_ROLE_NAME}"
LAMBDA_TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
if aws iam get-role --role-name "${INTERCEPTOR_ROLE_NAME}" >/dev/null 2>&1; then
  LAMBDA_ROLE_ARN="$(aws iam get-role --role-name "${INTERCEPTOR_ROLE_NAME}" --query Role.Arn --output text)"
else
  LAMBDA_ROLE_ARN="$(aws iam create-role --role-name "${INTERCEPTOR_ROLE_NAME}" \
    --assume-role-policy-document "$LAMBDA_TRUST" --query Role.Arn --output text)"
  sleep 8
fi
aws iam attach-role-policy --role-name "${INTERCEPTOR_ROLE_NAME}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole >/dev/null 2>&1 || true
GUARDRAIL_POLICY="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"bedrock:ApplyGuardrail\"],\"Resource\":[\"arn:aws:bedrock:${AWS_REGION}:${AWS_ACCOUNT_ID}:guardrail/*\"]}]}"
aws iam put-role-policy --role-name "${INTERCEPTOR_ROLE_NAME}" \
  --policy-name BedrockGuardrailAccess --policy-document "$GUARDRAIL_POLICY"
ok "Lambda role: ${LAMBDA_ROLE_ARN}"

# --- Package ----------------------------------------------------------------
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cp "${PROJECT_ROOT}/interceptor/guardrails_interceptor.py" "$TMP/guardrails_interceptor.py"
( cd "$TMP" && zip -q function.zip guardrails_interceptor.py )

# Note: AWS_REGION is a reserved Lambda variable (auto-populated), so we only
# set the guardrail ids/versions here.
ENV_VARS="Variables={CLINICIAN_GUARDRAIL_ID=${CLIN_ID},CLINICIAN_GUARDRAIL_VERSION=${CLIN_VER},RESTRICTIVE_GUARDRAIL_ID=${REST_ID},RESTRICTIVE_GUARDRAIL_VERSION=${REST_VER}}"

if aws lambda get-function --function-name "${INTERCEPTOR_LAMBDA_NAME}" --region "$AWS_REGION" >/dev/null 2>&1; then
  log "Updating existing function"
  aws lambda update-function-code --function-name "${INTERCEPTOR_LAMBDA_NAME}" --region "$AWS_REGION" \
    --zip-file "fileb://$TMP/function.zip" >/dev/null
  aws lambda wait function-updated --function-name "${INTERCEPTOR_LAMBDA_NAME}" --region "$AWS_REGION"
  aws lambda update-function-configuration --function-name "${INTERCEPTOR_LAMBDA_NAME}" --region "$AWS_REGION" \
    --handler guardrails_interceptor.lambda_handler --runtime python3.13 \
    --role "${LAMBDA_ROLE_ARN}" --timeout 30 --memory-size 256 \
    --environment "$ENV_VARS" >/dev/null
else
  log "Creating function"
  aws lambda create-function --function-name "${INTERCEPTOR_LAMBDA_NAME}" --region "$AWS_REGION" \
    --runtime python3.13 --handler guardrails_interceptor.lambda_handler \
    --role "${LAMBDA_ROLE_ARN}" --timeout 30 --memory-size 256 \
    --zip-file "fileb://$TMP/function.zip" --environment "$ENV_VARS" >/dev/null
fi
aws lambda wait function-updated --function-name "${INTERCEPTOR_LAMBDA_NAME}" --region "$AWS_REGION"

LAMBDA_ARN="$(aws lambda get-function --function-name "${INTERCEPTOR_LAMBDA_NAME}" --region "$AWS_REGION" \
  --query Configuration.FunctionArn --output text)"
state_set interceptor_lambda_arn "${LAMBDA_ARN}"
ok "Interceptor Lambda: ${LAMBDA_ARN}"
