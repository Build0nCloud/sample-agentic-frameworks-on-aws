"""A/B testing lifecycle, statistics, and the A/B traffic driver.

Two complementary paths are supported:

* **Service-side**: start an A/B test through AgentCore Gateway (config-bundle or
  target-based variants); the service scores sessions and reports significance. This is
  a thin delegation to the client (:meth:`ABTestRunner.poll`).
* **Locally computed**: drive labeled traffic per variant and compute the per-evaluator
  statistics ourselves (Welch's t-test) from per-session scores. This is deterministic
  and fast enough for a live demo, and is what the promotion gate consumes.

Winner detection (Property 4 / Requirement 6.1): a treatment variant is the winner iff
it is statistically significant (p < 0.05) *and* improves over control on the primary
evaluator; otherwise there is no winner and the result is not significant.

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9
"""

from __future__ import annotations

import math
import uuid
from typing import Mapping, Sequence

from ..agentcore_client import (
    ABTestHandle,
    ABTestSpec,
    ABTestVariant,
    AgentHandle,
    BundleRef,
    assign_variant,
)
from ..models import ABTestResult, EvalMetric

DEFAULT_ALPHA = 0.05
CONTROL = "control"
TREATMENT = "treatment"
# AgentCore requires A/B variant names to match exactly (C|T1).
CONTROL_VARIANT = "C"
TREATMENT_VARIANT = "T1"


