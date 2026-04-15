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
        final_score = ras * bandit_mult * ls_aggressiveness * eff_mult

        action = "deploy" if final_score > MIN_RAS_TO_DEPLOY else "avoid"
        market_data.append({
            "sm": sm, "ras": ras, "bandit": bandit_mult,
            "final_score": final_score, "predictions": preds,
            "action": action, "_efficiency_mult": eff_mult,
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

        alloc = min(raw_alloc, per_market_cap)

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
        # FIX 10: per_market_cap is a HARD ceiling, enforced AFTER the
        # min_cost floor. If we can't afford the exchange minimum within
        # the cap, drop the market — min_size floors inside compute_shares
        # would otherwise leak over-budget positions past capital guards.
        if per_market_cap < min_cost:
            md["action"] = "avoid"
            md["allocated_capital"] = 0
            continue
        alloc = min(per_market_cap, max(alloc, min_cost))
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
            d["_p_fill"] = round(preds.p_fill_24h, 4)
            d["_ras"] = round(md["ras"], 4)
            d["_risk_adjusted_score"] = round(md["ras"], 4)
            d["_confidence"] = preds.confidence
            d["_model_confidence"] = round(preds.model_confidence, 4)

        # STEP 5: record bandit multiplier and final score for observability
        d["_bandit"] = round(md.get("bandit", 1.0), 4)
        d["_bandit_multiplier"] = round(md.get("bandit", 1.0), 4)
        d["_final_score"] = round(md.get("final_score", 0.0), 4)
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

    # ── Phase E: Regime scaling (STEP 9 + STEP 11 + PART 8) ────
    # Detected AFTER redistribution so regime shrinks the final budget
    # rather than starving specific markets from the allocation stage.
    # PART 8: regime scaling ALWAYS multiplies allocations by the
    # regime_multiplier — it does not rely on conservation enforcement to
    # bind the budget. In normal regime the multiplier is 1.0 (no-op).
    # Invariant (STEP 12 #4): scaling removes <= 30% — HOSTILE_CAPITAL_SCALE
    # is 0.70 by construction, so the guard is tight.
    try:
        regime = detect_regime(db_path)
    except Exception as e:
        log.warning(f"[PROFIT] regime detect failed (defaulting normal): {e}")
        regime = "normal"

    regime_multiplier = (
        HOSTILE_CAPITAL_SCALE if regime == "hostile" else 1.0
    )
    budget_after_regime = effective_capital * regime_multiplier

    if regime_multiplier < 1.0:
        log.warning(
            f"[PROFIT] HOSTILE regime — deployable capital "
            f"${effective_capital:.0f} → ${budget_after_regime:.0f} "
            f"(×{regime_multiplier})"
        )
        # PART 8: scale every deploy allocation directly. This is the
        # "always apply" branch — regardless of whether totals already
        # fit the budget, allocations shrink by the regime multiplier.
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
                int(a.get("shares_per_side", 0) * regime_multiplier),
            )
            a["shares_per_side"] = new_shares
            a["est_capital_cost"] = round(new_shares * cpb, 2)

    # PART 10: stamp the regime multiplier on every allocation for observability
    for a in allocations:
        a["_regime_multiplier"] = round(regime_multiplier, 4)

    # ── Phase F: Capital conservation ──────────────────────────
    # PART 7: Total deployed capital must stay within ±1% of budget. The
    # conservation enforcer handles both over-budget (scale down) and
    # under-budget (proportional growth) within per_market_cap.
    allocations = _enforce_capital_conservation(
        allocations, budget_after_regime, per_market_cap=per_market_cap,
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
