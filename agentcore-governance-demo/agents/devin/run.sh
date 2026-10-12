#!/usr/bin/env bash
# ============================================================
# Devin CLI launcher for AgentCore Runtime (headless + interactive)
# ============================================================
# Baked into the image and called by connect_persona.py with the SAME contract
# as every other agent:
#   /app/run.sh                      # interactive devin
#   /app/run.sh --model X 'prompt'   # non-interactive one-shot
#
# MCP:
#   ~/.config/devin/mcp_config.json is generated at startup from GATEWAY_URL so
#   Devin's tool calls flow through the governed gateway (stdio MCP proxy). This
#   is the same proxy the claude-code agent uses — governance is identical.
#
# Auth:
#   Devin CLI reaches Cognition's backend for inference and needs a Devin API
#   token. run.sh fetches it from Secrets Manager (AGENT_SECRET_NAME, injected by
#   the runtime) and authenticates non-interactively. The token is never baked
#   into the image.
# ============================================================
set -euo pipefail

# ── Inherit runtime env from PID 1 if not already set ────────
_from_pid1() { cat /proc/1/environ 2>/dev/null | tr '\0' '\n' | grep "^$1=" | cut -d= -f2- || true; }
[ -z "${GATEWAY_URL:-}" ]        && export GATEWAY_URL="$(_from_pid1 GATEWAY_URL)"
[ -z "${AWS_REGION:-}" ]         && export AWS_REGION="$(_from_pid1 AWS_REGION)"
[ -z "${AGENT_SECRET_NAME:-}" ]  && export AGENT_SECRET_NAME="$(_from_pid1 AGENT_SECRET_NAME)"

export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-${AWS_REGION:-us-west-2}}"
export AWS_REGION="${AWS_REGION:-$AWS_DEFAULT_REGION}"
export HOME="/home/agent"
export PATH="/home/agent/.local/bin:/home/agent/.local/share/devin/bin:${PATH}"
cd "$HOME"

# ── Generate the MCP gateway config (stdio proxy) ────────────
if [ -n "${GATEWAY_URL:-}" ]; then
  mkdir -p "$HOME/.config/devin"
  cat > "$HOME/.config/devin/mcp_config.json" <<MCPEOF
{
  "mcpServers": {
    "gateway": {
      "command": "node",
      "args": ["/mnt/s3files/mcp/index.js", "--gateway-url", "${GATEWAY_URL}", "--region", "${AWS_REGION}"]
    }
  }
}
MCPEOF
fi

# ── Authenticate to Devin (non-interactive) ──────────────────
# The Devin CLI authenticates from a credentials file; for API-key auth the
# minimal, supported form is a single `windsurf_api_key` line (verified against
# `devin auth status` / `devin models list`). `devin auth login` is an
# interactive browser PKCE flow and is NOT usable headless, so we write the
# credentials file directly. The API key comes from Secrets Manager (never baked
# into the image). Do this once per microVM.
DEVIN_CREDS="${HOME}/.local/share/devin/credentials.toml"
if [ ! -s "$DEVIN_CREDS" ]; then
  if [ -z "${DEVIN_API_KEY:-}" ] && [ -n "${AGENT_SECRET_NAME:-}" ]; then
    # Secret may be a raw string or JSON like {"DEVIN_API_KEY":"..."}.
    _secret="$(aws secretsmanager get-secret-value --secret-id "$AGENT_SECRET_NAME" \
                 --region "$AWS_REGION" --query SecretString --output text 2>/dev/null || true)"
    if printf '%s' "$_secret" | grep -q '{'; then
      DEVIN_API_KEY="$(printf '%s' "$_secret" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("DEVIN_API_KEY") or d.get("api_key") or d.get("windsurf_api_key") or d.get("token") or "")' 2>/dev/null || true)"
    else
      DEVIN_API_KEY="$_secret"
    fi
    export DEVIN_API_KEY
  fi
  if [ -n "${DEVIN_API_KEY:-}" ]; then
    mkdir -p "$(dirname "$DEVIN_CREDS")"
    printf 'windsurf_api_key = "%s"\n' "$DEVIN_API_KEY" > "$DEVIN_CREDS"
    chmod 600 "$DEVIN_CREDS"
  fi
fi

# ── Parse --model flag (rest passes through) ─────────────────
MODEL=""
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
set -- "${ARGS[@]:-}"
MODEL_FLAG=(); [ -n "$MODEL" ] && MODEL_FLAG=(--model "$MODEL")

# ── Run ──────────────────────────────────────────────────────
# Headless one-shot: --print with bypass permission mode (auto-approve local
#   tool use; gateway still enforces tool access via Cedar) and workspace-trust
#   disabled (required for --print in a non-interactive dir).
# Interactive: plain session.
if [ "${#ARGS[@]}" -gt 0 ] && [ -n "${ARGS[0]:-}" ]; then
  # Use `--` to pass the prompt so it isn't parsed as a [PATH] positional.
  exec devin --print --permission-mode bypass --respect-workspace-trust false "${MODEL_FLAG[@]}" -- "$@"
else
  exec devin "${MODEL_FLAG[@]}"
fi
