"""Module 2: Market Scorer — scores and ranks markets by net profitability.

Pure logic, no I/O. Takes MarketMetrics from data_collector, returns
scored and ranked markets with deployment recommendations.

Scoring is data-driven: actual rewards vs actual fill costs. No keyword
matching, no heuristics. If a market earns and doesn't lose, it's in.
"""

import logging
from dataclasses import dataclass
from .data_collector import MarketMetrics

log = logging.getLogger("oversight.scorer")


@dataclass
class ScoredMarket:
    """A market with a computed score and deployment recommendation."""
    condition_id: str
    question: str
    score: float                   # net profit rate per dollar per hour (higher = better)
    action: str                    # "deploy" or "avoid"
    recommended_shares: int        # shares per side
    reason: str                    # human-readable explanation
    confidence: str                # "high" | "medium" | "low"
    actual_reward_total: float     # from API
    fill_damage: float             # fill_cost - dump_revenue (recent)
    fill_count: int                # fills in recent window
    daily_rate: float              # pool rate


def score_market(m: MarketMetrics, hours: float = 24, correction_factor: float = 1.0) -> float:
    """Compute net profitability score for a market.

    Uses Q-score estimate (daily_rate × q_share_pct), calibrated by the
    correction_factor derived from actual vs estimated daily payouts.

    Args:
        correction_factor: actual_daily_payout / estimated_daily_total.
            < 1.0 means our estimates are too high (common — hidden orders).
            > 1.0 means our estimates are too low.
            1.0 means no correction data available.

    Score = corrected_daily_reward - daily_fill_damage
    """
    # Estimated daily reward, corrected by actual payout data
    estimated_daily = m.daily_rate * m.q_share_pct
    effective_daily = estimated_daily * correction_factor

    if correction_factor != 1.0 and estimated_daily > 0:
        log.debug(
            f"Score {m.condition_id[:12]}: est=${estimated_daily:.2f}/d "
            f"× {correction_factor:.2f} = ${effective_daily:.2f}/d"
        )

    # Fill penalty: total damage in the window, scaled to daily
    fill_damage = m.fill_cost_recent - m.dump_revenue_recent
    daily_damage = fill_damage / max(hours / 24, 0.1)

    # Score: what we earn minus what we lose, per day
    score = effective_daily - daily_damage

    # Bonus for zero fills (reward certainty)
    if m.fill_count_recent == 0 and effective_daily > 0:
        score += effective_daily * 0.5  # 50% bonus for zero-fill certainty

    return score


def classify_market(
    m: MarketMetrics,
    score: float,
    min_shares: int = 20,
    default_shares: int = 50,
) -> ScoredMarket:
    """Classify a market as deploy/avoid and generate recommendation."""

    fill_damage = m.fill_cost_recent - m.dump_revenue_recent

    # Determine confidence based on data availability
    if m.on_book_hours >= 8:
        confidence = "high"
    elif m.on_book_hours >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    # ── Intelligent sizing based on Q-share ──
    # Most markets require min_size of 50 to qualify for rewards.
    # Never go below min_shares. Only increase above default when competing.
    q_pct = m.q_share_pct * 100  # convert to percentage

    if q_pct >= 50:
        # Dominant — min_size is enough (50 on most markets)
        sized_shares = default_shares
        size_reason = f"dominant Q-share ({q_pct:.0f}%), standard size"
    elif q_pct >= 10:
        # Moderate — standard size to maintain share
        sized_shares = default_shares
        size_reason = f"moderate Q-share ({q_pct:.0f}%), standard size"
    elif q_pct > 0 and m.daily_rate >= 50:
        # Low share, high rate — worth trying at standard size
        sized_shares = default_shares
        size_reason = f"low Q-share ({q_pct:.1f}%) but ${m.daily_rate:.0f}/d rate"
    elif q_pct > 0:
        # Low share, low rate — not worth the capital
        sized_shares = 0
        size_reason = f"low Q-share ({q_pct:.1f}%), low rate, skip"
    else:
        # Zero Q-share data — new market, deploy at standard
        sized_shares = default_shares
        size_reason = "no Q-share data, standard size"

    # ── Decision logic ──
    if m.fill_count_recent == 0 and m.daily_rate > 0:
        action = "deploy"
        shares = sized_shares if sized_shares > 0 else min_shares
        reason = f"Zero fills, ${m.daily_rate:.0f}/d, Q={q_pct:.0f}%, {size_reason}"

    elif score > 0:
        action = "deploy"
        shares = sized_shares if sized_shares > 0 else min_shares
        reason = f"Net positive: rew=${m.actual_reward_total:.2f} > dmg=${fill_damage:.2f}, {size_reason}"

    elif m.fill_count_recent >= 3 and fill_damage > m.actual_reward_total:
        action = "avoid"
        shares = 0
        reason = f"High fills ({m.fill_count_recent}), dmg=${fill_damage:.2f} > rew=${m.actual_reward_total:.2f}"

    elif confidence == "low" and m.daily_rate >= 5:
        action = "deploy"
        shares = min_shares
        reason = f"New market, ${m.daily_rate:.0f}/d pool — trial with min_size"

    else:
        action = "avoid"
        shares = 0
        reason = f"Net negative: score={score:.4f}, dmg=${fill_damage:.2f}"

    return ScoredMarket(
        condition_id=m.condition_id,
        question=m.question,
        score=score,
        action=action,
        recommended_shares=shares,
        reason=reason,
        confidence=confidence,
        actual_reward_total=m.actual_reward_total,
        fill_damage=fill_damage,
        fill_count=m.fill_count_recent,
        daily_rate=m.daily_rate,
    )


