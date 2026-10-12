#!/usr/bin/env bash
# Create the persona IAM roles and the persona AgentCore runtimes.
#
# Governance keys on the persona IAM role, NOT on the agent, so the 4 persona
# roles are shared across all enabled agents. Only the runtimes multiply:
# one runtime per (persona x agent). Each runtime differs by container image
# (the agent) and by the persona role it assumes.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

section "Personas: IAM roles + runtimes (agents: ${AGENTS[*]})"

SUBNET_1="$(state_require infra_subnet_1 'Run 10_infra.sh first.')"
SUBNET_2="$(state_require infra_subnet_2)"
SECURITY_GROUP="$(state_require infra_security_group)"
S3FILES_AP_ARN="$(state_require infra_s3files_ap_arn)"
INFRA_BUCKET="$(state_require infra_bucket)"
GATEWAY_URL="$(state_require gateway_url 'Run 40_gateway.sh first.')"
S3FILES_FS_ARN="${S3FILES_AP_ARN%%/access-point/*}"

persona_trust() {
  cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Principal":{"AWS":"arn:aws:iam::${AWS_ACCOUNT_ID}:root"},"Action":"sts:AssumeRole"},
 {"Effect":"Allow","Principal":{"Service":"bedrock-agentcore.amazonaws.com"},"Action":"sts:AssumeRole"},
 {"Effect":"Allow","Principal":{"Service":"elasticfilesystem.amazonaws.com"},"Action":"sts:AssumeRole",
  "Condition":{"StringEquals":{"aws:SourceAccount":"${AWS_ACCOUNT_ID}"},
  "ArnLike":{"aws:SourceArn":"arn:aws:s3files:${AWS_REGION}:${AWS_ACCOUNT_ID}:file-system/*"}}}]}
JSON
}

# Secret ARNs the persona role may read (one per enabled agent that needs a key).
# Empty resource list would make an IAM statement invalid, so this is only added
# when at least one enabled agent declares a secret.
agent_secret_resources() {
  local first=1 agent slug sname
  for agent in "${AGENTS[@]}"; do
    slug="$(agent_normalize_slug "$agent")"
    sname="$(agent_secret_name "$slug")"
    [[ -z "$sname" ]] && continue
    [[ $first -eq 1 ]] && first=0 || printf ','
    # Wildcard suffix: Secrets Manager appends a random 6-char suffix to ARNs.
    printf '"arn:aws:secretsmanager:%s:%s:secret:%s-*"' "$AWS_REGION" "$AWS_ACCOUNT_ID" "$sname"
  done
}

persona_exec_policy() {
  local secret_resources; secret_resources="$(agent_secret_resources)"
  local secrets_stmt=""
  if [[ -n "$secret_resources" ]]; then
    secrets_stmt=$(cat <<JSON
,
 {"Sid":"AgentSecrets","Effect":"Allow","Action":["secretsmanager:GetSecretValue"],
  "Resource":[${secret_resources}]}
JSON
)
  fi
  cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"Logs","Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogGroups","logs:DescribeLogStreams"],
  "Resource":["arn:aws:logs:${AWS_REGION}:${AWS_ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/*"]},
 {"Sid":"BedrockInvoke","Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream","bedrock:ListInferenceProfiles"],
  "Resource":["arn:aws:bedrock:*::foundation-model/*","arn:aws:bedrock:${AWS_REGION}:${AWS_ACCOUNT_ID}:*"]},
 {"Sid":"ECRAuth","Effect":"Allow","Action":["ecr:GetAuthorizationToken"],"Resource":["*"]},
 {"Sid":"ECRPull","Effect":"Allow","Action":["ecr:BatchGetImage","ecr:GetDownloadUrlForLayer"],
  "Resource":["arn:aws:ecr:${AWS_REGION}:${AWS_ACCOUNT_ID}:repository/coding-agents-*"]},
 {"Sid":"S3Files","Effect":"Allow","Action":["s3files:GetAccessPoint","s3files:GetFileSystem","s3files:GetMountTarget","s3files:DescribeMountTargets","s3files:ListMountTargets","s3files:ClientMount","s3files:ClientWrite","s3files:ClientRootAccess"],
  "Resource":["${S3FILES_AP_ARN}","${S3FILES_FS_ARN}"]},
 {"Sid":"EFS","Effect":"Allow","Action":["elasticfilesystem:ClientMount","elasticfilesystem:ClientWrite","elasticfilesystem:DescribeAccessPoints","elasticfilesystem:DescribeMountTargets"],
  "Resource":["arn:aws:elasticfilesystem:${AWS_REGION}:${AWS_ACCOUNT_ID}:file-system/*","arn:aws:elasticfilesystem:${AWS_REGION}:${AWS_ACCOUNT_ID}:access-point/*"]},
 {"Sid":"S3Bucket","Effect":"Allow","Action":["s3:ListBucket","s3:ListBucketVersions","s3:GetObject*","s3:PutObject*","s3:DeleteObject*","s3:AbortMultipartUpload"],
  "Resource":["arn:aws:s3:::${INFRA_BUCKET}","arn:aws:s3:::${INFRA_BUCKET}/*"]},
 {"Sid":"AgentCoreGateway","Effect":"Allow","Action":["bedrock-agentcore:InvokeGateway"],
  "Resource":["arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:gateway/*"]},
 {"Sid":"InvokeRuntime","Effect":"Allow","Action":["bedrock-agentcore:InvokeAgentRuntime"],
  "Resource":["arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:runtime/*"]}${secrets_stmt}
]}
JSON
}

