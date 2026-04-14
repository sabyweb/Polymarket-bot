"""Reward-per-dollar efficiency tracking.

Queries actual rewards from reward_daily table to measure portfolio
efficiency and determine optimal market count.
"""

import logging

from oversight.data_collector import _connect_db

log = logging.getLogger("profit.efficiency")

# Efficiency thresholds ($/$ per day)
EFFICIENCY_HIGH = 0.03    # expand market count
EFFICIENCY_LOW = 0.01     # concentrate capital
TREND_DECLINING = -0.005  # reduce by 3 markets


def get_efficiency(db_path: str, lookback_days: int = 7) -> dict:
    """Query reward_daily for portfolio-level efficiency.

    Returns:
        reward_per_dollar: actual_reward / est_daily (daily avg)
        days_with_data: number of days with payout data
        avg_daily_reward: average daily payout ($)
        avg_daily_capital: average estimated daily total ($)
        trend: slope of reward_per_dollar over time
    """
    result = {
        "reward_per_dollar": 0.0,
        "days_with_data": 0,
        "avg_daily_reward": 0.0,
        "avg_daily_capital": 0.0,
        "trend": 0.0,
    }

    try:
        db = _connect_db(db_path)
        rows = db.execute(
            "SELECT date, total_combined_usd, est_daily_total "
            "FROM reward_daily "
            "WHERE est_daily_total > 0 "
            "ORDER BY date DESC LIMIT ?",
            (lookback_days,),
        ).fetchall()
        db.close()
    except Exception as e:
        log.debug(f"Efficiency query failed: {e}")
        return result

    if not rows:
        return result

    # Compute daily efficiencies
    efficiencies: list[float] = []
    total_reward = 0.0
    total_capital = 0.0

    for r in rows:
        reward = r[1]
        capital = r[2]
        eff = reward / capital if capital > 0 else 0
        efficiencies.append(eff)
        total_reward += reward
        total_capital += capital

    n = len(efficiencies)
    result["days_with_data"] = n
    result["avg_daily_reward"] = total_reward / n
    result["avg_daily_capital"] = total_capital / n
    result["reward_per_dollar"] = total_reward / total_capital if total_capital > 0 else 0

    # Linear trend (slope of efficiency over time)
    if n >= 3:
        # x = 0,1,...,n-1 (oldest to newest)
        # rows are DESC, so reverse for trend
        eff_asc = list(reversed(efficiencies))
        x_mean = (n - 1) / 2.0
        y_mean = sum(eff_asc) / n
        num = sum((i - x_mean) * (eff_asc[i] - y_mean) for i in range(n))
        den = sum((i - x_mean) ** 2 for i in range(n))
        result["trend"] = num / den if den > 0 else 0.0

    return result


def get_target_market_count(
    efficiency: dict,
    current_count: int,
    min_markets: int = 5,
    max_markets: int = 60,
) -> int:
    """Determine optimal market count based on efficiency.

    Strategy:
      High efficiency (≥3%):  expand toward max_markets (+5)
      Medium (1%-3%):         hold steady
      Low (<1%):              concentrate into 80% of current
      Declining trend:        reduce by 3 more
    """
    rpd = efficiency.get("reward_per_dollar", 0)
    trend = efficiency.get("trend", 0)
    days = efficiency.get("days_with_data", 0)

    if days < 2:
        # Not enough data — hold steady
        return current_count

    if rpd >= EFFICIENCY_HIGH:
        target = min(current_count + 5, max_markets)
    elif rpd >= EFFICIENCY_LOW:
        target = current_count
    else:
        target = max(min_markets, int(current_count * 0.80))

    if trend < TREND_DECLINING:
        target = max(min_markets, target - 3)

    return max(min_markets, min(target, max_markets))
