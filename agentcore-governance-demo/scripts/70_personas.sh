#!/usr/bin/env bash
# Create the 4 persona IAM roles and the 4 persona AgentCore runtimes.
# Every persona uses the same agent image; they differ only by IAM role, which
# is what the Cedar policy engine keys on to apply per-persona governance.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

section "Personas: IAM roles + runtimes"

SUBNET_1="$(state_require infra_subnet_1 'Run 10_infra.sh first.')"
SUBNET_2="$(state_require infra_subnet_2)"
SECURITY_GROUP="$(state_require infra_security_group)"
S3FILES_AP_ARN="$(state_require infra_s3files_ap_arn)"
INFRA_BUCKET="$(state_require infra_bucket)"
GATEWAY_URL="$(state_require gateway_url 'Run 40_gateway.sh first.')"
IMAGE_URI="$(state_require agent_image_uri 'Run 68_agent_image.sh first.')"
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

persona_exec_policy() {
  cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"Logs","Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogGroups","logs:DescribeLogStreams"],
  "Resource":["arn:aws:logs:${AWS_REGION}:${AWS_ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/*"]},
 {"Sid":"BedrockInvoke","Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream","bedrock:ListInferenceProfiles"],
  "Resource":["arn:aws:bedrock:*::foundation-model/*","arn:aws:bedrock:${AWS_REGION}:${AWS_ACCOUNT_ID}:*"]},
 {"Sid":"ECRAuth","Effect":"Allow","Action":["ecr:GetAuthorizationToken"],"Resource":["*"]},
 {"Sid":"ECRPull","Effect":"Allow","Action":["ecr:BatchGetImage","ecr:GetDownloadUrlForLayer"],
  "Resource":["arn:aws:ecr:${AWS_REGION}:${AWS_ACCOUNT_ID}:repository/${AGENT_ECR_REPO}"]},
 {"Sid":"S3Files","Effect":"Allow","Action":["s3files:GetAccessPoint","s3files:GetFileSystem","s3files:GetMountTarget","s3files:DescribeMountTargets","s3files:ListMountTargets","s3files:ClientMount","s3files:ClientWrite","s3files:ClientRootAccess"],
  "Resource":["${S3FILES_AP_ARN}","${S3FILES_FS_ARN}"]},
 {"Sid":"EFS","Effect":"Allow","Action":["elasticfilesystem:ClientMount","elasticfilesystem:ClientWrite","elasticfilesystem:DescribeAccessPoints","elasticfilesystem:DescribeMountTargets"],
  "Resource":["arn:aws:elasticfilesystem:${AWS_REGION}:${AWS_ACCOUNT_ID}:file-system/*","arn:aws:elasticfilesystem:${AWS_REGION}:${AWS_ACCOUNT_ID}:access-point/*"]},
 {"Sid":"S3Bucket","Effect":"Allow","Action":["s3:ListBucket","s3:ListBucketVersions","s3:GetObject*","s3:PutObject*","s3:DeleteObject*","s3:AbortMultipartUpload"],
  "Resource":["arn:aws:s3:::${INFRA_BUCKET}","arn:aws:s3:::${INFRA_BUCKET}/*"]},
 {"Sid":"AgentCoreGateway","Effect":"Allow","Action":["bedrock-agentcore:InvokeGateway"],
  "Resource":["arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:gateway/*"]},
 {"Sid":"InvokeRuntime","Effect":"Allow","Action":["bedrock-agentcore:InvokeAgentRuntime"],
  "Resource":["arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:runtime/*"]}
]}
JSON
}

# --- Runtime config fragments ----------------------------------------------
ARTIFACT="{\"containerConfiguration\":{\"containerUri\":\"${IMAGE_URI}\"}}"
NETWORK="{\"networkMode\":\"VPC\",\"networkModeConfig\":{\"subnets\":[\"${SUBNET_1}\",\"${SUBNET_2}\"],\"securityGroups\":[\"${SECURITY_GROUP}\"]}}"
PROTOCOL='{"serverProtocol":"HTTP"}'
FILESYSTEM="[{\"s3FilesAccessPoint\":{\"accessPointArn\":\"${S3FILES_AP_ARN}\",\"mountPath\":\"/mnt/s3files\"}}]"
ENVVARS="GATEWAY_URL=${GATEWAY_URL},AWS_REGION=${AWS_REGION}"
LIFECYCLE="{\"idleRuntimeSessionTimeout\":${PERSONA_RUNTIME_IDLE_TIMEOUT},\"maxLifetime\":${PERSONA_RUNTIME_MAX_LIFETIME}}"

