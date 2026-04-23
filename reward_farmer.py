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


# ── Runtime safety guardrails (applied after allocation, before placement) ──
# All thresholds are live-capital bounds; the allocator/learning loop is
# unaware of them (correct layering: allocation decides intent, farmer
# enforces execution-time safety). Fail-open on missing data so a DB hiccup
# can never halt trading — only the explicit kill-switch triggers halt.
MAX_NOTIONAL_RATIO = 2.0                 # Σ live notional / total_capital cap
CLUSTER_NOTIONAL_LIMIT_FRAC = 0.5        # fraction of total_capital per fill cluster
MAX_DAILY_LOSS_FRAC = 0.1                # kill-switch: realized loss / total_capital
CRITICAL_CF_THRESHOLD = 0.01             # kill-switch: CF floor
FILL_RATE_SPIKE_FACTOR = 3.0             # kill-switch: short-window / rolling-avg
GUARDRAIL_FILLRATE_SHORT_SECS = 3600     # 1h fill-count window
GUARDRAIL_FILLRATE_BASELINE_SECS = 21600 # 6h baseline window
GUARDRAIL_FILLRATE_MIN_BASELINE = 5      # require ≥ N baseline fills before ratio fires


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
        self._last_reconcile = 0.0
        self._last_exchange_sync = 0.0
        # Issue 3: Cross-market fill storm detector
        self._global_fill_times: list[float] = []  # timestamps of all fills across all markets
        self._fill_storm_until: float = 0.0  # if > time.time(), halt new placements

        # Runtime safety guardrails state (see module-level constants).
        # Once the kill switch trips, it stays on until the operator
        # restarts the process — deliberate: the trigger conditions
        # (CF collapse, fill storm, daily-loss breach) all benefit from
        # human eyes-on before re-entry.
        self._kill_switch_active: bool = False
        self._kill_switch_reason: str = ""
        self._kill_switch_triggered_at: float = 0.0

    # ── Startup ─────────────────────────────────────────────────────

    def _reconcile_on_startup(self):
        """Check all existing orders for partial fills, then cancel and purge.

        Previous behaviour: cancel everything blindly, losing any partial
        fills that happened while the bot was offline. Now:
        1. Load tracked orders from DB (order_id → condition_id, side, etc.)
        2. Get exchange orders → build open_ids
        3. For each DB-tracked order, check if it was filled:
           - Still on exchange: read size_matched from get_order()
           - Gone from exchange: also check via get_order() (may have filled)
        4. Record fills (buy) or unwinds (dump) in PositionStore
        5. Cancel all remaining exchange orders
        6. Purge DB
        """
        if self.dry_run:
            log.info("[DRY] Skipping startup reconciliation")
            return

        # Step 1: Load tracked orders from DB BEFORE purging
        tracked = {}
        try:
            db_orders = self.db.load_active_orders()
            tracked = {o["order_id"]: o for o in db_orders}
            if tracked:
                log.info(f"Startup: {len(tracked)} tracked orders in DB")
        except Exception as e:
            log.debug(f"Load active orders failed: {e}")

        # Step 2: Get exchange orders
        existing = []
        try:
            existing = self.client.get_orders() or []
        except Exception as e:
            log.warning(f"Startup get_orders failed: {e}")

        open_ids = {o.get("id", "") for o in existing}

        # Step 3: Check ALL tracked orders for fills (on-exchange + off-exchange)
        fills_recovered = 0
        unwinds_recovered = 0

        for oid, info in tracked.items():
            cid = info.get("condition_id", "")
            side = info.get("side", "")
            order_type = info.get("order_type", "buy")
            db_price = float(info.get("price", 0))

            if not cid or not side:
                continue

            # Check order status via API
            try:
                status = self.client.get_order(oid)
                matched = float(status.get("size_matched", 0))
                order_status = status.get("status", "")
                api_price = float(status.get("price", 0))
            except Exception as e:
                log.debug(f"Startup order check failed {oid[:16]}: {e}")
                continue

            if matched <= 0:
                continue

            # Determine price (prefer API, fall back to DB)
            fill_price = api_price if api_price > 0 else db_price

            if order_type == "buy":
                # Convert CLOB price to YES-equiv for PositionStore
                from price import to_yes_equiv
                yes_equiv_price = to_yes_equiv(fill_price, side)

                self.positions.register_market(cid, f"startup-recovery-{cid[:12]}")
                self.positions.record_fill(cid, side, matched, yes_equiv_price)
                self.db.log_fill(
                    condition_id=cid, question=f"startup-recovery",
                    side=side, fill_type="STARTUP",
                    shares=matched, price=yes_equiv_price,
                    clob_cost=fill_price, usd_value=matched * fill_price,
                )
                fills_recovered += 1
                log.warning(
                    f"STARTUP FILL RECOVERED: {side.upper()} {matched:.0f}sh "
                    f"@ {fill_price:.4f} (order {order_status}) | cid={cid[:16]}"
                )
            elif order_type == "dump_sell":
                self.positions.record_unwind(cid, side, matched)
                self.db.log_unwind(
                    condition_id=cid, question=f"startup-recovery",
                    side=side, shares=matched,
                    sell_price=fill_price, usd_value=matched * fill_price,
                )
                unwinds_recovered += 1
                log.warning(
                    f"STARTUP UNWIND RECOVERED: {side.upper()} {matched:.0f}sh "
                    f"@ {fill_price:.4f} (order {order_status}) | cid={cid[:16]}"
                )

        if fills_recovered or unwinds_recovered:
            log.info(
                f"Startup recovery: {fills_recovered} fill(s), "
                f"{unwinds_recovered} unwind(s) recovered from offline orders"
            )

        # Step 4: Cancel all remaining exchange orders
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

        # Step 5: Purge ALL active_orders from DB (everything is now
        # either recovered or cancelled — DB records are stale)
        try:
            purged = self.db.purge_all_active_orders()
            if purged > 0:
                log.info(f"Startup: purged {purged} stale active_orders from DB")
        except Exception as e:
            log.warning(f"Startup active_orders purge failed: {e}")

    def _save_usdc_balance(self):
        """Query exchange for USDC balance and save to DB."""
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            usdc = float(bal.get("balance", 0)) / 1e6
            self.db.save_usdc_balance(usdc)
            log.debug(f"USDC balance: ${usdc:.2f}")
        except Exception as e:
            log.debug(f"USDC balance save failed: {e}")

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

    def _reconcile_orders(self):
        """Verify tracked orders actually exist on the exchange.

        Fetches the current open_ids from the exchange and cross-references
        every tracked buy order and dump order. Clears any that the exchange
        doesn't know about — these are ghost orders from cancelled orders
        where the bot missed the cancellation (API timeout, manual cancel,
        exchange-side cancel).

        Without this, a ghost order blocks the slot forever: no new order
        can be placed, no fill can be detected, and the market side is dead.

        For ghost dump orders, clears dump_orders but preserves dump_state
        so reprice_active_dumps can re-initiate the dump.
        """
        if self.dry_run:
            return
        try:
            exchange_orders = self.client.get_orders() or []
            open_ids = {o["id"] for o in exchange_orders}
        except Exception as e:
            log.warning(f"Order reconciliation: get_orders failed: {e}")
            return

        ghost_buys = 0
        ghost_dumps = 0

        for cid, ms in list(self.markets.items()):
            # Check buy orders
            for side in ("yes", "no"):
                slot = ms.orders[side]
                if slot.order_id and slot.order_id not in open_ids:
                    # Order exists in our state but NOT on exchange.
                    # Before clearing, check if it was filled (not just cancelled).
                    filled = False
                    try:
                        status = self.client.get_order(slot.order_id)
                        matched = float(status.get("size_matched", 0))
                        order_status = status.get("status", "")
                        if matched > 0 and order_status in ("MATCHED", "CANCELLED"):
                            # Silent fill — record it before clearing
                            raw_api_price = float(status.get("price", 0))
                            if raw_api_price > 0:
                                from price import to_yes_equiv
                                actual_price = to_yes_equiv(raw_api_price, side)
                            else:
                                actual_price = slot.price
                            log.warning(
                                f"ORDER RECONCILE: ghost {side.upper()} order was filled "
                                f"{matched:.0f}sh — recording | {ms.question[:30]}"
                            )
                            self.order_lifecycle.handle_fill(
                                ms, side, slot,
                                actual_shares=matched, actual_price=actual_price,
                            )
                            filled = True
                    except Exception as e:
                        log.debug(f"Ghost order status check failed {slot.order_id[:16]}: {e}")

                    if not filled:
                        log.warning(
                            f"ORDER RECONCILE: ghost {side.upper()} buy order "
                            f"{slot.order_id[:16]} — not on exchange, clearing | "
                            f"{ms.question[:30]}"
                        )
                    self.db.delete_active_order(slot.order_id)
                    ms.orders[side] = OrderSlot()
                    ghost_buys += 1

            # Check dump orders
            for side in ("yes", "no"):
                dump_oid = ms.dump_orders[side]
                if dump_oid and dump_oid not in open_ids:
                    # Dump order gone from exchange — clear order ref but
                    # preserve dump_state so reprice_active_dumps re-initiates.
                    log.warning(
                        f"ORDER RECONCILE: ghost {side.upper()} dump order "
                        f"{dump_oid[:16]} — not on exchange, clearing | "
                        f"{ms.question[:30]}"
                    )
                    self.db.delete_active_order(dump_oid)
                    ms.dump_orders[side] = None
                    # dump_state preserved — reprice_active_dumps will
                    # detect (dump_state exists, dump_orders=None) and re-post
                    ghost_dumps += 1

        if ghost_buys or ghost_dumps:
            log.info(
                f"Order reconciliation: cleared {ghost_buys} ghost buy(s), "
                f"{ghost_dumps} ghost dump(s)"
            )
        else:
            log.info("Order reconciliation: all tracked orders match exchange")

    def _scan_orphaned_positions(self):
        """Scan exchange for orphaned positions the bot lost track of.

        Queries all condition_ids from recent fills, checks exchange balance
        for any that aren't currently tracked in self.markets or positions.
        Re-registers orphans so the bot can dump them.
        """
        if self.dry_run:
            return
        try:
            import requests
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            from oversight.data_collector import _connect_db

            conn = _connect_db(self.db._db_path)

            # Get all condition_ids from fills in last 7 days
            cutoff = time.time() - 7 * 86400
            rows = conn.execute(
                "SELECT DISTINCT condition_id, question FROM fills WHERE ts > ?",
                (cutoff,),
            ).fetchall()
            conn.close()

            tracked_cids = set(self.markets.keys())
            candidates = [
                (r["condition_id"], r["question"])
                for r in rows
                if r["condition_id"] not in tracked_cids
            ]

            if not candidates:
                log.info("Orphan scan: no untracked fills found")
                return

            log.info(f"Orphan scan: checking {len(candidates)} untracked markets from recent fills")
            orphans_found = 0

            for cid, question in candidates:
                # Fetch token_ids from CLOB API
                try:
                    mkt_resp = requests.get(
                        f"https://clob.polymarket.com/markets/{cid}", timeout=10,
                    )
                    if mkt_resp.status_code != 200:
                        continue
                    mkt = mkt_resp.json()
                    tokens = mkt.get("tokens", [])
                    if len(tokens) < 2:
                        continue
                except Exception:
                    continue

                yes_tid = tokens[0]["token_id"]
                no_tid = tokens[1]["token_id"]
                tick = float(mkt.get("minimum_tick_size") or 0.01)
                orphan_sides = {}  # {side: actual_shares}

                # Check exchange balance for each side
                for side, tid in [("yes", yes_tid), ("no", no_tid)]:
                    try:
                        bal = self.client.get_balance_allowance(
                            BalanceAllowanceParams(
                                asset_type=AssetType.CONDITIONAL, token_id=tid,
                            )
                        )
                        actual = float(bal.get("balance", 0)) / 1e6
                        if actual >= 1.0:
                            orphan_sides[side] = actual
                    except Exception as e:
                        log.debug(f"Orphan balance check failed {cid[:16]} {side}: {e}")

                if not orphan_sides:
                    continue

                # Re-register in positions DB
                q = question or f"orphan-{cid[:12]}"
                self.positions.register_market(cid, q)
                for side, actual in orphan_sides.items():
                    self.positions.set_shares(cid, side, actual)
                    log.warning(
                        f"ORPHAN FOUND: {side.upper()} {actual:.0f}sh on exchange | "
                        f"{q[:50]}"
                    )
                    orphans_found += 1

                # Add to active markets so the bot can dump them
                if cid not in self.markets:
                    ms = MarketState(
                        cid=cid, question=q,
                        yes_tid=yes_tid, no_tid=no_tid,
                        daily_rate=0, max_spread=0.05,
                        min_size=1, tick_size=tick,
                        yes_price=None,
                    )
                    self.markets[cid] = ms
                    # Trigger immediate dump for each orphaned side
                    for side, actual in orphan_sides.items():
                        log.info(f"Triggering dump for orphan {side.upper()} {actual:.0f}sh | {q[:40]}")
                        self.dump_mgr.dump_position(ms, side, actual)

            if orphans_found:
                log.info(f"Orphan scan: re-registered {orphans_found} orphaned position(s)")
            else:
                log.info("Orphan scan: no orphans on exchange")

        except Exception as e:
            log.warning(f"Orphan scan failed: {e}")

    def _sync_exchange_positions(self):
        """Full position sync: Data API -> register orphans + clean stale DB entries.

        Queries the Data API for all real exchange positions, compares with the
        bot's tracked positions, registers any orphans, and removes DB entries
        for markets that no longer have exchange balances.
        """
        if self.dry_run:
            return
        try:
            import requests as req
            from config import FUNDER
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

            if not FUNDER:
                log.warning("Position sync skipped: FUNDER not set")
                return

            resp = req.get(
                "https://data-api.polymarket.com/positions",
                params={"user": FUNDER, "sizeThreshold": "0.1"},
                timeout=15,
            )
            if resp.status_code != 200:
                log.warning(f"Position sync: Data API returned {resp.status_code}")
                return

            exchange_data = resp.json()
            if not isinstance(exchange_data, list):
                log.warning("Position sync: unexpected Data API response format")
                return

            # Build lookup: condition_id -> {side: {shares, token_id, question}}
            exchange_by_cid: dict[str, dict] = {}
            for pos in exchange_data:
                cid = pos.get("conditionId", "")
                side = pos.get("outcome", "").lower()
                if not cid or side not in ("yes", "no"):
                    continue
                if cid not in exchange_by_cid:
                    exchange_by_cid[cid] = {}
                exchange_by_cid[cid][side] = {
                    "shares": float(pos.get("size", 0)),
                    "token_id": str(pos.get("asset", "")),
                    "question": pos.get("title", ""),
                    "avg_price": float(pos.get("avgPrice", 0)),
                }

            exchange_cids = set(exchange_by_cid.keys())
            tracked_cids = set(self.markets.keys())

            # ── Register orphans (on exchange but not tracked) ──
            orphan_cids = exchange_cids - tracked_cids
            orphans_found = 0
            for cid in orphan_cids:
                sides = exchange_by_cid[cid]
                question = next(iter(sides.values()))["question"]

                # Need both YES and NO token_ids to create MarketState
                try:
                    mkt_resp = req.get(
                        f"https://clob.polymarket.com/markets/{cid}", timeout=10,
                    )
                    if mkt_resp.status_code != 200:
                        continue
                    mkt = mkt_resp.json()
                    tokens = mkt.get("tokens", [])
                    if len(tokens) < 2:
                        continue
                except Exception:
                    continue

                yes_tid = tokens[0]["token_id"]
                no_tid = tokens[1]["token_id"]
                tick = float(mkt.get("minimum_tick_size") or 0.01)

                q = question or f"orphan-{cid[:12]}"
                self.positions.register_market(cid, q)
                for side, info in sides.items():
                    self.positions.set_shares(cid, side, info["shares"])
                    log.warning(
                        f"EXCHANGE SYNC: found {side.upper()} {info['shares']:.1f}sh "
                        f"not tracked | {q[:50]}"
                    )
                    orphans_found += 1

                # Add to active markets for dumping
                if cid not in self.markets:
                    ms = MarketState(
                        cid=cid, question=q,
                        yes_tid=yes_tid, no_tid=no_tid,
                        daily_rate=0, max_spread=0.05,
                        min_size=1, tick_size=tick,
                        yes_price=None,
                    )
                    self.markets[cid] = ms
                    for side, info in sides.items():
                        log.info(
                            f"Triggering dump for synced orphan {side.upper()} "
                            f"{info['shares']:.0f}sh | {q[:40]}"
                        )
                        self.dump_mgr.dump_position(ms, side, info["shares"])

            # ── Clean stale positions (in DB but not on exchange) ──
            db_positions = self.positions.get_all_positions()
            stale_removed = 0
            for cid in list(db_positions.keys()):
                if cid in exchange_cids:
                    continue
                # Position is in DB but not on exchange — check if it's being
                # actively dumped before removing
                is_dumping = (
                    cid in self.dump_mgr.dump_states
                    if hasattr(self.dump_mgr, "dump_states")
                    else False
                )
                if is_dumping:
                    continue
                db_yes = db_positions[cid].get("yes_shares", 0)
                db_no = db_positions[cid].get("no_shares", 0)
                if db_yes < 0.5 and db_no < 0.5:
                    # Already near-zero, just clean up
                    self.positions.remove_market(cid)
                    stale_removed += 1
                else:
                    # Non-trivial shares in DB but gone from exchange
                    # (market resolved, shares redeemed, or sold externally)
                    q = db_positions[cid].get("question", cid[:16])
                    log.info(
                        f"STALE CLEANUP: removing {q[:40]} "
                        f"(YES={db_yes:.0f} NO={db_no:.0f}) — not on exchange"
                    )
                    self.positions.remove_market(cid)
                    stale_removed += 1

            log.info(
                f"Exchange position sync: {len(exchange_cids)} on exchange, "
                f"{orphans_found} orphans registered, {stale_removed} stale removed"
            )

        except Exception as e:
            log.warning(f"Exchange position sync failed: {e}")

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
                    # Carry end_date_iso from agent allocation (agent has Gamma API data)
                    if alloc.get("end_date_iso") and not m.get("end_date_iso"):
                        m["end_date_iso"] = alloc["end_date_iso"]
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
            # Mark agent-approved markets; cancel buy orders on unapproved leftovers
            validated_cids = {m["condition_id"] for m in validated}
            for cid, ms in self.markets.items():
                ms.agent_approved = cid in validated_cids
                if not ms.agent_approved:
                    for side in ("yes", "no"):
                        slot = ms.orders[side]
                        if slot.order_id:
                            self.order_lifecycle.cancel_order(slot.order_id, reason="not_in_allocation")
                            self.db.delete_active_order(slot.order_id)
                            ms.orders[side] = OrderSlot()
            return

        # If agent was previously active but allocation is now stale/missing, block new orders
        if self._agent_mode:
            log.warning(f"Agent allocation stale/missing — blocking new orders on {len(self.markets)} markets")
            for ms in self.markets.values():
                ms.agent_approved = False
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
                    self.order_lifecycle.cancel_order(oid, reason="market_removed")
                    self.db.delete_active_order(oid)
                    ms.orders[side].order_id = None
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
                self.markets[cid].agent_approved = True
                new_shares = float(m.get("_agent_shares", 0))
                if new_shares > 0 and new_shares != self.markets[cid].agent_shares:
                    log.info(f"Resizing {self.markets[cid].question[:30]}: {self.markets[cid].agent_shares:.0f} → {new_shares:.0f}sh")
                    self.markets[cid].agent_shares = new_shares
                    # Cancel stale orders so next cycle replaces at new size
                    for side in ("yes", "no"):
                        slot = self.markets[cid].orders[side]
                        if slot.order_id:
                            self.order_lifecycle.cancel_order(slot.order_id, reason="resize")
                            self.db.delete_active_order(slot.order_id)
                            self.markets[cid].orders[side] = OrderSlot()
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
                    end_date_iso=m.get("end_date_iso") or "",
                    agent_approved=True,
                )
                self.positions.register_market(cid, m["question"])

        log.info(f"Active markets: {len(self.markets)}")
        for i, (cid, ms) in enumerate(self.markets.items()):
            if i < 10:
                log.info(f"  #{i+1} {ms.question[:45]} | ${ms.daily_rate:.0f}/d | {ms.agent_shares:.0f}sh")

        self._last_market_refresh = time.time()

    # ── Expiry Sweep ───────────────────────────────────────────────

    def _sweep_expiring_markets(self):
        """Layer 3: Cancel orders + dump shares on markets expiring within 1 hour.

        Runs every cycle. Only checks markets that have end_date_iso set.
        This catches markets that were safe at allocation time but resolved
        during the session — particularly sports markets where game time
        can be uncertain.
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        swept = 0

        for cid, ms in list(self.markets.items()):
            if not ms.end_date_iso:
                continue
            try:
                dt = datetime.fromisoformat(ms.end_date_iso.replace("Z", "+00:00"))
                hours_to_expiry = (dt - now).total_seconds() / 3600
            except Exception:
                continue

            if hours_to_expiry > 1.0:
                continue

            # Market expiring within 1 hour — cancel all orders
            for side in ("yes", "no"):
                slot = ms.orders[side]
                if slot.order_id:
                    self.order_lifecycle.cancel_order(slot.order_id, reason="expiry_sweep")
                    self.db.delete_active_order(slot.order_id)
                    ms.orders[side] = OrderSlot()

                # Cancel dump orders too — no point selling into a resolving market
                dump_oid = ms.dump_orders[side]
                if dump_oid:
                    self.order_lifecycle.cancel_order(dump_oid, reason="expiry_sweep")
                    self.db.delete_active_order(dump_oid)
                    ms.dump_orders[side] = None
                    ms.dump_state[side] = None
                    self.db.delete_dump_state(cid, side)

            # Block future placement
            ms.agent_approved = False
            swept += 1
            log.warning(
                f"EXPIRY SWEEP: {hours_to_expiry:.1f}h to expiry — "
                f"cancelled all orders, blocked placement | {ms.question[:40]}"
            )

        if swept:
            log.info(f"Expiry sweep: cleared {swept} market(s) approaching resolution")

    # ── Runtime Safety Guardrails ───────────────────────────────────
    # All helpers are read-only and fail-open: any missing data returns
    # None (or an empty result) and the caller skips the corresponding
    # check. The only control-flow changes are (a) blocking placements
    # and (b) activating the kill switch — both gated inside run_cycle.

    def _guardrail_total_capital_from_alloc(self) -> float | None:
        """Read `_total_capital` stamp from the first deploy row of the
        current allocation JSON. None when file is missing / stale /
        unstamped — farmer falls open (skips capital-fraction checks)."""
        alloc_path = os.path.join(
            os.path.dirname(__file__) or ".", "market_allocations.json",
        )
        try:
            if not os.path.exists(alloc_path):
                return None
            with open(alloc_path) as f:
                data = json.load(f)
            for m in data.get("markets", []):
                if m.get("action") != "deploy":
                    continue
                tc = m.get("_total_capital")
                if tc is not None:
                    return float(tc)
        except Exception as e:
            log.debug(f"[GUARDRAIL] total_capital read failed: {e}")
        return None

    def _guardrail_live_notional_per_market(self) -> dict[str, float]:
        """Per-market sum of (price × shares) over both sides for every
        slot that currently holds an order_id. Dump orders are included
        so the kill switch sees the full live exposure."""
        out: dict[str, float] = {}
        for cid, ms in self.markets.items():
            notional = 0.0
            for side in ("yes", "no"):
                slot = ms.orders.get(side)
                if slot and slot.order_id:
                    notional += float(slot.price or 0.0) * float(slot.shares or 0.0)
                # Dump sell orders also carry live exposure (we owe shares).
                dump_oid = ms.dump_orders.get(side) if isinstance(ms.dump_orders, dict) else None
                dump_state = ms.dump_state.get(side) if isinstance(ms.dump_state, dict) else None
                if dump_oid and isinstance(dump_state, dict):
                    dp = float(dump_state.get("price") or 0.0)
                    ds = float(dump_state.get("shares") or 0.0)
                    notional += dp * ds
            if notional > 0:
                out[cid] = notional
        return out

    def _guardrail_cluster_notional(
        self, live_notional: dict[str, float],
    ) -> tuple[dict[int, float], dict[str, int | None]]:
        """Group live notional by fill-cluster. Returns (cluster_id →
        notional, cid → cluster_id). Fail-open: returns empty dicts if
        the cluster DB read fails."""
        cluster_by_cid: dict[str, int | None] = {}
        cluster_notional: dict[int, float] = {}
        try:
            from profit.correlation import build_fill_clusters
            clusters, _oversized = build_fill_clusters(self.db._db_path)
        except Exception as e:
            log.debug(f"[GUARDRAIL] cluster build skipped: {e}")
            return {}, {}
        for cid, n in live_notional.items():
            cl = clusters.get(cid)
            cluster_by_cid[cid] = cl
            if cl is None:
                continue
            cluster_notional[cl] = cluster_notional.get(cl, 0.0) + n
        return cluster_notional, cluster_by_cid

    def _guardrail_current_cf(self) -> float | None:
        """Latest smoothed correction_factor. None on missing / error."""
        try:
            conn = self.db._get_conn()
            row = conn.execute(
                "SELECT correction_factor FROM reward_daily "
                "ORDER BY date DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            return float(row[0])
        except Exception as e:
            log.debug(f"[GUARDRAIL] CF read failed: {e}")
            return None

    def _guardrail_daily_realized_loss(self) -> float | None:
        """Realized loss over last 24h (positive number = how much we
        lost). Sum of negative PnL on unwinds. None on DB error — the
        kill-switch test is skipped in that case (fail-open)."""
        try:
            conn = self.db._get_conn()
            cutoff = time.time() - 86400
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM unwinds "
                "WHERE ts > ? AND pnl < 0",
                (cutoff,),
            ).fetchone()
            if row is None:
                return 0.0
            # Sum of negative PnL → convert to positive loss.
            return -float(row[0] or 0.0)
        except Exception as e:
            log.debug(f"[GUARDRAIL] daily realized loss read failed: {e}")
            return None

    def _guardrail_fill_rate_ratio(self) -> tuple[float | None, int, int]:
        """Return (ratio, short_count, baseline_count).

        short_count := fills across all markets in the last
            GUARDRAIL_FILLRATE_SHORT_SECS.
        baseline := fills in the last GUARDRAIL_FILLRATE_BASELINE_SECS
            (includes the short window).

        ratio is `short_count / (baseline_per_short_window_equivalent)`
        — i.e. `short_count / (baseline_count * short/baseline)`.
        None when the baseline is below GUARDRAIL_FILLRATE_MIN_BASELINE
        (not enough history for a meaningful ratio — fail-open)."""
        now = time.time()
        short_cutoff = now - GUARDRAIL_FILLRATE_SHORT_SECS
        base_cutoff = now - GUARDRAIL_FILLRATE_BASELINE_SECS
        short_count = 0
        base_count = 0
        for ms in self.markets.values():
            ft = ms.fill_times if isinstance(ms.fill_times, dict) else {}
            for side in ("yes", "no"):
                times = ft.get(side) or []
                for t in times:
                    if t >= base_cutoff:
                        base_count += 1
                        if t >= short_cutoff:
                            short_count += 1
        if base_count < GUARDRAIL_FILLRATE_MIN_BASELINE:
            return None, short_count, base_count
        # Scale baseline to the short-window duration for a like-for-like
        # comparison. ratio = observed-short / expected-short-from-baseline.
        window_ratio = (
            GUARDRAIL_FILLRATE_SHORT_SECS / GUARDRAIL_FILLRATE_BASELINE_SECS
        )
        expected_short = base_count * window_ratio
        if expected_short <= 0:
            return None, short_count, base_count
        return short_count / expected_short, short_count, base_count

    def _guardrail_check_and_log(self) -> dict:
        """Compute all guardrail signals, emit a structured telemetry
        line, and return a decision struct for run_cycle to act on.

        Schema of the returned dict (all keys always present):
            kill_switch        : bool
            kill_reason        : str ("" when False)
            notional_block     : bool
            blocked_clusters   : set[int]
            cluster_by_cid     : dict[str, int | None]
            total_capital      : float | None
        """
        total_capital = self._guardrail_total_capital_from_alloc()
        live_by_cid = self._guardrail_live_notional_per_market()
        total_live_notional = sum(live_by_cid.values())
        active_markets = len(live_by_cid)
        cluster_notional, cluster_by_cid = self._guardrail_cluster_notional(
            live_by_cid,
        )
        cf = self._guardrail_current_cf()
        daily_loss = self._guardrail_daily_realized_loss()
        fill_ratio, short_fills, base_fills = self._guardrail_fill_rate_ratio()

        notional_ratio: float | None = None
        if total_capital is not None and total_capital > 0:
            notional_ratio = total_live_notional / total_capital

        # ── Decisions ─────────────────────────────────────────────
        kill_reasons: list[str] = []
        if total_capital is not None and daily_loss is not None:
            loss_limit = MAX_DAILY_LOSS_FRAC * total_capital
            if daily_loss > loss_limit:
                kill_reasons.append(
                    f"daily_realized_loss={daily_loss:.2f} > "
                    f"{loss_limit:.2f} (= {MAX_DAILY_LOSS_FRAC:.0%}·T)"
                )
        if cf is not None and cf < CRITICAL_CF_THRESHOLD:
            kill_reasons.append(
                f"correction_factor={cf:.4f} < {CRITICAL_CF_THRESHOLD}"
            )
        if fill_ratio is not None and fill_ratio > FILL_RATE_SPIKE_FACTOR:
            kill_reasons.append(
                f"fill_rate_ratio={fill_ratio:.2f} > {FILL_RATE_SPIKE_FACTOR}× "
                f"(short={short_fills}, baseline={base_fills})"
            )

        notional_block = (
            notional_ratio is not None
            and notional_ratio > MAX_NOTIONAL_RATIO
        )
        blocked_clusters: set[int] = set()
        if total_capital is not None and total_capital > 0:
            cluster_limit_usd = CLUSTER_NOTIONAL_LIMIT_FRAC * total_capital
            for cl_id, cl_notional in cluster_notional.items():
                if cl_notional > cluster_limit_usd:
                    blocked_clusters.add(cl_id)

        # ── Structured telemetry (single machine-readable line) ──
        tele = {
            "event": "guardrail",
            "cycle": self.cycle_count,
            "ts": round(time.time(), 3),
            "total_capital": (
                round(total_capital, 2) if total_capital is not None else None
            ),
            "total_live_notional": round(total_live_notional, 2),
            "notional_ratio": (
                round(notional_ratio, 4) if notional_ratio is not None else None
            ),
            "active_markets": active_markets,
            "cluster_count": len(cluster_notional),
            "max_cluster_notional": (
                round(max(cluster_notional.values()), 2)
                if cluster_notional else 0.0
            ),
            "blocked_cluster_count": len(blocked_clusters),
            "fill_rate_short_1h": short_fills,
            "fill_rate_baseline_6h": base_fills,
            "fill_rate_ratio": (
                round(fill_ratio, 3) if fill_ratio is not None else None
            ),
            "realized_loss_24h": (
                round(daily_loss, 2) if daily_loss is not None else None
            ),
            "cf": round(cf, 6) if cf is not None else None,
            "notional_block": notional_block,
            "kill_switch": bool(kill_reasons),
        }
        log.info(f"[GUARDRAIL] {json.dumps(tele, sort_keys=True)}")

        return {
            "kill_switch": bool(kill_reasons),
            "kill_reason": "; ".join(kill_reasons),
            "notional_block": notional_block,
            "blocked_clusters": blocked_clusters,
            "cluster_by_cid": cluster_by_cid,
            "total_capital": total_capital,
        }

    def _activate_kill_switch(self, reason: str) -> None:
        """One-shot: flip the flag, log prominently, cancel every live
        BUY slot + dump sell. Subsequent cycles short-circuit out of
        run_cycle. Operator must restart the process to clear."""
        self._kill_switch_active = True
        self._kill_switch_reason = reason
        self._kill_switch_triggered_at = time.time()
        log.error(f"[GUARDRAIL] KILL SWITCH ACTIVATED: {reason}")
        log.error("[GUARDRAIL] cancelling all live + dump orders")
        cancelled = 0
        for ms in list(self.markets.values()):
            for side in ("yes", "no"):
                slot = ms.orders.get(side)
                if slot and slot.order_id:
                    try:
                        if self.order_lifecycle.cancel_order(
                            slot.order_id, reason="kill_switch",
                        ):
                            cancelled += 1
                    except Exception as e:
                        log.debug(f"[GUARDRAIL] cancel failed {slot.order_id}: {e}")
                dump_oid = (
                    ms.dump_orders.get(side)
                    if isinstance(ms.dump_orders, dict) else None
                )
                if dump_oid:
                    try:
                        if self.order_lifecycle.cancel_order(
                            dump_oid, reason="kill_switch_dump",
                        ):
                            cancelled += 1
                    except Exception as e:
                        log.debug(f"[GUARDRAIL] dump-cancel failed {dump_oid}: {e}")
        log.error(f"[GUARDRAIL] kill switch cancelled {cancelled} orders")
        log.info(
            f"[GUARDRAIL] "
            f"{json.dumps({'event': 'kill_switch_activated', 'cycle': self.cycle_count, 'reason': reason, 'cancelled_orders': cancelled, 'ts': round(time.time(), 3)}, sort_keys=True)}"
        )

    # ── Core Cycle ──────────────────────────────────────────────────

    def run_cycle(self):
        """One cycle: check dumps → detect fills → place orders → record rewards."""
        self.cycle_count += 1
        self.order_lifecycle.cycle_count = self.cycle_count
        self.order_lifecycle.capital_ceiling = None

        # Kill-switch short-circuit: if a prior cycle tripped the halt,
        # bail out immediately (no fills polled, no placements, no
        # reward recording). Reset requires a process restart.
        if self._kill_switch_active:
            if self.cycle_count % 10 == 0:
                log.warning(
                    f"[GUARDRAIL] kill switch ACTIVE: "
                    f"{self._kill_switch_reason} — skipping cycle"
                )
            return

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

        # Step 3.5: Pre-cycle expiry sweep (Layer 3)
        # Catches markets that were safe at allocation time but are now
        # about to resolve. Cancel orders + dump shares for any market
        # expiring within 1 hour.
        self._sweep_expiring_markets()

        # Step 3.6: Cross-market fill storm detection (Issue 3)
        # If ≥5 fills across ALL markets within 5 minutes, halt new
        # placements for 10 minutes. Catches correlated fill events
        # that per-market breakers miss.
        STORM_WINDOW_SECS = 300  # 5 minutes
        STORM_THRESHOLD = 5     # fills across all markets
        STORM_HALT_SECS = 600   # 10-minute halt
        now = time.time()
        # Count recent fills from all markets' fill_times
        global_recent = 0
        for ms in self.markets.values():
            for side in ("yes", "no"):
                global_recent += sum(1 for t in ms.fill_times.get(side, []) if now - t < STORM_WINDOW_SECS)
        if global_recent >= STORM_THRESHOLD and now >= self._fill_storm_until:
            self._fill_storm_until = now + STORM_HALT_SECS
            log.warning(
                f"FILL STORM DETECTED: {global_recent} fills in {STORM_WINDOW_SECS}s "
                f"across all markets. Halting new placements for {STORM_HALT_SECS}s."
            )
            # Log to DB so agent can see it
            try:
                self.db.execute_sql(
                    "INSERT INTO fills (ts, condition_id, side, fill_type, shares, price, clob_cost, usd_value) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (now, "__FILL_STORM__", "both", "STORM_ALERT", 0, 0, 0, 0),
                )
            except Exception:
                pass

        # Step 4: Place orders on priority batch
        market_list = list(self.markets.values())
        if not market_list:
            return

        # Step 4pre: runtime safety guardrails. Runs AFTER allocation
        # (already consumed by market filtering upstream) and BEFORE
        # new placements. Emits structured telemetry every cycle.
        guard = self._guardrail_check_and_log()
        if guard["kill_switch"]:
            self._activate_kill_switch(guard["kill_reason"])
            return

        # If fill storm active, skip all new placements
        if time.time() < self._fill_storm_until:
            remaining = self._fill_storm_until - time.time()
            if self.cycle_count % 10 == 0:  # log every ~5min
                log.warning(f"Fill storm halt active — {remaining:.0f}s remaining, skipping placements")
        elif guard["notional_block"]:
            if self.cycle_count % 10 == 0:
                log.warning(
                    f"[GUARDRAIL] notional_ratio > {MAX_NOTIONAL_RATIO} — "
                    f"blocking ALL new placements this cycle"
                )
        else:
            batch = self.order_lifecycle.get_priority_batch(market_list)
            blocked_clusters = guard["blocked_clusters"]
            cluster_by_cid = guard["cluster_by_cid"]
            skipped_cluster = 0
            for ms in batch:
                cl = cluster_by_cid.get(ms.cid)
                if cl is not None and cl in blocked_clusters:
                    skipped_cluster += 1
                    continue
                self.order_lifecycle.place_orders_for_market(ms)
            if skipped_cluster and self.cycle_count % 10 == 0:
                log.warning(
                    f"[GUARDRAIL] skipped {skipped_cluster} placement(s) in "
                    f"{len(blocked_clusters)} over-exposed cluster(s) "
                    f"(> {CLUSTER_NOTIONAL_LIMIT_FRAC:.0%}·T)"
                )

        # Step 4b: Remove dead markets (3+ consecutive book failures = resolved/delisted)
        BOOK_FAILURE_LIMIT = 3
        dead_cids = [
            cid for cid, ms in self.markets.items()
            if ms.book_failures >= BOOK_FAILURE_LIMIT
        ]
        for cid in dead_cids:
            ms = self.markets[cid]
            log.info(f"Removing dead market ({ms.book_failures} book failures): {ms.question[:50]}")
            for side in ["yes", "no"]:
                oid = ms.orders[side].order_id
                if oid:
                    self.order_lifecycle.cancel_order(oid, reason="dead_market")
                    self.db.delete_active_order(oid)
            del self.markets[cid]

        # Step 5: Record rewards
        from config import RF_BOOK_CACHE_TTL
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
            # Reuse the book cached by place_orders_for_market within TTL.
            # None means record_cycle will skip the Q-score sample for this
            # market this cycle — correct behavior when no fresh book exists.
            book_for_scoring = None
            if ms.cached_book and RF_BOOK_CACHE_TTL > 0:
                if time.time() - ms.last_book_fetch <= RF_BOOK_CACHE_TTL:
                    book_for_scoring = ms.cached_book
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
                order_book=book_for_scoring,
            )

        if self.cycle_count % 5 == 0:
            self.rewards._save()

        # Step 6: Phase 0 — record per-order scoring status (1 API call)
        # Only check every 5th cycle (~2.5 min) to avoid rate limit pressure
        if self.cycle_count % 5 == 0 and not self.dry_run:
            try:
                from py_clob_client.clob_types import OrdersScoringParams
                # Gather all live order IDs with their market context
                order_map = {}  # {order_id: (cid, side, price, shares)}
                for ms in market_list:
                    for side in ("yes", "no"):
                        slot = ms.orders[side]
                        if slot.order_id:
                            order_map[slot.order_id] = (ms.cid, side, slot.price, slot.shares)
                if order_map:
                    scoring_result = self.client.are_orders_scoring(
                        OrdersScoringParams(orderIds=list(order_map.keys()))
                    )
                    scoring_data = []
                    for oid, (cid, side, price, shares) in order_map.items():
                        is_scoring = scoring_result.get(oid, False)
                        scoring_data.append((oid, cid, side, is_scoring, price, shares))
                    self.db.log_scoring_snapshot(scoring_data)
            except Exception as e:
                log.debug(f"Phase0 scoring snapshot failed: {e}")

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
        self._scan_orphaned_positions()
        self._sync_exchange_positions()
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

            # Metrics + USDC balance snapshot every 10 cycles (~5 min)
            if self.cycle_count % 10 == 0 and self.cycle_count > 0:
                on_book = sum(1 for ms in self.markets.values()
                              if ms.orders["yes"].order_id or ms.orders["no"].order_id)
                active_dumps = sum(1 for ms in self.markets.values()
                                   if ms.dump_orders["yes"] or ms.dump_orders["no"])
                log.info(f"[metrics] cycle={self.cycle_count} | {cycle_duration:.1f}s | on_book={on_book}/{len(self.markets)} | dumps={active_dumps}")

                # Write actual USDC balance to DB for oversight agent
                if not self.dry_run:
                    self._save_usdc_balance()

            # Hourly reward log
            if time.time() - self._last_reward_log >= 3600:
                self._last_reward_log = time.time()
                self.rewards.maybe_log_hourly(self.all_market_data[:MAX_MARKETS()])
                self.rewards._last_hourly_log = 0

            # Full reconciliation every 15 min (exchange → DB sync)
            # Positions: verify share balances against exchange
            # Orders: verify tracked orders actually exist on exchange
            if not self.dry_run and time.time() - self._last_reconcile >= 900:
                self._reconcile_positions()
                self._reconcile_orders()
                self._last_reconcile = time.time()

            # Exchange position sync every 30 min (Data API → orphans + stale cleanup)
            if not self.dry_run and time.time() - self._last_exchange_sync >= 1800:
                self._sync_exchange_positions()
                self._last_exchange_sync = time.time()

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
