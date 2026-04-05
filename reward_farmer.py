#!/usr/bin/env python3
"""Reward Farmer — production bot for Polymarket LP reward farming.

Orchestrator: delegates order lifecycle and dump management to extracted modules.
Handles market discovery, agent allocations, startup, shutdown, and main loop.

Usage:
    python reward_farmer.py                    # normal mode
    python reward_farmer.py --dry-run          # log only, no real orders
    python reward_farmer.py --dry-run --duration 10m  # timed dry run
"""

import argparse
import json
import logging
import os
import signal
import time
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("reward_farmer")

from config import cfg
from models import OrderSlot, MarketState
from market_discovery import fetch_all_reward_markets, get_merged_book
from order_lifecycle import OrderLifecycle
from dump_manager import DumpManager

# Config accessors (hot-reloadable)
def SHARES_PER_SIDE(): return cfg("RF_SHARES_PER_SIDE")
def MIN_DAILY_RATE(): return cfg("RF_MIN_DAILY_RATE")
def MAX_LIQUIDITY(): return cfg("RF_MAX_LIQUIDITY")
def MAX_MARKETS(): return cfg("RF_MAX_MARKETS")
def CYCLE_SECS(): return cfg("RF_CYCLE_SECS")
def BATCH_SIZE(): return cfg("RF_BATCH_SIZE")
def MARKET_REFRESH_SECS(): return cfg("RF_MARKET_REFRESH_SECS")
def PLACEMENT_TICKS_INSIDE(): return cfg("RF_PLACEMENT_TICKS_INSIDE")
def MAX_COST_PER_MARKET(): return cfg("RF_MAX_COST_PER_MARKET")
def MAX_TOTAL_EXPOSURE(): return cfg("RF_MAX_TOTAL_EXPOSURE")


