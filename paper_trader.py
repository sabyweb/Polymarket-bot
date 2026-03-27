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
        fills = db.conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN side='yes' THEN shares*price ELSE shares*clob_cost END),0), COUNT(*) "
            "FROM fills WHERE ts >= ?", (hour_ago,)
        ).fetchone()
        total_bought = fills[0]
        num_fills = fills[1]

        # Query unwinds
        unwinds = db.conn.execute(
            "SELECT COALESCE(SUM(usd_value),0), COALESCE(SUM(pnl),0), COUNT(*) "
            "FROM unwinds WHERE ts >= ?", (hour_ago,)
        ).fetchone()
        total_sold = unwinds[0]
        realized_pnl = unwinds[1]
        num_unwinds = unwinds[2]

        # Stop losses
        stop_row = db.conn.execute(
            "SELECT COALESCE(SUM(loss_usd),0), COUNT(*) FROM stop_losses WHERE ts >= ?",
            (hour_ago,)
        ).fetchone()
        num_stop_losses = stop_row[1]

        # Danger cancels
        danger_row = db.conn.execute(
            "SELECT COUNT(*) FROM orders_cancelled WHERE ts >= ? AND reason='danger'",
            (hour_ago,)
        ).fetchone()
        num_danger = danger_row[0]

        # Position value
        total_pos = 0.0
        positions = db.conn.execute("SELECT * FROM positions").fetchall()
        for p in positions:
            total_pos += p[2] * p[3] + p[5] * p[6]  # yes_shares*yes_avg + no_shares*no_avg

        # Reward estimate
        est_reward = 0.0
        reward_stats = db.conn.execute("SELECT data FROM reward_market_stats").fetchall()
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

    # Create a shared reward tracker for Q-score estimation
    from reward_tracker import RewardTracker
    reward_tracker = RewardTracker()

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
                strategy_markets = all_markets[:max_mkts]
                run_one_cycle(session, strategy_markets, reward_tracker)
            except Exception as e:
                log.error(f"[{session.strategy.name}] Cycle error: {e}")

        # Hourly progress report + snapshots
        elapsed = time.time() - start_time
        if elapsed - (last_report - start_time) >= 3600:
            print_comparison(sessions, elapsed / 3600)
            last_report = time.time()
            # Log hourly snapshots per session
            for s in sessions:
                log_hourly_snapshot(s, reward_tracker)

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
