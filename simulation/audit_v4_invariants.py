"""simulation/audit_v4_invariants.py — strict invariant evaluators.

Evaluates INV3 / INV5 / INV7 against a list of V4CycleSnapshots.

Per spec:

  INV3 — Capital Utilisation
      Pass when:
        avg_overcommit ≥ 1.3
        AND
        avg(actual_notional / target_notional) ≥ 0.9

  INV5 — Deployment Floor
      Pass when:
        deploy_ratio ≥ 0.85 for ≥ 80% of post-warmup cycles

  INV7 — Oscillation Stability
      Pass when:
        rolling_flip_rate_100 ≤ 3 across ALL post-warmup cycles
        AND
        no sustained alternating run of > 20 cycles where every
        consecutive non-zero delta flips sign

Warmup cutoff is cycle > 100 (per spec section 5).

Each invariant returns a V4InvariantResult with:
  passed (bool), metric values, failing cycle ranges, root-cause hint.

The returned objects are serialisable for --trace-invariants output.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional

from .audit_v4_metrics import (
    V4CycleSnapshot, OSC_WINDOW_CYCLES, SUSTAINED_ALT_MIN_LENGTH,
)


# Per spec section 5: evaluate invariants after warmup (cycle > 100).
WARMUP_CUTOFF = 100

# INV3 thresholds
INV3_AVG_OVERCOMMIT_FLOOR = 1.3
INV3_ACTUAL_OVER_TARGET_FLOOR = 0.9

# INV5 thresholds
INV5_DEPLOY_RATIO_FLOOR = 0.85
INV5_PASS_FRACTION = 0.80

# INV7 thresholds
INV7_ROLLING_FLIP_RATE_CEILING = 3.0


@dataclass
class V4InvariantResult:
    """Structured outcome of a single invariant evaluation."""
    invariant: str                      # "INV3" | "INV5" | "INV7"
    passed: bool
    metric_values: dict                 # invariant-specific numerics
    failing_cycles: list[tuple[int, int]] = field(default_factory=list)
    reason: Optional[str] = None        # root-cause hint on fail

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ═══════════════════════════════════════════════════════════════
# INV3 — Capital Utilisation
# ═══════════════════════════════════════════════════════════════

def evaluate_inv3(snapshots: list[V4CycleSnapshot]) -> V4InvariantResult:
    """INV3 — both gates must clear on the post-warmup slice:
      (a) mean(overcommit_factor) ≥ 1.3
      (b) mean(actual_notional / target_notional) ≥ 0.9

    actual_notional is `total_notional` (sum deploy est_capital_cost).
    target_notional is the Patch 11/13 `_target_notional` stamp.
    Cycles where target_notional is None (e.g., OFF mode before ACTIVE
    kicks in) are EXCLUDED from the ratio average but still contribute
    to the overcommit-factor average when a factor was stamped.

    Failing-cycle ranges: contiguous runs of cycles where EITHER gate
    was below its per-cycle floor are reported so `--trace-invariants`
    can surface them."""
    post_warm = [s for s in snapshots if s.cycle > WARMUP_CUTOFF]
    if not post_warm:
        return V4InvariantResult(
            invariant="INV3", passed=False,
            metric_values={
                "avg_overcommit": None,
                "avg_actual_over_target": None,
                "post_warmup_cycles": 0,
            },
            reason="no post-warmup cycles (run too short)",
        )

    overcommit_vals = [
        float(s.overcommit_factor) for s in post_warm
        if s.overcommit_factor is not None
    ]
    ratio_vals = []
    per_cycle_ratios: list[Optional[float]] = []
    for s in post_warm:
        if s.target_notional is None or s.target_notional <= 0:
            per_cycle_ratios.append(None)
            continue
        r = s.total_notional / s.target_notional
        ratio_vals.append(r)
        per_cycle_ratios.append(r)

    avg_overcommit = (
        sum(overcommit_vals) / len(overcommit_vals) if overcommit_vals else 0.0
    )
    avg_ratio = (
        sum(ratio_vals) / len(ratio_vals) if ratio_vals else 0.0
    )

    passed = (
        avg_overcommit >= INV3_AVG_OVERCOMMIT_FLOOR
        and avg_ratio >= INV3_ACTUAL_OVER_TARGET_FLOOR
    )

    # Failing-cycle runs: a cycle fails if its ratio < 0.9 (we can't
    # meaningfully mark per-cycle overcommit failure since the floor
    # is an AVERAGE gate, not a per-cycle one).
    failing_cycles: list[tuple[int, int]] = []
    run_start: Optional[int] = None
    for s, r in zip(post_warm, per_cycle_ratios):
        if r is None or r >= INV3_ACTUAL_OVER_TARGET_FLOOR:
            if run_start is not None:
                failing_cycles.append((run_start, s.cycle - 1))
                run_start = None
        else:
            if run_start is None:
                run_start = s.cycle
    if run_start is not None:
        failing_cycles.append((run_start, post_warm[-1].cycle))

    reason = None
    if not passed:
        parts = []
        if avg_overcommit < INV3_AVG_OVERCOMMIT_FLOOR:
            parts.append(
                f"avg_overcommit={avg_overcommit:.3f} < "
                f"{INV3_AVG_OVERCOMMIT_FLOOR}"
            )
        if avg_ratio < INV3_ACTUAL_OVER_TARGET_FLOOR:
            parts.append(
                f"avg(actual/target)={avg_ratio:.3f} < "
                f"{INV3_ACTUAL_OVER_TARGET_FLOOR}"
            )
        # Root-cause hint — marginal-efficiency gate is the usual
        # suspect when notional stalls well below target.
        hint = ""
        if avg_ratio < 0.5 and overcommit_vals:
            hint = "; likely marginal_efficiency_gate_blocked_allocation"
        elif avg_ratio < INV3_ACTUAL_OVER_TARGET_FLOOR and overcommit_vals:
            # Check if per-market caps are the binding constraint — if
            # min_size_alloc_count stays high, markets are pinned at
            # the floor and can't be upsized further.
            avg_min_size = sum(s.min_size_alloc_count for s in post_warm) / len(post_warm)
            if avg_min_size > 0.5 * max(
                s.number_of_deployed_markets for s in post_warm
            ):
                hint = "; likely per_market_cap_binding (many markets at min_size)"
        reason = "; ".join(parts) + hint

    return V4InvariantResult(
        invariant="INV3",
        passed=passed,
        metric_values={
            "avg_overcommit": round(avg_overcommit, 4),
            "avg_actual_over_target": round(avg_ratio, 4),
            "post_warmup_cycles": len(post_warm),
            "ratio_samples": len(ratio_vals),
            "overcommit_samples": len(overcommit_vals),
        },
        failing_cycles=failing_cycles,
        reason=reason,
    )


# ═══════════════════════════════════════════════════════════════
# INV5 — Deployment Floor
# ═══════════════════════════════════════════════════════════════

def evaluate_inv5(snapshots: list[V4CycleSnapshot]) -> V4InvariantResult:
    """INV5 — deploy_ratio ≥ 0.85 on at least 80% of post-warmup cycles.

    Failing-cycle ranges: contiguous runs with deploy_ratio < 0.85."""
    post_warm = [s for s in snapshots if s.cycle > WARMUP_CUTOFF]
    if not post_warm:
        return V4InvariantResult(
            invariant="INV5", passed=False,
            metric_values={"frac_meeting_floor": None, "post_warmup_cycles": 0},
            reason="no post-warmup cycles (run too short)",
        )

    passing = sum(
        1 for s in post_warm if s.deploy_ratio >= INV5_DEPLOY_RATIO_FLOOR
    )
    frac = passing / len(post_warm)
    passed = frac >= INV5_PASS_FRACTION

    failing_cycles: list[tuple[int, int]] = []
    run_start: Optional[int] = None
    for s in post_warm:
        if s.deploy_ratio < INV5_DEPLOY_RATIO_FLOOR:
            if run_start is None:
                run_start = s.cycle
        else:
            if run_start is not None:
                failing_cycles.append((run_start, s.cycle - 1))
                run_start = None
    if run_start is not None:
        failing_cycles.append((run_start, post_warm[-1].cycle))

    reason = None
    if not passed:
        reason = (
            f"deploy_ratio ≥ {INV5_DEPLOY_RATIO_FLOOR:.2f} held on only "
            f"{frac:.1%} of {len(post_warm)} post-warmup cycles "
            f"(required ≥ {INV5_PASS_FRACTION:.0%})"
        )
        # Hint — if the longest failing run is large and the Patch 13
        # efficiency penalty was active throughout, the penalty likely
        # contracted allocations in step with the hysteresis lock.
        if failing_cycles:
            longest = max(
                (end - start + 1) for start, end in failing_cycles
            )
            if longest > 50:
                # Check penalty flag on the affected window.
                penalty_in_runs = any(
                    any(
                        s.efficiency_penalty_active for s in post_warm
                        if start <= s.cycle <= end
                    )
                    for start, end in failing_cycles
                )
                if penalty_in_runs:
                    reason += "; efficiency_penalty_active during failing window"

    return V4InvariantResult(
        invariant="INV5",
        passed=passed,
        metric_values={
            "frac_meeting_floor": round(frac, 4),
            "post_warmup_cycles": len(post_warm),
            "floor": INV5_DEPLOY_RATIO_FLOOR,
            "required_fraction": INV5_PASS_FRACTION,
        },
        failing_cycles=failing_cycles,
        reason=reason,
    )


# ═══════════════════════════════════════════════════════════════
# INV7 — Oscillation Stability
# ═══════════════════════════════════════════════════════════════

def _find_sustained_alternations(
    snapshots: list[V4CycleSnapshot], min_length: int,
) -> list[tuple[int, int]]:
    """Scan for runs where every consecutive NON-ZERO delta flips sign
    (|run| > min_length). Zero-deltas terminate a run (dead-band filters
    broke the alternation).

    Returns list of (cycle_start, cycle_end) tuples for each offending
    run. Only reports runs whose length exceeds `min_length`."""
    runs: list[tuple[int, int]] = []
    run_start: Optional[int] = None
    prev_nonzero_sign: Optional[int] = None
    prev_cycle: Optional[int] = None

    for s in snapshots:
        d = float(s.delta_capital_scale)
        if d == 0.0:
            # Dead-band hit → end any running alternation.
            if run_start is not None and prev_cycle is not None:
                length = prev_cycle - run_start + 1
                if length > min_length:
                    runs.append((run_start, prev_cycle))
                run_start = None
                prev_nonzero_sign = None
            continue
        sign = 1 if d > 0 else -1
        if prev_nonzero_sign is None:
            # First non-zero delta — start tracking.
            prev_nonzero_sign = sign
            run_start = s.cycle
            prev_cycle = s.cycle
            continue
        if sign != prev_nonzero_sign:
            # Alternation continues.
            prev_nonzero_sign = sign
            prev_cycle = s.cycle
        else:
            # Same sign as prev non-zero → alternation broken.
            if run_start is not None and prev_cycle is not None:
                length = prev_cycle - run_start + 1
                if length > min_length:
                    runs.append((run_start, prev_cycle))
            # Restart tracking from this cycle.
            prev_nonzero_sign = sign
            run_start = s.cycle
            prev_cycle = s.cycle

    # Tail — close any open run.
    if run_start is not None and prev_cycle is not None:
        length = prev_cycle - run_start + 1
        if length > min_length:
            runs.append((run_start, prev_cycle))

    return runs


def evaluate_inv7(snapshots: list[V4CycleSnapshot]) -> V4InvariantResult:
    """INV7 — rolling_flip_rate_100 ≤ 3 on every post-warmup cycle AND
    no sustained alternating pattern > 20 cycles on the post-warmup
    slice."""
    post_warm = [s for s in snapshots if s.cycle > WARMUP_CUTOFF]
    if not post_warm:
        return V4InvariantResult(
            invariant="INV7", passed=False,
            metric_values={
                "max_flip_rate_100": None,
                "post_warmup_cycles": 0,
                "sustained_alt_runs": 0,
            },
            reason="no post-warmup cycles (run too short)",
        )

    max_flip_rate = max(s.rolling_flip_rate_100 for s in post_warm)
    breached_cycles = [
        s.cycle for s in post_warm
        if s.rolling_flip_rate_100 > INV7_ROLLING_FLIP_RATE_CEILING
    ]
    sustained_runs = _find_sustained_alternations(
        post_warm, SUSTAINED_ALT_MIN_LENGTH,
    )

    rate_gate = max_flip_rate <= INV7_ROLLING_FLIP_RATE_CEILING
    alt_gate = len(sustained_runs) == 0
    passed = rate_gate and alt_gate

    # Build contiguous failing-cycle ranges for the rate gate.
    failing_cycles: list[tuple[int, int]] = []
    run_start: Optional[int] = None
    for s in post_warm:
        if s.rolling_flip_rate_100 > INV7_ROLLING_FLIP_RATE_CEILING:
            if run_start is None:
                run_start = s.cycle
        else:
            if run_start is not None:
                failing_cycles.append((run_start, s.cycle - 1))
                run_start = None
    if run_start is not None:
        failing_cycles.append((run_start, post_warm[-1].cycle))
    # Sustained alternation runs are appended too (they're also failures).
    failing_cycles.extend(sustained_runs)

    reason = None
    if not passed:
        parts = []
        if not rate_gate:
            parts.append(
                f"max rolling_flip_rate_100={max_flip_rate:.1f} > "
                f"{INV7_ROLLING_FLIP_RATE_CEILING}"
            )
        if not alt_gate:
            parts.append(
                f"{len(sustained_runs)} sustained-alternation run(s) "
                f">{SUSTAINED_ALT_MIN_LENGTH} cycles"
            )
        reason = "; ".join(parts)

    return V4InvariantResult(
        invariant="INV7",
        passed=passed,
        metric_values={
            "max_flip_rate_100": round(max_flip_rate, 3),
            "breached_cycles": len(breached_cycles),
            "sustained_alt_runs": len(sustained_runs),
            "post_warmup_cycles": len(post_warm),
            "flip_rate_ceiling": INV7_ROLLING_FLIP_RATE_CEILING,
            "sustained_alt_min_length": SUSTAINED_ALT_MIN_LENGTH,
        },
        failing_cycles=failing_cycles,
        reason=reason,
    )


# ═══════════════════════════════════════════════════════════════
# Umbrella — evaluate all three
# ═══════════════════════════════════════════════════════════════

def evaluate_all(
    snapshots: list[V4CycleSnapshot],
) -> dict[str, V4InvariantResult]:
    return {
        "INV3": evaluate_inv3(snapshots),
        "INV5": evaluate_inv5(snapshots),
        "INV7": evaluate_inv7(snapshots),
    }
