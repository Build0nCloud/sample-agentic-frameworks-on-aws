"""Unit tests for agentcore_demo.config (task 2.3).

Covers optional-email fallback to outbox, credential-chain usage (no hardcoded
secrets), region env override, path resolution, and validation.
Requirements: 8.1, 8.2, 8.6, 2.12
"""

from pathlib import Path

import pytest

from agentcore_demo import config as cfg


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "demo_config.yaml"
    p.write_text(text)
    return p


def test_defaults_and_email_outbox_fallback(tmp_path):
    # Minimal config with no email section -> delivery defaults to outbox, not enabled.
    c = cfg.load_config(_write(tmp_path, "aws:\n  region: us-west-2\n"))
    assert c.region == "us-west-2"
    assert c.email.delivery == "outbox"
    assert c.email.email_enabled is False
    assert c.email.target is None
    # Built-in defaults still present.
    assert c.runtime_name == "PatientSupport"
    assert "Builtin.GoalSuccessRate" in c.evaluators


def test_email_enabled_only_when_target_and_mechanism(tmp_path):
    text = (
        "aws: {region: us-east-1}\n"
        "email:\n"
        "  delivery: ses\n"
        "  target: team@example.com\n"
        "  sender: demo@example.com\n"
    )
    c = cfg.load_config(_write(tmp_path, text))
    assert c.email.delivery == "ses"
    assert c.email.email_enabled is True
    assert c.email.target == "team@example.com"


def test_email_ses_without_target_is_not_enabled(tmp_path):
    text = "aws: {region: us-east-1}\nemail: {delivery: ses}\n"
    c = cfg.load_config(_write(tmp_path, text))
    assert c.email.email_enabled is False


def test_invalid_delivery_raises(tmp_path):
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(_write(tmp_path, "email: {delivery: carrier_pigeon}\n"))


def test_region_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    c = cfg.load_config(_write(tmp_path, "aws:\n  region: us-east-1\n"))
    assert c.region == "eu-central-1"


def test_dataset_paths_resolved_absolute(tmp_path):
    c = cfg.load_config(_write(tmp_path, "datasets:\n  offline: datasets/x.jsonl\n"))
    assert c.offline_dataset.is_absolute()
    assert str(c.offline_dataset).endswith("datasets/x.jsonl")


def test_invalid_sampling_and_weights_raise(tmp_path):
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(_write(tmp_path, "online_eval: {sampling_percentage: 150}\n"))
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(_write(tmp_path, "ab_testing: {control_weight: 200}\n"))


def test_missing_file_raises():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config("/nonexistent/demo_config.yaml")


def test_shipped_config_loads_and_has_no_hardcoded_secrets():
    # The real shipped config must load, and must not contain credentials
    # (Requirement 8.2: credentials come from the standard chain, never config).
    c = cfg.load_config(cfg.DEFAULT_CONFIG_PATH)
    assert c.region
    # Email delivery mechanism is a supported value (an address is not a secret;
    # SES uses the AWS chain and SMTP creds come from the environment).
    assert c.email.delivery in cfg.DELIVERY_MECHANISMS
    raw_text = cfg.DEFAULT_CONFIG_PATH.read_text().lower()
    for secret_key in ("aws_access_key", "aws_secret_access_key", "secret_access_key", "password:"):
        assert secret_key not in raw_text
