#!/usr/bin/env python3
"""
Paper Trading Simulator — run multiple strategies on live market data
without placing real orders.

Usage:
    python paper_trader.py --duration 6h --balance 1000
    python paper_trader.py --duration 30m --balance 500  # quick smoke test

Runs 3 strategies in parallel on real-time order book data:
  - current:  ORDER_SIZE=150, MAX_MARKETS=5  (baseline)
  - min_size: ORDER_SIZE=5,   MAX_MARKETS=23 (min shares everywhere)
  - tiered:   ORDER_SIZE=50,  MAX_MARKETS=10 (adapt to market)
"""

import argparse
import logging
import os
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass, field

# Must set up before importing bot modules
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Suppress noisy loggers
for name in ("urllib3", "py_clob_client"):
    logging.getLogger(name).setLevel(logging.WARNING)


@dataclass
class PaperStrategy:
    """Configuration for one paper trading strategy."""
    name: str
    config_overrides: dict = field(default_factory=dict)
    initial_balance: float = 1000.0
    fill_model: str = "cross_through"
    queue_factor: float = 0.5
    custom_cycle: str | None = None  # "passive" for edge-placement + dump-on-fill


# ── Built-in strategies ──────────────────────────────────────────────────

STRATEGIES = [
    PaperStrategy(
        name="current",
        config_overrides={
            "ORDER_SIZE": 150,
            "MAX_MARKETS": 5,
            "DYNAMIC_SIZE_MIN": 50,
            "DYNAMIC_SIZE_MAX": 250,
            "DANGER_ZONE_CENTS": 0.01,
        },
        initial_balance=1000.0,
    ),
    PaperStrategy(
        name="min_size",
        config_overrides={
            "ORDER_SIZE": 5,
            "MAX_MARKETS": 23,
            "DYNAMIC_SIZING_ENABLED": False,
            "DYNAMIC_SIZE_MIN": 5,
            "DYNAMIC_SIZE_MAX": 5,
            "DANGER_ZONE_CENTS": 0.005,  # half-cent — smaller orders tolerate closer to mid
            "MIN_SCORE_THRESHOLD": 30,
            "MAX_VOLUME_TO_REWARD_RATIO": 200000,  # allow high-volume markets
        },
        initial_balance=1000.0,
    ),
    PaperStrategy(
        name="tiered",
        config_overrides={
            "ORDER_SIZE": 50,
            "MAX_MARKETS": 10,
            "DYNAMIC_SIZING_ENABLED": True,
            "DYNAMIC_SIZE_MIN": 5,
            "DYNAMIC_SIZE_MAX": 150,
            "DANGER_ZONE_CENTS": 0.005,
            "MIN_SCORE_THRESHOLD": 40,
            "MAX_VOLUME_TO_REWARD_RATIO": 100000,
        },
        initial_balance=1000.0,
    ),
    PaperStrategy(
        name="passive",
        config_overrides={
            "ORDER_SIZE": 300,  # go big — edge placement means near-zero fill risk
            "MAX_MARKETS": 20,
            "DYNAMIC_SIZING_ENABLED": False,
            "MIN_DAILY_RATE": 50,  # markets >= $50/day
            "DANGER_ZONE_CENTS": 0.0,  # irrelevant — custom cycle handles placement
            "MIN_SCORE_THRESHOLD": 0,
            "MAX_VOLUME_TO_REWARD_RATIO": 500000,
        },
        initial_balance=1000.0,
        custom_cycle="passive",
    ),
]


def parse_duration(s: str) -> int:
    """Parse '6h', '30m', '120s' into seconds."""
    s = s.strip().lower()
    if s.endswith("h"):
        return int(float(s[:-1]) * 3600)
    if s.endswith("m"):
        return int(float(s[:-1]) * 60)
    if s.endswith("s"):
        return int(float(s[:-1]))
    return int(s)


def apply_config_overrides(overrides: dict):
    """Apply config overrides by directly setting module attributes."""
    import config
    for key, value in overrides.items():
        if hasattr(config, key):
            setattr(config, key, value)


def restore_config(saved: dict):
    """Restore original config values."""
    import config
    for key, value in saved.items():
        setattr(config, key, value)


def save_config_state(keys: set) -> dict:
    """Save current config values for later restoration."""
    import config
    saved = {}
    for key in keys:
        if hasattr(config, key):
            saved[key] = getattr(config, key)
    return saved


