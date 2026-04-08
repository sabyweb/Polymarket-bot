"""Order lifecycle: placement, fill detection, priority batch, guards.

Extracted from reward_farmer.py. All order-related logic in one module.
"""

import logging
import time

from config import cfg
from models import OrderSlot, MarketState
from market_discovery import get_merged_book

log = logging.getLogger("reward_farmer")

# Config accessors
def SHARES_PER_SIDE(): return cfg("RF_SHARES_PER_SIDE")
def PLACEMENT_TICKS_INSIDE(): return cfg("RF_PLACEMENT_TICKS_INSIDE")
def BATCH_SIZE(): return cfg("RF_BATCH_SIZE")


class OrderLifecycle:
    """Manages order placement, fill detection, and priority batching."""

    def __init__(self, client, db, positions, rewards, markets, dry_run=False):
        """
        Args:
            client: RateLimitedClient (CLOB API)
            db: BotDatabase instance
            positions: PositionStore instance
            rewards: RewardTracker instance
            markets: dict[str, MarketState] — shared reference with RewardFarmer
            dry_run: if True, no real orders placed
        """
        self.client = client
        self.db = db
        self.positions = positions
        self.rewards = rewards
        self.markets = markets  # shared reference — mutations visible to caller
        self.dry_run = dry_run
        self.capital_ceiling: float | None = None  # lowest cost that hit "insufficient balance"
        self.cycle_count = 0
        self._batch_idx = 0

    def cancel_order(self, order_id: str, reason: str = "") -> bool:
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

    def detect_fills(self, open_ids: set):
        """Step 3: Detect BUY order fills from exchange state."""
        for cid, ms in list(self.markets.items()):
            for side in ["yes", "no"]:
                slot = ms.orders[side]
                if not slot.order_id:
                    continue

                if self.dry_run:
                    slot.order_id = None
                    continue

                if slot.order_id not in open_ids:
                    try:
                        status = self.client.get_order(slot.order_id)
                        order_status = status.get("status", "UNKNOWN")
                        matched = float(status.get("size_matched", 0))
                    except Exception as e:
                        log.debug(f"BUY order status check failed {slot.order_id[:16]}: {e}")
                        order_status = "UNKNOWN"
                        matched = 0

                    if matched > 0 and order_status in ("MATCHED", "CANCELLED"):
                        fill_type = "FULL" if matched >= slot.shares - 0.5 else "PARTIAL"
                        # API returns token-specific CLOB price; convert to
                        # YES-equiv so all internal pricing is consistent.
                        # For YES orders CLOB==YES-equiv; for NO orders
                        # CLOB price is 1-YES-equiv, so to_yes_equiv flips it.
                        raw_api_price = float(status.get("price", 0))
                        if raw_api_price > 0:
                            from price import to_yes_equiv
                            actual_price = to_yes_equiv(raw_api_price, side)
                        else:
                            actual_price = slot.price  # already YES-equiv
                        if fill_type == "PARTIAL":
                            log.info(
                                f"PARTIAL fill {side.upper()} {matched:.0f}/{slot.shares:.0f}sh "
                                f"(order {order_status}) | {ms.question[:30]}"
                            )
                        self.handle_fill(ms, side, slot, actual_shares=matched, actual_price=actual_price)
                        ms.unknown_count[side] = 0
                    elif order_status == "UNKNOWN":
                        ms.unknown_count[side] = ms.unknown_count.get(side, 0) + 1
                        if ms.unknown_count[side] >= cfg("RF_UNKNOWN_RETRY_THRESHOLD"):
                            log.warning(f"BUY order stuck UNKNOWN {cfg('RF_UNKNOWN_RETRY_THRESHOLD')}x, clearing | {ms.question[:30]}")
                            # Reconcile: check exchange balance to detect silent fills
                            self._reconcile_after_unknown(ms, side, slot)
                            self.db.delete_active_order(slot.order_id)
                            slot.order_id = None
                            ms.unknown_count[side] = 0
                        else:
                            log.warning(f"Order {slot.order_id[:16]} UNKNOWN ({ms.unknown_count[side]}/{cfg('RF_UNKNOWN_RETRY_THRESHOLD')})")
                        continue
                    else:
                        ms.unknown_count[side] = 0
                    if slot.order_id:
                        self.db.delete_active_order(slot.order_id)
                    slot.order_id = None

    def handle_fill(self, ms: MarketState, side: str, slot: OrderSlot,
                    actual_shares: float = 0, actual_price: float = 0.0):
        """Process a detected fill: record, then merge or dump."""
        from alerts import alert_fill
        from dump_manager import DumpManager

        filled_shares = actual_shares if actual_shares > 0 else slot.shares
        fill_price = actual_price if actual_price > 0 else slot.price
        cid = ms.cid

        log.info(
            f"FILL {side.upper()} {filled_shares:.0f}sh @ {fill_price:.4f} | "
            f"{ms.question[:35]}"
        )

        self.positions.record_fill(cid, side, filled_shares, fill_price, question=ms.question)

        from price import to_clob
        clob_cost = to_clob(fill_price, side)
        self.db.log_fill(
            condition_id=cid, question=ms.question,
            side=side, fill_type="FULL",
            shares=filled_shares, price=fill_price,
            clob_cost=clob_cost, usd_value=filled_shares * clob_cost,
        )

        alert_fill(
            fill_type="FULL", side=side.upper(),
            price=clob_cost, filled_shares=filled_shares,
            filled_usd=filled_shares * clob_cost,
            market_question=ms.question,
        )

        ms.last_fill_price[side] = fill_price
        ms.fill_times[side].append(time.time())

        yes_shares = self.positions.get_shares(cid, "yes")
        no_shares = self.positions.get_shares(cid, "no")
        merge_qty = min(yes_shares, no_shares)
        if merge_qty >= 1.0:
            # Use the dump_manager reference from the farmer
            self._dump_mgr.try_merge(ms, merge_qty)
            return

        self._dump_mgr.dump_position(ms, side, filled_shares)

    def set_dump_manager(self, dump_mgr):
        """Set reference to DumpManager (avoids circular import at init)."""
        self._dump_mgr = dump_mgr

    def place_orders_for_market(self, ms: MarketState):
        """Fetch book + place edge orders for one market."""
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY

        # Skip book fetch if both sides already have orders — saves 2 API calls.
        # Still fetch if a book refresh is due (every 5 min) so repricing and
        # resolution-proximity guards stay active.
        has_both = ms.orders["yes"].order_id and ms.orders["no"].order_id
        book_age = time.time() - ms.last_book_fetch
        if has_both and book_age < 300:
            return

        merged = get_merged_book(self.client, ms.yes_tid, ms.no_tid)
        if not merged or not merged["bids"] or not merged["asks"]:
            ms.book_failures += 1
            return

        ms.book_failures = 0  # reset on success
        best_bid = float(merged["bids"][0]["price"])
        best_ask = float(merged["asks"][0]["price"])
        midpoint = (best_bid + best_ask) / 2
        ms.midpoint = midpoint
        ms.last_book_fetch = time.time()

        if best_ask - best_bid > cfg("RF_MAX_BOOK_SPREAD"):
            self.db.write_placement_feedback(ms.cid, "yes", "skipped", "wide_spread")
            self.db.write_placement_feedback(ms.cid, "no", "skipped", "wide_spread")
            return

        # ── Resolution proximity guard (real-time) ──
        # The agent detects this every ~30min, but markets can move fast.
        # Block placement if midpoint suggests the market is near resolution.
        # This closes the gap between agent cycles and prevents placing on
        # markets that moved to 0.95 after the last agent run.
        if midpoint > 0.90 or midpoint < 0.10:
            log.info(
                f"SKIP resolution proximity | mid={midpoint:.3f} | {ms.question[:30]}"
            )
            self.db.write_placement_feedback(ms.cid, "yes", "skipped", "resolution_proximity")
            self.db.write_placement_feedback(ms.cid, "no", "skipped", "resolution_proximity")
            # Also cancel any existing orders — don't stay exposed
            for side in ("yes", "no"):
                slot = ms.orders[side]
                if slot.order_id:
                    if self.cancel_order(slot.order_id, reason="resolution_proximity"):
                        self.db.delete_active_order(slot.order_id)
                        slot.order_id = None
            return

        # ── Live sports guard ──
        # Sports markets (detected by "vs" pattern) expiring within 12 hours
        # are likely live or imminent — extreme adverse selection risk from
        # informed bettors watching the event. Skip placement AND cancel
        # any existing orders. Future sports markets (days away) are fine.
        if ms.question and ms.end_date_iso:
            q_lower = ms.question.lower()
            _is_sports_q = (
                " vs " in q_lower or " vs. " in q_lower
                or any(kw in q_lower for kw in (
                    "premier league", "serie a", "la liga", "bundesliga",
                    "champions league", "nba", "nfl", "mlb", "nhl",
                    "ipl", "cricket", "grand prix", "masters",
                    "atp", "wta", "ufc", "formula 1",
                ))
            )
            if _is_sports_q:
                try:
                    from datetime import datetime, timezone
                    dt = datetime.fromisoformat(ms.end_date_iso.replace("Z", "+00:00"))
                    hours_to_expiry = (dt - datetime.now(timezone.utc)).total_seconds() / 3600
                    if 0 < hours_to_expiry <= 12:
                        log.info(
                            f"SKIP live sports | expires in {hours_to_expiry:.1f}h | {ms.question[:40]}"
                        )
                        self.db.write_placement_feedback(ms.cid, "yes", "skipped", "live_sports")
                        self.db.write_placement_feedback(ms.cid, "no", "skipped", "live_sports")
                        for side in ("yes", "no"):
                            slot = ms.orders[side]
                            if slot.order_id:
                                if self.cancel_order(slot.order_id, reason="live_sports"):
                                    self.db.delete_active_order(slot.order_id)
                                    slot.order_id = None
                        return
                except Exception:
                    pass  # Can't parse date — continue normally

        tick = ms.tick_size
        decimals = max(2, len(str(tick).rstrip('0').split('.')[-1]))
        edge_bid = round(midpoint - ms.max_spread + tick * PLACEMENT_TICKS_INSIDE(), decimals)
        edge_ask = round(midpoint + ms.max_spread - tick * PLACEMENT_TICKS_INSIDE(), decimals)
        edge_bid = max(0.01, edge_bid)
        edge_ask = min(0.99, edge_ask)

        # Reprice stale orders outside reward window
        for side, edge_price in [("yes", edge_bid), ("no", edge_ask)]:
            slot = ms.orders[side]
            if not slot.order_id:
                continue
            order_dist = abs(slot.price - midpoint)
            if order_dist >= ms.max_spread:
                if self.cancel_order(slot.order_id, reason="outside_reward_window"):
                    log.info(
                        f"REPRICE {side.upper()} | old={slot.price:.3f} dist={order_dist:.3f} >= spread={ms.max_spread:.3f} | "
                        f"new={edge_price:.3f} | {ms.question[:30]}"
                    )
                    self.db.delete_active_order(slot.order_id)
                    slot.order_id = None

        # Exit liquidity check
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

        if not can_exit_yes and not ms.orders["yes"].order_id:
            self.db.write_placement_feedback(ms.cid, "yes", "skipped", "exit_liquidity")
        if not can_exit_no and not ms.orders["no"].order_id:
            self.db.write_placement_feedback(ms.cid, "no", "skipped", "exit_liquidity")

        # Sizing
        shares_target = ms.agent_shares if ms.agent_shares > 0 else SHARES_PER_SIDE()
        yes_shares = max(ms.min_size, shares_target)
        no_clob = round(1.0 - edge_ask, decimals)
        no_clob = max(0.01, no_clob)
        no_shares = max(ms.min_size, shares_target)

        # Place YES bid
        if can_exit_yes:
            can, reason = self.can_place(ms.cid, "yes", yes_shares * edge_bid)
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
                            self.db.write_placement_feedback(ms.cid, "yes", "failed", "no_order_id")
                            log.warning(f"YES order got no orderID | {ms.question[:25]}")
                    except Exception as e:
                        err_str = str(e).lower()
                        if "insufficient" in err_str or "balance" in err_str or "not enough" in err_str:
                            failed_cost = yes_shares * edge_bid
                            prev = self.capital_ceiling
                            self.capital_ceiling = min(prev if prev is not None else float('inf'), failed_cost)
                            log.warning(
                                f"Insufficient balance (YES, ${failed_cost:.1f}) — "
                                f"ceiling=${self.capital_ceiling:.1f} | {ms.question[:30]}"
                            )
                            self.db.write_placement_feedback(ms.cid, "yes", "failed", "capital_exhausted")
                            # Don't return — let NO side try if it's cheaper
                        else:
                            self.db.write_placement_feedback(ms.cid, "yes", "failed", "order_error")
                            log.debug(f"YES order failed {ms.question[:25]}: {e}")
            else:
                if reason not in ("already_has_order", "dump_pending"):
                    self.db.write_placement_feedback(ms.cid, "yes", "skipped", reason)

        # Place NO ask
        if can_exit_no:
            can, reason = self.can_place(ms.cid, "no", no_shares * no_clob)
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
                            failed_cost = no_shares * no_clob
                            prev = self.capital_ceiling
                            self.capital_ceiling = min(prev if prev is not None else float('inf'), failed_cost)
                            log.warning(
                                f"Insufficient balance (NO, ${failed_cost:.1f}) — "
                                f"ceiling=${self.capital_ceiling:.1f} | {ms.question[:30]}"
                            )
                            self.db.write_placement_feedback(ms.cid, "no", "failed", "capital_exhausted")
                        else:
                            self.db.write_placement_feedback(ms.cid, "no", "failed", "order_error")
                            log.debug(f"NO order failed {ms.question[:25]}: {e}")
            else:
                if reason not in ("already_has_order", "dump_pending"):
                    self.db.write_placement_feedback(ms.cid, "no", "skipped", reason)

    def get_priority_batch(self, market_list: list) -> list:
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

        needs_orders.sort(key=lambda x: x.daily_rate, reverse=True)
        batch = needs_orders[:BATCH_SIZE()]

        if len(batch) < BATCH_SIZE() and has_orders:
            remaining = BATCH_SIZE() - len(batch)
            start = self._batch_idx % max(len(has_orders), 1)
            for i in range(remaining):
                batch.append(has_orders[(start + i) % len(has_orders)])
            self._batch_idx = (start + remaining) % max(len(has_orders), 1)

        return batch

    def can_place(self, cid: str, side: str, est_cost: float) -> tuple[bool, str]:
        """All guards before placing an order. Returns (can_place, reason)."""
        ms = self.markets.get(cid)
        if not ms:
            return False, "no_market"
        if self.capital_ceiling is not None and est_cost >= self.capital_ceiling:
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
        # Fill-rate breaker: block placement if fills are clustering.
        # Per-side check catches directional cascades (same side hit repeatedly).
        # Total check catches broad activity across both sides.
        now = time.time()
        fill_window = cfg("RF_FILL_BREAKER_WINDOW")
        for s in ("yes", "no"):
            ms.fill_times[s] = [t for t in ms.fill_times[s] if now - t < fill_window]
        side_threshold = cfg("RF_FILL_BREAKER_SIDE_THRESHOLD")
        for s in ("yes", "no"):
            if len(ms.fill_times[s]) >= side_threshold:
                return False, "fill_rate_breaker"
        recent_fills = len(ms.fill_times["yes"]) + len(ms.fill_times["no"])
        if recent_fills >= cfg("RF_FILL_BREAKER_THRESHOLD"):
            return False, "fill_rate_breaker"
        return True, ""

    def _reconcile_after_unknown(self, ms: MarketState, side: str, slot: OrderSlot):
        """Check exchange balance when clearing an UNKNOWN order.

        If the order silently filled, the exchange will have more shares than
        we're tracking. Detect this and record the fill so position tracking
        stays in sync.
        """
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            tid = ms.yes_tid if side == "yes" else ms.no_tid
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
            )
            actual = float(bal.get("balance", 0)) / 1e6
            tracked = self.positions.get_shares(ms.cid, side)
            surplus = actual - tracked
            if surplus >= 1.0:
                log.warning(
                    f"UNKNOWN reconcile: exchange has {actual:.0f} but tracking {tracked:.0f} "
                    f"({surplus:.0f} surplus) — recording as fill | {ms.question[:30]}"
                )
                self.handle_fill(ms, side, slot, actual_shares=surplus, actual_price=slot.price)
            else:
                log.info(f"UNKNOWN reconcile: no surplus (exchange={actual:.0f} tracked={tracked:.0f}) | {ms.question[:30]}")
        except Exception as e:
            log.warning(f"UNKNOWN reconcile balance check failed {side} {ms.question[:25]}: {e}")

    def total_exposure(self) -> float:
        """Sum of all open position USD values."""
        total = 0.0
        for cid in self.markets:
            for side in ["yes", "no"]:
                total += self.positions.get_position(cid, side)
        return total
