"""Portfolio allocator — risk-adjusted capital allocation across markets.

Replaces compute_allocations() when calibrator is ready.
Produces identical output format (list[dict]) so safety filter
and reward_farmer work unchanged.

Algorithm (STEP 11 — pipeline order is load-bearing):
  Scaling:  deployable = total_capital * sqrt(efficiency / target)
  Phase A:  Risk-adjusted scoring  RAS = EV / (1 + p_fill * loss)
            × bandit multiplier    final = RAS * bandit
  Phase B:  Capital budgeting (proportional to final, hard per-market cap)
  Phase B2: Correlation cluster caps + capital redistribution
  Phase C:  Depth-aware sizing
  Phase D:  Rebalance churn control + re-applied caps
  Phase E:  Regime scaling (hostile market → reduce deployable 30%)
  Phase F:  Capital conservation enforcement (safety invariant)

SafetyController runs AFTER this module via the existing allocation_writer
pipeline. Nothing in this file may bypass it.
"""

import logging
import math

from oversight.market_scorer import ScoredMarket
from oversight.allocation_writer import _to_dict, _est_market_cost

from .sizing import compute_shares, _cost_per_share_both
from .efficiency import get_efficiency, get_target_market_count
from .rebalance import compute_deltas
from .correlation import (
    build_fill_clusters, apply_cluster_caps, compute_cluster_exposure,
    DEFAULT_MAX_CLUSTER_PCT,
)
from .bandit import Bandit
from .regime import detect_regime, HOSTILE_CAPITAL_SCALE

log = logging.getLogger("profit.allocator")

# Minimum risk-adjusted score to deploy
MIN_RAS_TO_DEPLOY = 0.0

# Rebalance credit
REBALANCE_CREDIT_PCT = 0.80

# ── PATCH 6 (DEPLOYMENT TARGETING) CONSTANTS ────────────────────
# Drives Part 2's deployment-correction block. When utilisation falls
# below MIN_DEPLOY_RATIO (and learning mode == ACTIVE) deploy allocations
# are multiplied by DEPLOYMENT_BOOST; capital conservation then reins any
# over-budget result back to the budget band.
TARGET_DEPLOY_RATIO = 0.85
MIN_DEPLOY_RATIO = 0.75
DEPLOYMENT_BOOST = 1.05

# Part 3 blend weights (objective correction).
PATCH6_EFFICIENCY_WEIGHT = 0.7
PATCH6_RAW_EV_WEIGHT = 0.3

# Part 4 floor — minimum number of deployed markets in ACTIVE mode.
PATCH6_MIN_MARKETS = 5

# ── PATCH 7 (OVERCOMMIT) CONSTANTS ──────────────────────────────
# Polymarket permits placing resting orders whose notional sum exceeds
# available balance — if one fills, the rest cancel. The allocator
# exploits this by budgeting up to `deployable_capital × overcommit_factor`
# in notional, then constrains the EXPECTED capital consumption
# (Σ p_fill × size) to ≤ total_capital × EXPECTED_CAPITAL_BUFFER.
OVERCOMMIT_MIN = 1.5
OVERCOMMIT_MAX = 6.0
OVERCOMMIT_DEFAULT = 3.0
EXPECTED_CAPITAL_BUFFER = 0.95   # keep 5% headroom below the nominal capital

# ── PATCH 9 (DEPLOYMENT EXPANSION) CONSTANTS ────────────────────
# Shift ACTIVE-mode allocator from "few markets × large size" to
# "many markets × small size". Lifts target_market_count, softens
# per_market_cap, halves per-market allocation, and enforces a hard
# market-count floor. Every branch is gated on ls_mode == "ACTIVE"
# so OFF/SHADOW/None callers see the pre-Patch-9 behaviour unchanged.
MIN_MARKETS_BASE = 12
MAX_MARKETS_CAP = 60
MIN_SIZE_REDUCTION_FACTOR = 0.5
MARKET_EXPANSION_FACTOR = 1.5
LOW_SCORE_EXPANSION_THRESHOLD = 0.6
MIN_MARKETS_ACTIVE_FLOOR = 15

# ── PATCH 10 (EXPOSURE FORCING) CONSTANTS ───────────────────────
# Shifts the ACTIVE-mode objective from "maximise EV per dollar" to
# "maximise reward exposure subject to expected_capital constraint".
# Every branch gates on ls_mode == "ACTIVE"; OFF/SHADOW/None keep the
# pre-Patch-10 EV-disciplined behaviour.
MIN_DEPLOY_RATIO_ACTIVE = 0.85        # floor for notional / total_capital
FORCE_DEPLOY_RATIO_TARGET = 0.95      # where the forced-exposure top-up aims
LOW_EV_ALLOWANCE_FACTOR = 0.5         # relax MIN_EV_THRESHOLD by this factor
NEGATIVE_EV_TOLERANCE = -0.02         # allow slightly negative EV, never lower
EXPOSURE_PRIORITY_WEIGHT = 0.3        # final_score boost in ACTIVE
# Local copy of calibration.manager.MIN_EV_THRESHOLD so the allocator
# doesn't have to cross-import a constant that is used here only for the
# Patch 10 relaxed-threshold computation.
PATCH10_MIN_EV_THRESHOLD = 0.10

# ── PATCH 11 (EXPOSURE SATURATION) CONSTANTS ────────────────────
# Post-Patch-10 layer that upsizes EXISTING deploys (rather than
# promoting avoids) toward the Patch 7 overcommit target. Addresses the
# §4.15.5 tension: when every market is marked _low_ev_override, Patch
# 10 has no avoids to promote, so notional stalls at 0.4–0.9× instead
# of reaching the overcommit target. Saturation closes the gap.
# Invariants preserved: cluster caps re-applied after scaling, and
# expected_capital ≤ EXPECTED_CAPITAL_BUFFER × total_capital re-enforced
# as the final step (Hard Guarantees #1, #2).
EXPOSURE_SATURATION_MAX_SCALE = 3.0   # hard ceiling on cumulative upsize
UPSCALE_STEP = 1.25                   # per-iteration multiplier
UPSCALE_MAX_ITERS = 5                 # bounded loop; ≤ 1.25^5 = 3.05× → clamp

# Efficiency-based capital scaling (Fix 6: sqrt damping)
TARGET_EFFICIENCY = 0.008
MIN_EFFICIENCY_SCALE = 0.30
MAX_EFFICIENCY_SCALE = 1.0

# Fix 5: Clustering failure tracking
CLUSTER_FAILURE_CONFIDENCE_PENALTY = 0.80
CLUSTER_FAILURE_WARN_CYCLES = 10

# Module-level counter for clustering failures (Fix 5)
_cycles_without_clustering: int = 0


def _compute_exploration_pct(efficiency, baseline) -> float:
    """PART 4 — dynamic exploration budget as a fraction of deployable
    capital. Baseline (5%) widens when current efficiency is below the
    baseline, up to a hard ceiling of 15%.

        gap = max(0, (baseline - efficiency) / baseline)
        pct = min(0.15, 0.05 + gap)

    Returns 0.05 (neutral) when either input is None — the allocator
    still respects ls_mode == ACTIVE as the on/off switch.
    """
    if efficiency is None or baseline is None:
        return 0.05
    if baseline <= 0:
        return 0.05
    gap = max(0.0, (baseline - efficiency) / baseline)
    return min(0.15, 0.05 + gap)


