#!/usr/bin/env bash
# Create and seed the DynamoDB table backing the MedicalRecords MCP server.
# Seeds 7 patient records: 3 with full PII, 4 anonymized.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

section "DynamoDB: ${MEDICAL_TABLE_NAME} (create + seed)"

PY="python3"
[[ -x "${PROJECT_ROOT}/.venv/bin/python" ]] && PY="${PROJECT_ROOT}/.venv/bin/python"

AWS_REGION="${AWS_REGION}" DYNAMODB_TABLE="${MEDICAL_TABLE_NAME}" \
  "${PY}" "${PROJECT_ROOT}/mcp-servers/dynamodb/seed_data.py"

state_set medical_table "${MEDICAL_TABLE_NAME}"
ok "Table seeded: ${MEDICAL_TABLE_NAME}"
