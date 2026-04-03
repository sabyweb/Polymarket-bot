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


def score_market(m: MarketMetrics, hours: float = 24) -> float:
    """Compute net profitability score for a market.

    Score = (reward_rate - fill_damage_rate) / capital_at_risk

    Positive = profitable. Negative = losing money. Higher = better.
    """
    # Estimate hourly reward from total and on-book time
    if m.on_book_hours > 1:
        reward_rate = m.actual_reward_total / m.on_book_hours  # $/hr
    elif m.daily_rate > 0:
        reward_rate = m.daily_rate / 24 * m.q_share_pct  # theoretical
    else:
        reward_rate = 0

    # Fill damage rate
    fill_damage = m.fill_cost_recent - m.dump_revenue_recent
    fill_damage_rate = fill_damage / max(hours, 1)  # $/hr

    net_rate = reward_rate - fill_damage_rate

    # Capital at risk (what we deployed)
    # Use current position if we have one, otherwise estimate from rate
    if m.current_position_usd > 1:
        capital = m.current_position_usd
    elif m.fill_cost_recent > 0:
        capital = m.fill_cost_recent / max(m.fill_count_recent, 1)  # avg cost per fill
    else:
        capital = max(m.daily_rate * 0.5, 10)  # rough estimate

    return net_rate / max(capital, 1.0)


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

    # Decision logic
    if m.fill_count_recent == 0 and m.daily_rate > 0:
        # Zero fills = pure reward earnings, always deploy
        action = "deploy"
        shares = default_shares
        reason = f"Zero fills, ${m.daily_rate:.0f}/d pool, Q-share={m.q_share_pct*100:.1f}%"

    elif score > 0:
        # Net positive — deploy
        action = "deploy"
        shares = default_shares
        reason = f"Net positive: reward=${m.actual_reward_total:.2f} > damage=${fill_damage:.2f}"

    elif m.fill_count_recent >= 3 and fill_damage > m.actual_reward_total:
        # Many fills, damage exceeds rewards — avoid
        action = "avoid"
        shares = 0
        reason = f"High fill rate ({m.fill_count_recent}), damage=${fill_damage:.2f} > reward=${m.actual_reward_total:.2f}"

    elif confidence == "low" and m.daily_rate >= 5:
        # New market, not enough data — trial with min_size
        action = "deploy"
        shares = min_shares
        reason = f"New market (low confidence), ${m.daily_rate:.0f}/d pool — trial"

    else:
        # Default: negative score, some data → avoid
        action = "avoid"
        shares = 0
        reason = f"Net negative: score={score:.4f}, damage=${fill_damage:.2f}"

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


def rank_markets(
    metrics: list[MarketMetrics],
    hours: float = 24,
    max_markets: int = 40,
) -> list[ScoredMarket]:
    """Score all markets, rank by score descending, return recommendations.

    Args:
        metrics: Raw per-market data from data_collector
        hours: Lookback window for fill/dump data
        max_markets: Maximum markets to recommend for deployment

    Returns:
        List of ScoredMarket sorted by score (highest first).
        Markets beyond max_markets are set to "avoid" even if positive.
    """
    scored = []
    for m in metrics:
        s = score_market(m, hours)
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
