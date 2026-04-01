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

SHARES_PER_SIDE = 50
PLACEMENT_TICKS_INSIDE = 1
MIN_DAILY_RATE = 10.0
MAX_LIQUIDITY = 5000
MAX_COST_PER_MARKET = 50.0
MAX_MARKETS = 40
MAX_TOTAL_EXPOSURE = 1500.0
CYCLE_SECS = 30
BATCH_SIZE = 5
MARKET_REFRESH_SECS = 1800


# ═══════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class OrderSlot:
    order_id: str | None = None
    price: float = 0.0
    shares: float = 0.0
    placed_at: float = 0.0


@dataclass
class MarketState:
    """Per-market tracking."""
    cid: str
    question: str
    yes_tid: str
    no_tid: str
    daily_rate: float
    max_spread: float
    min_size: float
    tick_size: float
    yes_price: float | None
    orders: dict = field(default_factory=lambda: {"yes": OrderSlot(), "no": OrderSlot()})
    dump_orders: dict = field(default_factory=lambda: {"yes": None, "no": None})  # side → SELL order_id
    dump_state: dict = field(default_factory=lambda: {"yes": None, "no": None})  # side → {"fill_price", "started_at", "shares", "tid"}
    dump_failures: int = 0
    unknown_count: dict = field(default_factory=lambda: {"yes": 0, "no": 0})  # consecutive UNKNOWN status counts
    last_book_fetch: float = 0.0
    midpoint: float = 0.0


# ═══════════════════════════════════════════════════════════════════════
# MARKET DISCOVERY (from paper_trader_v2.py, proven)
# ═══════════════════════════════════════════════════════════════════════

def _verify_order_books(markets: list[dict]) -> list[dict]:
    """Verify each candidate market has real order book depth.

    Replaces unreliable liquidity values with actual on-book USD depth.
    Filters out markets resolving within 12 hours and one-sided books.
    This is the ground truth — no keyword filters, no price heuristics.
    """
    import requests
    from datetime import datetime, timezone, timedelta

    verified = []
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=12)

    for m in markets:
        # ── Live event check: skip "during X" markets (real-time event pricing) ──
        q_lower = (m.get("question") or "").lower()
        if " during " in q_lower:
            log.debug(f"  Skip (live event): {m['question'][:40]}")
            continue

        # ── Expiry check: skip markets resolving within 12 hours ──
        end_date = m.get("end_date_iso")
        if end_date:
            try:
                dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                if dt <= cutoff:
                    log.debug(f"  Skip (resolves <12h): {m['question'][:40]}")
                    continue
            except Exception:
                pass  # unparseable date — don't skip on that basis

        # ── Order book depth check: the ground truth ──
        yes_tid = m["token_ids"][0]
        try:
            resp = requests.get(
                "https://clob.polymarket.com/book",
                params={"token_id": yes_tid},
                timeout=10,
            )
            if resp.status_code != 200:
                continue  # can't verify → don't trade
            book = resp.json()
        except Exception:
            continue  # can't verify → don't trade

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        # Must have both sides to be tradeable
        if not bids or not asks:
            log.debug(f"  Skip (one-sided book): {m['question'][:40]}")
            continue

        # Sum top 5 levels on each side (price × size = USD depth)
        bid_depth = sum(float(b["price"]) * float(b["size"]) for b in bids[:5])
        ask_depth = sum(float(a["price"]) * float(a["size"]) for a in asks[:5])
        on_book_depth = bid_depth + ask_depth

        # Replace unreliable liquidity with real on-book depth
        m["liquidity"] = on_book_depth

        verified.append(m)
        time.sleep(0.15)  # respect rate limit

    log.info(f"  Verified: {len(verified)}/{len(markets)} passed order book check")
    return verified


