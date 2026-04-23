"""simulation/audit_v5_invariants.py — invariant evaluators for the
continuous allocator.

V5 replaces V4's INV3 (overcommit / target_notional) and INV5
(notional deploy_ratio ≥ 0.85) with invariants aligned to what the
continuous allocator actually optimises:

  INV3_new — Expected Capital Utilisation
      Pass when:
        avg_{cycle > warmup} [ Σ (p_i × C_i) / total_capital ]
          ∈ [0.5, 0.95]

      Out of [0.5, 0.95] means the system is either under-deployed or
      violating the 0.95 safety ceiling; both are failures.

  INV5_new — Allocation Coverage
      Pass when:
        avg_{cycle > warmup} [ active_markets / total_markets ] ≥ 0.5

      active_markets = count(C_i > cpb_i × min_shares)
      total_markets  = deploy-candidate count

      Ensures the allocator isn't collapsing onto a handful of markets.

  INV7 — Oscillation Stability (UNCHANGED from V4; re-exported so the
      V5 CLI can evaluate the full trio without importing V4 directly.)

Warmup cutoff matches V4: only cycles with `cycle > 100` count.

This module does NOT touch the allocator or learning loop. It reads a
list of V4CycleSnapshot (produced by V4Tracker) plus — for the field
validation step — the raw per-cycle allocation rows.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional

from .audit_v4_invariants import (
    evaluate_inv7 as _evaluate_inv7_v4,
    V4InvariantResult,
    WARMUP_CUTOFF,
)
from .audit_v4_metrics import V4CycleSnapshot


# ═══════════════════════════════════════════════════════════════
# Thresholds (spec section 2)
# ═══════════════════════════════════════════════════════════════

# INV3_new — cap-normalised capital utilisation.
# capital_util = Σ(C_i) / total_capital    (pure capital coverage,
# unweighted by p_fill — measures control-loop deployment, not the
# fill model).
# feasible_capital_fraction = min(CAPITAL_BUFFER,
#                                 Σ cluster_cap_pct + unclustered_fraction)
# normalized_util = capital_util / feasible_capital_fraction
# PASS when post-warmup mean normalized_util ≥ INV3_NEW_NORMALIZED_MIN.
INV3_NEW_NORMALIZED_MIN = 0.70

# INV5_new — minimum allocation coverage (active markets / total).
INV5_NEW_COVERAGE_MIN = 0.50

# V5 validation — the per-market fields the invariants require.
# Order matters for deterministic error messages.
V5_REQUIRED_DEPLOY_FIELDS = (
    "_p_fill",
    "est_capital_cost",
    "shares_per_side",
    "min_size",
    "max_spread",
)


# ═══════════════════════════════════════════════════════════════
# Result type
# ═══════════════════════════════════════════════════════════════

@dataclass
class V5InvariantResult:
    """Structured outcome of a single V5 invariant evaluation.

    Schema intentionally mirrors V4InvariantResult so downstream report
    and CSV writers can treat the two interchangeably — the `invariant`
    field carries the V5 label (`INV3_new`, `INV5_new`, `INV7`).
    """
    invariant: str
    passed: bool
    metric_values: dict
    failing_cycles: list[tuple[int, int]] = field(default_factory=list)
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ═══════════════════════════════════════════════════════════════
# Field validation — spec §3.2 (raise, no silent fallback)
# ═══════════════════════════════════════════════════════════════

class V5FieldMissingError(RuntimeError):
    """Raised when a cycle's allocations lack a field V5 invariants need.

    The audit treats this as a contract violation — the continuous
    allocator MUST stamp the fields the audit reads. We never fabricate
    or infer values; we raise and let the operator notice."""


def validate_v5_fields(
    allocations: list[dict],
    *,
    cycle: int,
    scenario: str,
    seed: int,
    total_capital: float,
) -> None:
    """Raise V5FieldMissingError on the first deploy row that lacks any
    of the fields V5 invariants require.

    Validates:
      • total_capital is a finite positive scalar
      • every deploy row carries each field in V5_REQUIRED_DEPLOY_FIELDS

    Avoid rows (action != "deploy") are NOT checked — they legitimately
    carry zero shares and no prediction stamps.
    """
    if total_capital is None:
        raise V5FieldMissingError(
            f"V5 (scenario={scenario}, seed={seed}, cycle={cycle}): "
            f"total_capital is None"
        )
    tc = float(total_capital)
    if not (tc > 0 and tc == tc):  # positive + not NaN
        raise V5FieldMissingError(
            f"V5 (scenario={scenario}, seed={seed}, cycle={cycle}): "
            f"total_capital={total_capital!r} is not a positive finite scalar"
        )

    for row in allocations:
        if row.get("action") != "deploy":
            continue
        for key in V5_REQUIRED_DEPLOY_FIELDS:
            if row.get(key) is None:
                cid = row.get("condition_id", "<unknown>")
                raise V5FieldMissingError(
                    f"V5 (scenario={scenario}, seed={seed}, cycle={cycle}): "
                    f"deploy row {cid} missing required field {key!r}"
                )


# ═══════════════════════════════════════════════════════════════
# Derived metrics per cycle
# ═══════════════════════════════════════════════════════════════

def compute_expected_util(snap: V4CycleSnapshot) -> Optional[float]:
    """Σ (p_i · C_i) / total_capital — returns None when total_capital
    is zero (cycle should be excluded from the aggregate)."""
    tc = float(snap.total_capital or 0.0)
    if tc <= 0:
        return None
    return float(snap.expected_capital) / tc


def compute_coverage_ratio(snap: V4CycleSnapshot) -> Optional[float]:
    """active_markets / total_markets where active = C_i > min_capital_floor.

    active_markets := number_of_deployed_markets − min_size_alloc_count
    total_markets  := number_of_deployed_markets

    Returns None when no markets were deployed (degenerate; excluded
    from aggregate). The allocator only emits zero-deploy cycles when
    `total_capital <= 0` or the scorer marks every market as avoid."""
    total = int(snap.number_of_deployed_markets or 0)
    if total <= 0:
        return None
    at_min = int(snap.min_size_alloc_count or 0)
    active = max(0, total - at_min)
    return active / total


# ═══════════════════════════════════════════════════════════════
# INV3_new — Expected Capital Utilisation
# ═══════════════════════════════════════════════════════════════

def compute_capital_util(snap: V4CycleSnapshot) -> Optional[float]:
    """Σ(C_i) / total_capital — pure capital coverage across deploys.
    Returns None when total_capital ≤ 0 (cycle excluded from aggregate).
    Reads the already-stamped `total_notional` (= Σ est_capital_cost)."""
    tc = float(snap.total_capital or 0.0)
    if tc <= 0:
        return None
    return float(snap.total_notional or 0.0) / tc


def _normalized_capital_util(snap: V4CycleSnapshot) -> Optional[float]:
    """Cap-normalised capital utilisation:
        normalized_util = capital_util / feasible_capital_fraction
    Returns None when either side is missing or the denominator is
    non-positive (cycle is excluded from the aggregate)."""
    c = compute_capital_util(snap)
    if c is None:
        return None
    f = snap.feasible_capital_fraction
    if f is None or f <= 0.0:
        return None
    return c / f


def evaluate_inv3_expected_utilisation(
    snapshots: list[V4CycleSnapshot],
) -> V5InvariantResult:
    """INV3_new — post-warmup avg normalized_util ≥ INV3_NEW_NORMALIZED_MIN.

        capital_util = Σ(C_i) / total_capital
        feasible_capital_fraction =
            min(CAPITAL_BUFFER,
                Σ cluster_cap_pct + unclustered_fraction)
        normalized_util = capital_util / feasible_capital_fraction

    The numerator is pure capital coverage (unweighted by p_fill), so
    the invariant measures control-loop deployment quality — not the
    fill model. The denominator divides out the cap-stack's physical
    ceiling, so the metric is scenario-independent.
    """
    post_warm = [s for s in snapshots if s.cycle > WARMUP_CUTOFF]
    if not post_warm:
        return V5InvariantResult(
            invariant="INV3_new", passed=False,
            metric_values={
                "normalized_util": None,
                "post_warmup_cycles": 0,
                "normalized_util_min": INV3_NEW_NORMALIZED_MIN,
            },
            reason="no post-warmup cycles (run too short)",
        )

    per_cycle_norm: list[tuple[V4CycleSnapshot, Optional[float]]] = [
        (s, _normalized_capital_util(s)) for s in post_warm
    ]
    norm_vals = [v for _, v in per_cycle_norm if v is not None]

    # Auxiliary aggregates for the failure block.
    cap_vals = [
        compute_capital_util(s) for s in post_warm
        if compute_capital_util(s) is not None
    ]
    avg_capital_util = (
        sum(cap_vals) / len(cap_vals) if cap_vals else None
    )
    feasible_vals = [
        float(s.feasible_capital_fraction) for s in post_warm
        if s.feasible_capital_fraction is not None
    ]
    avg_feasible = (
        sum(feasible_vals) / len(feasible_vals) if feasible_vals else None
    )
    total_notional = sum(float(s.total_notional or 0.0) for s in post_warm)
    total_total_capital = sum(
        float(s.total_capital or 0.0) for s in post_warm
    )

    if not norm_vals:
        # No feasible_capital_fraction anywhere → invariant can't be
        # evaluated. Fail closed (don't silently pass on missing data).
        return V5InvariantResult(
            invariant="INV3_new", passed=False,
            metric_values={
                "normalized_util": None,
                "avg_capital_util": (
                    round(avg_capital_util, 6)
                    if avg_capital_util is not None else None
                ),
                "avg_feasible_capital_fraction": None,
                "post_warmup_cycles": len(post_warm),
                "util_samples": 0,
                "normalized_util_min": INV3_NEW_NORMALIZED_MIN,
            },
            reason=(
                "feasible_capital_fraction unavailable on every "
                "post-warmup cycle (V4Tracker db_path not set?)"
            ),
        )

    avg_norm_util = sum(norm_vals) / len(norm_vals)
    passed = avg_norm_util >= INV3_NEW_NORMALIZED_MIN

    # Per-cycle failing runs (normalized_util below threshold). Missing-
    # denominator cycles don't count against the invariant — they're
    # reported separately via util_samples < post_warmup_cycles.
    failing_cycles: list[tuple[int, int]] = []
    run_start: Optional[int] = None
    for s, v in per_cycle_norm:
        in_band = v is None or v >= INV3_NEW_NORMALIZED_MIN
        if in_band:
            if run_start is not None:
                failing_cycles.append((run_start, s.cycle - 1))
                run_start = None
        else:
            if run_start is None:
                run_start = s.cycle
    if run_start is not None:
        failing_cycles.append((run_start, post_warm[-1].cycle))

    reason: Optional[str] = None
    if not passed:
        reason = (
            f"avg normalized_util={avg_norm_util:.4f} < "
            f"{INV3_NEW_NORMALIZED_MIN} "
            f"(capital_util={avg_capital_util:.4f}, "
            f"feasible={avg_feasible:.4f})"
        )

    return V5InvariantResult(
        invariant="INV3_new",
        passed=passed,
        metric_values={
            # Primary field keeps the `expected_util` key for report
            # schema back-compat; its value is now the cap-normalised
            # capital utilisation.
            "expected_util": round(avg_norm_util, 6),
            "normalized_util": round(avg_norm_util, 6),
            "avg_capital_util": round(avg_capital_util, 6),
            "avg_feasible_capital_fraction": round(avg_feasible, 6),
            "total_notional": round(total_notional, 4),
            "total_capital": round(total_total_capital, 4),
            "util_samples": len(norm_vals),
            "post_warmup_cycles": len(post_warm),
            "normalized_util_min": INV3_NEW_NORMALIZED_MIN,
        },
        failing_cycles=failing_cycles,
        reason=reason,
    )


# ═══════════════════════════════════════════════════════════════
# INV5_new — Allocation Coverage
# ═══════════════════════════════════════════════════════════════

def evaluate_inv5_coverage(
    snapshots: list[V4CycleSnapshot],
) -> V5InvariantResult:
    """INV5_new — post-warmup average coverage_ratio must be ≥
    INV5_NEW_COVERAGE_MIN.

    A cycle with zero deploys contributes no sample (excluded).
    """
    post_warm = [s for s in snapshots if s.cycle > WARMUP_CUTOFF]
    if not post_warm:
        return V5InvariantResult(
            invariant="INV5_new", passed=False,
            metric_values={
                "coverage_ratio": None,
                "post_warmup_cycles": 0,
                "coverage_min": INV5_NEW_COVERAGE_MIN,
            },
            reason="no post-warmup cycles (run too short)",
        )

    per_cycle: list[tuple[V4CycleSnapshot, Optional[float]]] = [
        (s, compute_coverage_ratio(s)) for s in post_warm
    ]
    cov_vals = [c for _, c in per_cycle if c is not None]

    if not cov_vals:
        return V5InvariantResult(
            invariant="INV5_new", passed=False,
            metric_values={
                "coverage_ratio": None,
                "post_warmup_cycles": len(post_warm),
                "coverage_samples": 0,
                "coverage_min": INV5_NEW_COVERAGE_MIN,
            },
            reason=(
                "no valid coverage samples "
                "(no deploy rows on any post-warmup cycle)"
            ),
        )

    avg_cov = sum(cov_vals) / len(cov_vals)
    passed = avg_cov >= INV5_NEW_COVERAGE_MIN

    # Summed headline values for the diagnostics block (spec §3.5).
    total_active = sum(
        max(
            0,
            int(s.number_of_deployed_markets or 0)
            - int(s.min_size_alloc_count or 0),
        )
        for s in post_warm
    )
    total_markets = sum(
        int(s.number_of_deployed_markets or 0) for s in post_warm
    )

    # Per-cycle failing ranges.
    failing_cycles: list[tuple[int, int]] = []
    run_start: Optional[int] = None
    for s, c in per_cycle:
        below = c is not None and c < INV5_NEW_COVERAGE_MIN
        if below:
            if run_start is None:
                run_start = s.cycle
        else:
            if run_start is not None:
                failing_cycles.append((run_start, s.cycle - 1))
                run_start = None
    if run_start is not None:
        failing_cycles.append((run_start, post_warm[-1].cycle))

    reason: Optional[str] = None
    if not passed:
        reason = (
            f"avg coverage_ratio={avg_cov:.4f} < "
            f"{INV5_NEW_COVERAGE_MIN} (allocator collapsed onto too few "
            f"markets or most hit min_size floor)"
        )

    return V5InvariantResult(
        invariant="INV5_new",
        passed=passed,
        metric_values={
            "coverage_ratio": round(avg_cov, 6),
            "active_markets": int(total_active),
            "total_markets": int(total_markets),
            "coverage_samples": len(cov_vals),
            "post_warmup_cycles": len(post_warm),
            "coverage_min": INV5_NEW_COVERAGE_MIN,
        },
        failing_cycles=failing_cycles,
        reason=reason,
    )


# ═══════════════════════════════════════════════════════════════
# INV7 — re-exported from V4, wrapped in V5InvariantResult
# ═══════════════════════════════════════════════════════════════

def evaluate_inv7(snapshots: list[V4CycleSnapshot]) -> V5InvariantResult:
    """Thin adapter: V4's INV7 logic is correct and orthogonal to the
    allocation-shape changes, so we reuse it verbatim and rebox the
    result into a V5InvariantResult for uniform downstream handling."""
    v4_result: V4InvariantResult = _evaluate_inv7_v4(snapshots)
    return V5InvariantResult(
        invariant="INV7",
        passed=v4_result.passed,
        metric_values=dict(v4_result.metric_values),
        failing_cycles=list(v4_result.failing_cycles),
        reason=v4_result.reason,
    )


# ═══════════════════════════════════════════════════════════════
# Umbrella — evaluate all three invariants
# ═══════════════════════════════════════════════════════════════

def evaluate_all_v5(
    snapshots: list[V4CycleSnapshot],
) -> dict[str, V5InvariantResult]:
    """Evaluate INV3_new, INV5_new, and INV7 on the same snapshot list.
    Keys match the labels used by the V5 report (INV3_new / INV5_new /
    INV7) so downstream code can iterate results deterministically."""
    return {
        "INV3_new": evaluate_inv3_expected_utilisation(snapshots),
        "INV5_new": evaluate_inv5_coverage(snapshots),
        "INV7":     evaluate_inv7(snapshots),
    }
