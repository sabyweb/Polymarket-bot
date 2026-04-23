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
    CLAMP_CAP, CLAMP_TRUST, CLAMP_LAMBDA_1, CLAMP_LAMBDA_2,
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

# ── SIM PATCH PART 8 — sanity check thresholds ─────────────
# These are SIMULATION hygiene assertions, not system audit thresholds.
# They verify the harness itself produced valid per-cycle inputs for
# the production rules to consume. Any violation means the sim is
# broken, NOT that the system under test is broken.
SANITY_LOSS_PER_CAPITAL_MAX = 0.20
SANITY_FILL_RATE_MAX = 1.0
SANITY_FILL_RATE_MIN = 0.0
# A single cycle may legitimately have zero reward (e.g., no deploy).
# We check the post-run AVERAGE (rather than per-cycle) so the assertion
# tolerates cold-start gaps.
SANITY_REWARD_PER_DOLLAR_MIN_AVG = 0.0


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
    # PATCH 7 / V3 — EXPECTED-capital invariant (not naive deployed).
    # Polymarket permits notional overcommitment; the real hard ceiling
    # is Σ(p_fill × size) ≤ total_capital × tolerance. Under legacy
    # (learning_state=None) calls, _p_fill is still stamped so the
    # invariant remains meaningful; falls back to the naive check when
    # _p_fill is absent entirely (rare: allocations missing observability).
    capital_deployed = sum(
        float(a.get("est_capital_cost") or 0.0)
        for a in allocations
        if a.get("action") == "deploy"
    )
    any_p_fill_stamped = any(
        a.get("_p_fill") is not None for a in allocations
        if a.get("action") == "deploy"
    )
    if any_p_fill_stamped:
        expected_capital = sum(
            float(a.get("_p_fill") or 0.0)
            * float(a.get("est_capital_cost") or 0.0)
            for a in allocations
            if a.get("action") == "deploy"
        )
        if expected_capital > total_capital * CAPITAL_OVERRUN_TOLERANCE:
            out.append(InvariantViolation(
                "expected_capital_overrun",
                f"cycle {cycle}: expected=${expected_capital:.2f} > "
                f"budget=${total_capital:.2f} * {CAPITAL_OVERRUN_TOLERANCE}",
            ))
    else:
        # Legacy fallback (no p_fill observability): naive notional check.
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
            ("capital_scale", s.capital_scale, CLAMP_CAP),
            ("reward_trust", s.reward_trust, CLAMP_TRUST),
            ("lambda_1", s.lambda_1, CLAMP_LAMBDA_1),
            ("lambda_2", s.lambda_2, CLAMP_LAMBDA_2),
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


def check_sim_sanity(
    metrics_tracker,
    per_cycle_fill_rates: list,
    per_cycle_loss_per_capital: list,
    per_cycle_losses: list,
) -> list[InvariantViolation]:
    """SIM PATCH PART 8 — sanity checks on the harness itself.

    Returns violations if the simulation harness produced inputs that
    should never occur with correct per-cycle accounting. A violation
    here means the harness is broken, not the production system.

    Checks:
      1. 0 <= loss_per_capital <= 0.2 (per cycle)
      2. 0 <= fill_rate <= 1 (per cycle)
      3. reward_per_dollar average >= 0 (post-run)
      4. cycle_loss does NOT grow monotonically (per-cycle semantics)
    """
    out: list[InvariantViolation] = []

    for i, lpc in enumerate(per_cycle_loss_per_capital):
        if lpc is None:
            continue
        if not (0.0 <= float(lpc) <= SANITY_LOSS_PER_CAPITAL_MAX):
            out.append(InvariantViolation(
                "sanity_loss_per_capital",
                f"cycle {i}: loss_per_capital={lpc} outside "
                f"[0, {SANITY_LOSS_PER_CAPITAL_MAX}]",
            ))

    for i, fr in enumerate(per_cycle_fill_rates):
        if fr is None:
            continue
        if not (SANITY_FILL_RATE_MIN <= float(fr) <= SANITY_FILL_RATE_MAX):
            out.append(InvariantViolation(
                "sanity_fill_rate",
                f"cycle {i}: fill_rate={fr} outside "
                f"[{SANITY_FILL_RATE_MIN}, {SANITY_FILL_RATE_MAX}]",
            ))

    # Average reward_per_dollar over the run must be >= 0.
    rpd = metrics_tracker.series("reward_efficiency")
    if rpd:
        avg_rpd = sum(rpd) / len(rpd)
        if avg_rpd < SANITY_REWARD_PER_DOLLAR_MIN_AVG:
            out.append(InvariantViolation(
                "sanity_avg_reward_per_dollar",
                f"avg reward_per_dollar={avg_rpd} < "
                f"{SANITY_REWARD_PER_DOLLAR_MIN_AVG}",
            ))

    # cycle_loss must not grow monotonically — that's the "cumulative
    # bleed" smell. We tolerate a short early-run monotone stretch
    # (cold-start accumulation is possible) but require at least ONE
    # cycle where the loss DECREASES vs. its prior value.
    if len(per_cycle_losses) >= 10:
        series = [float(x) for x in per_cycle_losses if x is not None]
        if len(series) >= 10:
            strictly_monotone = all(
                b >= a for a, b in zip(series, series[1:])
            )
            if strictly_monotone:
                out.append(InvariantViolation(
                    "sanity_cumulative_bleed",
                    "cycle_loss grows monotonically across the entire "
                    "run — suggests cumulative accounting where per-"
                    "cycle is required (PART 1 DELETE not taking effect)",
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
