"""Deploy a code-based (Lambda) evaluator and register it with AgentCore.

Adapts the reference sample's custom-code-based-evaluation packaging: bundles the
Lambda source plus the ``bedrock-agentcore`` SDK and its runtime deps (built for the
Lambda platform), creates/updates the function, grants AgentCore permission to invoke
it, and registers it as a code-based evaluator via the control plane.

Requires AWS access and a local ``pip`` capable of building linux wheels; imported
lazily by ``AgentCoreClient`` and not exercised by unit tests.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

LAMBDA_ROLE_NAME = "AgentCoreLambdaEvaluatorRole"
LAMBDA_RUNTIME = "python3.12"
LAMBDA_PLATFORM = "manylinux2014_x86_64"
LAMBDA_PYVER = "312"

_LAMBDA_TRUST = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}],
    }
)


def ensure_lambda_role(client) -> str:
    """Create/reuse the Lambda execution role; return its ARN."""
    iam = client.iam
    account_id = client.sts.get_caller_identity()["Account"]
    arn = f"arn:aws:iam::{account_id}:role/{LAMBDA_ROLE_NAME}"
    try:
        iam.get_role(RoleName=LAMBDA_ROLE_NAME)
        return arn
    except iam.exceptions.NoSuchEntityException:
        iam.create_role(RoleName=LAMBDA_ROLE_NAME, AssumeRolePolicyDocument=_LAMBDA_TRUST, Description="AgentCore code-based evaluator Lambdas")
        iam.attach_role_policy(RoleName=LAMBDA_ROLE_NAME, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
        time.sleep(15)  # IAM propagation
        return arn


def _make_zip(source_dir: Path) -> bytes:
    """Bundle Lambda source + the bedrock-agentcore SDK (and deps) into a zip."""
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "packages"
        pkg.mkdir()
        subprocess.run([sys.executable, "-m", "pip", "install", "bedrock-agentcore>=1.6.0", "--no-deps", "--target", str(pkg), "--quiet"], check=True)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pydantic>=2.0.0", "--target", str(pkg),
             "--platform", LAMBDA_PLATFORM, "--implementation", "cp", "--python-version", LAMBDA_PYVER, "--only-binary=:all:", "--quiet"],
            check=True,
        )
        subprocess.run([sys.executable, "-m", "pip", "install", "starlette", "uvicorn", "websockets", "typing-extensions", "requests", "--target", str(pkg), "--quiet"], check=True)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for py in sorted(source_dir.glob("*.py")):
                zf.write(py, py.name)
            for f in sorted(pkg.rglob("*")):
                if f.is_file():
                    zf.write(f, str(f.relative_to(pkg)))
    buf.seek(0)
    return buf.read()


def deploy_lambda_evaluator(client, function_name: str, source_dir: Path, role_arn: str, timeout_s: int = 60) -> str:
    """Create/update the evaluator Lambda and grant AgentCore invoke permission; return ARN."""
    lam = client._client("lambda")
    account_id = client.sts.get_caller_identity()["Account"]
    zip_bytes = _make_zip(Path(source_dir))
    try:
        resp = lam.get_function(FunctionName=function_name)
        lam.update_function_code(FunctionName=function_name, ZipFile=zip_bytes)
        lam.get_waiter("function_updated_v2").wait(FunctionName=function_name)
        arn = resp["Configuration"]["FunctionArn"]
    except lam.exceptions.ResourceNotFoundException:
        resp = lam.create_function(
            FunctionName=function_name, Runtime=LAMBDA_RUNTIME, Role=role_arn,
            Handler="lambda_function.lambda_handler", Code={"ZipFile": zip_bytes},
            Timeout=timeout_s + 10, MemorySize=256, Description=f"AgentCore code-based evaluator: {function_name}",
        )
        lam.get_waiter("function_active_v2").wait(FunctionName=function_name)
        arn = resp["FunctionArn"]

    statement_id = "AllowAgentCoreEvaluateInvoke"
    try:
        lam.remove_permission(FunctionName=function_name, StatementId=statement_id)
    except lam.exceptions.ResourceNotFoundException:
        pass
    lam.add_permission(
        FunctionName=function_name, StatementId=statement_id, Action="lambda:InvokeFunction",
        Principal="bedrock-agentcore.amazonaws.com", SourceAccount=account_id,
    )
    return arn
