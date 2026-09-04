"""Report delivery tests (tasks 9.2, 9.3).

Property 9 (report delivery best-effort) plus SES/SMTP/outbox path unit tests.
Requirements: 2.11, 2.13, 2.14, 8.6
"""

import tempfile
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from agentcore_demo.config import EmailConfig
from agentcore_demo.models import EvaluationReport
from agentcore_demo.reporting.emailer import DeliveryResult, deliver_report
from agentcore_demo.state import StateStore


def _report():
    return EvaluationReport(
        baseline_name="baseline_v1",
        generated_at="2026-01-01T00:00:00+00:00",
        aggregate_scores={"Custom.Trajectory": 0.9, "Builtin.GoalSuccessRate": 0.8},
        case_count=12,
        per_case=[],
    )


def _store(tmp) -> StateStore:
    return StateStore(Path(tmp) / "artifacts")


class _RecordingSender:
    def __init__(self, raises=False):
        self.raises = raises
        self.calls = []

    def __call__(self, sender, target, subject, html_body, text_body):
        self.calls.append((sender, target, subject, html_body, text_body))
        if self.raises:
            raise RuntimeError("transport failure")


# --------------------------------------------------------------------------
# Property 9: Report delivery best-effort
# Validates: Requirements 2.13, 2.14
# --------------------------------------------------------------------------
@given(mechanism=st.sampled_from(["ses", "smtp", "outbox"]), raises=st.booleans())
def test_property_delivery_is_best_effort(mechanism, raises):
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        email = EmailConfig(
            delivery=mechanism,
            target=None if mechanism == "outbox" else "team@example.com",
            sender="demo@example.com",
            smtp_host="smtp.example.com",
        )
        sender = _RecordingSender(raises=raises)
        # deliver_report must never raise, regardless of mechanism or sender failure.
        result = deliver_report(_report(), email, store, ses_sender=sender, smtp_sender=sender)
        assert isinstance(result, DeliveryResult)
        if result.delivered:
            # Only possible for a configured mechanism with a successful send.
            assert result.error is None and mechanism in ("ses", "smtp") and not raises
        else:
            # Not delivered by email -> a local copy always exists.
            assert result.outbox_path and Path(result.outbox_path).exists()


# --------------------------------------------------------------------------
# Outbox default (email not configured)
# --------------------------------------------------------------------------
def test_outbox_default_when_email_not_configured():
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        report = _report()
        result = deliver_report(report, EmailConfig(delivery="outbox"), store)
        assert result.mechanism == "outbox" and result.delivered is False and result.error is None
        assert Path(result.outbox_path).exists()
        assert report.delivery == "outbox"


def test_ses_configured_without_target_falls_back_to_outbox():
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        result = deliver_report(_report(), EmailConfig(delivery="ses", target=None), store, ses_sender=_RecordingSender())
        assert result.mechanism == "outbox" and result.delivered is False


# --------------------------------------------------------------------------
# Successful sends
# --------------------------------------------------------------------------
def test_ses_success_marks_delivered_and_passes_content():
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        report = _report()
        sender = _RecordingSender()
        email = EmailConfig(delivery="ses", target="team@example.com", sender="demo@example.com")
        result = deliver_report(report, email, store, ses_sender=sender)
        assert result.delivered is True and result.mechanism == "ses" and result.target == "team@example.com"
        assert report.delivered_to == "team@example.com" and report.delivery == "ses"
        # sender received (sender, target, subject, html, text)
        s, t, subj, html, text = sender.calls[0]
        assert t == "team@example.com" and "baseline_v1" in subj and "baseline_v1" in html


def test_smtp_success_uses_smtp_sender():
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        sender = _RecordingSender()
        email = EmailConfig(delivery="smtp", target="ops@example.com", sender="demo@example.com", smtp_host="smtp.example.com")
        result = deliver_report(_report(), email, store, smtp_sender=sender)
        assert result.delivered is True and result.mechanism == "smtp"
        assert len(sender.calls) == 1


# --------------------------------------------------------------------------
# Failure fallback
# --------------------------------------------------------------------------
def test_send_failure_falls_back_to_outbox_and_records_error():
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        report = _report()
        sender = _RecordingSender(raises=True)
        email = EmailConfig(delivery="ses", target="team@example.com", sender="demo@example.com")
        result = deliver_report(report, email, store, ses_sender=sender)
        assert result.delivered is False
        assert result.mechanism == "outbox"
        assert result.error and "transport failure" in result.error
        assert Path(result.outbox_path).exists()
        assert report.delivery == "outbox"  # not marked as emailed
