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


_REWARD_MARKETS_CACHE_TTL = 2 * 3600  # 2h — stale cache is better than no data


def _fetch_all_clob_reward_markets(
    min_rate: float = 5.0,
    db_path: str = "bot_history.db",
) -> dict[str, dict]:
    """Fetch all reward markets from CLOB endpoint. Independent of bot's tracking.

    Returns {condition_id: {"daily_rate": float, "min_size": float, "max_spread": float}}.
    Only includes markets with daily_rate >= min_rate.

    On API failure, retries once then falls back to DB cache.
    On success, saves results to DB cache for future fallback.
    """
    import requests

    result = _fetch_clob_reward_markets_api(requests, min_rate)

    if result:
        _save_reward_markets_cache(result, db_path)
        return result

    # API failed — fall back to DB cache
    cached = _load_reward_markets_cache(db_path, min_rate)
    if cached:
        log.warning(f"CLOB reward API failed — using {len(cached)} cached markets")
    else:
        log.warning("CLOB reward API failed and no cache available — zero discovery this cycle")
    return cached


def _fetch_clob_reward_markets_api(requests, min_rate: float) -> dict[str, dict]:
    """Fetch from API with one retry on failure."""
    for attempt in range(2):
        result = {}
        cursor = ""
        failed = False
        for _ in range(20):
            params = {"limit": 500}
            if cursor:
                params["next_cursor"] = cursor
            try:
                resp = requests.get(
                    "https://clob.polymarket.com/rewards/markets/current",
                    params=params, timeout=15,
                )
                if resp.status_code != 200:
                    failed = True
                    break
                data = resp.json()
            except Exception as e:
                if attempt == 0:
                    log.debug(f"CLOB reward fetch attempt {attempt + 1} failed: {e}")
                failed = True
                break
            items = data.get("data", [])
            for m in items:
                rate = float(m.get("total_daily_rate") or 0)
                if rate >= min_rate:
                    result[m["condition_id"]] = {
                        "daily_rate": rate,
                        "min_size": float(m.get("rewards_min_size") or 50),
                        "max_spread": float(m.get("rewards_max_spread") or 4.5) / 100.0,
                    }
            cursor = data.get("next_cursor", "")
            if not cursor or not items or cursor == "LTE=":
                break
        if not failed and result:
            return result
        if attempt == 0 and failed:
            time.sleep(2)  # brief backoff before retry
    return {}


def _save_reward_markets_cache(markets: dict[str, dict], db_path: str) -> None:
    """Cache CLOB reward markets to DB for fallback on API failure."""
    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.execute(
            """CREATE TABLE IF NOT EXISTS reward_markets_cache (
                condition_id TEXT PRIMARY KEY,
                daily_rate   REAL NOT NULL,
                min_size     REAL NOT NULL,
                max_spread   REAL NOT NULL,
                fetched_at   REAL NOT NULL
            )"""
        )
        now = time.time()
        db.execute("DELETE FROM reward_markets_cache")
        db.executemany(
            "INSERT INTO reward_markets_cache (condition_id, daily_rate, min_size, max_spread, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(cid, m["daily_rate"], m["min_size"], m["max_spread"], now) for cid, m in markets.items()],
        )
        db.commit()
        db.close()
    except Exception as e:
        log.debug(f"Failed to cache reward markets: {e}")


def _load_reward_markets_cache(db_path: str, min_rate: float) -> dict[str, dict]:
    """Load cached CLOB reward markets from DB."""
    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row
        cutoff = time.time() - _REWARD_MARKETS_CACHE_TTL
        rows = db.execute(
            "SELECT * FROM reward_markets_cache WHERE fetched_at > ? AND daily_rate >= ?",
            (cutoff, min_rate),
        ).fetchall()
        db.close()
        return {
            r["condition_id"]: {
                "daily_rate": r["daily_rate"],
                "min_size": r["min_size"],
                "max_spread": r["max_spread"],
            }
            for r in rows
        }
    except Exception:
        return {}