def _efficiency_quintiles(eff_map: dict) -> tuple:
    """FIX 4 — return (p20, p80) cut points from the per-market efficiency
    map, or (None, None) if fewer than 5 markets (quintile degenerate).

    Uses simple index-based cuts on the sorted values. Markets at or below
    p20 are "bottom 20%", markets at or above p80 are "top 20%"."""
    if not eff_map or len(eff_map) < 5:
        return (None, None)
    vals = sorted(eff_map.values())
    n = len(vals)
    # Integer indices keep this deterministic and tie-safe. With n=5:
    # p20 = vals[1], p80 = vals[3] (bottom 1, top 1 as 20%).
    p20 = vals[max(0, n // 5 - 1)]
    p80 = vals[min(n - 1, 4 * n // 5)]
    return (p20, p80)


def _efficiency_multiplier(
    cid: str, eff_map: dict, p20, p80,
) -> float:
    """FIX 4 — return 0.8, 1.0, or 1.1 based on per-market efficiency
    quintile position. When the market has no entry in the map, or the
    map is too small, return 1.0 (neutral)."""
    if p20 is None or p80 is None:
        return 1.0
    eff = eff_map.get(cid)
    if eff is None:
        return 1.0
    if eff <= p20:
        return 0.8
    if eff >= p80:
        return 1.1
    return 1.0


def _risk_adjusted_score(ev_per_day: float, p_fill_24h: float,
                         loss_per_fill: float,
                         risk_multiplier: float = 1.0) -> float:
    """FIX 1 + 14: score = EV / (1 + p_fill * loss * risk_multiplier).

    - Uses raw p_fill (no 0.99 cap).
    - Uses loss term (previous formula ignored it — see audit finding #1).
    - No fallback_penalty — model_confidence is applied upstream in the
      calibrator and must not be double-counted here.
    - Negative or zero EV → 0 (market is not profitable; no positive score).
    - risk_multiplier: learning-loop scalar ∈ [1.0, 2.0]. 1.0 is neutral
      (no learning correction). Higher values inflate the denominator,
      discounting markets with high fill-loss exposure more aggressively.
    """
    if ev_per_day <= 0:
        return 0.0
    p = max(0.0, p_fill_24h)
    loss = max(0.0, loss_per_fill)
    rm = max(1.0, float(risk_multiplier))  # invariant: never below 1.0
    denom = 1.0 + p * loss * rm
    if denom <= 0:  # belt-and-braces — algebraically can't happen
        return 0.0
    return ev_per_day / denom


def _compute_efficiency_scale(db_path: str) -> float:
    """FIX 12: Unified efficiency-based capital scale.

    - efficiency is None (no computation possible) → MAX (1.0).
    - otherwise scale = sqrt(efficiency / target), clamped to [MIN, MAX].
    - No dependency on days_with_data — zero-measured efficiency produces
      MIN through the sqrt/clamp path, which is the correct behavior
      regardless of sample age.
    """
    try:
        eff = get_efficiency(db_path)
    except Exception:
        return MAX_EFFICIENCY_SCALE

    rpd = eff.get("reward_per_dollar")
    if rpd is None:
        return MAX_EFFICIENCY_SCALE

    raw_ratio = max(0.0, rpd) / TARGET_EFFICIENCY
    scale = math.sqrt(raw_ratio)
    scale = max(MIN_EFFICIENCY_SCALE, min(MAX_EFFICIENCY_SCALE, scale))

    log.info(
        f"[PROFIT] Efficiency: {rpd:.4f} | Target: {TARGET_EFFICIENCY} | "
        f"Scale: {scale:.2f}"
    )
    return scale


def _redistribute_cluster_savings(
    allocations: list[dict],
    clusters: dict[str, int],
    deployable_capital: float,
    max_cluster_pct: float,
) -> list[dict]:
    """Fix 3: Redistribute capital freed by cluster caps.

    Capital saved from capped clusters goes to uncapped markets
    proportionally by score. Re-checks cluster caps after redistribution.

    FIX 5: `remaining` is decremented inside the loop so cumulative
    redistribution never exceeds the initial surplus (share truncation
    and cluster headroom limits can otherwise cause drift).
    """
    allocated = sum(
        a.get("est_capital_cost", 0) for a in allocations
        if a.get("action") == "deploy"
    )
    remaining = deployable_capital - allocated

    if remaining <= deployable_capital * 0.05:
        return allocations  # not enough to redistribute

    # Eligible: deployed markets NOT cluster-capped
    eligible = [
        a for a in allocations
        if a.get("action") == "deploy" and not a.get("_cluster_capped")
    ]
    if not eligible:
        return allocations

    total_score = sum(max(a.get("_final_score", 0), 0) for a in eligible)
    if total_score <= 0:
        return allocations

    max_cluster_capital = deployable_capital * max_cluster_pct
    initial_remaining = remaining

    for a in eligible:
        if remaining <= 0:
            break
        score = max(a.get("_final_score", 0), 0)
        # Proportional share of initial surplus, capped at what's left
        add_capital = initial_remaining * (score / total_score)
        add_capital = min(add_capital, remaining)

        # Check cluster cap wouldn't be violated
        cid = a["condition_id"]
        cluster_id = clusters.get(cid)
        if cluster_id is not None:
            exposure = compute_cluster_exposure(allocations, clusters)
            current_exposure = exposure.get(cluster_id, 0)
            headroom = max_cluster_capital - current_exposure
            add_capital = min(add_capital, max(0, headroom))

        if add_capital <= 0:
            continue

        # Add shares
        spread = a.get("max_spread", 0.045)
        s = spread if spread > 0 else 0.045
        cpb = 2 * max(0.05, (1.0 - 2 * s) / 2)
        extra_shares = int(add_capital / cpb) if cpb > 0 else 0
        if extra_shares > 0:
            a["shares_per_side"] += extra_shares
            a["est_capital_cost"] = round(a["shares_per_side"] * cpb, 2)
            # FIX 5: decrement remaining by actual capital consumed
            remaining -= extra_shares * cpb

    return allocations


def _apply_per_market_cap(
    allocations: list[dict], per_market_cap: float,
) -> list[dict]:
    """FIX 8: Clamp any allocation that exceeds per-market capital cap.

    Called post-rebalance since delta computation can push shares above
    the cap if the pre-existing position was larger than our new target.
    """
    for a in allocations:
        if a.get("action") != "deploy":
            continue
        spread = a.get("max_spread", 0.045)
        s = spread if spread > 0 else 0.045
        cpb = 2 * max(0.05, (1.0 - 2 * s) / 2)
        if cpb <= 0:
            continue
        max_shares = int(per_market_cap / cpb)
        shares = a.get("shares_per_side", 0)
        if shares > max_shares:
            new_shares = max(int(a.get("min_size", 50)), max_shares)
            a["shares_per_side"] = new_shares
            a["est_capital_cost"] = round(new_shares * cpb, 2)
    return allocations


def _compute_overcommit_factor(learning_state, regime_multiplier: float) -> float:
    """PATCH 7 — compute the overcommit factor used to expand the
    allocator's notional budget.

    Baseline OVERCOMMIT_DEFAULT (=3.0) is scaled by the learning scalars:
      • aggressiveness ∈ [0.3, 1.5]: boosts the factor by 0.8 + 0.4×aggr
        (ranges from 0.92 at aggr=0.3 to 1.4 at aggr=1.5).
      • reward_trust ∈ [0.5, 1.0]: boosts by 0.9 + 0.2×trust
        (ranges from 1.0 at trust=0.5 to 1.1 at trust=1.0).
    The regime_multiplier (1.0 normal, <1.0 hostile) shrinks the factor
    so hostile markets see tighter commits.

    Final value clamped to [OVERCOMMIT_MIN, OVERCOMMIT_MAX].
    """
    base = OVERCOMMIT_DEFAULT
    if learning_state is not None:
        aggr = float(getattr(learning_state, "aggressiveness", 1.0) or 1.0)
        trust = float(getattr(learning_state, "reward_trust", 1.0) or 1.0)
        base *= (0.8 + 0.4 * aggr)
        base *= (0.9 + 0.2 * trust)
    base *= float(regime_multiplier)
    return max(OVERCOMMIT_MIN, min(OVERCOMMIT_MAX, base))


def _enforce_expected_capital(
    allocations: list[dict], total_capital: float,
) -> list[dict]:
    """PATCH 7 — cap EXPECTED capital consumption, not raw notional.

    Expected capital = Σ (_p_fill × est_capital_cost) over deploy rows.
    If that sum exceeds total_capital × EXPECTED_CAPITAL_BUFFER, scale
    each deploy allocation down uniformly so the invariant holds.

    Unlike the legacy _enforce_capital_conservation (which treated notional
    as the ceiling), this allows overcommitment on the notional side as
    long as probabilistic fill consumption stays bounded. min_size floor
    is still honored; shares_per_side stays consistent with est_capital_cost
    (both scaled together, not just est_capital_cost alone).
    """
    if total_capital <= 0:
        return allocations

    deploys = [a for a in allocations if a.get("action") == "deploy"]
    if not deploys:
        return allocations

    expected = 0.0
    for a in deploys:
        p = float(a.get("_p_fill") or 0.0)
        size = float(a.get("est_capital_cost") or 0.0)
        expected += p * size

    ceiling = total_capital * EXPECTED_CAPITAL_BUFFER
    if expected <= ceiling:
        return allocations

    scale = ceiling / max(expected, 1e-9)
    for a in deploys:
        # Scale COST directly (spec intent) — then recompute shares so
        # the allocation's internal state stays consistent. min_size is a
        # hard floor; if many allocations hit it, some overrun may
        # remain, but that's an explicit trade-off the spec accepts.
        old_cost = float(a.get("est_capital_cost") or 0.0)
        new_cost_target = old_cost * scale
        spread = a.get("max_spread", 0.045)
        s = spread if spread > 0 else 0.045
        cpb = 2 * max(0.05, (1.0 - 2 * s) / 2)
        if cpb > 0:
            min_shares = int(a.get("min_size", 50) or 50)
            new_shares = max(min_shares, int(new_cost_target / cpb))
            a["shares_per_side"] = new_shares
            a["est_capital_cost"] = round(new_shares * cpb, 2)
        else:
            a["est_capital_cost"] = round(new_cost_target, 2)
        # Update expected-capital observability to reflect the scaled value.
        a["_expected_capital"] = round(
            float(a.get("_p_fill") or 0.0) * a["est_capital_cost"], 4,
        )
    log.info(
        f"[PROFIT_P7] expected_capital ${expected:.2f} > ceiling "
        f"${ceiling:.2f} — scaled allocations by {scale:.3f}"
    )
    return allocations


def _enforce_capital_conservation(
    allocations: list[dict], budget: float,
    per_market_cap: float | None = None,
) -> list[dict]:
    """PART 7: Total deployed capital must stay within `budget * 1%`.

    Two-sided enforcement:
      • Over-budget by >1%  → scale all deployed markets down proportionally.
      • Under-budget by >1% → redistribute remaining capital proportionally
        to current allocations, respecting per_market_cap if provided.

    Either side may legitimately fail to converge (e.g., min_size floors
    pin total above budget; per-market caps prevent absorbing surplus).
    The function logs and returns the best-effort allocations rather than
    raising — the safety controller downstream is the hard guard.
    """
    if budget <= 0:
        return allocations

    total = sum(
        a.get("est_capital_cost", 0) for a in allocations
        if a.get("action") == "deploy"
    )

    # Within ±1% — invariant satisfied.
    if abs(total - budget) <= budget * 0.01:
        return allocations

    if total > budget * 1.01:
        # OVER-BUDGET → scale down proportionally. min_size acts as a hard
        # floor; if every market hits the floor, we can't shrink further.
        scale = budget / total
        for a in allocations:
            if a.get("action") != "deploy":
                continue
            spread = a.get("max_spread", 0.045)
            s = spread if spread > 0 else 0.045
            cpb = 2 * max(0.05, (1.0 - 2 * s) / 2)
            if cpb <= 0:
                continue
            new_shares = max(
                int(a.get("min_size", 50)),
                int(a.get("shares_per_side", 0) * scale),
            )
            a["shares_per_side"] = new_shares
            a["est_capital_cost"] = round(new_shares * cpb, 2)
        log.warning(
            f"[PROFIT] Capital conservation violated (over): "
            f"${total:.0f} > ${budget:.0f} — scaled by {scale:.2f}"
        )
        return allocations

    # UNDER-BUDGET by >1% → try to grow allocations proportionally,
    # respecting per_market_cap if supplied. If nothing can absorb more
    # (everyone at cap, or no eligible deploys), accept the under-allocation.
    deploy = [
        a for a in allocations
        if a.get("action") == "deploy" and a.get("est_capital_cost", 0) > 0
    ]
    if not deploy:
        return allocations
    surplus = budget - total
    initial_total = sum(a.get("est_capital_cost", 0) for a in deploy)
    if initial_total <= 0:
        return allocations

    grown = False
    for a in deploy:
        cur_cost = a.get("est_capital_cost", 0)
        share = cur_cost / initial_total
        add = surplus * share
        if per_market_cap is not None:
            headroom = max(0.0, per_market_cap - cur_cost)
            add = min(add, headroom)
        if add <= 0:
            continue
        spread = a.get("max_spread", 0.045)
        s = spread if spread > 0 else 0.045
        cpb = 2 * max(0.05, (1.0 - 2 * s) / 2)
        if cpb <= 0:
            continue
        extra_shares = int(add / cpb)
        if extra_shares <= 0:
            continue
        a["shares_per_side"] += extra_shares
        a["est_capital_cost"] = round(a["shares_per_side"] * cpb, 2)
        grown = True

    if grown:
        log.info(
            f"[PROFIT] Capital conservation under-budget — "
            f"${total:.0f} of ${budget:.0f}, redistributed surplus "
            f"${surplus:.0f}"
        )
    else:
        log.debug(
            f"[PROFIT] Capital conservation under-budget — "
            f"${total:.0f} of ${budget:.0f}, no headroom to grow"
        )
    return allocations


def allocate_portfolio(
    scored_markets: list[ScoredMarket],
    total_capital: float,
    calibrator,  # CalibrationManager
    db_path: str = "bot_history.db",
    max_per_market: float = 200.0,
    max_capital_pct: float = 0.15,
    max_group_pct: float = 0.30,
    max_cluster_pct: float = DEFAULT_MAX_CLUSTER_PCT,
    learning_state=None,  # profit.learning.LearningState | None
) -> list[dict]:
    """Allocate capital across markets using risk-adjusted EV.

    learning_state: optional real-time behavior correction. When None or
    at neutral defaults (all 1.0), behavior is identical to the prior
    version. When provided, applies four scalar adjustments:
      - capital_scale    scales deployable capital
      - risk_multiplier  inflates the loss term in RAS denominator
      - aggressiveness   scales final_score (RAS * bandit * aggressiveness)
      - reward_trust     handled upstream in CalibrationManager.reward_trust
    """
    global _cycles_without_clustering

    if not scored_markets:
        return []

    # Neutral defaults — OFF/SHADOW/unset caller = no learning influence.
    ls_capital_scale = 1.0
    ls_risk_multiplier = 1.0
    ls_aggressiveness = 1.0
    ls_reward_trust = 1.0
    ls_mode = "OFF"
    ls_market_eff_map: dict = {}
    ls_reward_efficiency = None
    ls_reward_efficiency_baseline = None
    if learning_state is not None:
        ls_capital_scale = float(getattr(learning_state, "capital_scale", 1.0))
        ls_risk_multiplier = float(getattr(learning_state, "risk_multiplier", 1.0))
        ls_aggressiveness = float(getattr(learning_state, "aggressiveness", 1.0))
        ls_reward_trust = float(getattr(learning_state, "reward_trust", 1.0))
        ls_mode = str(getattr(learning_state, "mode", "OFF") or "OFF")
        ls_market_eff_map = (
            getattr(learning_state, "market_efficiency_map", None) or {}
        )
        ls_reward_efficiency = getattr(
            learning_state, "reward_efficiency", None,
        )
        ls_reward_efficiency_baseline = getattr(
            learning_state, "reward_efficiency_baseline", None,
        )

    # ── Efficiency-based capital scaling ───────────────────────
    eff_scale = _compute_efficiency_scale(db_path)
    # STEP 4.1: learning capital_scale composes with eff_scale. Both are
    # multiplicative discounts on deployable capital, applied here (once).
    deployable_capital_total = total_capital * eff_scale * ls_capital_scale

    # ── PATCH 7 PART 1: overcommit factor ─────────────────────
    # Regime is detected up front so the overcommit factor (which
    # multiplies regime_multiplier) is known before Phase A/B. Phase E
    # retains the regime detection for observability but no longer
    # shrinks allocations — the regime signal is already priced in here.
    #
    # Backward-compat gate: overcommit applies ONLY when the learning
    # loop is in ACTIVE mode (same pattern Patch 6 used for Parts 2/4).
    # Default LearningState() has mode=OFF so it matches the
    # learning_state=None path exactly — legacy callers and OFF/SHADOW
    # callers both see the pre-Patch-7 notional-budget semantics.
    try:
        _regime_early = detect_regime(db_path)
    except Exception as e:
        log.warning(
            f"[PROFIT_P7] regime detect failed (defaulting normal): {e}"
        )
        _regime_early = "normal"
    regime_multiplier_early = (
        HOSTILE_CAPITAL_SCALE if _regime_early == "hostile" else 1.0
    )
    if learning_state is not None and ls_mode == "ACTIVE":
        overcommit_factor = _compute_overcommit_factor(
            learning_state, regime_multiplier_early,
        )
    else:
        overcommit_factor = 1.0
    # Expand deployable_capital_total by the overcommit factor. All
    # downstream per-market caps, group caps, Phase B budgeting, and the
    # micro-exploration budget inherit this expanded ceiling.
    deployable_capital_total = deployable_capital_total * overcommit_factor

    # PART 4 — dynamic exploration budget. Scales from 5% (neutral /
    # missing data) up to 15% as current efficiency falls below the
    # adaptive baseline. Only spent in ACTIVE mode; capped at 15%.
    exploration_pct = _compute_exploration_pct(
        ls_reward_efficiency, ls_reward_efficiency_baseline,
    )
    if ls_mode == "ACTIVE":
        exploration_budget = deployable_capital_total * exploration_pct
        deployable_capital = deployable_capital_total - exploration_budget
    else:
        exploration_budget = 0.0
        deployable_capital = deployable_capital_total

    # FIX 4 — per-market efficiency quintile thresholds. Computed ONCE
    # per cycle from the learning map. Returns (p20, p80) or (None, None)
    # when too few markets are available (rule skipped).
    _eff_p20, _eff_p80 = _efficiency_quintiles(ls_market_eff_map)

    # FIX 4: If clustering has been failing for many cycles, reduce capital
    # — we can't safely reason about correlated risk without cluster info.
    if _cycles_without_clustering >= CLUSTER_FAILURE_WARN_CYCLES:
        deployable_capital *= CLUSTER_FAILURE_CONFIDENCE_PENALTY
        log.warning(
            f"[PROFIT] Clustering unavailable for {_cycles_without_clustering} prior cycles — "
            f"deployable capital reduced by {(1 - CLUSTER_FAILURE_CONFIDENCE_PENALTY):.0%}"
        )

    # ── STEP 5: Thompson-sampling bandit multiplier ────────────
    # Sample once per cycle; the draw is the per-market exploration bonus.
    # Missing markets fall back to the neutral 1.0 multiplier.
    try:
        bandit_scores = Bandit(db_path).sample()
    except Exception as e:
        log.warning(f"[PROFIT] bandit sample failed (using neutral): {e}")
        bandit_scores = {}

    # ── Phase A: Compute risk-adjusted scores ──────────────────
    # FIX 2: Selection pressure is expressed via deployable_capital alone.
    # We no longer multiply RAS by eff_scale — that was double-counting.
    # STEP 5: bandit multiplier IS applied here — it shapes exploration
    # and is independent of the efficiency/capital signal.

    market_data: list[dict] = []
    for sm in scored_markets:
        if sm.action != "deploy" or sm.score <= 0:
            market_data.append({
                "sm": sm, "ras": 0.0, "bandit": 1.0, "final_score": 0.0,
                "predictions": None, "action": "avoid",
            })
            continue

        preds = calibrator.get_predictions(
            condition_id=sm.condition_id,
            daily_rate=sm.daily_rate,
            q_share_pct=sm.q_share_pct,
            on_book_hours=0,
            fill_count_recent=sm.fill_count,
            fill_cost_recent=sm.fill_damage,
            dump_revenue_recent=0,
            agent_shares=sm.min_size,
            correction_factor=1.0,
        )

        # FIX 1 + 14: pass p_fill and e_loss_given_fill directly.
        # STEP 4.2: learning risk_multiplier inflates the loss term in
        # the RAS denominator (discounts risky markets more aggressively).
        ras = _risk_adjusted_score(
            preds.ev_per_day, preds.p_fill_24h, preds.e_loss_given_fill,
            risk_multiplier=ls_risk_multiplier,
        )

        # STEP 5: combine with bandit draw. Unknown market → 1.0 (neutral).
        # STEP 4.3: learning aggressiveness scales final_score, shaping
        # capital concentration on top-ranked markets.
        bandit_mult = bandit_scores.get(sm.condition_id, 1.0)
        # FIX 4 — per-market efficiency ranking. Bottom quintile gets 0.8×,
        # top quintile gets 1.1×. Applied BEFORE the action decision so
        # rank-flip is possible for markets at the deploy threshold.
        # Returns 1.0 when the map is too small or the market has no data.
        eff_mult = _efficiency_multiplier(
            sm.condition_id, ls_market_eff_map, _eff_p20, _eff_p80,
        )
        # ── PATCH 10 PART 2.3: RELAX EV FILTERING IN ACTIVE ──────
        # Shift objective from "maximise EV per dollar" to "maximise
        # exposure subject to expected_capital". Markets with slightly
        # negative or low-positive EV are included under ACTIVE mode;
        # a synthetic small-positive RAS ensures they survive the
        # `final_score > 0` deploy gate. NEGATIVE_EV_TOLERANCE remains
        # the hard floor — markets worse than that stay avoided.
        low_ev_override = False
        effective_ras = ras
        if learning_state is not None and ls_mode == "ACTIVE":
            ev_pd = float(preds.ev_per_day)
            if ev_pd < NEGATIVE_EV_TOLERANCE:
                # Catastrophically negative → leave ras=0; market will avoid.
                pass
            elif ev_pd < PATCH10_MIN_EV_THRESHOLD * LOW_EV_ALLOWANCE_FACTOR:
                # Relaxed zone: keep in the deploy set with a floor ras
                # so the downstream filter doesn't drop it.
                low_ev_override = True
                effective_ras = max(ras, 0.01)

        # ── PATCH 6 PART 3: OBJECTIVE CORRECTION ──────────────────
        # Blend risk-adjusted EV (efficiency) with clamped raw EV so
        # low-capital "fake efficiency" can't dominate. Gated on
        # learning_state presence to preserve backward-compat when
        # callers pass no learning signal (Global Rule 5).
        if learning_state is not None:
            raw_ev_clamped = max(0.0, min(float(preds.raw_ev_per_day), 1.0))
            blended_score = (
                PATCH6_EFFICIENCY_WEIGHT * effective_ras
                + PATCH6_RAW_EV_WEIGHT * raw_ev_clamped
            )
            final_score = blended_score * bandit_mult * ls_aggressiveness * eff_mult
        else:
            final_score = effective_ras * bandit_mult * ls_aggressiveness * eff_mult

        # ── PATCH 10 PART 2.2: EXPOSURE BOOST ────────────────────
        # Favour breadth over strict score in ACTIVE. This is a
        # score-shaping multiplier; selection logic unchanged.
        if learning_state is not None and ls_mode == "ACTIVE":
            final_score *= (1.0 + EXPOSURE_PRIORITY_WEIGHT)

        action = "deploy" if final_score > MIN_RAS_TO_DEPLOY else "avoid"
        if low_ev_override:
            # Force deploy even if final_score didn't clear (unusual edge
            # given the synthetic ras floor above, but belt-and-braces).
            action = "deploy"
        market_data.append({
            "sm": sm, "ras": effective_ras, "bandit": bandit_mult,
            "final_score": final_score, "predictions": preds,
            "action": action, "_efficiency_mult": eff_mult,
            "_low_ev_override": low_ev_override,
        })

    # ── PART 11: Hard profit guard ─────────────────────────────
    # If the SUM of expected per-market EVs (across to-deploy markets) is
    # negative, the portfolio is unprofitable in expectation — return
    # ALL avoid (no deployment). This is stricter than the per-market RAS
    # gate: even if some markets have positive RAS, a negative aggregate
    # EV means deployment loses money on average.
    total_expected_ev = sum(
        md["predictions"].ev_per_day
        for md in market_data
        if md["predictions"] is not None and md["action"] == "deploy"
    )
    if total_expected_ev < 0:
        # ── PATCH 10 PART 2.6: hard-profit-guard override ────────
        # In ACTIVE mode we log and permit deployment — the system's
        # objective has shifted to reward exposure, bounded by the
        # expected_capital invariant enforced in Phase F. OFF/SHADOW/None
        # keep the legacy fail-safe behaviour.
        if learning_state is not None and ls_mode == "ACTIVE":
            log.info(
                f"[PATCH10] Total expected EV ${total_expected_ev:.2f} < 0 "
                f"— allowing exposure (ACTIVE mode)"
            )
        else:
            log.warning(
                f"[PROFIT] Total expected EV ${total_expected_ev:.2f} < 0 — "
                f"NO DEPLOYMENT (hard profit guard)"
            )
            for md in market_data:
                md["action"] = "avoid"
                md["allocated_capital"] = 0
                md["final_score"] = 0.0

    # ── Phase B: Capital budgeting ─────────────────────────────

    rebalance_credit = 0.0
    for md in market_data:
        sm = md["sm"]
        if md["action"] == "avoid" and sm.locked_position_usd > 1.0:
            rebalance_credit += sm.locked_position_usd * REBALANCE_CREDIT_PCT

    effective_capital = deployable_capital + rebalance_credit
    per_market_cap = min(max_per_market, effective_capital * max_capital_pct)
    # ── PATCH 9 PART 3.5: softer per-market cap in ACTIVE ────
    # Allows each market to temporarily hold up to 1.5× its normal cap
    # so that — combined with the per_market_scale halving below — more
    # markets can be funded from the same total notional budget.
    if learning_state is not None and ls_mode == "ACTIVE":
        effective_per_market_cap = per_market_cap * 1.5
    else:
        effective_per_market_cap = per_market_cap
    # ── PATCH 9 PART 3.4: per-market size reduction in ACTIVE ─
    # Halves each market's allocation so breadth (not depth) grows.
    # OFF/SHADOW/None get the original multiplier 1.0 (no change).
    if learning_state is not None and ls_mode == "ACTIVE":
        per_market_scale = MIN_SIZE_REDUCTION_FACTOR
    else:
        per_market_scale = 1.0
    per_group_cap = effective_capital * max_group_pct

    # Dynamic market count
    try:
        eff = get_efficiency(db_path)
        deploy_count = sum(1 for md in market_data if md["action"] == "deploy")
        target_count = get_target_market_count(
            eff, deploy_count, min_markets=5, max_markets=60,
        )
    except Exception:
        target_count = 60

    # ── PATCH 9: expand target_count in ACTIVE ────────────────
    # Clamped into [MIN_MARKETS_ACTIVE_FLOOR, MAX_MARKETS_CAP] so breadth
    # grows without unbounded explosion.
    if learning_state is not None and ls_mode == "ACTIVE":
        expanded_target = int(target_count * MARKET_EXPANSION_FACTOR)
        target_count = max(
            MIN_MARKETS_ACTIVE_FLOOR,
            min(MAX_MARKETS_CAP, expanded_target),
        )

    # STEP 5: sort + budget by `final_score` (RAS × bandit). Gating on
    # `ras > 0` keeps FIX 13's no-positive-EV guard intact even if the
    # bandit draw is high — a negative-EV market still gets zero capital.
    deploy_markets = [
        md for md in market_data
        if md["action"] == "deploy" and md["ras"] > 0 and md["final_score"] > 0
    ]
    deploy_markets.sort(key=lambda md: md["final_score"], reverse=True)

    if len(deploy_markets) > target_count:
        for md in deploy_markets[target_count:]:
            md["action"] = "avoid"
        deploy_markets = deploy_markets[:target_count]

    total_score = sum(md["final_score"] for md in deploy_markets)

    # FIX 13: If no positive risk-adjusted EV, don't speculatively deploy
    # to min-cost positions. Mark all deployments as avoid and skip budgeting.
    if total_score <= 0:
        for md in deploy_markets:
            md["action"] = "avoid"
            md["allocated_capital"] = 0
        deploy_markets = []

    group_capital: dict[str, float] = {}

    for md in deploy_markets:
        sm = md["sm"]
        share = md["final_score"] / total_score
        raw_alloc = effective_capital * share

        # PATCH 9 — use softer per-market cap (1.5× in ACTIVE).
        alloc = min(raw_alloc, effective_per_market_cap)

        group = getattr(sm, "question_group", "") or ""
        if group:
            used = group_capital.get(group, 0.0)
            headroom = per_group_cap - used
            if headroom <= 0:
                md["action"] = "avoid"
                md["allocated_capital"] = 0
                continue
            alloc = min(alloc, headroom)
            group_capital[group] = used + alloc
        else:
            group_capital[group] = group_capital.get(group, 0) + alloc

        min_cost = _est_market_cost(int(sm.min_size), sm.max_spread)
        # FIX 10: effective_per_market_cap is a HARD ceiling, enforced
        # AFTER the min_cost floor. If we can't afford the exchange
        # minimum within the cap, drop the market — min_size floors
        # inside compute_shares would otherwise leak over-budget positions
        # past capital guards.
        if effective_per_market_cap < min_cost:
            md["action"] = "avoid"
            md["allocated_capital"] = 0
            continue
        alloc = min(effective_per_market_cap, max(alloc, min_cost))
        # ── PATCH 9 PART 3.4: per-market size reduction in ACTIVE ──
        # Scale each allocation DOWN to spread capital across more markets.
        # min_cost remains the floor — we never go below the exchange min.
        alloc = max(min_cost, alloc * per_market_scale)
        md["allocated_capital"] = alloc

    # ── Phase C: Depth-aware sizing ────────────────────────────

    allocations: list[dict] = []
    for md in market_data:
        sm = md["sm"]

        if md["action"] != "deploy":
            allocations.append(_to_dict(sm, 0, action_override="avoid",
                                        reason_override=sm.reason or "Below risk threshold"))
            continue

        alloc_cap = md.get("allocated_capital", 0)
        if alloc_cap <= 0:
            allocations.append(_to_dict(sm, 0, action_override="avoid",
                                        reason_override="No capital allocated"))
            continue

        book = calibrator._book_cache.get(sm.condition_id, {})
        depth_ahead = book.get("bid_depth_ahead", 0)

        shares, est_cost = compute_shares(
            allocated_capital=alloc_cap,
            spread=sm.max_spread,
            min_size=sm.min_size,
            depth_ahead=depth_ahead,
            max_per_market=max_per_market,
        )

        d = _to_dict(sm, shares)
        d["est_capital_cost"] = est_cost

        preds = md.get("predictions")
        if preds:
            d["_ev_per_day"] = round(preds.ev_per_day, 4)
            d["_raw_ev_per_day"] = round(preds.raw_ev_per_day, 4)
            # PART 10: spec-named observability aliases
            d["_ev_after_confidence"] = round(preds.ev_per_day, 4)
            # PATCH 7 PART 2.3 — retain a non-zero floor on the stamped
            # p_fill. The underlying prediction may legitimately be very
            # small (cold-start / fallback_fill) but must never round to
            # exactly zero, because expected_capital depends on it and a
            # zero p_fill would silently make V3 metrics undefined.
            d["_p_fill"] = max(1e-4, round(preds.p_fill_24h, 6))
            d["_ras"] = round(md["ras"], 4)
            d["_risk_adjusted_score"] = round(md["ras"], 4)
            d["_confidence"] = preds.confidence
            d["_model_confidence"] = round(preds.model_confidence, 4)
            # PATCH 7 PART 4 — expected-capital observability.
            d["_expected_capital"] = round(
                d["_p_fill"] * float(d.get("est_capital_cost") or 0.0), 4,
            )
            d["_overcommit_factor"] = round(overcommit_factor, 4)

        # STEP 5: record bandit multiplier and final score for observability
        d["_bandit"] = round(md.get("bandit", 1.0), 4)
        d["_bandit_multiplier"] = round(md.get("bandit", 1.0), 4)
        d["_final_score"] = round(md.get("final_score", 0.0), 4)
        # PATCH 10: carry the Phase-A low-EV flag onto the allocation.
        d["_low_ev_override"] = bool(md.get("_low_ev_override", False))
        # PART 10: regime multiplier is set after Phase E; default 1.0 here
        d["_regime_multiplier"] = 1.0
        # Learning loop observability (STEP 8). All four scalars are
        # always stamped — default 1.0 when no learning_state was passed.
        d["_learning_aggressiveness"] = round(ls_aggressiveness, 4)
        d["_learning_capital_scale"] = round(ls_capital_scale, 4)
        d["_learning_risk_multiplier"] = round(ls_risk_multiplier, 4)
        d["_learning_reward_trust"] = round(ls_reward_trust, 4)
        d["_learning_mode"] = ls_mode
        # FIX 4 — per-market efficiency quintile multiplier
        d["_efficiency_mult"] = round(md.get("_efficiency_mult", 1.0), 4)
        # PART 4 — effective exploration percentage (same for every market
        # this cycle; stamped per-row for observability).
        d["_exploration_pct"] = round(exploration_pct, 4)

        allocations.append(d)

    # ── Phase B2: Correlation cluster caps ─────────────────────

    clusters: dict[str, int] = {}
    oversized_ids: set[int] = set()
    try:
        # FIX 7, 9: build_fill_clusters returns (clusters, oversized_ids)
        clusters, oversized_ids = build_fill_clusters(db_path)
        if clusters:
            allocations = apply_cluster_caps(
                allocations, clusters, max_cluster_pct, effective_capital,
                oversized_cluster_ids=oversized_ids,
            )
            # Fix 3: Redistribute freed capital
            allocations = _redistribute_cluster_savings(
                allocations, clusters, effective_capital, max_cluster_pct,
            )
            exposure = compute_cluster_exposure(allocations, clusters)
            if exposure:
                largest = max(exposure.values())
                largest_pct = (
                    largest / effective_capital if effective_capital > 0 else 0
                )
                log.info(
                    f"[PROFIT] Clusters detected: {len(set(clusters.values()))} | "
                    f"Largest cluster: {largest_pct:.0%} capital | "
                    f"oversized: {len(oversized_ids)}"
                )
        _cycles_without_clustering = 0
    except Exception as e:
        log.warning(f"Cluster cap failed (skipping): {e}")
        # FIX 3: single increment on failure (previous code double-counted)
        _cycles_without_clustering += 1

    # ── Phase D: Rebalance churn control ───────────────────────

    allocations = compute_deltas(allocations, db_path)

    # FIX 8: Re-apply per-market + cluster caps after rebalance. Delta
    # computation can bump shares above caps when pre-existing positions
    # exceed the new target.
    allocations = _apply_per_market_cap(allocations, per_market_cap)
    if clusters:
        allocations = apply_cluster_caps(
            allocations, clusters, max_cluster_pct, effective_capital,
            oversized_cluster_ids=oversized_ids,
        )

    # ── Phase E: Regime observability (PATCH 7 makes this a no-op) ────
    # Regime detection moved to the top of allocate_portfolio so the
    # overcommit factor could reflect it. We keep the regime signal in
    # the allocation observability fields, but the shares-shrinking
    # loop is REMOVED — the regime_multiplier is already baked into
    # overcommit_factor so applying it again here would double-shrink.
    regime = _regime_early
    regime_multiplier = regime_multiplier_early

    if regime_multiplier < 1.0:
        log.warning(
            f"[PROFIT_P7] HOSTILE regime — overcommit_factor already "
            f"reduced via regime_multiplier={regime_multiplier:.2f}"
        )

    # PART 10: stamp the regime multiplier on every allocation for observability
    for a in allocations:
        a["_regime_multiplier"] = round(regime_multiplier, 4)

    # ── PATCH 6 PART 2: DEPLOYMENT TARGETING ───────────────────
    # When the allocator's output under-deploys vs deployable_capital_total
    # AND learning is ACTIVE, nudge every deploy up by DEPLOYMENT_BOOST.
    # Capital conservation (Phase F) runs immediately after and will rein
    # any over-budget result back into [budget ± 1%], so this block can
    # only push UP within caps. Invariants (cap × 1.05, per-cluster 30%,
    # exploration 15%, EV ≥ 0) are preserved by the following phases.
    total_allocated_pre_boost = sum(
        a.get("est_capital_cost", 0) for a in allocations
        if a.get("action") == "deploy"
    )
    deploy_ratio = (
        total_allocated_pre_boost / deployable_capital_total
        if deployable_capital_total > 0 else 0.0
    )
    if (
        learning_state is not None
        and ls_mode == "ACTIVE"
        and deploy_ratio < MIN_DEPLOY_RATIO
    ):
        scale_up = DEPLOYMENT_BOOST
        for a in allocations:
            if a.get("action") != "deploy":
                continue
            spread = a.get("max_spread", 0.045)
            s = spread if spread > 0 else 0.045
            cpb = 2 * max(0.05, (1.0 - 2 * s) / 2)
            if cpb <= 0:
                continue
            # Boost shares and est_capital_cost together so downstream
            # consumers stay consistent. min_size floor honored.
            new_shares = max(
                int(a.get("min_size", 50) or 50),
                int(a.get("shares_per_side", 0) * scale_up),
            )
            a["shares_per_side"] = new_shares
            a["est_capital_cost"] = round(new_shares * cpb, 2)
        log.info(
            f"[PROFIT_P6] deploy_ratio={deploy_ratio:.3f} < "
            f"{MIN_DEPLOY_RATIO:.2f} → boosted deploys by {scale_up:.2f}×"
        )

    # PATCH 6 PART 5 observability — stamp deploy_ratio on every row
    # PATCH 9 PART 3.7 observability — expansion-mode fields
    # PATCH 10 PART 2.7 observability — forced-exposure + low-EV fields
    for a in allocations:
        a["_deploy_ratio"] = round(deploy_ratio, 4)
        a["_target_deploy"] = TARGET_DEPLOY_RATIO
        a["_expansion_mode"] = (
            ls_mode if learning_state is not None else "NONE"
        )
        a["_target_market_count"] = int(target_count)
        a["_per_market_scale"] = round(per_market_scale, 4)
        # Patch 10 fields default to False; forced / override markers
        # are set by the Phase A and Patch 10 deployment-floor blocks.
        a["_forced_exposure"] = bool(a.get("_forced", False))
        a["_low_ev_override"] = bool(a.get("_low_ev_override", False))

    # ── Phase F: Capital ceiling enforcement ───────────────────
    # In ACTIVE mode, PATCH 7 uses expected-capital semantics
    # (Σ p_fill × size ≤ capital × buffer) so notional overcommitment
    # is permitted while probabilistic drawdown stays bounded. In
    # OFF/SHADOW (or learning_state=None), fall back to the pre-Patch-7
    # notional-budget conservation to preserve behavioural backward
    # compat with older tests + integrations.
    if learning_state is not None and ls_mode == "ACTIVE":
        allocations = _enforce_expected_capital(allocations, total_capital)
    else:
        # Legacy path: regime_multiplier=1.0 here because early-detected
        # regime was not applied to budget when overcommit was skipped.
        budget_after_regime_legacy = effective_capital * regime_multiplier
        allocations = _enforce_capital_conservation(
            allocations, budget_after_regime_legacy,
            per_market_cap=per_market_cap,
        )

    # ── Phase G: FIX 7 — Micro-exploration (ACTIVE mode only) ─────────
    # Spend the reserved 5% on markets that were positive-RAS but didn't
    # make the top-ranked deploy set. Equal small positions, bounded by
    # min_size floor and the exploration budget. Runs AFTER conservation
    # so the reserved slice is additive, not subject to scale-down.
    if exploration_budget > 0 and ls_mode == "ACTIVE":
        allocations = _apply_micro_exploration(
            allocations, market_data, exploration_budget, max_per_market,
        )

    # ── PATCH 6 PART 4: MIN CAPITAL UTILISATION GUARD ─────────
    # Floor on deploy count in ACTIVE mode. Promotes highest-RAS "avoid"
    # allocations to "deploy" at min_size so "0 or near-0 deployment" can
    # never silently persist. EV invariant preserved because candidates
    # are drawn from `market_data` rows with ras > 0 (RAS > 0 implies
    # EV > 0 upstream). Candidate source is market_data rather than the
    # allocation dicts because `_ras` is only stamped on deploy rows.
    if learning_state is not None and ls_mode == "ACTIVE":
        deploy_count_pre_guard = sum(
            1 for a in allocations if a.get("action") == "deploy"
        )
        if deploy_count_pre_guard < PATCH6_MIN_MARKETS:
            alloc_by_cid = {
                a["condition_id"]: a for a in allocations
                if a.get("condition_id")
            }
            candidates: list[tuple] = []
            for md in market_data:
                sm = md.get("sm")
                if sm is None:
                    continue
                a = alloc_by_cid.get(sm.condition_id)
                if a is None or a.get("action") != "avoid":
                    continue
                if float(md.get("ras") or 0.0) <= 0:
                    continue
                candidates.append((a, md))
            candidates.sort(
                key=lambda t: float(t[1].get("ras") or 0.0), reverse=True,
            )
            need = PATCH6_MIN_MARKETS - deploy_count_pre_guard
            promoted = 0
            for a, md in candidates[:need]:
                sm = md["sm"]
                spread = getattr(sm, "max_spread", 0.045)
                sp = spread if spread > 0 else 0.045
                cpb = 2 * max(0.05, (1.0 - 2 * sp) / 2)
                if cpb <= 0:
                    continue
                shares = int(sm.min_size or 50)
                a["action"] = "deploy"
                a["shares_per_side"] = shares
                a["est_capital_cost"] = round(shares * cpb, 2)
                a["reason"] = "PATCH6_MIN_MARKETS"
                promoted += 1
            if promoted:
                log.info(
                    f"[PROFIT_P6] min-markets guard promoted {promoted} "
                    f"avoid→deploy (pre={deploy_count_pre_guard}, "
                    f"target≥{PATCH6_MIN_MARKETS})"
                )

    # ── PATCH 9 PART 3.6: MIN_MARKETS_ACTIVE_FLOOR guard ─────
    # Stricter floor than Patch 6's PATCH6_MIN_MARKETS=5. Runs only in
    # ACTIVE mode and promotes the highest-RAS avoids (drawn from
    # market_data so we have access to the real ras value) to deploy
    # at min_size cost until the floor is met. EV invariant preserved —
    # candidates are filtered on `ras > 0`, which upstream implies
    # positive EV. Promoted rows are tagged `_expansion=True` for
    # downstream observability.
    if learning_state is not None and ls_mode == "ACTIVE":
        deploy_count_post_p6 = sum(
            1 for a in allocations if a.get("action") == "deploy"
        )
        if deploy_count_post_p6 < MIN_MARKETS_ACTIVE_FLOOR:
            alloc_by_cid = {
                a["condition_id"]: a for a in allocations
                if a.get("condition_id")
            }
            candidates: list[tuple] = []
            for md in market_data:
                sm = md.get("sm")
                if sm is None:
                    continue
                a = alloc_by_cid.get(sm.condition_id)
                if a is None or a.get("action") != "avoid":
                    continue
                if float(md.get("ras") or 0.0) <= 0:
                    continue
                candidates.append((a, md))
            candidates.sort(
                key=lambda t: float(t[1].get("ras") or 0.0), reverse=True,
            )
            need = MIN_MARKETS_ACTIVE_FLOOR - deploy_count_post_p6
            promoted = 0
            for a, md in candidates[:need]:
                sm = md["sm"]
                spread = getattr(sm, "max_spread", 0.045)
                sp = spread if spread > 0 else 0.045
                cpb = 2 * max(0.05, (1.0 - 2 * sp) / 2)
                if cpb <= 0:
                    continue
                shares = int(sm.min_size or 50)
                a["action"] = "deploy"
                a["shares_per_side"] = shares
                a["est_capital_cost"] = round(shares * cpb, 2)
                a["reason"] = "PATCH9_EXPANSION_FLOOR"
                a["_expansion"] = True
                promoted += 1
            if promoted:
                log.info(
                    f"[PROFIT_P9] expansion-floor promoted {promoted} "
                    f"avoid→deploy (pre={deploy_count_post_p6}, "
                    f"floor={MIN_MARKETS_ACTIVE_FLOOR})"
                )

    # ── PATCH 10 PART 2.4: DEPLOYMENT FLOOR (FORCED EXPOSURE) ─────
    # If the allocator's output leaves deploy_ratio < MIN_DEPLOY_RATIO_ACTIVE,
    # top up with additional markets drawn from the avoid set (highest
    # RAS first), each sized at min_size cost, until we reach the
    # FORCE_DEPLOY_RATIO_TARGET. Markets with ev_per_day below
    # NEGATIVE_EV_TOLERANCE are excluded — the Patch 10 relaxed EV gate
    # already handled that floor upstream.
    # NOTE: this deliberately over-deploys NOTIONAL; the expected_capital
    # invariant is re-enforced immediately after (PART 2.5) so
    # Σ p_fill × size ≤ total_capital × EXPECTED_CAPITAL_BUFFER.
    if learning_state is not None and ls_mode == "ACTIVE":
        deployed_notional = sum(
            float(a.get("est_capital_cost") or 0.0)
            for a in allocations if a.get("action") == "deploy"
        )
        p10_deploy_ratio = (
            deployed_notional / max(float(total_capital), 1e-6)
        )
        if p10_deploy_ratio < MIN_DEPLOY_RATIO_ACTIVE:
            deficit = (
                FORCE_DEPLOY_RATIO_TARGET * float(total_capital)
                - deployed_notional
            )
            alloc_by_cid_p10 = {
                a["condition_id"]: a for a in allocations
                if a.get("condition_id")
            }
            candidates_p10: list = []
            for md in market_data:
                sm = md.get("sm")
                if sm is None:
                    continue
                a = alloc_by_cid_p10.get(sm.condition_id)
                if a is None or a.get("action") != "avoid":
                    continue
                # Spec: sort by descending RAS, no EV pre-filter. Markets
                # with ras=0 (ev≤0) are still candidates — they just end
                # up at the bottom of the priority list. The expected-
                # capital re-enforcement (PART 2.5) is the real safety net.
                candidates_p10.append(
                    (a, md, float(md.get("ras") or 0.0))
                )
            candidates_p10.sort(key=lambda t: t[2], reverse=True)
            forced = 0
            for a, md, _ras in candidates_p10:
                if deficit <= 0:
                    break
                sm = md["sm"]
                spread = getattr(sm, "max_spread", 0.045)
                sp = spread if spread > 0 else 0.045
                cpb = 2 * max(0.05, (1.0 - 2 * sp) / 2)
                if cpb <= 0:
                    continue
                shares = int(sm.min_size or 50)
                cost = round(shares * cpb, 2)
                a["action"] = "deploy"
                a["shares_per_side"] = shares
                a["est_capital_cost"] = cost
                a["reason"] = "FORCED_EXPOSURE"
                a["_forced"] = True
                # Stamp _p_fill + _expected_capital so
                # _enforce_expected_capital counts this row properly.
                preds_m = md.get("predictions")
                if preds_m is not None:
                    p_fill_stamped = max(
                        1e-4, round(float(preds_m.p_fill_24h), 6),
                    )
                    a["_p_fill"] = p_fill_stamped
                    a["_expected_capital"] = round(
                        p_fill_stamped * cost, 4,
                    )
                deficit -= cost
                forced += 1
            if forced:
                log.info(
                    f"[PATCH10] forced exposure: promoted {forced} "
                    f"avoid→deploy (pre_ratio={p10_deploy_ratio:.2%}, "
                    f"target≥{MIN_DEPLOY_RATIO_ACTIVE:.0%})"
                )

        # ── PATCH 10 PART 2.5: expected-capital safety re-enforcement ──
        # The forced promotions above push notional up; re-enforce the
        # Σ p_fill × size ≤ total_capital × buffer invariant. Missing
        # `_p_fill` on newly-promoted rows is handled by
        # _enforce_expected_capital's default 0.0 — forced rows with
        # unknown p_fill contribute 0 to expected_capital.
        allocations = _enforce_expected_capital(allocations, total_capital)

    # ── PATCH 11: EXPOSURE SATURATION ─────────────────────────────
    # Upsize EXISTING deploys toward the Patch 7 overcommit target.
    # Patch 10 promotes avoids; when there are no avoids (all markets
    # marked _low_ev_override), Patch 11 is what closes the gap.
    #
    # Target is the LIVE Patch-7 overcommit_factor × total_capital —
    # not a hardcoded ratio — so saturation tracks the learning-loop-
    # and regime-aware commit level.
    #
    # Cumulative scaling: each iter multiplies by UPSCALE_STEP (1.25),
    # clamped at EXPOSURE_SATURATION_MAX_SCALE (3.0). Loop exits early
    # when the target is reached.
    #
    # Safety sequence after the loop:
    #   1. Re-apply cluster caps (Hard Guarantee #2).
    #   2. Re-enforce expected_capital ≤ 0.95 × total (Hard Guarantee #1).
    #
    # ACTIVE-only: OFF/SHADOW/None keep pre-Patch-11 behaviour (HG #4).
    if learning_state is not None and ls_mode == "ACTIVE":
        target_notional = float(total_capital) * overcommit_factor
        current_notional = sum(
            float(a.get("est_capital_cost") or 0.0)
            for a in allocations if a.get("action") == "deploy"
        )
        cumulative_scale = 1.0
        if current_notional < target_notional and current_notional > 0:
            for _ in range(UPSCALE_MAX_ITERS):
                next_scale = min(
                    EXPOSURE_SATURATION_MAX_SCALE,
                    cumulative_scale * UPSCALE_STEP,
                )
                if next_scale <= cumulative_scale:
                    break  # hit MAX ceiling — further iters would no-op
                step_ratio = next_scale / cumulative_scale
                for a in allocations:
                    if a.get("action") != "deploy":
                        continue
                    spread = a.get("max_spread", 0.045)
                    s = spread if spread > 0 else 0.045
                    cpb = 2 * max(0.05, (1.0 - 2 * s) / 2)
                    if cpb <= 0:
                        continue
                    old_cost = float(a.get("est_capital_cost") or 0.0)
                    # Per-market cap: respect Patch-9's soft ceiling so
                    # an individual market never grows past
                    # effective_per_market_cap, even if that means
                    # saturation falls short of the overcommit target.
                    new_cost_target = min(
                        old_cost * step_ratio,
                        float(effective_per_market_cap),
                    )
                    min_shares = int(a.get("min_size", 50) or 50)
                    new_shares = max(
                        min_shares, int(new_cost_target / cpb),
                    )
                    a["shares_per_side"] = new_shares
                    a["est_capital_cost"] = round(new_shares * cpb, 2)
                cumulative_scale = next_scale
                current_notional = sum(
                    float(a.get("est_capital_cost") or 0.0)
                    for a in allocations if a.get("action") == "deploy"
                )
                if current_notional >= target_notional:
                    break

            # Re-apply fill-cluster caps (Hard Guarantee #2) — upsizing
            # may have pushed a clustered market past the 30% cap.
            if clusters:
                allocations = apply_cluster_caps(
                    allocations, clusters, max_cluster_pct,
                    effective_capital,
                    oversized_cluster_ids=oversized_ids,
                )

            # Re-enforce question_group cap (max_group_pct) — distinct
            # from fill-cluster caps above. The inline enforcement in
            # Phase B doesn't re-run after saturation, so a single group
            # can otherwise balloon past its 30% ceiling.
            group_totals: dict[str, float] = {}
            for a in allocations:
                if a.get("action") != "deploy":
                    continue
                g = a.get("question_group", "") or ""
                if not g:
                    continue
                group_totals[g] = group_totals.get(g, 0.0) + float(
                    a.get("est_capital_cost") or 0.0,
                )
            for g, total in group_totals.items():
                if total <= per_group_cap:
                    continue
                scale_down = per_group_cap / total
                for a in allocations:
                    if a.get("action") != "deploy":
                        continue
                    if (a.get("question_group") or "") != g:
                        continue
                    spread = a.get("max_spread", 0.045)
                    s = spread if spread > 0 else 0.045
                    cpb = 2 * max(0.05, (1.0 - 2 * s) / 2)
                    if cpb <= 0:
                        continue
                    old_cost = float(a.get("est_capital_cost") or 0.0)
                    new_cost_target = old_cost * scale_down
                    min_shares = int(a.get("min_size", 50) or 50)
                    new_shares = max(
                        min_shares, int(new_cost_target / cpb),
                    )
                    a["shares_per_side"] = new_shares
                    a["est_capital_cost"] = round(new_shares * cpb, 2)

            # Final safety net: re-enforce Σ p_fill × size ≤ 0.95 × total.
            allocations = _enforce_expected_capital(
                allocations, total_capital,
            )

            log.info(
                f"[PATCH11] saturation: target=${target_notional:.0f} "
                f"final=${sum(float(a.get('est_capital_cost') or 0.0) for a in allocations if a.get('action') == 'deploy'):.0f} "
                f"scale={cumulative_scale:.2f}× "
                f"(overcommit_factor={overcommit_factor:.2f})"
            )

        # Observability stamps — on every deploy row regardless of fire.
        for a in allocations:
            if a.get("action") == "deploy":
                a["_saturation_applied"] = cumulative_scale > 1.0
                a["_target_notional"] = round(target_notional, 2)
                a["_saturation_scale"] = round(cumulative_scale, 4)

    # Log summary
    n_deploy = sum(1 for a in allocations if a["action"] == "deploy")
    total_cost = sum(a.get("est_capital_cost", 0) for a in allocations
                     if a["action"] == "deploy")
    n_explor = sum(1 for a in allocations if a.get("_exploration"))
    log.info(
        f"Profit engine: {n_deploy} markets, ${total_cost:.0f} capital "
        f"(target={target_count}, deployable=${deployable_capital:.0f}, "
        f"eff_scale={eff_scale:.2f}, regime={regime}, "
        f"explor={n_explor}/${exploration_budget:.0f})"
    )

    return allocations


def _apply_micro_exploration(
    allocations: list[dict],
    market_data: list[dict],
    exploration_budget: float,
    max_per_market: float,
) -> list[dict]:
    """FIX 7 — allocate exploration_budget equally across markets that have
    positive RAS but were ranked out of the deploy set.

    Candidates: markets currently marked "avoid" in the allocations list
    that had ras > 0 in market_data (i.e., they were positive-EV but the
    rank cap or redistribution squeezed them out).

    Allocation: min_size cost per market, equal share of exploration_budget
    otherwise. Capped at max_per_market per market. Marks each with
    `_exploration=True` and `reason="EXPLORATION"` for observability.
    """
    if exploration_budget <= 0:
        return allocations

    # Map condition_id → market_data row for fast lookup
    md_by_cid = {md["sm"].condition_id: md for md in market_data}

    # Candidates: allocations that are currently "avoid" but had ras > 0
    # (they were positive-EV just not top-ranked).
    cid_deployed = {a["condition_id"] for a in allocations if a["action"] == "deploy"}
    candidates = []
    for a in allocations:
        if a["action"] != "avoid":
            continue
        cid = a.get("condition_id")
        if cid in cid_deployed:
            continue  # already deploying via normal path
        md = md_by_cid.get(cid)
        if md is None:
            continue
        if float(md.get("ras", 0.0)) <= 0:
            continue
        candidates.append((a, md))

    if not candidates:
        return allocations

    # Rank candidates by RAS so we spend the exploration budget on the
    # best of the rejected set first. This keeps exploration deterministic
    # (no randomness) while still being about markets we'd otherwise skip.
    candidates.sort(key=lambda t: t[1]["ras"], reverse=True)

    # How many fit with a floor of each market's min_size cost?
    deployed_here: list[tuple] = []
    remaining = exploration_budget
    for a, md in candidates:
        sm = md["sm"]
        min_cost = _est_market_cost(int(sm.min_size), getattr(sm, "max_spread", 0.045))
        if min_cost > remaining:
            break
        deployed_here.append((a, md, min_cost))
        remaining -= min_cost

    if not deployed_here:
        return allocations

    # Equal top-up: give every selected market its min_cost, then distribute
    # any leftover proportionally up to max_per_market.
    leftover = exploration_budget - sum(c for _, _, c in deployed_here)
    if leftover > 0 and len(deployed_here) > 0:
        per_bonus = leftover / len(deployed_here)
    else:
        per_bonus = 0.0

    for alloc_dict, md, base_cost in deployed_here:
        sm = md["sm"]
        spread = getattr(sm, "max_spread", 0.045)
        target_cap = min(max_per_market, base_cost + per_bonus)
        # Convert dollars back to shares (symmetric to _est_market_cost)
        est_price_per_side = max(0.10, (1.0 - 2 * spread) / 2)
        shares = int(target_cap / (est_price_per_side * 2))
        if shares < int(sm.min_size):
            shares = int(sm.min_size)
        final_cost = shares * est_price_per_side * 2
        alloc_dict["action"] = "deploy"
        alloc_dict["shares_per_side"] = shares
        alloc_dict["est_capital_cost"] = round(final_cost, 2)
        alloc_dict["_exploration"] = True
        alloc_dict["reason"] = "EXPLORATION"

    return allocations
