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
import collections
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
import alerts
# Oversight integration (final safety layer): the farmer calls
# `oversight_agent.evaluate(guard)` once per cycle, after the guardrail
# computation and before the placement decision. The contract is
# `{"action": "continue"|"pause"|"kill", "reason": str}`. The evaluator
# is optional — if `oversight_agent.evaluate` is absent or raises, the
# farmer fails open ("continue") via the try/except in run_cycle.
import oversight_agent

# Config accessors (hot-reloadable)
def SHARES_PER_SIDE(): return cfg("RF_SHARES_PER_SIDE")
def MIN_DAILY_RATE(): return cfg("RF_MIN_DAILY_RATE")
def MAX_LIQUIDITY(): return cfg("RF_MAX_LIQUIDITY")
def MAX_MARKETS(): return cfg("RF_MAX_MARKETS")
def CYCLE_SECS(): return cfg("RF_CYCLE_SECS")
def BATCH_SIZE(): return cfg("RF_BATCH_SIZE")
def MARKET_REFRESH_SECS(): return cfg("RF_MARKET_REFRESH_SECS")
def PLACEMENT_TICKS_INSIDE(): return cfg("RF_PLACEMENT_TICKS_INSIDE")
# FX-011: MAX_COST_PER_MARKET / MAX_TOTAL_EXPOSURE accessors were here but
# never called by production code. Per-market and total exposure are
# enforced by the v5.0 runtime guardrails (notional ratio, cluster cap,
# hard-enforcement multi-cancel, kill switch) downstream. Deleted with
# their config constants 2026-05-18.


# ── Runtime safety guardrails (applied after allocation, before placement) ──
# All thresholds are live-capital bounds; the allocator/learning loop is
# unaware of them (correct layering: allocation decides intent, farmer
# enforces execution-time safety). Fail-open on missing data so a DB hiccup
# can never halt trading — only the explicit kill-switch triggers halt.
#
# FX-058 (P1 of 9/10 plan): the four notional thresholds below are now
# cfg()-driven so they can be retuned without code redeploy. The legacy
# hardcoded values (MAX_NOTIONAL_RATIO=2.0, HARD_NOTIONAL_RATIO=2.5) were
# Rule-2 violations — capped below the 3-8× overcommit design point and
# would have tripped on the FX-052/053 OverCommitAllocator's intended
# operating regime. Replaced with raised cfg defaults (5.0 soft / 8.0
# hard) + a NEW acceleration-based rapid-growth kill that catches
# anomalous bursts without false-firing on healthy overcommit.
def MAX_NOTIONAL_RATIO(): return cfg("RF_MAX_NOTIONAL_RATIO")
def HARD_NOTIONAL_RATIO(): return cfg("RF_HARD_NOTIONAL_RATIO")
def RAPID_GROWTH_KILL_RATIO(): return cfg("RF_RAPID_GROWTH_KILL_RATIO")
def RAPID_GROWTH_WINDOW_SEC(): return cfg("RF_RAPID_GROWTH_WINDOW_SEC")
def RAPID_GROWTH_MIN_BASELINE_RATIO(): return cfg("RF_RAPID_GROWTH_MIN_BASELINE_RATIO")
CLUSTER_NOTIONAL_LIMIT_FRAC = 0.5        # soft+hard: block new placements AND
                                         # actively cancel once a cluster exceeds
                                         # 0.5·total_capital (§4.1 of spec — the
                                         # soft and hard cluster thresholds are
                                         # intentionally the same fraction).
MAX_DAILY_LOSS_FRAC = 0.1                # kill-switch: realized loss / total_capital
CRITICAL_CF_THRESHOLD = 0.01             # kill-switch: CF floor
FILL_RATE_SPIKE_FACTOR = 3.0             # kill-switch: short-window / rolling-avg
GUARDRAIL_FILLRATE_SHORT_SECS = 3600     # 1h fill-count window
GUARDRAIL_FILLRATE_BASELINE_SECS = 21600 # 6h baseline window
MIN_FILL_BASELINE = 5                    # require ≥ N baseline fills before the
                                         # fill-rate spike trigger can fire
                                         # (stabilises cold-start behaviour)
MAX_CANCELS_PER_CYCLE = 5                # hard-enforcement cap per helper per
                                         # cycle. Reduces exposure faster than
                                         # one-at-a-time without burst-cancelling
                                         # the whole book during a large breach.
MAX_BREACH_CYCLES = 3                    # after N consecutive cycles of
                                         # notional_ratio > HARD_NOTIONAL_RATIO,
                                         # emit [CRITICAL] persistent_overexposure.
                                         # Observational only — does NOT auto-trip
                                         # the kill switch (§5.3).

# Execution modes (§3.1). Staged deployment: DRY_RUN → SHADOW → LIVE.
#   DRY_RUN : no API calls at all; every intent is logged only.
#   SHADOW  : API reads permitted (get_orders, book fetches); no writes
#             (no place, no cancel) — intent-logged instead.
#   LIVE    : full execution, guardrails active.
# Default is DRY_RUN so instantiating RewardFarmer() without an explicit
# mode runs in log-only mode. Upgrade via --mode shadow / --mode live.
MODE_DRY_RUN = "DRY_RUN"
MODE_SHADOW  = "SHADOW"
MODE_LIVE    = "LIVE"
VALID_MODES  = (MODE_DRY_RUN, MODE_SHADOW, MODE_LIVE)
DEFAULT_MODE = MODE_DRY_RUN
ROLLING_STATS_WINDOW = 100               # cycles retained for rolling averages
ROLLING_STATS_EMIT_EVERY = 10            # emit [ROLLING_STATS] every N cycles
OVERSIGHT_LATENCY_WARN_MS = 50           # warn when oversight_agent.evaluate
                                         # exceeds this wall-time per cycle.
                                         # Synchronous integration — slow
                                         # evaluators block the 30 s farmer
                                         # cycle, so this surfaces it loudly.


