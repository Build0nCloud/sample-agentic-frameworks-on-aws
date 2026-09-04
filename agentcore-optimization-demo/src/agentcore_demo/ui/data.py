"""Read-only data-access layer for the Streamlit front end.

This module owns *all* artifact parsing for the UI so the Streamlit app
(``app.py``) stays presentation-only. It reuses the same building blocks as the CLI —
:class:`StateStore`, the ``models`` dataclasses, and the ``orchestrator`` loop-state /
promotion-decision helpers — so the UI reflects exactly what the loop persisted rather
than a parallel interpretation.

Nothing here imports Streamlit (so it is unit-testable without a browser) and nothing
here mutates state or touches AWS. All live actions (approve/reject) live in
``actions.py`` and are invoked explicitly by the app.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import datetime as _dt
import json
import re

from ..config import load_config
from ..models import (
    ABTestResult,
    ApprovalRequest,
    ApprovalStatus,
    AuditEntry,
    Baseline,
    BundleConfig,
    BundleVersion,
    EvaluationReport,
)
from ..orchestrator import (
    DRAFT_SIDES,
    config_drafts_exist,
    draft_path,
    load_config_draft,
    load_loop_state,
    save_config_draft,
)
from ..state import StateStore

# A short list of known-good model ids offered in the Bundles-tab dropdown. Users can
# still enter a custom id; only region-enabled models will actually run.
KNOWN_MODEL_IDS = (
    "us.anthropic.claude-sonnet-5",
    "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "us.anthropic.claude-3-5-haiku-20241022-v1:0",
    "us.amazon.nova-pro-v1:0",
    "us.amazon.nova-lite-v1:0",
)


# The agent streams responses as server-sent events; persisted final_response text looks
# like:  data: "chunk"\n\ndata: " next"\n\n...  — join the quoted chunks back into prose.
_SSE_CHUNK = re.compile(r'data:\s*"((?:[^"\\]|\\.)*)"')


def clean_sse_text(text: str | None) -> str:
    """Decode an SSE-style ``data: "..."`` transcript into plain text.

    Falls back to returning the input unchanged if it isn't SSE-encoded.
    """
    if not text:
        return ""
    chunks = _SSE_CHUNK.findall(text)
    if not chunks:
        return text
    decoded = []
    for c in chunks:
        try:
            decoded.append(json.loads(f'"{c}"'))  # unescape \n, \", \uXXXX, etc.
        except json.JSONDecodeError:
            decoded.append(c)
    return "".join(decoded)


# ---------------------------------------------------------------------------
# Timestamp display: stored timestamps are always UTC ISO-8601; the UI shows them
# as UTC plus a configurable local timezone so times aren't confusing across zones.
# ---------------------------------------------------------------------------
def resolve_timezone(name: str) -> _dt.tzinfo:
    """Resolve a config ``display_timezone`` to a tzinfo.

    ``"local"`` (default) uses the machine's local zone; otherwise an IANA name such as
    ``"America/Chicago"``. Falls back to UTC if the name can't be resolved.
    """
    if not name or name.lower() == "local":
        return _dt.datetime.now().astimezone().tzinfo or _dt.timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown zone / missing tzdata
        return _dt.timezone.utc


def _parse_iso_utc(value: str) -> _dt.datetime | None:
    if not value:
        return None
    try:
        dt = _dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    # Treat naive timestamps as UTC (that's how the demo writes them).
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt


def tz_abbrev(tz: _dt.tzinfo, at: _dt.datetime | None = None) -> str:
    """A short label for a timezone (e.g. 'CDT', 'UTC'), best-effort."""
    at = at or _dt.datetime.now(_dt.timezone.utc)
    name = at.astimezone(tz).tzname()
    return name or "local"


def format_dual(value: str | None, tz: _dt.tzinfo, *, with_date: bool = True) -> str:
    """Format an ISO UTC timestamp as ``local (UTC)`` for display.

    Example: ``2026-09-01 17:26 CDT (22:26 UTC)``. Returns ``"—"`` for empty input and
    the raw value if it can't be parsed.
    """
    if not value:
        return "—"
    dt = _parse_iso_utc(value)
    if dt is None:
        return value
    utc = dt.astimezone(_dt.timezone.utc)
    local = dt.astimezone(tz)
    date_fmt = "%Y-%m-%d %H:%M" if with_date else "%H:%M"
    local_str = f"{local.strftime(date_fmt)} {tz_abbrev(tz, dt)}"
    utc_str = f"{utc.strftime('%H:%M')} UTC"
    return f"{local_str} ({utc_str})"


# ---------------------------------------------------------------------------
# View models — small, presentation-friendly shapes the app renders.
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class MetricRow:
    """One evaluator's control-vs-treatment comparison, flattened for a table."""

    evaluator: str
    control_mean: float | None
    treatment_mean: float | None
    abs_change: float
    pct_change: float
    p_value: float
    significant: bool

    @property
    def delta(self) -> float | None:
        if self.control_mean is None or self.treatment_mean is None:
            return None
        return self.treatment_mean - self.control_mean


