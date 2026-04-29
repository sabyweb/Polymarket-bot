"""
Paper Trading Simulator — clean architecture rewrite.

Runs multiple strategies against LIVE Polymarket order books without
risking real capital.  Each strategy gets its own isolated:
  - PaperClient  (simulated balance + order book)
  - BotDatabase  (paper_<name>.db)
  - RewardTracker (Q-score → reward estimation)

Architecture:
  1. Fetch markets ONCE per refresh (two lists: standard + wide-filter)
  2. Each strategy defines a `run_cycle` callable
  3. Main loop calls each strategy's cycle, then runs hourly bookkeeping
  4. DB is swapped per-session ONLY around DB writes — never globally

Usage:
    python paper_trader.py --duration 6h --balance 3300
"""

import argparse
import json
import logging
import os
import signal
import sys
import time

log = logging.getLogger("paper_trader")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# ═══════════════════════════════════════════════════════════════════════
# 1. DATA TYPES
# ═══════════════════════════════════════════════════════════════════════

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Strategy:
    """Defines a paper trading strategy."""
    name: str
    run_cycle: Callable  # (session, markets, book_cache) -> None
    config_overrides: dict = field(default_factory=dict)
    use_wide_markets: bool = False  # True = skip default hygiene, apply own filter


@dataclass
class Session:
    """Isolated runtime state for one strategy."""
    strategy: Strategy
    paper_client: object  # PaperClient
    db: object            # BotDatabase
    reward_tracker: object  # RewardTracker
    cycle_count: int = 0
    start_time: float = 0.0
    last_hourly_ts: float = 0.0
    state: dict = field(default_factory=dict)  # strategy-specific mutable state


# ═══════════════════════════════════════════════════════════════════════
# 2. STRATEGY IMPLEMENTATIONS
# ═══════════════════════════════════════════════════════════════════════

def _book_to_dict(book) -> dict | None:
    """Convert OrderBookSummary to dict expected by reward_tracker."""
    if not book or not hasattr(book, "bids"):
        return None
    return {
        "bids": [{"price": float(b.price), "size": float(b.size)} for b in book.bids],
        "asks": [{"price": float(a.price), "size": float(a.size)} for a in book.asks],
    }


def _record_qscore(session, market, book_cache, bid_price, ask_price, bid_size, ask_size):
    """Record one cycle of Q-score data for a market."""
    cid = market["condition_id"]
    tokens = market.get("token_ids", [])
    if len(tokens) < 2:
        return

    session.reward_tracker.get_or_create(
        condition_id=cid,
        question=market.get("question", ""),
        daily_rate=market.get("daily_rate", 0),
        max_spread=market.get("max_spread", 0),
    )

    # Get order book for market-Q estimation
    book_dict = None
    try:
        yes_book = book_cache.get_order_book(tokens[0])
        book_dict = _book_to_dict(yes_book)
        if book_dict and book_dict["bids"] and book_dict["asks"]:
            midpoint = (float(book_dict["bids"][0]["price"]) + float(book_dict["asks"][0]["price"])) / 2
        else:
            midpoint = market.get("yes_price") or 0.5
    except Exception:
        midpoint = market.get("yes_price") or 0.5

    has_yes = bid_price > 0
    has_no = ask_price > 0

    session.reward_tracker.record_cycle(
        condition_id=cid,
        has_yes_order=has_yes, has_no_order=has_no,
        bid_price=bid_price, ask_price=ask_price,
        inventory_usd=0.0,
        cooldown_active=False, skew_active=False,
        cycle_duration_secs=30.0,
        midpoint=midpoint,
        bid_size=bid_size, ask_size=ask_size,
        order_book=book_dict,
    )