# --- Shared runtime config fragments ---------------------------------------
NETWORK="{\"networkMode\":\"VPC\",\"networkModeConfig\":{\"subnets\":[\"${SUBNET_1}\",\"${SUBNET_2}\"],\"securityGroups\":[\"${SECURITY_GROUP}\"]}}"
PROTOCOL='{"serverProtocol":"HTTP"}'
FILESYSTEM="[{\"s3FilesAccessPoint\":{\"accessPointArn\":\"${S3FILES_AP_ARN}\",\"mountPath\":\"/mnt/s3files\"}}]"
LIFECYCLE="{\"idleRuntimeSessionTimeout\":${PERSONA_RUNTIME_IDLE_TIMEOUT},\"maxLifetime\":${PERSONA_RUNTIME_MAX_LIFETIME}}"

# --- Per-persona IAM role (shared across agents) ---------------------------
declare_persona_role() {
  local persona="$1" role_name role_arn
  role_name="${PERSONA_ROLE_PREFIX}${persona}"
  # This function returns the role ARN on stdout, so ALL diagnostics must go to
  # stderr — otherwise log lines get captured into the caller's role_arn and the
  # create-agent-runtime --role-arn becomes garbage (surfaces as AccessDenied).
  log "[$persona] IAM role ${role_name}" >&2
  if aws iam get-role --role-name "$role_name" >/dev/null 2>&1; then
    role_arn="$(aws iam get-role --role-name "$role_name" --query Role.Arn --output text)"
  else
    role_arn="$(aws iam create-role --role-name "$role_name" \
      --assume-role-policy-document "$(persona_trust)" --query Role.Arn --output text)"
    sleep 8
  fi
  aws iam put-role-policy --role-name "$role_name" --policy-name AgentCoreExecution \
    --policy-document "$(persona_exec_policy)" >&2
  printf '%s' "$role_arn"
}