class PaperSession:
    """Manages one paper trading strategy's state."""

    def __init__(self, strategy: PaperStrategy, real_client, book_cache):
        from paper_client import PaperClient
        import database

        self.strategy = strategy
        self.paper_client = PaperClient(
            real_client=real_client,
            initial_balance=strategy.initial_balance,
            fill_model=strategy.fill_model,
            queue_position_factor=strategy.queue_factor,
            label=strategy.name,
            book_cache=book_cache,
        )

        # Separate database for this strategy
        self.db_path = f"paper_{strategy.name}.db"
        # Remove old paper DB if exists (fresh start)
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

        self.db = database.BotDatabase(db_path=self.db_path)

        # Per-session reward tracker (isolated from other strategies)
        from reward_tracker import RewardTracker
        import database as _db_mod
        _orig = _db_mod._instance
        _db_mod._instance = self.db
        self.reward_tracker = RewardTracker()
        _db_mod._instance = _orig

        # Tracking
        self.cycle_count = 0
        self.start_time = time.time()
        self.total_bought = 0.0
        self.total_sold = 0.0
        self.last_hourly_snapshot = time.time()


def create_real_client():
    """Create the real CLOB client for read-only API access."""
    from config import (
        CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE,
        HOST, PRIVATE_KEY, CHAIN_ID, SIGNATURE_TYPE, FUNDER,
    )
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    from rate_limiter import RateLimitedClient

    creds = ApiCreds(
        api_key=CLOB_API_KEY,
        api_secret=CLOB_SECRET,
        api_passphrase=CLOB_PASS_PHRASE,
    )
    raw_client = ClobClient(
        HOST,
        key=PRIVATE_KEY,
        chain_id=CHAIN_ID,
        creds=creds,
        signature_type=SIGNATURE_TYPE,
        funder=FUNDER,
    )
    return RateLimitedClient(raw_client)


def record_paper_cycle_stats(
    session: PaperSession, market: dict, mgr, reward_tracker
) -> None:
    """Record per-market reward stats after each cycle (mirrors bot.py._record_cycle_stats)."""
    import config as _cfg
    import time as _t
    from database import get_db

    try:
        condition_id = market["condition_id"]
        reward_tracker.get_or_create(
            condition_id=condition_id,
            question=market.get("question", ""),
            daily_rate=market.get("daily_rate", 0),
            max_spread=market.get("max_spread", 0),
        )

        active_sides = {o.side for o in mgr.active_orders.values()}
        has_yes = "yes" in active_sides
        has_no = "no" in active_sides

        bid_price = ask_price = 0.0
        bid_size = ask_size = 0.0
        for o in mgr.active_orders.values():
            if o.side == "yes":
                bid_price, bid_size = o.price, o.size or o.original_size
            elif o.side == "no":
                ask_price, ask_size = o.price, o.size or o.original_size

        best_bid = getattr(mgr, "_cached_best_bid", 0)
        best_ask = getattr(mgr, "_cached_best_ask", 1)
        midpoint = (best_bid + best_ask) / 2 if best_bid > 0 else 0
        cached_book = getattr(mgr, "_last_order_book", None)

        yes_usd = session.position_tracker.get_position(condition_id, "yes")
        no_usd = session.position_tracker.get_position(condition_id, "no")
        inventory_usd = yes_usd + no_usd

        cooldown_active = (
            _t.time() - mgr._last_fill_time.get("yes", 0) < _cfg.POST_FILL_COOLDOWN_SECS
            or _t.time() - mgr._last_fill_time.get("no", 0) < _cfg.POST_FILL_COOLDOWN_SECS
        )
        skew_active = inventory_usd > _cfg.INVENTORY_SKEW_THRESHOLD

        reward_tracker.record_cycle(
            condition_id=condition_id,
            has_yes_order=has_yes,
            has_no_order=has_no,
            bid_price=bid_price,
            ask_price=ask_price,
            inventory_usd=inventory_usd,
            cooldown_active=cooldown_active,
            skew_active=skew_active,
            cycle_duration_secs=_cfg.ORDER_REFRESH_SECS,
            midpoint=midpoint,
            bid_size=bid_size,
            ask_size=ask_size,
            order_book=cached_book,
        )

        # Cycle snapshot every 10th cycle
        if session.cycle_count % 10 == 0:
            get_db().log_cycle_snapshot(
                cycle_num=session.cycle_count,
                condition_id=condition_id,
                best_bid=best_bid, best_ask=best_ask,
                our_bid=bid_price, our_ask=ask_price,
                yes_position_usd=yes_usd,
                no_position_usd=no_usd,
                active_orders=len(mgr.active_orders),
                unwind_orders=len(mgr.unwind_orders),
            )
    except Exception as e:
        log.debug(f"Paper cycle stats error: {e}")


