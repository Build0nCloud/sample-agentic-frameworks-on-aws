#!/usr/bin/env bash
# Store per-agent API keys in Secrets Manager, for each enabled agent that needs
# one. The key is read from config.env (e.g. DEVIN_API_KEY) and stored as a raw
# secret string; the persona runtimes fetch it at startup (see agents/<slug>/run.sh)
# and it is never baked into an image. Agents that authenticate via the runtime
# IAM role (e.g. claude-code on Bedrock) declare no secret and are skipped.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

section "Agent API keys -> Secrets Manager (agents: ${AGENTS[*]})"

created=0
for agent in "${AGENTS[@]}"; do
  slug="$(agent_normalize_slug "$agent")"
  sname="$(agent_secret_name "$slug")"
  key_var="$(agent_key_env_var "$slug")"
  # No secret needed for this agent (e.g. claude-code uses the runtime IAM role).
  [[ -z "$sname" || -z "$key_var" ]] && continue

  key_val="${!key_var:-}"
  [[ -n "$key_val" ]] || die "[$slug] ${key_var} is not set in config.env (required to create ${sname})."

  if aws secretsmanager describe-secret --secret-id "$sname" --region "$AWS_REGION" >/dev/null 2>&1; then
    log "[$slug] secret exists — updating value"
    aws secretsmanager put-secret-value --secret-id "$sname" \
      --secret-string "$key_val" --region "$AWS_REGION" >/dev/null
    secret_arn="$(aws secretsmanager describe-secret --secret-id "$sname" \
      --region "$AWS_REGION" --query ARN --output text)"
  else
    log "[$slug] creating secret ${sname}"
    secret_arn="$(aws secretsmanager create-secret --name "$sname" \
      --description "API key for the ${slug} coding agent" \
      --secret-string "$key_val" --region "$AWS_REGION" --query ARN --output text)"
  fi
  state_set "agent_secret_arn_${slug}" "$secret_arn"
  ok "[$slug] secret ARN: ${secret_arn}"
  created=$((created+1))
done

[[ $created -gt 0 ]] && ok "Stored ${created} agent secret(s)" || ok "No agent secrets needed for: ${AGENTS[*]}"