def _detect_fills_and_handle(session, market, book_cache):
    """Check for fills on a market's orders, merge or dump inventory.

    Returns (fills_count, dump_pnl).
    """
    pc = session.paper_client
    cid = market["condition_id"]
    tokens = market.get("token_ids", [])
    if len(tokens) < 2:
        return 0, 0.0

    yes_tid, no_tid = tokens[0], tokens[1]
    orders = session.state.setdefault("orders", {}).setdefault(cid, {})
    inv = session.state.setdefault("inventory", {}).setdefault(
        cid, {"yes": 0.0, "no": 0.0, "yes_cost": 0.0, "no_cost": 0.0}
    )
    fills = 0
    dump_pnl = 0.0

    # ── Detect fills ───────────────────────────────────────────────
    for side_key, side_name, tid in [("yes_oid", "yes", yes_tid), ("no_oid", "no", no_tid)]:
        oid = orders.get(side_key)
        if not oid:
            continue
        status = pc._paper_get_order(oid)
        if status.get("status") != "MATCHED":
            continue
        matched = float(status.get("size_matched", 0))
        fill_price = float(status.get("price", 0))
        if matched < 1.0:
            orders[side_key] = None
            continue

        inv[side_name] += matched
        inv[f"{side_name}_cost"] += matched * fill_price
        fills += 1

        log.info(
            f"[{session.strategy.name}] FILL | {side_name.upper()} {matched:.0f} sh "
            f"@ {fill_price:.4f} | {market['question'][:35]}"
        )
        _db_write(session, "log_fill",
            condition_id=cid, question=market.get("question", ""),
            side=side_name, fill_type="FULL",
            shares=matched, price=fill_price,
            clob_cost=fill_price, usd_value=matched * fill_price,
        )
        orders[side_key] = None

    # ── Merge if both sides have inventory ─────────────────────────
    merge_qty = min(inv["yes"], inv["no"])
    if merge_qty >= 1.0:
        result = pc.merge_positions(cid, merge_qty)
        if isinstance(result, dict) and result.get("success"):
            yc = (inv["yes_cost"] / inv["yes"]) * merge_qty if inv["yes"] > 0 else 0
            nc = (inv["no_cost"] / inv["no"]) * merge_qty if inv["no"] > 0 else 0
            pnl = merge_qty - yc - nc
            inv["yes"] -= merge_qty
            inv["no"] -= merge_qty
            inv["yes_cost"] -= yc
            inv["no_cost"] -= nc
            dump_pnl += pnl
            log.info(f"[{session.strategy.name}] MERGE | {merge_qty:.0f} pairs | pnl=${pnl:+.2f}")
            _db_write(session, "log_unwind",
                condition_id=cid, question=market.get("question", ""),
                side="merge", shares=merge_qty,
                sell_price=1.0, usd_value=merge_qty,
                vwap_cost=yc + nc,
            )

    # ── Dump remaining single-side inventory at market ─────────────
    for side_name, tid in [("yes", yes_tid), ("no", no_tid)]:
        remaining = inv[side_name]
        if remaining < 1.0:
            continue
        other = "no" if side_name == "yes" else "yes"
        if inv[other] >= 1.0:
            continue  # wait for merge

        try:
            book = book_cache.get_order_book(tid)
            if not book or not book.bids:
                continue
            dump_price = float(book.bids[0].price)

            available = pc._token_balances.get(tid, 0.0)
            dump_size = min(remaining, available)
            if dump_size < 1.0:
                inv[side_name] = 0.0
                inv[f"{side_name}_cost"] = 0.0
                continue

            # Instant dump (no order placement — simulates marketable limit)
            with pc._lock:
                pc._token_balances[tid] = pc._token_balances.get(tid, 0.0) - dump_size
                pc._usdc_balance += dump_size * dump_price

            cost_frac = (dump_size / remaining) if remaining > 0 else 1.0
            dump_cost = inv[f"{side_name}_cost"] * cost_frac
            pnl = dump_size * dump_price - dump_cost
            dump_pnl += pnl

            log.info(
                f"[{session.strategy.name}] DUMP | {side_name.upper()} {dump_size:.0f} sh "
                f"@ {dump_price:.4f} | pnl=${pnl:+.2f} | {market['question'][:30]}"
            )
            _db_write(session, "log_unwind",
                condition_id=cid, question=market.get("question", ""),
                side=side_name, shares=dump_size,
                sell_price=dump_price, usd_value=dump_size * dump_price,
                vwap_cost=dump_cost,
            )
            inv[side_name] -= dump_size
            inv[f"{side_name}_cost"] -= dump_cost
        except Exception as e:
            log.debug(f"[{session.strategy.name}] Dump {side_name} failed: {e}")

    return fills, dump_pnl