def _is_volatile_market(market: dict) -> bool:
    """Reject markets with high price volatility or geopolitical keywords."""
    q = (market.get("question") or "").lower()
    # Keyword blacklist — topics with sharp, unpredictable moves
    VOLATILE_KEYWORDS = [
        "iran", "crude oil", "invade", "forces enter", "regime fall",
        "ceasefire", "military", "war ", "attack", "strike", "missile",
        "nuclear", "sanctions",
    ]
    if any(kw in q for kw in VOLATILE_KEYWORDS):
        return True

    # Activity ratio: high volume relative to liquidity = active/volatile
    vol = market.get("volume_24h", 0)
    liq = market.get("liquidity", 0)
    if liq > 0 and vol / liq > 4.0:
        return True

    return False


def run_passive_cycle(session: PaperSession, active_markets: list, reward_tracker):
    """Custom cycle for the passive farmer strategy.

    Picks calm, non-volatile markets with rewards >= $50/day.
    Places BIG orders at the EDGE of the reward window on BOTH sides.
    If both sides fill → merge for $1.00 (profit).
    If one side fills → dump at market (small known loss).
    Goal: earn Q-score rewards with near-zero fill probability.
    """
    import config as _cfg
    import database as db_module
    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.order_builder.constants import BUY, SELL
    from database import get_db

    # Apply config overrides
    all_keys = set(session.strategy.config_overrides.keys())
    saved = save_config_state(all_keys)
    apply_config_overrides(session.strategy.config_overrides)

    original_db = db_module._instance
    db_module._instance = session.db

    try:
        session.cycle_count += 1
        pc = session.paper_client

        # Lazy init
        if not hasattr(session, "_passive_orders"):
            session._passive_orders = {}   # cid → {"yes_oid": ..., "no_oid": ...}
            session._passive_inventory = {}  # cid → {"yes": shares, "no": shares, "yes_cost": $, "no_cost": $}

        # ── Filter markets: calm + rewarding only ─────────────────────────
        calm_markets = []
        for market in active_markets:
            if _is_volatile_market(market):
                continue
            rate = market.get("daily_rate", 0)
            if rate < _cfg.MIN_DAILY_RATE:
                continue
            calm_markets.append(market)

        # Register markets with paper client
        for market in calm_markets:
            cid = market["condition_id"]
            tokens = market.get("token_ids", [])
            if len(tokens) >= 2:
                pc.register_market(cid, tokens[0], tokens[1])

        # Simulate fills from last cycle
        pc.simulate_fills()

        # ── Phase 1: Check for fills → merge or dump ──────────────────────
        for market in calm_markets:
            cid = market["condition_id"]
            orders = session._passive_orders.get(cid, {})
            inv = session._passive_inventory.setdefault(
                cid, {"yes": 0.0, "no": 0.0, "yes_cost": 0.0, "no_cost": 0.0}
            )
            tokens = market.get("token_ids", [])
            if len(tokens) < 2:
                continue
            yes_tid, no_tid = tokens[0], tokens[1]

            # Check each side for fills
            for side_key, tid, side_name in [
                ("yes_oid", yes_tid, "yes"), ("no_oid", no_tid, "no")
            ]:
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

                # Record inventory
                inv[side_name] += matched
                inv[f"{side_name}_cost"] += matched * fill_price

                log.info(
                    f"[passive] FILL | {side_name.upper()} {matched:.0f} sh "
                    f"@ {fill_price:.4f} | inv: YES={inv['yes']:.0f} NO={inv['no']:.0f} | "
                    f"{market['question'][:35]}"
                )

                # Record fill to DB
                get_db().log_fill(
                    condition_id=cid,
                    question=market.get("question", ""),
                    side=side_name, fill_type="FULL",
                    shares=matched, price=fill_price,
                    clob_cost=fill_price,
                    usd_value=matched * fill_price,
                )
                orders[side_key] = None  # clear for re-placement

            # ── Try MERGE first (both sides have inventory) ───────────
            merge_amount = min(inv["yes"], inv["no"])
            if merge_amount >= 1.0:
                try:
                    result = pc.merge_positions(cid, merge_amount)
                    if isinstance(result, dict) and result.get("success"):
                        merge_revenue = merge_amount  # $1 per pair
                        yes_cost_portion = (inv["yes_cost"] / inv["yes"]) * merge_amount if inv["yes"] > 0 else 0
                        no_cost_portion = (inv["no_cost"] / inv["no"]) * merge_amount if inv["no"] > 0 else 0
                        total_cost = yes_cost_portion + no_cost_portion
                        merge_pnl = merge_revenue - total_cost

                        log.info(
                            f"[passive] MERGE | {merge_amount:.0f} pairs | "
                            f"revenue=${merge_revenue:.2f} cost=${total_cost:.2f} "
                            f"pnl=${merge_pnl:+.2f} | {market['question'][:35]}"
                        )

                        # Update inventory
                        inv["yes"] -= merge_amount
                        inv["no"] -= merge_amount
                        inv["yes_cost"] -= yes_cost_portion
                        inv["no_cost"] -= no_cost_portion

                        get_db().log_unwind(
                            condition_id=cid,
                            question=market.get("question", ""),
                            side="merge", shares=merge_amount,
                            sell_price=1.0, usd_value=merge_revenue,
                            vwap_cost=total_cost, pnl=merge_pnl,
                        )
                except Exception as e:
                    log.warning(f"[passive] Merge failed for {cid[:16]}: {e}")

            # ── Dump any remaining single-side inventory at market ────
            for side_name, tid in [("yes", yes_tid), ("no", no_tid)]:
                remaining = inv[side_name]
                if remaining < 1.0:
                    continue
                # Only dump if we DON'T have the other side (merge would have caught it)
                other = "no" if side_name == "yes" else "yes"
                if inv[other] >= 1.0:
                    continue  # wait for merge opportunity

                try:
                    book = pc.get_order_book(tid)
                    if not book or not book.bids:
                        continue
                    dump_price = float(book.bids[0].price)

                    # Cap dump size to available token balance
                    available = pc._token_balances.get(tid, 0.0)
                    dump_size = min(remaining, available)
                    if dump_size < 1.0:
                        log.warning(
                            f"[passive] Skip dump {side_name}: "
                            f"inv={remaining:.0f} but tokens={available:.0f}"
                        )
                        inv[side_name] = 0.0
                        inv[f"{side_name}_cost"] = 0.0
                        continue

                    # Instant dump: directly deduct tokens and credit USDC
                    # (simulates a marketable limit order that fills immediately)
                    with pc._lock:
                        pc._token_balances[tid] = pc._token_balances.get(tid, 0.0) - dump_size
                        pc._usdc_balance += dump_size * dump_price

                    cost_fraction = (dump_size / remaining) if remaining > 0 else 1.0
                    dump_cost = inv[f"{side_name}_cost"] * cost_fraction
                    dump_revenue = dump_size * dump_price
                    dump_pnl = dump_revenue - dump_cost

                    log.info(
                        f"[passive] DUMP | {side_name.upper()} {dump_size:.0f} sh "
                        f"@ {dump_price:.4f} | cost=${dump_cost:.2f} "
                        f"rev=${dump_revenue:.2f} pnl=${dump_pnl:+.2f} | "
                        f"{market['question'][:35]}"
                    )

                    get_db().log_unwind(
                        condition_id=cid,
                        question=market.get("question", ""),
                        side=side_name, shares=dump_size,
                        sell_price=dump_price, usd_value=dump_revenue,
                        vwap_cost=dump_cost, pnl=dump_pnl,
                    )

                    inv[side_name] -= dump_size
                    inv[f"{side_name}_cost"] -= dump_cost
                except Exception as e:
                    log.warning(f"[passive] Dump {side_name} failed: {e}")

        # ── Phase 2: Place BIG edge orders on calm markets ────────────────
        for market in calm_markets:
            cid = market["condition_id"]
            tokens = market.get("token_ids", [])
            if len(tokens) < 2:
                continue
            yes_tid, no_tid = tokens[0], tokens[1]

            orders = session._passive_orders.setdefault(cid, {})
            max_spread = market.get("max_spread", 0.03)
            tick = market.get("tick_size", 0.01)
            min_size = market.get("min_size", 5)
            decimals = max(2, len(str(tick).rstrip('0').split('.')[-1]))

            # Get order book for midpoint
            try:
                yes_book = pc.get_order_book(yes_tid)
                if not yes_book or not yes_book.bids or not yes_book.asks:
                    continue
                best_bid = float(yes_book.bids[0].price)
                best_ask = float(yes_book.asks[0].price)
                midpoint = (best_bid + best_ask) / 2
            except Exception:
                continue

            # Edge prices: farthest from mid but inside reward window
            # Place at FULL max_spread from midpoint (maximum distance = minimum fill risk)
            edge_offset = max_spread
            edge_bid = round(midpoint - edge_offset, decimals)
            edge_ask = round(midpoint + edge_offset, decimals)

            # Clamp to sane range
            edge_bid = max(0.01, min(edge_bid, midpoint - tick))
            edge_ask = min(0.99, max(edge_ask, midpoint + tick))

            # BIG order sizes — $ORDER_SIZE budget, converted to shares
            yes_shares = max(min_size, _cfg.ORDER_SIZE / max(edge_bid, 0.01))
            yes_shares = min(yes_shares, 10000)  # hard cap
            no_clob_price = round(1.0 - edge_ask, decimals)
            no_clob_price = max(0.01, no_clob_price)
            no_shares = max(min_size, _cfg.ORDER_SIZE / max(no_clob_price, 0.01))
            no_shares = min(no_shares, 10000)

            # Place YES-side (bid at edge of reward window)
            if not orders.get("yes_oid"):
                try:
                    args = OrderArgs(
                        token_id=yes_tid, price=edge_bid,
                        size=float(yes_shares), side=BUY,
                    )
                    resp = pc.create_and_post_order(args)
                    oid = resp.get("orderID") if isinstance(resp, dict) else None
                    if oid:
                        orders["yes_oid"] = oid
                        get_db().log_order_placed(
                            condition_id=cid, side="yes",
                            price=edge_bid, size=float(yes_shares),
                            order_id=oid, order_type="BUY",
                        )
                        if session.cycle_count <= 3:  # log first few cycles
                            log.info(
                                f"[passive] EDGE BID | YES @ {edge_bid:.3f} "
                                f"({yes_shares:.0f} sh, ${edge_bid*yes_shares:.0f}) | "
                                f"mid={midpoint:.3f} | {market['question'][:35]}"
                            )
                except Exception as e:
                    log.debug(f"[passive] YES edge order failed: {e}")

            # Place NO-side (ask at edge — buy NO token at 1 - edge_ask)
            if not orders.get("no_oid"):
                try:
                    args = OrderArgs(
                        token_id=no_tid, price=no_clob_price,
                        size=float(no_shares), side=BUY,
                    )
                    resp = pc.create_and_post_order(args)
                    oid = resp.get("orderID") if isinstance(resp, dict) else None
                    if oid:
                        orders["no_oid"] = oid
                        get_db().log_order_placed(
                            condition_id=cid, side="no",
                            price=edge_ask, size=float(no_shares),
                            order_id=oid, order_type="BUY",
                        )
                        if session.cycle_count <= 3:
                            log.info(
                                f"[passive] EDGE ASK | NO @ {edge_ask:.3f} "
                                f"(CLOB {no_clob_price:.3f}, {no_shares:.0f} sh, "
                                f"${no_clob_price*no_shares:.0f}) | "
                                f"mid={midpoint:.3f} | {market['question'][:35]}"
                            )
                except Exception as e:
                    log.debug(f"[passive] NO edge order failed: {e}")

            # ── Record reward tracking stats ──────────────────────────
            reward_tracker.get_or_create(
                condition_id=cid,
                question=market.get("question", ""),
                daily_rate=market.get("daily_rate", 0),
                max_spread=max_spread,
            )
            has_yes = orders.get("yes_oid") is not None
            has_no = orders.get("no_oid") is not None

            # Convert OrderBookSummary to dict for reward_tracker
            book_dict = None
            if yes_book and hasattr(yes_book, "bids"):
                book_dict = {
                    "bids": [{"price": float(b.price), "size": float(b.size)} for b in yes_book.bids],
                    "asks": [{"price": float(a.price), "size": float(a.size)} for a in yes_book.asks],
                }

            reward_tracker.record_cycle(
                condition_id=cid,
                has_yes_order=has_yes, has_no_order=has_no,
                bid_price=edge_bid if has_yes else 0,
                ask_price=edge_ask if has_no else 0,
                inventory_usd=0.0,
                cooldown_active=False, skew_active=False,
                cycle_duration_secs=_cfg.ORDER_REFRESH_SECS,
                midpoint=midpoint,
                bid_size=float(yes_shares) if has_yes else 0,
                ask_size=float(no_shares) if has_no else 0,
                order_book=book_dict,
            )

            if session.cycle_count % 10 == 0:
                get_db().log_cycle_snapshot(
                    cycle_num=session.cycle_count,
                    condition_id=cid,
                    best_bid=best_bid, best_ask=best_ask,
                    our_bid=edge_bid, our_ask=edge_ask,
                    yes_position_usd=0, no_position_usd=0,
                    active_orders=int(has_yes) + int(has_no),
                    unwind_orders=0,
                )

        # Persist reward stats to session DB (while DB is still swapped)
        try:
            reward_tracker._save()
        except Exception:
            pass

    finally:
        restore_config(saved)
        db_module._instance = original_db


