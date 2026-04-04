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

    Priority: Q-share is king. A market where we own 100% of the reward pool
    is infinitely better than one where we own 0.1%, regardless of pool size.

    Score = q_share_weight × daily_rate - fill_penalty

    This ensures:
    - 100% Q-share at $50/day scores HIGHER than 0.1% Q-share at $7500/day
    - Any fills make the score negative (fill_penalty dominates)
    - Zero-fill markets with high Q-share are always ranked first
    """
    # Effective daily reward: what we actually capture
    # Q-share 100% at $50/day = $50. Q-share 0.1% at $7500/day = $7.50.
    effective_daily = m.daily_rate * m.q_share_pct  # $/day we capture

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
    # Deploy minimum where we dominate (saves capital, same reward).
    # Deploy more only where extra shares increase our Q-share meaningfully.
    q_pct = m.q_share_pct * 100  # convert to percentage

    if q_pct >= 90:
        sized_shares = min_shares  # dominant — min_size captures the same pool
        size_reason = "dominant Q-share, min_size sufficient"
    elif q_pct >= 50:
        sized_shares = min_shares  # strong — still safe with min_size
        size_reason = "strong Q-share, min_size to reduce fill risk"
    elif q_pct >= 10:
        sized_shares = default_shares  # moderate — full size to compete
        size_reason = "moderate Q-share, full size to compete"
    elif q_pct > 0 and m.daily_rate >= 50:
        sized_shares = min_shares  # low share, high rate — trial
        size_reason = "low Q-share high rate, trial with min_size"
    else:
        sized_shares = 0  # negligible — not worth capital
        size_reason = "negligible Q-share"

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