class RewardFarmer:
    """Production reward farming bot — orchestrator."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

        # Create CLOB client
        from config import (
            CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE,
            HOST, PRIVATE_KEY, CHAIN_ID, SIGNATURE_TYPE, FUNDER,
        )
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        from rate_limiter import RateLimitedClient

        creds = ApiCreds(
            api_key=CLOB_API_KEY, api_secret=CLOB_SECRET,
            api_passphrase=CLOB_PASS_PHRASE,
        )
        raw = ClobClient(
            host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
            signature_type=SIGNATURE_TYPE, funder=FUNDER, creds=creds,
        )
        self.client = RateLimitedClient(raw)
        log.info("Connected to Polymarket CLOB API")

        # Position tracker
        from state import PositionStore
        self.positions = PositionStore()
        log.info("Loaded positions from SQLite")

        # Reward tracker
        from reward_tracker import RewardTracker
        self.rewards = RewardTracker()

        # Database
        from database import get_db
        self.db = get_db()

        # Startup reconciliation
        self._reconcile_on_startup()

        # Market state
        self.markets: dict[str, MarketState] = {}
        self.all_market_data: list[dict] = []
        self._market_lock = threading.Lock()

        # Extracted modules — share the markets dict reference
        self.order_lifecycle = OrderLifecycle(
            client=self.client, db=self.db, positions=self.positions,
            rewards=self.rewards, markets=self.markets, dry_run=dry_run,
        )
        self.dump_mgr = DumpManager(
            client=self.client, db=self.db, positions=self.positions,
            cancel_fn=self.order_lifecycle.cancel_order, dry_run=dry_run,
        )
        self.order_lifecycle.set_dump_manager(self.dump_mgr)

        # Cycle state
        self.cycle_count = 0
        self._shutdown = False
        self._agent_mode = False
        self._pending_market_data: list[dict] | None = None
        self._last_market_refresh = 0.0
        self._last_reward_log = 0.0
        self._alloc_mtime = 0.0

    # ── Startup ─────────────────────────────────────────────────────

    def _reconcile_on_startup(self):
        """Cancel all existing orders on startup."""
        if self.dry_run:
            log.info("[DRY] Skipping startup reconciliation")
            return
        try:
            existing = self.client.get_orders() or []
            if existing:
                cancelled = 0
                for order in existing:
                    oid = order.get("id", "")
                    try:
                        self.client.cancel(oid)
                        cancelled += 1
                    except Exception as e:
                        log.debug(f"Cancel failed for {oid[:16]}: {e}")
                log.info(f"Startup: cancelled {cancelled}/{len(existing)} existing orders")
            else:
                log.info("No existing orders found — starting clean.")
        except Exception as e:
            log.warning(f"Startup order check failed: {e}")

    def _reconcile_positions(self):
        """Verify tracked positions against actual exchange balances."""
        if self.dry_run:
            return
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            corrections = 0
            for cid, ms in self.markets.items():
                for side, tid in [("yes", ms.yes_tid), ("no", ms.no_tid)]:
                    tracked_shares = self.positions.get_shares(cid, side)
                    if tracked_shares < 0.5:
                        continue
                    try:
                        bal = self.client.get_balance_allowance(
                            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
                        )
                        actual = float(bal.get("balance", 0)) / 1e6
                        if abs(actual - tracked_shares) > 1.0:
                            log.warning(f"Position mismatch {side.upper()} {ms.question[:25]}: tracked={tracked_shares:.0f} actual={actual:.0f}")
                            self.positions.set_shares(cid, side, actual)
                            corrections += 1
                    except Exception as e:
                        log.debug(f"Balance check failed for {ms.question[:20]} {side}: {e}")
            if corrections:
                log.info(f"Reconciled {corrections} position mismatches")
            else:
                log.info("All positions match exchange balances")
        except Exception as e:
            log.warning(f"Position reconciliation failed: {e}")

    def _restore_dump_states(self):
        """Restore dump states from DB after crash/restart."""
        if self.dry_run:
            return
        saved = self.db.load_all_dump_states()
        if not saved:
            return
        restored = 0
        cleaned = 0
        for (cid, side), state in saved.items():
            ms = self.markets.get(cid)
            if not ms:
                self.db.delete_dump_state(cid, side)
                cleaned += 1
                continue
            elapsed_min = (time.time() - state["started_at"]) / 60.0
            if elapsed_min >= cfg("RF_DUMP_ABANDON_MINS"):
                self.db.delete_dump_state(cid, side)
                cleaned += 1
                continue
            ms.dump_state[side] = state
            saved_oid = state.get("dump_order_id", "")
            ms.dump_orders[side] = saved_oid if saved_oid else None
            restored += 1
            log.info(f"Restored dump {side.upper()} {state['shares']:.0f}sh @ {state['fill_price']:.4f} ({elapsed_min:.0f}m elapsed) | {ms.question[:30]}")
        if restored or cleaned:
            log.info(f"Dump state recovery: restored {restored}, cleaned {cleaned}")

    # ── Market Management ───────────────────────────────────────────

    def refresh_markets(self):
        """Discover and filter reward markets (blocking, for startup)."""
        log.info("Refreshing reward markets...")
        self.all_market_data = fetch_all_reward_markets()
        self._apply_market_changes()

    def _check_allocation_update(self):
        """Check if allocation file changed since last read. Apply if so."""
        alloc_path = os.path.join(os.path.dirname(__file__) or ".", "market_allocations.json")
        try:
            if not os.path.exists(alloc_path):
                return
            mtime = os.path.getmtime(alloc_path)
            if mtime <= self._alloc_mtime:
                return
            self._alloc_mtime = mtime
            log.info("Allocation file updated — applying changes")
            self._apply_market_changes()
        except Exception as e:
            log.debug(f"Allocation check error: {e}")

    def _load_allocations(self) -> list[dict] | None:
        """Load oversight agent allocations if available and fresh."""
        alloc_path = os.path.join(os.path.dirname(__file__) or ".", "market_allocations.json")
        try:
            if not os.path.exists(alloc_path):
                return None
            with open(alloc_path) as f:
                data = json.load(f)
            generated_at = data.get("generated_at", "")
            if generated_at:
                from datetime import datetime, timezone, timedelta
                gen_dt = datetime.fromisoformat(generated_at)
                age = datetime.now(timezone.utc) - gen_dt
                if age > timedelta(hours=cfg("RF_ALLOCATION_TTL_HOURS")):
                    log.debug("Allocation file stale — skipping")
                    return None
            deploy = [m for m in data.get("markets", []) if m.get("action") == "deploy"]
            if not deploy:
                return None
            log.info(f"Loaded {len(deploy)} market allocations from oversight agent")
            return deploy
        except Exception as e:
            log.debug(f"Allocation load failed: {e}")
            return None

    def _apply_market_changes(self):
        """Apply market data. Uses agent allocations if available, else default filtering."""
        agent_alloc = self._load_allocations()
        if agent_alloc is not None:
            self._agent_mode = True
            raw_by_cid = {m["condition_id"]: m for m in self.all_market_data}
            eligible = []
            for alloc in agent_alloc:
                cid = alloc["condition_id"]
                m = raw_by_cid.get(cid)
                if m:
                    m["_agent_shares"] = alloc.get("shares_per_side", SHARES_PER_SIDE())
                    eligible.append(m)
                else:
                    # Agent discovered a market the bot doesn't have — fetch from CLOB
                    try:
                        import requests
                        mkt_resp = requests.get(f"https://clob.polymarket.com/markets/{cid}", timeout=10)
                        if mkt_resp.status_code == 200:
                            mkt = mkt_resp.json()
                            tokens_data = mkt.get("tokens", [])
                            if len(tokens_data) >= 2:
                                fetched = {
                                    "condition_id": cid,
                                    "question": mkt.get("question", ""),
                                    "token_ids": [tokens_data[0]["token_id"], tokens_data[1]["token_id"]],
                                    "yes_price": float(tokens_data[0].get("price", 0.5)),
                                    "daily_rate": alloc.get("daily_rate", 0),
                                    "min_size": alloc.get("min_size", float(mkt.get("minimum_order_size") or 50)),
                                    "max_spread": alloc.get("max_spread", float(mkt.get("minimum_tick_size") or 0.01) * 4.5),
                                    "tick_size": float(mkt.get("minimum_tick_size") or 0.01),
                                    "liquidity": 0,
                                    "volume_24h": 0,
                                    "end_date_iso": mkt.get("end_date_iso", ""),
                                    "_agent_shares": alloc.get("shares_per_side", SHARES_PER_SIDE()),
                                }
                                eligible.append(fetched)
                                log.info(f"Agent market fetched from CLOB: {fetched['question'][:40]}")
                    except Exception as e:
                        log.debug(f"Agent market {cid[:16]} CLOB fetch failed: {e}")

            # Minimal validation
            validated = []
            for m in eligible:
                tokens = m.get("token_ids", [])
                if len(tokens) < 2:
                    continue
                yes_p = m.get("yes_price") or 0.5
                if yes_p < 0.02 or yes_p > 0.98:
                    continue
                validated.append(m)

            log.info(f"Using agent allocations: {len(validated)}/{len(eligible)} markets (after validation)")
            self._update_market_states(validated)
            return

        # If agent was previously active but allocation is now stale/missing, keep current markets
        if self._agent_mode:
            log.warning(f"Agent allocation stale/missing — keeping current {len(self.markets)} markets")
            return

        # Default filtering (only when agent has NEVER been active)
        raw = self.all_market_data
        eligible = []
        for m in raw:
            if m["daily_rate"] < MIN_DAILY_RATE():
                continue
            if MAX_LIQUIDITY() > 0 and m.get("liquidity", 0) > MAX_LIQUIDITY():
                continue
            tokens = m.get("token_ids", [])
            if len(tokens) < 2:
                continue
            eligible.append(m)

        eligible.sort(key=lambda x: x["daily_rate"] / max(x.get("liquidity", 1), 1), reverse=True)
        eligible = eligible[:MAX_MARKETS()]
        self._update_market_states(eligible)

    def _update_market_states(self, eligible: list[dict]):
        """Update active market set. Drops removed markets, adds new, updates sizing."""
        new_cids = {m["condition_id"] for m in eligible}
        old_cids = set(self.markets.keys())

        # Remove dropped markets
        for cid in old_cids - new_cids:
            ms = self.markets[cid]
            log.info(f"Dropping market: {ms.question[:40]}")
            for side in ["yes", "no"]:
                oid = ms.orders[side].order_id
                if oid:
                    if self.order_lifecycle.cancel_order(oid, reason="market_removed"):
                        ms.orders[side].order_id = None
                    else:
                        log.warning(f"Orphaned order {oid[:16]} — cancel failed on market drop")
            for side in ["yes", "no"]:
                shares = self.positions.get_shares(cid, side)
                if shares > 1:
                    self.dump_mgr.dump_position(ms, side, shares)
            del self.markets[cid]

        # Add new markets + update sizing for existing ones
        for m in eligible:
            cid = m["condition_id"]
            if cid in self.markets:
                # Update agent sizing for existing markets
                new_shares = float(m.get("_agent_shares", 0))
                if new_shares > 0 and new_shares != self.markets[cid].agent_shares:
                    log.info(f"Resizing {self.markets[cid].question[:30]}: {self.markets[cid].agent_shares:.0f} → {new_shares:.0f}sh")
                    self.markets[cid].agent_shares = new_shares
            else:
                self.markets[cid] = MarketState(
                    cid=cid,
                    question=m["question"],
                    yes_tid=m["token_ids"][0],
                    no_tid=m["token_ids"][1],
                    daily_rate=m["daily_rate"],
                    max_spread=m["max_spread"],
                    min_size=m["min_size"],
                    tick_size=m.get("tick_size", 0.01),
                    yes_price=m.get("yes_price"),
                    agent_shares=float(m.get("_agent_shares", 0)),
                )
                self.positions.register_market(cid, m["question"])

        log.info(f"Active markets: {len(self.markets)}")
        for i, (cid, ms) in enumerate(self.markets.items()):
            if i < 10:
                log.info(f"  #{i+1} {ms.question[:45]} | ${ms.daily_rate:.0f}/d | {ms.agent_shares:.0f}sh")

        self._last_market_refresh = time.time()

    # ── Core Cycle ──────────────────────────────────────────────────

    def run_cycle(self):
        """One cycle: check dumps → detect fills → place orders → record rewards."""
        self.cycle_count += 1
        self.order_lifecycle.cycle_count = self.cycle_count
        self.order_lifecycle.capital_exhausted = False

        # Step 1: Fetch exchange orders
        if self.dry_run:
            open_ids = set()
        else:
            try:
                exchange_orders = self.client.get_orders() or []
            except Exception as e:
                log.error(f"get_orders failed: {e}")
                return
            open_ids = {o["id"] for o in exchange_orders}

        # Step 2: Check dump SELL orders
        self.dump_mgr.check_dump_fills(self.markets, open_ids)

        # Step 2.5: Reprice active dumps
        self.dump_mgr.reprice_active_dumps(self.markets, open_ids)

        # Step 3: Detect BUY fills
        self.order_lifecycle.detect_fills(open_ids)

        # Step 4: Place orders on priority batch
        market_list = list(self.markets.values())
        if not market_list:
            return
        batch = self.order_lifecycle.get_priority_batch(market_list)
        for ms in batch:
            self.order_lifecycle.place_orders_for_market(ms)

        # Step 5: Record rewards
        for ms in market_list:
            has_yes = ms.orders["yes"].order_id is not None
            has_no = ms.orders["no"].order_id is not None
            if not has_yes and not has_no:
                continue
            self.rewards.get_or_create(
                condition_id=ms.cid, question=ms.question,
                daily_rate=ms.daily_rate, max_spread=ms.max_spread,
            )
            mid = ms.midpoint if ms.midpoint > 0 else (
                (ms.orders["yes"].price + ms.orders["no"].price) / 2
                if ms.orders["yes"].price > 0 and ms.orders["no"].price > 0 else 0
            )
            self.rewards.record_cycle(
                condition_id=ms.cid,
                has_yes_order=has_yes, has_no_order=has_no,
                bid_price=ms.orders["yes"].price if has_yes else 0,
                ask_price=ms.orders["no"].price if has_no else 0,
                inventory_usd=0.0, cooldown_active=False, skew_active=False,
                cycle_duration_secs=CYCLE_SECS(),
                midpoint=mid,
                bid_size=ms.orders["yes"].shares if has_yes else 0,
                ask_size=ms.orders["no"].shares if has_no else 0,
            )

        if self.cycle_count % 5 == 0:
            self.rewards._save()

    # ── Main Loop ───────────────────────────────────────────────────

    def run(self, duration_secs: int = 0):
        """Main loop."""
        def _sig(signum, frame):
            self._shutdown = True
            log.info("Shutdown requested...")
        signal.signal(signal.SIGINT, _sig)

        self.refresh_markets()
        if not self.markets:
            log.error("No eligible markets found. Exiting.")
            return

        self._reconcile_positions()
        self._restore_dump_states()

        start = time.time()
        last_status = time.time()
        log.info(f"Starting reward farming | {len(self.markets)} markets | dry_run={self.dry_run}")

        while not self._shutdown:
            if duration_secs > 0 and (time.time() - start) >= duration_secs:
                break

            t0 = time.time()

            from config import BotConfig
            BotConfig.instance().check_and_reload()

            # Background market refresh
            if time.time() - self._last_market_refresh >= MARKET_REFRESH_SECS():
                if not hasattr(self, '_refresh_thread') or not self._refresh_thread.is_alive():
                    def _bg_refresh():
                        try:
                            new_data = fetch_all_reward_markets()
                            with self._market_lock:
                                self._pending_market_data = new_data
                        except Exception as e:
                            log.warning(f"Background market refresh failed: {e}")
                    self._refresh_thread = threading.Thread(target=_bg_refresh, daemon=True)
                    self._refresh_thread.start()
                    self._last_market_refresh = time.time()

            # Apply pending market data
            with self._market_lock:
                if self._pending_market_data is not None:
                    self.all_market_data = self._pending_market_data
                    self._pending_market_data = None
                    apply_now = True
                else:
                    apply_now = False
            if apply_now:
                self._apply_market_changes()

            # Check allocation file every cycle
            self._check_allocation_update()

            # Run cycle
            try:
                self.run_cycle()
            except Exception as e:
                log.error(f"Cycle error: {e}")
            cycle_duration = time.time() - t0

            # Metrics every 10 cycles
            if self.cycle_count % 10 == 0 and self.cycle_count > 0:
                on_book = sum(1 for ms in self.markets.values()
                              if ms.orders["yes"].order_id or ms.orders["no"].order_id)
                active_dumps = sum(1 for ms in self.markets.values()
                                   if ms.dump_orders["yes"] or ms.dump_orders["no"])
                log.info(f"[metrics] cycle={self.cycle_count} | {cycle_duration:.1f}s | on_book={on_book}/{len(self.markets)} | dumps={active_dumps}")

            # Hourly reward log
            if time.time() - self._last_reward_log >= 3600:
                self._last_reward_log = time.time()
                self.rewards.maybe_log_hourly(self.all_market_data[:MAX_MARKETS()])
                self.rewards._last_hourly_log = 0

            # Status every 5 min
            if time.time() - last_status >= 300:
                elapsed = (time.time() - start) / 60
                on_book = sum(1 for ms in self.markets.values()
                              if ms.orders["yes"].order_id or ms.orders["no"].order_id)
                log.info(f"Cycle {self.cycle_count} | {elapsed:.0f}m | {on_book}/{len(self.markets)} on-book | dry_run={self.dry_run}")
                last_status = time.time()

            # Sleep
            sleep_time = max(0, CYCLE_SECS() - (time.time() - t0))
            if sleep_time > 0 and not self._shutdown:
                for _ in range(int(sleep_time)):
                    if self._shutdown:
                        break
                    time.sleep(1)

        self._shutdown_cleanup()

    def _shutdown_cleanup(self):
        """Cancel ALL orders, save state."""
        log.info("Shutting down...")
        for ms in self.markets.values():
            for side in ["yes", "no"]:
                oid = ms.orders[side].order_id
                if oid and oid != "dry_yes" and oid != "dry_no":
                    self.order_lifecycle.cancel_order(oid, reason="shutdown")
                dump_oid = ms.dump_orders[side]
                if dump_oid:
                    self.order_lifecycle.cancel_order(dump_oid, reason="shutdown_dump")
        self.rewards._save()
        log.info("All orders cancelled. Shutdown complete.")


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

def parse_duration(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("h"):
        return int(float(s[:-1]) * 3600)
    if s.endswith("m"):
        return int(float(s[:-1]) * 60)
    return int(s.rstrip("s"))


def main():
    parser = argparse.ArgumentParser(description="Reward Farmer — Polymarket LP reward farming bot")
    parser.add_argument("--dry-run", action="store_true", help="Log only, no real orders")
    parser.add_argument("--duration", default="0", help="Run duration (e.g. 10m, 1h, 6h). 0 = indefinite")
    args = parser.parse_args()

    duration = parse_duration(args.duration) if args.duration != "0" else 0

    log.info("Reward Farmer starting")
    log.info(f"  Dry run: {args.dry_run}")
    log.info(f"  Duration: {'indefinite' if duration == 0 else f'{duration}s'}")
    log.info(f"  Strategy: {SHARES_PER_SIDE()}sh/side, {PLACEMENT_TICKS_INSIDE()} tick inside edge")
    log.info(f"  Markets: max {MAX_MARKETS()}, rate >= ${MIN_DAILY_RATE()}/d, liq < ${MAX_LIQUIDITY()}")

    bot = RewardFarmer(dry_run=args.dry_run)
    bot.run(duration_secs=duration)


if __name__ == "__main__":
    main()