def _fetch_reward_market_expiries(condition_ids: list[str] | None = None,
                                   db_path: str = "bot_history.db") -> dict[str, str]:
    """Fetch end_date_iso for markets. Uses DB cache + Gamma + CLOB fallback.

    Cache-first: loads from market_expiry_cache table, only fetches
    for CIDs not in cache or with stale entries (>24h old).
    Reduces ~671 CLOB calls to ~10-20 per cycle (only new markets).
    """
    import requests
    result = {}
    cache_ttl = 24 * 3600  # 24h cache validity

    # Step 0: Load from DB cache
    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row
        cutoff = time.time() - cache_ttl
        rows = db.execute(
            "SELECT condition_id, end_date_iso FROM market_expiry_cache WHERE fetched_at > ?",
            (cutoff,)
        ).fetchall()
        for r in rows:
            result[r["condition_id"]] = r["end_date_iso"]
        db.close()
        if result:
            log.debug(f"Expiry cache: {len(result)} markets loaded from DB")
    except Exception:
        pass  # Table may not exist yet

    # Determine which CIDs still need fetching
    need_fetch = []
    if condition_ids:
        need_fetch = [cid for cid in condition_ids if cid not in result]
    if not need_fetch:
        log.info(f"Expiry: {len(result)} from cache, 0 to fetch")
        return result

    # Step 1: Bulk fetch from Gamma (fast, covers most markets)
    gamma_fetched = {}
    try:
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
                    gamma_fetched[cid] = end_date
    except Exception as e:
        log.debug(f"Gamma expiry fetch failed: {e}")

    # Apply Gamma results
    for cid in need_fetch:
        if cid in gamma_fetched:
            result[cid] = gamma_fetched[cid]

    # Step 2: CLOB fallback for markets still missing
    still_missing = [cid for cid in need_fetch if cid not in result]
    if still_missing:
        log.info(f"Fetching expiry for {len(still_missing)} CLOB-only markets")
        for cid in still_missing:
            try:
                resp = requests.get(
                    f"https://clob.polymarket.com/markets/{cid}", timeout=10
                )
                if resp.status_code == 200:
                    mkt = resp.json()
                    end_date = mkt.get("end_date_iso", "")
                    if end_date:
                        result[cid] = end_date
            except Exception:
                pass

    # Step 3: Write new results to cache
    new_entries = {cid: result[cid] for cid in need_fetch if cid in result}
    if new_entries:
        try:
            db = sqlite3.connect(db_path, timeout=5)
            now = time.time()
            db.executemany(
                "INSERT OR REPLACE INTO market_expiry_cache (condition_id, end_date_iso, fetched_at) VALUES (?, ?, ?)",
                [(cid, end_date, now) for cid, end_date in new_entries.items()],
            )
            db.commit()
            db.close()
        except Exception:
            pass

    cached = len(result) - len(new_entries)
    log.info(f"Expiry: {cached} cached + {len(new_entries)} fetched = {len(result)} total")
    return result


def query_placement_feedback(db_path: str) -> dict[str, dict]:
    """Read placement feedback from bot. Returns {cid: {"yes": {status, reason, ts}, "no": ...}}."""
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


