#!/usr/bin/env bash
# Build + push the three self-hosted MCP servers and deploy them as AgentCore
# runtimes (MCP protocol, PUBLIC network):
#   - GitHubMCP       (github)   -> reads GitHub App secret from Secrets Manager
#   - JiraMock        (jira)     -> in-memory mock data
#   - MedicalRecords  (dynamodb) -> reads the medical-records-demo table
#
# The Tavily target is an external hosted MCP server (no runtime needed).
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

section "MCP server runtimes (build, push, deploy)"

log "Logging finch into ECR"
aws ecr get-login-password --region "$AWS_REGION" \
  | finch login --username AWS --password-stdin "${ECR_REGISTRY}"

# --- runtime trust policy (shared) -----------------------------------------
runtime_trust_policy() {
  cat <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
"Principal":{"Service":"bedrock-agentcore.amazonaws.com"},"Action":"sts:AssumeRole",
"Condition":{"StringEquals":{"aws:SourceAccount":"${AWS_ACCOUNT_ID}"},
"ArnLike":{"aws:SourceArn":"arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:*"}}}]}
JSON
}

# Base execution permissions common to every MCP runtime (ECR, logs, xray, metrics).
base_statements() {
  local repo="$1"
  cat <<JSON
{"Sid":"ECRImage","Effect":"Allow","Action":["ecr:BatchGetImage","ecr:GetDownloadUrlForLayer"],
 "Resource":["arn:aws:ecr:${AWS_REGION}:${AWS_ACCOUNT_ID}:repository/${repo}"]},
{"Sid":"ECRToken","Effect":"Allow","Action":["ecr:GetAuthorizationToken"],"Resource":"*"},
{"Sid":"LogsGroup","Effect":"Allow","Action":["logs:CreateLogGroup","logs:DescribeLogStreams"],
 "Resource":["arn:aws:logs:${AWS_REGION}:${AWS_ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/runtimes/*"]},
{"Sid":"LogsDescribe","Effect":"Allow","Action":["logs:DescribeLogGroups"],
 "Resource":["arn:aws:logs:${AWS_REGION}:${AWS_ACCOUNT_ID}:log-group:*"]},
{"Sid":"LogsWrite","Effect":"Allow","Action":["logs:CreateLogStream","logs:PutLogEvents"],
 "Resource":["arn:aws:logs:${AWS_REGION}:${AWS_ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"]},
{"Sid":"XRay","Effect":"Allow","Action":["xray:PutTraceSegments","xray:PutTelemetryRecords","xray:GetSamplingRules","xray:GetSamplingTargets"],"Resource":"*"},
{"Sid":"Metrics","Effect":"Allow","Action":"cloudwatch:PutMetricData","Resource":"*",
 "Condition":{"StringEquals":{"cloudwatch:namespace":"bedrock-agentcore"}}}
JSON
}

