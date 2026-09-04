"""Single thin service layer over **real** Amazon Bedrock AgentCore.

`AgentCoreClient` concentrates all AWS access in one place so the evaluation,
optimization, and promotion modules call one well-defined seam instead of scattering
boto3 across the codebase. It adapts the AWS sample's ``deploy.py`` / ``invoke.py`` /
``optimize.py`` logic (see ``reference/``) into methods, and owns AWS concerns:
credential resolution (standard chain), retry/backoff on throttling, and the sample's
timing waits.

Design for testability: all AWS clients are injectable via the constructor, so unit
tests pass lightweight fakes (no boto3 required, no real AWS). The pure helpers at
module scope (variant assignment, retry, baggage, response parsers, request shaping)
are importable and testable on their own.

Requirements: 1.6, 2.3, 3.1, 4.1, 4.3, 4.5, 5.1, 5.7, 5.8, 6.4, 8.2
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .models import ABTestResult, BundleConfig, BundleVersion, EvalMetric, SessionResult

SPANS_LOG_GROUP = "aws/spans"

# Error codes we treat as transient and retry with backoff.
RETRYABLE_ERROR_CODES = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "InternalServerException",
        "InternalFailure",
    }
)

# Batch-evaluation / recommendation / A-B terminal states.
BATCH_TERMINAL = frozenset({"COMPLETED", "FAILED", "STOPPED", "COMPLETED_WITH_ERRORS"})
REC_TERMINAL = frozenset({"COMPLETED", "FAILED"})


# ===========================================================================
# Client-facing handles / value objects
# ===========================================================================
@dataclass
class AgentHandle:
    """Identifiers for a deployed AgentCore runtime (mirrors the sample state file)."""

    runtime_name: str
    runtime_arn: str
    runtime_id: str
    log_group: str
    service_name: str
    role_arn: str
    region: str
    s3_bucket: str | None = None
    s3_key: str | None = None
    version: str = "v1"
    account_id: str | None = None

    @property
    def spans_log_group(self) -> str:
        return SPANS_LOG_GROUP

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_name": self.runtime_name,
            "runtime_arn": self.runtime_arn,
            "runtime_id": self.runtime_id,
            "log_group": self.log_group,
            "service_name": self.service_name,
            "role_arn": self.role_arn,
            "region": self.region,
            "s3_bucket": self.s3_bucket,
            "s3_key": self.s3_key,
            "version": self.version,
            "account_id": self.account_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentHandle":
        return cls(
            runtime_name=d["runtime_name"],
            runtime_arn=d["runtime_arn"],
            runtime_id=d["runtime_id"],
            log_group=d["log_group"],
            service_name=d["service_name"],
            role_arn=d["role_arn"],
            region=d["region"],
            s3_bucket=d.get("s3_bucket"),
            s3_key=d.get("s3_key"),
            version=d.get("version", "v1"),
            account_id=d.get("account_id"),
        )


@dataclass
class BundleRef:
    """A reference to a specific configuration bundle version."""

    bundle_arn: str
    version_id: str
    bundle_id: str | None = None


@dataclass
class EvalTarget:
    """What a batch evaluation should score."""

    name: str
    service_name: str
    log_groups: list[str]
    session_ids: list[str] | None = None


@dataclass
class OnlineEvalConfig:
    name: str
    service_name: str
    log_groups: list[str]
    evaluators: list[str]
    role_arn: str
    sampling_percentage: float = 100.0
    session_timeout_minutes: int = 2
    description: str = ""


@dataclass
class OnlineEvalHandle:
    config_id: str
    config_arn: str
    name: str


@dataclass
class RecommendationRequest:
    name: str
    kind: str  # "system_prompt" | "tool_description"
    current_system_prompt: str | None = None
    current_tool_descriptions: dict[str, str] | None = None
    log_group_arns: list[str] = field(default_factory=list)
    service_names: list[str] = field(default_factory=list)
    target_evaluator_arn: str = "arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate"
    lookback_days: int = 7


@dataclass
class Recommendation:
    kind: str
    recommended_system_prompt: str | None = None
    recommended_tool_descriptions: dict[str, str] | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class GatewayHandle:
    gateway_id: str
    gateway_arn: str
    gateway_url: str


@dataclass
class GatewayTargetHandle:
    target_id: str
    name: str


@dataclass
class ABTestVariant:
    name: str
    weight: int
    bundle: BundleRef | None = None            # config-bundle variant
    target_endpoint: str | None = None          # target-based variant (gateway target name)


@dataclass
class ABTestSpec:
    name: str
    gateway_arn: str
    role_arn: str
    online_eval_arn: str
    variants: list[ABTestVariant]
    description: str = ""


@dataclass
class ABTestHandle:
    ab_test_id: str
    name: str


@dataclass
class PromotionDecision:
    """How to promote a winning variant (Requirement 6.4)."""

    strategy: str  # "config_bundle" | "target_based"
    ab_test_id: str | None = None
    # config_bundle strategy:
    bundle_id: str | None = None
    agent_arn: str | None = None
    config: BundleConfig | None = None
    parent_version_ids: list[str] = field(default_factory=list)
    commit_message: str = "Promote treatment (A/B validated)"


# ===========================================================================
# Pure helpers (importable / testable without boto3)
# ===========================================================================
def make_config_bundle_baggage(bundle_arn: str, bundle_version: str) -> str:
    """Build the W3C baggage header that injects a config bundle into a request."""
    return (
        f"aws.agentcore.configbundle_arn={bundle_arn},"
        f"aws.agentcore.configbundle_version={bundle_version}"
    )


def assign_variant(session_id: str, variants: Sequence[tuple[str, int]]) -> str:
    """Deterministically assign a session to a weighted variant (sticky).

    Uses a stable hash of ``session_id`` (SHA-256, not the salted built-in ``hash``)
    so the same session id always maps to the same variant across turns/processes —
    the property the A/B traffic driver relies on (Property 10 / Requirement 5.4).
    """
    weighted = [(name, int(w)) for name, w in variants if int(w) > 0]
    total = sum(w for _, w in weighted)
    if total <= 0:
        raise ValueError("variant weights must sum to a positive number")
    bucket = int(hashlib.sha256(session_id.encode("utf-8")).hexdigest(), 16) % total
    upto = 0
    for name, w in weighted:
        upto += w
        if bucket < upto:
            return name
    return weighted[-1][0]


def error_code(exc: Exception) -> str | None:
    """Extract an AWS error code from a botocore ClientError-like exception."""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = resp.get("Error", {}).get("Code")
        if code:
            return code
    return None


def is_retryable(exc: Exception) -> bool:
    return error_code(exc) in RETRYABLE_ERROR_CODES


def retry_call(
    fn: Callable[[], Any],
    *,
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    retryable: Callable[[Exception], bool] = is_retryable,
) -> Any:
    """Call ``fn`` with exponential backoff on retryable errors."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - re-raised below if not retryable/last
            last = exc
            if i == attempts - 1 or not retryable(exc):
                raise
            sleep(min(base_delay * (2 ** i), max_delay))
    assert last is not None  # unreachable
    raise last