def _place_edge_order(session, market, book_cache, shares_per_side):
    """Place orders at the FARTHEST edge of the reward window. Both sides.

    Returns (bid_price, ask_price, bid_size, ask_size) for Q-score tracking.
    """
    pc = session.paper_client
    cid = market["condition_id"]
    tokens = market.get("token_ids", [])
    if len(tokens) < 2:
        return 0, 0, 0, 0

    yes_tid, no_tid = tokens[0], tokens[1]
    max_spread = market.get("max_spread", 0.03)
    min_size = market.get("min_size", 5)
    tick = market.get("tick_size", 0.01)
    decimals = max(2, len(str(tick).rstrip('0').split('.')[-1]))

    orders = session.state.setdefault("orders", {}).setdefault(cid, {})

    # Register for merge support
    pc.register_market(cid, yes_tid, no_tid)

    # Get midpoint
    try:
        yes_book = book_cache.get_order_book(yes_tid)
        if not yes_book or not yes_book.bids or not yes_book.asks:
            return 0, 0, 0, 0
        best_bid = float(yes_book.bids[0].price)
        best_ask = float(yes_book.asks[0].price)
        midpoint = (best_bid + best_ask) / 2
    except Exception:
        return 0, 0, 0, 0

    # Edge prices: maximum distance from mid while staying in reward window
    edge_bid = round(midpoint - max_spread, decimals)
    edge_ask = round(midpoint + max_spread, decimals)
    edge_bid = max(0.01, edge_bid)
    edge_ask = min(0.99, edge_ask)

    # Don't place at or above best — we want to be BEHIND the queue
    if edge_bid >= best_bid:
        edge_bid = round(best_bid - tick, decimals)
    if edge_bid <= 0:
        return 0, 0, 0, 0

    yes_shares = max(min_size, shares_per_side)
    no_clob = round(1.0 - edge_ask, decimals)
    no_clob = max(0.01, no_clob)
    no_shares = max(min_size, shares_per_side)

    # Balance check
    total_cost = yes_shares * edge_bid + no_shares * no_clob
    if total_cost > pc._usdc_balance * 0.9:
        scale = (pc._usdc_balance * 0.9) / total_cost if total_cost > 0 else 0
        yes_shares = max(min_size, yes_shares * scale)
        no_shares = max(min_size, no_shares * scale)

    bid_placed = ask_placed = 0.0
    bid_sz = ask_sz = 0.0

    # Place YES bid
    if not orders.get("yes_oid"):
        try:
            from py_clob_client_v2.clob_types import OrderArgs
            from py_clob_client_v2.order_builder.constants import BUY
            resp = pc.create_and_post_order(
                OrderArgs(token_id=yes_tid, price=edge_bid, size=float(yes_shares), side=BUY)
            )
            oid = resp.get("orderID") if isinstance(resp, dict) else None
            if oid:
                orders["yes_oid"] = oid
                bid_placed = edge_bid
                bid_sz = float(yes_shares)
                _db_write(session, "log_order_placed",
                    condition_id=cid, side="yes", price=edge_bid,
                    size=float(yes_shares), order_id=oid, order_type="BUY",
                )
        except Exception as e:
            log.debug(f"[{session.strategy.name}] YES order failed: {e}")
    else:
        bid_placed = edge_bid
        bid_sz = float(yes_shares)

    # Place NO ask
    if not orders.get("no_oid"):
        try:
            from py_clob_client_v2.clob_types import OrderArgs
            from py_clob_client_v2.order_builder.constants import BUY
            resp = pc.create_and_post_order(
                OrderArgs(token_id=no_tid, price=no_clob, size=float(no_shares), side=BUY)
            )
            oid = resp.get("orderID") if isinstance(resp, dict) else None
            if oid:
                orders["no_oid"] = oid
                ask_placed = edge_ask
                ask_sz = float(no_shares)
                _db_write(session, "log_order_placed",
                    condition_id=cid, side="no", price=edge_ask,
                    size=float(no_shares), order_id=oid, order_type="BUY",
                )
        except Exception as e:
            log.debug(f"[{session.strategy.name}] NO order failed: {e}")
    else:
        ask_placed = edge_ask
        ask_sz = float(no_shares)

    return bid_placed, ask_placed, bid_sz, ask_sz


