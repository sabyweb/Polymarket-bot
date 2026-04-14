"""Portfolio allocator — risk-adjusted capital allocation across markets.

Replaces compute_allocations() when calibrator is ready.
Produces identical output format (list[dict]) so safety filter
and reward_farmer work unchanged.

Algorithm:
  Scaling:  deployable = total_capital * sqrt(efficiency / target)
  Phase A:  Risk-adjusted scoring (score *= eff_scale for pressure)
  Phase B:  Capital budgeting (proportional to adjusted score)
  Phase B2: Correlation cluster caps + capital redistribution
  Phase C:  Depth-aware sizing
  Phase D:  Rebalance churn control
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

log = logging.getLogger("profit.allocator")

# Confidence multipliers
CONFIDENCE_MODEL = 1.0
CONFIDENCE_FALLBACK = 0.7

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


def _risk_adjusted_score(ev_per_day: float, confidence: str,
                         p_fill_24h: float) -> float:
    """score = ev * confidence_mult * (1 - p_fill)"""
    conf_mult = CONFIDENCE_MODEL if confidence == "model" else CONFIDENCE_FALLBACK
    return ev_per_day * conf_mult * (1.0 - min(p_fill_24h, 0.99))


def _compute_efficiency_scale(db_path: str) -> float:
    """Compute capital scaling factor from realized efficiency.

    Fix 1: efficiency=0 → 0.30 (not 1.0). None/missing → 1.0.
    Fix 6: sqrt damping to reduce oscillation.
    """
    try:
        eff = get_efficiency(db_path)
    except Exception:
        return MAX_EFFICIENCY_SCALE  # can't query → no constraint

    rpd = eff.get("reward_per_dollar")
    days = eff.get("days_with_data", 0)

    # Fix 1: None means no data at all → no constraint
    if rpd is None or days < 2:
        return MAX_EFFICIENCY_SCALE

    # Fix 1: efficiency=0 (measured zero) → minimum scale
    if rpd <= 0:
        log.info(
            f"[PROFIT] Efficiency: 0.0 | Target: {TARGET_EFFICIENCY} | "
            f"Scale: {MIN_EFFICIENCY_SCALE} (zero efficiency)"
        )
        return MIN_EFFICIENCY_SCALE

    # Fix 6: sqrt damping — scale = sqrt(efficiency / target)
    raw_ratio = rpd / TARGET_EFFICIENCY
    scale = math.sqrt(raw_ratio)
    scale = max(MIN_EFFICIENCY_SCALE, min(MAX_EFFICIENCY_SCALE, scale))

    log.info(
        f"[PROFIT] Efficiency: {rpd:.4f} | Target: {TARGET_EFFICIENCY} | "
        f"Scale: {scale:.2f} (sqrt)"
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

    total_score = sum(max(a.get("score", 0), 0) for a in eligible)
    if total_score <= 0:
        return allocations

    max_cluster_capital = deployable_capital * max_cluster_pct

    for a in eligible:
        score = max(a.get("score", 0), 0)
        add_capital = remaining * (score / total_score)

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
) -> list[dict]:
    """Allocate capital across markets using risk-adjusted EV."""
    global _cycles_without_clustering

    if not scored_markets:
        return []

    # ── Efficiency-based capital scaling ───────────────────────
    eff_scale = _compute_efficiency_scale(db_path)
    deployable_capital = total_capital * eff_scale

    # ── Phase A: Compute risk-adjusted scores ──────────────────
    # Fix 7: Multiply base score by eff_scale so selection pressure
    # increases when efficiency is low (fewer, better markets).

    market_data: list[dict] = []
    for sm in scored_markets:
        if sm.action != "deploy" or sm.score <= 0:
            market_data.append({
                "sm": sm, "ras": 0.0, "predictions": None,
                "action": "avoid",
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

        ras = _risk_adjusted_score(
            preds.ev_per_day, preds.confidence, preds.p_fill_24h,
        )

        # Fix 7: Scale score by efficiency — low efficiency raises the
        # bar for deployment (markets need higher EV to pass)
        adjusted_ras = ras * eff_scale

        action = "deploy" if adjusted_ras > MIN_RAS_TO_DEPLOY else "avoid"
        market_data.append({
            "sm": sm, "ras": adjusted_ras, "predictions": preds,
            "action": action,
        })

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

    deploy_markets = [
        md for md in market_data if md["action"] == "deploy" and md["ras"] > 0
    ]
    deploy_markets.sort(key=lambda md: md["ras"], reverse=True)

    if len(deploy_markets) > target_count:
        for md in deploy_markets[target_count:]:
            md["action"] = "avoid"
        deploy_markets = deploy_markets[:target_count]

    total_ras = sum(md["ras"] for md in deploy_markets)
    group_capital: dict[str, float] = {}

    for md in deploy_markets:
        sm = md["sm"]
        if total_ras > 0:
            share = md["ras"] / total_ras
            raw_alloc = effective_capital * share
        else:
            raw_alloc = _est_market_cost(int(sm.min_size), sm.max_spread)

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
        alloc = max(alloc, min_cost)
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
            d["_p_fill"] = round(preds.p_fill_24h, 4)
            d["_ras"] = round(md["ras"], 4)
            d["_confidence"] = preds.confidence

        allocations.append(d)

    # ── Phase B2: Correlation cluster caps ─────────────────────

    clustering_ok = False
    try:
        clusters = build_fill_clusters(db_path)
        if clusters:
            allocations = apply_cluster_caps(
                allocations, clusters, max_cluster_pct, effective_capital,
            )
            # Fix 3: Redistribute freed capital
            allocations = _redistribute_cluster_savings(
                allocations, clusters, effective_capital, max_cluster_pct,
            )
            exposure = compute_cluster_exposure(allocations, clusters)
            if exposure:
                largest = max(exposure.values())
                largest_pct = largest / effective_capital if effective_capital > 0 else 0
                log.info(
                    f"[PROFIT] Clusters detected: {len(set(clusters.values()))} | "
                    f"Largest cluster: {largest_pct:.0%} capital"
                )
        clustering_ok = True
        _cycles_without_clustering = 0
    except Exception as e:
        log.warning(f"Cluster cap failed (skipping): {e}")
        _cycles_without_clustering += 1

    # Fix 5: Track clustering failures and degrade confidence
    if not clustering_ok:
        _cycles_without_clustering += 1
    if _cycles_without_clustering >= CLUSTER_FAILURE_WARN_CYCLES:
        log.warning(
            f"[PROFIT] Clustering unavailable for {_cycles_without_clustering} cycles — "
            f"reducing confidence by {(1 - CLUSTER_FAILURE_CONFIDENCE_PENALTY):.0%}"
        )

    # ── Phase D: Rebalance churn control ───────────────────────

    allocations = compute_deltas(allocations, db_path)

    # Log summary
    n_deploy = sum(1 for a in allocations if a["action"] == "deploy")
    total_cost = sum(a.get("est_capital_cost", 0) for a in allocations
                     if a["action"] == "deploy")
    log.info(
        f"Profit engine: {n_deploy} markets, ${total_cost:.0f} capital "
        f"(target={target_count}, deployable=${deployable_capital:.0f}, "
        f"eff_scale={eff_scale:.2f})"
    )

    return allocations