def parse_batch_scores(result: dict[str, Any]) -> dict[str, float]:
    """Extract per-evaluator average scores from a get_batch_evaluation response."""
    scores: dict[str, float] = {}
    summaries = (result.get("evaluationResults", {}) or {}).get("evaluatorSummaries", []) or []
    for s in summaries:
        avg = (s.get("statistics", {}) or {}).get("averageScore")
        if avg is not None and s.get("evaluatorId"):
            scores[s["evaluatorId"]] = float(avg)
    return scores


def _evaluator_name(arn: str) -> str:
    return arn.split("/")[-1] if arn else arn


def _pct_change(control_mean: Any, variant_mean: Any, given: Any) -> float:
    if given is not None:
        return float(given)
    try:
        cm, vm = float(control_mean), float(variant_mean)
    except (TypeError, ValueError):
        return 0.0
    return ((vm - cm) / cm * 100.0) if cm != 0 else 0.0


def parse_ab_test_result(ab_test_id: str, ab_response: dict[str, Any]) -> ABTestResult:
    """Parse a get_ab_test response into an ABTestResult (metrics only).

    Winner selection and overall significance policy are refined by the A/B testing
    module (task 13); here we faithfully translate the per-evaluator, per-variant
    statistics and flag significance if any variant metric is marked significant.
    """
    results = ab_response.get("results", {}) or {}
    metrics = results.get("evaluatorMetrics", []) or []
    per_variant: dict[str, dict[str, EvalMetric]] = {}
    any_sig = False

    for m in metrics:
        name = _evaluator_name(m.get("evaluatorArn", ""))
        control = m.get("controlStats", {}) or {}
        control_mean = control.get("mean")
        if control_mean is not None:
            per_variant.setdefault("control", {})[name] = EvalMetric(mean=float(control_mean))
        for vr in m.get("variantResults", []) or []:
            variant_name = vr.get("name") or vr.get("variantName") or "treatment"
            ci = vr.get("confidenceInterval", {}) or {}
            sig = bool(vr.get("isSignificant", False))
            any_sig = any_sig or sig
            per_variant.setdefault(variant_name, {})[name] = EvalMetric(
                mean=float(vr.get("mean", 0.0) or 0.0),
                abs_change=float(vr.get("absoluteChange", 0.0) or 0.0),
                pct_change=_pct_change(control_mean, vr.get("mean"), vr.get("percentChange")),
                p_value=float(vr.get("pValue", 1.0) or 1.0),
                ci_low=float(ci.get("lower", 0.0) or 0.0),
                ci_high=float(ci.get("upper", 0.0) or 0.0),
                significant=sig,
            )
    return ABTestResult(ab_test_id=ab_test_id, per_variant=per_variant, winner=None, significant=any_sig)


