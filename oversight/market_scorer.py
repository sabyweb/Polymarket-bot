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
    min_size: float = 50.0         # minimum order size for rewards
    max_spread: float = 0.045      # maximum spread for rewards
    est_capital_cost: float = 0.0  # estimated $ to deploy (both sides)
    locked_position_usd: float = 0.0  # $ currently locked in open positions
    question_group: str = ""       # topic group for concentration limits


def score_market(m: MarketMetrics, hours: float = 24, correction_factor: float = 1.0) -> float:
    """Compute net profitability score for a market.

    Uses Q-score estimate (daily_rate × q_share_pct), calibrated by the
    correction_factor derived from actual vs estimated daily payouts.

    Args:
        correction_factor: actual_daily_payout / estimated_daily_total.
            < 1.0 means our estimates are too high (common — hidden orders).
            > 1.0 means our estimates are too low.
            1.0 means no correction data available.

    Score = corrected_daily_reward - daily_fill_damage + zero_fill_bonus
    Zero-fill bonus is capped at $2/day so low-rate markets can't outrank
    genuinely profitable ones purely on fill luck.
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

    # Bonus for zero fills — capped so a $0.01/day market with zero fills
    # can't outscore a $50/day market with minor fill damage.
    # Cap: min(50% of effective, $2/day) — meaningful for real markets,
    # negligible for dust-rate ones.
    if m.fill_count_recent == 0 and effective_daily > 0:
        score += min(effective_daily * 0.5, 2.0)

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

    # ── Capital-aware continuous sizing ──
    # Think in DOLLARS, not shares. A share at $0.02 costs 50× less than one
    # at $0.95, so raw share counts are meaningless without price context.
    #
    # Step 1: Compute cost per share for this market.
    #   For symmetric reward LPs quoting both sides, cost per side ≈ (1 - 2*spread)/2.
    #   Combined cost = cost_per_share_per_side × 2 (YES + NO).
    # Step 2: Decide a TARGET CAPITAL amount based on score (continuous log curve).
    # Step 3: Convert target capital → shares via actual cost.
    #
    # This means a $0.02 market gets ~10× more shares than a $0.20 market
    # for the same capital, which is correct — cheap shares are cheap to deploy.
    q_pct = m.q_share_pct * 100
    import math

    spread = m.max_spread if m.max_spread > 0 else 0.045
    cost_per_share_per_side = max(0.05, (1.0 - 2 * spread) / 2)
    cost_per_share_both = cost_per_share_per_side * 2  # YES + NO side

    # Base capital is a FIXED dollar amount — what default_shares costs at
    # a typical spread (0.045). This is the anchor; actual share count then
    # varies by market price. Cheap markets get more shares for the same $.
    typical_cost_both = 2 * max(0.05, (1.0 - 2 * 0.045) / 2)  # ~$0.91
    base_capital = default_shares * typical_cost_both  # ~$45.50

    # Score-based capital multiplier (continuous log curve):
    #   score=0   → 1.0× base capital
    #   score=25  → ~1.7× base capital
    #   score=50  → ~2.0× base capital
    #   score=100 → ~2.3× base capital
    # Logarithmic = diminishing returns, prevents over-concentration.
    scale_param = 25.0  # score at which we roughly double capital allocation
    if score > 0 and m.fill_count_recent == 0:
        raw_mult = 1.0 + math.log2(1 + score / scale_param)
        multiplier = min(raw_mult, 4.0)  # cap at 4× base capital
    elif score > 0:
        # Has fills but still net positive — conservative 1.0–1.5×
        raw_mult = 1.0 + 0.5 * math.log2(1 + score / scale_param)
        multiplier = min(raw_mult, 1.5)
    else:
        multiplier = 1.0

    target_capital = base_capital * multiplier
    sized_shares = max(default_shares, int(target_capital / cost_per_share_both))

    if score > 0 and m.fill_count_recent == 0:
        size_reason = (
            f"zero-fill {q_pct:.0f}%Q ${m.daily_rate:.0f}/d, "
            f"${cost_per_share_per_side:.2f}/sh, "
            f"cap-mult={multiplier:.1f}x → {sized_shares}sh (${target_capital:.0f})"
        )
    elif score > 0:
        size_reason = (
            f"{q_pct:.0f}%Q, fills={m.fill_count_recent}, "
            f"${cost_per_share_per_side:.2f}/sh, "
            f"cons-mult={multiplier:.1f}x → {sized_shares}sh (${target_capital:.0f})"
        )
    elif q_pct == 0:
        size_reason = f"new market, ${cost_per_share_per_side:.2f}/sh, standard {sized_shares}sh"
    else:
        size_reason = f"{q_pct:.0f}%Q, score≤0, ${cost_per_share_per_side:.2f}/sh, standard"

    # ── Decision logic ──
    # Primary gate: score > 0 means reward exceeds damage. Deploy.
    # Secondary: zero-fill markets with score > 0 get sized shares.
    # Trial: new markets (low confidence) with decent rate get a small trial.
    # Avoid: everything else.
    if score > 0:
        action = "deploy"
        shares = sized_shares if sized_shares > 0 else default_shares
        if m.fill_count_recent == 0:
            reason = f"Net positive (zero fills), ${m.daily_rate:.0f}/d, Q={q_pct:.0f}%, {size_reason}"
        else:
            reason = f"Net positive: rew=${m.actual_reward_total:.2f} > dmg=${fill_damage:.2f}, {size_reason}"

    elif m.fill_count_recent >= 3 and fill_damage > m.actual_reward_total:
        action = "avoid"
        shares = 0
        reason = f"High fills ({m.fill_count_recent}), dmg=${fill_damage:.2f} > rew=${m.actual_reward_total:.2f}"

    elif confidence == "low" and m.daily_rate >= 5:
        # New market with no data — small trial to gather signal
        action = "deploy"
        shares = default_shares
        reason = f"New market, ${m.daily_rate:.0f}/d pool — trial with default_size"

    elif m.fill_count_recent == 0 and m.daily_rate >= 10:
        # Score <= 0 but zero fills and decent rate — trial at default size.
        # Could be a market where q_share is low (hidden competition)
        # but no fill risk. Worth a small probe.
        action = "deploy"
        shares = default_shares
        reason = f"Zero fills but low Q-share, trial at {default_shares}sh"

    else:
        action = "avoid"
        shares = 0
        reason = f"Net negative: score={score:.4f}, dmg=${fill_damage:.2f}"

    # Compute estimated capital cost for this allocation
    est_capital = shares * cost_per_share_both if shares > 0 else 0.0

    # ── Capital efficiency gate ──
    # If the total pool reward is too low relative to capital deployed,
    # this market can never be capital-efficient regardless of Q-share.
    # Example: $0.14/day pool, $186 deployed → 0.075%/day even at 100% Q.
    # Threshold: pool rate must be at least 1% of deployed capital per day
    # (i.e., payback in <100 days at 100% Q-share, which is already generous).
    if action == "deploy" and est_capital > 0 and m.daily_rate > 0:
        max_daily_return_pct = m.daily_rate / est_capital
        if max_daily_return_pct < 0.01:  # < 1% of capital/day even at 100% Q
            reason = (
                f"Capital inefficient: ${m.daily_rate:.2f}/d pool vs "
                f"${est_capital:.0f} deployed = {max_daily_return_pct:.4%}/d max return"
            )
            action = "avoid"
            shares = 0
            est_capital = 0.0

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
        min_size=m.min_size,
        max_spread=m.max_spread,
        est_capital_cost=est_capital,
        locked_position_usd=m.current_position_usd,
        question_group=getattr(m, "question_group", ""),
    )


def load_historical_adjustments(db_path: str, days: int = 7) -> dict[str, dict]:
    """Load historical performance data to adjust scoring.

    For markets with 3+ historical snapshots, computes:
    - trend: is the market getting better or worse over time?
    - reliability: how consistent is the score across snapshots?
    - fill_rate: what fraction of snapshots had fills (recency-weighted)?

    Recent snapshots are weighted more heavily via exponential decay
    (half-life = 2 days). A fill yesterday counts ~3.5x more than a
    fill 5 days ago.

    Returns {condition_id: {"trend_mult": float, "fill_rate": float, "snapshots": int}}
    """
    import sqlite3
    import math
    now = __import__("time").time()
    cutoff_ts = now - days * 86400
    result = {}
    half_life_secs = 2 * 86400  # 2-day half-life

    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row

        # Fetch individual snapshots for recency-weighted fill rate
        rows = db.execute(
            """SELECT condition_id, ts, fill_count, net_score
               FROM market_performance
               WHERE ts > ?
               ORDER BY condition_id, ts""",
            (cutoff_ts,),
        ).fetchall()
        db.close()

        # Group by condition_id
        per_market: dict[str, list] = {}
        for r in rows:
            cid = r["condition_id"]
            per_market.setdefault(cid, []).append(dict(r))

        for cid, snaps in per_market.items():
            if len(snaps) < 3:
                continue

            # Recency-weighted fill rate: each snapshot weighted by
            # 2^(-age_days / half_life_days)
            weighted_fills = 0.0
            total_weight = 0.0
            worst_score = float("inf")
            score_sum = 0.0

            for s in snaps:
                age_secs = max(0, now - s["ts"])
                weight = math.pow(0.5, age_secs / half_life_secs)
                total_weight += weight
                if s["fill_count"] > 0:
                    weighted_fills += weight
                worst_score = min(worst_score, s["net_score"])
                score_sum += s["net_score"] * weight

            fill_rate = weighted_fills / total_weight if total_weight > 0 else 0

            # Trend multiplier: penalize markets that fill frequently
            # fill_rate 0% → 1.2 bonus, fill_rate 50% → 0.8 penalty, fill_rate 100% → 0.5
            trend_mult = max(0.5, 1.2 - fill_rate * 0.7)

            # Extra penalty if worst score is very negative (risky market)
            if worst_score < -5.0:
                trend_mult *= 0.8

            result[cid] = {
                "trend_mult": trend_mult,
                "fill_rate": fill_rate,
                "avg_score": score_sum / total_weight if total_weight > 0 else 0,
                "snapshots": len(snaps),
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
    min_rate = 5.0  # minimum $/day to consider — matches discovery filter
    filtered_reasons = {"zero_rate": 0, "low_rate": 0, "expiring": 0, "no_expiry": 0, "both_skipped": 0}
    for m in metrics:
        if m.daily_rate <= 0:
            filtered_reasons["zero_rate"] += 1
            continue
        if m.daily_rate < min_rate:
            filtered_reasons["low_rate"] += 1
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
        elif m.on_book_hours == 0:
            # Fail-closed: new market with no expiry data — skip until expiry is fetched
            # Only skip markets we haven't tracked yet (on_book_hours=0).
            # Markets the bot already tracks (on_book_hours>0) are allowed through
            # because the bot's _verify_order_books already checked their expiry.
            filtered_reasons["no_expiry"] += 1
            continue
        active_metrics.append(m)

    if sum(filtered_reasons.values()) > 0:
        log.info(f"Filtered {sum(filtered_reasons.values())} markets: {dict(filtered_reasons)}")

    scored = []
    for m in active_metrics:
        s = score_market(m, hours, correction_factor=correction_factor)

        # ── Fast-react: immediate penalty for recent fills ──
        # If a market had fills in THIS window, apply a circuit-breaker
        # penalty proportional to fill count. Don't wait for historical
        # data to accumulate — react NOW. This is the single biggest
        # adaptation speed improvement.
        if m.fill_count_recent > 0:
            # Each fill in the window reduces score by 15% (compounds)
            # 1 fill → 0.85x, 2 fills → 0.72x, 3+ fills → 0.61x or worse
            fast_react_mult = 0.85 ** m.fill_count_recent
            fast_react_mult = max(fast_react_mult, 0.3)  # floor at 30%
            s *= fast_react_mult

        # ── Confidence ramp-up for new markets ──
        # Markets with < 8h on-book get a graduated confidence discount
        # on their sizing (not score). This prevents the agent from
        # over-allocating to unproven markets. Ramps linearly:
        #   0h → 50%, 4h → 75%, 8h+ → 100%
        confidence_mult = 1.0
        if m.on_book_hours < 8:
            confidence_mult = 0.5 + 0.5 * (m.on_book_hours / 8.0)

        # Penalize markets the bot persistently can't place on
        placement_penalty = 1.0
        fb = feedback.get(m.condition_id, {})
        yes_skip = fb.get("yes", {}).get("status") == "skipped"
        no_skip = fb.get("no", {}).get("status") == "skipped"
        if yes_skip and no_skip:
            skip_reason = fb.get("yes", {}).get("reason", "")
            if skip_reason in ("wide_spread", "exit_liquidity"):
                s *= 0.3  # Heavy penalty — bot can't trade this market
                placement_penalty = 0.3
                log.debug(f"Placement penalty {m.condition_id[:12]}: both sides skipped ({skip_reason})")
            elif skip_reason not in ("capital_exhausted", "already_has_order"):
                s *= 0.5  # Moderate penalty for other persistent skips
                placement_penalty = 0.5

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

        # Apply confidence ramp-up to sizing (not score)
        if confidence_mult < 1.0 and sm.recommended_shares > 0:
            sm.recommended_shares = max(
                int(m.min_size),
                int(sm.recommended_shares * confidence_mult),
            )

        # Reduce allocation size when bot can't place — don't just penalize
        # the score, also shrink the order so the bot doesn't keep failing
        # with the same oversized order next cycle.
        if placement_penalty < 1.0 and sm.recommended_shares > 0:
            sm.recommended_shares = max(
                int(m.min_size),
                int(sm.recommended_shares * placement_penalty),
            )

        scored.append(sm)

    # Sort by score descending
    scored.sort(key=lambda x: x.score, reverse=True)

    # Log how many are beyond the soft max (informational only).
    # Do NOT demote deploy→avoid based on estimated capital — the bot
    # places orders in score order and stops when the exchange returns
    # an insufficient-balance error. That's the real capital gate.
    deploy_count = sum(1 for sm in scored if sm.action == "deploy")
    if deploy_count > max_markets:
        log.info(
            f"Note: {deploy_count} markets marked deploy (soft cap={max_markets}). "
            f"Bot will place in score order and stop on exchange balance error."
        )

    deploy = [s for s in scored if s.action == "deploy"]
    avoid = [s for s in scored if s.action == "avoid"]

    top_score = f"{scored[0].score:.4f}" if scored else "N/A"
    log.info(
        f"Scored {len(scored)} markets: {len(deploy)} deploy, {len(avoid)} avoid | "
        f"top score={top_score}"
    )

    return scored