class RewardFarmer:
    """Production reward farming bot — orchestrator."""

    def __init__(self, mode: str = DEFAULT_MODE):
        if mode not in VALID_MODES:
            raise ValueError(
                f"mode must be one of {VALID_MODES}, got {mode!r}"
            )
        self.mode = mode
        # Back-compat read-gate: self.dry_run stays True only in DRY_RUN,
        # which preserves the existing startup/get_orders skips for
        # reads. SHADOW allows reads, so dry_run=False there. All
        # order-writing sites now go through _gated_place_orders_for_market
        # / _gated_cancel_order which enforce the full mode semantics.
        self.dry_run = (mode == MODE_DRY_RUN)

        # Create CLOB client
        from config import (
            CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE,
            HOST, PRIVATE_KEY, CHAIN_ID, SIGNATURE_TYPE, FUNDER,
            BUILDER_CODE,
        )
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds, BuilderConfig
        from rate_limiter import RateLimitedClient

        creds = ApiCreds(
            api_key=CLOB_API_KEY, api_secret=CLOB_SECRET,
            api_passphrase=CLOB_PASS_PHRASE,
        )
        raw = ClobClient(
            host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
            signature_type=SIGNATURE_TYPE, funder=FUNDER, creds=creds,
            builder_config=BuilderConfig(builder_code=BUILDER_CODE) if BUILDER_CODE else None,
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

        # Extracted modules — share the markets dict reference.
        # Pass dry_run=True to OL + DumpManager in any non-LIVE mode
        # (belt-and-suspenders: the farmer's _gated_* wrappers already
        # prevent write calls, and OL's internal dry_run handling adds a
        # second layer so any code path that bypasses the wrapper still
        # can't fire a real API write).
        _module_dry = (self.mode != MODE_LIVE)
        self.order_lifecycle = OrderLifecycle(
            client=self.client, db=self.db, positions=self.positions,
            rewards=self.rewards, markets=self.markets, dry_run=_module_dry,
        )
        self.dump_mgr = DumpManager(
            client=self.client, db=self.db, positions=self.positions,
            cancel_fn=self._gated_cancel_order, dry_run=_module_dry,
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
        # FX-028: timestamp of the last unliquidatable_markets re-probe sweep.
        # The actual per-cid staleness gate is RF_UNLIQUIDATABLE_REPROBE_SECS
        # (~6h); this loop-level timestamp throttles the SWEEP itself so the
        # 30-s cycle doesn't issue the DB query 720× per probe window.
        self._last_unliquidatable_reprobe = 0.0
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
        # FX-068: oversight-side kill switch, captured from
        # market_allocations.json by _load_allocations and honored as a
        # real farmer halt (cancel-all + sticky) in run_cycle. Pre-FX-068
        # the farmer only read action=="deploy" rows and never read the
        # alloc's kill_switch field, so an oversight kill degraded to an
        # empty deploy list (stop new orders) without cancelling existing
        # exposure or engaging the sticky halt.
        self._alloc_kill_switch: bool = False
        self._alloc_kill_reason: str = ""
        # Persistent-breach tracker (spec §5.1): consecutive cycles
        # where notional_ratio > HARD_NOTIONAL_RATIO. Incremented in
        # _guardrail_check_and_log; reset when ratio drops back under
        # the hard threshold. Unknown (signal missing) leaves the
        # counter unchanged so a DB hiccup doesn't mask a real breach.
        self._consecutive_hard_notional_breach_cycles: int = 0
        # FX-058: rolling samples of (ts, notional_ratio) over the
        # RAPID_GROWTH_WINDOW_SEC window. Used by
        # _guardrail_rapid_notional_growth to detect anomalous bursts
        # that the loose static thresholds (5×/8×) intentionally permit.
        # deque with no maxlen — eviction is time-based, capped by the
        # 30 s cycle interval × 300 s window = ~10 entries in steady state.
        from collections import deque
        self._notional_ratio_samples: deque[tuple[float, float]] = deque()

        # Cycle-scope observability counters. Reset at the top of each
        # run_cycle; emitted in [CYCLE_SUMMARY] at every run_cycle exit.
        # Counter coverage: farmer-side cancels inside run_cycle
        # (kill-switch, hard enforcement, expiry sweep, dead-market
        # cleanup) and batch placements. Startup/shutdown cancels and
        # between-cycle refreshes aren't counted — they run outside
        # run_cycle.
        self._cycle_orders_placed: int = 0
        self._cycle_orders_cancelled: int = 0
        self._rolling_stats: collections.deque = collections.deque(
            maxlen=ROLLING_STATS_WINDOW,
        )
        # Most recent _guardrail_check_and_log return dict; consumed by
        # _emit_cycle_telemetry so the summary doesn't re-query the DB.
        # None when the guardrail check hasn't run this cycle yet
        # (early returns on kill-switch-active / no-market).
        self._last_guard: dict | None = None

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
            # V2 SDK renamed get_orders → get_open_orders (drop-in compatible,
            # no args, returns list of open exchange orders for this account).
            existing = self.client.get_open_orders() or []
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

        # Step 4: Cancel all remaining exchange orders. In non-LIVE
        # modes, log intent instead of executing — SHADOW mode
        # specifically must not cancel real orders even during
        # startup reconciliation (spec §3.2). Stub-safe: getattr
        # defaults to LIVE so unit-test fixtures (which call this
        # method on a minimal Stub) still see the real cancel path.
        mode = getattr(self, "mode", MODE_LIVE)
        if existing:
            cancelled = 0
            for order in existing:
                oid = order.get("id", "")
                if mode != MODE_LIVE:
                    self._log_dry_run_intent(
                        "cancel_order", order_id=oid,
                        reason="startup_reconcile",
                    )
                    continue
                try:
                    # V2 SDK: cancel_order takes an OrderPayload, not a bare string.
                    from py_clob_client_v2.clob_types import OrderPayload
                    self.client.cancel_order(OrderPayload(orderID=oid))
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
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
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
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
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
            # V2 SDK: get_open_orders replaces V1's get_orders.
            exchange_orders = self.client.get_open_orders() or []
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
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
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
                # FX-007: skip cids the bot has already confirmed have a
                # dead orderbook. Without this gate, the scan keeps finding
                # the same on-chain CTF balance (the orphan tokens never
                # leave the wallet — CTF redemption is manual UI-only) and
                # re-queueing a dump that immediately 400s.
                if self.db.is_unliquidatable(cid):
                    continue
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
                    # FX-066 Tier 2: reconstruct cost basis from the fills the
                    # orphan scan already keyed on (this scan only considers
                    # cids with fills in the last 7d, reward_farmer.py:565), so
                    # the registered position has a real avg_price instead of 0.
                    # Fixes dump pnl (Tier 1 only floored it), per-market ROI,
                    # AND notional-guardrail visibility at the source.
                    _fs, _vwap = self.db.fills_vwap(cid, side)
                    self.positions.set_shares(
                        cid, side, actual,
                        avg_price=_vwap if _vwap > 0 else None,
                    )
                    log.warning(
                        f"ORPHAN FOUND: {side.upper()} {actual:.0f}sh on exchange "
                        f"(cost basis {'reconstructed' if _vwap > 0 else 'UNKNOWN — Tier 1 floor at dump'}) | "
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
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

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
                # FX-007: skip cids the bot has already confirmed have a
                # dead orderbook. The CTF balance on-exchange never clears
                # (CTF redemption is manual UI-only); without this gate
                # the 30-min sync would re-register the same orphan into
                # self.markets every cycle indefinitely. The downstream
                # dump_position gate blocks the SELL, but the MarketState
                # would still accumulate.
                if self.db.is_unliquidatable(cid):
                    continue
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
                    # FX-066 Tier 2: reconstruct cost basis from fills if any
                    # exist. This (data-api) path can find true orphans with NO
                    # local fills (prior deployment) → fills_vwap returns 0 →
                    # avg_price left unset → Tier 1 floor handles the dump.
                    _fs, _vwap = self.db.fills_vwap(cid, side)
                    self.positions.set_shares(
                        cid, side, info["shares"],
                        avg_price=_vwap if _vwap > 0 else None,
                    )
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
                # FX-070: DumpManager has no `dump_states` attribute — the
                # active-dump signal lives on the per-market MarketState as
                # ms.dump_state[side] / ms.dump_orders[side]. The old hasattr()
                # guard was therefore always False, so a position mid-dump could
                # be removed here (stranding it + losing its loss-accounting
                # trail). Check the real in-memory state instead.
                ms = self.markets.get(cid)
                is_dumping = bool(ms) and any(
                    ms.dump_state[side] or ms.dump_orders[side]
                    for side in ("yes", "no")
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
        """Restore dump states from DB after crash/restart.

        FX-008: silently drops rows for unliquidatable cids. Without this
        gate, a restart would re-load the failing Tamilaga-style dump_state
        and re-trigger the 400 spam loop, since the in-memory
        unliquidatable cache is loaded at the same startup phase as this
        method (both before the main farming loop).
        """
        if self.dry_run:
            return
        saved = self.db.load_all_dump_states()
        if not saved:
            return
        restored = 0
        cleaned = 0
        unliquidatable_skipped = 0
        for (cid, side), state in saved.items():
            ms = self.markets.get(cid)
            if not ms:
                self.db.delete_dump_state(cid, side)
                cleaned += 1
                continue
            if self.db.is_unliquidatable(cid):
                # FX-008: the orderbook is dead; the saved dump_state would
                # just re-trigger the 400 loop. Drop the row and move on.
                self.db.delete_dump_state(cid, side)
                unliquidatable_skipped += 1
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
        if restored or cleaned or unliquidatable_skipped:
            log.info(
                f"Dump state recovery: restored {restored}, cleaned {cleaned}, "
                f"skipped {unliquidatable_skipped} on unliquidatable cids"
            )

    def _reprobe_unliquidatable(self):
        """FX-028: periodic re-probe of unliquidatable cids.

        For each cid whose ``last_retry_at`` is older than
        ``RF_UNLIQUIDATABLE_REPROBE_SECS`` (default 6h), call
        ``get_merged_book``. If both YES + NO books return data → un-mark
        (the orderbook has come back to life, very rare but possible).
        Otherwise stamp ``last_retry_at`` and leave the row in place.

        Called from the main loop on a cycle-counter cadence, NOT every
        cycle, so the cost is bounded. The DB query already filters
        on staleness, so an empty result set returns immediately.
        """
        if self.dry_run:
            return
        try:
            stale_secs = cfg("RF_UNLIQUIDATABLE_REPROBE_SECS")
            candidates = self.db.get_unliquidatable_for_reprobe(stale_secs)
            if not candidates:
                return
            from market_discovery import get_merged_book
            unmarked = 0
            still_dead = 0
            for cid, _last_retry in candidates:
                # We need token_ids to probe. Try the in-memory markets
                # first; fall back to a CLOB market lookup if absent.
                yes_tid = None
                no_tid = None
                ms = self.markets.get(cid)
                if ms:
                    yes_tid, no_tid = ms.yes_tid, ms.no_tid
                else:
                    try:
                        import requests
                        resp = requests.get(
                            f"https://clob.polymarket.com/markets/{cid}", timeout=10,
                        )
                        if resp.status_code == 200:
                            tokens = resp.json().get("tokens") or []
                            if len(tokens) >= 2:
                                yes_tid = tokens[0]["token_id"]
                                no_tid = tokens[1]["token_id"]
                    except Exception:
                        pass
                if not yes_tid or not no_tid:
                    # Can't probe without token_ids — leave alone, stamp retry.
                    self.db.update_unliquidatable_retry(cid)
                    still_dead += 1
                    continue
                merged = get_merged_book(self.client, yes_tid, no_tid)
                if merged and merged.get("bids") and merged.get("asks"):
                    log.info(
                        f"Re-enabling {cid[:16]}: orderbook returned to life "
                        f"after being marked unliquidatable"
                    )
                    self.db.delete_unliquidatable(cid)
                    unmarked += 1
                else:
                    self.db.update_unliquidatable_retry(cid)
                    still_dead += 1
            if unmarked or still_dead:
                log.info(
                    f"Unliquidatable re-probe: {unmarked} un-marked, "
                    f"{still_dead} still dead"
                )
        except Exception as e:
            log.debug(f"Unliquidatable re-probe failed: {e}")

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
            # FX-068: honor the oversight-side kill switch. simple_allocator
            # writes kill_switch=True (+ deploys=[]) when check_kill_switch
            # fires (24h loss > 10% wallet, or 15% drawdown). The alloc is
            # fresh here (past the TTL gate above). Capture it so run_cycle
            # can act on it; without this the empty-deploy `return None`
            # below silently drops the kill.
            self._alloc_kill_switch = bool(data.get("kill_switch"))
            self._alloc_kill_reason = str(data.get("kill_reason", ""))[:200]
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
                            self._gated_cancel_order(slot.order_id, reason="not_in_allocation")
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
                    self._gated_cancel_order(oid, reason="market_removed")
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
                            self._gated_cancel_order(slot.order_id, reason="resize")
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

            # Market expiring within 1 hour — cancel all orders.
            # Use _gated_cancel_order so non-LIVE modes log intent
            # instead of cancelling real orders (§3.2). Stub-safe:
            # unit-test fixtures call _sweep_expiring_markets on a
            # minimal FarmerStub that doesn't define _gated_cancel_order;
            # the getattr fallback drops to the raw (mocked) cancel so
            # those tests continue to pass.
            _cancel = getattr(
                self, "_gated_cancel_order",
                self.order_lifecycle.cancel_order,
            )
            for side in ("yes", "no"):
                slot = ms.orders[side]
                if slot.order_id:
                    _cancel(slot.order_id, reason="expiry_sweep")
                    self.db.delete_active_order(slot.order_id)
                    ms.orders[side] = OrderSlot()

                # Cancel dump orders too — no point selling into a resolving market
                dump_oid = ms.dump_orders[side]
                if dump_oid:
                    _cancel(dump_oid, reason="expiry_sweep")
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
        """Read `_total_capital` from the current allocation JSON.

        FX-043 resolution order (lifts the silent fail-open on 0-deploy
        cycles observed 2026-05-21 19:50-19:54 UTC):
          1. Top-level metadata `_total_capital` (preferred; FX-043 stamp
             added in simple-1.1 alloc-file version).
          2. First DEPLOY row's `_total_capital` (legacy; pre-FX-043).
          3. First AVOID row's `_total_capital` (further fallback;
             SimpleAllocator stamps it on every row).
          4. None — warns + farmer falls open.

        Net effect: any cycle whose allocator successfully ran (even with
        0 deploys) carries a usable capital signal for all wallet-fraction
        guardrails. Only a genuinely missing/corrupted alloc file falls
        open now.
        """
        alloc_path = os.path.join(
            os.path.dirname(__file__) or ".", "market_allocations.json",
        )
        try:
            if not os.path.exists(alloc_path):
                log.warning(
                    "[GUARDRAIL_WARNING] missing_signal=total_capital "
                    "(alloc file not found)"
                )
                return None
            with open(alloc_path) as f:
                data = json.load(f)
            # FX-043 step 1: prefer top-level metadata stamp
            tc = data.get("_total_capital")
            if tc is not None:
                return float(tc)
            # FX-043 step 2-3: fall back to any row's stamp (deploy first,
            # then avoid). Iterating once is O(N) but N is small (≤200).
            deploy_tc = None
            avoid_tc = None
            for m in data.get("markets", []):
                row_tc = m.get("_total_capital")
                if row_tc is None:
                    continue
                if m.get("action") == "deploy" and deploy_tc is None:
                    deploy_tc = float(row_tc)
                elif m.get("action") == "avoid" and avoid_tc is None:
                    avoid_tc = float(row_tc)
            if deploy_tc is not None:
                return deploy_tc
            if avoid_tc is not None:
                return avoid_tc
            log.warning(
                "[GUARDRAIL_WARNING] missing_signal=total_capital "
                "(no top-level or row-level _total_capital stamp in alloc)"
            )
        except Exception as e:
            log.warning(
                f"[GUARDRAIL_WARNING] missing_signal=total_capital (error: {e})"
            )
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
            log.warning(
                f"[GUARDRAIL_WARNING] missing_signal=cluster_data (error: {e})"
            )
            return {}, {}
        for cid, n in live_notional.items():
            cl = clusters.get(cid)
            cluster_by_cid[cid] = cl
            if cl is None:
                continue
            cluster_notional[cl] = cluster_notional.get(cl, 0.0) + n
        return cluster_notional, cluster_by_cid

    def _guardrail_current_cf(self) -> float | None:
        """Latest smoothed correction_factor. None on missing / error;
        emits [GUARDRAIL_WARNING] so the CF-kill gap is visible."""
        try:
            conn = self.db._get_conn()
            row = conn.execute(
                "SELECT correction_factor FROM reward_daily "
                "ORDER BY date DESC LIMIT 1"
            ).fetchone()
            if row is None:
                log.warning(
                    "[GUARDRAIL_WARNING] missing_signal=cf "
                    "(no reward_daily row yet)"
                )
                return None
            return float(row[0])
        except Exception as e:
            log.warning(
                f"[GUARDRAIL_WARNING] missing_signal=cf (error: {e})"
            )
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

    def _guardrail_short_realized_loss(self) -> float | None:
        """Realized loss (positive = how much we lost) over the fill-rate SHORT
        window (GUARDRAIL_FILLRATE_SHORT_SECS, 1h). Loss-gates the fill-rate spike
        kill: a burst of benign min_size fills that exit flat shows ~0 here and so
        must NOT halt the farmer. None on DB error — the caller then FAIL-SAFES to
        the legacy ratio-only kill (we never relax protection on missing data)."""
        try:
            conn = self.db._get_conn()
            cutoff = time.time() - GUARDRAIL_FILLRATE_SHORT_SECS
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM unwinds "
                "WHERE ts > ? AND pnl < 0",
                (cutoff,),
            ).fetchone()
            if row is None:
                return 0.0
            return -float(row[0] or 0.0)
        except Exception as e:
            log.debug(f"[GUARDRAIL] short realized loss read failed: {e}")
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
            # FX-069: read the kill-switch history (pruned only to the 6h
            # baseline), NOT ms.fill_times — can_place prunes fill_times to a
            # 180s window, which previously starved this baseline and blinded
            # the spike kill to slow bleed.
            times = ms.kill_fill_times if isinstance(ms.kill_fill_times, list) else []
            for t in times:
                if t >= base_cutoff:
                    base_count += 1
                    if t >= short_cutoff:
                        short_count += 1
        min_baseline = cfg("RF_FILL_RATE_MIN_BASELINE")
        if min_baseline is None:
            min_baseline = MIN_FILL_BASELINE
        if base_count < min_baseline:
            log.warning(
                f"[GUARDRAIL_WARNING] missing_signal=fill_rate "
                f"(baseline={base_count} < MIN_FILL_BASELINE={min_baseline})"
            )
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

    def _guardrail_rapid_notional_growth(
        self, notional_ratio: float | None,
    ) -> tuple[bool, float | None]:
        """FX-058: detect a rapid burst in notional_ratio across the
        RAPID_GROWTH_WINDOW_SEC lookback.

        Returns (kill, observed_multiplier). kill=True when:
          (a) RAPID_GROWTH_KILL_RATIO > 0  (i.e., not disabled)
          (b) we have at least 2 samples in the window
          (c) max_in_window / min_in_window > RAPID_GROWTH_KILL_RATIO
          (d) FX-087: the window MINIMUM is >= RAPID_GROWTH_MIN_BASELINE_RATIO
              (an established operating baseline). A ramp UP from ~0 (cold
              start / post-kill-cancel / a sub-baseline canary) is normal
              startup, NOT an anomalous burst, and is bounded by the static
              soft/hard notional caps — so the kill stays disarmed there.

        The acceleration-based check is the load-bearing kill protection
        under the OverCommitAllocator (FX-052/053). The static thresholds
        (MAX/HARD_NOTIONAL_RATIO 5/8) intentionally permit healthy
        overcommit; this catches the misconfigured-allocator scenario
        (e.g., deploying 10× normal) while permitting normal 3-8× operation.

        Fail-open semantics:
          - kill_disabled (kill_ratio <= 0) → never kills
          - missing signal (notional_ratio is None) → leaves deque
            unchanged so a DB hiccup can't reset the burst-detection
            window AND can't trigger a false kill
        """
        kill_ratio = RAPID_GROWTH_KILL_RATIO()
        if kill_ratio is None or kill_ratio <= 0:
            return False, None
        if notional_ratio is None:
            return False, None
        now = time.time()
        window = RAPID_GROWTH_WINDOW_SEC()
        self._notional_ratio_samples.append((now, float(notional_ratio)))
        # Evict samples older than the window.
        cutoff = now - window
        while (
            self._notional_ratio_samples
            and self._notional_ratio_samples[0][0] < cutoff
        ):
            self._notional_ratio_samples.popleft()
        if len(self._notional_ratio_samples) < 2:
            return False, None
        vals = [v for _, v in self._notional_ratio_samples]
        lo = min(vals)
        hi = max(vals)
        # FX-087: the burst ratio is only meaningful relative to an ESTABLISHED
        # operating baseline. On cold start (first live placement) or right
        # after a kill-cancel, the window minimum is ~0, so hi/lo explodes and
        # FALSELY trips on the very first orders — the canary's opening
        # placement (0 -> 0.16x) read as a 1578x "burst" and sticky-killed the
        # farmer; this would fire on EVERY live restart's ramp-up. A ramp UP
        # from a near-zero baseline is normal startup, not anomalous
        # acceleration; the dangerous high-exposure case is already bounded by
        # the static soft/hard notional caps. Only treat growth as a kill-worthy
        # burst once the window minimum is itself a real operating level.
        baseline_floor = RAPID_GROWTH_MIN_BASELINE_RATIO()
        if baseline_floor and baseline_floor > 0 and lo < baseline_floor:
            return False, None
        if lo <= 0.0001:
            # Baseline guard disabled (<=0) AND a genuine ~0 minimum → the burst
            # ratio is undefined; never divide by ~0 or false-kill on it.
            return False, None
        observed = hi / lo
        return observed > kill_ratio, observed

    def _guardrail_oversight_silence_drawdown(
        self, total_live_notional: "float | None",
    ) -> "tuple[bool, str]":
        """FX-082 — farmer-side drawdown backstop for oversight silence.

        The 15% drawdown kill normally lives ONLY in the oversight process
        (simple_allocator.check_kill_switch, written into market_allocations.json).
        If oversight dies or wedges (FX-080 did exactly this for ~2 days) the
        alloc file stops being rewritten; the farmer's stale-alloc handling only
        BLOCKS NEW orders — it does not cancel existing positions or trip any
        drawdown kill, so live exposure can ride a >15% drawdown unprotected.

        Fires the farmer's own sticky kill when, AND ONLY when, all of:
          (1) oversight has been silent past RF_OVERSIGHT_SILENCE_KILL_HOURS
              (alloc-file mtime age via self._alloc_mtime),
          (2) the farmer holds live notional (something is at risk), and
          (3) a farmer-computed drawdown (current wallet vs the
              portfolio_snapshots high-water mark) exceeds
              RF_FARMER_DRAWDOWN_KILL_FRAC.
        Gated on silence so it never duplicates/fights the oversight limb while
        oversight is alive.

        Fail-safe: ANY missing signal (disabled knob, no alloc loaded yet, no
        exposure, no peak, no current wallet, non-positive peak) returns
        (False, "") — a healthy bot is never falsely killed (mirrors the
        missing-signal-leaves-counter-unchanged pattern above).
        """
        silence_hours = cfg("RF_OVERSIGHT_SILENCE_KILL_HOURS")
        if not silence_hours or silence_hours <= 0:
            return False, ""                       # disabled
        if not self._alloc_mtime:                  # no alloc ever loaded → no signal
            return False, ""
        silent_secs = time.time() - self._alloc_mtime
        if silent_secs < silence_hours * 3600.0:
            return False, ""                       # oversight recent → it owns drawdown
        if not total_live_notional or total_live_notional <= 0:
            return False, ""                       # nothing at risk
        peak = self.db.get_wallet_peak_usd()
        cash, _ts = self.db.load_usdc_balance()
        if peak is None or cash is None or peak <= 0:
            return False, ""                       # missing wallet signal → fail-open
        from portfolio_value import compute_portfolio_value
        positions = self.db.load_all_positions()
        mids = {
            cid: getattr(ms, "midpoint", 0.0) or 0.0
            for cid, ms in self.markets.items()
        }
        current = compute_portfolio_value(float(cash), positions, mids)
        if current <= 0:
            current = float(cash)
        drawdown = 1.0 - (current / peak)
        frac = cfg("RF_FARMER_DRAWDOWN_KILL_FRAC")
        if drawdown > frac:
            return True, (
                f"oversight_silent={silent_secs / 3600.0:.2f}h > {silence_hours}h "
                f"AND farmer_drawdown={drawdown:.1%} > {frac:.0%} "
                f"(peak=${peak:.2f}, portfolio=${current:.2f}, cash=${cash:.2f}, "
                f"live_notional=${total_live_notional:.2f})"
            )
        return False, ""

    def _guardrail_unrealized_loss(
        self, total_capital: "float | None",
    ) -> "tuple[bool, str]":
        """FX-084 — held-inventory (unrealized) loss kill.

        The other kill limbs only see REALIZED loss (unwinds.pnl<0) and
        wallet-CASH drawdown (peak vs current balance). Neither catches a
        marked-down OPEN position or an FX-071 floored-but-unfilled dump that
        bleeds without ever crystallizing a negative unwind or lowering the cash
        peak. This limb marks every held leg to the market midpoint and trips
        the sticky kill when NET unrealized loss exceeds
        RF_UNREALIZED_LOSS_KILL_FRAC of total_capital.

        Mark per leg (YES-equivalent avg per state.py): YES pnl =
        yes_shares·(mid − yes_avg); NO pnl = no_shares·(no_avg − mid)
        [= no_shares·((1−mid) − (1−no_avg))]. Per-leg loss capped at cost
        basis. Midpoint from the live MarketState.

        Fail-open (returns (False, "")) on any missing signal: disabled knob,
        no/zero total_capital, positions read error, or nothing markable. A leg
        is skipped (not counted) when its cost basis is unknown (avg_price<=0,
        e.g. orphan/startup positions handled by FX-066/074) or its market has
        no fresh/valid midpoint (requires 0 < mid < 1). Net (gains offset
        losses) so a single noisy mark cannot trip the kill alone.
        """
        self._last_unrealized_loss = None
        frac = cfg("RF_UNREALIZED_LOSS_KILL_FRAC")
        if not frac or frac <= 0:
            return False, ""                          # disabled
        if total_capital is None or total_capital <= 0:
            return False, ""                          # no denominator → fail-open
        try:
            positions = self.db.load_all_positions()
        except Exception:
            return False, ""                          # read error → fail-open
        if not positions:
            return False, ""
        from portfolio_mark import portfolio_unrealized_loss

        leg_rows: list[tuple[str, float, float, float]] = []
        for cid, pos in positions.items():
            ms = self.markets.get(cid)
            if ms is None:
                continue
            mid = getattr(ms, "midpoint", 0.0) or 0.0
            ys = float(pos.get("yes_shares", 0.0) or 0.0)
            ya = float(pos.get("yes_avg_price", 0.0) or 0.0)
            ns = float(pos.get("no_shares", 0.0) or 0.0)
            na = float(pos.get("no_avg_price", 0.0) or 0.0)
            if ys > 0 and ya > 0:
                leg_rows.append(("yes", ys, ya, mid))
            if ns > 0 and na > 0:
                leg_rows.append(("no", ns, na, mid))
        unrealized_loss, marked_legs = portfolio_unrealized_loss(leg_rows)
        if marked_legs == 0:
            return False, ""                          # nothing markable → fail-open
        self._last_unrealized_loss = unrealized_loss
        limit = frac * total_capital
        if unrealized_loss > limit:
            return True, (
                f"unrealized_loss=${unrealized_loss:.2f} > {frac:.0%}·T "
                f"(=${limit:.2f}) across {marked_legs} marked leg(s)"
            )
        return False, ""

    def _emit_and_check_heartbeat(self) -> None:
        """FX-083: write the farmer's own liveness heartbeat and page if the
        OVERSIGHT peer's heartbeat has gone stale.

        Heartbeat write is mode-independent — a dry shadow is still a live
        process whose stall must be detectable. The oversight check fires a
        Discord page at RF_OVERSIGHT_HEARTBEAT_STALE_SECS (~1h), BEFORE the 2h
        FX-082 drawdown backstop, so a human is warned before the kill. Fully
        fail-open: any error here must never break a trading cycle.
        """
        try:
            self.db.record_heartbeat("farmer")
        except Exception as e:
            log.debug(f"[HEARTBEAT] farmer record failed (fail-open): {e}")
        try:
            ts = self.db.get_heartbeat("oversight")
            if alerts.maybe_alert_stale_heartbeat(
                "oversight",
                ts,
                time.time(),
                cfg("RF_OVERSIGHT_HEARTBEAT_STALE_SECS"),
                cfg("RF_HEARTBEAT_REPAGE_SECS"),
            ):
                log.warning(
                    "[HEARTBEAT] oversight peer heartbeat STALE — Discord paged"
                )
        except Exception as e:
            log.debug(f"[HEARTBEAT] oversight check skipped (fail-open): {e}")

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

        # Persistent-breach tracking (spec §5.1–§5.3). Count only when
        # we have a signal; missing signal leaves the counter unchanged
        # so a DB hiccup can't mask nor reset a real breach.
        if notional_ratio is None:
            pass  # no signal — leave counter as-is
        elif notional_ratio > HARD_NOTIONAL_RATIO():
            self._consecutive_hard_notional_breach_cycles += 1
        else:
            self._consecutive_hard_notional_breach_cycles = 0
        if (
            self._consecutive_hard_notional_breach_cycles >= MAX_BREACH_CYCLES
            and notional_ratio is not None
        ):
            log.error(
                f"[CRITICAL] persistent_overexposure "
                f"{json.dumps({'notional_ratio': round(notional_ratio, 4), 'cycles': self._consecutive_hard_notional_breach_cycles}, sort_keys=True)}"
            )

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
        spike_factor = cfg("RF_FILL_RATE_SPIKE_FACTOR")
        if spike_factor is None:
            spike_factor = FILL_RATE_SPIKE_FACTOR
        if fill_ratio is not None and fill_ratio > spike_factor:
            # LOSS-GATE: a fill-rate spike only escalates to a KILL when it
            # coincides with real short-window realized loss. A spike on benign
            # min_size fills that exit flat (loss below the gate) is logged but
            # does NOT halt — the 10%/24h realized-loss kill and the 20%
            # unrealized-loss kill remain the money backstops. FAIL-SAFE: if the
            # short loss or total_capital can't be computed, preserve the legacy
            # ratio-only kill (never relax protection on missing data).
            loss_frac = cfg("RF_FILL_RATE_KILL_LOSS_FRAC")
            short_loss = self._guardrail_short_realized_loss()
            loss_gate = (
                loss_frac * total_capital
                if (loss_frac is not None and loss_frac > 0
                    and total_capital is not None and total_capital > 0)
                else None
            )
            benign = (
                short_loss is not None
                and loss_gate is not None
                and short_loss <= loss_gate
            )
            if benign:
                log.warning(
                    f"[GUARDRAIL_WARNING] fill_rate_spike ratio={fill_ratio:.2f} "
                    f"(short={short_fills}, baseline={base_fills}) but 1h realized "
                    f"loss ${short_loss:.2f} <= gate ${loss_gate:.2f} "
                    f"({loss_frac:.2%}·T) — benign, NOT killing"
                )
            else:
                reason = (
                    f"fill_rate_ratio={fill_ratio:.2f} > {spike_factor}× "
                    f"(short={short_fills}, baseline={base_fills})"
                )
                if short_loss is not None and loss_gate is not None:
                    reason += f" + 1h_loss=${short_loss:.2f} > ${loss_gate:.2f}"
                else:
                    reason += " (loss/capital unknown — fail-safe kill)"
                kill_reasons.append(reason)
        # FX-058: rapid-growth (acceleration) kill. Catches anomalous bursts
        # like a misconfigured allocator deploying 10× normal. Loose static
        # thresholds (MAX 5×, HARD 8×) intentionally permit healthy
        # overcommit; this check is the load-bearing kill protection.
        rapid_kill, rapid_mult = self._guardrail_rapid_notional_growth(
            notional_ratio
        )
        if rapid_kill:
            kill_reasons.append(
                f"notional_ratio_burst={rapid_mult:.2f}× over "
                f"{RAPID_GROWTH_WINDOW_SEC():.0f}s > "
                f"{RAPID_GROWTH_KILL_RATIO():.2f}× kill threshold"
            )

        # FX-082: farmer-side drawdown backstop when oversight goes silent.
        # The 15% drawdown kill otherwise lives only in oversight; if oversight
        # dies/wedges while the farmer holds exposure, this is the only drawdown
        # protection left. Fail-open on any missing signal.
        silence_kill, silence_reason = self._guardrail_oversight_silence_drawdown(
            total_live_notional
        )
        if silence_kill:
            kill_reasons.append(silence_reason)

        # FX-084: held-inventory (unrealized) loss kill. Catches marked-down
        # open positions / FX-071 floored-but-unfilled dumps that the
        # realized-loss and cash-drawdown limbs structurally miss. Fail-open on
        # any missing signal (sets self._last_unrealized_loss for telemetry).
        unrealized_kill, unrealized_reason = self._guardrail_unrealized_loss(
            total_capital
        )
        if unrealized_kill:
            kill_reasons.append(unrealized_reason)

        notional_block = (
            notional_ratio is not None
            and notional_ratio > MAX_NOTIONAL_RATIO()
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
            "unrealized_loss": (
                round(self._last_unrealized_loss, 2)
                if getattr(self, "_last_unrealized_loss", None) is not None
                else None
            ),
            "cf": round(cf, 6) if cf is not None else None,
            "oversight_silent_secs": (
                round(time.time() - self._alloc_mtime, 1)
                if self._alloc_mtime else None
            ),
            "notional_block": notional_block,
            "kill_switch": bool(kill_reasons),
        }
        log.info(f"[GUARDRAIL] {json.dumps(tele, sort_keys=True)}")

        # Previous-cycle order/cancel counters from the rolling-stats deque
        # so the oversight shadow evaluator can compute cancel-pressure
        # (signal D, §4.21). Empty on the first cycle; falls back to 0.
        prev_stat = self._rolling_stats[-1] if self._rolling_stats else {}
        result = {
            "kill_switch": bool(kill_reasons),
            "kill_reason": "; ".join(kill_reasons),
            "notional_block": notional_block,
            "blocked_clusters": blocked_clusters,
            "cluster_by_cid": cluster_by_cid,
            "cluster_notional": cluster_notional,
            "live_by_cid": live_by_cid,
            "total_live_notional": total_live_notional,
            "notional_ratio": notional_ratio,
            "total_capital": total_capital,
            "cf": cf,
            "daily_loss": daily_loss,
            "orders_placed_prev_cycle": int(prev_stat.get("orders", 0) or 0),
            "orders_cancelled_prev_cycle": int(prev_stat.get("cancels", 0) or 0),
        }
        # Cached for the cycle-summary emitter so it doesn't re-query.
        self._last_guard = result
        return result

    def _activate_kill_switch(self, reason: str) -> None:
        """Atomic halt (spec §5.1): set flag → cancel every live order
        → log event → caller returns immediately. No further logic
        runs in this cycle. Operator must restart the process to
        resume — deliberate: the trigger conditions all benefit from
        human eyes-on before re-entry."""
        # 1. Flag FIRST so any concurrent re-entry short-circuits.
        self._kill_switch_active = True
        self._kill_switch_reason = reason
        self._kill_switch_triggered_at = time.time()

        # 2. Cancel ALL live orders immediately (BUY slots + dump SELLs).
        # All cancels go through _gated_cancel_order. Because
        # _kill_switch_active is True (set in step 1 above), its
        # force_execute path is taken and real cancels fire regardless
        # of self.mode — spec §5.1 "kill switch ALWAYS executes real
        # cancellations, IGNORE mode". The wrapper also increments
        # _cycle_orders_cancelled internally.
        cancelled = 0
        for ms in list(self.markets.values()):
            for side in ("yes", "no"):
                slot = ms.orders.get(side)
                if slot and slot.order_id:
                    if self._gated_cancel_order(
                        slot.order_id, reason="kill_switch",
                    ):
                        cancelled += 1
                dump_oid = (
                    ms.dump_orders.get(side)
                    if isinstance(ms.dump_orders, dict) else None
                )
                if dump_oid:
                    if self._gated_cancel_order(
                        dump_oid, reason="kill_switch_dump",
                    ):
                        cancelled += 1

        # 3. Log event (one loud line + one structured record).
        log.error(
            f"[GUARDRAIL] KILL SWITCH ACTIVATED: {reason} "
            f"— cancelled {cancelled} live orders"
        )
        log.info(
            f"[GUARDRAIL] "
            f"{json.dumps({'event': 'kill_switch_activated', 'cycle': self.cycle_count, 'reason': reason, 'cancelled_orders': cancelled, 'ts': round(time.time(), 3)}, sort_keys=True)}"
        )
        # 3b. FX-092: page the operator. A kill leaves THIS process alive-but-
        # idle, so the stale-heartbeat alert never fires for it — without this
        # page nobody is told the farmer stopped trading. Fail-safe (the kill
        # must complete even if Discord is down).
        try:
            alerts.alert_kill_switch(reason, cancelled)
        except Exception as e:
            log.warning(f"[GUARDRAIL] kill-switch Discord alert failed (non-fatal): {e}")
        # 4. Caller returns from run_cycle — see integration in run_cycle.

    # ── Hard enforcement helpers ────────────────────────────────────
    # Soft guardrails (§3.1 notional_block, §4 cluster block) prevent
    # growth. Hard enforcement actively reduces exposure that's already
    # past the hard thresholds (can happen if fills land between cycles
    # or if allocation/cap policy drift puts existing orders over). All
    # cancellations are incremental: sort by priority, cancel one at a
    # time, stop as soon as the threshold is cleared.

    def _guardrail_cancellation_order(
        self, filter_cids: set[str] | None = None,
    ) -> list[tuple]:
        """Return live BUY orders in LOWEST-priority-first order.

        Priority key (ascending sort, spec §4.1):
            (daily_rate, -notional, -max_spread, cid, side)
        → lowest reward first; within equal reward, largest notional
        first (remove biggest exposure); within equal notional, highest
        spread first (risk proxy); then deterministic string tiebreak.
        Intent (§4.2): remove lowest value first, and within that,
        remove largest risk first. Dump sell orders are intentionally
        excluded — they exist to flatten a filled position and cancelling
        them would leave the position stranded. The kill-switch path
        still cancels dumps because it's terminal.
        """
        cands: list[tuple] = []
        for cid, ms in self.markets.items():
            if filter_cids is not None and cid not in filter_cids:
                continue
            for side in ("yes", "no"):
                slot = ms.orders.get(side)
                if not slot or not slot.order_id:
                    continue
                notional = float(slot.price or 0.0) * float(slot.shares or 0.0)
                if notional <= 0:
                    continue
                key = (
                    float(ms.daily_rate or 0.0),
                    -notional,
                    -float(ms.max_spread or 0.0),
                    str(cid), side,
                )
                cands.append((key, ms, side, slot, notional))
        cands.sort(key=lambda c: c[0])
        return cands

    def _guardrail_cancel_slot(
        self, ms: "MarketState", side: str, slot: "OrderSlot", reason: str,
    ) -> bool:
        """Cancel + clear slot + delete DB entry. Matches the existing
        expiry_sweep pattern. Returns True on successful cancel.
        Intent logging + counter increment live inside
        _gated_cancel_order."""
        oid = slot.order_id
        if not oid:
            return False
        ok = self._gated_cancel_order(oid, reason=reason)
        if ok:
            try:
                self.db.delete_active_order(oid)
            except Exception:
                pass
            ms.orders[side] = OrderSlot()
        return ok

    def _guardrail_hard_enforce_notional(
        self, total_capital: float | None, total_live_notional: float,
    ) -> int:
        """Cancel lowest-priority orders until
        notional_ratio ≤ MAX_NOTIONAL_RATIO once ratio has exceeded
        HARD_NOTIONAL_RATIO. Incremental: one cancel per iteration,
        stops as soon as the running total drops under the soft cap."""
        if total_capital is None or total_capital <= 0:
            return 0
        hard_ratio = HARD_NOTIONAL_RATIO()
        soft_ratio = MAX_NOTIONAL_RATIO()
        hard_usd = hard_ratio * total_capital
        soft_usd = soft_ratio * total_capital
        if total_live_notional <= hard_usd:
            return 0
        log.error(
            f"[GUARDRAIL] HARD notional breach: "
            f"${total_live_notional:.2f} > {hard_ratio}·T=${hard_usd:.2f} "
            f"— cancelling to ≤ {soft_ratio}·T=${soft_usd:.2f}"
        )
        cancelled = 0
        running = total_live_notional
        for key, ms, side, slot, notional in self._guardrail_cancellation_order():
            if running <= soft_usd:
                break
            if cancelled >= MAX_CANCELS_PER_CYCLE:
                log.warning(
                    f"[GUARDRAIL] hard-notional cancel cap reached "
                    f"({MAX_CANCELS_PER_CYCLE}/cycle); residual breach "
                    f"${running - soft_usd:.2f} carries into next cycle"
                )
                break
            oid = slot.order_id
            if self._guardrail_cancel_slot(ms, side, slot, "notional_hard_enforce"):
                running -= notional
                cancelled += 1
                log.warning(
                    f"[GUARDRAIL] hard-notional cancel {oid} "
                    f"({ms.question[:40]}/{side}, notional=${notional:.2f}, "
                    f"daily_rate=${float(ms.daily_rate or 0.0):.2f}) "
                    f"→ running ${running:.2f}"
                )
        log.error(
            f"[GUARDRAIL] hard-notional enforcement cancelled {cancelled} orders "
            f"→ notional=${running:.2f}/${total_capital:.2f} "
            f"(ratio={running / max(total_capital, 1e-9):.3f})"
        )
        return cancelled

    def _guardrail_hard_enforce_clusters(
        self,
        total_capital: float | None,
        cluster_notional: dict[int, float],
        cluster_by_cid: dict[str, int | None],
    ) -> int:
        """For each cluster whose notional > 0.5·T, cancel lowest-
        priority members until the cluster drops back under the limit.
        Incremental per cluster; other clusters untouched."""
        if total_capital is None or total_capital <= 0:
            return 0
        limit = CLUSTER_NOTIONAL_LIMIT_FRAC * total_capital
        cancelled_total = 0
        for cl_id, cl_notional in cluster_notional.items():
            if cl_notional <= limit:
                continue
            member_cids = {
                cid for cid, cl in cluster_by_cid.items() if cl == cl_id
            }
            if not member_cids:
                continue
            log.error(
                f"[GUARDRAIL] HARD cluster breach: cluster={cl_id} "
                f"notional=${cl_notional:.2f} > "
                f"{CLUSTER_NOTIONAL_LIMIT_FRAC}·T=${limit:.2f} "
                f"— cancelling lowest-priority in cluster"
            )
            running = cl_notional
            cnt = 0
            for key, ms, side, slot, notional in self._guardrail_cancellation_order(
                filter_cids=member_cids,
            ):
                if running <= limit:
                    break
                if cnt >= MAX_CANCELS_PER_CYCLE:
                    log.warning(
                        f"[GUARDRAIL] cluster={cl_id} cancel cap reached "
                        f"({MAX_CANCELS_PER_CYCLE}/cycle); residual breach "
                        f"${running - limit:.2f} carries into next cycle"
                    )
                    break
                oid = slot.order_id
                if self._guardrail_cancel_slot(
                    ms, side, slot, "cluster_hard_enforce",
                ):
                    running -= notional
                    cnt += 1
                    log.warning(
                        f"[GUARDRAIL] cluster={cl_id} cancel {oid} "
                        f"(notional=${notional:.2f}) → running ${running:.2f}"
                    )
            log.error(
                f"[GUARDRAIL] cluster={cl_id} enforcement cancelled {cnt} orders "
                f"→ ${running:.2f} (limit ${limit:.2f})"
            )
            cancelled_total += cnt
        return cancelled_total

    # ── Dry-run + cycle telemetry helpers ───────────────────────────
    # Both are pure observability — they never change trading decisions.
    # Fail-open: any logging exception is swallowed at DEBUG level.

    def _log_dry_run_intent(self, action: str, **kv) -> None:
        """Emit a `[DRY_RUN|SHADOW] <action> {…json…}` line when the
        current mode is non-LIVE. Prefix is dynamic so DRY_RUN and
        SHADOW are visually separable in logs. No-op in LIVE mode and
        when self.mode is missing (stub fixtures default to LIVE)."""
        mode = getattr(self, "mode", MODE_LIVE)
        if mode == MODE_LIVE:
            return
        try:
            log.info(f"[{mode}] {action} {json.dumps(kv, sort_keys=True)}")
        except Exception as e:
            log.debug(f"[{mode}] log emit failed ({action}): {e}")

    def _gated_place_orders_for_market(self, ms) -> None:
        """Mode-gated wrapper around OrderLifecycle.place_orders_for_market
        (§4.2). In non-LIVE modes: emit structured intent log and return.
        In LIVE: delegate + accumulate the cycle counter by the count of
        API-confirmed placements returned by the wrapped call (FX-004).
        Returns None for backwards-compat; callers don't consume the value.

        Counter semantics (FX-004): _cycle_orders_placed accumulates only
        the LIVE-mode placements that received a valid orderID from the
        API and wrote a row to the orders_placed DB table. Early returns
        (no book, wide spread, sports block, resolution proximity, etc.)
        and API failures contribute 0. Telemetry's [CYCLE_SUMMARY]
        orders_placed therefore matches SELECT COUNT(*) FROM orders_placed
        for the cycle window — operator can trust the counter.

        Stub-safe: when self.mode is missing (test fixtures), defaults to
        LIVE so the delegated call still fires. AttributeError guard on
        the counter accumulation preserves the same stub-tolerance pattern
        the v5.0 wrapper used."""
        mode = getattr(self, "mode", MODE_LIVE)
        if mode != MODE_LIVE:
            self._log_dry_run_intent(
                "place_order", cid=ms.cid,
                question=str(ms.question)[:40],
            )
            return
        n_placed = self.order_lifecycle.place_orders_for_market(ms)
        # Defensive: pre-FX-004 stubs may return None; treat as 0.
        if not isinstance(n_placed, int):
            n_placed = 0
        try:
            self._cycle_orders_placed += n_placed
        except AttributeError:
            pass

    def _gated_cancel_order(self, order_id: str, reason: str = "") -> bool:
        """Mode-gated wrapper around OrderLifecycle.cancel_order (§4.2)
        with kill-switch override (§5.1). Returns True on successful
        cancel, False when skipped or failed. Logic:

            force_execute = self._kill_switch_active
            if mode != LIVE and not force_execute:
                log intent; return False
            else:
                execute cancel; increment counter

        Kill-switch override: §5.1 requires real cancels whenever the
        kill switch has been activated this cycle, even if the current
        mode is DRY_RUN / SHADOW — capital protection trumps mode
        safety. _activate_kill_switch sets the flag BEFORE looping
        through cancels, so every cancel it issues hits the LIVE path.
        Stub-safe defaults as in _gated_place_orders_for_market."""
        if not order_id:
            return False
        mode = getattr(self, "mode", MODE_LIVE)
        force_execute = bool(getattr(self, "_kill_switch_active", False))
        if mode != MODE_LIVE and not force_execute:
            self._log_dry_run_intent(
                "cancel_order", order_id=order_id, reason=reason,
            )
            return False
        try:
            # Phase 5 audit: propagate force_execute into OL so it bypasses
            # its own dry_run short-circuit. Without this, kill-switch /
            # shutdown cancels in SHADOW would log success but never call
            # the real API, leaking operator-poked orders.
            ok = bool(
                self.order_lifecycle.cancel_order(
                    order_id, reason=reason, force=force_execute,
                )
            )
        except Exception as e:
            log.debug(f"[GATED] cancel error {order_id}: {e}")
            return False
        if ok:
            try:
                self._cycle_orders_cancelled += 1
            except AttributeError:
                pass
        return ok

    def _emit_cycle_telemetry(self) -> None:
        """Emit one [CYCLE_SUMMARY] per run_cycle exit. Every
        ROLLING_STATS_EMIT_EVERY cycles, also emit [ROLLING_STATS]
        over the last ROLLING_STATS_WINDOW cycles. All arithmetic is
        deterministic; fail-open on any logging error."""
        try:
            g = self._last_guard or {}
            notional_ratio = g.get("notional_ratio")
            cluster_notional_vals = g.get("cluster_notional") or {}
            max_cluster = (
                max(cluster_notional_vals.values())
                if cluster_notional_vals else 0.0
            )
            summary = {
                "cycle": self.cycle_count,
                "ts": round(time.time(), 3),
                "active_markets": len(g.get("live_by_cid") or {}),
                "total_live_notional": round(
                    float(g.get("total_live_notional") or 0.0), 2,
                ),
                "notional_ratio": (
                    round(notional_ratio, 4)
                    if notional_ratio is not None else None
                ),
                "max_cluster_notional": round(max_cluster, 2),
                "cluster_count": len(cluster_notional_vals),
                "blocked_clusters": len(g.get("blocked_clusters") or ()),
                "orders_placed": self._cycle_orders_placed,
                "orders_cancelled": self._cycle_orders_cancelled,
                "kill_switch": bool(self._kill_switch_active),
                "realized_loss_24h": g.get("daily_loss"),
                "cf": g.get("cf"),
            }
            log.info(
                f"[CYCLE_SUMMARY] {json.dumps(summary, sort_keys=True)}"
            )

            # Push this cycle's sample into the rolling window.
            self._rolling_stats.append({
                "notional_ratio": (
                    float(notional_ratio) if notional_ratio is not None else 0.0
                ),
                "orders": self._cycle_orders_placed,
                "cancels": self._cycle_orders_cancelled,
            })

            if (
                self.cycle_count % ROLLING_STATS_EMIT_EVERY == 0
                and self._rolling_stats
            ):
                n = len(self._rolling_stats)
                ratios = [s["notional_ratio"] for s in self._rolling_stats]
                stats = {
                    "avg_notional_ratio": round(sum(ratios) / n, 4),
                    "max_notional_ratio": round(max(ratios), 4),
                    "avg_orders": round(
                        sum(s["orders"] for s in self._rolling_stats) / n, 3,
                    ),
                    "avg_cancels": round(
                        sum(s["cancels"] for s in self._rolling_stats) / n, 3,
                    ),
                }
                log.info(
                    f"[ROLLING_STATS] {json.dumps(stats, sort_keys=True)}"
                )
        except Exception as e:
            log.debug(f"[CYCLE_SUMMARY] emit failed: {e}")

    # ── Core Cycle ──────────────────────────────────────────────────

    def run_cycle(self):
        """One cycle: check dumps → detect fills → place orders → record rewards."""
        self.cycle_count += 1
        self.order_lifecycle.cycle_count = self.cycle_count
        self.order_lifecycle.capital_ceiling = None

        # Reset per-cycle observability counters + guard cache. These
        # populate the [CYCLE_SUMMARY] emit at every run_cycle exit.
        self._cycle_orders_placed = 0
        self._cycle_orders_cancelled = 0
        self._last_guard = None

        # FX-083: liveness heartbeat + oversight-peer staleness paging. Done at
        # cycle top, BEFORE the kill-switch short-circuit, so a halted-but-alive
        # farmer still records liveness and still pages on a dead oversight.
        self._emit_and_check_heartbeat()

        # Kill-switch short-circuit: if a prior cycle tripped the halt,
        # bail out immediately (no fills polled, no placements, no
        # reward recording). Reset requires a process restart.
        if self._kill_switch_active:
            if self.cycle_count % 10 == 0:
                log.warning(
                    f"[GUARDRAIL] kill switch ACTIVE: "
                    f"{self._kill_switch_reason} — skipping cycle"
                )
            self._emit_cycle_telemetry()
            return

        # FX-068: oversight-side kill switch (from market_allocations.json).
        # _load_allocations captures simple_allocator's kill_switch flag into
        # self._alloc_kill_switch on every fresh alloc load (the `run` loop
        # calls _check_allocation_update before run_cycle). Honor it as a REAL
        # halt — cancel-all + sticky — not just an empty deploy list. Guard on
        # not-already-active so it fires once, then the short-circuit above
        # owns every subsequent cycle. kill switch cancels fire in any mode.
        if self._alloc_kill_switch and not self._kill_switch_active:
            self._activate_kill_switch(reason="oversight:" + self._alloc_kill_reason)
            self._emit_cycle_telemetry()
            return

        # Step 1: Fetch exchange orders
        if self.dry_run:
            open_ids = set()
        else:
            try:
                # V2 SDK: get_open_orders replaces V1's get_orders.
                exchange_orders = self.client.get_open_orders() or []
            except Exception as e:
                log.error(f"get_orders failed: {e}")
                self._emit_cycle_telemetry()
                return
            open_ids = {o["id"] for o in exchange_orders}

        # FX-072: snapshot outstanding dumps BEFORE check_dump_fills so the
        # end-of-cycle drift sweep can recover a real concurrent BUY that a
        # dump drain masked (tracked overstates when an on-chain drain happens
        # without its unwind being recorded this cycle). Free/in-memory, no RPC.
        # Same OrderLifecycle instance that runs detect_fills below (Step 3).
        self.order_lifecycle.capture_pre_cycle_dumps()

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
            # Persist a marker row to the fills table so the oversight/agent
            # and post-incident analysis can see the storm. Typed, truthful
            # writer (replaces a no-op self.db.execute_sql call that never
            # existed and was swallowed by a bare except — the storm HALT above
            # via self._fill_storm_until always worked; the audit row did not).
            self.db.log_fill_storm_marker(now)

        # Step 4: Place orders on priority batch
        market_list = list(self.markets.values())
        if not market_list:
            self._emit_cycle_telemetry()
            return

        # Step 4pre: runtime safety guardrails. Runs AFTER allocation
        # (already consumed by market filtering upstream) and BEFORE
        # new placements. Emits structured telemetry every cycle.
        guard = self._guardrail_check_and_log()

        # Oversight evaluation (final safety layer). Single deterministic
        # call per cycle. hasattr() distinguishes "function not yet
        # implemented" (silent fallback to continue) from "function
        # exists but raised" (loud log.error). Decision contract:
        # {"action": "continue"|"pause"|"kill", "reason": str}.
        # `decision` / `action` / `reason` / `latency_ms` are all
        # cycle-local — never persisted on self.
        start = time.time()
        missing_evaluate = not hasattr(oversight_agent, "evaluate")
        if missing_evaluate:
            decision = {"action": "continue", "reason": "not_implemented"}
        else:
            try:
                decision = oversight_agent.evaluate(guard)
            except Exception as e:
                log.error("[OVERSIGHT_ERROR] evaluation failed: %s", e)
                decision = {"action": "continue", "reason": "error"}
        latency_ms = (time.time() - start) * 1000.0

        # Strict decision validation. action restricted to the contract's
        # three values; reason normalised to str and truncated to 200
        # chars to bound log volume from a misbehaving evaluator.
        if not isinstance(decision, dict):
            log.error(
                "[OVERSIGHT_ERROR] invalid decision type: %s", type(decision),
            )
            action = "continue"
            reason = "invalid"
        else:
            action = decision.get("action")
            reason = decision.get("reason", "")
            if action not in ("continue", "pause", "kill"):
                log.error("[OVERSIGHT_ERROR] invalid action: %s", action)
                action = "continue"
                reason = "invalid"
        reason = str(reason)[:200]

        if latency_ms > OVERSIGHT_LATENCY_WARN_MS:
            log.warning(
                "[OVERSIGHT_WARNING] slow evaluation: %.2fms > %dms",
                latency_ms,
                OVERSIGHT_LATENCY_WARN_MS,
            )

        # Decision log every cycle (full auditability — no throttle).
        log.info(
            "[OVERSIGHT] action=%s reason=%s latency_ms=%.2f",
            action,
            reason,
            latency_ms,
        )

        # Oversight kill — fires BEFORE the existing guard kill so an
        # oversight-driven halt isn't masked by a coincident guard
        # signal. Same atomic ordering as _activate_kill_switch (flag →
        # cancel → log) plus telemetry-once invariant: emit then return.
        if action == "kill":
            self._activate_kill_switch(reason="oversight:" + reason)
            self._emit_cycle_telemetry()
            return

        if guard["kill_switch"]:
            # Atomic halt: activate (flag → cancel → log) → return.
            # Nothing after this executes in the killed cycle.
            self._activate_kill_switch(guard["kill_reason"])
            self._emit_cycle_telemetry()
            return

        # Hard enforcement: cancel lowest-priority orders to bring
        # notional + cluster exposure back under their soft caps when
        # they've drifted past the hard thresholds. No-ops when within
        # bounds. Runs BEFORE the soft-block / fill-storm gate so a
        # breach is actively reduced even on cycles where new
        # placements are already blocked for other reasons.
        self._guardrail_hard_enforce_notional(
            total_capital=guard["total_capital"],
            total_live_notional=guard["total_live_notional"],
        )
        self._guardrail_hard_enforce_clusters(
            total_capital=guard["total_capital"],
            cluster_notional=guard["cluster_notional"],
            cluster_by_cid=guard["cluster_by_cid"],
        )

        # If fill storm active, skip all new placements
        if time.time() < self._fill_storm_until:
            remaining = self._fill_storm_until - time.time()
            if self.cycle_count % 10 == 0:  # log every ~5min
                log.warning(f"Fill storm halt active — {remaining:.0f}s remaining, skipping placements")
        elif guard["notional_block"]:
            if self.cycle_count % 10 == 0:
                log.warning(
                    f"[GUARDRAIL] notional_ratio > {MAX_NOTIONAL_RATIO()} — "
                    f"blocking ALL new placements this cycle"
                )
        elif action == "pause":
            # Oversight pause — skip placements only. Existing cycle
            # work (fills, dumps, hard enforcement, telemetry) already
            # ran above; the natural-end emit at the bottom of
            # run_cycle covers [CYCLE_SUMMARY] for this cycle.
            log.warning("[OVERSIGHT] placements skipped: %s", reason)
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
                # _gated_place_orders_for_market logs [DRY_RUN|SHADOW]
                # intent and skips real execution in non-LIVE modes;
                # counter lives inside the wrapper.
                self._gated_place_orders_for_market(ms)
            if skipped_cluster and self.cycle_count % 10 == 0:
                log.warning(
                    f"[GUARDRAIL] skipped {skipped_cluster} placement(s) in "
                    f"{len(blocked_clusters)} over-exposed cluster(s) "
                    f"(> {CLUSTER_NOTIONAL_LIMIT_FRAC:.0%}·T)"
                )

        # Step 4b: Remove dead markets (3+ consecutive book failures).
        # FX-006: also cascade to dump_states so a saved dump for this cid
        # doesn't survive into the next restart's restore.
        # FX-032: no longer mark these cids as unliquidatable. `book_failures`
        # is incremented whenever `get_merged_book` returns None — which
        # happens for a much wider class of conditions than the canonical
        # "orderbook does not exist" body (SDK parse errors, transient
        # network hiccups, empty bids/asks in a brief market lull). Marking
        # those as permanently unliquidatable was overreach: on Helsinki's
        # v5.1.14 startup, 60 healthy markets (incl. one paying $200/day in
        # rewards) got flagged at 03:23:38 and the FX-028 re-probe couldn't
        # un-mark them within the working window. The canonical FX-007 path
        # in `OrderLifecycle` and `DumpManager` remains the ONLY source of
        # truth for unliquidatable marking — it only fires when the V2 SDK
        # returns a 400 with both "orderbook" AND "does not exist" in the
        # body, which is the actual resolved-market signal. Markets removed
        # from `self.markets` here will reappear on the next
        # `_refresh_reward_markets` call and get another chance — appropriate
        # for transient failure modes, conservative for genuine dead markets
        # (which the FX-007 path will catch on the next placement attempt).
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
                    self._gated_cancel_order(oid, reason="dead_market")
                    self.db.delete_active_order(oid)
                # FX-006: cascade to dump_states so a saved dump for this
                # cid doesn't survive into the next restart's restore.
                self.db.delete_dump_state(cid, side)
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
                from py_clob_client_v2.clob_types import OrdersScoringParams
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

        # Natural end of run_cycle → emit cycle telemetry + (every 10th
        # cycle) rolling stats. Every early-return path above emits too.
        self._emit_cycle_telemetry()

    # ── Main Loop ───────────────────────────────────────────────────

    def run(self, duration_secs: int = 0):
        """Main loop."""
        def _sig(signum, frame):
            # FX-015: handle SIGINT (Ctrl+C) and SIGTERM (systemctl stop)
            # identically. Both flip _shutdown so the main loop exits at the
            # next cycle boundary, then _shutdown_cleanup cancels any live
            # orders before the process returns. Logging the name aids
            # operator debugging from journalctl ("which signal killed me?").
            name = (
                "SIGINT" if signum == signal.SIGINT
                else "SIGTERM" if signum == signal.SIGTERM
                else f"signal {signum}"
            )
            self._shutdown = True
            log.info(f"[SHUTDOWN] {name} received — exiting at next cycle boundary")
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)

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

            # FX-013: write usdc_balance on cycle 1 (in addition to every 10
            # cycles below) so the oversight agent has a fresh balance row
            # from the very first oversight cycle. Without this, the agent
            # falls through to the legacy `--capital` value (which itself is
            # now None by default per FX-025) for up to ~5 min on a fresh
            # LIVE cutover, calibrating safety thresholds against a value
            # that may not reflect the wallet.
            if self.cycle_count == 1 and not self.dry_run:
                self._save_usdc_balance()

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

            # FX-028: re-probe unliquidatable markets every ~6h. Per-cid
            # staleness check is enforced inside the DB query, so this loop
            # is a no-op when nothing has aged out.
            if not self.dry_run and time.time() - self._last_unliquidatable_reprobe >= 1800:
                self._reprobe_unliquidatable()
                self._last_unliquidatable_reprobe = time.time()

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
        """Cancel ALL orders, save state.

        FX-015 + Phase 5 audit: uses the V2 SDK's ``cancel_orders`` batch
        endpoint — a single API call cancels every tracked order, fitting
        comfortably under ``TimeoutStopSec=30`` even at the worst-case
        60-markets × 4 sides = 240 orders. The per-order ``cancel_order``
        loop is preserved as a fallback for when the batch call itself
        raises (rate-limit, network, malformed payload).

        ``_kill_switch_active = True`` is set first so any fallback
        per-order cancels bypass OL's ``dry_run`` short-circuit via the
        ``force=True`` propagation in ``_gated_cancel_order``. Direct
        ``self.client.cancel_orders`` calls bypass OL entirely so the
        flag isn't strictly needed for the batch path, but setting it
        keeps the semantics consistent if the fallback fires.
        """
        # Enumerate pending orders first so the [SHUTDOWN] log line has
        # accurate counts before we start cancelling. Skip dry-run
        # placeholders ("dry_yes" / "dry_no" written by OL's internal
        # dry_run branches) — they have no Polymarket counterpart and
        # the batch endpoint would reject the request.
        live_buys: list[str] = []
        live_dumps: list[str] = []
        for ms in self.markets.values():
            for side in ("yes", "no"):
                oid = ms.orders[side].order_id
                if oid and oid not in ("dry_yes", "dry_no"):
                    live_buys.append(oid)
                dump_oid = ms.dump_orders[side]
                if dump_oid:
                    live_dumps.append(dump_oid)

        log.info(
            f"[SHUTDOWN] cleanup beginning: {len(live_buys)} buy orders + "
            f"{len(live_dumps)} dump orders across {len(self.markets)} markets"
        )

        # Force-execute flag for the per-order fallback path. Idempotent
        # (the kill-switch may have already armed it earlier this run).
        self._kill_switch_active = True

        all_oids = live_buys + live_dumps
        cancelled = 0
        failed = 0

        if all_oids:
            # Batch path — one API call to cancel everything. Latency cliff
            # mitigation: at the worst-case 240 orders this would take
            # ~24s at 100ms/cancel via per-order loop; the batch returns
            # in well under a second.
            try:
                self.client.cancel_orders(all_oids)
                cancelled = len(all_oids)
                log.info(
                    f"[SHUTDOWN] batch cancel succeeded: {cancelled} orders "
                    f"in 1 API call"
                )
            except Exception as e:
                log.warning(
                    f"[SHUTDOWN] batch cancel failed ({e}) — falling back "
                    f"to per-order cancels"
                )
                # Per-order fallback: each cancel goes through the gated
                # wrapper, which propagates force=True so OL's dry_run
                # shortcut is bypassed.
                for oid in live_buys:
                    if self._gated_cancel_order(oid, reason="shutdown"):
                        cancelled += 1
                    else:
                        failed += 1
                for oid in live_dumps:
                    if self._gated_cancel_order(oid, reason="shutdown_dump"):
                        cancelled += 1
                    else:
                        failed += 1

        try:
            self.rewards._save()
        except Exception as e:
            log.warning(f"[SHUTDOWN] reward state save failed: {e}")

        total = len(all_oids)
        log.info(
            f"[SHUTDOWN] cleanup complete: cancelled {cancelled}/{total} orders "
            f"({failed} failed)"
        )


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
    # Execution mode: staged deployment DRY_RUN → SHADOW → LIVE. Default
    # is `dry` so running with no flags never sends real orders.
    parser.add_argument(
        "--mode", choices=("dry", "shadow", "live"), default="dry",
        help="dry: no API calls (default); shadow: reads only, no writes; "
             "live: full execution",
    )
    parser.add_argument("--duration", default="0", help="Run duration (e.g. 10m, 1h, 6h). 0 = indefinite")
    args = parser.parse_args()

    duration = parse_duration(args.duration) if args.duration != "0" else 0

    mode_map = {"dry": MODE_DRY_RUN, "shadow": MODE_SHADOW, "live": MODE_LIVE}
    mode = mode_map[args.mode]

    log.info("Reward Farmer starting")
    log.info(f"  Mode: {mode}")
    log.info(f"  Duration: {'indefinite' if duration == 0 else f'{duration}s'}")
    log.info(f"  Strategy: {SHARES_PER_SIDE()}sh/side, {PLACEMENT_TICKS_INSIDE()} tick inside edge")
    log.info(f"  Markets: max {MAX_MARKETS()}, rate >= ${MIN_DAILY_RATE()}/d, liq < ${MAX_LIQUIDITY()}")

    bot = RewardFarmer(mode=mode)
    bot.run(duration_secs=duration)


if __name__ == "__main__":
    main()