def parse_system_prompt_recommendation(resp: dict[str, Any], fallback: str | None) -> Recommendation:
    rec = (resp.get("recommendationResult", {}) or {}).get("systemPromptRecommendationResult", {}) or {}
    return Recommendation(
        kind="system_prompt",
        recommended_system_prompt=rec.get("recommendedSystemPrompt") or fallback,
        error_code=rec.get("errorCode"),
        error_message=rec.get("errorMessage"),
        raw=resp,
    )


def parse_tool_description_recommendation(resp: dict[str, Any], current: dict[str, str]) -> Recommendation:
    result = (resp.get("recommendationResult", {}) or {}).get("toolDescriptionRecommendationResult", {}) or {}
    merged = dict(current)
    keys = list(current.keys())
    for i, item in enumerate(result.get("tools", []) or []):
        name = item.get("toolName") or (keys[i] if i < len(keys) else f"tool_{i}")
        desc = item.get("recommendedToolDescription")
        if desc:
            merged[name] = desc
    return Recommendation(
        kind="tool_description",
        recommended_tool_descriptions=merged,
        error_code=result.get("errorCode"),
        error_message=result.get("errorMessage"),
        raw=resp,
    )


def build_gateway_invoke(gateway_url: str, target_name: str, session_id: str, prompt: str) -> tuple[str, dict[str, str], bytes]:
    """Build the (url, headers, body) for a SigV4-signed gateway invocation.

    Pure/testable; signing and the HTTP POST happen in ``send_via_gateway``.
    """
    url = f"{gateway_url.rstrip('/')}/{target_name}/invocations"
    body = json.dumps({"prompt": prompt, "sessionId": session_id}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }
    return url, headers, body


