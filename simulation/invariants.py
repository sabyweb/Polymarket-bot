"""simulation/invariants.py — hard invariant assertions.

Two layers:

  1. Per-cycle (`check_per_cycle`) — fast checks against the cycle's
     allocations + applied learning state. Raises InvariantViolation
     on any failure (caller decides whether to fail-fast or accumulate).

  2. Post-run (`check_post_run`) — softer trend assertions over the
     full run history (oscillation persistence, no-update gates).

All thresholds are CONSTANTS pulled from production modules whenever
possible; we never re-define them with looser values.
"""

from __future__ import annotations
from typing import Optional

from profit.learning import (
    CLAMP_AGGR, CLAMP_CAP, CLAMP_RISK, CLAMP_TRUST,
)


class InvariantViolation(Exception):
    """Raised by an invariant check when the system breaks a guarantee."""

    def __init__(self, name: str, detail: str):
        super().__init__(f"{name}: {detail}")
        self.name = name
        self.detail = detail


# Hard caps from production code paths.
CAPITAL_OVERRUN_TOLERANCE = 1.05
EXPLORATION_PCT_MAX = 0.15
CLUSTER_PCT_MAX = 0.30


# Trend thresholds (post-run).
NO_UPDATE_WINDOW = 50           # cycles
OSCILLATION_RUN_LIMIT = 20      # consecutive oscillating windows


def check_per_cycle(
    cycle: int,
    allocations: list[dict],
    applied_state,
    total_capital: float,
    total_ev: float,
    exploration_pct: float,
) -> list[InvariantViolation]:
    """Return a list of violations (empty when all invariants hold)."""
    out: list[InvariantViolation] = []

    # --- capital ----------------------------------------------
    capital_deployed = sum(
        float(a.get("est_capital_cost") or 0.0)
        for a in allocations
        if a.get("action") == "deploy"
    )
    if capital_deployed > total_capital * CAPITAL_OVERRUN_TOLERANCE:
        out.append(InvariantViolation(
            "capital_overrun",
            f"cycle {cycle}: deployed=${capital_deployed:.2f} > "
            f"budget=${total_capital:.2f} * {CAPITAL_OVERRUN_TOLERANCE}",
        ))

    # --- profit guard -----------------------------------------
    if total_ev < 0 and capital_deployed > 0:
        out.append(InvariantViolation(
            "ev_negative_with_deployment",
            f"cycle {cycle}: total_ev=${total_ev:.2f} < 0 yet "
            f"deployed=${capital_deployed:.2f}",
        ))

    # --- learning scalars within clamps -----------------------
    s = applied_state
    if s is not None:
        for name, val, (lo, hi) in (
            ("aggressiveness", s.aggressiveness, CLAMP_AGGR),
            ("capital_scale", s.capital_scale, CLAMP_CAP),
            ("risk_multiplier", s.risk_multiplier, CLAMP_RISK),
            ("reward_trust", s.reward_trust, CLAMP_TRUST),
        ):
            if not (lo - 1e-9 <= float(val) <= hi + 1e-9):
                out.append(InvariantViolation(
                    f"{name}_out_of_clamp",
                    f"cycle {cycle}: {name}={val:.4f} not in [{lo}, {hi}]",
                ))

    # --- exploration budget cap -------------------------------
    if exploration_pct > EXPLORATION_PCT_MAX + 1e-9:
        out.append(InvariantViolation(
            "exploration_pct_overrun",
            f"cycle {cycle}: exploration_pct={exploration_pct:.3f} > "
            f"{EXPLORATION_PCT_MAX}",
        ))

    # --- cluster concentration --------------------------------
    # Allocations carry `_cluster_id` only when correlation grouping
    # actually fired; default to per-market clusters (no concentration).
    cluster_totals: dict = {}
    for a in allocations:
        if a.get("action") != "deploy":
            continue
        cid = a.get("_cluster_id") or a.get("condition_id")
        cap = float(a.get("est_capital_cost") or 0.0)
        cluster_totals[cid] = cluster_totals.get(cid, 0.0) + cap
    if cluster_totals and total_capital > 0:
        max_cluster = max(cluster_totals.values())
        if max_cluster / total_capital > CLUSTER_PCT_MAX + 1e-6:
            out.append(InvariantViolation(
                "cluster_overconcentration",
                f"cycle {cycle}: max cluster=${max_cluster:.0f} "
                f"({max_cluster/total_capital:.1%}) > {CLUSTER_PCT_MAX}",
            ))

    return out


