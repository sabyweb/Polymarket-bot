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
    q_share_pct: float = 0.0       # our share of Q-score pool (competition signal)
    end_date_iso: str = ""         # market expiry date (ISO format)


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

    elif confidence == "low" and m.daily_rate >= 5 and m.fill_count_recent == 0:
        # New market with NO fills and no data — small trial to gather signal.
        # Use CAPPED min_size to limit exposure on markets with huge min_size
        # (e.g., sports markets with min_size=1000 would be $500+ per trial).
        # CRITICAL: only trial if zero fills — if we already have fills and
        # score <= 0, the market is actively losing money regardless of confidence.
        action = "deploy"
        trial_size = min(int(m.min_size), default_shares)
        shares = max(trial_size, 20)  # at least 20 shares
        reason = f"Trial (competition unknown), ${m.daily_rate:.0f}/d pool, trial_size={shares}sh"

    elif m.fill_count_recent == 0 and m.daily_rate >= 10:
        # Score <= 0 but zero fills and decent rate — trial at capped min_size.
        # Competition likely high (q_share is low / unknown).
        # Worth a small probe but cap exposure until we have data.
        action = "deploy"
        trial_size = min(int(m.min_size), default_shares)
        shares = max(trial_size, 20)  # at least 20 shares
        reason = f"Zero fills, trial_size={shares}sh (competition unknown)"

    else:
        action = "avoid"
        shares = 0
        reason = f"Net negative: score={score:.4f}, dmg=${fill_damage:.2f}"

    # Compute estimated capital cost for this allocation
    est_capital = shares * cost_per_share_both if shares > 0 else 0.0

    # ── Sports / short-duration protection ──
    # Sports markets near expiry have extreme adverse selection risk.
    # Layer 1 (agent): HARD AVOID if sports + (< 4h to expiry OR missing end_date).
    # Non-sports short-duration markets (< 72h) get capped to min_size.
    from config import SPORTS_KEYWORDS, RF_SPORTS_BLOCK_HOURS

    _is_sports = False
    if m.question and action == "deploy":
        q_lower = m.question.lower()
        if any(kw in q_lower for kw in SPORTS_KEYWORDS):
            _is_sports = True

    if _is_sports and action == "deploy":
        if not m.end_date_iso:
            # No end_date = no proof it's safe. Default-deny for sports.
            action = "avoid"
            shares = 0
            est_capital = 0.0
            reason = f"Sports market with no expiry date — cannot verify safety"
        else:
            from datetime import datetime, timezone
            try:
                dt = datetime.fromisoformat(m.end_date_iso.replace("Z", "+00:00"))
                hours_to_expiry = (dt - datetime.now(timezone.utc)).total_seconds() / 3600
                if hours_to_expiry <= RF_SPORTS_BLOCK_HOURS:
                    action = "avoid"
                    shares = 0
                    est_capital = 0.0
                    reason = f"Sports market expiring in {hours_to_expiry:.1f}h (< {RF_SPORTS_BLOCK_HOURS}h block)"
                elif hours_to_expiry <= 72:
                    # Sports > 4h but < 72h: cap to min_size (reduced exposure)
                    if shares > int(m.min_size):
                        shares = int(m.min_size)
                        est_capital = shares * cost_per_share_both
                        size_reason = f"sports cap → min_size={shares}sh (${est_capital:.0f})"
            except Exception:
                # Can't parse date — treat as no date for sports
                action = "avoid"
                shares = 0
                est_capital = 0.0
                reason = f"Sports market with unparseable expiry date"

    # Non-sports short-duration cap (< 72h → min_size)
    _is_short_duration = False
    if not _is_sports and m.end_date_iso and action == "deploy":
        from datetime import datetime, timezone, timedelta
        try:
            dt = datetime.fromisoformat(m.end_date_iso.replace("Z", "+00:00"))
            hours_to_expiry = (dt - datetime.now(timezone.utc)).total_seconds() / 3600
            if 0 < hours_to_expiry <= 72:
                _is_short_duration = True
        except Exception:
            pass

    if _is_short_duration and action == "deploy" and shares > int(m.min_size):
        shares = int(m.min_size)
        est_capital = shares * cost_per_share_both
        size_reason = f"short-duration cap → min_size={shares}sh (${est_capital:.0f})"

    # ── Capital efficiency gate ──
    # Two checks:
    # 1. Pool-rate check: even at 100% Q, pool must justify capital.
    #    Catches tiny pools ($0.14/day) that can never be profitable.
    # 2. Competition-adjusted check: when q_share is KNOWN (> 0), use
    #    our REALISTIC daily earnings (rate × q_share), not the pool rate.
    #    A $50/day pool where we have 0.1% Q means $0.05/day actual earnings
    #    — the pool rate looks fine but the real return is terrible.
    if action == "deploy" and est_capital > 0 and m.daily_rate > 0:
        # Check 1: Pool rate floor (regardless of competition)
        max_daily_return_pct = m.daily_rate / est_capital
        if max_daily_return_pct < 0.01:  # < 1% of capital/day even at 100% Q
            reason = (
                f"Capital inefficient: ${m.daily_rate:.2f}/d pool vs "
                f"${est_capital:.0f} deployed = {max_daily_return_pct:.4%}/d max return"
            )
            action = "avoid"
            shares = 0
            est_capital = 0.0

        # Check 2: Competition-adjusted efficiency (only when q_share known)
        elif m.q_share_pct > 0:
            effective_daily = m.daily_rate * m.q_share_pct
            effective_return_pct = effective_daily / est_capital
            if effective_return_pct < 0.005:  # < 0.5% of capital/day with competition
                reason = (
                    f"Competition inefficient: ${effective_daily:.2f}/d effective "
                    f"(${m.daily_rate:.0f}/d × {m.q_share_pct:.1%} Q-share) vs "
                    f"${est_capital:.0f} deployed = {effective_return_pct:.4%}/d"
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
        q_share_pct=m.q_share_pct,
        end_date_iso=getattr(m, "end_date_iso", ""),
    )