# ===========================================================================
# The client
# ===========================================================================
class AgentCoreClient:
    """Thin wrapper over real AgentCore data-plane and control-plane APIs."""

    def __init__(
        self,
        config,
        *,
        session=None,
        dp=None,
        ctrl=None,
        logs=None,
        iam=None,
        s3=None,
        sts=None,
        xray=None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.region = getattr(config, "region", None)
        self._session = session
        self._clients: dict[str, Any] = {
            "bedrock-agentcore": dp,
            "bedrock-agentcore-control": ctrl,
            "logs": logs,
            "iam": iam,
            "s3": s3,
            "sts": sts,
            "xray": xray,
        }
        self._sleep = sleep

    # --- lazy client access ------------------------------------------------
    @property
    def session(self):
        if self._session is None:
            self._session = self.config.create_session()
        return self._session

    def _client(self, name: str):
        c = self._clients.get(name)
        if c is None:
            c = self.session.client(name, region_name=self.region)
            self._clients[name] = c
        return c

    @property
    def dp(self):
        return self._client("bedrock-agentcore")

    @property
    def ctrl(self):
        return self._client("bedrock-agentcore-control")

    @property
    def logs(self):
        return self._client("logs")

    @property
    def iam(self):
        return self._client("iam")

    @property
    def s3(self):
        return self._client("s3")

    @property
    def sts(self):
        return self._client("sts")

    @property
    def xray(self):
        return self._client("xray")

    @property
    def lam(self):
        return self._client("lambda")

    def _retry(self, fn: Callable[[], Any]) -> Any:
        return retry_call(fn, sleep=self._sleep)

    # =======================================================================
    # Agent / traffic (task 5.1)
    # =======================================================================
    def invoke_once(self, runtime_arn: str, session_id: str, prompt: str, baggage: str | None = None) -> str:
        """Invoke the runtime once and return the response text (data-plane)."""
        kwargs: dict[str, Any] = {
            "agentRuntimeArn": runtime_arn,
            "runtimeSessionId": session_id,
            "payload": json.dumps({"prompt": prompt}).encode("utf-8"),
        }
        if baggage:
            kwargs["baggage"] = baggage
        resp = self._retry(lambda: self.dp.invoke_agent_runtime(**kwargs))
        body = resp["response"].read()
        return body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)

    def send_session(
        self,
        agent: AgentHandle,
        turns: Sequence[str],
        session_id: str | None = None,
        bundle: BundleRef | None = None,
        tool_calls: Sequence[str] | None = None,
    ) -> SessionResult:
        """Drive one multi-turn session against the deployed agent (Requirement 1.6).

        All turns share a single ``runtimeSessionId`` so the runtime preserves
        conversation history across turns. Returns the final response; ``tool_calls``
        can be supplied by the caller (e.g. reconstructed from traces) for trajectory
        evaluation, and defaults to empty.
        """
        session_id = session_id or str(uuid.uuid4())
        baggage = make_config_bundle_baggage(bundle.bundle_arn, bundle.version_id) if bundle else None
        final = ""
        for turn in turns:
            final = self.invoke_once(agent.runtime_arn, session_id, turn, baggage)
        return SessionResult(session_id=session_id, final_response=final, tool_calls=list(tool_calls or []))

    def send_via_gateway(self, gateway_url: str, target_name: str, session_id: str, prompt: str, timeout: int = 120):
        """SigV4-sign and POST a single gateway invocation (used to drive A/B traffic)."""
        # Lazy imports: only needed for the live signed HTTP path.
        import requests as http_requests
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        url, headers, body = build_gateway_invoke(gateway_url, target_name, session_id, prompt)
        creds = self.session.get_credentials().get_frozen_credentials()
        signed = AWSRequest(method="POST", url=url, data=body, headers=headers)
        SigV4Auth(creds, "bedrock-agentcore", self.region).add_auth(signed)
        return http_requests.post(url, data=body, headers=dict(signed.headers), timeout=timeout)

    def deploy_agent(self, name: str, version: str = "v1", agent_source: str | None = None) -> AgentHandle:
        """Deploy the patient-support agent to AgentCore Runtime (adapts deploy.py).

        Builds an ARM64 dependency package containing the agent as ``main.py``, uploads
        it to S3, creates the runtime, and polls until ACTIVE/READY. Requires AWS
        access and network; not exercised by unit tests.
        """
        from .deploy import deploy_runtime  # local import keeps heavy deploy logic optional

        return deploy_runtime(self, name=name, version=version, agent_source=agent_source)

    # =======================================================================
    # Offline (batch) evaluation (task 5.2)
    # =======================================================================
    def run_batch_evaluation(self, target: EvalTarget, evaluators: Sequence[str], wait: bool = True, poll_seconds: int = 30) -> dict[str, Any]:
        """Start a batch evaluation and (optionally) poll to completion.

        Returns a dict with ``batch_evaluation_id``, ``status``, ``scores`` (per-evaluator
        averages), and the raw ``result``.
        """
        cw: dict[str, Any] = {"serviceNames": [target.service_name], "logGroupNames": list(target.log_groups)}
        if target.session_ids:
            cw["filterConfig"] = {"sessionIds": list(target.session_ids)}
        resp = self._retry(
            lambda: self.dp.start_batch_evaluation(
                batchEvaluationName=target.name,
                evaluators=[{"evaluatorId": e} for e in evaluators],
                dataSourceConfig={"cloudWatchLogs": cw},
                clientToken=str(uuid.uuid4()),
            )
        )
        eval_id = resp["batchEvaluationId"]
        if not wait:
            return {"batch_evaluation_id": eval_id, "status": "STARTED", "scores": {}, "result": resp}

        result = self._poll(lambda: self.dp.get_batch_evaluation(batchEvaluationId=eval_id), BATCH_TERMINAL, poll_seconds)
        return {
            "batch_evaluation_id": eval_id,
            "status": result.get("status"),
            "scores": parse_batch_scores(result),
            "result": result,
        }

    def _poll(self, fn: Callable[[], dict[str, Any]], terminal: frozenset[str], poll_seconds: int, max_polls: int = 120) -> dict[str, Any]:
        for _ in range(max_polls):
            result = self._retry(fn)
            if result.get("status") in terminal:
                return result
            self._sleep(poll_seconds)
        return result  # last observed

    # =======================================================================
    # Online evaluation (task 5.2)
    # =======================================================================
    # --- code-based (Lambda) evaluators ---
    def deploy_code_evaluator(self, function_name: str, source_dir, level: str = "SESSION", timeout_s: int = 60, evaluator_name: str | None = None) -> str:
        """Deploy the evaluator Lambda and register it; return the evaluator id."""
        from .lambda_deploy import deploy_lambda_evaluator, ensure_lambda_role

        role_arn = ensure_lambda_role(self)
        lambda_arn = deploy_lambda_evaluator(self, function_name, source_dir, role_arn, timeout_s)
        return self.create_code_evaluator(evaluator_name or function_name.replace("-", "_"), lambda_arn, level, timeout_s)

    def create_code_evaluator(self, name: str, lambda_arn: str, level: str = "SESSION", timeout_s: int = 60) -> str:
        resp = self._retry(
            lambda: self.ctrl.create_evaluator(
                evaluatorName=name,
                level=level,
                evaluatorConfig={"codeBased": {"lambdaConfig": {"lambdaArn": lambda_arn, "lambdaTimeoutInSeconds": timeout_s}}},
            )
        )
        return resp["evaluatorId"]

    def create_online_eval(self, cfg: OnlineEvalConfig) -> OnlineEvalHandle:
        resp = self._retry(
            lambda: self.ctrl.create_online_evaluation_config(
                onlineEvaluationConfigName=cfg.name,
                description=cfg.description or f"{cfg.name} online evaluation",
                dataSourceConfig={"cloudWatchLogs": {"logGroupNames": list(cfg.log_groups), "serviceNames": [cfg.service_name]}},
                evaluators=[{"evaluatorId": e} for e in cfg.evaluators],
                rule={
                    "samplingConfig": {"samplingPercentage": float(cfg.sampling_percentage)},
                    "sessionConfig": {"sessionTimeoutMinutes": int(cfg.session_timeout_minutes)},
                },
                evaluationExecutionRoleArn=cfg.role_arn,
                enableOnCreate=True,
                clientToken=str(uuid.uuid4()),
            )
        )
        return OnlineEvalHandle(config_id=resp["onlineEvaluationConfigId"], config_arn=resp["onlineEvaluationConfigArn"], name=cfg.name)

    def get_online_scores(self, handle: OnlineEvalHandle) -> dict[str, Any]:
        return self._retry(lambda: self.ctrl.get_online_evaluation_config(onlineEvaluationConfigId=handle.config_id))

    def disable_online_eval(self, handle: OnlineEvalHandle) -> None:
        """Stop an online evaluation by disabling it (Requirement 3.7)."""
        self._retry(lambda: self.ctrl.update_online_evaluation_config(onlineEvaluationConfigId=handle.config_id, executionStatus="DISABLED"))

    def delete_online_eval(self, handle: OnlineEvalHandle) -> None:
        """Delete an online evaluation config (teardown)."""
        self._retry(lambda: self.ctrl.delete_online_evaluation_config(onlineEvaluationConfigId=handle.config_id))

    # =======================================================================
    # Recommendations + bundles (task 5.2)
    # =======================================================================
    def start_recommendation(self, req: RecommendationRequest, wait: bool = True, poll_seconds: int = 30) -> Recommendation:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        start_dt = now - timedelta(days=req.lookback_days)
        traces = {"cloudwatchLogs": {"logGroupArns": list(req.log_group_arns), "serviceNames": list(req.service_names), "startTime": start_dt, "endTime": now}}

        if req.kind == "system_prompt":
            rec_type = "SYSTEM_PROMPT_RECOMMENDATION"
            rec_cfg = {
                "systemPromptRecommendationConfig": {
                    "systemPrompt": {"text": req.current_system_prompt or ""},
                    "agentTraces": traces,
                    "evaluationConfig": {"evaluators": [{"evaluatorArn": req.target_evaluator_arn}]},
                }
            }
        elif req.kind == "tool_description":
            rec_type = "TOOL_DESCRIPTION_RECOMMENDATION"
            tools = [{"toolName": n, "toolDescription": {"text": d}} for n, d in (req.current_tool_descriptions or {}).items()]
            rec_cfg = {"toolDescriptionRecommendationConfig": {"toolDescription": {"toolDescriptionText": {"tools": tools}}, "agentTraces": traces}}
        else:
            raise ValueError(f"unknown recommendation kind {req.kind!r}")

        resp = self._retry(lambda: self.dp.start_recommendation(name=req.name, type=rec_type, recommendationConfig=rec_cfg, clientToken=str(uuid.uuid4())))
        rec_id = resp["recommendationId"]
        if not wait:
            return Recommendation(kind=req.kind, raw={"recommendationId": rec_id})

        result = self._poll(lambda: self.dp.get_recommendation(recommendationId=rec_id), REC_TERMINAL, poll_seconds)
        if req.kind == "system_prompt":
            return parse_system_prompt_recommendation(result, req.current_system_prompt)
        return parse_tool_description_recommendation(result, req.current_tool_descriptions or {})

    def create_bundle_version(self, bundle_name: str, agent_arn: str, config: BundleConfig, commit_message: str, description: str = "") -> BundleVersion:
        resp = self._retry(
            lambda: self.ctrl.create_configuration_bundle(
                bundleName=bundle_name,
                description=description or bundle_name,
                components={agent_arn: {"configuration": self._bundle_configuration(config)}},
                commitMessage=commit_message,
                clientToken=str(uuid.uuid4()),
            )
        )
        return BundleVersion(bundle_id=resp["bundleId"], version_id=resp["versionId"], parent_version_ids=[], commit_message=commit_message, config=config, bundle_arn=resp.get("bundleArn"))

    def update_bundle_version(self, bundle_id: str, agent_arn: str, config: BundleConfig, parent_version_ids: Sequence[str], commit_message: str) -> BundleVersion:
        resp = self._retry(
            lambda: self.ctrl.update_configuration_bundle(
                bundleId=bundle_id,
                components={agent_arn: {"configuration": self._bundle_configuration(config)}},
                parentVersionIds=list(parent_version_ids),
                commitMessage=commit_message,
                clientToken=str(uuid.uuid4()),
            )
        )
        return BundleVersion(bundle_id=bundle_id, version_id=resp["versionId"], parent_version_ids=list(parent_version_ids), commit_message=commit_message, config=config, bundle_arn=resp.get("bundleArn"))

    @staticmethod
    def _bundle_configuration(config: BundleConfig) -> dict[str, Any]:
        cfg: dict[str, Any] = {"system_prompt": config.system_prompt, "tool_descriptions": dict(config.tool_descriptions)}
        if config.model_id:
            cfg["model_id"] = config.model_id
        return cfg

    def get_bundle(self, bundle_id: str) -> dict[str, Any]:
        return self._retry(lambda: self.ctrl.get_configuration_bundle(bundleId=bundle_id))

    def list_bundle_versions(self, bundle_id: str) -> list[dict[str, Any]]:
        resp = self._retry(lambda: self.ctrl.list_configuration_bundle_versions(bundleId=bundle_id))
        return resp.get("versions", resp.get("bundleVersions", [])) or []

    # =======================================================================
    # Gateway + targets (needed for A/B routing)
    # =======================================================================
    def create_gateway(self, name: str, role_arn: str, description: str = "", poll_seconds: int = 5) -> GatewayHandle:
        resp = self._retry(
            lambda: self.ctrl.create_gateway(
                name=name,
                description=description or f"{name} A/B gateway",
                authorizerType="AWS_IAM",
                roleArn=role_arn,
                clientToken=str(uuid.uuid4()),
            )
        )
        gid = resp["gatewayId"]
        gw = self._poll(lambda: self.ctrl.get_gateway(gatewayIdentifier=gid), frozenset({"READY"}), poll_seconds)
        arn = gw.get("gatewayArn") or resp.get("gatewayArn")
        url = gw.get("gatewayUrl") or resp.get("gatewayUrl")
        return GatewayHandle(gateway_id=gid, gateway_arn=arn, gateway_url=url)

    def create_gateway_target(self, gateway_id: str, name: str, runtime_arn: str, qualifier: str = "DEFAULT", poll_seconds: int = 5) -> GatewayTargetHandle:
        resp = self._retry(
            lambda: self.ctrl.create_gateway_target(
                gatewayIdentifier=gateway_id,
                name=name,
                description=f"{name} runtime target",
                targetConfiguration={"http": {"agentcoreRuntime": {"arn": runtime_arn, "qualifier": qualifier}}},
                credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
                clientToken=str(uuid.uuid4()),
            )
        )
        tid = resp["targetId"]
        self._poll(lambda: self.ctrl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=tid), frozenset({"READY"}), poll_seconds)
        return GatewayTargetHandle(target_id=tid, name=name)

    def delete_gateway_target(self, gateway_id: str, target_id: str) -> None:
        self._retry(lambda: self.ctrl.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id))

    def delete_gateway(self, gateway_id: str) -> None:
        self._retry(lambda: self.ctrl.delete_gateway(gatewayIdentifier=gateway_id))

    def delete_runtime(self, runtime_id: str) -> None:
        self._retry(lambda: self.ctrl.delete_agent_runtime(agentRuntimeId=runtime_id))

    def delete_bundle(self, bundle_id: str) -> None:
        self._retry(lambda: self.ctrl.delete_configuration_bundle(bundleId=bundle_id))

    def _run_logs_query(self, log_group: str, start: int, end: int, query: str, max_polls: int = 40) -> list[dict[str, str]]:
        """Run a CloudWatch Logs Insights query and return rows as {field: value} dicts."""
        started = self._retry(lambda: self.logs.start_query(logGroupName=log_group, startTime=start, endTime=end, queryString=query))
        qid = started["queryId"]
        result: dict = {}
        for _ in range(max_polls):
            result = self._retry(lambda: self.logs.get_query_results(queryId=qid))
            if result.get("status") == "Complete":
                break
            self._sleep(1)
        return [{f.get("field"): f.get("value") for f in row} for row in result.get("results", [])]

    def fetch_session_tool_calls(self, session_id: str, spans_log_group: str = SPANS_LOG_GROUP, lookback_minutes: int = 60) -> list[str]:
        """Reconstruct the ordered tool calls for a runtime session from OTel spans.

        Strands emits one ``execute_tool <name>`` span per tool call (attribute
        ``gen_ai.tool.name``); the session id lives on the invoke/server spans as
        ``attributes.session.id``, which share a ``traceId`` with their tool spans.
        So: session.id -> traceId(s) -> execute_tool spans ordered by start time.

        Returns an empty list on any error (evaluation degrades gracefully).
        """
        import time as _t

        try:
            end = int(_t.time())
            start = end - lookback_minutes * 60

            trace_rows = self._run_logs_query(
                spans_log_group, start, end,
                f"fields traceId | filter attributes.session.id = '{session_id}' | sort @timestamp asc | limit 200",
            )
            trace_ids: list[str] = []
            for row in trace_rows:
                tid = row.get("traceId")
                if tid and tid not in trace_ids:
                    trace_ids.append(tid)
            if not trace_ids:
                return []

            cond = " or ".join(f"traceId = '{t}'" for t in trace_ids)
            tool_rows = self._run_logs_query(
                spans_log_group, start, end,
                (
                    "fields attributes.gen_ai.tool.name as toolName, startTimeUnixNano as t "
                    f"| filter attributes.gen_ai.operation.name = 'execute_tool' and ({cond}) "
                    "| sort t asc | limit 200"
                ),
            )
            return [row["toolName"] for row in tool_rows if row.get("toolName")]
        except Exception:  # noqa: BLE001 - best-effort trace reconstruction
            return []

    # =======================================================================
    # A/B testing (task 5.2)
    # =======================================================================
    def start_ab_test(self, spec: ABTestSpec) -> ABTestHandle:
        resp = self._retry(
            lambda: self.dp.create_ab_test(
                name=spec.name,
                description=spec.description or spec.name,
                gatewayArn=spec.gateway_arn,
                roleArn=spec.role_arn,
                enableOnCreate=True,
                evaluationConfig={"onlineEvaluationConfigArn": spec.online_eval_arn},
                variants=[self._variant_payload(v) for v in spec.variants],
                clientToken=str(uuid.uuid4()),
            )
        )
        return ABTestHandle(ab_test_id=resp["abTestId"], name=spec.name)

    @staticmethod
    def _variant_payload(v: ABTestVariant) -> dict[str, Any]:
        if v.bundle is not None:
            variant_cfg = {"configurationBundle": {"bundleArn": v.bundle.bundle_arn, "bundleVersion": v.bundle.version_id}}
        elif v.target_endpoint is not None:
            variant_cfg = {"gatewayTarget": {"targetName": v.target_endpoint}}
        else:
            raise ValueError(f"variant {v.name!r} must set either bundle or target_endpoint")
        return {"name": v.name, "weight": int(v.weight), "variantConfiguration": variant_cfg}

    def get_ab_test(self, handle: ABTestHandle) -> ABTestResult:
        resp = self._retry(lambda: self.dp.get_ab_test(abTestId=handle.ab_test_id))
        return parse_ab_test_result(handle.ab_test_id, resp)

    def get_ab_test_raw(self, ab_test_id: str) -> dict[str, Any]:
        return self._retry(lambda: self.dp.get_ab_test(abTestId=ab_test_id))

    def stop_ab_test(self, handle: ABTestHandle) -> None:
        self._retry(lambda: self.dp.update_ab_test(abTestId=handle.ab_test_id, executionStatus="STOPPED"))

    # =======================================================================
    # Promotion primitive (task 5.2 / used by the promotion gate, task 15)
    # =======================================================================
    def promote_variant(self, decision: PromotionDecision) -> None:
        """Execute a promotion (Requirement 6.4).

        config_bundle: write the winning configuration into the baseline bundle as a
        new version (records parent lineage). target_based: stop the A/B test so a
        full cutover to the winning runtime can proceed.
        """
        if decision.strategy == "config_bundle":
            if not (decision.bundle_id and decision.agent_arn and decision.config is not None):
                raise ValueError("config_bundle promotion requires bundle_id, agent_arn, and config")
            parents = decision.parent_version_ids
            if not parents:
                current = self.get_bundle(decision.bundle_id)
                if current.get("versionId"):
                    parents = [current["versionId"]]
            self.update_bundle_version(decision.bundle_id, decision.agent_arn, decision.config, parents, decision.commit_message)
        elif decision.strategy == "target_based":
            if not decision.ab_test_id:
                raise ValueError("target_based promotion requires ab_test_id")
            self.stop_ab_test(ABTestHandle(ab_test_id=decision.ab_test_id, name=decision.ab_test_id))
        else:
            raise ValueError(f"unknown promotion strategy {decision.strategy!r}")
