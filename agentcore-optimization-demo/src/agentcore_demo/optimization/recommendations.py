"""AI-generated recommendations for the agent's system prompt and tool descriptions.

Wraps the AgentCore Recommendations API (via the client), surfaces the proposed change
with a "what changed and why" summary, and can apply a recommendation to a
:class:`BundleConfig` so it can be packaged as a new configuration bundle version.

Requirements: 4.1, 4.2, 4.3
"""

from __future__ import annotations

import copy
from typing import Sequence

from ..agentcore_client import Recommendation, RecommendationRequest
from ..evaluation.evaluators import builtin_arn
from ..models import BundleConfig


class RecommendationService:
    """Requests system-prompt / tool-description recommendations from AgentCore."""

    def __init__(
        self,
        client,
        *,
        log_group_arns: Sequence[str],
        service_names: Sequence[str],
        name_prefix: str = "PatientRec",
        lookback_days: int = 7,
    ):
        self.client = client
        self.log_group_arns = list(log_group_arns)
        self.service_names = list(service_names)
        self.name_prefix = name_prefix
        self.lookback_days = lookback_days

    def recommend_system_prompt(self, current_prompt: str, target_evaluator: str = "Builtin.GoalSuccessRate", wait: bool = True, on_poll=None) -> Recommendation:
        req = RecommendationRequest(
            name=f"{self.name_prefix}Sp",
            kind="system_prompt",
            current_system_prompt=current_prompt,
            log_group_arns=self.log_group_arns,
            service_names=self.service_names,
            target_evaluator_arn=builtin_arn(target_evaluator),
            lookback_days=self.lookback_days,
        )
        return self.client.start_recommendation(req, wait=wait, on_poll=on_poll)

    def recommend_tool_descriptions(self, current_descriptions: dict[str, str], wait: bool = True) -> Recommendation:
        req = RecommendationRequest(
            name=f"{self.name_prefix}Td",
            kind="tool_description",
            current_tool_descriptions=dict(current_descriptions),
            log_group_arns=self.log_group_arns,
            service_names=self.service_names,
            lookback_days=self.lookback_days,
        )
        return self.client.start_recommendation(req, wait=wait)


# ---------------------------------------------------------------------------
# Pure helpers: presentation + application (Requirements 4.2, 4.3)
# ---------------------------------------------------------------------------
def present_change(
    rec: Recommendation,
    *,
    current_system_prompt: str | None = None,
    current_tool_descriptions: dict[str, str] | None = None,
    target_evaluator: str | None = None,
) -> dict:
    """Summarize a recommendation as a before/after change with an explanation (R4.2)."""
    if rec.error_code:
        return {
            "kind": rec.kind,
            "changed": False,
            "error": f"{rec.error_code}: {rec.error_message or ''}".strip(),
            "explanation": "Recommendation could not be generated; keeping current configuration.",
        }

    if rec.kind == "system_prompt":
        before = current_system_prompt or ""
        after = rec.recommended_system_prompt or before
        changed = after != before
        why = "Optimized the system prompt"
        if target_evaluator:
            why += f" to improve {target_evaluator}"
        why += " based on production traces."
        return {"kind": rec.kind, "changed": changed, "before": before, "after": after, "explanation": why}

    # tool_description
    before = dict(current_tool_descriptions or {})
    after = dict(rec.recommended_tool_descriptions or before)
    changed_tools = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    why = "Refined tool descriptions to improve tool selection based on production traces."
    if changed_tools:
        why += f" Changed: {', '.join(changed_tools)}."
    return {"kind": rec.kind, "changed": bool(changed_tools), "before": before, "after": after, "changed_tools": changed_tools, "explanation": why}


def apply_to_config(rec: Recommendation, base: BundleConfig) -> BundleConfig:
    """Return a new BundleConfig with the recommendation applied (base is not mutated)."""
    new = copy.deepcopy(base)
    if rec.kind == "system_prompt" and rec.recommended_system_prompt:
        new.system_prompt = rec.recommended_system_prompt
    elif rec.kind == "tool_description" and rec.recommended_tool_descriptions:
        merged = dict(new.tool_descriptions)
        merged.update(rec.recommended_tool_descriptions)
        new.tool_descriptions = merged
    return new
