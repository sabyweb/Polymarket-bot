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

from config import RF_NEW_MARKET_Q_SHARE_PRIOR, RF_POISONED_Q_SHARE_THRESHOLD

log = logging.getLogger("oversight.collector")


_REWARD_MARKETS_CACHE_TTL = 2 * 3600  # 2h — stale cache is better than no data


def _connect_db(db_path: str, timeout: int = 10) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and retry on lock.

    The bot process writes to bot_history.db every 30s. The agent reads
    every 30min. Without WAL, concurrent access causes
    'database is locked' errors that silently return empty data.
    WAL allows concurrent readers + single writer.
    """
    db = sqlite3.connect(db_path, timeout=timeout)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")  # 10s wait on lock
    db.row_factory = sqlite3.Row
    return db


def _query_with_retry(db_path: str, query: str, params: tuple = (),
                       fetch: str = "all") -> list | None:
    """Execute a read query with one retry on lock failure.

    Returns list of rows, or None on failure (never empty-as-failure).
    Callers MUST distinguish None (query failed) from [] (no results).
    """
    for attempt in range(2):
        try:
            db = _connect_db(db_path)
            if fetch == "all":
                result = db.execute(query, params).fetchall()
            else:
                result = db.execute(query, params).fetchone()
            db.close()
            return result if result is not None else []
        except sqlite3.OperationalError as e:
            if attempt == 0 and "locked" in str(e).lower():
                log.warning(f"DB locked on read, retrying in 2s: {e}")
                time.sleep(2)
                continue
            log.warning(f"DB query failed after retry: {e}")
            return None
        except Exception as e:
            log.warning(f"DB query error: {e}")
            return None
    return None


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
        db = _connect_db(db_path)
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


def compute_clob_rate_delta(
    current_markets: dict[str, dict],
    db_path: str = "bot_history.db",
) -> float:
    """Compare current CLOB total reward rate against previous cache.

    Returns percentage change: (current - previous) / previous.
    Positive = rates increased, negative = rates decreased.
    Returns 0.0 if no previous cache exists.

    This is a FORWARD-LOOKING indicator: CLOB rates change immediately
    when Polymarket adjusts rewards, while Data API payouts lag by 24h.
    A -30% delta means the payout window is stale and the correction
    factor will be wrong for the next 24h.
    """
    current_total = sum(m.get("daily_rate", 0) for m in current_markets.values())
    if current_total <= 0:
        return 0.0

    row = _query_with_retry(
        db_path, "SELECT SUM(daily_rate) as total FROM reward_markets_cache",
        fetch="one",
    )
    if row is None:
        return 0.0
    previous_total = row[0] if row[0] else 0

    if previous_total <= 0:
        return 0.0

    delta_pct = (current_total - previous_total) / previous_total
    if abs(delta_pct) > 0.10:
        log.info(
            f"CLOB rate delta: {delta_pct:+.1%} "
            f"(${previous_total:.0f}/d → ${current_total:.0f}/d)"
        )
    return delta_pct


def _load_reward_markets_cache(db_path: str, min_rate: float) -> dict[str, dict]:
    """Load cached CLOB reward markets from DB."""
    cutoff = time.time() - _REWARD_MARKETS_CACHE_TTL
    rows = _query_with_retry(
        db_path,
        "SELECT * FROM reward_markets_cache WHERE fetched_at > ? AND daily_rate >= ?",
        (cutoff, min_rate),
    )
    if rows is None:
        return {}
    return {
        r["condition_id"]: {
            "daily_rate": r["daily_rate"],
            "min_size": r["min_size"],
            "max_spread": r["max_spread"],
        }
        for r in rows
    }


def _fetch_reward_market_expiries(condition_ids: list[str] | None = None,
                                   db_path: str = "bot_history.db") -> dict[str, dict[str, str]]:
    """Fetch end_date_iso, game_start_time, and question text for markets.

    Returns {condition_id: {"end_date_iso": str, "game_start_time": str, "question": str}}.
    game_start_time is the actual event/kickoff time (ISO 8601); it is only
    populated for markets fetched via CLOB (Gamma API does not expose this
    field). Gamma-routed markets will have game_start_time="".
    question is the human-readable market question; it gates safety controls
    in market_scorer (sports detection) and allocator (per-group concentration cap).

    Cache-first: loads from market_expiry_cache table, only fetches
    for CIDs not in cache or with stale entries (>24h old).
    Reduces ~671 CLOB calls to ~10-20 per cycle (only new markets).
    """
    import requests
    result: dict[str, dict[str, str]] = {}
    cache_ttl = 24 * 3600  # 24h cache validity

    # Step 0: Load from DB cache
    cutoff = time.time() - cache_ttl
    rows = _query_with_retry(
        db_path,
        "SELECT condition_id, end_date_iso, game_start_time, question FROM market_expiry_cache WHERE fetched_at > ?",
        (cutoff,),
    )
    if rows:
        for r in rows:
            result[r["condition_id"]] = {
                "end_date_iso": r["end_date_iso"] or "",
                "game_start_time": r["game_start_time"] or "",
                "question": r["question"] or "",
            }
        log.debug(f"Expiry cache: {len(result)} markets loaded from DB")

    # Determine which CIDs still need fetching
    need_fetch = []
    if condition_ids:
        need_fetch = [cid for cid in condition_ids if cid not in result]
    if not need_fetch:
        log.info(f"Expiry: {len(result)} from cache, 0 to fetch")
        return result

    # Step 1: Bulk fetch from Gamma (fast, covers most markets).
    # Uses keyset pagination — `offset` was deprecated 2026-04-10.
    gamma_fetched: dict[str, dict[str, str]] = {}
    try:
        cursor = ""
        for _ in range(100):
            params = {"limit": 100, "closed": "false"}
            if cursor:
                params["next_cursor"] = cursor
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets/keyset",
                params=params, timeout=15,
            )
            if resp.status_code != 200:
                break
            data = resp.json()
            if not isinstance(data, dict):
                break
            batch = data.get("markets") or []
            if not batch:
                break
            for m in batch:
                cid = m.get("conditionId", "")
                end_date = m.get("endDateIso") or m.get("end_date_iso", "")
                question = m.get("question", "") or ""
                if cid and end_date:
                    gamma_fetched[cid] = {"end_date_iso": end_date, "question": question}
            next_cursor = data.get("next_cursor") or ""
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
    except Exception as e:
        log.debug(f"Gamma expiry fetch failed: {e}")

    # Apply Gamma results (no game_start_time available from Gamma)
    for cid in need_fetch:
        if cid in gamma_fetched:
            result[cid] = {
                "end_date_iso": gamma_fetched[cid]["end_date_iso"],
                "game_start_time": "",
                "question": gamma_fetched[cid]["question"],
            }

    # Step 2: CLOB fallback for markets still missing — CLOB exposes
    # game_start_time for sports markets (~73% of CLOB markets).
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
                    end_date = mkt.get("end_date_iso", "") or ""
                    game_start = mkt.get("game_start_time", "") or ""
                    question = mkt.get("question", "") or ""
                    if end_date or game_start:
                        result[cid] = {
                            "end_date_iso": end_date,
                            "game_start_time": game_start,
                            "question": question,
                        }
            except Exception:
                pass

    # Step 3: Write new results to cache
    new_entries = {cid: result[cid] for cid in need_fetch if cid in result}
    if new_entries:
        try:
            db = _connect_db(db_path)
            now = time.time()
            db.executemany(
                "INSERT OR REPLACE INTO market_expiry_cache "
                "(condition_id, end_date_iso, game_start_time, question, fetched_at) VALUES (?, ?, ?, ?, ?)",
                [(cid, v["end_date_iso"], v["game_start_time"], v.get("question", ""), now)
                 for cid, v in new_entries.items()],
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
    rows = _query_with_retry(db_path, "SELECT * FROM placement_feedback")
    if rows is None:
        return {}
    result: dict[str, dict] = {}
    for r in rows:
        cid = r["condition_id"]
        if cid not in result:
            result[cid] = {}
        result[cid][r["side"]] = {"status": r["status"], "reason": r["reason"], "ts": r["ts"]}
    return result


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
        rows = _query_with_retry(
            db_path,
            """SELECT condition_id, ts, net_score, fill_count, q_share_pct, action
               FROM market_performance
               WHERE ts > ?
               ORDER BY condition_id, ts""",
            (cutoff_ts,),
        )
        if rows is None:
            return result

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
    end_date_iso: str = ""         # market expiry / resolution deadline (from CLOB/Gamma)
    game_start_time: str = ""      # actual event start time (ISO 8601); sports only, CLOB-fetched
    min_size: float = 50.0         # minimum order size for rewards
    max_spread: float = 0.045      # maximum spread for rewards
    question_group: str = ""       # grouping key for portfolio concentration limits
    # Regime detection fields
    avg_bid: float = 0.0           # average bid price (from reward_tracker)
    avg_ask: float = 0.0           # average ask price (from reward_tracker)
    adverse_fills: int = 0         # fills where we lost to adverse selection
    reward_window_pct: float = 0.0 # fraction of cycles in reward spread window
    total_market_q: float = 0.0    # total market Q-score (competition depth)
    # Recent prices (from cycle_snapshots, last few hours — fresher than lifetime avg)
    recent_bid: float = 0.0        # median best_bid over recent cycles
    recent_ask: float = 0.0        # median best_ask over recent cycles


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
    """Fetch actual REWARD payouts from Data API for correction factor computation.

    Only uses REWARD type for correction factor — NOT MAKER_REBATE.
    MAKER_REBATE is earned on ALL trades including damage-control unwinds,
    so including it would inflate the correction factor with revenue that
    comes from losing trades (double-counting: loss in fill_damage,
    partial recovery in rebate). MAKER_REBATE is still tracked separately
    in Phase 0 daily attribution for total income reporting.

    Returns:
        Total REWARD amount paid in the lookback window (raw, not a factor).
        Returns 0.0 if no data available.
    """
    import requests

    # Use FUNDER address (that's where rewards are paid)
    funder = os.getenv("FUNDER", "")
    if not funder:
        # Fall back to WALLET_ADDRESS
        funder = os.getenv("WALLET_ADDRESS", "")
    if not funder:
        log.debug("No FUNDER or WALLET_ADDRESS — cannot compute correction factor")
        return 0.0

    try:
        cutoff_ts = time.time() - hours * 3600
        reward_total = 0.0
        rebate_total = 0.0
        limit = 500
        reward_count = 0
        rebate_count = 0

        # Fetch REWARD and MAKER_REBATE separately for clean accounting
        for payout_type in ("REWARD", "MAKER_REBATE"):
            type_offset = 0
            while True:
                resp = requests.get(
                    "https://data-api.polymarket.com/activity",
                    params={
                        "user": funder,
                        "type": payout_type,
                        "limit": limit,
                        "offset": type_offset,
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
                        if payout_type == "REWARD":
                            reward_total += amount
                            reward_count += 1
                        else:
                            rebate_total += amount
                            rebate_count += 1

                if len(data) < limit:
                    break
                type_offset += limit
                time.sleep(0.2)

        if reward_total > 0 or rebate_total > 0:
            log.info(
                f"Payouts ({hours:.0f}h): REWARD=${reward_total:.2f} ({reward_count}), "
                f"MAKER_REBATE=${rebate_total:.2f} ({rebate_count}), "
                f"combined=${reward_total + rebate_total:.2f}"
            )
        else:
            log.debug(f"No reward payouts found in {hours:.0f}h window")

        # Return ONLY reward total for correction factor computation.
        # MAKER_REBATE is excluded to prevent inflation from unwind rebates.
        return reward_total

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
        db = _connect_db(db_path)

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
    rows = _query_with_retry(
        db_path,
        """SELECT condition_id,
                  SUM(shares * clob_cost) as cost,
                  COUNT(*) as cnt,
                  SUM(shares) as total_shares
           FROM fills WHERE ts > ?
           GROUP BY condition_id""",
        (cutoff,),
    )
    if rows is None:
        return {}
    return {
        r["condition_id"]: {
            "cost": r["cost"] or 0,
            "count": r["cnt"] or 0,
            "shares": r["total_shares"] or 0,
        }
        for r in rows
    }


def query_dump_revenue(db_path: str, hours: float = 24) -> dict[str, dict]:
    """Query unwinds table for per-market dump revenue in recent window.

    Returns {condition_id: {"revenue": float, "pnl": float, "count": int}}.
    """
    cutoff = time.time() - hours * 3600
    rows = _query_with_retry(
        db_path,
        """SELECT condition_id,
                  SUM(usd_value) as revenue,
                  SUM(pnl) as total_pnl,
                  COUNT(*) as cnt
           FROM unwinds WHERE ts > ?
           GROUP BY condition_id""",
        (cutoff,),
    )
    if rows is None:
        return {}
    return {
        r["condition_id"]: {
            "revenue": r["revenue"] or 0,
            "pnl": r["total_pnl"] or 0,
            "count": r["cnt"] or 0,
        }
        for r in rows
    }


def query_positions(db_path: str) -> dict[str, dict]:
    """Query positions table for current open exposure.

    Returns {condition_id: {"yes_usd": float, "no_usd": float, "total": float, "question": str}}.
    """
    rows = _query_with_retry(
        db_path,
        "SELECT * FROM positions WHERE yes_shares > 0.5 OR no_shares > 0.5",
    )
    if rows is None:
        return {}
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


def _detect_book_depth_changes(db_path: str) -> set[str]:
    """Issue 10: Detect markets with rapid book depth changes.

    Returns set of condition_ids where depth changed >3x between the
    latest two book_snapshots. These markets need a shorter scoring
    window because a competitor may have just entered or exited.
    """
    rows = _query_with_retry(
        db_path,
        """WITH ranked AS (
            SELECT condition_id, bid_depth_5c, ask_depth_5c, ts,
                   ROW_NUMBER() OVER (PARTITION BY condition_id ORDER BY ts DESC) as rn
            FROM book_snapshots
            WHERE ts > ?
        )
        SELECT a.condition_id,
               a.bid_depth_5c as bid_new, a.ask_depth_5c as ask_new,
               b.bid_depth_5c as bid_old, b.ask_depth_5c as ask_old
        FROM ranked a JOIN ranked b
            ON a.condition_id = b.condition_id
        WHERE a.rn = 1 AND b.rn = 2""",
        (time.time() - 3600,),
    )
    if rows is None:
        return set()

    try:
        changed = set()
        for r in rows:
            old_depth = (r["bid_old"] or 0) + (r["ask_old"] or 0)
            new_depth = (r["bid_new"] or 0) + (r["ask_new"] or 0)
            if old_depth > 10 and new_depth > 10:
                ratio = new_depth / old_depth if old_depth > 0 else 1.0
                if ratio > 3.0 or ratio < 0.33:
                    changed.add(r["condition_id"])
        if changed:
            log.info(f"Book depth shift detected on {len(changed)} markets (>3x change)")
        return changed
    except Exception:
        return set()


def _query_windowed_scoring(db_path: str, window_hours: float = 4.0) -> dict[str, dict]:
    """Compute q_share from scoring_snapshots (Phase 0 data) over a recent window.

    Returns {condition_id: {"scoring_ratio": float, "samples": int}}.

    scoring_ratio = count(scoring=True) / count(total) for each market.
    This measures what fraction of the time our orders are actually scoring,
    which is an upper bound on our Q-share. Unlike the cumulative
    total_q_score / total_market_q from reward_tracker, this decays
    naturally as the window slides — no stale 1.0 values from Day 1.

    Issue 10: Markets with rapid book depth changes get a 1h window
    instead of 4h, so q_share reflects the new competitive landscape
    within minutes instead of hours.
    """
    # Detect markets needing short window
    depth_changed = _detect_book_depth_changes(db_path)

    cutoff_normal = time.time() - window_hours * 3600
    cutoff_fast = time.time() - 1.0 * 3600  # 1h for fast-react markets

    try:
        db = _connect_db(db_path)

        # Fetch ALL scoring data in the wider window
        rows = db.execute(
            """SELECT condition_id, ts,
                      COUNT(*) as total,
                      SUM(CASE WHEN scoring = 1 THEN 1 ELSE 0 END) as scoring_count
               FROM scoring_snapshots
               WHERE ts > ?
               GROUP BY condition_id""",
            (cutoff_normal,),
        ).fetchall()

        # Also fetch narrow window for fast-react markets
        fast_rows = {}
        if depth_changed:
            for r in db.execute(
                """SELECT condition_id,
                          COUNT(*) as total,
                          SUM(CASE WHEN scoring = 1 THEN 1 ELSE 0 END) as scoring_count
                   FROM scoring_snapshots
                   WHERE ts > ?
                   GROUP BY condition_id""",
                (cutoff_fast,),
            ).fetchall():
                if r["condition_id"] in depth_changed:
                    fast_rows[r["condition_id"]] = r

        db.close()

        result = {}
        fast_used = 0
        for r in rows:
            cid = r["condition_id"]
            # Issue 10: prefer narrow window for depth-changed markets
            if cid in fast_rows and fast_rows[cid]["total"] >= 2:
                fr = fast_rows[cid]
                total = fr["total"]
                scoring = fr["scoring_count"]
                fast_used += 1
            else:
                total = r["total"]
                scoring = r["scoring_count"]

            if total >= 2:
                result[cid] = {
                    "scoring_ratio": scoring / total,
                    "samples": total,
                }

        if result:
            log.debug(
                f"Windowed scoring: {len(result)} markets "
                f"({fast_used} using 1h fast window, rest {window_hours}h)"
            )
        return result
    except Exception as e:
        log.debug(f"Windowed scoring query failed: {e}")
        return {}


def query_reward_stats(db_path: str) -> dict[str, dict]:
    """Query reward_market_stats for Q-score, on-book data, and regime signals.

    Q-share computation strategy (prioritized):
    1. Use windowed scoring_ratio from scoring_snapshots (Phase 0, last 4h)
       — this naturally decays, preventing stale 1.0 values
    2. Fall back to cumulative total_q_score / total_market_q
       — but cap at 0.5 if on_book > 4h (stale cumulative data is suspect)
    3. Default to 0.0 for unknown markets

    Returns {condition_id: {rate, q_share, on_book_hrs, question, fills,
    cycles_with_orders, total_cycles, avg_bid, avg_ask, adverse_fills,
    spread_capture_usd, cycles_in_reward_window, cycles_both_in_window,
    total_market_q}}.
    """
    # Get windowed scoring data first (preferred source for q_share)
    windowed = _query_windowed_scoring(db_path, window_hours=4.0)

    # GAP 3: Get last-seen timestamps for stale market decay
    last_seen_map: dict[str, float] = {}
    _ls_rows = _query_with_retry(
        db_path,
        "SELECT condition_id, MAX(ts) as last_ts FROM scoring_snapshots GROUP BY condition_id",
    )
    if _ls_rows:
        for r in _ls_rows:
            last_seen_map[r["condition_id"]] = r["last_ts"]
    now_ts = time.time()

    try:
        rows = _query_with_retry(db_path, "SELECT * FROM reward_market_stats")
        if rows is None:
            return {}
        result = {}
        windowed_used = 0
        cumulative_capped = 0
        stale_decayed = 0
        stale_excluded = 0
        prior_used = 0
        poisoned_skipped = 0
        for r in rows:
            d = json.loads(r["data"])
            cid = d["condition_id"]
            total_market_q = d.get("total_market_q", 0)
            on_book = d.get("time_on_book_secs", 0) / 3600

            # GAP 3: Stale market decay
            # Markets not seen in >24h are excluded entirely (q_share=0, marked stale).
            # Markets not seen in >6h get q_share forced to 0 (data too old to trust).
            last_seen = last_seen_map.get(cid, 0)
            hours_since_seen = (now_ts - last_seen) / 3600 if last_seen > 0 else float("inf")

            if hours_since_seen > 24 and on_book > 1:
                # Completely stale — exclude from all calculations
                q_share = 0.0
                stale_excluded += 1
                result[cid] = {
                    "rate": d.get("daily_rate", 0),
                    "q_share": 0.0,
                    "on_book_hrs": on_book,
                    "question": d.get("question", ""),
                    "fills": d.get("buy_fills", 0),
                    "cycles_with_orders": d.get("cycles_with_orders", 0),
                    "total_cycles": d.get("total_cycles", 0),
                    "avg_bid": d.get("avg_bid_price", 0),
                    "avg_ask": d.get("avg_ask_price", 0),
                    "adverse_fills": d.get("adverse_fills", 0),
                    "spread_capture_usd": d.get("spread_capture_usd", 0),
                    "cycles_in_reward_window": d.get("cycles_in_reward_window", 0),
                    "cycles_both_in_window": d.get("cycles_both_in_window", 0),
                    "total_market_q": total_market_q,
                    "_stale": True,
                }
                continue

            if hours_since_seen > 6 and on_book > 1:
                # Moderately stale — force q_share to 0 (no trust in old data)
                q_share = 0.0
                stale_decayed += 1
            else:
                # Priority 1: windowed scoring data from Phase 0
                ws = windowed.get(cid)
                if ws and ws["samples"] >= 3:
                    q_share = min(ws["scoring_ratio"] * 0.5, 0.5)
                    windowed_used += 1
                elif total_market_q > 0 and d.get("q_score_samples", 0) > 0:
                    # Priority 2: cumulative from reward_tracker, with
                    # poisoned-row guard.
                    raw_cumulative = d["total_q_score"] / total_market_q
                    if raw_cumulative > RF_POISONED_Q_SHARE_THRESHOLD:
                        # Legacy poisoned row (see memory file:
                        # project_market_q_fallback_bug.md). Cumulative totals
                        # are contaminated by q_share=1.0 saturation from the
                        # pre-Option-B max(market_q, our_q) fallback. Treat as
                        # cold-start so Priority 1 (windowed) can take over
                        # once samples accumulate; self-heals when dilution
                        # drops the ratio under the threshold.
                        q_share = RF_NEW_MARKET_Q_SHARE_PRIOR
                        poisoned_skipped += 1
                    else:
                        q_share = raw_cumulative
                        if on_book > 4.0 and q_share > 0.5:
                            q_share = 0.5
                            cumulative_capped += 1
                elif on_book < 2.0 and d.get("q_score_samples", 0) == 0:
                    # Priority 3: cold-start prior.
                    # Markets with no posting history (< 2h on book, zero scoring
                    # samples) get a conservative prior instead of 0.0. This
                    # escapes the cold-start trap where score=0 would classify
                    # every unknown market as a "trial" and cap discovery at
                    # RF_MAX_TRIAL_MARKETS. Discovery churn is still limited via
                    # the confidence-based trial cap in market_scorer.rank_markets().
                    q_share = RF_NEW_MARKET_Q_SHARE_PRIOR
                    prior_used += 1
                else:
                    q_share = 0.0  # stale/broken — explicit fallthrough

            result[cid] = {
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
        if windowed_used or stale_decayed or stale_excluded or prior_used or poisoned_skipped:
            log.info(
                f"Q-share: {windowed_used} windowed, {cumulative_capped} cumulative capped, "
                f"{prior_used} cold-start prior, {poisoned_skipped} poisoned skipped, "
                f"{stale_decayed} decayed (>6h), {stale_excluded} excluded (>24h)"
            )
        return result
    except Exception as e:
        log.warning(f"Reward stats query failed: {e}")
        return {}


def query_recent_prices(db_path: str, lookback_hours: float = 3.0) -> dict[str, dict]:
    """Query cycle_snapshots for recent best_bid/best_ask per market.

    Returns {condition_id: {"recent_bid": float, "recent_ask": float, "samples": int}}.
    Uses median of last N hours to resist outlier cycles.
    """
    import statistics

    cutoff = time.time() - lookback_hours * 3600
    rows = _query_with_retry(
        db_path,
        "SELECT condition_id, best_bid, best_ask FROM cycle_snapshots "
        "WHERE ts >= ? AND best_bid IS NOT NULL AND best_bid > 0 "
        "AND best_ask IS NOT NULL AND best_ask > 0",
        (cutoff,),
    )
    if rows is None:
        return {}

    # Group by condition_id
    by_cid: dict[str, list[tuple[float, float]]] = {}
    for cid, bid, ask in rows:
        by_cid.setdefault(cid, []).append((bid, ask))

    result = {}
    for cid, prices in by_cid.items():
        bids = [p[0] for p in prices]
        asks = [p[1] for p in prices]
        result[cid] = {
            "recent_bid": statistics.median(bids),
            "recent_ask": statistics.median(asks),
            "samples": len(prices),
        }
    return result


def compute_available_capital(
    db_path: str,
    total_capital: float | None = None,
    exchange_balance: float | None = None,
) -> float:
    """Compute available capital for new order deployment.

    When ``exchange_balance`` is provided (real USDC balance from the
    exchange, written to DB by the bot every ~5 min, FX-013), the
    function uses it directly as the available capital — the exchange
    already accounts for pending orders. Positions and dumps are still
    logged for transparency and used to reconstruct total portfolio
    value.

    When ``exchange_balance`` is None, falls back to the legacy path:
    ``total_capital`` minus DB-derived locked items. ``total_capital``
    is itself None by default (FX-025); callers that reach this branch
    with both inputs None get ``0.0`` back. The agent's outer flow
    short-circuits with `[CAPITAL_SOURCE] source=none` before reaching
    this function in that case, so the 0.0 return is a defensive floor
    rather than the primary path.

    Returns actual deployable capital (never negative, floors at 0).
    """
    positions = query_positions(db_path)
    locked_positions = sum(p["total"] for p in positions.values())

    locked_dumps = 0.0
    locked_pending = 0.0
    try:
        db = _connect_db(db_path)

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

    # ── Exchange-balance path (preferred) ──
    if exchange_balance is not None:
        # The exchange USDC balance already reflects pending orders
        # (exchange deducts collateral when an order is placed).
        # Available = free USDC on exchange.
        available = exchange_balance
        # Reconstruct total portfolio value for logging.
        total_portfolio = exchange_balance + locked_positions + locked_dumps + locked_pending
        log.info(
            f"Capital (exchange): ${exchange_balance:.0f} USDC + "
            f"${locked_positions:.0f} positions + ${locked_dumps:.0f} dumps "
            f"+ ${locked_pending:.0f} orders = ${total_portfolio:.0f} portfolio | "
            f"${available:.0f} available"
        )
        return max(0, available)

    # ── Legacy path: hardcoded total minus DB-derived locked ──
    # FX-025: total_capital can be None now. The agent's outer flow
    # short-circuits before reaching this branch in that case, but we
    # add a defensive floor so the function returns a sensible 0.0
    # rather than raising on None arithmetic.
    if total_capital is None:
        log.warning(
            "compute_available_capital: total_capital is None and "
            "exchange_balance is None — returning 0.0. "
            "The agent's outer flow should have short-circuited earlier."
        )
        return 0.0

    locked = locked_positions + locked_dumps + locked_pending

    # Sanity check: if locked capital exceeds total by a wide margin,
    # the active_orders table likely has stale records (the bot purges
    # them on startup, but if it hasn't restarted, ghosts accumulate).
    # In this case, only trust positions (exchange-verified) + dumps,
    # not the potentially-stale pending orders.
    if locked_pending > total_capital * 2:
        log.warning(
            f"Capital: pending orders (${locked_pending:.0f}) exceed 2× budget "
            f"(${total_capital:.0f}) — likely stale DB records. "
            f"Ignoring pending orders for capital calculation."
        )
        locked = locked_positions + locked_dumps

    available = max(0, total_capital - locked)
    log.info(
        f"Capital (hardcoded): ${total_capital:.0f} total - ${locked_positions:.0f} positions "
        f"- ${locked_dumps:.0f} dumps - ${locked_pending:.0f} pending = ${available:.0f} available"
    )
    return available


def _smooth_correction_factor(
    raw_factor: float,
    db_path: str,
    alpha: float = 0.3,
    has_new_observation: bool = True,
    estimated_daily: float = 0.0,
    actual_daily: float = 0.0,
    deployed_count: int = 0,
) -> float:
    """EMA-smooth the correction factor using DB-persisted history.

    Stores each raw observation with timestamp and context (estimated/actual
    daily totals, deployed market count) for post-hoc debugging.

    Circuit breaker: if raw_factor < 0.01 (estimates > 100x reality),
    skip smoothing and use raw directly — the model is broken and EMA
    would take 10+ cycles to converge, losing money the entire time.

    If no new observation this cycle, returns the last stored EMA (or 1.0).
    """
    try:
        db = _connect_db(db_path)
        db.execute(
            """CREATE TABLE IF NOT EXISTS correction_factor_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              REAL    NOT NULL,
                raw             REAL    NOT NULL,
                smoothed        REAL    NOT NULL,
                estimated_daily REAL    NOT NULL DEFAULT 0,
                actual_daily    REAL    NOT NULL DEFAULT 0,
                deployed_count  INTEGER NOT NULL DEFAULT 0
            )"""
        )
        # Migrate old tables missing new columns
        for col, typedef in [
            ("estimated_daily", "REAL NOT NULL DEFAULT 0"),
            ("actual_daily", "REAL NOT NULL DEFAULT 0"),
            ("deployed_count", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            try:
                db.execute(f"ALTER TABLE correction_factor_history ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # column already exists

        # Read last smoothed value
        row = db.execute(
            "SELECT smoothed FROM correction_factor_history ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        prev_smoothed = row[0] if row else 1.0

        if has_new_observation:
            # Circuit breaker: if raw is extremely low, the model is broken.
            # Don't smooth — jump directly to reality so the system reacts
            # within 1 cycle instead of 10+.
            if raw_factor < 0.01:
                log.warning(
                    f"CIRCUIT BREAKER: raw correction factor {raw_factor:.6f} "
                    f"< 0.01 (estimates >100x reality). Skipping EMA, using raw."
                )
                smoothed = raw_factor
            elif raw_factor < 0.05 and prev_smoothed > 0.2:
                # Fast convergence mode: raw says severe miscalibration but
                # EMA is still high from history. Use alpha=0.7 (fast adapt).
                smoothed = 0.7 * raw_factor + 0.3 * prev_smoothed
                log.warning(
                    f"FAST ADAPT: raw={raw_factor:.4f} << prev={prev_smoothed:.4f}. "
                    f"Using alpha=0.7 → smoothed={smoothed:.4f}"
                )
            else:
                # Normal EMA: new_smoothed = alpha * raw + (1 - alpha) * prev_smoothed
                smoothed = alpha * raw_factor + (1 - alpha) * prev_smoothed

            # Clamp to [1e-6, 10.0]. No consumer divides by CF (audited
            # 2026-04-20); lower bound preserves "effectively nonzero"
            # semantics for downstream multiplicands without masking
            # catastrophic calibration errors. Legacy floors: 0.10
            # pre-d792156, 0.001 post-d792156.
            smoothed = max(1e-6, min(10.0, smoothed))

            db.execute(
                "INSERT INTO correction_factor_history "
                "(ts, raw, smoothed, estimated_daily, actual_daily, deployed_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), raw_factor, smoothed,
                 estimated_daily, actual_daily, deployed_count),
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

            if abs(smoothed - raw_factor) > 0.01:
                log.info(
                    f"Correction factor smoothed: raw={raw_factor:.4f} → "
                    f"EMA={smoothed:.4f} (prev={prev_smoothed:.4f}, α={alpha})"
                )
        else:
            smoothed = prev_smoothed
            if smoothed != 1.0:
                log.info(f"No new payout data — using last smoothed factor={smoothed:.4f}")

        db.close()
        return smoothed

    except Exception as e:
        log.debug(f"Correction factor smoothing failed: {e}")
        return raw_factor if has_new_observation else 1.0


def persist_deployed_cids(
    db_path: str,
    deployed_cids: set[str],
    probe_cids: set[str] | None = None,
) -> None:
    """Write the current deployed market set to DB.

    Called by the agent after allocation so subsequent CF calculations
    can read from DB instead of parsing market_allocations.json.
    """
    try:
        db = _connect_db(db_path)
        db.execute(
            """CREATE TABLE IF NOT EXISTS deployed_markets (
                condition_id TEXT PRIMARY KEY,
                ts           REAL NOT NULL,
                is_probe     INTEGER NOT NULL DEFAULT 0
            )"""
        )
        db.execute("DELETE FROM deployed_markets")
        now = time.time()
        probe_cids = probe_cids or set()
        db.executemany(
            "INSERT INTO deployed_markets (condition_id, ts, is_probe) VALUES (?, ?, ?)",
            [(cid, now, 1 if cid in probe_cids else 0) for cid in deployed_cids],
        )
        db.commit()
        db.close()
    except Exception as e:
        log.debug(f"Failed to persist deployed CIDs: {e}")


def _load_deployed_cids(db_path: str) -> tuple[set[str], set[str]]:
    """Load deployed market CIDs from DB, excluding probe markets.

    Returns (deployed_cids, probe_cids).
    deployed_cids excludes probes — use for CF denominator.
    probe_cids are UNSAFE-state data-collection-only markets.

    Falls back to active_orders table, then empty set.
    """
    # Primary: DB table written by persist_deployed_cids
    rows = _query_with_retry(
        db_path,
        "SELECT condition_id, is_probe FROM deployed_markets WHERE ts > ?",
        (time.time() - 7200,),  # stale after 2h
    )
    if rows:
        deployed = {r[0] for r in rows if not r[1]}
        probes = {r[0] for r in rows if r[1]}
        if deployed or probes:
            return deployed, probes

    # Fallback: active_orders (no probe distinction)
    rows = _query_with_retry(db_path, "SELECT DISTINCT condition_id FROM active_orders")
    if rows:
        return {r[0] for r in rows}, set()

    return set(), set()


def collect_all(
    db_path: str = "bot_history.db",
    hours: float = 24,
) -> tuple[list, float, float, float, float]:
    """Main entry. Cross-references all data sources into per-market metrics.

    Args:
        db_path: Path to bot_history.db
        hours: Lookback window for fill/dump data

    Returns:
        Tuple of (metrics, correction_factor, clob_rate_delta, data_completeness,
        actual_daily_total).
        correction_factor: actual_daily / estimated_daily. Use to scale Q-score estimates.
        clob_rate_delta: % change in total CLOB rates vs cached (forward-looking).
        data_completeness: fraction of expected markets returned (0.0–1.0+).
        actual_daily_total: raw REWARD payout from Data API (avoids re-fetching).
    """
    # Gather from all sources
    actual_rewards = fetch_actual_rewards()  # empty (per-market not available)
    actual_daily_total = fetch_reward_correction_factor(hours)
    fills = query_fill_costs(db_path, hours)
    dumps = query_dump_revenue(db_path, hours)
    positions = query_positions(db_path)
    stats = query_reward_stats(db_path)
    recent_prices = query_recent_prices(db_path)

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
            # Cold-start prior: CLOB-discovered markets we've never posted on
            # get a conservative q_share prior instead of 0.0, so they produce
            # score > 0 at discovery. The trial cap in market_scorer still
            # throttles how many of these deploy per cycle.
            stats[cid] = {
                "rate": mkt_data["daily_rate"],
                "q_share": RF_NEW_MARKET_Q_SHARE_PRIOR,
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

        question = (
            stat_data.get("question")
            or pos_data.get("question", "")
            or expiry_map.get(cid, {}).get("question", "")
        )
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
            end_date_iso=expiry_map.get(cid, {}).get("end_date_iso", ""),
            game_start_time=expiry_map.get(cid, {}).get("game_start_time", ""),
            min_size=clob_reward_markets.get(cid, {}).get("min_size", 50.0),
            max_spread=clob_reward_markets.get(cid, {}).get("max_spread", 0.045),
            question_group=_question_group_key(question) if question else "",
            # Regime detection
            avg_bid=stat_data.get("avg_bid", 0),
            avg_ask=stat_data.get("avg_ask", 0),
            adverse_fills=stat_data.get("adverse_fills", 0),
            reward_window_pct=reward_window_pct,
            total_market_q=stat_data.get("total_market_q", 0),
            recent_bid=recent_prices.get(cid, {}).get("recent_bid", 0.0),
            recent_ask=recent_prices.get(cid, {}).get("recent_ask", 0.0),
        ))

    # ── GAP 1 FIX: Portfolio-only correction factor ──
    # The correction factor must compare actual payouts against DEPLOYED
    # markets only — not the entire tracked universe. Probe markets
    # (UNSAFE state, data-collection only) are excluded because they
    # don't earn meaningful rewards.
    #
    # Load deployed CIDs from DB (persisted by agent after each cycle).
    deployed_cids, probe_cids = _load_deployed_cids(db_path)
    deployed_count = len(deployed_cids)

    if deployed_cids:
        # Portfolio calibration: only non-probe deployed markets contribute
        estimated_daily_total = sum(
            m.daily_rate * m.q_share_pct
            for m in metrics
            if m.q_share_pct > 0 and m.condition_id in deployed_cids
        )
        log.info(
            f"Portfolio CF denominator: {deployed_count} deployed markets "
            f"({len(probe_cids)} probes excluded), est=${estimated_daily_total:.2f}/d"
        )
    else:
        # No previous allocation — use all markets with on_book > 0
        # (first run or after DB reset). This is the "exploration" estimate.
        estimated_daily_total = sum(
            m.daily_rate * m.q_share_pct
            for m in metrics
            if m.q_share_pct > 0 and m.on_book_hours > 0
        )
        log.info(
            f"No deployed market set — using {sum(1 for m in metrics if m.q_share_pct > 0 and m.on_book_hours > 0)} "
            f"on-book markets for CF, est=${estimated_daily_total:.2f}/d"
        )

    # Minimum denominator guard: when estimated_daily_total is tiny
    # (< $0.50/day), the ratio becomes noise-dominated. A single
    # $0.10 payout swing causes 20%+ CF swings. Skip CF update.
    MIN_EST_DAILY = 0.50
    raw_factor = 1.0
    est_actual_ratio = 0.0
    has_new_cf = False
    if actual_daily_total > 0 and estimated_daily_total >= MIN_EST_DAILY:
        actual_per_day = actual_daily_total / max(hours / 24, 0.1)
        raw_factor = actual_per_day / estimated_daily_total
        est_actual_ratio = estimated_daily_total / actual_per_day
        raw_factor = max(0.001, min(10.0, raw_factor))
        has_new_cf = True
        log.info(
            f"Reward correction: actual=${actual_per_day:.2f}/d vs "
            f"estimated=${estimated_daily_total:.2f}/d → raw_factor={raw_factor:.4f} "
            f"(est/actual ratio={est_actual_ratio:.1f}x, "
            f"based on {deployed_count} deployed markets)"
        )
        if est_actual_ratio > 10:
            log.warning(
                f"MISCALIBRATION: estimates {est_actual_ratio:.0f}x higher than actual "
                f"(deployed portfolio only)."
            )
    elif actual_daily_total > 0 and estimated_daily_total < MIN_EST_DAILY:
        log.info(
            f"Reward correction: est=${estimated_daily_total:.2f}/d < "
            f"${MIN_EST_DAILY}/d minimum — skipping CF update (noise-dominated)"
        )
    elif actual_daily_total > 0 and deployed_count < 3:
        log.info(
            f"Reward correction: actual=${actual_daily_total:.2f} but <3 deployed "
            f"markets — skipping ratio (insufficient signal)"
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
        has_new_observation=has_new_cf,
        estimated_daily=estimated_daily_total,
        actual_daily=actual_daily_total,
        deployed_count=deployed_count,
    )

    # Issue 2+9: Compute forward-looking rate delta and data completeness
    clob_rate_delta = compute_clob_rate_delta(clob_reward_markets, db_path)
    data_completeness = 1.0
    if len(clob_reward_markets) > 0:
        # Compare against expected: if we got <80% of what cache had, data is partial
        _row = _query_with_retry(
            db_path, "SELECT COUNT(*) FROM reward_markets_cache", fetch="one",
        )
        if _row and _row[0]:
            data_completeness = len(clob_reward_markets) / _row[0]
    elif len(stats) == 0:
        # Both CLOB and stats empty — total data failure
        data_completeness = 0.0

    log.info(
        f"Collected metrics for {len(metrics)} markets | "
        f"fills={len(fills)} positions={len(positions)} correction={correction_factor:.4f} | "
        f"clob_rate_delta={clob_rate_delta:+.1%} data_completeness={data_completeness:.0%}"
    )
    return metrics, correction_factor, clob_rate_delta, data_completeness, actual_daily_total
