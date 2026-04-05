"""Module 1: Data Collector — gathers per-market metrics from all sources.

Zero dependencies on reward_farmer.py. Uses raw requests + sqlite3 only.
Each data source fails independently — partial data is better than no data.
"""

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass

log = logging.getLogger("oversight.collector")


def _fetch_reward_market_expiries() -> dict[str, str]:
    """Fetch end_date_iso from Gamma API (CLOB rewards endpoint doesn't have it)."""
    import requests
    result = {}
    try:
        # Gamma API has endDateIso — fetch in bulk
        for offset in range(0, 10000, 100):
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"limit": 100, "offset": offset, "closed": "false"},
                timeout=15,
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            for m in batch:
                cid = m.get("conditionId", "")
                end_date = m.get("endDateIso") or m.get("end_date_iso", "")
                if cid and end_date:
                    result[cid] = end_date
        log.info(f"Fetched expiry data for {len(result)} markets from Gamma")
    except Exception as e:
        log.debug(f"Expiry fetch failed: {e}")
    return result


def query_placement_feedback(db_path: str) -> dict[str, dict]:
    """Read placement feedback from bot. Returns {cid: {"yes": {status, reason}, "no": ...}}."""
    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM placement_feedback").fetchall()
        db.close()
        result: dict[str, dict] = {}
        for r in rows:
            cid = r["condition_id"]
            if cid not in result:
                result[cid] = {}
            result[cid][r["side"]] = {"status": r["status"], "reason": r["reason"], "ts": r["ts"]}
        return result
    except Exception as e:
        log.debug(f"Placement feedback query failed: {e}")
        return {}


@dataclass
class MarketMetrics:
    """Per-market performance data from all sources."""
    condition_id: str
    question: str
    daily_rate: float              # reward pool $/day (from CLOB)
    actual_reward_total: float     # lifetime earnings (from /rewards/earned)
    fill_cost_recent: float        # fill costs in recent window (from DB)
    dump_revenue_recent: float     # dump revenue in recent window (from DB)
    fill_count_recent: int         # number of fills in recent window
    net_pnl_recent: float          # reward_delta - (fill_cost - dump_revenue)
    current_position_usd: float    # open position value (from DB)
    on_book_hours: float           # time with orders on book (from reward_tracker)
    q_share_pct: float             # our share of Q-score pool
    end_date_iso: str = ""         # market expiry (from CLOB rewards data)


def fetch_actual_rewards() -> dict[str, float]:
    """Fetch actual reward payouts from Polymarket Data API.

    Polymarket pays rewards as daily lump sums (no per-market breakdown in the API).
    This returns an empty dict for per-market data, but the daily totals are used
    by fetch_reward_correction_factor() to calibrate Q-score estimates.

    Returns: empty dict (per-market data not available from API).
    """
    # Per-market reward data is not available from any API.
    # Rewards are paid as lump sums with conditionId="".
    # Use fetch_reward_correction_factor() for estimate calibration instead.
    return {}