def load_historical_adjustments(db_path: str, days: int = 7) -> dict[str, dict]:
    """Load historical performance data to adjust scoring.

    For markets with 2+ historical snapshots (lowered from 3 for faster learning),
    computes recency-weighted fill rate and trend multiplier.

    Recent snapshots weighted via exponential decay (half-life = 2 days).

    Returns {condition_id: {"trend_mult": float, "fill_rate": float, "snapshots": int,
                            "avg_score": float, "q_share_trend": float}}
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

        rows = db.execute(
            """SELECT condition_id, ts, fill_count, net_score, q_share_pct
               FROM market_performance
               WHERE ts > ?
               ORDER BY condition_id, ts""",
            (cutoff_ts,),
        ).fetchall()
        db.close()

        per_market: dict[str, list] = {}
        for r in rows:
            per_market.setdefault(r["condition_id"], []).append(dict(r))

        for cid, snaps in per_market.items():
            if len(snaps) < 2:  # lowered from 3 → 2 for faster onset
                continue

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
            # fill_rate 0% → 1.2 bonus, 50% → 0.8 penalty, 100% → 0.5
            trend_mult = max(0.5, 1.2 - fill_rate * 0.7)

            if worst_score < -5.0:
                trend_mult *= 0.8

            # Q-share trend: detect competition changes
            # Compare latest 25% of snapshots to earliest 25%
            q_share_trend = 1.0
            if len(snaps) >= 4:
                quarter = max(1, len(snaps) // 4)
                early_q = [s.get("q_share_pct", 0) for s in snaps[:quarter]]
                late_q = [s.get("q_share_pct", 0) for s in snaps[-quarter:]]
                avg_early = sum(early_q) / len(early_q) if early_q else 0
                avg_late = sum(late_q) / len(late_q) if late_q else 0
                if avg_early > 0.001:
                    q_share_trend = avg_late / avg_early  # < 1 = losing share

            result[cid] = {
                "trend_mult": trend_mult,
                "fill_rate": fill_rate,
                "avg_score": score_sum / total_weight if total_weight > 0 else 0,
                "snapshots": len(snaps),
                "q_share_trend": q_share_trend,
            }

        if result:
            log.info(f"Historical adjustments: {len(result)} markets with 2+ snapshots")

    except Exception as e:
        log.debug(f"Historical adjustments load failed: {e}")

    return result


def _detect_regime_signals(m: MarketMetrics) -> dict:
    """Detect structural market regime changes from available data.

    Returns dict of detected signals with multiplier and reason.
    Each signal is a multiplier on score (< 1.0 = penalty, > 1.0 = bonus).
    """
    signals = {}

    # ── Resolution proximity: price near 0 or 1 ──
    # Prefer recent prices (last ~3h from cycle_snapshots) over lifetime
    # averages. A market at 0.50 for weeks that moves to 0.95 would show
    # avg ~0.55 — the recent median catches this immediately.
    bid = m.recent_bid if m.recent_bid > 0 else m.avg_bid
    ask = m.recent_ask if m.recent_ask > 0 else m.avg_ask
    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2
        if mid > 0.92 or mid < 0.08:
            signals["resolution_proximity"] = {
                "mult": 0.3,
                "reason": f"Price near resolution ({mid:.2f})"
            }
        elif mid > 0.85 or mid < 0.15:
            signals["resolution_proximity"] = {
                "mult": 0.6,
                "reason": f"Price drifting to resolution ({mid:.2f})"
            }

    # ── Low reward window utilization ──
    # If the bot's orders are rarely within the reward spread window,
    # the market is structurally hard to earn on (competition pushes
    # prices outside the reward band). Only trigger for established markets.
    if m.on_book_hours >= 4 and m.reward_window_pct > 0:
        if m.reward_window_pct < 0.20:
            signals["low_reward_window"] = {
                "mult": 0.5,
                "reason": f"Orders in reward window only {m.reward_window_pct:.0%} of cycles"
            }
        elif m.reward_window_pct < 0.40:
            signals["low_reward_window"] = {
                "mult": 0.7,
                "reason": f"Low reward window utilization ({m.reward_window_pct:.0%})"
            }

    # ── Adverse selection signal ──
    # If most fills are adverse (we get picked off by informed traders),
    # this market has structural adverse selection. Penalty scales with severity.
    total_fills = m.fill_count_recent + getattr(m, "adverse_fills", 0)
    if m.adverse_fills > 0 and total_fills >= 3:
        adverse_pct = m.adverse_fills / total_fills
        if adverse_pct > 0.60:
            signals["adverse_selection"] = {
                "mult": 0.4,
                "reason": f"High adverse fill rate ({adverse_pct:.0%} of {total_fills} fills)"
            }

    return signals


def rank_markets(
    metrics: list[MarketMetrics],
    hours: float = 24,
    max_markets: int = 40,
    correction_factor: float = 1.0,
    db_path: str = "bot_history.db",
) -> list[ScoredMarket]:
    """Score all markets, rank by score descending, return recommendations.

    Adaptation layers (fastest → slowest):
    1. Regime detection: structural signals (resolution, adverse selection) — immediate
    2. Fast-react: THIS cycle's fill count → compound penalty — immediate
    3. Placement feedback: bot's skip/fail signals → score + size penalty — 1 cycle lag
    4. Short-term trend: 4h performance snapshots → persistent-fill detection — 1-2h
    5. Historical adjustments: 7-day trend with 2-day half-life — 2+ snapshots
    6. Q-share trend: competition shift detection from historical data — 4+ snapshots
    7. Confidence ramp: new markets start at 50% sizing — linear ramp to 8h

    Args:
        metrics: Raw per-market data from data_collector
        hours: Lookback window for fill/dump data
        max_markets: Maximum markets to recommend for deployment
        correction_factor: Actual/estimated reward ratio for calibration
        db_path: Path to bot DB for historical performance lookup

    Returns:
        List of ScoredMarket sorted by score (highest first).
    """
    import time as _time

    # Load adaptation data sources
    historical = load_historical_adjustments(db_path)

    from .data_collector import query_placement_feedback, query_short_term_performance
    feedback = query_placement_feedback(db_path)
    short_term = query_short_term_performance(db_path, hours=4.0)

    # Filter stale/resolved/expiring markets before scoring
    from datetime import datetime, timezone, timedelta
    now_dt = datetime.now(timezone.utc)
    now_ts = _time.time()
    agent_cutoff = now_dt + timedelta(hours=24)

    active_metrics = []
    min_rate = 5.0
    filtered_reasons = {"zero_rate": 0, "low_rate": 0, "expiring": 0, "no_expiry": 0}
    for m in metrics:
        if m.daily_rate <= 0:
            filtered_reasons["zero_rate"] += 1
            continue
        if m.daily_rate < min_rate:
            filtered_reasons["low_rate"] += 1
            continue
        if m.end_date_iso:
            try:
                dt = datetime.fromisoformat(m.end_date_iso.replace("Z", "+00:00"))
                if dt <= agent_cutoff:
                    filtered_reasons["expiring"] += 1
                    continue
            except Exception:
                pass
        elif m.on_book_hours == 0:
            filtered_reasons["no_expiry"] += 1
            continue
        active_metrics.append(m)

    if sum(filtered_reasons.values()) > 0:
        log.info(f"Filtered {sum(filtered_reasons.values())} markets: {dict(filtered_reasons)}")

    regime_detections = 0
    short_term_penalties = 0
    feedback_penalties = 0

    scored = []
    for m in active_metrics:
        s = score_market(m, hours, correction_factor=correction_factor)

        # ── Layer 1: Regime detection (immediate) ──
        # Detect structural market changes: resolution proximity, adverse
        # selection, low reward window utilization.
        regime = _detect_regime_signals(m)
        regime_mult = 1.0
        for sig_name, sig in regime.items():
            regime_mult *= sig["mult"]
        if regime_mult < 1.0:
            s *= regime_mult
            regime_detections += 1

        # ── Layer 2: Fast-react (immediate) ──
        # Each fill reduces score by 12% (compounds). Adverse fills count 1.5×.
        if m.fill_count_recent > 0:
            adverse = getattr(m, "adverse_fills", 0)
            effective_fills = m.fill_count_recent + adverse * 0.5
            fast_react_mult = max(0.35, 0.88 ** effective_fills)
            s *= fast_react_mult

        # ── Layer 3: Placement feedback (1 cycle lag) ──
        # Enhanced: handles one-sided skips, checks feedback freshness,
        # adjusts both score AND sizing.
        placement_penalty = 1.0
        fb = feedback.get(m.condition_id, {})
        yes_fb = fb.get("yes", {})
        no_fb = fb.get("no", {})
        yes_skip = yes_fb.get("status") == "skipped"
        no_skip = no_fb.get("status") == "skipped"
        yes_fail = yes_fb.get("status") == "failed"
        no_fail = no_fb.get("status") == "failed"

        # Check feedback freshness (ignore stale feedback > 2h old)
        feedback_fresh = True
        fb_ts = max(yes_fb.get("ts", 0), no_fb.get("ts", 0))
        if fb_ts > 0 and (now_ts - fb_ts) > 7200:
            feedback_fresh = False

        if feedback_fresh:
            if yes_skip and no_skip:
                # Both sides can't place — heavy penalty
                skip_reason = yes_fb.get("reason", "")
                if skip_reason in ("wide_spread", "exit_liquidity"):
                    s *= 0.3
                    placement_penalty = 0.3
                    feedback_penalties += 1
                elif skip_reason not in ("capital_exhausted", "already_has_order"):
                    s *= 0.5
                    placement_penalty = 0.5
                    feedback_penalties += 1
            elif yes_skip or no_skip:
                # One side can't place — moderate penalty (still earning half rewards)
                skip_side = "yes" if yes_skip else "no"
                skip_reason = fb.get(skip_side, {}).get("reason", "")
                if skip_reason in ("wide_spread", "exit_liquidity"):
                    s *= 0.6  # 40% penalty for one-sided structural skip
                    placement_penalty = 0.6
                    feedback_penalties += 1
            if yes_fail and no_fail:
                # Both sides failed — something fundamentally broken
                fail_reason = yes_fb.get("reason", "")
                if fail_reason not in ("capital_exhausted",):
                    s *= 0.4
                    placement_penalty = 0.4
                    feedback_penalties += 1

        # ── Layer 4: Short-term trend (1-2h) ──
        # Bridges gap between immediate fast-react and 7-day historical.
        # If fills are persistent across multiple recent snapshots, apply
        # extra penalty on top of fast-react (which only sees this cycle).
        st = short_term.get(m.condition_id)
        short_term_mult = 1.0
        if st and st["snapshots"] >= 2:
            # Persistent fills: if 50%+ of recent snapshots had fills, penalize
            fill_persistence = st["fill_snapshots"] / st["snapshots"]
            if fill_persistence >= 0.75:
                short_term_mult = 0.5  # 75%+ snapshots with fills → heavy
                short_term_penalties += 1
            elif fill_persistence >= 0.50:
                short_term_mult = 0.7  # 50%+ snapshots with fills → moderate
                short_term_penalties += 1

            # Score declining rapidly (latest < 50% of earliest)
            if st["score_trend"] < 0.5 and st["avg_score"] < 0:
                short_term_mult *= 0.7  # compounding penalty

            s *= short_term_mult

        # ── Layer 5: Confidence ramp-up for new markets ──
        confidence_mult = 1.0
        if m.on_book_hours < 8:
            confidence_mult = 0.5 + 0.5 * (m.on_book_hours / 8.0)

        # ── Layer 6: Historical adjustments (7-day) ──
        hist = historical.get(m.condition_id)
        if hist:
            original_score = s
            s *= hist["trend_mult"]

            # Layer 6b: Q-share trend — competition shift detection
            # If our Q-share is dropping (new competitors entering),
            # apply additional penalty proportional to the decline.
            q_trend = hist.get("q_share_trend", 1.0)
            if q_trend < 0.5:
                # Q-share halved or worse → heavy penalty (losing competition)
                s *= 0.6
                log.debug(f"Q-share declining {m.condition_id[:12]}: trend={q_trend:.2f}")
            elif q_trend < 0.75:
                # Q-share declining meaningfully → moderate penalty
                s *= 0.8

            if abs(s - original_score) > 0.01:
                log.debug(
                    f"Historical adj {m.condition_id[:12]}: "
                    f"{original_score:.4f} → {s:.4f} "
                    f"(fill_rate={hist['fill_rate']:.0%}, q_trend={q_trend:.2f}, "
                    f"{hist['snapshots']} snapshots)"
                )

        sm = classify_market(m, s)

        # Apply confidence ramp-up to sizing (not score)
        if confidence_mult < 1.0 and sm.recommended_shares > 0:
            sm.recommended_shares = max(
                int(m.min_size),
                int(sm.recommended_shares * confidence_mult),
            )

        # Apply placement penalty to sizing too
        if placement_penalty < 1.0 and sm.recommended_shares > 0:
            sm.recommended_shares = max(
                int(m.min_size),
                int(sm.recommended_shares * placement_penalty),
            )

        # Apply short-term penalty to sizing (persistent fills → shrink orders)
        if short_term_mult < 1.0 and sm.recommended_shares > 0:
            sm.recommended_shares = max(
                int(m.min_size),
                int(sm.recommended_shares * short_term_mult),
            )

        scored.append(sm)

    # Sort by score descending
    scored.sort(key=lambda x: x.score, reverse=True)

    # ── Cap trial deployments ──
    # Trial markets (score <= 0, deployed for discovery) are valuable but
    # must be limited. Without a cap, all ~1000 CLOB reward markets with
    # no history get deployed, flooding the bot with unknown markets and
    # consuming all capital on min_size orders.
    # Sort trials by daily_rate desc so the richest pools get trialed first.
    max_trials = 10
    trials = [(i, sm) for i, sm in enumerate(scored) if sm.action == "deploy" and sm.score <= 0]
    trials.sort(key=lambda x: x[1].daily_rate, reverse=True)
    for rank, (idx, sm) in enumerate(trials):
        if rank >= max_trials:
            sm.action = "avoid"
            sm.recommended_shares = 0
            sm.reason = f"Trial cap reached ({max_trials} max)"
    if len(trials) > max_trials:
        log.info(f"Trial cap: kept {max_trials} of {len(trials)} trial markets (by daily_rate)")

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
    if regime_detections or short_term_penalties or feedback_penalties:
        log.info(
            f"Adaptation: {regime_detections} regime signals, "
            f"{short_term_penalties} short-term penalties, "
            f"{feedback_penalties} feedback penalties"
        )

    return scored