# ===========================================================================
# Statistics (pure)
# ===========================================================================
def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _variance(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = mean(xs)
    return sum((x - m) ** 2 for x in xs) / (n - 1)


def _betacf(a: float, b: float, x: float, itmax: int = 300, eps: float = 1e-10) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_sf(t: float, df: float) -> float:
    """Survival function P(T > t) for a Student-t with df degrees of freedom (t >= 0)."""
    if df <= 0:
        return 1.0
    x = df / (df + t * t)
    return 0.5 * _betai(df / 2.0, 0.5, x)


def two_sided_p(t: float, df: float) -> float:
    """Two-sided p-value for a t statistic."""
    if df <= 0:
        return 1.0
    return min(1.0, 2.0 * t_sf(abs(t), df))


def t_ppf_975(df: float) -> float:
    """Approximate the 0.975 quantile of Student-t (for a 95% CI) via bisection."""
    if df <= 0:
        return 1.96
    lo, hi = 0.0, 1000.0
    for _ in range(100):
        mid = (lo + hi) / 2.0
        # cdf(mid) = 1 - sf(mid); want cdf == 0.975 -> sf == 0.025
        if t_sf(mid, df) > 0.025:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def welch_t_test(control: Sequence[float], treatment: Sequence[float]) -> tuple[float, float, float, float]:
    """Welch's t-test. Returns (t_stat, df, p_value, standard_error_of_difference)."""
    na, nb = len(control), len(treatment)
    if na < 2 or nb < 2:
        return 0.0, 0.0, 1.0, 0.0
    va, vb = _variance(control), _variance(treatment)
    se2 = va / na + vb / nb
    se = math.sqrt(se2)
    diff = mean(treatment) - mean(control)
    # Welch-Satterthwaite denominator; can underflow to 0 for negligible variance.
    denom = (va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1)
    if se == 0.0 or denom == 0.0:
        # Negligible/zero variance: significant iff the means actually differ.
        return (math.inf if diff else 0.0), float(na + nb - 2), (0.0 if diff else 1.0), se
    t = diff / se
    df = se2 ** 2 / denom
    return t, df, two_sided_p(t, df), se


def compute_metric(control: Sequence[float], treatment: Sequence[float], alpha: float = DEFAULT_ALPHA) -> EvalMetric:
    """Per-evaluator treatment metric vs control (mean, changes, p-value, CI, significance)."""
    ma, mb = mean(control), mean(treatment)
    diff = mb - ma
    pct = (diff / ma * 100.0) if ma != 0 else 0.0
    t, df, p, se = welch_t_test(control, treatment)
    tc = t_ppf_975(df) if df > 0 else 1.96
    ci_low, ci_high = diff - tc * se, diff + tc * se
    significant = p < alpha and diff > 0
    return EvalMetric(mean=mb, abs_change=diff, pct_change=pct, p_value=p, ci_low=ci_low, ci_high=ci_high, significant=significant)


def determine_winner(per_variant: Mapping[str, Mapping[str, EvalMetric]], primary_evaluator: str, alpha: float = DEFAULT_ALPHA) -> tuple[str | None, bool]:
    """Pick the significant, improved treatment variant on the primary evaluator (P4)."""
    best: str | None = None
    best_pct = 0.0
    for variant, metrics in per_variant.items():
        if variant == CONTROL:
            continue
        m = metrics.get(primary_evaluator)
        if m and m.significant and m.pct_change > 0 and m.pct_change > best_pct:
            best, best_pct = variant, m.pct_change
    return best, best is not None


def determine_winner_any(
    per_variant: Mapping[str, Mapping[str, EvalMetric]],
    alpha: float = DEFAULT_ALPHA,
    control_name: str = CONTROL,
) -> tuple[str | None, bool]:
    """Winner policy: a treatment variant wins if it is significantly better on at least
    one evaluator and not significantly worse on any (a decisive, no-regression win).

    Useful when the headline improvement (e.g. a perfectly-separated trajectory metric)
    has no computable p-value, but the treatment is significantly better on another
    quality metric with no regressions.
    """
    best: str | None = None
    for variant, metrics in per_variant.items():
        if variant == control_name:
            continue
        improved = any(m.significant and m.pct_change > 0 for m in metrics.values())
        regressed = any(m.significant and m.pct_change < 0 for m in metrics.values())
        if improved and not regressed:
            best = variant
            break
    return best, best is not None


def compute_ab_result(
    ab_test_id: str,
    control_scores: Mapping[str, Sequence[float]],
    treatment_scores: Mapping[str, Sequence[float]],
    *,
    primary_evaluator: str | None = None,
    alpha: float = DEFAULT_ALPHA,
) -> ABTestResult:
    """Compute an ABTestResult from per-evaluator control/treatment score samples (R5.6)."""
    evaluators = list(control_scores.keys())
    primary = primary_evaluator or (evaluators[0] if evaluators else None)

    per_variant: dict[str, dict[str, EvalMetric]] = {CONTROL: {}, TREATMENT: {}}
    for ev in evaluators:
        c = list(control_scores.get(ev, []))
        t = list(treatment_scores.get(ev, []))
        per_variant[CONTROL][ev] = EvalMetric(mean=mean(c))
        per_variant[TREATMENT][ev] = compute_metric(c, t, alpha=alpha)

    if primary is None:
        return ABTestResult(ab_test_id=ab_test_id, per_variant=per_variant, winner=None, significant=False)
    winner, significant = determine_winner(per_variant, primary, alpha=alpha)
    return ABTestResult(ab_test_id=ab_test_id, per_variant=per_variant, winner=winner, significant=significant)


# ===========================================================================
# A/B test lifecycle + traffic driver
# ===========================================================================
class ABTestRunner:
    """Starts/polls/stops A/B tests and drives labeled A/B traffic."""

    def __init__(self, client):
        self.client = client

    # --- start (R5.1, R5.2, R5.3) ---------------------------------------
    def start_config_bundle_test(
        self,
        name: str,
        *,
        gateway_arn: str,
        role_arn: str,
        online_eval_arn: str,
        control: BundleRef,
        treatment: BundleRef,
        control_weight: int = 50,
        treatment_weight: int = 50,
    ) -> ABTestHandle:
        spec = ABTestSpec(
            name=name,
            gateway_arn=gateway_arn,
            role_arn=role_arn,
            online_eval_arn=online_eval_arn,
            variants=[
                ABTestVariant(CONTROL_VARIANT, control_weight, bundle=control),
                ABTestVariant(TREATMENT_VARIANT, treatment_weight, bundle=treatment),
            ],
        )
        return self.client.start_ab_test(spec)

    def start_target_based_test(
        self,
        name: str,
        *,
        gateway_arn: str,
        role_arn: str,
        online_eval_arn: str,
        control_target: str,
        treatment_target: str,
        control_weight: int = 90,
        treatment_weight: int = 10,
    ) -> ABTestHandle:
        spec = ABTestSpec(
            name=name,
            gateway_arn=gateway_arn,
            role_arn=role_arn,
            online_eval_arn=online_eval_arn,
            variants=[
                ABTestVariant(CONTROL_VARIANT, control_weight, target_endpoint=control_target),
                ABTestVariant(TREATMENT_VARIANT, treatment_weight, target_endpoint=treatment_target),
            ],
        )
        return self.client.start_ab_test(spec)

    # --- poll (service-side) / stop (R5.7, R5.8) ------------------------
    def poll(self, handle: ABTestHandle) -> ABTestResult:
        return self.client.get_ab_test(handle)

    def stop(self, handle: ABTestHandle) -> None:
        self.client.stop_ab_test(handle)

    # --- locally-labeled traffic driver (R5.4, R5.9) --------------------
    def split_and_drive(
        self,
        agent: AgentHandle,
        sessions: Sequence[dict],
        variant_bundles: Mapping[str, BundleRef],
        weights: Sequence[tuple[str, int]],
        *,
        loops: int = 1,
    ) -> dict[str, list[str]]:
        """Drive traffic, assigning each session to a variant by sticky weighted hash.

        Returns variant name -> list of session ids sent to it. A given session id
        always maps to the same variant (Requirement 5.4), and ``loops`` replays the
        dataset to reach enough volume for a reportable result (Requirement 5.9).
        """
        assignments: dict[str, list[str]] = {name: [] for name, _ in weights}
        for _loop in range(max(1, loops)):
            for spec in sessions:
                session_id = str(uuid.uuid4())
                variant = assign_variant(session_id, weights)
                self.client.send_session(agent, spec["turns"], session_id=session_id, bundle=variant_bundles.get(variant))
                assignments.setdefault(variant, []).append(session_id)
        return assignments
