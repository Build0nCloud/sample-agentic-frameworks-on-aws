#!/usr/bin/env bash
# Shared network infrastructure for the persona runtimes:
#   - S3 bucket (shared files)
#   - VPC + private subnets + NAT + security group + S3 Files (via CloudFormation)
#   - uploads the MCP gateway proxy (index.js + node_modules) and skills to S3,
#     mounted at /mnt/s3files inside every persona runtime.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

section "Infrastructure: S3 bucket + VPC + S3 Files"

# --- S3 bucket --------------------------------------------------------------
if aws s3api head-bucket --bucket "${INFRA_BUCKET}" 2>/dev/null; then
  ok "Bucket exists: ${INFRA_BUCKET}"
else
  log "Creating bucket ${INFRA_BUCKET}"
  if [[ "${AWS_REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${INFRA_BUCKET}" --region "${AWS_REGION}" >/dev/null
  else
    aws s3api create-bucket --bucket "${INFRA_BUCKET}" --region "${AWS_REGION}" \
      --create-bucket-configuration LocationConstraint="${AWS_REGION}" >/dev/null
  fi
fi
aws s3api put-bucket-versioning --bucket "${INFRA_BUCKET}" \
  --versioning-configuration Status=Enabled --region "${AWS_REGION}"
state_set infra_bucket "${INFRA_BUCKET}"

# --- CloudFormation (VPC + S3 Files) ---------------------------------------
log "Deploying CloudFormation stack ${INFRA_STACK_NAME}"
aws cloudformation deploy \
  --template-file "${PROJECT_ROOT}/infra/cfn-vpc.yaml" \
  --stack-name "${INFRA_STACK_NAME}" \
  --region "${AWS_REGION}" \
  --parameter-overrides BucketName="${INFRA_BUCKET}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset

log "Reading stack outputs"
OUTPUTS="$(aws cloudformation describe-stacks --stack-name "${INFRA_STACK_NAME}" \
  --region "${AWS_REGION}" --query "Stacks[0].Outputs" --output json)"
get_out() { echo "$OUTPUTS" | jq -r --arg k "$1" '.[] | select(.OutputKey==$k) | .OutputValue'; }

VPC_ID="$(get_out VpcId)"
SUBNET_1="$(get_out PrivateSubnet1Id)"
SUBNET_2="$(get_out PrivateSubnet2Id)"
SECURITY_GROUP="$(get_out SecurityGroupId)"
S3FILES_FS_ID="$(get_out S3FilesFileSystemId)"
S3FILES_AP_ID="$(get_out S3FilesAccessPointId)"
S3FILES_AP_ARN="$(get_out S3FilesAccessPointArn)"

state_set infra_vpc_id "${VPC_ID}"
state_set infra_subnet_1 "${SUBNET_1}"
state_set infra_subnet_2 "${SUBNET_2}"
state_set infra_security_group "${SECURITY_GROUP}"
state_set infra_s3files_ap_arn "${S3FILES_AP_ARN}"
ok "VPC ${VPC_ID} | subnets ${SUBNET_1},${SUBNET_2} | sg ${SECURITY_GROUP}"
ok "S3 Files AP ${S3FILES_AP_ARN}"

# --- Upload MCP gateway proxy + skills to S3 --------------------------------
MCP_DIR="${PROJECT_ROOT}/mcp-proxy"
MCP_S3="s3://${INFRA_BUCKET}/agents/mnt/s3files/mcp"
SKILLS_S3="s3://${INFRA_BUCKET}/agents/mnt/s3files/skills"

if [[ ! -d "${MCP_DIR}/node_modules" ]]; then
  log "Installing MCP proxy dependencies (npm)"
  ( cd "${MCP_DIR}" && npm install --omit=dev )
fi

log "Uploading MCP proxy to ${MCP_S3}"
aws s3 cp "${MCP_DIR}/index.js" "${MCP_S3}/index.js" --region "${AWS_REGION}" >/dev/null
aws s3 cp "${MCP_DIR}/package.json" "${MCP_S3}/package.json" --region "${AWS_REGION}" >/dev/null
aws s3 sync "${MCP_DIR}/node_modules" "${MCP_S3}/node_modules" --region "${AWS_REGION}" >/dev/null
ok "MCP proxy uploaded"

if [[ -f "${MCP_DIR}/github-mcp.md" ]]; then
  aws s3 cp "${MCP_DIR}/github-mcp.md" "${SKILLS_S3}/github-mcp.md" --region "${AWS_REGION}" >/dev/null
  ok "Skill uploaded: github-mcp.md"
fi

ok "Infrastructure ready"
