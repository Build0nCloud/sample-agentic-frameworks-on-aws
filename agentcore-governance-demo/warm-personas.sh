#!/usr/bin/env bash
# ============================================================================
# Warm up the persona runtimes so the live demo responds instantly.
#
# Persona runtimes are VPC microVMs with an S3 Files mount; a cold start takes
# longer than the WebSocket shell's handshake timeout, so the first connection
# attempt reliably times out while it triggers the VM start. This script drives
# the fix automatically: for each (persona, agent) it uses one stable session id
# and retries that same session — the first attempt kicks off the microVM, later
# attempts land on the now-warm VM.
#
# It is account-aware (uses the per-account state file resolved by common.sh) and
# agent-aware (warms every enabled agent in AGENTS, or a subset via --agent).
#
# Usage:
#   ./warm-personas.sh                        # all personas, all enabled agents
#   ./warm-personas.sh --persona clinician    # one persona
#   ./warm-personas.sh --agent devin.ai       # one agent (all personas)
#   ./warm-personas.sh --persona dev --agent claude-code
#   ./warm-personas.sh --attempts 8 --wait 30 # tune retry loop
# ============================================================================
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source lib/common.sh

# --- args -------------------------------------------------------------------
WANT_PERSONA=""; WANT_AGENT=""; ATTEMPTS=6; WAIT=40; PER_ATTEMPT_TIMEOUT=150
while [[ $# -gt 0 ]]; do
  case "$1" in
    --persona) WANT_PERSONA="$2"; shift 2 ;;
    --agent)   WANT_AGENT="$(agent_normalize_slug "$2")"; shift 2 ;;
    --attempts) ATTEMPTS="$2"; shift 2 ;;
    --wait)    WAIT="$2"; shift 2 ;;
    --timeout) PER_ATTEMPT_TIMEOUT="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

# --- python + inputs --------------------------------------------------------
PYBIN="${PROJECT_ROOT}/.venv/bin/python"
[[ -x "$PYBIN" ]] || PYBIN="python3"

# Which personas / agents to warm
personas=("${PERSONAS[@]}"); [[ -n "$WANT_PERSONA" ]] && personas=("$WANT_PERSONA")
agents=(); for a in "${AGENTS[@]}"; do agents+=("$(agent_normalize_slug "$a")"); done
[[ -n "$WANT_AGENT" ]] && agents=("$WANT_AGENT")

section "Warming personas (account ${AWS_ACCOUNT_ID}, region ${AWS_REGION})"
log "personas: ${personas[*]}  |  agents: ${agents[*]}  |  attempts: ${ATTEMPTS}  wait: ${WAIT}s"

# Runtime ARN for (persona, agent) from state, with legacy fallback for claude-code.
runtime_arn_for() {
  local persona="$1" slug="$2" a
  a="$(state_get "persona_runtime_${persona}_${slug}_arn")"
  [[ -z "$a" && "$slug" == "claude-code" ]] && a="$(state_get "persona_runtime_${persona}_arn")"
  printf '%s' "$a"
}

# Stable, >=33-char session id per (account, persona, agent) so repeat runs reuse
# the same warm VM. Session ids must be at least 33 chars (shorter ids get HTTP 400).
session_id_for() {
  local persona="$1" slug="$2" h
  h="$(printf '%s' "${AWS_ACCOUNT_ID}:${persona}:${slug}" | shasum -a 1 | cut -c1-40)"
  printf 'warm-%s-%s-%s' "$persona" "$slug" "$h"
}

MARKER="__WARM_OK__"
total=0; warmed=0; failed=0; skipped=0; failures=()

for slug in "${agents[@]}"; do
  for persona in "${personas[@]}"; do
    arn="$(runtime_arn_for "$persona" "$slug")"
    if [[ -z "$arn" ]]; then
      warn "[$persona/$slug] no runtime in state — skipping"
      skipped=$((skipped+1)); continue
    fi
    total=$((total+1))
    sid="$(session_id_for "$persona" "$slug")"
    ok_flag=0
    log "[$persona/$slug] warming (up to ${ATTEMPTS} attempts)…"
    for ((try=1; try<=ATTEMPTS; try++)); do
      out="$(timeout "$PER_ATTEMPT_TIMEOUT" "$PYBIN" connect_persona.py \
              --persona "$persona" --agent "$slug" --session "$sid" \
              --cmd "echo ${MARKER}" 2>&1)" || true
      # Pure-bash match (avoids pipefail edge cases that could abort the loop).
      if [[ "$out" == *"$MARKER"* ]]; then
        ok "[$persona/$slug] warm (attempt ${try})"
        warmed=$((warmed+1)); ok_flag=1; break
      fi
      # Cold start in progress (handshake timeout) — wait and retry same session.
      reason="$(printf '%s\n' "$out" | grep -oE 'TimeoutError.*|HTTP [0-9]+|No runtime for persona.*' | head -1 || true)"
      if [[ $try -lt $ATTEMPTS ]]; then
        log "[$persona/$slug] not ready (attempt ${try}: ${reason:-unknown}) — retrying in ${WAIT}s"
        sleep "$WAIT"
      fi
    done
    if [[ $ok_flag -ne 1 ]]; then
      err "[$persona/$slug] failed to warm after ${ATTEMPTS} attempts (last: ${reason:-unknown})"
      failed=$((failed+1)); failures+=("$persona/$slug")
    fi
  done
done

echo
section "Warm-up summary"
log "warmed ${warmed}/${total}  |  skipped ${skipped}"
if [[ $failed -gt 0 ]]; then
  err "failed: ${failures[*]}"
  err "Runtimes can be slow to cold-start; try again with --attempts 8 --wait 30, or re-run."
  exit 1
fi
ok "All targeted personas are warm."