# ── Reward Farmer Max ──────────────────────────────────────────────────

def cycle_reward_farmer_max(session, markets, book_cache):
    """ALL markets >= $50/day, 500 shares per side at the farthest edge."""
    session.cycle_count += 1
    pc = session.paper_client
    pc.simulate_fills(book_cache)

    eligible = [m for m in markets if m.get("daily_rate", 0) >= 50]

    for market in eligible:
        _detect_fills_and_handle(session, market, book_cache)
        bp, ap, bs, as_ = _place_edge_order(session, market, book_cache, shares_per_side=500)
        _record_qscore(session, market, book_cache, bp, ap, bs, as_)


# ── Passive Farmer (calm markets only) ─────────────────────────────────

_VOLATILE_KEYWORDS = [
    "iran", "crude oil", "invade", "forces enter", "regime fall",
    "ceasefire", "military", "war ", "attack", "strike", "missile",
    "nuclear", "sanctions",
]

def _is_calm(market: dict) -> bool:
    """True if market is low-volatility and has wide enough reward window."""
    q = (market.get("question") or "").lower()
    if any(kw in q for kw in _VOLATILE_KEYWORDS):
        return False
    ms = market.get("max_spread", 0.03)
    if ms < 0.025:
        return False  # reward window too narrow for edge placement
    vol = market.get("volume_24h", 0)
    liq = market.get("liquidity", 0)
    if liq > 0 and vol / liq > 4.0:
        return False
    return True

def cycle_passive(session, markets, book_cache):
    """Calm, non-volatile markets only. 300 shares at edge."""
    session.cycle_count += 1
    pc = session.paper_client
    pc.simulate_fills(book_cache)

    calm = [m for m in markets if _is_calm(m) and m.get("daily_rate", 0) >= 50]

    for market in calm:
        _detect_fills_and_handle(session, market, book_cache)
        bp, ap, bs, as_ = _place_edge_order(session, market, book_cache, shares_per_side=300)
        _record_qscore(session, market, book_cache, bp, ap, bs, as_)


# ── Standard Bot Strategies (use OrderManager) ────────────────────────

def cycle_standard(session, markets, book_cache):
    """Run the real bot's OrderManager cycle in paper mode."""
    from order_manager import OrderManager
    from state import PositionTracker
    import config as _cfg

    session.cycle_count += 1
    pc = session.paper_client
    pc.simulate_fills(book_cache)

    # Apply config overrides
    overrides = session.strategy.config_overrides
    saved = {}
    for k, v in overrides.items():
        if hasattr(_cfg, k):
            saved[k] = getattr(_cfg, k)
            setattr(_cfg, k, v)

    try:
        # Lazy init position tracker + order managers
        if "position_tracker" not in session.state:
            session.state["position_tracker"] = PositionTracker()
            session.state["order_managers"] = {}

        pt = session.state["position_tracker"]
        managers = session.state["order_managers"]

        max_mkts = int(overrides.get("MAX_MARKETS", _cfg.MAX_MARKETS))
        selected = markets[:max_mkts]

        # Get exchange orders once
        exchange_orders = pc.get_orders()

        for market in selected:
            cid = market["condition_id"]
            tokens = market.get("token_ids", [])
            if len(tokens) < 2:
                continue

            pc.register_market(cid, tokens[0], tokens[1])

            if cid not in managers:
                managers[cid] = OrderManager(pc, market, pt)
                if hasattr(managers[cid], '_reward_tracker'):
                    managers[cid]._reward_tracker = session.reward_tracker

            mgr = managers[cid]
            try:
                mgr.run_cycle(exchange_orders=exchange_orders)

                # Record Q-score stats
                bid_p = ask_p = bid_s = ask_s = 0.0
                for o in mgr.active_orders.values():
                    if o.side == "yes":
                        bid_p, bid_s = o.price, o.size or o.original_size
                    elif o.side == "no":
                        ask_p, ask_s = o.price, o.size or o.original_size
                _record_qscore(session, market, book_cache, bid_p, ask_p, bid_s, ask_s)

            except Exception as e:
                log.debug(f"[{session.strategy.name}] {market['question'][:30]}: {e}")

    finally:
        for k, v in saved.items():
            setattr(_cfg, k, v)