# --- Create/update one (persona, agent) runtime ----------------------------
deploy_runtime() {
  local persona="$1" slug="$2" role_arn="$3"
  local runtime_name image_uri artifact envvars rid state_key sname key_env

  runtime_name="$(agent_runtime_name "$persona" "$slug")"
  image_uri="$(state_require "agent_image_uri_${slug}" "Run 68_agent_image.sh (agent '${slug}' not built).")"
  artifact="{\"containerConfiguration\":{\"containerUri\":\"${image_uri}\"}}"

  # Base env every agent gets; add the agent's secret name so run.sh can fetch
  # its API key from Secrets Manager at startup (key is never baked into image).
  envvars="GATEWAY_URL=${GATEWAY_URL},AWS_REGION=${AWS_REGION},AGENT_SLUG=${slug}"
  sname="$(agent_secret_name "$slug")"
  [[ -n "$sname" ]] && envvars="${envvars},AGENT_SECRET_NAME=${sname}"

  log "[$persona/$slug] runtime ${runtime_name}"
  state_key="$(agent_state_key "$persona" "$slug" id)"
  rid="$(state_get "$state_key")"
  if [[ -z "$rid" ]]; then
    rid="$(aws bedrock-agentcore-control list-agent-runtimes --region "$AWS_REGION" \
      --query "agentRuntimes[?agentRuntimeName=='${runtime_name}'].agentRuntimeId | [0]" --output text 2>/dev/null || true)"
    [[ "$rid" == "None" ]] && rid=""
  fi

  if [[ -n "$rid" ]] && aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$rid" --region "$AWS_REGION" >/dev/null 2>&1; then
    log "[$persona/$slug] updating $rid"
    aws bedrock-agentcore-control update-agent-runtime --agent-runtime-id "$rid" --region "$AWS_REGION" \
      --agent-runtime-artifact "$artifact" --role-arn "$role_arn" \
      --network-configuration "$NETWORK" --protocol-configuration "$PROTOCOL" \
      --filesystem-configurations "$FILESYSTEM" --environment-variables "$envvars" \
      --lifecycle-configuration "$LIFECYCLE" \
      --description "${slug} persona: ${persona}" >/dev/null
  else
    log "[$persona/$slug] creating runtime"
    # Newly-created execution roles can take longer than the fixed sleep to
    # propagate; AgentCore validates ECR + S3 Files access at create time and
    # returns a ValidationException until the role is usable. Retry that transient.
    local attempt="" create_err=""
    for attempt in 1 2 3 4 5 6; do
      if rid="$(aws bedrock-agentcore-control create-agent-runtime --agent-runtime-name "$runtime_name" --region "$AWS_REGION" \
        --agent-runtime-artifact "$artifact" --role-arn "$role_arn" \
        --network-configuration "$NETWORK" --protocol-configuration "$PROTOCOL" \
        --filesystem-configurations "$FILESYSTEM" --environment-variables "$envvars" \
        --lifecycle-configuration "$LIFECYCLE" \
        --description "${slug} persona: ${persona}" \
        --query agentRuntimeId --output text 2>/tmp/persona_create_err)"; then
        break
      fi
      create_err="$(cat /tmp/persona_create_err)"
      # All three are IAM role-propagation transients right after put-role-policy:
      # AgentCore validates ECR + S3 Files access and authorizes the passed role
      # at create time and can briefly reject the call until the freshly-attached
      # policy propagates (the last one surfaces as an AccessDeniedException).
      if echo "$create_err" | grep -qE "Access denied while validating ECR URI|Execution role is missing required permissions|AccessDeniedException|not authorized to perform"; then
        warn "[$persona/$slug] role not yet propagated (attempt $attempt) — retrying in 15s"
        sleep 15
      else
        die "[$persona/$slug] create-agent-runtime failed: $create_err"
      fi
    done
    [[ -n "$rid" && "$rid" != "None" ]] || die "[$persona/$slug] runtime not created after retries: ${create_err:-unknown error}"
  fi

  wait_runtime_ready "$rid"
  local runtime_arn="arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:runtime/${rid}"
  state_set "$state_key" "$rid"
  state_set "$(agent_state_key "$persona" "$slug" arn)" "$runtime_arn"
  # Back-compat: keep the legacy per-persona keys pointing at claude-code.
  if [[ "$slug" == "claude-code" ]]; then
    state_set "persona_runtime_${persona}_id" "$rid"
    state_set "persona_runtime_${persona}_arn" "$runtime_arn"
  fi
  ok "[$persona/$slug] READY: $runtime_arn"
}

# Agents to deploy: all enabled (AGENTS) by default, or a subset passed as args
# (e.g. `bash scripts/70_personas.sh devin` to add just one agent's runtimes
# without touching the others).
deploy_agents=()
if [[ $# -gt 0 ]]; then
  for a in "$@"; do deploy_agents+=("$(agent_normalize_slug "$a")"); done
else
  for a in "${AGENTS[@]}"; do deploy_agents+=("$(agent_normalize_slug "$a")"); done
fi
log "Deploying runtimes for agents: ${deploy_agents[*]}"

for persona in "${PERSONAS[@]}"; do
  role_arn="$(declare_persona_role "$persona")"
  for slug in "${deploy_agents[@]}"; do
    deploy_runtime "$persona" "$slug" "$role_arn"
  done
done

ok "All persona x agent runtimes deployed"