@dataclasses.dataclass
class ABView:
    """A flattened A/B result ready for display."""

    ab_test_id: str
    winner: str | None
    significant: bool
    control_variant: str
    treatment_variant: str
    rows: list[MetricRow]

    @property
    def significant_evaluators(self) -> list[str]:
        return [r.evaluator for r in self.rows if r.significant]


class DemoData:
    """Facade over the ``artifacts/`` tree for the UI (read-only)."""

    def __init__(self, config_path: str | None = None):
        self.config = load_config(config_path)
        self.store = StateStore(self.config.artifacts_dir)
        self._loop_state: dict[str, Any] | None = None

    # --- config / meta ----------------------------------------------------
    @property
    def artifacts_dir(self) -> Path:
        return self.store.root

    @property
    def region(self) -> str:
        return getattr(self.config, "region", "") or ""

    @property
    def runtime_name(self) -> str:
        return getattr(self.config, "runtime_name", "") or ""

    @property
    def model_id(self) -> str:
        return getattr(self.config, "model_id", "") or ""

    @property
    def display_tz(self) -> _dt.tzinfo:
        return resolve_timezone(getattr(self.config, "display_timezone", "local"))

    def fmt_ts(self, value: str | None, *, with_date: bool = True) -> str:
        """Format a stored ISO UTC timestamp as 'local (UTC)' for display."""
        return format_dual(value, self.display_tz, with_date=with_date)

    # --- loop state -------------------------------------------------------
    def loop_state(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._loop_state is None or refresh:
            self._loop_state = load_loop_state(self.store)
        return self._loop_state

    def recommendation(self) -> dict[str, Any] | None:
        return self.loop_state().get("recommendation")

    def online_result(self) -> dict[str, Any] | None:
        return self.loop_state().get("online_result")

    def treatment_config(self) -> dict[str, Any] | None:
        """The candidate (treatment) bundle config produced by the `build-bundles` stage."""
        return self.loop_state().get("treatment_config")

    # --- editable config drafts (generate-config output) ------------------
    def drafts_exist(self) -> bool:
        return config_drafts_exist(self.store)

    def draft_config(self, side: str) -> dict[str, Any] | None:
        """Return the editable draft for ``side`` as {model_id, system_prompt}, or None."""
        cfg = load_config_draft(self.store, side)
        if cfg is None:
            return None
        return {"model_id": cfg.model_id, "system_prompt": cfg.system_prompt}

    def save_draft_config(self, side: str, *, model_id: str | None, system_prompt: str) -> None:
        """Persist an edited draft for ``side``, preserving its (non-editable) tool descriptions."""
        existing = load_config_draft(self.store, side)
        tool_descriptions = dict(existing.tool_descriptions) if existing else {}
        save_config_draft(
            self.store,
            side,
            BundleConfig(system_prompt=system_prompt, model_id=model_id or None, tool_descriptions=tool_descriptions),
        )

    def draft_file(self, side: str) -> str:
        return str(draft_path(self.store, side))

    @property
    def draft_sides(self) -> tuple[str, ...]:
        return tuple(DRAFT_SIDES)

    def baseline_prompt(self) -> str | None:
        """The configured (deliberately weak) control/baseline system prompt, if any.

        This is the 'before' the recommend step improves on. Falls back to what the
        bundle stage recorded as the control config if config has none.
        """
        cfg_prompt = getattr(self.config, "baseline_system_prompt", None)
        if cfg_prompt:
            return cfg_prompt
        control = self.loop_state().get("control_ref")  # no prompt stored on ref
        _ = control
        return None

    def gateway(self) -> dict[str, Any] | None:
        return self.loop_state().get("gateway")

    def trajectory_evaluator_id(self) -> str | None:
        return self.loop_state().get("trajectory_evaluator_id")

    # --- A/B result -------------------------------------------------------
    def ab_result(self) -> ABTestResult | None:
        raw = self.loop_state().get("ab_result")
        if not raw:
            return None
        return ABTestResult.from_dict(raw)

    def ab_view(self) -> ABView | None:
        """Flatten the persisted A/B result into a display-ready view.

        Picks the control/treatment variant names dynamically (the loop uses
        ``control``/``T1``, but this tolerates any two-variant split), and orders rows
        so significant improvements surface first.
        """
        result = self.ab_result()
        if result is None or not result.per_variant:
            return None

        variants = list(result.per_variant.keys())
        # Treatment is the winner if known, else the non-control variant.
        control = "control" if "control" in variants else variants[0]
        treatment = (
            result.winner
            if result.winner and result.winner in variants
            else next((v for v in variants if v != control), control)
        )

        ctrl_metrics = result.per_variant.get(control, {})
        trt_metrics = result.per_variant.get(treatment, {})
        evaluators = list(dict.fromkeys([*trt_metrics.keys(), *ctrl_metrics.keys()]))

        rows: list[MetricRow] = []
        for ev in evaluators:
            t = trt_metrics.get(ev)
            c = ctrl_metrics.get(ev)
            rows.append(
                MetricRow(
                    evaluator=ev,
                    control_mean=(c.mean if c else None),
                    treatment_mean=(t.mean if t else None),
                    abs_change=(t.abs_change if t else 0.0),
                    pct_change=(t.pct_change if t else 0.0),
                    p_value=(t.p_value if t else 1.0),
                    significant=(t.significant if t else False),
                )
            )
        # Significant rows first, then by absolute change descending.
        rows.sort(key=lambda r: (not r.significant, -abs(r.abs_change)))

        return ABView(
            ab_test_id=result.ab_test_id,
            winner=result.winner,
            significant=result.significant,
            control_variant=control,
            treatment_variant=treatment,
            rows=rows,
        )

    # --- approvals --------------------------------------------------------
    def approval_ids(self) -> list[str]:
        return self.store.list_json("approvals")

    def approvals(self) -> list[ApprovalRequest]:
        out: list[ApprovalRequest] = []
        for rid in self.approval_ids():
            try:
                out.append(ApprovalRequest.from_dict(self.store.read_json("approvals", rid)))
            except (KeyError, ValueError):
                # Skip malformed / non-approval json rather than crash the whole page.
                continue
        # Newest first by created_at (ISO strings sort lexically).
        out.sort(key=lambda r: r.created_at or "", reverse=True)
        return out

    def pending_approvals(self) -> list[ApprovalRequest]:
        return [r for r in self.approvals() if r.status == ApprovalStatus.PENDING_APPROVAL]

    def approval(self, request_id: str) -> ApprovalRequest | None:
        if not self.store.exists("approvals", request_id):
            return None
        return ApprovalRequest.from_dict(self.store.read_json("approvals", request_id))

    def has_promotion_decision(self, request_id: str) -> bool:
        """True if a saved PromotionDecision exists for this request (approve is wired)."""
        return self.store.exists("bundles", f"decision-{request_id}")

    # --- audit ------------------------------------------------------------
    def audit_entries(self) -> list[AuditEntry]:
        out: list[AuditEntry] = []
        for raw in self.store.read_audit():
            try:
                out.append(AuditEntry.from_dict(raw))
            except (KeyError, ValueError):
                continue
        out.sort(key=lambda e: e.timestamp or "", reverse=True)
        return out

    # --- baselines / bundles / runs --------------------------------------
    def baselines(self) -> list[Baseline]:
        out: list[Baseline] = []
        for name in self.store.list_json("baselines"):
            try:
                out.append(Baseline.from_dict(self.store.read_json("baselines", name)))
            except (KeyError, ValueError):
                continue
        return out

    def bundle_versions(self) -> list[BundleVersion]:
        out: list[BundleVersion] = []
        for name in self.store.list_json("bundles"):
            if name.startswith("decision-"):
                continue  # promotion decisions are not bundle versions
            try:
                out.append(BundleVersion.from_dict(self.store.read_json("bundles", name)))
            except (KeyError, ValueError):
                continue
        return out

    def run_ids(self) -> list[str]:
        return self.store.list_runs()

    # --- offline evaluation reports --------------------------------------
    def report_names(self) -> list[str]:
        """Names of persisted offline reports (subdirs of ``artifacts/reports/``)."""
        reports_dir = self.store.subdir("reports")
        if not reports_dir.exists():
            return []
        names = [p.name for p in reports_dir.iterdir() if p.is_dir() and (p / "report.json").exists()]
        return sorted(names)

    def report(self, name: str) -> EvaluationReport | None:
        """Load one persisted report by name (the ``report.json`` under its subdir)."""
        path = self.store.subdir("reports") / name / "report.json"
        if not path.exists():
            return None
        try:
            return EvaluationReport.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (KeyError, ValueError, json.JSONDecodeError):
            return None

    def report_html(self, name: str) -> str | None:
        """The rendered ``report.html`` for a report, if present."""
        path = self.store.subdir("reports") / name / "report.html"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")
