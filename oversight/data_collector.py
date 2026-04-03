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


def fetch_actual_rewards() -> dict[str, float]:
    """Query actual earned rewards from Polymarket API.

    Returns {condition_id: total_earned_usd}. Empty dict on failure.
    """
    import requests

    api_key = os.getenv("CLOB_API_KEY", "")
    secret = os.getenv("CLOB_SECRET", "")
    passphrase = os.getenv("CLOB_PASS_PHRASE", "")

    if not api_key:
        log.warning("No CLOB_API_KEY — skipping rewards API")
        return {}

    try:
        headers = {
            "POLY_API_KEY": api_key,
            "POLY_SECRET": secret,
            "POLY_PASSPHRASE": passphrase,
        }
        resp = requests.get(
            "https://clob.polymarket.com/rewards/earned",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            log.debug(f"Rewards API returned {resp.status_code}")
            return {}

        data = resp.json()
        result: dict[str, float] = {}

        if isinstance(data, dict):
            for item in data.get("markets", []):
                cid = item.get("condition_id", "")
                earned = float(item.get("earned", 0))
                if cid and earned > 0:
                    result[cid] = earned
        elif isinstance(data, list):
            for item in data:
                cid = item.get("condition_id", "")
                earned = float(item.get("earned", 0))
                if cid and earned > 0:
                    result[cid] = earned

        log.info(f"Rewards API: {len(result)} markets, ${sum(result.values()):.2f} total")
        return result

    except Exception as e:
        log.warning(f"Rewards API failed: {e}")
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


def collect_all(
    db_path: str = "bot_history.db",
    hours: float = 24,
) -> list[MarketMetrics]:
    """Main entry. Cross-references all data sources into per-market metrics.

    Args:
        db_path: Path to bot_history.db
        hours: Lookback window for fill/dump data

    Returns:
        List of MarketMetrics, one per known market.
    """
    # Gather from all sources
    actual_rewards = fetch_actual_rewards()
    fills = query_fill_costs(db_path, hours)
    dumps = query_dump_revenue(db_path, hours)
    positions = query_positions(db_path)
    stats = query_reward_stats(db_path)

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

        # Net P&L: positive = profitable
        # We don't have reward delta (would need previous snapshot), use total for now
        net_pnl = dump_rev - fill_cost  # trading P&L only (excludes rewards)

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
        ))

    log.info(
        f"Collected metrics for {len(metrics)} markets | "
        f"rewards={len(actual_rewards)} fills={len(fills)} positions={len(positions)}"
    )
    return metrics