# ═══════════════════════════════════════════════════════════════════════
# 3. DATABASE HELPERS (isolated per-session writes)
# ═══════════════════════════════════════════════════════════════════════

def _db_write(session, method_name: str, **kwargs):
    """Call a BotDatabase method on the session's isolated DB."""
    try:
        fn = getattr(session.db, method_name)
        fn(**kwargs)
    except Exception as e:
        log.debug(f"DB write {method_name} failed: {e}")


def _save_rewards(session, markets):
    """Persist reward tracker state to session's DB."""
    import database as db_mod
    orig = db_mod._instance
    db_mod._instance = session.db
    try:
        session.reward_tracker._save()
    except Exception as e:
        log.debug(f"Reward save failed: {e}")
    finally:
        db_mod._instance = orig


def _run_reward_hourly(session, markets):
    """Trigger hourly reward estimation (Q-score -> est_reward_usd)."""
    import database as db_mod
    orig = db_mod._instance
    db_mod._instance = session.db
    try:
        session.reward_tracker.maybe_log_hourly(markets)
    except Exception as e:
        log.debug(f"Reward hourly failed: {e}")
    finally:
        db_mod._instance = orig


def _log_hourly_snapshot(session):
    """Write hourly P&L snapshot to session's DB."""
    from datetime import datetime
    now = time.time()
    if now - session.last_hourly_ts < 3600:
        return
    session.last_hourly_ts = now

    db = session.db
    hour_label = datetime.now().strftime("%Y-%m-%d %H:00")
    hour_ago = now - 3600
    conn = db._get_conn()

    try:
        r = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN side='yes' THEN shares*price ELSE shares*clob_cost END),0), COUNT(*) "
            "FROM fills WHERE ts >= ?", (hour_ago,)
        ).fetchone()
        total_bought, num_fills = r[0], r[1]

        r = conn.execute(
            "SELECT COALESCE(SUM(usd_value),0), COALESCE(SUM(pnl),0), COUNT(*) "
            "FROM unwinds WHERE ts >= ?", (hour_ago,)
        ).fetchone()
        total_sold, realized_pnl, num_unwinds = r[0], r[1], r[2]

        r = conn.execute(
            "SELECT COALESCE(SUM(loss_usd),0), COUNT(*) FROM stop_losses WHERE ts >= ?",
            (hour_ago,)
        ).fetchone()
        num_stop_losses = r[1]

        r = conn.execute(
            "SELECT COUNT(*) FROM orders_cancelled WHERE ts >= ? AND reason='danger'",
            (hour_ago,)
        ).fetchone()
        num_danger = r[0]

        total_pos = 0.0
        for p in conn.execute("SELECT * FROM positions").fetchall():
            total_pos += p[2] * p[3] + p[5] * p[6]

        est_reward = 0.0
        for r in conn.execute("SELECT data FROM reward_market_stats").fetchall():
            est_reward += json.loads(r[0]).get("est_reward_usd", 0)

        unrealized = total_pos - total_bought if total_bought > 0 else 0

        db.log_hourly_snapshot(
            hour_label=hour_label,
            num_markets=0,
            total_bought_usd=total_bought,
            total_sold_usd=total_sold,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized,
            total_position_usd=total_pos,
            est_reward_usd=est_reward,
            est_reward_rate_hr=est_reward / max(1, (now - session.start_time) / 3600),
            num_fills=num_fills,
            num_unwinds=num_unwinds,
            num_stop_losses=num_stop_losses,
            num_danger_cancels=num_danger,
            avg_uptime_pct=0.0,
        )
    except Exception as e:
        log.debug(f"Hourly snapshot error: {e}")


