#!/usr/bin/env bash
# Create the two Bedrock Guardrails used by the response interceptor and publish
# version 1 of each:
#   clinician-guardrail   : blocks hate speech + political topics only (PII/medical allowed)
#   restrictive-guardrail : also blocks medical diagnoses + PII (SSN/phone/email/address/...)
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

section "Bedrock Guardrails"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# --- Shared config fragments ------------------------------------------------
TOPIC_HATE='{"name":"HateSpeech","definition":"Content that promotes hatred, discrimination, or violence against individuals or groups based on race, ethnicity, religion, gender, sexual orientation, disability, or other protected characteristics.","examples":["I hate all people from that country","Those people are inferior","We should eliminate that group"],"type":"DENY"}'
TOPIC_POLITICS='{"name":"PoliticalTopics","definition":"Discussions about political parties, elections, political candidates, political ideologies, partisan debates, or government policy positions.","examples":["Who should I vote for in the next election","The Republican party is better than Democrats","What is your opinion on immigration policy"],"type":"DENY"}'
TOPIC_MEDICAL='{"name":"MedicalDiagnoses","definition":"Specific medical diagnoses, conditions, treatment plans, or clinical assessments tied to identifiable patients. Includes cancer staging, disease progression, medication dosages for specific patients.","examples":["Patient has Stage II breast cancer","Diagnosed with early-onset Parkinsons disease","INR levels stable at 2.3, continue warfarin"],"type":"DENY"}'

CONTENT_FILTERS='[{"type":"VIOLENCE","inputStrength":"HIGH","outputStrength":"HIGH"},{"type":"HATE","inputStrength":"HIGH","outputStrength":"HIGH"},{"type":"SEXUAL","inputStrength":"HIGH","outputStrength":"HIGH"},{"type":"INSULTS","inputStrength":"MEDIUM","outputStrength":"MEDIUM"}]'

PII_ENTITIES='[{"type":"US_SOCIAL_SECURITY_NUMBER","action":"BLOCK"},{"type":"PHONE","action":"BLOCK"},{"type":"EMAIL","action":"BLOCK"},{"type":"NAME","action":"ANONYMIZE"},{"type":"ADDRESS","action":"BLOCK"},{"type":"US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER","action":"BLOCK"},{"type":"CREDIT_DEBIT_CARD_NUMBER","action":"BLOCK"}]'

# ensure_guardrail NAME DESC TOPICS_JSON CONTENT_JSON PII_JSON_or_empty IN_MSG OUT_MSG STATE_ID_KEY STATE_VER_KEY
ensure_guardrail() {
  local name="$1" desc="$2" topics="$3" content="$4" pii="$5" in_msg="$6" out_msg="$7" id_key="$8" ver_key="$9"

  local gid; gid="$(aws bedrock list-guardrails --region "$AWS_REGION" \
    --query "guardrails[?name=='${name}'].id | [0]" --output text 2>/dev/null || true)"
  [[ "$gid" == "None" ]] && gid=""

  jq -n --argjson topics "$topics" '{topicsConfig:$topics}' > "$TMP/topic.json"
  jq -n --argjson f "$content" '{filtersConfig:$f}' > "$TMP/content.json"

  local pii_args=()
  if [[ -n "$pii" ]]; then
    jq -n --argjson p "$pii" '{piiEntitiesConfig:$p}' > "$TMP/pii.json"
    pii_args=(--sensitive-information-policy-config "file://$TMP/pii.json")
  fi

  if [[ -z "$gid" ]]; then
    log "[$name] creating guardrail"
    gid="$(aws bedrock create-guardrail --region "$AWS_REGION" --name "$name" --description "$desc" \
      --topic-policy-config "file://$TMP/topic.json" \
      --content-policy-config "file://$TMP/content.json" \
      "${pii_args[@]+"${pii_args[@]}"}" \
      --blocked-input-messaging "$in_msg" --blocked-outputs-messaging "$out_msg" \
      --query guardrailId --output text)"
  else
    log "[$name] exists ($gid) — updating"
    aws bedrock update-guardrail --region "$AWS_REGION" --guardrail-identifier "$gid" \
      --name "$name" --description "$desc" \
      --topic-policy-config "file://$TMP/topic.json" \
      --content-policy-config "file://$TMP/content.json" \
      "${pii_args[@]+"${pii_args[@]}"}" \
      --blocked-input-messaging "$in_msg" --blocked-outputs-messaging "$out_msg" >/dev/null
  fi

  # Publish version 1 if we don't already have a published version tracked.
  local ver; ver="$(state_get "$ver_key")"
  if [[ -z "$ver" ]]; then
    sleep 3
    ver="$(aws bedrock create-guardrail-version --region "$AWS_REGION" \
      --guardrail-identifier "$gid" --description "Published for interceptor" \
      --query version --output text)"
  fi

  state_set "$id_key" "$gid"
  state_set "$ver_key" "$ver"
  ok "[$name] id=$gid version=$ver"
}

ensure_guardrail "${CLINICIAN_GUARDRAIL_NAME}" \
  "For clinician persona: blocks hate speech and political topics only. PII and medical content allowed." \
  "[$TOPIC_HATE,$TOPIC_POLITICS]" "$CONTENT_FILTERS" "" \
  "This request contains content that violates our policies. Hate speech and political topics are not permitted." \
  "The response was blocked because it contained content that violates our policies (hate speech or political topics)." \
  clinician_guardrail_id clinician_guardrail_version

ensure_guardrail "${RESTRICTIVE_GUARDRAIL_NAME}" \
  "For non-clinician personas: blocks hate speech, political topics, PII, and medical diagnoses." \
  "[$TOPIC_HATE,$TOPIC_POLITICS,$TOPIC_MEDICAL]" "$CONTENT_FILTERS" "$PII_ENTITIES" \
  "This request was blocked. PII, medical diagnoses, hate speech, and political topics are not permitted for your role." \
  "The response was blocked because it contained sensitive information (PII or medical diagnoses) or violated content policies. Contact an administrator for access." \
  restrictive_guardrail_id restrictive_guardrail_version

ok "Guardrails ready"
