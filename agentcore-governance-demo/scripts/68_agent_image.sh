#!/usr/bin/env bash
# Build + push the Claude Code agent container image (shared by all 4 personas).
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

section "Agent image: ${AGENT_ECR_REPO}"

IMAGE_URI="${ECR_REGISTRY}/${AGENT_ECR_REPO}:latest"

aws ecr describe-repositories --repository-names "${AGENT_ECR_REPO}" --region "$AWS_REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "${AGENT_ECR_REPO}" --region "$AWS_REGION" >/dev/null

log "finch login to ECR"
aws ecr get-login-password --region "$AWS_REGION" | finch login --username AWS --password-stdin "${ECR_REGISTRY}"

log "build (arm64) + push ${IMAGE_URI}"
finch build --platform linux/arm64 -t "${AGENT_ECR_REPO}:latest" -f "${PROJECT_ROOT}/agent/Dockerfile" "${PROJECT_ROOT}/agent"
finch tag "${AGENT_ECR_REPO}:latest" "${IMAGE_URI}"
finch push "${IMAGE_URI}"

state_set agent_image_uri "${IMAGE_URI}"
ok "Agent image: ${IMAGE_URI}"