def run_one_cycle(session: PaperSession, active_markets: list, reward_tracker):
    """Run one cycle for a paper session using the shared market list."""
    import config
    from order_manager import OrderManager
    from state import PositionStore
    import database as db_module

    # Apply config overrides for this strategy
    all_keys = set(session.strategy.config_overrides.keys())
    saved = save_config_state(all_keys)
    apply_config_overrides(session.strategy.config_overrides)

    # Swap DB singleton
    original_db = db_module._instance
    db_module._instance = session.db

    try:
        session.cycle_count += 1

        # Ensure order managers exist for each market
        if not hasattr(session, "order_managers"):
            session.order_managers = {}
            session.position_tracker = PositionStore()

        # Register markets with paper client
        for market in active_markets:
            cid = market["condition_id"]
            tokens = market.get("token_ids", [])
            if len(tokens) >= 2:
                session.paper_client.register_market(cid, tokens[0], tokens[1])

            if cid not in session.order_managers:
                mgr = OrderManager(
                    client=session.paper_client,
                    market=market,
                    position_tracker=session.position_tracker,
                )
                mgr._reward_tracker = reward_tracker
                session.order_managers[cid] = mgr

        # Fetch exchange orders (returns our paper orders)
        exchange_orders = session.paper_client._paper_get_orders()

        # Run each market cycle
        for market in active_markets:
            cid = market["condition_id"]
            mgr = session.order_managers.get(cid)
            if mgr:
                try:
                    mgr.run_cycle(exchange_orders=exchange_orders)
                    # Record Q-score and reward stats (mirrors bot.py)
                    record_paper_cycle_stats(session, market, mgr, reward_tracker)
                except Exception as e:
                    log.debug(
                        f"[{session.strategy.name}] Cycle error "
                        f"{market['question'][:30]}: {e}"
                    )

        # Simulate fills based on real order book
        session.paper_client.simulate_fills()

        # Persist reward stats to session DB (while DB is still swapped)
        try:
            reward_tracker._save()
        except Exception:
            pass

    finally:
        # Restore config and DB
        restore_config(saved)
        db_module._instance = original_db