for persona in "${PERSONAS[@]}"; do
  role_name="${PERSONA_ROLE_PREFIX}${persona}"
  runtime_name="${PERSONA_RUNTIME_PREFIX}${persona}"

  # --- IAM role ---
  log "[$persona] IAM role ${role_name}"
  if aws iam get-role --role-name "$role_name" >/dev/null 2>&1; then
    role_arn="$(aws iam get-role --role-name "$role_name" --query Role.Arn --output text)"
  else
    role_arn="$(aws iam create-role --role-name "$role_name" \
      --assume-role-policy-document "$(persona_trust)" --query Role.Arn --output text)"
    sleep 8
  fi
  aws iam put-role-policy --role-name "$role_name" --policy-name AgentCoreExecution \
    --policy-document "$(persona_exec_policy)"

  # --- Runtime ---
  log "[$persona] runtime ${runtime_name}"
  state_key="persona_runtime_${persona}_id"
  rid="$(state_get "$state_key")"
  if [[ -z "$rid" ]]; then
    rid="$(aws bedrock-agentcore-control list-agent-runtimes --region "$AWS_REGION" \
      --query "agentRuntimes[?agentRuntimeName=='${runtime_name}'].agentRuntimeId | [0]" --output text 2>/dev/null || true)"
    [[ "$rid" == "None" ]] && rid=""
  fi

  if [[ -n "$rid" ]] && aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$rid" --region "$AWS_REGION" >/dev/null 2>&1; then
    log "[$persona] updating $rid"
    aws bedrock-agentcore-control update-agent-runtime --agent-runtime-id "$rid" --region "$AWS_REGION" \
      --agent-runtime-artifact "$ARTIFACT" --role-arn "$role_arn" \
      --network-configuration "$NETWORK" --protocol-configuration "$PROTOCOL" \
      --filesystem-configurations "$FILESYSTEM" --environment-variables "$ENVVARS" \
      --lifecycle-configuration "$LIFECYCLE" \
      --description "Claude Code persona: ${persona}" >/dev/null
  else
    log "[$persona] creating runtime"
    # Newly-created execution roles can take longer than the fixed sleep above to
    # propagate; AgentCore validates ECR access at create time and returns a
    # ValidationException until the role is usable. Retry that transient.
    attempt=""; create_err=""
    for attempt in 1 2 3 4 5 6; do
      if rid="$(aws bedrock-agentcore-control create-agent-runtime --agent-runtime-name "$runtime_name" --region "$AWS_REGION" \
        --agent-runtime-artifact "$ARTIFACT" --role-arn "$role_arn" \
        --network-configuration "$NETWORK" --protocol-configuration "$PROTOCOL" \
        --filesystem-configurations "$FILESYSTEM" --environment-variables "$ENVVARS" \
        --lifecycle-configuration "$LIFECYCLE" \
        --description "Claude Code persona: ${persona}" \
        --query agentRuntimeId --output text 2>/tmp/persona_create_err)"; then
        break
      fi
      create_err="$(cat /tmp/persona_create_err)"
      # Both messages below are IAM role-propagation transients: AgentCore
      # validates the execution role's ECR and S3 Files permissions at create
      # time and rejects the call until the freshly-attached policy propagates.
      if echo "$create_err" | grep -qE "Access denied while validating ECR URI|Execution role is missing required permissions"; then
        warn "[$persona] role not yet propagated (attempt $attempt) — retrying in 15s"
        sleep 15
      else
        die "[$persona] create-agent-runtime failed: $create_err"
      fi
    done
    [[ -n "$rid" && "$rid" != "None" ]] || die "[$persona] runtime not created after retries: ${create_err:-unknown error}"
  fi
  wait_runtime_ready "$rid"
  runtime_arn="arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:runtime/${rid}"
  state_set "$state_key" "$rid"
  state_set "persona_runtime_${persona}_arn" "$runtime_arn"
  ok "[$persona] READY: $runtime_arn"
done

ok "All personas deployed"