def check_post_run(
    metrics_tracker,
    learning_history: list,
) -> list[InvariantViolation]:
    """Aggregate trend invariants over the full run."""
    out: list[InvariantViolation] = []

    # --- valid_cycles counter monotonicity --------------------
    # The counter is reported as `applied_state.valid_cycles_observed`
    # which is 0 in OFF/SHADOW (neutral applied state) and the real
    # persisted value in ACTIVE. So an OFF→ACTIVE transition naturally
    # produces a one-time jump from 0 to whatever counter the SHADOW
    # phase accumulated. We tolerate that single transition jump and
    # only flag deltas > 1 AFTER the first ACTIVE entry.
    counters = [
        int(s.get("valid_cycles_observed", 0) or 0)
        for s in learning_history
    ]
    modes = [str(s.get("mode") or "OFF") for s in learning_history]
    first_active_idx: Optional[int] = None
    for i, m in enumerate(modes):
        if m == "ACTIVE":
            first_active_idx = i
            break
    for i in range(1, len(counters)):
        delta = counters[i] - counters[i - 1]
        if delta < 0:
            out.append(InvariantViolation(
                "valid_cycles_decreased",
                f"counter went {counters[i-1]} -> {counters[i]} at idx {i}",
            ))
        # Skip the OFF→ACTIVE transition jump (counter resets from 0 to
        # the real persisted value as we leave the neutral applied state).
        if first_active_idx is not None and i == first_active_idx:
            continue
        if delta > 1 and modes[i] == "ACTIVE" and modes[i - 1] == "ACTIVE":
            out.append(InvariantViolation(
                "valid_cycles_jumped",
                f"counter jumped by {delta} (>1) at idx {i} "
                f"(both cycles in ACTIVE)",
            ))

    # --- no high-frequency oscillation in capital_scale -------
    # True oscillation = the LEARNED capital_scale repeatedly changes
    # direction. A monotone transition (e.g. 1.0 → 0.30 contraction)
    # has high amplitude but ZERO sign changes — that is correct
    # learning, not oscillation. We measure direction-flips per window:
    # the delta series must cross zero (change sign) more than a
    # threshold fraction of the window to qualify as oscillating.
    window = 20
    # Require >= 6 direction reversals in a 20-step window to count as
    # oscillating — i.e., the series flips direction at least 30% of
    # the time. A monotone transition produces 0–2 reversals from noise.
    sign_change_threshold = 6
    cap_scale_series = [
        float(s.get("capital_scale", 1.0) or 1.0)
        for s in learning_history
    ]
    deltas = [
        cap_scale_series[i] - cap_scale_series[i - 1]
        for i in range(1, len(cap_scale_series))
    ]
    consec = 0
    max_consec = 0
    for i in range(window, len(deltas) + 1):
        seg = deltas[i - window:i]
        # Count sign changes in the delta series (skip near-zero deltas
        # which are EMA convergence noise).
        sign_changes = 0
        prev_sign = 0
        for d in seg:
            if abs(d) < 1e-6:
                continue
            sign = 1 if d > 0 else -1
            if prev_sign != 0 and sign != prev_sign:
                sign_changes += 1
            prev_sign = sign
        if sign_changes >= sign_change_threshold:
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0
    if max_consec > OSCILLATION_RUN_LIMIT:
        out.append(InvariantViolation(
            "oscillation_persistent",
            f"capital_scale flipped direction >= {sign_change_threshold} "
            f"times for {max_consec} consecutive {window}-cycle windows "
            f"(limit {OSCILLATION_RUN_LIMIT})",
        ))

    return out