def log_hourly_snapshot(session: PaperSession, reward_tracker) -> None:
    """Log hourly P&L snapshot for a paper session (mirrors bot.py._log_hourly_pnl_snapshot)."""
    import database as db_module
    from datetime import datetime

    now = time.time()
    if now - session.last_hourly_snapshot < 3600:
        return

    session.last_hourly_snapshot = now

    # Swap DB to session's DB
    original_db = db_module._instance
    db_module._instance = session.db

    try:
        db = session.db
        hour_label = datetime.now().strftime("%Y-%m-%d %H:00")
        hour_ago = now - 3600

        # Query fills in the last hour
        fills = db._get_conn().execute(
            "SELECT COALESCE(SUM(CASE WHEN side='yes' THEN shares*price ELSE shares*clob_cost END),0), COUNT(*) "
            "FROM fills WHERE ts >= ?", (hour_ago,)
        ).fetchone()
        total_bought = fills[0]
        num_fills = fills[1]

        # Query unwinds
        unwinds = db._get_conn().execute(
            "SELECT COALESCE(SUM(usd_value),0), COALESCE(SUM(pnl),0), COUNT(*) "
            "FROM unwinds WHERE ts >= ?", (hour_ago,)
        ).fetchone()
        total_sold = unwinds[0]
        realized_pnl = unwinds[1]
        num_unwinds = unwinds[2]

        # Stop losses
        stop_row = db._get_conn().execute(
            "SELECT COALESCE(SUM(loss_usd),0), COUNT(*) FROM stop_losses WHERE ts >= ?",
            (hour_ago,)
        ).fetchone()
        num_stop_losses = stop_row[1]

        # Danger cancels
        danger_row = db._get_conn().execute(
            "SELECT COUNT(*) FROM orders_cancelled WHERE ts >= ? AND reason='danger'",
            (hour_ago,)
        ).fetchone()
        num_danger = danger_row[0]

        # Position value
        total_pos = 0.0
        positions = db._get_conn().execute("SELECT * FROM positions").fetchall()
        for p in positions:
            total_pos += p[2] * p[3] + p[5] * p[6]  # yes_shares*yes_avg + no_shares*no_avg

        # Reward estimate
        est_reward = 0.0
        reward_stats = db._get_conn().execute("SELECT data FROM reward_market_stats").fetchall()
        import json
        for r in reward_stats:
            d = json.loads(r[0])
            est_reward += d.get("est_reward_usd", 0)

        unrealized = total_pos - total_bought if total_bought > 0 else 0

        db.log_hourly_snapshot(
            hour_label=hour_label,
            num_markets=len(getattr(session, 'order_managers', {})),
            total_bought_usd=total_bought,
            total_sold_usd=total_sold,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized,
            total_position_usd=total_pos,
            est_reward_usd=est_reward,
            est_reward_rate_hr=est_reward,
            num_fills=num_fills,
            num_unwinds=num_unwinds,
            num_stop_losses=num_stop_losses,
            num_danger_cancels=num_danger,
            avg_uptime_pct=100.0,
            config_json="{}",
        )

        log.info(
            f"[{session.strategy.name}] HOURLY SNAPSHOT | "
            f"bought=${total_bought:.2f} sold=${total_sold:.2f} "
            f"pos=${total_pos:.2f} rew=${est_reward:.2f} "
            f"fills={num_fills} unwinds={num_unwinds} stop={num_stop_losses} "
            f"danger={num_danger}"
        )
    except Exception as e:
        log.debug(f"Hourly snapshot error for {session.strategy.name}: {e}")
    finally:
        db_module._instance = original_db