# ═══════════════════════════════════════════════════════════════════════
# 4. STRATEGY DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════

STRATEGIES = [
    Strategy(
        name="current",
        run_cycle=cycle_standard,
        config_overrides={
            "ORDER_SIZE": 150,
            "MAX_MARKETS": 5,
            "DYNAMIC_SIZING_ENABLED": True,
        },
    ),
    Strategy(
        name="min_size",
        run_cycle=cycle_standard,
        config_overrides={
            "ORDER_SIZE": 5,
            "MAX_MARKETS": 23,
            "DYNAMIC_SIZING_ENABLED": False,
            "DANGER_ZONE_CENTS": 0.005,
            "MIN_SCORE_THRESHOLD": 30,
        },
    ),
    Strategy(
        name="tiered",
        run_cycle=cycle_standard,
        config_overrides={
            "ORDER_SIZE": 50,
            "MAX_MARKETS": 10,
            "DYNAMIC_SIZING_ENABLED": True,
            "DYNAMIC_SIZE_MIN": 5,
            "DYNAMIC_SIZE_MAX": 150,
            "DANGER_ZONE_CENTS": 0.005,
            "MIN_SCORE_THRESHOLD": 40,
        },
    ),
    Strategy(
        name="passive",
        run_cycle=cycle_passive,
        use_wide_markets=True,
    ),
    Strategy(
        name="reward_farmer_max",
        run_cycle=cycle_reward_farmer_max,
        use_wide_markets=True,
    ),
]


# ═══════════════════════════════════════════════════════════════════════
# 5. INITIALIZATION
# ═══════════════════════════════════════════════════════════════════════

