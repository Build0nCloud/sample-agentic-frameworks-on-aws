#!/usr/bin/env bash
# Build + push a container image for every enabled coding agent.
#
# Which agents are built is driven by AGENTS in config.env (default: claude-code).
# Each agent lives under agents/<slug>/ with its own Dockerfile and honors the
# common /app/run.sh contract. All agents reach tools only through the stdio MCP
# proxy, so governance is identical regardless of which agent runs.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

section "Agent images: ${AGENTS[*]}"

log "finch login to ECR"
aws ecr get-login-password --region "$AWS_REGION" | finch login --username AWS --password-stdin "${ECR_REGISTRY}"

for agent in "${AGENTS[@]}"; do
  slug="$(agent_normalize_slug "$agent")"
  repo="$(agent_ecr_repo "$slug")"
  build_dir="$(agent_build_dir "$slug")"
  image_uri="${ECR_REGISTRY}/${repo}:latest"

  [[ -f "${build_dir}/Dockerfile" ]] || die "[$slug] no Dockerfile at ${build_dir}. Is the slug in AGENTS correct?"

  log "[$slug] ECR repo ${repo}"
  aws ecr describe-repositories --repository-names "${repo}" --region "$AWS_REGION" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "${repo}" --region "$AWS_REGION" >/dev/null

  log "[$slug] build (arm64) + push ${image_uri}"
  finch build --platform linux/arm64 -t "${repo}:latest" -f "${build_dir}/Dockerfile" "${build_dir}"
  finch tag "${repo}:latest" "${image_uri}"
  finch push "${image_uri}"

  # Per-agent image URI (used by the persona matrix in 70_personas.sh).
  state_set "agent_image_uri_${slug}" "${image_uri}"
  # Back-compat: keep the legacy single key pointing at claude-code.
  [[ "$slug" == "claude-code" ]] && state_set agent_image_uri "${image_uri}"
  ok "[$slug] image: ${image_uri}"
done

ok "All agent images built"
