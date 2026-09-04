"""Evaluation-report delivery (Requirements 2.11-2.14, 8.6).

Delivers a rendered :class:`EvaluationReport` to the configured target via ``ses`` or
``smtp``. When email is not configured (``delivery: outbox`` or no target), the report
is written to a local outbox instead of being sent.

Best-effort policy (Property 9 / Requirements 2.13, 2.14): delivery never aborts the
run. On any send failure the error is captured and the rendered report is written to
the local outbox, so a copy is always kept locally.

Delivery mechanisms and their transports are injectable, so unit tests exercise the
SES, SMTP, and outbox paths (including failures) without touching AWS or a mail server.
Secrets (SMTP credentials) are read from the environment at send time, never from config.
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import logging
import os
import re
from dataclasses import dataclass
from typing import Callable

from ..config import EmailConfig
from ..models import EvaluationReport
from ..state import StateStore

logger = logging.getLogger(__name__)

# A sender raises on failure; returns None on success.
Sender = Callable[[str, str, str, str, str], None]  # (sender, target, subject, html_body, text_body)


@dataclass
class DeliveryResult:
    """Outcome of attempting to deliver a report."""

    mechanism: str            # final mechanism: "ses" | "smtp" | "outbox"
    delivered: bool           # True only if an email was actually sent
    target: str | None
    outbox_path: str | None
    error: str | None


def _safe_stem(report: EvaluationReport) -> str:
    ts = re.sub(r"[^0-9A-Za-z]", "", report.generated_at)[:14]
    name = re.sub(r"[^0-9A-Za-z._-]", "_", report.baseline_name)
    return f"{name}_{ts}" if ts else name


def render_summary_html(report: EvaluationReport) -> str:
    def esc(x) -> str:
        return _html.escape(str(x))

    rows = "".join(f"<tr><td>{esc(n)}</td><td>{s:.3f}</td></tr>" for n, s in sorted(report.aggregate_scores.items()))
    return (
        "<!doctype html><html><body>"
        f"<h1>Offline evaluation: {esc(report.baseline_name)}</h1>"
        f"<p>Generated: {esc(report.generated_at)}<br>Cases evaluated: {esc(report.case_count)}</p>"
        f"<h2>Aggregate scores</h2><table border='1'><tr><th>Evaluator</th><th>Mean</th></tr>{rows}</table>"
        "</body></html>"
    )


def render_summary_text(report: EvaluationReport) -> str:
    lines = [
        f"Offline evaluation: {report.baseline_name}",
        f"Generated: {report.generated_at}",
        f"Cases evaluated: {report.case_count}",
        "",
        "Aggregate scores:",
    ]
    for n, s in sorted(report.aggregate_scores.items()):
        lines.append(f"  {n}: {s:.3f}")
    return "\n".join(lines) + "\n"


def deliver_report(
    report: EvaluationReport,
    email: EmailConfig,
    store: StateStore,
    *,
    subject: str | None = None,
    html_body: str | None = None,
    text_body: str | None = None,
    ses_sender: Sender | None = None,
    smtp_sender: Sender | None = None,
) -> DeliveryResult:
    """Deliver ``report`` best-effort; always keep a local copy; never raise."""
    subject = subject or f"Offline evaluation report: {report.baseline_name}"
    html_body = html_body if html_body is not None else render_summary_html(report)
    text_body = text_body if text_body is not None else render_summary_text(report)
    outbox_filename = f"{_safe_stem(report)}.html"

    # Email requested and addressable: attempt a real send.
    if email.email_enabled:
        sender = _resolve_sender(email, ses_sender, smtp_sender)
        try:
            sender(email.sender or "", email.target or "", subject, html_body, text_body)
            report.delivery = email.delivery
            report.delivered_to = email.target
            return DeliveryResult(mechanism=email.delivery, delivered=True, target=email.target, outbox_path=None, error=None)
        except Exception as exc:  # noqa: BLE001 - best-effort: fall back, never abort (P9)
            logger.warning("Report email delivery via %s failed: %s", email.delivery, exc)
            path = store.write_outbox(outbox_filename, html_body)
            report.delivery = "outbox"
            report.delivered_to = None
            return DeliveryResult(
                mechanism="outbox",
                delivered=False,
                target=email.target,
                outbox_path=str(path),
                error=f"{type(exc).__name__}: {exc}",
            )

    # Email not configured: write to the local outbox to demonstrate the behavior.
    path = store.write_outbox(outbox_filename, html_body)
    report.delivery = "outbox"
    report.delivered_to = None
    return DeliveryResult(mechanism="outbox", delivered=False, target=None, outbox_path=str(path), error=None)


def _resolve_sender(email: EmailConfig, ses_sender: Sender | None, smtp_sender: Sender | None) -> Sender:
    if email.delivery == "ses":
        return ses_sender or _default_ses_sender()
    if email.delivery == "smtp":
        return smtp_sender or _default_smtp_sender(email)
    # Should not happen (email_enabled guards delivery in {ses, smtp}); be safe.
    raise ValueError(f"unsupported email delivery mechanism: {email.delivery}")


def _default_ses_sender() -> Sender:  # pragma: no cover - requires AWS SES
    def send(sender: str, target: str, subject: str, html_body: str, text_body: str) -> None:
        import boto3

        ses = boto3.client("ses", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
        ses.send_email(
            Source=sender,
            Destination={"ToAddresses": [target]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Html": {"Data": html_body}, "Text": {"Data": text_body}},
            },
        )

    return send


def _default_smtp_sender(email: EmailConfig) -> Sender:  # pragma: no cover - requires SMTP server
    def send(sender: str, target: str, subject: str, html_body: str, text_body: str) -> None:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        if not email.smtp_host:
            raise ValueError("email.smtp_host is required for smtp delivery")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = target
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP(email.smtp_host, email.smtp_port) as server:
            if email.smtp_use_tls:
                server.starttls()
            username = os.environ.get("SMTP_USERNAME")
            password = os.environ.get("SMTP_PASSWORD")
            if username and password:
                server.login(username, password)
            server.sendmail(sender, [target], msg.as_string())

    return send