def load_historical_adjustments(db_path: str, days: int = 7) -> dict[str, dict]:
    """Load historical performance data to adjust scoring.

    For markets with 3+ historical snapshots, computes:
    - trend: is the market getting better or worse over time?
    - reliability: how consistent is the score across snapshots?
    - fill_rate: what fraction of snapshots had fills?

    Returns {condition_id: {"trend_mult": float, "fill_rate": float, "snapshots": int}}
    """
    import sqlite3
    cutoff_ts = __import__("time").time() - days * 86400
    result = {}

    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row

        # Get per-market aggregates from performance history
        rows = db.execute(
            """SELECT condition_id,
                      COUNT(*) as snapshots,
                      AVG(net_score) as avg_score,
                      AVG(fill_count) as avg_fills,
                      SUM(CASE WHEN fill_count > 0 THEN 1 ELSE 0 END) as fill_snapshots,
                      MIN(net_score) as worst_score,
                      MAX(net_score) as best_score
               FROM market_performance
               WHERE ts > ?
               GROUP BY condition_id
               HAVING snapshots >= 3""",
            (cutoff_ts,),
        ).fetchall()
        db.close()

        for r in rows:
            cid = r["condition_id"]
            snapshots = r["snapshots"]
            fill_rate = r["fill_snapshots"] / snapshots if snapshots > 0 else 0

            # Trend multiplier: penalize markets that fill frequently
            # fill_rate 0% → 1.2 bonus, fill_rate 50% → 0.8 penalty, fill_rate 100% → 0.5
            trend_mult = max(0.5, 1.2 - fill_rate * 0.7)

            # Extra penalty if worst score is very negative (risky market)
            if r["worst_score"] < -5.0:
                trend_mult *= 0.8

            result[cid] = {
                "trend_mult": trend_mult,
                "fill_rate": fill_rate,
                "avg_score": r["avg_score"],
                "snapshots": snapshots,
            }

        if result:
            log.info(f"Historical adjustments: {len(result)} markets with 3+ snapshots")

    except Exception as e:
        log.debug(f"Historical adjustments load failed: {e}")

    return result


def rank_markets(
    metrics: list[MarketMetrics],
    hours: float = 24,
    max_markets: int = 40,
    correction_factor: float = 1.0,
    db_path: str = "bot_history.db",
) -> list[ScoredMarket]:
    """Score all markets, rank by score descending, return recommendations.

    Uses historical performance data (when available) to adjust scores:
    - Markets with zero fills historically get a reliability bonus
    - Markets with frequent fills get penalized
    - New markets (< 3 snapshots) scored purely on current data

    Args:
        metrics: Raw per-market data from data_collector
        hours: Lookback window for fill/dump data
        max_markets: Maximum markets to recommend for deployment
        correction_factor: Actual/estimated reward ratio for calibration
        db_path: Path to bot DB for historical performance lookup

    Returns:
        List of ScoredMarket sorted by score (highest first).
        Markets beyond max_markets are set to "avoid" even if positive.
    """
    # Load historical adjustments for adaptive scoring
    historical = load_historical_adjustments(db_path)

    scored = []
    for m in metrics:
        s = score_market(m, hours, correction_factor=correction_factor)

        # Apply historical adjustment if available
        hist = historical.get(m.condition_id)
        if hist:
            original_score = s
            s *= hist["trend_mult"]
            if abs(s - original_score) > 0.01:
                log.debug(
                    f"Historical adj {m.condition_id[:12]}: "
                    f"{original_score:.4f} × {hist['trend_mult']:.2f} = {s:.4f} "
                    f"(fill_rate={hist['fill_rate']:.0%}, {hist['snapshots']} snapshots)"
                )

        sm = classify_market(m, s)
        scored.append(sm)

    # Sort by score descending
    scored.sort(key=lambda x: x.score, reverse=True)

    # Cap at max_markets deployments
    deploy_count = 0
    for sm in scored:
        if sm.action == "deploy":
            deploy_count += 1
            if deploy_count > max_markets:
                sm.action = "avoid"
                sm.recommended_shares = 0
                sm.reason = f"Beyond top {max_markets} — capital budget exhausted"

    deploy = [s for s in scored if s.action == "deploy"]
    avoid = [s for s in scored if s.action == "avoid"]

    top_score = f"{scored[0].score:.4f}" if scored else "N/A"
    log.info(
        f"Scored {len(scored)} markets: {len(deploy)} deploy, {len(avoid)} avoid | "
        f"top score={top_score}"
    )

    return scored
