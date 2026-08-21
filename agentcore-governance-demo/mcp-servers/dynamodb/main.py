"""
DynamoDB MCP Server for AgentCore Gateway.

Provides tools to query/scan/put/delete items in a DynamoDB table.
Seeded with medical patient records — some containing PII for guardrails demos.
Uses the runtime IAM role for DynamoDB access (no API keys needed).
"""

import json
import os
from decimal import Decimal
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key, Attr
from fastmcp import FastMCP

mcp = FastMCP("DynamoDB")

AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "medical-records-demo")

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table(TABLE_NAME)


# ── Helper to handle Decimal serialization ───────────────────────────────────

class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o) if o % 1 else int(o)
        return super().default(o)


def serialize(obj):
    return json.loads(json.dumps(obj, cls=DecimalEncoder))


# ── Tools ────────────────────────────────────────────────────────────────────


@mcp.tool()
def get_patient(patient_id: str) -> dict:
    """Get a patient record by ID. Returns full patient details including medical history."""
    response = table.get_item(Key={"patient_id": patient_id})
    item = response.get("Item")
    if not item:
        return {"error": f"Patient {patient_id} not found"}
    return serialize(item)


@mcp.tool()
def search_patients(
    department: Optional[str] = None,
    diagnosis: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """Search patient records by department, diagnosis, or status. Returns summary list."""
    filter_expression = None

    if department:
        filter_expression = Attr("department").eq(department)
    if diagnosis:
        expr = Attr("diagnosis").contains(diagnosis)
        filter_expression = filter_expression & expr if filter_expression else expr
    if status:
        expr = Attr("status").eq(status)
        filter_expression = filter_expression & expr if filter_expression else expr

    scan_kwargs = {}
    if filter_expression:
        scan_kwargs["FilterExpression"] = filter_expression

    response = table.scan(**scan_kwargs)
    items = response.get("Items", [])

    # Return summary (not full PII)
    return serialize([{
        "patient_id": item["patient_id"],
        "name": item.get("name", "Unknown"),
        "department": item.get("department", ""),
        "diagnosis": item.get("diagnosis", ""),
        "status": item.get("status", ""),
    } for item in items])


@mcp.tool()
def list_patients() -> list[dict]:
    """List all patients in the system. Returns patient_id, name, department, and status."""
    response = table.scan()
    items = response.get("Items", [])
    return serialize([{
        "patient_id": item["patient_id"],
        "name": item.get("name", "Unknown"),
        "department": item.get("department", ""),
        "status": item.get("status", ""),
    } for item in items])


@mcp.tool()
def put_patient(
    patient_id: str,
    name: str,
    department: str,
    diagnosis: str,
    status: str = "Active",
    ssn: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    address: Optional[str] = None,
    insurance_id: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Create or update a patient record in the database."""
    item = {
        "patient_id": patient_id,
        "name": name,
        "department": department,
        "diagnosis": diagnosis,
        "status": status,
    }
    if ssn:
        item["ssn"] = ssn
    if phone:
        item["phone"] = phone
    if email:
        item["email"] = email
    if address:
        item["address"] = address
    if insurance_id:
        item["insurance_id"] = insurance_id
    if notes:
        item["notes"] = notes

    table.put_item(Item=item)
    return {"patient_id": patient_id, "action": "created/updated"}


@mcp.tool()
def delete_patient(patient_id: str) -> dict:
    """Delete a patient record permanently. This action cannot be undone."""
    table.delete_item(Key={"patient_id": patient_id})
    return {"patient_id": patient_id, "action": "deleted"}


@mcp.tool()
def update_patient_status(patient_id: str, status: str) -> dict:
    """Update a patient's status (Active, Discharged, Deceased, Transferred)."""
    valid_statuses = ["Active", "Discharged", "Deceased", "Transferred"]
    if status not in valid_statuses:
        return {"error": f"Invalid status. Valid: {valid_statuses}"}

    table.update_item(
        Key={"patient_id": patient_id},
        UpdateExpression="SET #s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": status},
    )
    return {"patient_id": patient_id, "status": status}


@mcp.tool()
def add_clinical_note(patient_id: str, note: str, author: str = "system") -> dict:
    """Add a clinical note to a patient's record."""
    from datetime import datetime

    response = table.get_item(Key={"patient_id": patient_id})
    item = response.get("Item")
    if not item:
        return {"error": f"Patient {patient_id} not found"}

    notes = item.get("clinical_notes", [])
    notes.append({
        "author": author,
        "note": note,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })

    table.update_item(
        Key={"patient_id": patient_id},
        UpdateExpression="SET clinical_notes = :notes",
        ExpressionAttributeValues={":notes": notes},
    )
    return {"patient_id": patient_id, "notes_count": len(notes)}


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "8000")), stateless_http=True)