def fetch_reward_correction_factor(hours: float = 24) -> float:
    """Compute correction factor: actual_daily_payout / estimated_daily_total.

    Fetches actual reward payouts from Data API (lump sums), computes total
    paid in the lookback window, and returns a scaling factor for Q-score estimates.

    Returns:
        Correction factor (e.g. 0.5 means estimates are 2× too high).
        Returns 1.0 if no data available (no correction).
    """
    import requests

    # Use FUNDER address (that's where rewards are paid)
    funder = os.getenv("FUNDER", "")
    if not funder:
        # Fall back to WALLET_ADDRESS
        funder = os.getenv("WALLET_ADDRESS", "")
    if not funder:
        log.debug("No FUNDER or WALLET_ADDRESS — cannot compute correction factor")
        return 1.0

    try:
        cutoff_ts = time.time() - hours * 3600
        total_paid = 0.0
        offset = 0
        limit = 500
        payout_count = 0

        while True:
            resp = requests.get(
                "https://data-api.polymarket.com/activity",
                params={
                    "user": funder,
                    "type": "REWARD",
                    "limit": limit,
                    "offset": offset,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                break

            data = resp.json()
            if not data:
                break

            for item in data:
                ts = float(item.get("timestamp", 0))
                if ts < cutoff_ts:
                    continue
                amount = float(item.get("usdcSize", 0) or item.get("amount", 0))
                if amount > 0:
                    total_paid += amount
                    payout_count += 1

            if len(data) < limit:
                break
            offset += limit
            time.sleep(0.2)

        if total_paid > 0:
            log.info(
                f"Reward correction: ${total_paid:.2f} paid in {payout_count} payouts "
                f"over {hours:.0f}h"
            )
        else:
            log.debug(f"No reward payouts found in {hours:.0f}h window")

        return total_paid  # Return raw total; caller computes the factor

    except Exception as e:
        log.warning(f"Reward correction factor fetch failed: {e}")
        return 0.0


def query_per_market_pnl(db_path: str, hours: float = 24) -> dict[str, dict]:
    """Query per-market realized P&L from fills, unwinds, and merges.

    Returns {condition_id: {"fill_cost": float, "dump_revenue": float,
                            "merge_revenue": float, "net_trading_pnl": float}}.
    """
    cutoff = time.time() - hours * 3600
    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row

        # Aggregate fills
        fills = {}
        for r in db.execute(
            """SELECT condition_id,
                      SUM(CASE WHEN side='yes' THEN shares*price ELSE shares*clob_cost END) as cost,
                      COUNT(*) as cnt
               FROM fills WHERE ts > ? GROUP BY condition_id""",
            (cutoff,),
        ).fetchall():
            fills[r["condition_id"]] = {"cost": r["cost"] or 0, "count": r["cnt"] or 0}

        # Aggregate unwinds (dumps)
        unwinds = {}
        for r in db.execute(
            """SELECT condition_id, SUM(usd_value) as revenue
               FROM unwinds WHERE ts > ? GROUP BY condition_id""",
            (cutoff,),
        ).fetchall():
            unwinds[r["condition_id"]] = r["revenue"] or 0

        # Aggregate merges
        merges = {}
        for r in db.execute(
            """SELECT condition_id, SUM(shares) as merged_shares
               FROM merges WHERE ts > ? GROUP BY condition_id""",
            (cutoff,),
        ).fetchall():
            # Each merged share returns $1.00
            merges[r["condition_id"]] = r["merged_shares"] or 0

        db.close()

        # Combine
        all_cids = set(fills.keys()) | set(unwinds.keys()) | set(merges.keys())
        result = {}
        for cid in all_cids:
            fill_cost = fills.get(cid, {}).get("cost", 0)
            dump_rev = unwinds.get(cid, 0)
            merge_rev = merges.get(cid, 0)
            result[cid] = {
                "fill_cost": fill_cost,
                "dump_revenue": dump_rev,
                "merge_revenue": merge_rev,
                "net_trading_pnl": dump_rev + merge_rev - fill_cost,
            }
        return result

    except Exception as e:
        log.warning(f"Per-market P&L query failed: {e}")
        return {}


def query_fill_costs(db_path: str, hours: float = 24) -> dict[str, dict]:
    """Query fills table for per-market fill costs in recent window.

    Returns {condition_id: {"cost": float, "count": int, "shares": float}}.
    """
    cutoff = time.time() - hours * 3600
    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT condition_id,
                      SUM(CASE WHEN side='yes' THEN shares*price ELSE shares*clob_cost END) as cost,
                      COUNT(*) as cnt,
                      SUM(shares) as total_shares
               FROM fills WHERE ts > ?
               GROUP BY condition_id""",
            (cutoff,),
        ).fetchall()
        db.close()
        return {
            r["condition_id"]: {
                "cost": r["cost"] or 0,
                "count": r["cnt"] or 0,
                "shares": r["total_shares"] or 0,
            }
            for r in rows
        }
    except Exception as e:
        log.warning(f"Fill query failed: {e}")
        return {}


def query_dump_revenue(db_path: str, hours: float = 24) -> dict[str, dict]:
    """Query unwinds table for per-market dump revenue in recent window.

    Returns {condition_id: {"revenue": float, "pnl": float, "count": int}}.
    """
    cutoff = time.time() - hours * 3600
    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT condition_id,
                      SUM(usd_value) as revenue,
                      SUM(pnl) as total_pnl,
                      COUNT(*) as cnt
               FROM unwinds WHERE ts > ?
               GROUP BY condition_id""",
            (cutoff,),
        ).fetchall()
        db.close()
        return {
            r["condition_id"]: {
                "revenue": r["revenue"] or 0,
                "pnl": r["total_pnl"] or 0,
                "count": r["cnt"] or 0,
            }
            for r in rows
        }
    except Exception as e:
        log.warning(f"Dump query failed: {e}")
        return {}


def query_positions(db_path: str) -> dict[str, dict]:
    """Query positions table for current open exposure.

    Returns {condition_id: {"yes_usd": float, "no_usd": float, "total": float, "question": str}}.
    """
    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM positions WHERE yes_shares > 0.5 OR no_shares > 0.5"
        ).fetchall()
        db.close()
        result = {}
        for r in rows:
            yv = r["yes_shares"] * r["yes_avg_price"]
            nv = r["no_shares"] * r["no_avg_price"]
            result[r["condition_id"]] = {
                "yes_usd": yv,
                "no_usd": nv,
                "total": yv + nv,
                "question": r["question"],
            }
        return result
    except Exception as e:
        log.warning(f"Position query failed: {e}")
        return {}