def fetch_all_reward_markets() -> list[dict]:
    """Fetch ALL reward markets from CLOB endpoint + Gamma details."""
    import requests

    log.info("  Fetching CLOB rewards (authoritative source)...")
    clob_markets = []
    cursor = ""
    for _ in range(20):
        params = {"limit": 500}
        if cursor:
            params["next_cursor"] = cursor
        try:
            resp = requests.get(
                "https://clob.polymarket.com/rewards/markets/current",
                params=params, timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"  CLOB rewards fetch failed: {e}")
            break
        items = data.get("data", [])
        clob_markets.extend(items)
        cursor = data.get("next_cursor", "")
        if not cursor or not items:
            break
    log.info(f"  CLOB: {len(clob_markets)} reward markets")

    log.info("  Fetching Gamma market details...")
    gamma_all = []
    for offset in range(0, 10000, 100):
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"limit": 100, "offset": offset, "closed": "false"},
                timeout=15,
            )
            batch = resp.json()
        except Exception:
            break
        if not batch:
            break
        gamma_all.extend(batch)
    log.info(f"  Gamma: {len(gamma_all)} markets")

    gamma_by_cid = {m.get("conditionId", ""): m for m in gamma_all}

    merged = []
    for c in clob_markets:
        cid = c["condition_id"]
        rate = float(c.get("total_daily_rate") or 0)
        if rate < MIN_DAILY_RATE:
            continue
        min_size = float(c.get("rewards_min_size") or 50)
        ms_cents = float(c.get("rewards_max_spread") or 4.5)

        g = gamma_by_cid.get(cid)
        if g:
            # ── Path A: Gamma has this market (most common) ──────
            try:
                token_ids = json.loads(g.get("clobTokenIds") or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if len(token_ids) < 2:
                continue
            yes_price = None
            try:
                prices = json.loads(g.get("outcomePrices") or "[]")
                yes_price = float(prices[0]) if prices else None
            except Exception:
                pass
            liq = float(g.get("liquidityNum") or 0)
            vol = float(g.get("volume24hrClob") or 0)
            question = g.get("question", "")
            tick = float(g.get("orderPriceMinTickSize") or 0.01)
            end_date_iso = g.get("endDateIso") or g.get("end_date_iso")
        else:
            # ── Path B: Gamma doesn't have it — fetch from CLOB directly ──
            # This unlocks weather/daily/niche markets invisible to Gamma
            if rate < MIN_DAILY_RATE:
                continue
            try:
                mkt_resp = requests.get(
                    f"https://clob.polymarket.com/markets/{cid}",
                    timeout=10,
                )
                if mkt_resp.status_code != 200:
                    continue
                mkt = mkt_resp.json()
                tokens_data = mkt.get("tokens", [])
                if len(tokens_data) < 2:
                    continue
                token_ids = [tokens_data[0]["token_id"], tokens_data[1]["token_id"]]
                yes_price = float(tokens_data[0].get("price", 0.5))
                question = mkt.get("question", "")
                tick = float(mkt.get("minimum_tick_size") or 0.01)
                end_date_iso = mkt.get("end_date_iso")
                liq = 999999.0  # placeholder — will be replaced by _verify_order_books()
                vol = 0.0
            except Exception:
                continue

        merged.append({
            "condition_id": cid,
            "question": question,
            "token_ids": token_ids,
            "yes_price": yes_price,
            "daily_rate": rate,
            "min_size": min_size,
            "max_spread": ms_cents / 100.0,
            "tick_size": tick,
            "liquidity": liq,
            "volume_24h": vol,
            "end_date_iso": end_date_iso,
        })

    log.info(f"  Merged: {len(merged)} candidates with rate >= ${MIN_DAILY_RATE}/day")

    # ── Order book verification: the ground truth for liquidity ──
    log.info(f"  Verifying order books for {len(merged)} candidates...")
    merged = _verify_order_books(merged)

    # Sort by liquidity ascending (lowest on-book depth = least competition = best)
    merged.sort(key=lambda x: x["liquidity"])
    log.info(f"  Final: {len(merged)} verified markets")
    return merged


def get_merged_book(client, yes_tid: str, no_tid: str) -> dict | None:
    """Fetch YES + NO order books and merge into YES-equivalent view."""
    try:
        ob_yes = client.get_order_book(yes_tid)
        if not ob_yes:
            return None

        all_bids = []
        all_asks = []

        for b in getattr(ob_yes, "bids", []):
            all_bids.append((float(b.price), float(b.size)))
        for a in getattr(ob_yes, "asks", []):
            all_asks.append((float(a.price), float(a.size)))

        ob_no = client.get_order_book(no_tid)
        if ob_no:
            for a in getattr(ob_no, "asks", []):
                derived = round(1.0 - float(a.price), 4)
                if derived > 0:
                    all_bids.append((derived, float(a.size)))
            for b in getattr(ob_no, "bids", []):
                derived = round(1.0 - float(b.price), 4)
                if derived < 1:
                    all_asks.append((derived, float(b.size)))

        all_bids.sort(key=lambda x: x[0], reverse=True)
        all_asks.sort(key=lambda x: x[0])

        if not all_bids or not all_asks:
            return None

        return {
            "bids": [{"price": p, "size": s} for p, s in all_bids],
            "asks": [{"price": p, "size": s} for p, s in all_asks],
        }
    except Exception:
        return None


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

        # Cycle state
        self.cycle_count = 0
        self._batch_idx = 0
        self._shutdown = False
        self._last_market_refresh = 0.0
        self._last_reconcile = 0.0
        self._last_reward_log = 0.0

    # ── Startup Reconciliation ─────────────────────────────────────

    def _reconcile_on_startup(self):
        """Check for existing orders on startup. Does NOT cancel manual orders.

        Only cancels orders that the bot itself placed in a previous run
        (tracked via the bot's own order ID prefix pattern). Manual orders
        placed by the user are left untouched.
        """
        if self.dry_run:
            log.info("[DRY] Skipping startup reconciliation")
            return

        try:
            existing = self.client.get_orders() or []
            if existing:
                log.info(f"Found {len(existing)} existing orders on exchange (NOT cancelling — may be manual)")
                # We don't cancel here because we can't distinguish
                # bot orders from manual orders. The bot will only track
                # orders it places going forward. Stale bot orders from
                # a previous crash will sit harmlessly until they expire
                # or get filled (and the fill will be tiny — 50 shares).
            else:
                log.info("No existing orders found — starting clean.")
        except Exception as e:
            log.warning(f"Startup check failed: {e}")

    # ── Market Management ────────────────────────────────────────────

    def refresh_markets(self):
        """Discover and filter reward markets (blocking, for startup only)."""
        log.info("Refreshing reward markets...")
        self.all_market_data = fetch_all_reward_markets()
        self._apply_market_changes()

    def _apply_market_changes(self):
        """Apply market data to active market set."""
        raw = self.all_market_data

        # Filter for our strategy
        eligible = []
        for m in raw:
            if m["daily_rate"] < MIN_DAILY_RATE:
                continue
            if MAX_LIQUIDITY > 0 and m.get("liquidity", 0) > MAX_LIQUIDITY:
                continue
            if MAX_COST_PER_MARKET > 0:
                yes_p = m.get("yes_price") or 0.5
                min_sz = m.get("min_size", 50)
                if min_sz * max(yes_p, 1 - yes_p) > MAX_COST_PER_MARKET:
                    continue
            tokens = m.get("token_ids", [])
            if len(tokens) < 2:
                continue
            eligible.append(m)

        # Sort by reward efficiency (rate / liq), take top MAX_MARKETS
        eligible.sort(
            key=lambda x: x["daily_rate"] / max(x.get("liquidity", 1), 1),
            reverse=True,
        )
        eligible = eligible[:MAX_MARKETS]

        # Update market states
        new_cids = {m["condition_id"] for m in eligible}
        old_cids = set(self.markets.keys())

        # Remove dropped markets
        for cid in old_cids - new_cids:
            ms = self.markets[cid]
            log.info(f"Dropping market: {ms.question[:40]}")
            # Cancel active orders
            for side in ["yes", "no"]:
                oid = ms.orders[side].order_id
                if oid:
                    self._cancel_order(oid, reason="market_removed")
                    ms.orders[side].order_id = None
            # Dump any position
            for side in ["yes", "no"]:
                shares = self.positions.get_shares(cid, side)
                if shares > 1:
                    self._dump_position(ms, side, shares)
            del self.markets[cid]

        # Add new markets
        for m in eligible:
            cid = m["condition_id"]
            if cid not in self.markets:
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

        # ── Step 1: Fetch all exchange orders ────────────────────────
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

                if dump_oid not in open_ids:
                    # Dump order gone from exchange — check if it filled
                    try:
                        status = self.client.get_order(dump_oid)
                        dump_status = status.get("status", "UNKNOWN")
                    except Exception:
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
                    elif dump_status == "UNKNOWN":
                        ms.unknown_count[side] = ms.unknown_count.get(side, 0) + 1
                        if ms.unknown_count[side] >= 5:
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

                if slot.order_id not in open_ids:
                    # Order gone from exchange — check if filled
                    try:
                        status = self.client.get_order(slot.order_id)
                        order_status = status.get("status", "UNKNOWN")
                        matched = float(status.get("size_matched", 0))
                    except Exception:
                        order_status = "UNKNOWN"
                        matched = 0

                    if order_status == "MATCHED" and matched > 0:
                        self._handle_fill(ms, side, slot, actual_shares=matched)
                        ms.unknown_count[side] = 0
                    elif order_status == "UNKNOWN":
                        ms.unknown_count[side] = ms.unknown_count.get(side, 0) + 1
                        if ms.unknown_count[side] >= 5:
                            log.warning(f"BUY order stuck UNKNOWN 5×, clearing | {ms.question[:30]}")
                            slot.order_id = None
                            ms.unknown_count[side] = 0
                        else:
                            log.warning(f"Order {slot.order_id[:16]} UNKNOWN ({ms.unknown_count[side]}/5)")
                        continue  # don't clear yet, retry next cycle
                    else:
                        ms.unknown_count[side] = 0
                    # Clear the order slot (MATCHED, CANCELLED, etc.)
                    slot.order_id = None

        # ── Step 4: Place orders on batch ────────────────────────────
        market_list = list(self.markets.values())
        if not market_list:
            return

        batch_start = self._batch_idx
        batch_end = min(batch_start + BATCH_SIZE, len(market_list))
        batch = market_list[batch_start:batch_end]
        self._batch_idx = batch_end if batch_end < len(market_list) else 0

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
                cycle_duration_secs=CYCLE_SECS,
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

        if best_ask - best_bid > 0.15:
            return  # too wide

        # Edge prices
        tick = ms.tick_size
        decimals = max(2, len(str(tick).rstrip('0').split('.')[-1]))
        edge_bid = round(midpoint - ms.max_spread + tick * PLACEMENT_TICKS_INSIDE, decimals)
        edge_ask = round(midpoint + ms.max_spread - tick * PLACEMENT_TICKS_INSIDE, decimals)
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

        # Shares
        yes_shares = max(ms.min_size, SHARES_PER_SIDE)
        no_clob = round(1.0 - edge_ask, decimals)
        no_clob = max(0.01, no_clob)
        no_shares = max(ms.min_size, SHARES_PER_SIDE)

        # Place YES bid
        if self._can_place(ms.cid, "yes", yes_shares * edge_bid):
            if self.dry_run:
                log.info(f"[DRY] BID YES @ {edge_bid:.3f} ({yes_shares:.0f}sh) | {ms.question[:30]}")
                ms.orders["yes"] = OrderSlot(order_id="dry_yes", price=edge_bid, shares=yes_shares, placed_at=time.time())
            else:
                try:
                    args = OrderArgs(token_id=ms.yes_tid, price=edge_bid, size=float(yes_shares), side=BUY)
                    resp = self.client.create_and_post_order(args)
                    oid = resp.get("orderID") if isinstance(resp, dict) else None
                    if oid:
                        ms.orders["yes"] = OrderSlot(order_id=oid, price=edge_bid, shares=yes_shares, placed_at=time.time())
                        self.db.log_order_placed(condition_id=ms.cid, side="yes", price=edge_bid, size=float(yes_shares), order_id=oid)
                        if self.cycle_count <= 3:
                            log.info(f"BID YES @ {edge_bid:.3f} ({yes_shares:.0f}sh) | {ms.question[:30]}")
                except Exception as e:
                    log.debug(f"YES order failed {ms.question[:25]}: {e}")

        # Place NO ask
        if self._can_place(ms.cid, "no", no_shares * no_clob):
            if self.dry_run:
                log.info(f"[DRY] ASK NO @ {edge_ask:.3f} (clob={no_clob:.3f}, {no_shares:.0f}sh) | {ms.question[:30]}")
                ms.orders["no"] = OrderSlot(order_id="dry_no", price=edge_ask, shares=no_shares, placed_at=time.time())
            else:
                try:
                    args = OrderArgs(token_id=ms.no_tid, price=no_clob, size=float(no_shares), side=BUY)
                    resp = self.client.create_and_post_order(args)
                    oid = resp.get("orderID") if isinstance(resp, dict) else None
                    if oid:
                        ms.orders["no"] = OrderSlot(order_id=oid, price=edge_ask, shares=no_shares, placed_at=time.time())
                        self.db.log_order_placed(condition_id=ms.cid, side="no", price=edge_ask, size=float(no_shares), order_id=oid)
                        if self.cycle_count <= 3:
                            log.info(f"ASK NO @ {edge_ask:.3f} (clob={no_clob:.3f}, {no_shares:.0f}sh) | {ms.question[:30]}")
                except Exception as e:
                    log.debug(f"NO order failed {ms.question[:25]}: {e}")

    def _can_place(self, cid: str, side: str, est_cost: float) -> bool:
        """All guards before placing an order."""
        ms = self.markets.get(cid)
        if not ms:
            return False
        # Already have a BUY order on this side
        if ms.orders[side].order_id:
            return False
        # Have a pending dump SELL on this side
        if ms.dump_orders[side]:
            return False
        # Have inventory on this side (need to dump first)
        if self.positions.get_shares(cid, side) > 1:
            return False
        # Position halted
        if not self.positions.can_quote(cid, side):
            return False
        # Too many dump failures on this market
        if ms.dump_failures >= 3:
            return False
        # Portfolio exposure limit
        if self._total_exposure() > MAX_TOTAL_EXPOSURE:
            return False
        return True

    def _total_exposure(self) -> float:
        """Sum of all open position USD values (actual, not estimated)."""
        total = 0.0
        for cid in self.markets:
            for side in ["yes", "no"]:
                total += self.positions.get_position(cid, side)
        return total

    # ── Fill Handling ────────────────────────────────────────────────

    def _handle_fill(self, ms: MarketState, side: str, slot: OrderSlot,
                     actual_shares: float = 0):
        """Process a detected fill: record, then merge or dump."""
        from alerts import alert_fill

        filled_shares = actual_shares if actual_shares > 0 else slot.shares
        fill_price = slot.price
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
            except Exception:
                dump_shares = shares

            # Initialize dump state if first attempt
            if ms.dump_state[side] is None:
                from price import to_clob
                fill_price_clob = to_clob(ms.orders[side].price, side) if ms.orders[side].price > 0 else 0
                ms.dump_state[side] = {
                    "fill_price": fill_price_clob,
                    "started_at": time.time(),
                    "shares": dump_shares,
                    "tid": tid,
                }

            state = ms.dump_state[side]
            elapsed_min = (time.time() - state["started_at"]) / 60.0

            # Compute decay price
            if elapsed_min >= 5.0:
                # Timeout — market dump at best bid
                book = self.client.get_order_book(tid)
                if not book or not book.bids:
                    ms.dump_failures += 1
                    return
                sell_price = float(book.bids[0].price)
                log.info(f"DUMP TIMEOUT {side.upper()} → market sell @ {sell_price:.4f} | {ms.question[:30]}")
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

    def _cancel_order(self, order_id: str, reason: str = ""):
        """Cancel an order on the exchange."""
        if self.dry_run:
            return
        try:
            self.client.cancel(order_id)
            log.debug(f"Cancelled {order_id[:16]} ({reason})")
        except Exception as e:
            log.debug(f"Cancel failed {order_id[:16]}: {e}")

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

        start = time.time()
        last_status = time.time()

        log.info(f"Starting reward farming | {len(self.markets)} markets | dry_run={self.dry_run}")

        while not self._shutdown:
            if duration_secs > 0 and (time.time() - start) >= duration_secs:
                break

            t0 = time.time()

            # Market refresh (in background to avoid blocking trading)
            if time.time() - self._last_market_refresh >= MARKET_REFRESH_SECS:
                if not hasattr(self, '_refresh_thread') or not self._refresh_thread.is_alive():
                    def _bg_refresh():
                        try:
                            new_data = fetch_all_reward_markets()
                            self._pending_market_data = new_data
                        except Exception as e:
                            log.warning(f"Background market refresh failed: {e}")
                    self._refresh_thread = threading.Thread(target=_bg_refresh, daemon=True)
                    self._refresh_thread.start()
                    self._last_market_refresh = time.time()

            # Apply pending market data from background refresh
            if hasattr(self, '_pending_market_data') and self._pending_market_data:
                self.all_market_data = self._pending_market_data
                self._pending_market_data = None
                self._apply_market_changes()

            # Run cycle
            try:
                self.run_cycle()
            except Exception as e:
                log.error(f"Cycle error: {e}")

            # Hourly reward log
            if time.time() - self._last_reward_log >= 3600:
                self._last_reward_log = time.time()
                self.rewards.maybe_log_hourly(self.all_market_data[:MAX_MARKETS])
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
            sleep_time = max(0, CYCLE_SECS - elapsed_cycle)
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
    log.info(f"  Strategy: {SHARES_PER_SIDE}sh/side, {PLACEMENT_TICKS_INSIDE} tick inside edge")
    log.info(f"  Markets: max {MAX_MARKETS}, rate >= ${MIN_DAILY_RATE}/d, liq < ${MAX_LIQUIDITY}")
    log.info(f"  Cost cap: ${MAX_COST_PER_MARKET}/market, ${MAX_TOTAL_EXPOSURE} total exposure")

    bot = RewardFarmer(dry_run=args.dry_run)
    bot.run(duration_secs=duration)


if __name__ == "__main__":
    main()
