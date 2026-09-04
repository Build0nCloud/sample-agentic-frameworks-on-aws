"""Deploy the patient-support agent to AgentCore Runtime (adapts the sample deploy.py).

Ported from
``reference/.../03-optimize/deploy.py`` and parameterized to package this project's
``agent/patient_support_agent.py`` as the runtime ``main.py``. Builds an ARM64
dependency package, uploads it to S3, creates the runtime, and polls until ready.

Requires AWS access, network, and a local ``pip`` capable of building an ARM64
(manylinux2014_aarch64) wheel set. This module is imported lazily by
``AgentCoreClient.deploy_agent`` and is not exercised by unit tests.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from .agentcore_client import AgentHandle

# Path to the deployable agent module (packaged as main.py inside the runtime zip).
AGENT_MODULE_PATH = Path(__file__).resolve().parent / "agent" / "patient_support_agent.py"


def _trust_policy(account_id: str) -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {
                        "StringEquals": {"aws:SourceAccount": account_id},
                        "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:*:{account_id}:*"},
                    },
                }
            ],
        }
    )


_PERMISSIONS_POLICY = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:*",
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeIndexPolicies",
                    "logs:PutIndexPolicy",
                    "logs:FilterLogEvents",
                    "logs:GetLogEvents",
                    "logs:StartQuery",
                    "logs:GetQueryResults",
                    "logs:StopQuery",
                    "cloudwatch:*",
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "sts:AssumeRole",
                    "s3:GetObject",
                    "s3:ListBucket",
                    "lambda:GetFunction",
                    "lambda:InvokeFunction",
                ],
                "Resource": "*",
            }
        ],
    }
)

# v2 code-level change (used by target-based A/B tests): add an escalation tool and
# refine the system prompt. Applied by transforming the v1 agent source text.
_V2_EXTRA_TOOL = '''

    @tool
    def escalate_to_care_manager(patient_id: str, issue: str) -> dict:
        """Escalate a complex, non-clinical issue to a human care manager.

        Args:
            patient_id: Patient identifier involved in the escalation.
            issue: Brief description of the administrative issue needing human review.
        """
        import uuid as _uuid
        ticket_id = f"CM-{_uuid.uuid4().hex[:8].upper()}"
        return {
            "ticket_id": ticket_id,
            "patient_id": patient_id,
            "issue": issue,
            "assigned_to": "Care Manager (on-call)",
            "status": "OPEN",
            "message": f"Escalation {ticket_id} created; a care manager will follow up within 1 business day.",
        }
'''


def build_agent_code(version: str, agent_source: str | None = None) -> str:
    """Return the agent source to deploy as main.py (v1 as-is; v2 adds a tool)."""
    base = agent_source if agent_source is not None else AGENT_MODULE_PATH.read_text()
    if version == "v1":
        return base
    if version != "v2":
        raise ValueError(f"unknown agent version {version!r}")
    # Insert the extra tool just before the model is constructed, and register it.
    v2 = base.replace("    _MODEL = BedrockModel(", _V2_EXTRA_TOOL + "\n    _MODEL = BedrockModel(")
    v2 = v2.replace("        lookup_health_policy,\n    ]", "        lookup_health_policy,\n        escalate_to_care_manager,\n    ]")
    return v2


def _ensure_role(client, role_name: str, account_id: str) -> str:
    iam = client.iam
    try:
        resp = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=_trust_policy(account_id))
        role_arn = resp["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
    iam.put_role_policy(RoleName=role_name, PolicyName=f"{role_name}Policy", PolicyDocument=_PERMISSIONS_POLICY)
    time.sleep(10)  # allow IAM propagation
    return role_arn


def _build_package(build_dir: Path, agent_code: str) -> Path:
    if build_dir.exists():
        shutil.rmtree(build_dir)
    pkg_dir = build_dir / "pkg"
    pkg_dir.mkdir(parents=True)
    subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "strands-agents[otel]", "bedrock-agentcore", "aws-opentelemetry-distro",
            "-t", str(pkg_dir),
            "--platform", "manylinux2014_aarch64",
            "--only-binary=:all:",
            "--python-version", "3.13",
            "--quiet",
        ],
        check=True,
    )
    (pkg_dir / "main.py").write_text(agent_code)
    zip_path = build_dir / "deployment_package.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(pkg_dir):
            for f in files:
                if f.endswith(".pyc") or "__pycache__" in root:
                    continue
                full = Path(root) / f
                zf.write(full, full.relative_to(pkg_dir))
    return zip_path


def _ensure_bucket(client, bucket: str, region: str) -> None:
    s3 = client.s3
    try:
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket)
        else:
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
    except (s3.exceptions.BucketAlreadyOwnedByYou, s3.exceptions.BucketAlreadyExists):
        pass


def deploy_runtime(client, name: str, version: str = "v1", agent_source: str | None = None) -> AgentHandle:
    """Deploy the agent and return an AgentHandle (adapts the sample deploy.py)."""
    region = client.region
    account_id = client.sts.get_caller_identity()["Account"]
    role_name = f"{name}Role"
    bucket = f"bedrock-agentcore-code-{account_id}-{region}"
    s3_key = f"{name}/deployment_package.zip"
    build_dir = Path(f"/tmp/{name}_build")  # nosec B108

    role_arn = _ensure_role(client, role_name, account_id)
    zip_path = _build_package(build_dir, build_agent_code(version, agent_source))
    _ensure_bucket(client, bucket, region)
    client.s3.upload_file(str(zip_path), bucket, s3_key)

    resp = client.ctrl.create_agent_runtime(
        agentRuntimeName=name,
        agentRuntimeArtifact={
            "codeConfiguration": {
                "code": {"s3": {"bucket": bucket, "prefix": s3_key}},
                "runtime": "PYTHON_3_13",
                "entryPoint": ["opentelemetry-instrument", "main.py"],
            }
        },
        networkConfiguration={"networkMode": "PUBLIC"},
        roleArn=role_arn,
    )
    runtime_id = resp["agentRuntimeId"]

    runtime_arn = None
    for _ in range(90):
        detail = client.ctrl.get_agent_runtime(agentRuntimeId=runtime_id)
        status = detail.get("status", "UNKNOWN")
        if status in ("ACTIVE", "READY"):
            runtime_arn = detail.get("agentRuntimeArn")
            break
        if "FAILED" in status:
            raise RuntimeError(f"Runtime failed: {detail.get('failureReason')}")
        time.sleep(10)
    else:
        raise RuntimeError("Runtime did not become ready within 15 minutes")

    return AgentHandle(
        runtime_name=name,
        runtime_arn=runtime_arn,
        runtime_id=runtime_id,
        log_group=f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT",
        service_name=f"{name}.DEFAULT",
        role_arn=role_arn,
        region=region,
        s3_bucket=bucket,
        s3_key=s3_key,
        version=version,
        account_id=account_id,
    )
