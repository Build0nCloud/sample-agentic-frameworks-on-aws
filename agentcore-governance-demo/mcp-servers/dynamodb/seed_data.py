"""Seed the DynamoDB table with sample medical records (some with PII, some without)."""

import boto3
import os

AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "medical-records-demo")

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)

# Create table if not exists
client = boto3.client("dynamodb", region_name=AWS_REGION)
try:
    client.describe_table(TableName=TABLE_NAME)
    print(f"Table '{TABLE_NAME}' already exists.")
except client.exceptions.ResourceNotFoundException:
    print(f"Creating table '{TABLE_NAME}'...")
    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "patient_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "patient_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    waiter = client.get_waiter("table_exists")
    waiter.wait(TableName=TABLE_NAME)
    print("Table created.")

table = dynamodb.Table(TABLE_NAME)

# ── Sample records ───────────────────────────────────────────────────────────
# Records with PII (for guardrails demo — should be blocked)
RECORDS_WITH_PII = [
    {
        "patient_id": "PAT-001",
        "name": "John Michael Smith",
        "ssn": "123-45-6789",
        "phone": "+1-555-867-5309",
        "email": "john.smith@personalmail.com",
        "address": "1234 Oak Street, Springfield, IL 62701",
        "department": "Cardiology",
        "diagnosis": "Atrial fibrillation",
        "status": "Active",
        "insurance_id": "BCBS-98765432",
        "clinical_notes": [
            {"author": "Dr. Williams", "note": "Patient presents with irregular heartbeat. Starting anticoagulation therapy.", "timestamp": "2026-07-01T09:00:00Z"},
            {"author": "Dr. Williams", "note": "Follow-up: INR levels stable at 2.3. Continue current dosage.", "timestamp": "2026-07-15T10:30:00Z"},
        ],
    },
    {
        "patient_id": "PAT-002",
        "name": "Maria Elena Rodriguez",
        "ssn": "987-65-4321",
        "phone": "+1-555-234-5678",
        "email": "maria.rodriguez@email.com",
        "address": "5678 Pine Avenue, Austin, TX 78701",
        "department": "Oncology",
        "diagnosis": "Stage II breast cancer",
        "status": "Active",
        "insurance_id": "UHC-11223344",
        "clinical_notes": [
            {"author": "Dr. Patel", "note": "Biopsy confirmed invasive ductal carcinoma. Recommending lumpectomy followed by radiation.", "timestamp": "2026-06-15T14:00:00Z"},
            {"author": "Dr. Patel", "note": "Surgery successful. Clean margins achieved. Scheduling radiation planning.", "timestamp": "2026-07-10T11:00:00Z"},
        ],
    },
    {
        "patient_id": "PAT-003",
        "name": "Robert James Chen",
        "ssn": "456-78-9012",
        "phone": "+1-555-345-6789",
        "email": "r.chen@workmail.com",
        "address": "910 Elm Boulevard, Seattle, WA 98101",
        "department": "Neurology",
        "diagnosis": "Early-onset Parkinson's disease",
        "status": "Active",
        "insurance_id": "AETNA-55667788",
        "clinical_notes": [
            {"author": "Dr. Nakamura", "note": "Tremor onset in left hand. DaTscan confirms dopaminergic deficit. Starting carbidopa-levodopa.", "timestamp": "2026-05-20T08:30:00Z"},
        ],
    },
]

# Records without PII (safe to return)
RECORDS_WITHOUT_PII = [
    {
        "patient_id": "PAT-004",
        "name": "Anonymous Patient A",
        "department": "Emergency",
        "diagnosis": "Fractured radius (left arm)",
        "status": "Discharged",
        "clinical_notes": [
            {"author": "Dr. Thompson", "note": "Simple fracture, no surgical intervention needed. Cast applied.", "timestamp": "2026-07-20T22:00:00Z"},
        ],
    },
    {
        "patient_id": "PAT-005",
        "name": "Anonymous Patient B",
        "department": "Cardiology",
        "diagnosis": "Hypertension, well-controlled",
        "status": "Active",
        "clinical_notes": [
            {"author": "Dr. Williams", "note": "BP 128/82 on current medication. Continue lisinopril 10mg daily.", "timestamp": "2026-07-25T09:00:00Z"},
        ],
    },
    {
        "patient_id": "PAT-006",
        "name": "Anonymous Patient C",
        "department": "Orthopedics",
        "diagnosis": "ACL reconstruction recovery",
        "status": "Active",
        "clinical_notes": [
            {"author": "Dr. Garcia", "note": "6 weeks post-op. ROM improving. Continue physical therapy 3x/week.", "timestamp": "2026-07-22T15:00:00Z"},
        ],
    },
    {
        "patient_id": "PAT-007",
        "name": "Anonymous Patient D",
        "department": "Oncology",
        "diagnosis": "Lymphoma in remission",
        "status": "Active",
        "clinical_notes": [
            {"author": "Dr. Patel", "note": "PET scan clear. 6-month follow-up scheduled. No treatment needed.", "timestamp": "2026-07-18T10:00:00Z"},
        ],
    },
]

# Seed all records
all_records = RECORDS_WITH_PII + RECORDS_WITHOUT_PII
with table.batch_writer() as batch:
    for record in all_records:
        batch.put_item(Item=record)

print(f"Seeded {len(all_records)} patient records:")
print(f"  - {len(RECORDS_WITH_PII)} with PII (SSN, phone, email, address)")
print(f"  - {len(RECORDS_WITHOUT_PII)} without PII")
print(f"Table: {TABLE_NAME}")
