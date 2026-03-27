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
            "MIN_SCORE_THRESHOLD": 30,
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
            "MIN_SCORE_THRESHOLD": 40,
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
    last_market_refresh = 0
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

        # Hourly progress report
        elapsed = time.time() - start_time
        if elapsed - (last_report - start_time) >= 3600:
            print_comparison(sessions, elapsed / 3600)
            last_report = time.time()

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