# deploy_mcp_runtime NAME ECR_REPO CONTEXT_DIR ROLE_NAME EXTRA_STMTS_JSON ENV_JSON STATE_KEY
deploy_mcp_runtime() {
  local name="$1" repo="$2" ctx="$3" role_name="$4" extra_stmts="$5" env_json="$6" state_key="$7"
  local image_uri="${ECR_REGISTRY}/${repo}:latest"

  log "[$name] ECR repo"
  aws ecr describe-repositories --repository-names "$repo" --region "$AWS_REGION" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "$repo" --region "$AWS_REGION" \
         --image-scanning-configuration scanOnPush=true >/dev/null

  log "[$name] build (arm64) + push -> ${image_uri}"
  finch build --platform linux/arm64 -t "${repo}:latest" "$ctx"
  finch tag "${repo}:latest" "${image_uri}"
  finch push "${image_uri}"

  log "[$name] IAM role ${role_name}"
  local role_arn
  if aws iam get-role --role-name "$role_name" >/dev/null 2>&1; then
    role_arn="$(aws iam get-role --role-name "$role_name" --query Role.Arn --output text)"
  else
    role_arn="$(aws iam create-role --role-name "$role_name" \
      --assume-role-policy-document "$(runtime_trust_policy)" --query Role.Arn --output text)"
    sleep 8
  fi
  local policy_doc="{\"Version\":\"2012-10-17\",\"Statement\":[$(base_statements "$repo")${extra_stmts:+,$extra_stmts}]}"
  aws iam put-role-policy --role-name "$role_name" \
    --policy-name "AgentCoreRuntimeExecution" --policy-document "$policy_doc"

  log "[$name] AgentCore runtime"
  local rid; rid="$(state_get "$state_key")"
  if [[ -z "$rid" ]]; then
    rid="$(aws bedrock-agentcore-control list-agent-runtimes --region "$AWS_REGION" \
      --query "agentRuntimes[?agentRuntimeName=='${name}'].agentRuntimeId | [0]" --output text 2>/dev/null || true)"
    [[ "$rid" == "None" ]] && rid=""
  fi

  local artifact="{\"containerConfiguration\":{\"containerUri\":\"${image_uri}\"}}"
  local network='{"networkMode":"PUBLIC"}'
  local protocol='{"serverProtocol":"MCP"}'
  local lifecycle="{\"idleRuntimeSessionTimeout\":${MCP_RUNTIME_IDLE_TIMEOUT},\"maxLifetime\":${MCP_RUNTIME_MAX_LIFETIME}}"

  if [[ -n "$rid" ]] && aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$rid" --region "$AWS_REGION" >/dev/null 2>&1; then
    log "[$name] updating existing runtime $rid"
    aws bedrock-agentcore-control update-agent-runtime --agent-runtime-id "$rid" --region "$AWS_REGION" \
      --agent-runtime-artifact "$artifact" --role-arn "$role_arn" \
      --network-configuration "$network" --protocol-configuration "$protocol" \
      --lifecycle-configuration "$lifecycle" \
      ${env_json:+--environment-variables "$env_json"} >/dev/null
  else
    log "[$name] creating runtime"
    # Newly-created execution roles can take longer than the fixed sleep above to
    # propagate; AgentCore validates ECR access at create time and returns a
    # ValidationException until the role is usable. Retry that transient.
    local attempt create_err
    for attempt in 1 2 3 4 5 6; do
      if rid="$(aws bedrock-agentcore-control create-agent-runtime --agent-runtime-name "$name" --region "$AWS_REGION" \
        --agent-runtime-artifact "$artifact" --role-arn "$role_arn" \
        --network-configuration "$network" --protocol-configuration "$protocol" \
        --lifecycle-configuration "$lifecycle" \
        ${env_json:+--environment-variables "$env_json"} \
        --query agentRuntimeId --output text 2>/tmp/mcp_create_err)"; then
        break
      fi
      create_err="$(cat /tmp/mcp_create_err)"
      if echo "$create_err" | grep -q "Access denied while validating ECR URI"; then
        warn "[$name] role not yet propagated (attempt $attempt) — retrying in 15s"
        sleep 15
      else
        die "[$name] create-agent-runtime failed: $create_err"
      fi
    done
    [[ -n "$rid" && "$rid" != "None" ]] || die "[$name] runtime not created after retries: ${create_err:-unknown error}"
  fi
  wait_runtime_ready "$rid"
  state_set "$state_key" "$rid"
  ok "[$name] runtime READY: $rid"
}

# --- GitHub MCP -------------------------------------------------------------
SECRET_ARN="$(state_require github_app_secret_arn 'Run 05_github_secret.sh first.')"
GH_STMTS="{\"Sid\":\"SecretsManager\",\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":[\"${SECRET_ARN}\"]}"
deploy_mcp_runtime "${GITHUB_RUNTIME_NAME}" "${GITHUB_ECR_REPO}" \
  "${PROJECT_ROOT}/mcp-servers/github" "agentcore-github-mcp-role" \
  "${GH_STMTS}" "GITHUB_APP_SECRET_ARN=${SECRET_ARN}" "github_runtime_id"

# --- Jira Mock --------------------------------------------------------------
deploy_mcp_runtime "${JIRA_RUNTIME_NAME}" "${JIRA_ECR_REPO}" \
  "${PROJECT_ROOT}/mcp-servers/jira-mock" "agentcore-jira-mock-role" \
  "" "" "jira_runtime_id"

# --- Medical Records (DynamoDB) --------------------------------------------
DDB_STMTS="{\"Sid\":\"DynamoDB\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:PutItem\",\"dynamodb:UpdateItem\",\"dynamodb:DeleteItem\",\"dynamodb:Scan\",\"dynamodb:Query\"],\"Resource\":[\"arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${MEDICAL_TABLE_NAME}\"]}"
deploy_mcp_runtime "${MEDICAL_RUNTIME_NAME}" "${MEDICAL_ECR_REPO}" \
  "${PROJECT_ROOT}/mcp-servers/dynamodb" "agentcore-dynamodb-mcp-role" \
  "${DDB_STMTS}" "DYNAMODB_TABLE=${MEDICAL_TABLE_NAME}" "medical_runtime_id"

ok "All MCP server runtimes deployed"