def parse_duration(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("h"):
        return int(float(s[:-1]) * 3600)
    if s.endswith("m"):
        return int(float(s[:-1]) * 60)
    return int(s.rstrip("s"))


def create_real_client():
    from config import (
        CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE,
        HOST, PRIVATE_KEY, CHAIN_ID, SIGNATURE_TYPE, FUNDER, BUILDER_CODE,
    )
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds, BuilderConfig
    from rate_limiter import RateLimitedClient

    creds = ApiCreds(api_key=CLOB_API_KEY, api_secret=CLOB_SECRET, api_passphrase=CLOB_PASS_PHRASE)
    raw = ClobClient(
        HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
        signature_type=SIGNATURE_TYPE, funder=FUNDER, creds=creds,
        builder_config=BuilderConfig(builder_code=BUILDER_CODE) if BUILDER_CODE else None,
    )
    return RateLimitedClient(raw)


def fetch_markets(wide: bool = False):
    """Fetch reward markets. If wide=True, use relaxed filters."""
    import config as _cfg
    from market import get_rewards_markets

    if not wide:
        return get_rewards_markets(limit=50)

    # Temporarily relax filters
    saved = {}
    relaxed = {"MAX_VOLUME_TO_REWARD_RATIO": 500000, "MIN_DAILY_RATE": 5, "MIN_SCORE_THRESHOLD": 0}
    for k, v in relaxed.items():
        if hasattr(_cfg, k):
            saved[k] = getattr(_cfg, k)
            setattr(_cfg, k, v)
    try:
        return get_rewards_markets(limit=50)
    finally:
        for k, v in saved.items():
            setattr(_cfg, k, v)


def create_session(strategy: Strategy, real_client, book_cache, balance: float) -> Session:
    """Create a fully isolated session for one strategy."""
    import database

    # Fresh DB
    db_path = f"paper_{strategy.name}.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    db = database.BotDatabase(db_path=db_path)

    # Paper client
    from paper_client import PaperClient
    pc = PaperClient(
        real_client, initial_balance=balance,
        label=strategy.name, book_cache=book_cache,
    )

    # Isolated reward tracker (init with session DB)
    from reward_tracker import RewardTracker
    orig = database._instance
    database._instance = db
    try:
        rt = RewardTracker()
    finally:
        database._instance = orig

    now = time.time()
    return Session(
        strategy=strategy, paper_client=pc, db=db,
        reward_tracker=rt, start_time=now, last_hourly_ts=now,
    )


# ═══════════════════════════════════════════════════════════════════════
# 6. REPORTING
# ═══════════════════════════════════════════════════════════════════════

def print_comparison(sessions: list[Session], hours: float):
    """Print side-by-side comparison table."""
    import sqlite3

    print(f"\n{'='*90}")
    print(f"  COMPARISON @ {hours:.1f} hours")
    print(f"{'='*90}")

    header = f"{'Metric':<18s}"
    for s in sessions:
        header += f" | {s.strategy.name:>12s}"
    print(header)
    print("-" * (20 + 15 * len(sessions)))

    rows = {}
    for s in sessions:
        conn = s.db._get_conn()
        r = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN side='yes' THEN shares*price ELSE shares*clob_cost END),0), COUNT(*) FROM fills"
        ).fetchone()
        bought, fills = r[0], r[1]

        r = conn.execute("SELECT COALESCE(SUM(usd_value),0), COUNT(*) FROM unwinds").fetchone()
        sold, unwinds = r[0], r[1]

        r = conn.execute("SELECT COALESCE(SUM(loss_usd),0), COUNT(*) FROM stop_losses").fetchone()
        stops_usd, stops_n = r[0], r[1]

        pos = 0.0
        for p in conn.execute("SELECT * FROM positions").fetchall():
            pos += p[2] * p[3] + p[5] * p[6]

        rewards = 0.0
        for r in conn.execute("SELECT data FROM reward_market_stats").fetchall():
            rewards += json.loads(r[0]).get("est_reward_usd", 0)

        net = sold + pos - bought
        rows[s.strategy.name] = {
            "Fills": fills, "Unwinds": unwinds, "Stop losses": stops_n,
            "Bought": bought, "Sold": sold, "Open pos": pos,
            "Stop loss $": stops_usd, "Est rewards": rewards,
            "Trading P&L": net, "NET (all-in)": net + rewards,
            "Balance": s.paper_client._usdc_balance,
        }

    for metric in ["Fills", "Unwinds", "Stop losses", "Bought", "Sold", "Open pos",
                    "Stop loss $", "Est rewards", "Trading P&L", "NET (all-in)", "Balance"]:
        line = f"{metric:<18s}"
        for s in sessions:
            v = rows[s.strategy.name][metric]
            if isinstance(v, int) or metric in ("Fills", "Unwinds", "Stop losses"):
                line += f" | {int(v):>12d}"
            elif metric in ("Trading P&L", "NET (all-in)"):
                line += f" | ${v:>+10.2f}"
            else:
                line += f" | ${v:>10.2f}"
        print(line)
    print()


