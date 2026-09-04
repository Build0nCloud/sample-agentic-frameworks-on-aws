"""Centralized configuration for the AgentCore optimization demo.

All configurable values live in a single documented source (``config/demo_config.yaml``)
so there are no scattered constants (Requirement 8.1). AWS credentials are resolved
via the standard AWS credential chain and are never read from config or hardcoded
(Requirement 8.2). Report email settings are optional; when email is not configured,
delivery falls back to a local outbox (Requirements 8.6, 2.12, 2.14).

Precedence for a value: explicit ``load_config`` argument > environment override
(only for a small set of well-known keys) > YAML file > built-in default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Repo root = three levels up from this file: src/agentcore_demo/config.py -> repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "demo_config.yaml"

# Environment variable that can point at an alternate config file.
CONFIG_PATH_ENV = "AGENTCORE_DEMO_CONFIG"

# Valid report-delivery mechanisms. ``outbox`` writes the rendered report to a local
# directory instead of sending email, and is the default when email is not configured.
DELIVERY_MECHANISMS = ("ses", "smtp", "outbox")


class ConfigError(ValueError):
    """Raised when the configuration is missing required values or is malformed."""


@dataclass
class EmailConfig:
    """Report email settings (Requirements 2.11, 2.12, 8.6).

    Optional by design: when ``delivery`` is ``outbox`` (the default) or no target is
    set, the emailer writes the rendered report to the local outbox instead of sending.
    Secrets (e.g. SMTP credentials) are never stored here; the emailer resolves them
    from the environment at send time.
    """

    delivery: str = "outbox"
    target: str | None = None
    sender: str | None = None
    # SMTP transport settings (used only when delivery == "smtp"); credentials come
    # from the environment (SMTP_USERNAME / SMTP_PASSWORD), never from config.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_use_tls: bool = True

    def __post_init__(self) -> None:
        self.delivery = (self.delivery or "outbox").lower()
        if self.delivery not in DELIVERY_MECHANISMS:
            raise ConfigError(
                f"email.delivery must be one of {DELIVERY_MECHANISMS}, got {self.delivery!r}"
            )

    @property
    def email_enabled(self) -> bool:
        """True only when a real send is both requested and addressable."""
        return self.delivery in ("ses", "smtp") and bool(self.target)


@dataclass
class Config:
    """Resolved demo configuration."""

    # AWS
    region: str = "us-east-1"

    # Agent / runtime / gateway identifiers
    runtime_name: str = "PatientSupport"
    runtime_v2_name: str = "PatientSupportV2"
    gateway_name: str = "PatientSupportGateway"

    # Model (Claude Sonnet 5 US cross-region inference profile)
    model_id: str = "us.anthropic.claude-sonnet-5"

    # Optional deliberately-weak control/baseline system prompt (demo scenario). When set,
    # the control bundle uses this prompt so the recommended treatment has real headroom
    # to win on GoalSuccessRate. Leave unset for the agent's normal (strong) prompt.
    baseline_system_prompt: str | None = None

    # Evaluators applied by offline/online evaluation
    evaluators: list[str] = field(
        default_factory=lambda: [
            "Builtin.GoalSuccessRate",
            "Builtin.Helpfulness",
            "Builtin.Correctness",
        ]
    )

    # Dataset paths (resolved to absolute against the repo root)
    offline_dataset: Path = REPO_ROOT / "datasets" / "offline_multiturn.jsonl"
    traffic_dataset: Path = REPO_ROOT / "datasets" / "traffic_sessions.jsonl"

    # Online-eval sampling percentage (0-100)
    sampling_percentage: float = 100.0

    # A/B traffic split (config-bundle default 50/50; target-based canary 90/10)
    control_weight: int = 50
    treatment_weight: int = 50
    canary_control_weight: int = 90
    canary_treatment_weight: int = 10

    # Include the custom code-based trajectory evaluator in the A/B online evaluation
    # (Phase 2), so the A/B can score the tool-trajectory dimension, not just built-ins.
    enable_trajectory_evaluator: bool = False

    # Where generated artifacts are written
    artifacts_dir: Path = REPO_ROOT / "artifacts"

    # Timezone used to display timestamps in the UI (alongside UTC). An IANA name
    # (e.g. "America/Chicago", "Europe/London", "UTC") or "local" to use the machine's
    # local timezone. Stored timestamps are always UTC; this only affects display.
    display_timezone: str = "local"

    # Report email settings
    email: EmailConfig = field(default_factory=EmailConfig)

    # Original parsed mapping, for forward-compatibility / debugging.
    raw: dict[str, Any] = field(default_factory=dict)

    def create_session(self):  # pragma: no cover - thin boto3 wrapper
        """Create a boto3 Session for ``region`` using the standard credential chain.

        boto3 is imported lazily so config can be loaded and tested without it, and so
        no credentials are ever read here directly (Requirement 8.2).
        """
        import boto3  # local import keeps config importable without boto3 installed

        return boto3.Session(region_name=self.region)


def _resolve_path(value: Any, default: Path) -> Path:
    """Resolve a possibly-relative path (relative to the repo root) to absolute."""
    if value is None:
        return default
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p)


def _coerce_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load and validate the demo configuration.

    Args:
        path: explicit path to a YAML config file. If omitted, uses the
            ``AGENTCORE_DEMO_CONFIG`` environment variable, then the default
            ``config/demo_config.yaml``.

    Returns:
        A fully-resolved :class:`Config`.

    Raises:
        ConfigError: if the file is missing, unreadable, or malformed.
    """
    cfg_path = Path(path) if path else Path(os.environ.get(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH))
    if not cfg_path.exists():
        raise ConfigError(f"Config file not found: {cfg_path}")

    try:
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse YAML config {cfg_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Config root must be a mapping, got {type(raw).__name__}")

    aws = raw.get("aws", {}) or {}
    agent = raw.get("agent", {}) or {}
    ab = raw.get("ab_testing", {}) or {}
    online = raw.get("online_eval", {}) or {}
    datasets = raw.get("datasets", {}) or {}
    email_raw = raw.get("email", {}) or {}

    # Region: env override (standard AWS env vars) > yaml > default.
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or aws.get("region")
        or "us-east-1"
    )

    defaults = Config()  # for field defaults

    email = EmailConfig(
        delivery=email_raw.get("delivery", "outbox"),
        target=email_raw.get("target"),
        sender=email_raw.get("sender"),
        smtp_host=email_raw.get("smtp_host"),
        smtp_port=_coerce_int(email_raw.get("smtp_port", 587), "email.smtp_port"),
        smtp_use_tls=bool(email_raw.get("smtp_use_tls", True)),
    )

    evaluators = raw.get("evaluators") or defaults.evaluators

    config = Config(
        region=region,
        runtime_name=agent.get("runtime_name", defaults.runtime_name),
        runtime_v2_name=agent.get("runtime_v2_name", defaults.runtime_v2_name),
        gateway_name=agent.get("gateway_name", defaults.gateway_name),
        model_id=agent.get("model_id", defaults.model_id),
        baseline_system_prompt=agent.get("baseline_system_prompt", defaults.baseline_system_prompt),
        evaluators=list(evaluators),
        offline_dataset=_resolve_path(datasets.get("offline"), defaults.offline_dataset),
        traffic_dataset=_resolve_path(datasets.get("traffic"), defaults.traffic_dataset),
        sampling_percentage=float(online.get("sampling_percentage", defaults.sampling_percentage)),
        control_weight=_coerce_int(ab.get("control_weight", defaults.control_weight), "ab_testing.control_weight"),
        treatment_weight=_coerce_int(ab.get("treatment_weight", defaults.treatment_weight), "ab_testing.treatment_weight"),
        canary_control_weight=_coerce_int(
            ab.get("canary_control_weight", defaults.canary_control_weight), "ab_testing.canary_control_weight"
        ),
        canary_treatment_weight=_coerce_int(
            ab.get("canary_treatment_weight", defaults.canary_treatment_weight), "ab_testing.canary_treatment_weight"
        ),
        enable_trajectory_evaluator=bool(raw.get("enable_trajectory_evaluator", defaults.enable_trajectory_evaluator)),
        artifacts_dir=_resolve_path(raw.get("artifacts_dir"), defaults.artifacts_dir),
        display_timezone=str((raw.get("ui", {}) or {}).get("display_timezone") or raw.get("display_timezone") or defaults.display_timezone),
        email=email,
        raw=raw,
    )

    _validate(config)
    return config


def _validate(config: Config) -> None:
    """Sanity-check resolved values (Requirement 8.1)."""
    if not config.region:
        raise ConfigError("aws.region must be set")
    if not config.runtime_name:
        raise ConfigError("agent.runtime_name must be set")
    if not (0 <= config.sampling_percentage <= 100):
        raise ConfigError("online_eval.sampling_percentage must be within [0, 100]")
    for w_name, w in (
        ("control_weight", config.control_weight),
        ("treatment_weight", config.treatment_weight),
        ("canary_control_weight", config.canary_control_weight),
        ("canary_treatment_weight", config.canary_treatment_weight),
    ):
        if not (0 <= w <= 100):
            raise ConfigError(f"ab_testing.{w_name} must be within [0, 100], got {w}")
    if not config.evaluators:
        raise ConfigError("at least one evaluator must be configured")
    # Validate the display timezone resolves ("local" is always valid).
    if config.display_timezone.lower() != "local":
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(config.display_timezone)
        except Exception as exc:  # noqa: BLE001 - unknown zone / missing tzdata
            raise ConfigError(
                f"display_timezone {config.display_timezone!r} is not a valid IANA "
                f"timezone (or 'local'): {exc}"
            ) from exc