def print_comparison(sessions: list[PaperSession], elapsed_hrs: float):
    """Print side-by-side strategy comparison."""
    print(f"\n{'='*80}")
    print(f"  PAPER TRADING REPORT — {elapsed_hrs:.1f} hours elapsed")
    print(f"{'='*80}")
    print(
        f"  {'Strategy':<12s} | {'Balance':>8s} | {'Live':>5s} | "
        f"{'Filled':>6s} | {'Tokens':>6s} | {'Status':>10s}"
    )
    print(f"  {'-'*60}")

    for s in sessions:
        summary = s.paper_client.get_summary()
        token_count = sum(
            v for v in summary["token_balances"].values() if v > 1
        )
        status = "ACTIVE" if summary["live_orders"] > 0 else "IDLE"
        print(
            f"  {summary['label']:<12s} | "
            f"${summary['usdc_balance']:>7.2f} | "
            f"{summary['live_orders']:>5d} | "
            f"{summary['filled_orders']:>6d} | "
            f"{token_count:>6.0f} | "
            f"{status:>10s}"
        )

    print(f"{'='*80}\n")


def print_final_report(sessions: list[PaperSession], duration_secs: float):
    """Print comprehensive final comparison."""
    hours = duration_secs / 3600

    print(f"\n{'#'*80}")
    print(f"  FINAL PAPER TRADING RESULTS — {hours:.1f} hours")
    print(f"{'#'*80}\n")

    results = []
    for s in sessions:
        summary = s.paper_client.get_summary()
        token_value = sum(
            shares * 0.5  # rough estimate at midpoint
            for shares in summary["token_balances"].values()
            if shares > 1
        )
        initial = s.strategy.initial_balance
        current = summary["usdc_balance"] + token_value
        net_pnl = current - initial

        # Query the paper DB for fill/unwind counts
        try:
            db = sqlite3.connect(s.db_path)
            fills = db.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
            unwinds = db.execute("SELECT COUNT(*) FROM unwinds").fetchone()[0]
            db.close()
        except Exception:
            fills = summary["filled_orders"]
            unwinds = 0

        results.append({
            "name": s.strategy.name,
            "initial": initial,
            "usdc": summary["usdc_balance"],
            "token_value": token_value,
            "net_pnl": net_pnl,
            "fills": fills,
            "unwinds": unwinds,
            "live_orders": summary["live_orders"],
            "pnl_per_hour": net_pnl / hours if hours > 0 else 0,
        })

    # Sort by net P&L
    results.sort(key=lambda r: r["net_pnl"], reverse=True)

    print(
        f"  {'Strategy':<12s} | {'Initial':>8s} | {'USDC':>8s} | "
        f"{'Tokens':>8s} | {'Net P&L':>8s} | {'$/hr':>7s} | "
        f"{'Fills':>5s} | {'Unwinds':>7s}"
    )
    print(f"  {'-'*80}")
    for r in results:
        winner = " <-- WINNER" if r == results[0] else ""
        print(
            f"  {r['name']:<12s} | "
            f"${r['initial']:>7.0f} | "
            f"${r['usdc']:>7.2f} | "
            f"${r['token_value']:>7.2f} | "
            f"${r['net_pnl']:>+7.2f} | "
            f"${r['pnl_per_hour']:>+6.2f} | "
            f"{r['fills']:>5d} | "
            f"{r['unwinds']:>7d}"
            f"{winner}"
        )

    print(f"\n{'#'*80}\n")
    return results


