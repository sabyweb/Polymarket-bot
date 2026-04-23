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

# INV3_new — expected capital utilisation band.
INV3_NEW_UTIL_MIN = 0.50
INV3_NEW_UTIL_MAX = 0.95

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

def evaluate_inv3_expected_utilisation(
    snapshots: list[V4CycleSnapshot],
) -> V5InvariantResult:
    """INV3_new — post-warmup average expected_util must fall in
    [INV3_NEW_UTIL_MIN, INV3_NEW_UTIL_MAX]."""
    post_warm = [s for s in snapshots if s.cycle > WARMUP_CUTOFF]
    if not post_warm:
        return V5InvariantResult(
            invariant="INV3_new", passed=False,
            metric_values={
                "avg_expected_util": None,
                "post_warmup_cycles": 0,
                "util_min": INV3_NEW_UTIL_MIN,
                "util_max": INV3_NEW_UTIL_MAX,
            },
            reason="no post-warmup cycles (run too short)",
        )

    # Per-cycle util (None excluded from aggregate; kept aligned for
    # failing-cycle scanning below).
    per_cycle: list[tuple[V4CycleSnapshot, Optional[float]]] = [
        (s, compute_expected_util(s)) for s in post_warm
    ]
    util_vals = [u for _, u in per_cycle if u is not None]

    if not util_vals:
        # Every cycle had total_capital=0 — degenerate scenario.
        return V5InvariantResult(
            invariant="INV3_new", passed=False,
            metric_values={
                "avg_expected_util": None,
                "post_warmup_cycles": len(post_warm),
                "util_samples": 0,
                "util_min": INV3_NEW_UTIL_MIN,
                "util_max": INV3_NEW_UTIL_MAX,
            },
            reason=(
                "no valid expected_util samples "
                "(total_capital ≤ 0 on every post-warmup cycle)"
            ),
        )

    avg_util = sum(util_vals) / len(util_vals)
    passed = INV3_NEW_UTIL_MIN <= avg_util <= INV3_NEW_UTIL_MAX

    # Per-cycle failures (out-of-band) for the diagnostics block.
    failing_cycles: list[tuple[int, int]] = []
    run_start: Optional[int] = None
    for s, u in per_cycle:
        in_band = (
            u is not None
            and INV3_NEW_UTIL_MIN <= u <= INV3_NEW_UTIL_MAX
        )
        if in_band:
            if run_start is not None:
                failing_cycles.append((run_start, s.cycle - 1))
                run_start = None
        else:
            if run_start is None:
                run_start = s.cycle
    if run_start is not None:
        failing_cycles.append((run_start, post_warm[-1].cycle))

    # Headline aggregates for the failure block (spec §3.5).
    total_expected = sum(float(s.expected_capital or 0.0) for s in post_warm)
    total_total_capital = sum(
        float(s.total_capital or 0.0) for s in post_warm
    )

    reason: Optional[str] = None
    if not passed:
        if avg_util < INV3_NEW_UTIL_MIN:
            reason = (
                f"avg expected_util={avg_util:.4f} < "
                f"{INV3_NEW_UTIL_MIN} (under-deployed)"
            )
        else:
            reason = (
                f"avg expected_util={avg_util:.4f} > "
                f"{INV3_NEW_UTIL_MAX} (safety ceiling breached)"
            )

    return V5InvariantResult(
        invariant="INV3_new",
        passed=passed,
        metric_values={
            "expected_util": round(avg_util, 6),
            "total_expected_capital": round(total_expected, 4),
            "total_capital": round(total_total_capital, 4),
            "util_samples": len(util_vals),
            "post_warmup_cycles": len(post_warm),
            "util_min": INV3_NEW_UTIL_MIN,
            "util_max": INV3_NEW_UTIL_MAX,
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
