"""
Gateway RESPONSE interceptor — applies Bedrock Guardrails per persona.

Identity: event["mcp"]["gatewayRequest"]["context"]["identity"]["awsPrincipalArn"]

Routing:
- persona-clinician → permissive guardrail (hate speech + politics only)
- All others → restrictive guardrail (hate speech + politics + PII + medical)
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Guardrail ids/versions are injected as environment variables at deploy time
# (see scripts/55_interceptor.sh). No account-specific defaults are baked in.
CLINICIAN_GUARDRAIL_ID = os.environ.get("CLINICIAN_GUARDRAIL_ID", "")
CLINICIAN_GUARDRAIL_VERSION = os.environ.get("CLINICIAN_GUARDRAIL_VERSION", "1")
RESTRICTIVE_GUARDRAIL_ID = os.environ.get("RESTRICTIVE_GUARDRAIL_ID", "")
RESTRICTIVE_GUARDRAIL_VERSION = os.environ.get("RESTRICTIVE_GUARDRAIL_VERSION", "1")
REGION = os.environ.get("AWS_REGION", "us-west-2")
CLINICIAN_ROLE = "persona-clinician"

bedrock_runtime = boto3.client("bedrock-runtime", region_name=REGION)


def lambda_handler(event, context):
    mcp_data = event.get("mcp", {})
    gateway_response = mcp_data.get("gatewayResponse", {})
    response_body = gateway_response.get("body", {})
    status_code = gateway_response.get("statusCode", 200)

    # Extract caller identity
    principal_arn = ""
    try:
        principal_arn = event["mcp"]["gatewayRequest"]["context"]["identity"]["awsPrincipalArn"]
    except (KeyError, TypeError):
        pass

    caller_role = ""
    if ":role/" in principal_arn:
        caller_role = principal_arn.split(":role/")[-1].strip()

    is_clinician = (caller_role == CLINICIAN_ROLE)
    logger.info(f"CALLER: arn={principal_arn}, role={caller_role}, is_clinician={is_clinician}")

    # Only filter tools/call responses
    request_body = mcp_data.get("gatewayRequest", {}).get("body", {})
    method = request_body.get("method", "")

    if method != "tools/call":
        return {
            "interceptorOutputVersion": "1.0",
            "mcp": {
                "transformedGatewayResponse": {
                    "body": response_body,
                    "statusCode": status_code
                }
            }
        }

    # Extract text content from tool response
    result = response_body.get("result", {})
    content_list = result.get("content", [])

    if not content_list:
        return {
            "interceptorOutputVersion": "1.0",
            "mcp": {
                "transformedGatewayResponse": {
                    "body": response_body,
                    "statusCode": status_code
                }
            }
        }

    # Select guardrail
    if is_clinician:
        guardrail_id = CLINICIAN_GUARDRAIL_ID
        guardrail_version = CLINICIAN_GUARDRAIL_VERSION
    else:
        guardrail_id = RESTRICTIVE_GUARDRAIL_ID
        guardrail_version = RESTRICTIVE_GUARDRAIL_VERSION

    logger.info(f"GUARDRAIL: using {guardrail_id} v{guardrail_version}")

    # Apply guardrail to text content
    modified = False
    new_content = []

    for item in content_list:
        if item.get("type") != "text":
            new_content.append(item)
            continue

        text = item.get("text", "")
        if not text:
            new_content.append(item)
            continue

        try:
            assessment = bedrock_runtime.apply_guardrail(
                guardrailIdentifier=guardrail_id,
                guardrailVersion=guardrail_version,
                source="OUTPUT",
                content=[{"text": {"text": text}}]
            )
        except Exception as e:
            logger.error(f"Guardrail API error: {e}")
            new_content.append(item)
            continue

        if assessment.get("action") == "GUARDRAIL_INTERVENED":
            outputs = assessment.get("outputs", [])
            blocked_text = outputs[0].get("text", "Content blocked by guardrail.") if outputs else "Content blocked by guardrail."
            new_content.append({"type": "text", "text": blocked_text})
            modified = True
            logger.info(f"BLOCKED: guardrail intervened for {caller_role}")
        else:
            new_content.append(item)

    if modified:
        result["content"] = new_content
        result.pop("structuredContent", None)
        response_body["result"] = result

    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "body": response_body,
                "statusCode": status_code
            }
        }
    }