# ═══════════════════════════════════════════════════════════════════════
# 7. MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Paper Trading Simulator")
    parser.add_argument("--duration", default="6h", help="Duration (e.g., 6h, 30m, 120s)")
    parser.add_argument("--balance", type=float, default=500, help="Starting balance per strategy")
    parser.add_argument("--strategies", nargs="*", default=None, help="Which strategies to run")
    args = parser.parse_args()

    duration_secs = parse_duration(args.duration)
    balance = args.balance

    log.info("Paper Trading Simulator starting")
    log.info(f"  Duration: {args.duration} ({duration_secs}s)")
    log.info(f"  Balance: ${balance} per strategy")

    # Select strategies
    active_strategies = STRATEGIES
    if args.strategies:
        active_strategies = [s for s in STRATEGIES if s.name in args.strategies]
    log.info(f"  Strategies: {[s.name for s in active_strategies]}")

    # Connect to real API (read-only)
    real_client = create_real_client()
    log.info("Connected to Polymarket CLOB API")

    # Shared order book cache
    from paper_client import CachedOrderBookProvider
    book_cache = CachedOrderBookProvider(real_client, ttl_secs=25.0)

    # Create sessions
    sessions = []
    for strategy in active_strategies:
        session = create_session(strategy, real_client, book_cache, balance)
        sessions.append(session)
        log.info(f"  Session '{strategy.name}' ready (${balance})")

    # Fetch markets
    log.info("Fetching markets...")
    try:
        std_markets = fetch_markets(wide=False)
        wide_markets = fetch_markets(wide=True)
        log.info(f"Markets: {len(std_markets)} standard, {len(wide_markets)} wide")
    except Exception as e:
        log.error(f"Failed to fetch markets: {e}")
        sys.exit(1)

    if not std_markets and not wide_markets:
        log.error("No markets found. Exiting.")
        sys.exit(1)

    # Shutdown handling
    shutdown = False
    def _signal(signum, frame):
        nonlocal shutdown
        shutdown = True
        log.info("Shutdown requested...")
    signal.signal(signal.SIGINT, _signal)

    # ── Main loop ────────────────────────────────────────────────────
    import config
    cycle_interval = getattr(config, "ORDER_REFRESH_SECS", 30)
    refresh_interval = getattr(config, "MARKET_REFRESH_SECS", 1800)

    start_time = time.time()
    last_market_refresh = time.time()
    last_report = start_time
    cycle = 0

    log.info("Starting paper trading cycles...")

    while not shutdown and (time.time() - start_time) < duration_secs:
        cycle += 1
        cycle_start = time.time()

        # Refresh markets periodically
        if time.time() - last_market_refresh >= refresh_interval:
            try:
                book_cache.invalidate()
                std_markets = fetch_markets(wide=False)
                wide_markets = fetch_markets(wide=True)
                last_market_refresh = time.time()
                log.info(f"Market refresh: {len(std_markets)} std, {len(wide_markets)} wide")
            except Exception as e:
                log.warning(f"Market refresh failed: {e}")

        # Run each strategy's cycle
        for session in sessions:
            try:
                markets = wide_markets if session.strategy.use_wide_markets else std_markets
                session.strategy.run_cycle(session, markets, book_cache)

                # Save reward stats (every cycle is fine — it's fast)
                _save_rewards(session, markets)

            except Exception as e:
                log.error(f"[{session.strategy.name}] Cycle error: {e}")

        # Hourly bookkeeping
        for session in sessions:
            markets = wide_markets if session.strategy.use_wide_markets else std_markets
            _run_reward_hourly(session, markets)
            _log_hourly_snapshot(session)

        # Hourly comparison table
        elapsed = time.time() - start_time
        if elapsed - (last_report - start_time) >= 3600:
            print_comparison(sessions, elapsed / 3600)
            last_report = time.time()

        # Status every 10 cycles
        if cycle % 10 == 0:
            elapsed_min = (time.time() - start_time) / 60
            remaining_min = (duration_secs - elapsed) / 60
            log.info(
                f"Cycle {cycle} | {elapsed_min:.0f}m elapsed, "
                f"{remaining_min:.0f}m remaining"
            )

        # Sleep until next cycle
        cycle_duration = time.time() - cycle_start
        sleep_time = max(0, cycle_interval - cycle_duration)
        if sleep_time > 0 and not shutdown:
            time.sleep(sleep_time)

    # ── Final report ─────────────────────────────────────────────────
    elapsed_hrs = (time.time() - start_time) / 3600
    log.info(f"\nPaper trading complete. {elapsed_hrs:.1f} hours, {cycle} cycles.\n")
    print_comparison(sessions, elapsed_hrs)

    # Final reward estimation for any incomplete hours
    for session in sessions:
        markets = wide_markets if session.strategy.use_wide_markets else std_markets
        # Force the hourly report by resetting the timer
        session.reward_tracker._last_hourly_log = 0
        _run_reward_hourly(session, markets)

    # Print final comparison with updated rewards
    print_comparison(sessions, elapsed_hrs)
    log.info("Done.")


if __name__ == "__main__":
    main()
