#!/usr/bin/env python3
"""Reward Farmer — production bot for Polymarket LP reward farming.

Targets low-competition markets with high reward-to-liquidity ratios.
Places orders at the edge of the reward window on both sides.
Dumps immediately on fill. Merges when both sides fill.

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
import sys
import time
import threading
from dataclasses import dataclass, field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("reward_farmer")

# ═══════════════════════════════════════════════════════════════════════
# STRATEGY PARAMETERS (from paper testing)
# ═══════════════════════════════════════════════════════════════════════

from config import cfg

# Shorthand accessors for frequently-used parameters (hot-reloadable via cfg())
def SHARES_PER_SIDE(): return cfg("RF_SHARES_PER_SIDE")
def PLACEMENT_TICKS_INSIDE(): return cfg("RF_PLACEMENT_TICKS_INSIDE")
def MIN_DAILY_RATE(): return cfg("RF_MIN_DAILY_RATE")
def MAX_LIQUIDITY(): return cfg("RF_MAX_LIQUIDITY")
def MAX_COST_PER_MARKET(): return cfg("RF_MAX_COST_PER_MARKET")
def MAX_MARKETS(): return cfg("RF_MAX_MARKETS")
def MAX_TOTAL_EXPOSURE(): return cfg("RF_MAX_TOTAL_EXPOSURE")
def CYCLE_SECS(): return cfg("RF_CYCLE_SECS")
def BATCH_SIZE(): return cfg("RF_BATCH_SIZE")
def MARKET_REFRESH_SECS(): return cfg("RF_MARKET_REFRESH_SECS")


# ═══════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════

from models import OrderSlot, MarketState
from market_discovery import fetch_all_reward_markets, get_merged_book



# ═══════════════════════════════════════════════════════════════════════
# REWARD FARMER BOT
# ═══════════════════════════════════════════════════════════════════════

class RewardFarmer:
    """Production reward farming bot."""

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

        # Position tracker (SQLite-backed, crash-safe)
        from state import PositionStore
        self.positions = PositionStore()
        log.info(f"Loaded positions from SQLite")

        # Reward tracker
        from reward_tracker import RewardTracker
        self.rewards = RewardTracker()

        # Database
        from database import get_db
        self.db = get_db()

        # Startup reconciliation: cancel orphaned orders from previous run
        self._reconcile_on_startup()

        # Market state
        self.markets: dict[str, MarketState] = {}  # cid → MarketState
        self.all_market_data: list[dict] = []       # raw market dicts from fetcher
        self._market_lock = threading.Lock()        # protects all_market_data + _pending_market_data

        # Cycle state
        self.cycle_count = 0
        self._batch_idx = 0
        self._shutdown = False
        self._capital_exhausted = False  # Set True when exchange rejects for insufficient funds
        self._agent_mode = False  # Set True when first valid agent allocation loads
        self._pending_market_data: list[dict] | None = None
        self._last_market_refresh = 0.0
        self._last_reconcile = 0.0
        self._last_reward_log = 0.0

    # ── Startup Reconciliation ─────────────────────────────────────

    def _reconcile_on_startup(self):
        """Cancel all existing orders and verify positions on startup.

        After a crash, stale BUY orders remain on exchange and can cause
        duplicate fills. We cancel everything — the bot re-places its own
        orders in the first cycle. This is safe: reward farming orders are
        tiny (50 shares) and easily re-created.
        """
        if self.dry_run:
            log.info("[DRY] Skipping startup reconciliation")
            return

        # Step 1: Cancel all existing orders (start clean)
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

        # Step 2: Position verification happens later in _reconcile_positions()
        # after market data is loaded (we need token IDs to check balances)

    def _reconcile_positions(self):
        """Verify tracked positions against actual exchange balances.

        Called after market data is loaded (need token IDs).
        """
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
                        diff = abs(actual - tracked_shares)
                        if diff > 1.0:
                            log.warning(
                                f"Position mismatch {side.upper()} {ms.question[:25]}: "
                                f"tracked={tracked_shares:.0f} actual={actual:.0f}"
                            )
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

    # ── Market Management ────────────────────────────────────────────

    def refresh_markets(self):
        """Discover and filter reward markets (blocking, for startup only)."""
        log.info("Refreshing reward markets...")
        self.all_market_data = fetch_all_reward_markets()
        self._apply_market_changes()

    def _load_allocations(self) -> list[dict] | None:
        """Load oversight agent allocations if available and fresh.

        Returns list of "deploy" market dicts, or None if:
        - File doesn't exist (agent not running)
        - File is stale (> 2 hours old)
        - File is corrupt
        """
        alloc_path = os.path.join(
            os.path.dirname(__file__) or ".", "market_allocations.json"
        )
        try:
            if not os.path.exists(alloc_path):
                return None

            with open(alloc_path) as f:
                data = json.load(f)

            # Check freshness
            generated_at = data.get("generated_at", "")
            if generated_at:
                from datetime import datetime, timezone, timedelta
                gen_dt = datetime.fromisoformat(generated_at)
                age = datetime.now(timezone.utc) - gen_dt
                if age > timedelta(hours=cfg("RF_ALLOCATION_TTL_HOURS")):
                    log.debug("Allocation file stale (>2h) — using default logic")
                    return None

            # Extract deploy markets
            deploy = [m for m in data.get("markets", []) if m.get("action") == "deploy"]
            if not deploy:
                return None

            log.info(f"Loaded {len(deploy)} market allocations from oversight agent")
            return deploy

        except Exception as e:
            log.debug(f"Allocation load failed: {e}")
            return None

    def _apply_market_changes(self):
        """Apply market data to active market set.

        If oversight agent allocations exist and are fresh, use those.
        Otherwise fall back to default filtering logic.
        """
        # Check for oversight agent allocations
        agent_alloc = self._load_allocations()
        if agent_alloc is not None:
            self._agent_mode = True  # Agent is active — never fall back to default
            # Build eligible list from agent's recommendations
            raw_by_cid = {m["condition_id"]: m for m in self.all_market_data}
            eligible = []
            for alloc in agent_alloc:
                cid = alloc["condition_id"]
                m = raw_by_cid.get(cid)
                if m:
                    # Use agent's share recommendation
                    m["_agent_shares"] = alloc.get("shares_per_side", SHARES_PER_SIDE())
                    eligible.append(m)
                else:
                    # Agent discovered a market the bot doesn't have — fetch from CLOB
                    try:
                        import requests
                        mkt_resp = requests.get(
                            f"https://clob.polymarket.com/markets/{cid}", timeout=10
                        )
                        if mkt_resp.status_code == 200:
                            mkt = mkt_resp.json()
                            tokens_data = mkt.get("tokens", [])
                            if len(tokens_data) >= 2:
                                fetched = {
                                    "condition_id": cid,
                                    "question": mkt.get("question", ""),
                                    "token_ids": [tokens_data[0]["token_id"], tokens_data[1]["token_id"]],
                                    "yes_price": float(tokens_data[0].get("price", 0.5)),
                                    "daily_rate": alloc.get("score", 0),
                                    "min_size": alloc.get("min_size", 50),
                                    "max_spread": alloc.get("max_spread", 0.045),
                                    "tick_size": float(mkt.get("minimum_tick_size") or 0.01),
                                    "liquidity": 0,
                                    "volume_24h": 0,
                                    "end_date_iso": mkt.get("end_date_iso", ""),
                                    "_agent_shares": alloc.get("shares_per_side", SHARES_PER_SIDE()),
                                }
                                eligible.append(fetched)
                                log.info(f"Agent market fetched from CLOB: {fetched['question'][:40]}")
                            else:
                                log.debug(f"Agent market {cid[:16]}: <2 tokens in CLOB")
                        else:
                            log.debug(f"Agent market {cid[:16]}: CLOB returned {mkt_resp.status_code}")
                    except Exception as e:
                        log.debug(f"Agent market {cid[:16]} CLOB fetch failed: {e}")

            # Minimal validation — agent markets still need basic sanity checks
            validated = []
            for m in eligible:
                tokens = m.get("token_ids", [])
                if len(tokens) < 2:
                    continue
                yes_p = m.get("yes_price") or 0.5
                if yes_p < 0.02 or yes_p > 0.98:
                    log.debug(f"Agent market skipped (extreme price {yes_p}): {m.get('question', '')[:30]}")
                    continue
                validated.append(m)

            log.info(f"Using agent allocations: {len(validated)}/{len(eligible)} markets (after validation)")
            self._update_market_states(validated)
            return

        # B2 fix: If agent was previously active but allocation is now stale/missing,
        # keep current markets. Don't revert to default filtering.
        if self._agent_mode:
            log.warning(
                "Agent allocation stale/missing but agent was active — "
                f"keeping current {len(self.markets)} markets (not reverting to default)"
            )
            return

        raw = self.all_market_data

        # Default filtering (only when agent has NEVER been active)
        eligible = []
        for m in raw:
            if m["daily_rate"] < MIN_DAILY_RATE():
                continue
            if MAX_LIQUIDITY() > 0 and m.get("liquidity", 0) > MAX_LIQUIDITY():
                continue
            # No per-market cost cap — let the exchange reject if insufficient funds
            tokens = m.get("token_ids", [])
            if len(tokens) < 2:
                continue
            eligible.append(m)

        # Sort by reward efficiency (rate / liq), take top MAX_MARKETS
        eligible.sort(
            key=lambda x: x["daily_rate"] / max(x.get("liquidity", 1), 1),
            reverse=True,
        )
        eligible = eligible[:MAX_MARKETS()]

        self._update_market_states(eligible)

    def _update_market_states(self, eligible: list[dict]):
        """Update active market set from eligible list. Shared by default + agent paths."""
        new_cids = {m["condition_id"] for m in eligible}
        old_cids = set(self.markets.keys())

        # Remove dropped markets
        for cid in old_cids - new_cids:
            ms = self.markets[cid]
            log.info(f"Dropping market: {ms.question[:40]}")
            for side in ["yes", "no"]:
                oid = ms.orders[side].order_id
                if oid:
                    self._cancel_order(oid, reason="market_removed")
                    ms.orders[side].order_id = None
            for side in ["yes", "no"]:
                shares = self.positions.get_shares(cid, side)
                if shares > 1:
                    self._dump_position(ms, side, shares)
            del self.markets[cid]

        # Add new markets + update sizing for existing ones
        for m in eligible:
            cid = m["condition_id"]
            if cid in self.markets:
                # B3 fix: Update agent sizing for existing markets
                new_shares = float(m.get("_agent_shares", 0))
                if new_shares > 0 and new_shares != self.markets[cid].agent_shares:
                    log.info(
                        f"Resizing {self.markets[cid].question[:30]}: "
                        f"{self.markets[cid].agent_shares:.0f} → {new_shares:.0f}sh"
                    )
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
                log.info(f"  #{i+1} {ms.question[:45]} | ${ms.daily_rate:.0f}/d")

        self._last_market_refresh = time.time()

    # ── Core Cycle ───────────────────────────────────────────────────

    def run_cycle(self):
        """One 30-second cycle. Steps:
        1. Fetch exchange orders (1 API call)
        2. Check dump SELL orders (did they fill?)
        3. Detect BUY order fills + handle
        4. Place orders on batch of markets
        5. Record reward tracking
        """
        self.cycle_count += 1
        self._capital_exhausted = False  # reset each cycle

        # ── Step 1: Fetch all exchange orders ────────────────────────
        if self.dry_run:
            exchange_orders = []
            open_ids = set()
        else:
            try:
                exchange_orders = self.client.get_orders() or []
            except Exception as e:
                log.error(f"get_orders failed: {e}")
                return
            open_ids = {o["id"] for o in exchange_orders}

        # ── Step 2: Check dump SELL orders ───────────────────────────
        for cid, ms in list(self.markets.items()):
            for side in ["yes", "no"]:
                dump_oid = ms.dump_orders[side]
                if not dump_oid:
                    continue

                # Skip exchange API calls for dry-run fake order IDs
                if self.dry_run:
                    ms.dump_orders[side] = None
                    ms.dump_state[side] = None
                    continue

                if dump_oid not in open_ids:
                    # Dump order gone from exchange — check if it filled
                    try:
                        status = self.client.get_order(dump_oid)
                        dump_status = status.get("status", "UNKNOWN")
                    except Exception as e:
                        log.debug(f"Dump order status check failed {dump_oid[:16]}: {e}")
                        dump_status = "UNKNOWN"

                    if dump_status == "MATCHED":
                        # Get REAL fill price from exchange (not the stale book price)
                        actual_price = float(status.get("price", 0))
                        actual_matched = float(status.get("size_matched", 0))
                        sell_revenue = actual_matched * actual_price if actual_price > 0 else 0

                        from price import to_clob
                        avg_p = self.positions.get_avg_price(ms.cid, side)
                        vwap_cost = actual_matched * to_clob(avg_p, side) if avg_p > 0 else 0

                        log.info(
                            f"DUMP CONFIRMED {side.upper()} {actual_matched:.0f}sh @ {actual_price:.4f} | "
                            f"rev=${sell_revenue:.2f} cost=${vwap_cost:.2f} pnl=${sell_revenue - vwap_cost:+.2f} | "
                            f"{ms.question[:30]}"
                        )

                        # NOW record with real numbers
                        self.positions.record_unwind(ms.cid, side, actual_matched)
                        self.db.log_unwind(
                            condition_id=ms.cid, question=ms.question,
                            side=side, shares=actual_matched,
                            sell_price=actual_price, usd_value=sell_revenue,
                            vwap_cost=vwap_cost,
                        )
                        from alerts import alert_unwind
                        alert_unwind(
                            side=side.upper(), price=actual_price,
                            size=actual_matched, usd_value=sell_revenue,
                            market_question=ms.question,
                        )

                        ms.dump_orders[side] = None
                        ms.dump_state[side] = None  # clear decay state
                        ms.dump_failures = 0
                        self.db.delete_dump_state(ms.cid, side)
                    elif dump_status == "UNKNOWN":
                        ms.unknown_count[side] = ms.unknown_count.get(side, 0) + 1
                        if ms.unknown_count[side] >= cfg("RF_UNKNOWN_RETRY_THRESHOLD"):
                            log.warning(f"Dump order stuck UNKNOWN 5×, clearing | {ms.question[:30]}")
                            ms.dump_orders[side] = None
                    else:
                        # CANCELLED or other — dump failed, will retry
                        log.warning(f"Dump order {dump_status} — will retry | {ms.question[:30]}")
                        ms.dump_orders[side] = None

        # ── Step 2.5: Reprice active dumps (decay schedule) ──────────
        for cid, ms in list(self.markets.items()):
            for side in ["yes", "no"]:
                if not ms.dump_state[side]:
                    continue
                dump_oid = ms.dump_orders[side]
                # If dump order is still LIVE on exchange, check if it needs repricing
                if dump_oid and dump_oid in open_ids:
                    elapsed_min = (time.time() - ms.dump_state[side]["started_at"]) / 60.0
                    last_reprice_min = ms.dump_state[side].get("last_reprice_min", 0)
                    # Reprice every minute
                    if int(elapsed_min) > int(last_reprice_min):
                        ms.dump_state[side]["last_reprice_min"] = elapsed_min
                        shares = ms.dump_state[side]["shares"]
                        self._dump_position(ms, side, shares)
                # If dump order gone but not confirmed in Step 2, it was cancelled → retry
                elif not dump_oid and ms.dump_state[side]:
                    shares = ms.dump_state[side]["shares"]
                    self._dump_position(ms, side, shares)

        # ── Step 3: Detect BUY order fills ───────────────────────────
        for cid, ms in list(self.markets.items()):
            for side in ["yes", "no"]:
                slot = ms.orders[side]
                if not slot.order_id:
                    continue

                # Skip exchange API calls for dry-run fake order IDs
                if self.dry_run:
                    slot.order_id = None
                    continue

                if slot.order_id not in open_ids:
                    # Order gone from exchange — check if filled
                    try:
                        status = self.client.get_order(slot.order_id)
                        order_status = status.get("status", "UNKNOWN")
                        matched = float(status.get("size_matched", 0))
                    except Exception as e:
                        log.debug(f"BUY order status check failed {slot.order_id[:16]}: {e}")
                        order_status = "UNKNOWN"
                        matched = 0

                    if matched > 0 and order_status in ("MATCHED", "CANCELLED"):
                        # Full or partial fill — record whatever matched
                        fill_type = "FULL" if matched >= slot.shares - 0.5 else "PARTIAL"
                        actual_price = float(status.get("price", slot.price))
                        if fill_type == "PARTIAL":
                            log.info(
                                f"PARTIAL fill {side.upper()} {matched:.0f}/{slot.shares:.0f}sh "
                                f"(order {order_status}) | {ms.question[:30]}"
                            )
                        self._handle_fill(ms, side, slot, actual_shares=matched, actual_price=actual_price)
                        ms.unknown_count[side] = 0
                    elif order_status == "UNKNOWN":
                        ms.unknown_count[side] = ms.unknown_count.get(side, 0) + 1
                        if ms.unknown_count[side] >= cfg("RF_UNKNOWN_RETRY_THRESHOLD"):
                            log.warning(f"BUY order stuck UNKNOWN 5×, clearing | {ms.question[:30]}")
                            slot.order_id = None
                            ms.unknown_count[side] = 0
                        else:
                            log.warning(f"Order {slot.order_id[:16]} UNKNOWN ({ms.unknown_count[side]}/5)")
                        continue  # don't clear yet, retry next cycle
                    else:
                        ms.unknown_count[side] = 0
                    # Clear the order slot (MATCHED, CANCELLED, etc.)
                    if slot.order_id:
                        self.db.delete_active_order(slot.order_id)
                    slot.order_id = None

        # ── Step 4: Place orders on batch (priority: empty slots first) ──
        market_list = list(self.markets.values())
        if not market_list:
            return

        batch = self._get_priority_batch(market_list)

        for ms in batch:
            self._place_orders_for_market(ms)

        # ── Step 5: Record rewards for all on-book markets ───────────
        for ms in market_list:
            has_yes = ms.orders["yes"].order_id is not None
            has_no = ms.orders["no"].order_id is not None
            if not has_yes and not has_no:
                continue

            self.rewards.get_or_create(
                condition_id=ms.cid,
                question=ms.question,
                daily_rate=ms.daily_rate,
                max_spread=ms.max_spread,
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
                inventory_usd=0.0,
                cooldown_active=False, skew_active=False,
                cycle_duration_secs=CYCLE_SECS(),
                midpoint=mid,
                bid_size=ms.orders["yes"].shares if has_yes else 0,
                ask_size=ms.orders["no"].shares if has_no else 0,
            )

        # Periodic saves
        if self.cycle_count % 5 == 0:
            self.rewards._save()

    # ── Order Placement ──────────────────────────────────────────────

    def _place_orders_for_market(self, ms: MarketState):
        """Fetch book + place edge orders for one market."""
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY

        # Fetch merged book
        merged = get_merged_book(self.client, ms.yes_tid, ms.no_tid)
        if not merged or not merged["bids"] or not merged["asks"]:
            return

        best_bid = float(merged["bids"][0]["price"])
        best_ask = float(merged["asks"][0]["price"])
        midpoint = (best_bid + best_ask) / 2
        ms.midpoint = midpoint
        ms.last_book_fetch = time.time()

        if best_ask - best_bid > cfg("RF_MAX_BOOK_SPREAD"):
            self.db.write_placement_feedback(ms.cid, "yes", "skipped", "wide_spread")
            self.db.write_placement_feedback(ms.cid, "no", "skipped", "wide_spread")
            return

        # Edge prices
        tick = ms.tick_size
        decimals = max(2, len(str(tick).rstrip('0').split('.')[-1]))
        edge_bid = round(midpoint - ms.max_spread + tick * PLACEMENT_TICKS_INSIDE(), decimals)
        edge_ask = round(midpoint + ms.max_spread - tick * PLACEMENT_TICKS_INSIDE(), decimals)
        edge_bid = max(0.01, edge_bid)
        edge_ask = min(0.99, edge_ask)

        # ── Reprice stale orders outside reward window ───────────────
        # If midpoint moved, our orders may be outside the reward window
        # (earning zero Q-score but still taking fill risk). Cancel and re-place.
        for side, edge_price in [("yes", edge_bid), ("no", edge_ask)]:
            slot = ms.orders[side]
            if not slot.order_id:
                continue
            order_dist = abs(slot.price - midpoint)
            if order_dist >= ms.max_spread:
                # Order is outside reward window — cancel it
                self._cancel_order(slot.order_id, reason="outside_reward_window")
                log.info(
                    f"REPRICE {side.upper()} | old={slot.price:.3f} dist={order_dist:.3f} >= spread={ms.max_spread:.3f} | "
                    f"new={edge_price:.3f} | {ms.question[:30]}"
                )
                slot.order_id = None  # will be re-placed below

        # ── Exit liquidity check: only place if we can dump within buffer ──
        exit_buf = cfg("RF_DUMP_EXIT_DEPTH_BUFFER")
        yes_exit_depth = sum(
            float(b["size"]) for b in merged["bids"]
            if float(b["price"]) >= edge_bid - exit_buf
        )
        no_exit_depth = sum(
            float(a["size"]) for a in merged["asks"]
            if float(a["price"]) <= edge_ask + exit_buf
        )
        effective_shares = ms.agent_shares if ms.agent_shares > 0 else SHARES_PER_SIDE()
        can_exit_yes = yes_exit_depth >= effective_shares
        can_exit_no = no_exit_depth >= effective_shares

        if not can_exit_yes:
            self.db.write_placement_feedback(ms.cid, "yes", "skipped", "exit_liquidity")
        if not can_exit_no:
            self.db.write_placement_feedback(ms.cid, "no", "skipped", "exit_liquidity")

        # Shares — use agent's per-market sizing if available, else default
        shares_target = ms.agent_shares if ms.agent_shares > 0 else SHARES_PER_SIDE()
        yes_shares = max(ms.min_size, shares_target)
        no_clob = round(1.0 - edge_ask, decimals)
        no_clob = max(0.01, no_clob)
        no_shares = max(ms.min_size, shares_target)

        # Place YES bid (only if exit liquidity exists)
        if can_exit_yes:
            can, reason = self._can_place(ms.cid, "yes", yes_shares * edge_bid)
            if can:
                if self.dry_run:
                    log.info(f"[DRY] BID YES @ {edge_bid:.3f} ({yes_shares:.0f}sh) | {ms.question[:30]}")
                    ms.orders["yes"] = OrderSlot(order_id="dry_yes", price=edge_bid, shares=yes_shares, placed_at=time.time())
                    self.db.write_placement_feedback(ms.cid, "yes", "placed", "")
                else:
                    try:
                        args = OrderArgs(token_id=ms.yes_tid, price=edge_bid, size=float(yes_shares), side=BUY)
                        resp = self.client.create_and_post_order(args)
                        oid = resp.get("orderID") if isinstance(resp, dict) else None
                        if oid:
                            ms.orders["yes"] = OrderSlot(order_id=oid, price=edge_bid, shares=yes_shares, placed_at=time.time())
                            self.db.log_order_placed(condition_id=ms.cid, side="yes", price=edge_bid, size=float(yes_shares), order_id=oid)
                            self.db.save_active_order(oid, ms.cid, "yes", "buy", edge_bid, yes_shares)
                            self.db.write_placement_feedback(ms.cid, "yes", "placed", "")
                            if self.cycle_count <= 3:
                                log.info(f"BID YES @ {edge_bid:.3f} ({yes_shares:.0f}sh) | {ms.question[:30]}")
                        else:
                            # B8: Exchange returned response but no orderID
                            self.db.write_placement_feedback(ms.cid, "yes", "failed", "no_order_id")
                            log.warning(f"YES order got no orderID | {ms.question[:25]}")
                    except Exception as e:
                        err_str = str(e).lower()
                        if "insufficient" in err_str or "balance" in err_str or "not enough" in err_str:
                            log.warning(f"Capital exhausted (YES) — stopping placement this cycle | {ms.question[:30]}")
                            self._capital_exhausted = True
                            self.db.write_placement_feedback(ms.cid, "yes", "failed", "capital_exhausted")
                            return
                        self.db.write_placement_feedback(ms.cid, "yes", "failed", "order_error")
                        log.debug(f"YES order failed {ms.question[:25]}: {e}")
            else:
                # B1 fix: Don't overwrite "placed" with transient per-cycle guards
                if reason not in ("already_has_order", "dump_pending"):
                    self.db.write_placement_feedback(ms.cid, "yes", "skipped", reason)

        # Place NO ask (only if exit liquidity exists)
        if can_exit_no:
            can, reason = self._can_place(ms.cid, "no", no_shares * no_clob)
            if can:
                if self.dry_run:
                    log.info(f"[DRY] ASK NO @ {edge_ask:.3f} (clob={no_clob:.3f}, {no_shares:.0f}sh) | {ms.question[:30]}")
                    ms.orders["no"] = OrderSlot(order_id="dry_no", price=edge_ask, shares=no_shares, placed_at=time.time())
                    self.db.write_placement_feedback(ms.cid, "no", "placed", "")
                else:
                    try:
                        args = OrderArgs(token_id=ms.no_tid, price=no_clob, size=float(no_shares), side=BUY)
                        resp = self.client.create_and_post_order(args)
                        oid = resp.get("orderID") if isinstance(resp, dict) else None
                        if oid:
                            ms.orders["no"] = OrderSlot(order_id=oid, price=edge_ask, shares=no_shares, placed_at=time.time())
                            self.db.log_order_placed(condition_id=ms.cid, side="no", price=edge_ask, size=float(no_shares), order_id=oid)
                            self.db.save_active_order(oid, ms.cid, "no", "buy", edge_ask, no_shares)
                            self.db.write_placement_feedback(ms.cid, "no", "placed", "")
                            if self.cycle_count <= 3:
                                log.info(f"ASK NO @ {edge_ask:.3f} (clob={no_clob:.3f}, {no_shares:.0f}sh) | {ms.question[:30]}")
                        else:
                            self.db.write_placement_feedback(ms.cid, "no", "failed", "no_order_id")
                            log.warning(f"NO order got no orderID | {ms.question[:25]}")
                    except Exception as e:
                        err_str = str(e).lower()
                        if "insufficient" in err_str or "balance" in err_str or "not enough" in err_str:
                            log.warning(f"Capital exhausted (NO) — stopping placement this cycle | {ms.question[:30]}")
                            self._capital_exhausted = True
                            self.db.write_placement_feedback(ms.cid, "no", "failed", "capital_exhausted")
                            return
                        self.db.write_placement_feedback(ms.cid, "no", "failed", "order_error")
                        log.debug(f"NO order failed {ms.question[:25]}: {e}")
            else:
                # B1 fix: Don't overwrite "placed" with transient per-cycle guards
                if reason not in ("already_has_order", "dump_pending"):
                    self.db.write_placement_feedback(ms.cid, "no", "skipped", reason)

    def _get_priority_batch(self, market_list: list) -> list:
        """Priority-based batch: empty slots first (highest daily_rate), then rotation."""
        needs_orders = []
        has_orders = []

        for ms in market_list:
            empty_slots = sum(
                1 for s in ["yes", "no"]
                if not ms.orders[s].order_id and not ms.dump_orders[s]
            )
            if empty_slots > 0:
                needs_orders.append(ms)
            else:
                has_orders.append(ms)

        # Highest-value empty slots first
        needs_orders.sort(key=lambda x: x.daily_rate, reverse=True)
        batch = needs_orders[:BATCH_SIZE()]

        # Fill remaining slots with rotation for reprice checks
        if len(batch) < BATCH_SIZE() and has_orders:
            remaining = BATCH_SIZE() - len(batch)
            start = self._batch_idx % max(len(has_orders), 1)
            for i in range(remaining):
                batch.append(has_orders[(start + i) % len(has_orders)])
            self._batch_idx = (start + remaining) % max(len(has_orders), 1)

        return batch

    def _can_place(self, cid: str, side: str, est_cost: float) -> tuple[bool, str]:
        """All guards before placing an order. Returns (can_place, reason).

        Capital gating is NOT done here. Polymarket allows limit orders
        even when they exceed available balance. We let the exchange reject
        if funds are insufficient (caught in _place_orders_for_market).
        """
        ms = self.markets.get(cid)
        if not ms:
            return False, "no_market"
        if self._capital_exhausted:
            return False, "capital_exhausted"
        if ms.orders[side].order_id:
            return False, "already_has_order"
        if ms.dump_orders[side]:
            return False, "dump_pending"
        if self.positions.get_shares(cid, side) > 1:
            return False, "inventory"
        if not self.positions.can_quote(cid, side):
            return False, "halted"
        if ms.dump_failures >= cfg("RF_DUMP_MAX_FAILURES"):
            return False, "dump_failures"
        return True, ""

    def _total_exposure(self) -> float:
        """Sum of all open position USD values (actual, not estimated)."""
        total = 0.0
        for cid in self.markets:
            for side in ["yes", "no"]:
                total += self.positions.get_position(cid, side)
        return total

    # ── Fill Handling ────────────────────────────────────────────────

    def _handle_fill(self, ms: MarketState, side: str, slot: OrderSlot,
                     actual_shares: float = 0, actual_price: float = 0.0):
        """Process a detected fill: record, then merge or dump.

        Args:
            actual_shares: Exchange-reported filled qty (from size_matched)
            actual_price: Exchange-reported fill price (from get_order response)
        """
        from alerts import alert_fill

        filled_shares = actual_shares if actual_shares > 0 else slot.shares
        fill_price = actual_price if actual_price > 0 else slot.price
        cid = ms.cid

        log.info(
            f"FILL {side.upper()} {filled_shares:.0f}sh @ {fill_price:.4f} | "
            f"{ms.question[:35]}"
        )

        # Record to position tracker
        self.positions.record_fill(cid, side, filled_shares, fill_price, question=ms.question)

        # Record to DB
        from price import to_clob
        clob_cost = to_clob(fill_price, side)
        self.db.log_fill(
            condition_id=cid, question=ms.question,
            side=side, fill_type="FULL",
            shares=filled_shares, price=fill_price,
            clob_cost=clob_cost, usd_value=filled_shares * clob_cost,
        )

        # Alert
        alert_fill(
            fill_type="FULL", side=side.upper(),
            price=clob_cost, filled_shares=filled_shares,
            filled_usd=filled_shares * clob_cost,
            market_question=ms.question,
        )

        # Capture fill price BEFORE slot might be cleared
        # _dump_position needs this to compute decay start price
        ms.last_fill_price[side] = fill_price  # YES-equivalent price

        # Try merge first
        yes_shares = self.positions.get_shares(cid, "yes")
        no_shares = self.positions.get_shares(cid, "no")
        merge_qty = min(yes_shares, no_shares)
        if merge_qty >= 1.0:
            self._try_merge(ms, merge_qty)
            return

        # Dump single side
        self._dump_position(ms, side, filled_shares)

    def _try_merge(self, ms: MarketState, amount: float):
        """Merge YES + NO positions for $1 each. Falls back to dual dump."""
        if self.dry_run:
            log.info(f"[DRY] MERGE {amount:.0f} pairs | {ms.question[:30]}")
            self.positions.record_unwind(ms.cid, "yes", amount)
            self.positions.record_unwind(ms.cid, "no", amount)
            return

        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            # Ensure allowance for both tokens
            for tid in [ms.yes_tid, ms.no_tid]:
                self.client.update_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
                )
            # Execute merge
            result = self.client.merge_positions(ms.cid, amount)
            log.info(f"MERGE {amount:.0f} pairs | {ms.question[:30]}")
            self.positions.record_unwind(ms.cid, "yes", amount)
            self.positions.record_unwind(ms.cid, "no", amount)
            self.db.log_unwind(
                condition_id=ms.cid, question=ms.question,
                side="merge", shares=amount,
                sell_price=1.0, usd_value=amount,
            )
        except Exception as e:
            log.warning(f"Merge failed ({e}) — falling back to dual dump | {ms.question[:30]}")
            for side in ["yes", "no"]:
                shares = self.positions.get_shares(ms.cid, side)
                if shares >= 1:
                    self._dump_position(ms, side, shares)

    def _dump_position(self, ms: MarketState, side: str, shares: float):
        """Smart dump: SELL near fill price, decay over 5 minutes.

        T+0:  SELL at fill_price - 1 tick (near breakeven)
        T+1m: lower by 1 tick
        T+2m: lower by 1 tick
        T+3m: lower by 1 tick
        T+4m: lower by 1 tick
        T+5m: market dump at best bid (safety timeout)

        If already have a dump in progress, check if it needs repricing.
        """
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import SELL

        tid = ms.yes_tid if side == "yes" else ms.no_tid
        tick = ms.tick_size

        if self.dry_run:
            log.info(f"[DRY] DUMP {side.upper()} {shares:.0f}sh | {ms.question[:30]}")
            self.positions.record_unwind(ms.cid, side, shares)
            return

        try:
            # Check actual token balance
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            try:
                bal = self.client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
                )
                actual_balance = float(bal.get("balance", 0)) / 1e6
                dump_shares = min(shares, actual_balance)
                if dump_shares < 1.0:
                    log.warning(f"Skip dump {side}: want {shares:.0f} but only {actual_balance:.0f} on exchange | {ms.question[:30]}")
                    ms.dump_failures += 1
                    return
            except Exception as e:
                log.debug(f"Balance check failed for dump {side}: {e}")
                dump_shares = shares

            # Initialize dump state if first attempt
            if ms.dump_state[side] is None:
                from price import to_clob
                # Use captured fill price (set in _handle_fill before slot cleared)
                # Fall back to slot price, then 0
                fill_price_yes_equiv = 0
                if ms.last_fill_price.get(side, 0) > 0:
                    fill_price_yes_equiv = ms.last_fill_price[side]
                elif ms.orders[side].price > 0:
                    fill_price_yes_equiv = ms.orders[side].price

                fill_price_clob = to_clob(fill_price_yes_equiv, side) if fill_price_yes_equiv > 0 else 0
                ms.dump_state[side] = {
                    "fill_price": fill_price_clob,
                    "started_at": time.time(),
                    "shares": dump_shares,
                    "tid": tid,
                }
                self.db.save_dump_state(ms.cid, side, ms.dump_state[side])

            state = ms.dump_state[side]
            elapsed_min = (time.time() - state["started_at"]) / 60.0

            # Compute decay price
            if elapsed_min >= cfg("RF_DUMP_ABANDON_MINS"):
                # Hard timeout. Accept the loss, clear state.
                log.warning(f"DUMP ABANDONED {side.upper()} after {elapsed_min:.0f}m | {ms.question[:30]}")
                ms.dump_state[side] = None
                self.db.delete_dump_state(ms.cid, side)
                if ms.dump_orders[side]:
                    self._cancel_order(ms.dump_orders[side], reason="dump_30m_timeout")
                    ms.dump_orders[side] = None
                return

            elif elapsed_min >= cfg("RF_DUMP_AGGRESSIVE_MINS"):
                # Past aggressive decay — switch to passive mode.
                # Reprice to merged book fair price periodically.
                passive_interval = cfg("RF_DUMP_PASSIVE_REPRICE_MINS")
                last_passive = state.get("last_passive_reprice", cfg("RF_DUMP_AGGRESSIVE_MINS"))
                if elapsed_min - last_passive < passive_interval:
                    return  # keep current SELL alive, don't reprice yet
                state["last_passive_reprice"] = elapsed_min

                merged = get_merged_book(self.client, ms.yes_tid, ms.no_tid)
                if not merged or not merged["bids"] or not merged["asks"]:
                    ms.dump_failures += 1
                    return

                if side == "yes":
                    sell_price = float(merged["bids"][0]["price"])
                else:
                    best_yes_ask = float(merged["asks"][0]["price"])
                    sell_price = round(1.0 - best_yes_ask, 4)

                sell_price = max(0.01, sell_price)
                log.info(f"DUMP PASSIVE {side.upper()} @ {sell_price:.4f} ({elapsed_min:.0f}m) | {ms.question[:30]}")
            else:
                # Decay: fill_price - (1 + elapsed_minutes) ticks
                decay_ticks = 1 + int(elapsed_min)
                sell_price = round(state["fill_price"] - decay_ticks * tick, 4)
                sell_price = max(0.01, sell_price)

            # Cancel existing dump order if any (repricing)
            if ms.dump_orders[side]:
                self._cancel_order(ms.dump_orders[side], reason="dump_reprice")
                ms.dump_orders[side] = None

            args = OrderArgs(token_id=tid, price=sell_price, size=float(dump_shares), side=SELL)
            resp = self.client.create_and_post_order(args)
            oid = resp.get("orderID") if isinstance(resp, dict) else None

            if oid:
                ms.dump_orders[side] = oid
                ms.dump_failures = 0
                # Persist dump order ID for crash recovery
                state["dump_order_id"] = oid
                self.db.save_dump_state(ms.cid, side, state)
                self.db.save_active_order(oid, ms.cid, side, "dump_sell", sell_price, dump_shares)
                if elapsed_min < 0.1:  # only log first placement
                    log.info(
                        f"DUMP POSTED {side.upper()} {dump_shares:.0f}sh @ {sell_price:.4f} "
                        f"(fill was {state['fill_price']:.4f}) | {ms.question[:30]}"
                    )
            else:
                log.warning(f"Dump {side} no order ID | {ms.question[:30]}")
                ms.dump_failures += 1

        except Exception as e:
            log.error(f"Dump {side} FAILED: {e} | {ms.question[:30]}")
            ms.dump_failures += 1

    # ── Utility ──────────────────────────────────────────────────────

    def _cancel_order(self, order_id: str, reason: str = "") -> bool:
        """Cancel an order on the exchange. Returns True on success."""
        if self.dry_run:
            return True
        try:
            self.client.cancel(order_id)
            log.debug(f"Cancelled {order_id[:16]} ({reason})")
            return True
        except Exception as e:
            log.warning(f"Cancel FAILED {order_id[:16]} ({reason}): {e}")
            return False

    def _restore_dump_states(self):
        """Restore dump states from DB after crash/restart.

        Called after refresh_markets() and _reconcile_on_startup(). Resumes
        dump decay from where it left off — does NOT reset started_at.
        """
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
                log.debug(f"Dump expired during downtime: {ms.question[:30]} {side} ({elapsed_min:.0f}m)")
                continue

            # Restore — order was cancelled on startup, will be re-placed next cycle
            ms.dump_state[side] = state
            ms.dump_orders[side] = None  # _dump_position will re-post the SELL
            restored += 1
            log.info(
                f"Restored dump {side.upper()} {state['shares']:.0f}sh @ {state['fill_price']:.4f} "
                f"({elapsed_min:.0f}m elapsed) | {ms.question[:30]}"
            )

        if restored or cleaned:
            log.info(f"Dump state recovery: restored {restored}, cleaned {cleaned}")

    # ── Main Loop ────────────────────────────────────────────────────

    def run(self, duration_secs: int = 0):
        """Main loop. Runs indefinitely or for duration_secs if > 0."""

        def _sig(signum, frame):
            self._shutdown = True
            log.info("Shutdown requested...")
        signal.signal(signal.SIGINT, _sig)

        # Initial market fetch
        self.refresh_markets()
        if not self.markets:
            log.error("No eligible markets found. Exiting.")
            return

        # Verify positions against exchange (needs token IDs from market data)
        self._reconcile_positions()

        # Restore any dump states from DB (resume from where we left off)
        self._restore_dump_states()

        start = time.time()
        last_status = time.time()

        log.info(f"Starting reward farming | {len(self.markets)} markets | dry_run={self.dry_run}")

        while not self._shutdown:
            if duration_secs > 0 and (time.time() - start) >= duration_secs:
                break

            t0 = time.time()

            # Hot-reload config if config_overrides.json changed
            from config import BotConfig
            BotConfig.instance().check_and_reload()

            # Market refresh (in background to avoid blocking trading)
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

            # Apply pending market data from background refresh (thread-safe)
            with self._market_lock:
                if self._pending_market_data is not None:
                    self.all_market_data = self._pending_market_data
                    self._pending_market_data = None
                    apply_now = True
                else:
                    apply_now = False
            if apply_now:
                self._apply_market_changes()

            # Run cycle
            cycle_t0 = time.time()
            try:
                self.run_cycle()
            except Exception as e:
                log.error(f"Cycle error: {e}")
            cycle_duration = time.time() - cycle_t0

            # Log cycle metrics every 10 cycles
            if self.cycle_count % 10 == 0 and self.cycle_count > 0:
                on_book = sum(1 for ms in self.markets.values()
                              if ms.orders["yes"].order_id or ms.orders["no"].order_id)
                active_dumps = sum(1 for ms in self.markets.values()
                                   if ms.dump_orders["yes"] or ms.dump_orders["no"])
                log.info(
                    f"[metrics] cycle={self.cycle_count} | {cycle_duration:.1f}s | "
                    f"on_book={on_book}/{len(self.markets)} | dumps={active_dumps}"
                )

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
                log.info(
                    f"Cycle {self.cycle_count} | {elapsed:.0f}m | "
                    f"{on_book}/{len(self.markets)} on-book | "
                    f"dry_run={self.dry_run}"
                )
                last_status = time.time()

            # Sleep
            elapsed_cycle = time.time() - t0
            sleep_time = max(0, CYCLE_SECS() - elapsed_cycle)
            if sleep_time > 0 and not self._shutdown:
                # Sleep in 1s intervals for responsive shutdown
                for _ in range(int(sleep_time)):
                    if self._shutdown:
                        break
                    time.sleep(1)

        # Shutdown
        self._shutdown_cleanup()

    def _shutdown_cleanup(self):
        """Cancel ALL orders (BUY + dump SELL), save state."""
        log.info("Shutting down...")
        for ms in self.markets.values():
            for side in ["yes", "no"]:
                # Cancel BUY orders
                oid = ms.orders[side].order_id
                if oid and oid != "dry_yes" and oid != "dry_no":
                    self._cancel_order(oid, reason="shutdown")
                # Cancel dump SELL orders
                dump_oid = ms.dump_orders[side]
                if dump_oid:
                    self._cancel_order(dump_oid, reason="shutdown_dump")
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
    log.info(f"  Cost cap: ${MAX_COST_PER_MARKET()}/market, ${MAX_TOTAL_EXPOSURE()} total exposure")

    bot = RewardFarmer(dry_run=args.dry_run)
    bot.run(duration_secs=duration)


if __name__ == "__main__":
    main()