def query_reward_stats(db_path: str) -> dict[str, dict]:
    """Query reward_market_stats for Q-score and on-book data.

    Returns {condition_id: {"rate": float, "q_share": float, "on_book_hrs": float, "question": str}}.
    """
    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM reward_market_stats").fetchall()
        db.close()
        result = {}
        for r in rows:
            d = json.loads(r["data"])
            q_share = 0.0
            if d.get("total_market_q", 0) > 0 and d.get("q_score_samples", 0) > 0:
                q_share = d["total_q_score"] / d["total_market_q"]
            on_book = d.get("time_on_book_secs", 0) / 3600
            result[d["condition_id"]] = {
                "rate": d.get("daily_rate", 0),
                "q_share": q_share,
                "on_book_hrs": on_book,
                "question": d.get("question", ""),
                "fills": d.get("buy_fills", 0),
                "cycles_with_orders": d.get("cycles_with_orders", 0),
                "total_cycles": d.get("total_cycles", 0),
            }
        return result
    except Exception as e:
        log.warning(f"Reward stats query failed: {e}")
        return {}


def compute_available_capital(db_path: str, total_capital: float = 1500.0) -> float:
    """Compute available capital by subtracting locked positions AND pending dumps.

    Returns actual deployable capital (never negative, floors at 0).
    """
    positions = query_positions(db_path)
    locked_positions = sum(p["total"] for p in positions.values())

    # Also count capital locked in pending dumps
    locked_dumps = 0.0
    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT shares, fill_price FROM dump_states").fetchall()
        db.close()
        for r in rows:
            locked_dumps += r["shares"] * r["fill_price"]
    except Exception:
        pass  # table may not exist yet

    locked = locked_positions + locked_dumps
    available = max(0, total_capital - locked)
    log.info(
        f"Capital: ${total_capital:.0f} total - ${locked_positions:.0f} positions "
        f"- ${locked_dumps:.0f} dumps = ${available:.0f} available"
    )
    return available


def collect_all(
    db_path: str = "bot_history.db",
    hours: float = 24,
) -> tuple[list[MarketMetrics], float]:
    """Main entry. Cross-references all data sources into per-market metrics.

    Args:
        db_path: Path to bot_history.db
        hours: Lookback window for fill/dump data

    Returns:
        Tuple of (list of MarketMetrics, reward_correction_factor).
        correction_factor: actual_daily / estimated_daily. Use to scale Q-score estimates.
        Returns 1.0 if actual payout data is unavailable.
    """
    # Gather from all sources
    actual_rewards = fetch_actual_rewards()  # empty (per-market not available)
    actual_daily_total = fetch_reward_correction_factor(hours)
    fills = query_fill_costs(db_path, hours)
    dumps = query_dump_revenue(db_path, hours)
    positions = query_positions(db_path)
    stats = query_reward_stats(db_path)

    # Fetch expiry dates from CLOB rewards endpoint (single page, fast)
    expiry_map = _fetch_reward_market_expiries()

    # Build unified set of all known condition_ids
    all_cids = set()
    all_cids.update(actual_rewards.keys())
    all_cids.update(fills.keys())
    all_cids.update(positions.keys())
    all_cids.update(stats.keys())

    metrics = []
    for cid in all_cids:
        reward = actual_rewards.get(cid, 0)
        fill_data = fills.get(cid, {"cost": 0, "count": 0})
        dump_data = dumps.get(cid, {"revenue": 0, "pnl": 0})
        pos_data = positions.get(cid, {"total": 0, "question": ""})
        stat_data = stats.get(cid, {"rate": 0, "q_share": 0, "on_book_hrs": 0, "question": ""})

        question = stat_data.get("question") or pos_data.get("question", "")
        fill_cost = fill_data["cost"]
        dump_rev = dump_data["revenue"]
        net_pnl = dump_rev - fill_cost

        metrics.append(MarketMetrics(
            condition_id=cid,
            question=question,
            daily_rate=stat_data.get("rate", 0),
            actual_reward_total=reward,
            fill_cost_recent=fill_cost,
            dump_revenue_recent=dump_rev,
            fill_count_recent=fill_data["count"],
            net_pnl_recent=net_pnl,
            current_position_usd=pos_data.get("total", 0),
            on_book_hours=stat_data.get("on_book_hrs", 0),
            q_share_pct=stat_data.get("q_share", 0),
            end_date_iso=expiry_map.get(cid, ""),
        ))

    # Compute correction factor: actual total paid / sum of estimates
    estimated_daily_total = sum(
        m.daily_rate * m.q_share_pct for m in metrics if m.q_share_pct > 0
    )
    correction_factor = 1.0
    if actual_daily_total > 0 and estimated_daily_total > 0:
        # Scale to daily if window != 24h
        actual_per_day = actual_daily_total / max(hours / 24, 0.1)
        correction_factor = actual_per_day / estimated_daily_total
        correction_factor = max(0.1, min(10.0, correction_factor))  # clamp to reasonable range
        log.info(
            f"Reward correction: actual=${actual_per_day:.2f}/d vs "
            f"estimated=${estimated_daily_total:.2f}/d → factor={correction_factor:.2f}"
        )
    elif actual_daily_total > 0:
        log.info(f"Reward correction: actual=${actual_daily_total:.2f} but no estimates to compare")
    else:
        log.debug("No actual reward data — using estimates at face value (factor=1.0)")

    log.info(
        f"Collected metrics for {len(metrics)} markets | "
        f"fills={len(fills)} positions={len(positions)} correction={correction_factor:.2f}"
    )
    return metrics, correction_factor