def main():
    parser = argparse.ArgumentParser(description="Paper Trading Simulator")
    parser.add_argument(
        "--duration", default="6h",
        help="How long to run (e.g., 6h, 30m, 120s)"
    )
    parser.add_argument(
        "--balance", type=float, default=1000.0,
        help="Starting USDC balance per strategy"
    )
    args = parser.parse_args()

    duration_secs = parse_duration(args.duration)
    for s in STRATEGIES:
        s.initial_balance = args.balance

    log.info(f"Paper Trading Simulator starting")
    log.info(f"  Duration: {args.duration} ({duration_secs}s)")
    log.info(f"  Balance: ${args.balance:.0f} per strategy")
    log.info(f"  Strategies: {[s.name for s in STRATEGIES]}")

    # Create real client (shared for read-only API access)
    real_client = create_real_client()

    # Create shared order book cache
    from paper_client import CachedOrderBookProvider
    book_cache = CachedOrderBookProvider(real_client, ttl_secs=25.0)

    # Create sessions
    sessions = []
    for strategy in STRATEGIES:
        session = PaperSession(strategy, real_client, book_cache)
        sessions.append(session)
        log.info(f"  Session '{strategy.name}' initialized (${strategy.initial_balance:.0f})")

    # Discover reward markets (shared across strategies)
    from market import get_rewards_markets
    log.info("Fetching reward markets...")
    try:
        all_markets = get_rewards_markets(limit=50)  # fetch all, strategies select subsets
        log.info(f"Found {len(all_markets)} eligible reward markets")
    except Exception as e:
        log.error(f"Failed to fetch markets: {e}")
        sys.exit(1)

    if not all_markets:
        log.error("No eligible markets found. Exiting.")
        sys.exit(1)

    # Each session has its own reward_tracker (created in PaperSession.__init__)

    # Shutdown handling
    shutdown = False

    def _handle_signal(signum, frame):
        nonlocal shutdown
        shutdown = True
        log.info("Shutdown requested...")

    signal.signal(signal.SIGINT, _handle_signal)

    # ── Main loop ────────────────────────────────────────────────────
    start_time = time.time()
    last_report = start_time
    last_market_refresh = time.time()  # initial fetch already done above
    cycle = 0

    import config
    refresh_interval = getattr(config, "MARKET_REFRESH_SECS", 1800)
    cycle_interval = getattr(config, "ORDER_REFRESH_SECS", 30)

    log.info("Starting paper trading cycles...")

    while not shutdown and (time.time() - start_time) < duration_secs:
        cycle += 1
        cycle_start = time.time()

        # Refresh markets periodically
        if time.time() - last_market_refresh >= refresh_interval:
            try:
                book_cache.invalidate()
                all_markets = get_rewards_markets(limit=50)
                last_market_refresh = time.time()
                log.info(f"Market refresh: {len(all_markets)} eligible markets")
            except Exception as e:
                log.warning(f"Market refresh failed: {e}")

        # Select markets per strategy and run cycle
        for session in sessions:
            try:
                # Each strategy may want different # of markets
                max_mkts = session.strategy.config_overrides.get(
                    "MAX_MARKETS", 5
                )
                strategy_markets = all_markets[:int(max_mkts)]

                if session.strategy.custom_cycle == "passive":
                    run_passive_cycle(session, strategy_markets, session.reward_tracker)
                else:
                    run_one_cycle(session, strategy_markets, session.reward_tracker)
            except Exception as e:
                log.error(f"[{session.strategy.name}] Cycle error: {e}")

        # Hourly progress report + snapshots
        elapsed = time.time() - start_time
        if elapsed - (last_report - start_time) >= 3600:
            print_comparison(sessions, elapsed / 3600)
            last_report = time.time()
            # Log hourly snapshots per session
            for s in sessions:
                log_hourly_snapshot(s, s.reward_tracker)

        # Brief status every 10 cycles
        if cycle % 10 == 0:
            elapsed_min = (time.time() - start_time) / 60
            remaining_min = (duration_secs - elapsed) / 60
            log.info(
                f"Cycle {cycle} | {elapsed_min:.0f}m elapsed | "
                f"{remaining_min:.0f}m remaining"
            )
            for s in sessions:
                summary = s.paper_client.get_summary()
                log.info(
                    f"  [{summary['label']}] "
                    f"balance=${summary['usdc_balance']:.2f} | "
                    f"live={summary['live_orders']} | "
                    f"filled={summary['filled_orders']}"
                )

        # Sleep until next cycle
        cycle_elapsed = time.time() - cycle_start
        sleep_time = max(1, cycle_interval - cycle_elapsed)
        for _ in range(int(sleep_time)):
            if shutdown:
                break
            time.sleep(1)

    # ── Final report ─────────────────────────────────────────────────
    total_duration = time.time() - start_time
    results = print_final_report(sessions, total_duration)

    log.info("Paper trading complete.")
    return results


if __name__ == "__main__":
    main()