def query_short_term_performance(db_path: str, hours: float = 4.0) -> dict[str, dict]:
    """Query recent performance snapshots for fast adaptation.

    Bridges the gap between immediate fast-react (THIS cycle's fills) and
    the 7-day historical adjustments (needs 3+ snapshots over days).

    Returns {condition_id: {
        "snapshots": int,
        "avg_score": float,
        "fill_snapshots": int,     -- snapshots where fills > 0
        "total_fills": int,        -- total fills across all snapshots
        "q_share_trend": float,    -- latest q_share / earliest q_share (< 1 = declining)
        "score_trend": float,      -- latest score / earliest score (< 1 = worsening)
        "latest_action": str,      -- most recent action ("deploy" or "avoid")
    }}
    """
    cutoff_ts = time.time() - hours * 3600
    result = {}
    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT condition_id, ts, net_score, fill_count, q_share_pct, action
               FROM market_performance
               WHERE ts > ?
               ORDER BY condition_id, ts""",
            (cutoff_ts,),
        ).fetchall()
        db.close()

        # Group by condition_id
        per_market: dict[str, list] = {}
        for r in rows:
            per_market.setdefault(r["condition_id"], []).append(dict(r))

        for cid, snaps in per_market.items():
            if len(snaps) < 2:
                continue

            fill_snaps = sum(1 for s in snaps if s["fill_count"] > 0)
            total_fills = sum(s["fill_count"] for s in snaps)
            avg_score = sum(s["net_score"] for s in snaps) / len(snaps)

            # Q-share trend: compare latest to earliest
            first_q = snaps[0].get("q_share_pct", 0)
            last_q = snaps[-1].get("q_share_pct", 0)
            q_trend = (last_q / first_q) if first_q > 0.001 else 1.0

            # Score trend: compare latest to earliest
            first_s = snaps[0].get("net_score", 0)
            last_s = snaps[-1].get("net_score", 0)
            if first_s > 0.01:
                score_trend = last_s / first_s
            elif first_s < -0.01:
                score_trend = last_s / first_s  # both negative → >1 if improving
            else:
                score_trend = 1.0

            result[cid] = {
                "snapshots": len(snaps),
                "avg_score": avg_score,
                "fill_snapshots": fill_snaps,
                "total_fills": total_fills,
                "q_share_trend": q_trend,
                "score_trend": score_trend,
                "latest_action": snaps[-1].get("action", "deploy"),
            }

        if result:
            log.debug(f"Short-term performance: {len(result)} markets with 2+ recent snapshots")

    except Exception as e:
        log.debug(f"Short-term performance query failed: {e}")

    return result


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
    min_size: float = 50.0         # minimum order size for rewards
    max_spread: float = 0.045      # maximum spread for rewards
    question_group: str = ""       # grouping key for portfolio concentration limits
    # Regime detection fields
    avg_bid: float = 0.0           # average bid price (from reward_tracker)
    avg_ask: float = 0.0           # average ask price (from reward_tracker)
    adverse_fills: int = 0         # fills where we lost to adverse selection
    reward_window_pct: float = 0.0 # fraction of cycles in reward spread window
    total_market_q: float = 0.0    # total market Q-score (competition depth)


def _question_group_key(question: str) -> str:
    """Extract a grouping key from a market question.

    Polymarket often has multiple markets on the same event, e.g.:
      "Will Bitcoin reach $100k by June?"
      "Will Bitcoin reach $150k by June?"
    These share the topic "bitcoin" and concentrating on all of them
    is risky — one fill event can hit all simultaneously.

    Strategy: normalize to lowercase, strip punctuation, take the first
    4 non-stopword tokens. This groups related questions together while
    keeping genuinely different topics separate.
    """
    import re
    stops = {"will", "the", "a", "an", "be", "by", "in", "on", "to", "of",
             "at", "is", "it", "or", "and", "for", "this", "that", "what",
             "how", "do", "does", "has", "have", "was", "were"}
    # Strip punctuation, normalize to lowercase
    text = re.sub(r"[^a-z0-9\s]", "", question.lower())
    # Strip numbers — they're the variable part (e.g., $100k vs $150k)
    text = re.sub(r"\b\d+\w*\b", "", text)
    words = text.split()
    key_words = [w for w in words if w not in stops and len(w) > 1]
    return " ".join(key_words[:4])


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

        # Aggregate fills — use clob_cost for both sides (actual USDC spent)
        fills = {}
        for r in db.execute(
            """SELECT condition_id,
                      SUM(shares * clob_cost) as cost,
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
                      SUM(shares * clob_cost) as cost,
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
            # avg_price is YES-equivalent; CLOB cost for NO = (1 - avg_price)
            yv = r["yes_shares"] * r["yes_avg_price"]
            no_avg = r["no_avg_price"]
            nv = r["no_shares"] * (1 - no_avg) if no_avg > 0 else 0
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
    """Query reward_market_stats for Q-score, on-book data, and regime signals.

    Returns {condition_id: {rate, q_share, on_book_hrs, question, fills,
    cycles_with_orders, total_cycles, avg_bid, avg_ask, adverse_fills,
    spread_capture_usd, cycles_in_reward_window, cycles_both_in_window,
    total_market_q}}.
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
            total_market_q = d.get("total_market_q", 0)
            if total_market_q > 0 and d.get("q_score_samples", 0) > 0:
                q_share = d["total_q_score"] / total_market_q
            on_book = d.get("time_on_book_secs", 0) / 3600
            result[d["condition_id"]] = {
                "rate": d.get("daily_rate", 0),
                "q_share": q_share,
                "on_book_hrs": on_book,
                "question": d.get("question", ""),
                "fills": d.get("buy_fills", 0),
                "cycles_with_orders": d.get("cycles_with_orders", 0),
                "total_cycles": d.get("total_cycles", 0),
                # Regime detection signals
                "avg_bid": d.get("avg_bid_price", 0),
                "avg_ask": d.get("avg_ask_price", 0),
                "adverse_fills": d.get("adverse_fills", 0),
                "spread_capture_usd": d.get("spread_capture_usd", 0),
                "cycles_in_reward_window": d.get("cycles_in_reward_window", 0),
                "cycles_both_in_window": d.get("cycles_both_in_window", 0),
                "total_market_q": total_market_q,
            }
        return result
    except Exception as e:
        log.warning(f"Reward stats query failed: {e}")
        return {}


def compute_available_capital(db_path: str, total_capital: float = 1500.0) -> float:
    """Compute available capital by subtracting locked positions, pending dumps,
    AND pending (unfilled) BUY orders.

    Returns actual deployable capital (never negative, floors at 0).
    """
    positions = query_positions(db_path)
    locked_positions = sum(p["total"] for p in positions.values())

    locked_dumps = 0.0
    locked_pending = 0.0
    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row

        # Capital locked in pending dumps
        for r in db.execute("SELECT shares, fill_price FROM dump_states").fetchall():
            locked_dumps += r["shares"] * r["fill_price"]

        # Capital locked in pending (unfilled) BUY orders
        for r in db.execute(
            "SELECT shares, price, side FROM active_orders WHERE order_type = 'buy'"
        ).fetchall():
            # price is YES-equivalent; CLOB cost depends on side
            if r["side"] == "yes":
                locked_pending += r["shares"] * r["price"]
            else:
                locked_pending += r["shares"] * (1 - r["price"])

        db.close()
    except Exception:
        pass  # tables may not exist yet

    locked = locked_positions + locked_dumps + locked_pending
    available = max(0, total_capital - locked)
    log.info(
        f"Capital: ${total_capital:.0f} total - ${locked_positions:.0f} positions "
        f"- ${locked_dumps:.0f} dumps - ${locked_pending:.0f} pending = ${available:.0f} available"
    )
    return available


def _smooth_correction_factor(
    raw_factor: float,
    db_path: str,
    alpha: float = 0.3,
    has_new_observation: bool = True,
) -> float:
    """EMA-smooth the correction factor using DB-persisted history.

    Stores each raw observation with timestamp. On read, computes an
    exponential moving average so a single noisy day doesn't whip the factor.

    If no new observation this cycle, returns the last stored EMA (or 1.0).
    """
    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.execute(
            """CREATE TABLE IF NOT EXISTS correction_factor_history (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        REAL    NOT NULL,
                raw       REAL    NOT NULL,
                smoothed  REAL    NOT NULL
            )"""
        )

        # Read last smoothed value
        row = db.execute(
            "SELECT smoothed FROM correction_factor_history ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        prev_smoothed = row[0] if row else 1.0

        if has_new_observation:
            # EMA: new_smoothed = alpha * raw + (1 - alpha) * prev_smoothed
            smoothed = alpha * raw_factor + (1 - alpha) * prev_smoothed
            smoothed = max(0.1, min(10.0, smoothed))

            db.execute(
                "INSERT INTO correction_factor_history (ts, raw, smoothed) VALUES (?, ?, ?)",
                (time.time(), raw_factor, smoothed),
            )
            # Keep only last 30 observations (~30 cycles ≈ 15h at 30min intervals)
            db.execute(
                """DELETE FROM correction_factor_history
                   WHERE id NOT IN (
                       SELECT id FROM correction_factor_history
                       ORDER BY ts DESC LIMIT 30
                   )"""
            )
            db.commit()

            if abs(smoothed - raw_factor) > 0.05:
                log.info(
                    f"Correction factor smoothed: raw={raw_factor:.3f} → "
                    f"EMA={smoothed:.3f} (prev={prev_smoothed:.3f}, α={alpha})"
                )
        else:
            smoothed = prev_smoothed
            if smoothed != 1.0:
                log.info(f"No new payout data — using last smoothed factor={smoothed:.3f}")

        db.close()
        return smoothed

    except Exception as e:
        log.debug(f"Correction factor smoothing failed: {e}")
        return raw_factor if has_new_observation else 1.0


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

    # Build unified set of all known condition_ids (from bot's DB)
    all_cids = set()
    all_cids.update(actual_rewards.keys())
    all_cids.update(fills.keys())
    all_cids.update(positions.keys())
    all_cids.update(stats.keys())

    # Independent discovery: fetch ALL CLOB reward markets so the agent
    # can score markets the bot hasn't tracked yet (closes discovery gap)
    clob_reward_markets = _fetch_all_clob_reward_markets(db_path=db_path)
    new_discovered = 0
    for cid, mkt_data in clob_reward_markets.items():
        if cid not in all_cids:
            all_cids.add(cid)
            new_discovered += 1
            stats[cid] = {
                "rate": mkt_data["daily_rate"],
                "q_share": 0,
                "on_book_hrs": 0,
                "question": "",
            }
    if new_discovered:
        log.info(f"Discovery: {new_discovered} new markets from CLOB (not yet tracked by bot)")
    log.info(f"Total: {len(clob_reward_markets)} CLOB reward markets, {len(all_cids)} CIDs")

    # Fetch expiry dates (Gamma bulk + CLOB fallback for CLOB-only markets)
    expiry_map = _fetch_reward_market_expiries(condition_ids=list(all_cids))

    metrics = []
    for cid in all_cids:
        reward = actual_rewards.get(cid, 0)
        fill_data = fills.get(cid, {"cost": 0, "count": 0})
        dump_data = dumps.get(cid, {"revenue": 0, "pnl": 0})
        pos_data = positions.get(cid, {"total": 0, "question": ""})
        stat_data = stats.get(cid, {"rate": 0, "q_share": 0, "on_book_hrs": 0, "question": ""})

        # CRITICAL: prefer fresh CLOB rate over stale DB stats.
        # The bot's reward_market_stats stores the rate from first discovery
        # and never refreshes it. A market that was $50/day when found could
        # drop to $0.14/day — without this, we'd keep using the stale $50.
        clob_data = clob_reward_markets.get(cid, {})
        if clob_data.get("daily_rate", 0) > 0:
            daily_rate = clob_data["daily_rate"]
        else:
            daily_rate = stat_data.get("rate", 0)

        question = stat_data.get("question") or pos_data.get("question", "")
        fill_cost = fill_data["cost"]
        dump_rev = dump_data["revenue"]
        net_pnl = dump_rev - fill_cost

        # Compute reward window utilization (what % of cycles are in reward spread)
        total_cyc = stat_data.get("total_cycles", 0)
        reward_window_cyc = stat_data.get("cycles_in_reward_window", 0)
        reward_window_pct = reward_window_cyc / total_cyc if total_cyc > 0 else 0.0

        metrics.append(MarketMetrics(
            condition_id=cid,
            question=question,
            daily_rate=daily_rate,
            actual_reward_total=reward,
            fill_cost_recent=fill_cost,
            dump_revenue_recent=dump_rev,
            fill_count_recent=fill_data["count"],
            net_pnl_recent=net_pnl,
            current_position_usd=pos_data.get("total", 0),
            on_book_hours=stat_data.get("on_book_hrs", 0),
            q_share_pct=stat_data.get("q_share", 0),
            end_date_iso=expiry_map.get(cid, ""),
            min_size=clob_reward_markets.get(cid, {}).get("min_size", 50.0),
            max_spread=clob_reward_markets.get(cid, {}).get("max_spread", 0.045),
            question_group=_question_group_key(question) if question else "",
            # Regime detection
            avg_bid=stat_data.get("avg_bid", 0),
            avg_ask=stat_data.get("avg_ask", 0),
            adverse_fills=stat_data.get("adverse_fills", 0),
            reward_window_pct=reward_window_pct,
            total_market_q=stat_data.get("total_market_q", 0),
        ))

    # Compute correction factor: actual total paid / sum of estimates
    # Uses EMA smoothing so a single noisy payout day doesn't whip the factor.
    estimated_daily_total = sum(
        m.daily_rate * m.q_share_pct for m in metrics if m.q_share_pct > 0
    )
    raw_factor = 1.0
    if actual_daily_total > 0 and estimated_daily_total > 0:
        actual_per_day = actual_daily_total / max(hours / 24, 0.1)
        raw_factor = actual_per_day / estimated_daily_total
        raw_factor = max(0.1, min(10.0, raw_factor))  # clamp to reasonable range
        log.info(
            f"Reward correction: actual=${actual_per_day:.2f}/d vs "
            f"estimated=${estimated_daily_total:.2f}/d → raw_factor={raw_factor:.2f}"
        )
    elif actual_daily_total > 0:
        log.info(f"Reward correction: actual=${actual_daily_total:.2f} but no estimates to compare")
    else:
        log.debug("No actual reward data — using estimates at face value (factor=1.0)")

    # EMA smoothing: blend new observation with historical average.
    # Alpha=0.3 means ~70% weight on history, ~30% on latest observation.
    # This damps out single-day spikes (late payouts, double payouts).
    correction_factor = _smooth_correction_factor(
        raw_factor, db_path, alpha=0.3,
        has_new_observation=(actual_daily_total > 0 and estimated_daily_total > 0),
    )

    log.info(
        f"Collected metrics for {len(metrics)} markets | "
        f"fills={len(fills)} positions={len(positions)} correction={correction_factor:.2f}"
    )
    return metrics, correction_factor
