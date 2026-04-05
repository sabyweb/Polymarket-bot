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
    default_shares: int = 50,
) -> ScoredMarket:
    """Classify a market as deploy/avoid and set sizing.

    Sizing strategy:
    - Never below market's min_size (most are 50, some are 100-200+)
    - Scale UP on zero-fill, high Q-share, high-rate markets
    - More capital on proven profitable markets = more rewards
    """

    fill_damage = m.fill_cost_recent - m.dump_revenue_recent

    # Determine confidence based on data availability
    if m.on_book_hours >= 8:
        confidence = "high"
    elif m.on_book_hours >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    # ── Intelligent sizing ──
    # Base: market's min_size (usually 50, can be 100, 200, 1000+)
    # Scale up for zero-fill high-value markets — more shares = more Q-score = more reward
    q_pct = m.q_share_pct * 100

    # Start at default
    sized_shares = default_shares

    if m.fill_count_recent == 0 and q_pct >= 90 and m.daily_rate >= 50:
        # Zero fills + dominant + high rate: scale to 2x-4x default
        # Higher rate = more worth deploying extra capital
        if m.daily_rate >= 200:
            sized_shares = default_shares * 4  # 200sh for $200+/day markets
        elif m.daily_rate >= 100:
            sized_shares = default_shares * 3  # 150sh for $100+/day markets
        else:
            sized_shares = default_shares * 2  # 100sh for $50+/day markets
        size_reason = f"zero-fill {q_pct:.0f}%Q ${m.daily_rate:.0f}/d, scaled {sized_shares}sh"
    elif m.fill_count_recent == 0 and q_pct >= 50:
        # Zero fills + strong Q: standard or slight bump
        sized_shares = default_shares
        size_reason = f"zero-fill {q_pct:.0f}%Q, standard {sized_shares}sh"
    elif q_pct > 0 and m.daily_rate >= 50:
        sized_shares = default_shares
        size_reason = f"{q_pct:.0f}%Q ${m.daily_rate:.0f}/d rate, standard"
    elif q_pct == 0:
        sized_shares = default_shares
        size_reason = "new market, standard size"
    else:
        sized_shares = default_shares
        size_reason = f"{q_pct:.0f}%Q, standard"

    # ── Decision logic ──
    if m.fill_count_recent == 0 and m.daily_rate > 0:
        action = "deploy"
        shares = sized_shares if sized_shares > 0 else default_shares
        reason = f"Zero fills, ${m.daily_rate:.0f}/d, Q={q_pct:.0f}%, {size_reason}"

    elif score > 0:
        action = "deploy"
        shares = sized_shares if sized_shares > 0 else default_shares
        reason = f"Net positive: rew=${m.actual_reward_total:.2f} > dmg=${fill_damage:.2f}, {size_reason}"

    elif m.fill_count_recent >= 3 and fill_damage > m.actual_reward_total:
        action = "avoid"
        shares = 0
        reason = f"High fills ({m.fill_count_recent}), dmg=${fill_damage:.2f} > rew=${m.actual_reward_total:.2f}"

    elif confidence == "low" and m.daily_rate >= 5:
        action = "deploy"
        shares = default_shares
        reason = f"New market, ${m.daily_rate:.0f}/d pool — trial with default_size"

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

    # Filter stale/resolved/expiring markets before scoring
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    agent_cutoff = now + timedelta(hours=24)  # Agent is more conservative: 24h (bot uses 12h)

    # Load placement feedback for closed-loop scoring
    from .data_collector import query_placement_feedback
    feedback = query_placement_feedback(db_path)

    active_metrics = []
    filtered_reasons = {"zero_rate": 0, "expiring": 0, "both_skipped": 0}
    for m in metrics:
        if m.daily_rate <= 0:
            filtered_reasons["zero_rate"] += 1
            continue
        # Expiry check using actual end_date_iso from CLOB
        if m.end_date_iso:
            try:
                dt = datetime.fromisoformat(m.end_date_iso.replace("Z", "+00:00"))
                if dt <= agent_cutoff:
                    filtered_reasons["expiring"] += 1
                    continue
            except Exception:
                pass
        active_metrics.append(m)

    if sum(filtered_reasons.values()) > 0:
        log.info(f"Filtered {sum(filtered_reasons.values())} markets: {dict(filtered_reasons)}")

    scored = []
    for m in active_metrics:
        s = score_market(m, hours, correction_factor=correction_factor)

        # Penalize markets the bot persistently can't place on
        fb = feedback.get(m.condition_id, {})
        yes_skip = fb.get("yes", {}).get("status") == "skipped"
        no_skip = fb.get("no", {}).get("status") == "skipped"
        if yes_skip and no_skip:
            skip_reason = fb.get("yes", {}).get("reason", "")
            if skip_reason in ("wide_spread", "exit_liquidity"):
                s *= 0.3  # Heavy penalty — bot can't trade this market
                log.debug(f"Placement penalty {m.condition_id[:12]}: both sides skipped ({skip_reason})")
            elif skip_reason not in ("capital_exhausted", "already_has_order"):
                s *= 0.5  # Moderate penalty for other persistent skips

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
