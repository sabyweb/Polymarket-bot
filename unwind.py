"""
Inventory unwinding logic for the Polymarket market-making bot.

Handles SELL order placement, price decay, stop-loss, position merging,
and unwind reconciliation.
"""

import logging
import threading
import time as _time
from py_clob_client_v2.clob_types import OrderArgs, BalanceAllowanceParams, AssetType
from py_clob_client_v2.order_builder.constants import SELL
from config import (
    MIN_UNWIND_SHARES,
    UNWIND_DECAY_INTERVAL_SECS, UNWIND_DECAY_TICKS,
    MIN_SELL_PRICE, STOP_LOSS_PCT, MIN_STOP_LOSS_USD, STOP_LOSS_MIN_PRICE,
    UNWIND_ACCEL_TIERS,
    UNWIND_AGE_ACCEL_HOURS, UNWIND_AGE_ACCEL_TICKS,
    UNWIND_AGE_MAX_HOURS, UNWIND_AGE_MAX_TICKS,
    REWARD_LOSS_BUDGET_PCT,
)
from alerts import alert_unwind, alert_merge_needed
from price import to_clob
from database import get_db

log = logging.getLogger(__name__)


class UnwindMixin:
    """Mixin providing inventory unwinding methods for OrderManager."""

    def _get_reward_rate(self) -> float:
        """Get estimated hourly reward rate for this market.

        Used by reward-offset unwind pricing to bound decay losses
        to a fraction of earned rewards.

        Returns:
            Estimated $/hour reward rate, or 0.0 if unknown.
        """
        tracker = getattr(self, "_reward_tracker", None)
        if not tracker:
            return 0.0
        cid = self.market["condition_id"]
        stats = tracker.markets.get(cid)
        if not stats:
            return 0.0
        snapshots = getattr(stats, "reward_snapshots", [])
        if not snapshots:
            return 0.0
        # Use the most recent hourly snapshot
        latest = snapshots[-1] if snapshots else {}
        return latest.get("est_hourly", 0.0)

    def place_unwind_order(
        self, side: str, fill_price: float, fill_size: float,
        clob_price_override: float | None = None,
        created_at_override: float | None = None,
    ) -> str | None:
        """Place a SELL limit order to offload filled inventory.

        When a BUY order fills, we hold tokens we don't want. This places
        a SELL order to unwind the position. The sell price starts at
        acquisition cost (VWAP) and decays over time to ensure the sell
        eventually fills.

        Args:
            side: "yes" or "no" — which side was filled.
            fill_price: The YES-equivalent price we paid (VWAP).
            fill_size: Number of shares to sell.
            clob_price_override: If set, use this as the CLOB sell price
                instead of computing from fill_price. Used for decayed
                and stop-loss sells.

        Returns:
            Exchange order ID of the unwind order, or None on failure.
        """
        question = self.market["question"]

        if clob_price_override is not None:
            clob_price = clob_price_override
            if side == "yes":
                token_id = self.market["token_ids"][0]
            else:
                token_id = self.market["token_ids"][1]
        elif side == "yes":
            token_id = self.market["token_ids"][0]
            clob_price = self.round_down_to_tick(fill_price)
        else:
            token_id = self.market["token_ids"][1]
            clob_price = self.round_down_to_tick(to_clob(fill_price, "no"))

        if clob_price is None or clob_price <= 0:
            log.warning(
                f"Invalid unwind price ({clob_price}) for {side.upper()} "
                f"on {question[:40]} — skipping unwind"
            )
            return None

        # Pre-flight: verify we actually hold the tokens before attempting SELL.
        actual_balance = self.verify_token_balance(side)
        if actual_balance >= 0 and actual_balance < fill_size - 0.5:
            log.warning(
                f"SELL PRE-FLIGHT FAILED | {side.upper()} | "
                f"want_to_sell={fill_size:.2f} | "
                f"actual_balance={actual_balance:.2f} | "
                f"market={question[:40]} — "
                f"reducing sell size to actual balance"
            )
            if actual_balance < MIN_UNWIND_SHARES:
                log.info(
                    f"Actual balance too small to unwind ({actual_balance:.2f}) "
                    f"— skipping SELL"
                )
                return None
            fill_size = actual_balance

        log.info(
            f"SELL PRE-FLIGHT | {side.upper()} | "
            f"token_id={token_id[:16]}... | "
            f"actual_balance={actual_balance:.2f} | "
            f"sell_size={fill_size:.2f} | "
            f"clob_price={clob_price:.4f} | "
            f"market={question[:40]}"
        )

        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=clob_price,
                size=float(fill_size),
                side=SELL,
            )
            response = self.client.create_and_post_order(order_args)

            if isinstance(response, dict) and not response.get("success", True):
                raise Exception(
                    f"Unwind rejected: {response.get('errorMsg', response)}"
                )

            # Use POST response orderID directly
            order_id = None
            if isinstance(response, dict):
                order_id = response.get("orderID")

            if order_id:
                # base_clob_price: the VWAP-based sell price (before decay).
                if side == "yes":
                    base = self.round_down_to_tick(fill_price)
                else:
                    base = self.round_down_to_tick(to_clob(fill_price, "no"))

                from order_manager import UnwindOrder
                self.unwind_orders[order_id] = UnwindOrder(
                    side=side,
                    price=fill_price,
                    clob_price=clob_price,
                    size=float(fill_size),
                    placed_at=_time.time(),
                    created_at=created_at_override or _time.time(),
                    base_clob_price=base,
                )

                log.info(
                    f"UNWIND ORDER PLACED | SELL {side.upper()} | "
                    f"price={clob_price:.4f} | size={fill_size:.2f} | "
                    f"market={question[:40]} | id={order_id}"
                )
                get_db().log_order_placed(
                    condition_id=self.market["condition_id"],
                    side=side, price=clob_price,
                    size=float(fill_size), order_id=order_id,
                    order_type="SELL",
                )
                return order_id
            else:
                log.error(
                    f"Unwind order placed but POST response returned no ID "
                    f"for {side.upper()} on {question[:40]} — "
                    f"will be reconciled next cycle"
                )
                return None

        except Exception as e:
            error_msg = str(e).lower()
            if "not enough balance" in error_msg or "allowance" in error_msg:
                log.warning(
                    f"SELL rejected (balance/allowance) for {side.upper()} "
                    f"on {question[:40]} — fixing allowance + scheduling async retry"
                )
                try:
                    self.client.update_balance_allowance(
                        BalanceAllowanceParams(
                            asset_type=AssetType.CONDITIONAL,
                            token_id=token_id,
                        )
                    )
                    log.info(
                        f"CONDITIONAL allowance updated for token "
                        f"{token_id[:16]}... — retry scheduled in 5s (non-blocking)"
                    )
                except Exception as allowance_err:
                    log.error(
                        f"Allowance update failed for {side.upper()} "
                        f"on {question[:40]}: {allowance_err}"
                    )

                # Schedule non-blocking retry: Timer fires in 5s on a daemon thread.
                # If this fails, reconcile_unwinds() will catch the uncovered
                # position on the next cycle — no inventory is lost.
                retry_args = (
                    token_id, side, clob_price, float(fill_size),
                    fill_price, question, created_at_override,
                )
                timer = threading.Timer(5.0, self._retry_unwind_sell, args=retry_args)
                timer.daemon = True
                timer.start()
            else:
                log.error(
                    f"Failed to place unwind order for {side.upper()} "
                    f"on {question[:40]}: {e}"
                )
            return None

    def _retry_unwind_sell(
        self, token_id: str, side: str, clob_price: float,
        fill_size: float, fill_price: float, question: str,
        created_at_override: float | None,
    ) -> None:
        """Async retry for SELL orders after allowance fix (runs on Timer thread).

        Called 5s after the initial attempt failed due to allowance/balance issues.
        If this also fails, schedules one more retry at +10s. After that, gives up
        and lets reconcile_unwinds() handle it on the next cycle.
        """
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=clob_price,
                size=fill_size,
                side=SELL,
            )
            response = self.client.create_and_post_order(order_args)

            if isinstance(response, dict) and not response.get("success", True):
                raise Exception(
                    f"Retry rejected: {response.get('errorMsg', response)}"
                )

            order_id = None
            if isinstance(response, dict):
                order_id = response.get("orderID")
            if order_id:
                if side == "yes":
                    base = self.round_down_to_tick(fill_price)
                else:
                    base = self.round_down_to_tick(to_clob(fill_price, "no"))
                from order_manager import UnwindOrder
                # Thread-safe: use lock since this runs on a Timer thread
                lock = getattr(self, "_unwind_lock", None)
                if lock:
                    with lock:
                        self.unwind_orders[order_id] = UnwindOrder(
                            side=side, price=fill_price,
                            clob_price=clob_price, size=fill_size,
                            placed_at=_time.time(),
                            created_at=created_at_override or _time.time(),
                            base_clob_price=base,
                        )
                else:
                    self.unwind_orders[order_id] = UnwindOrder(
                        side=side, price=fill_price,
                        clob_price=clob_price, size=fill_size,
                        placed_at=_time.time(),
                        created_at=created_at_override or _time.time(),
                        base_clob_price=base,
                    )
                log.info(
                    f"UNWIND ORDER PLACED (async retry) | "
                    f"SELL {side.upper()} | price={clob_price:.4f} | "
                    f"size={fill_size:.2f} | market={question[:40]} | "
                    f"id={order_id}"
                )
                get_db().log_order_placed(
                    condition_id=self.market["condition_id"],
                    side=side, price=clob_price,
                    size=fill_size, order_id=order_id,
                    order_type="SELL",
                )
                return

        except Exception as retry_err:
            log.warning(
                f"SELL async retry #1 failed for {side.upper()} "
                f"on {question[:40]}: {retry_err} — scheduling final retry in 10s"
            )

        # Schedule final retry
        retry_args = (
            token_id, side, clob_price, fill_size,
            fill_price, question, created_at_override,
        )
        timer = threading.Timer(10.0, self._retry_unwind_final, args=retry_args)
        timer.daemon = True
        timer.start()

    def _retry_unwind_final(
        self, token_id: str, side: str, clob_price: float,
        fill_size: float, fill_price: float, question: str,
        created_at_override: float | None,
    ) -> None:
        """Final async retry for SELL order (runs on Timer thread).

        If this fails, reconcile_unwinds() will detect the uncovered position
        next cycle and place a fresh sell order.
        """
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=clob_price,
                size=fill_size,
                side=SELL,
            )
            response = self.client.create_and_post_order(order_args)

            if isinstance(response, dict) and not response.get("success", True):
                raise Exception(
                    f"Final retry rejected: {response.get('errorMsg', response)}"
                )

            order_id = None
            if isinstance(response, dict):
                order_id = response.get("orderID")
            if order_id:
                if side == "yes":
                    base = self.round_down_to_tick(fill_price)
                else:
                    base = self.round_down_to_tick(to_clob(fill_price, "no"))
                from order_manager import UnwindOrder
                lock = getattr(self, "_unwind_lock", None)
                uorder = UnwindOrder(
                    side=side, price=fill_price,
                    clob_price=clob_price, size=fill_size,
                    placed_at=_time.time(),
                    created_at=created_at_override or _time.time(),
                    base_clob_price=base,
                )
                if lock:
                    with lock:
                        self.unwind_orders[order_id] = uorder
                else:
                    self.unwind_orders[order_id] = uorder
                log.info(
                    f"UNWIND ORDER PLACED (final retry) | "
                    f"SELL {side.upper()} | price={clob_price:.4f} | "
                    f"size={fill_size:.2f} | market={question[:40]} | "
                    f"id={order_id}"
                )
                get_db().log_order_placed(
                    condition_id=self.market["condition_id"],
                    side=side, price=clob_price,
                    size=fill_size, order_id=order_id,
                    order_type="SELL",
                )
            else:
                log.error(
                    f"Final SELL retry returned no order ID for {side.upper()} "
                    f"on {question[:40]} — reconcile_unwinds will handle next cycle"
                )
        except Exception as e:
            log.error(
                f"Final SELL retry also failed for {side.upper()} "
                f"on {question[:40]}: {e} — reconcile_unwinds will handle next cycle"
            )

    def _tiered_decay_ticks(
        self, vwap_clob: float, side: str, created_at: float | None = None,
    ) -> int:
        """Calculate decay ticks based on loss severity AND position age.

        Two independent accelerators stack:
        1. Loss tiers: 5% -> 2x, 10% -> 3x, 15% -> 4x
        2. Age: >24h -> +2 ticks, >48h -> +4 ticks
        """
        base_ticks = UNWIND_DECAY_TICKS

        # Loss-based acceleration
        if vwap_clob > 0:
            market_bid = self._last_market_bid(side)
            cur_loss = (vwap_clob - market_bid) / vwap_clob
            multiplier = 1
            for threshold, mult in UNWIND_ACCEL_TIERS:
                if cur_loss >= threshold:
                    multiplier = mult
            base_ticks = UNWIND_DECAY_TICKS * multiplier

        # Age-based acceleration (additive, stacks with loss tiers)
        if created_at is not None:
            age_hours = (_time.time() - created_at) / 3600
            if age_hours >= UNWIND_AGE_MAX_HOURS:
                base_ticks += UNWIND_AGE_MAX_TICKS
            elif age_hours >= UNWIND_AGE_ACCEL_HOURS:
                base_ticks += UNWIND_AGE_ACCEL_TICKS

        return base_ticks

    def reconcile_unwinds(self) -> None:
        """Position-based unwind reconciliation with exchange verification.

        Each cycle, for each side:
        1. ALWAYS check actual token balance on exchange (catches untracked fills)
        2. Sync tracker with exchange reality
        3. Check for merge opportunity (both YES and NO held)
        4. Sum sizes of all active unwind orders for this side
        5. If unhedged > MIN_UNWIND_SHARES: place ONE unwind order
        """
        condition_id = self.market["condition_id"]
        actual_balances: dict[str, float] = {}

        # ── Phase 1: Verify actual balances for BOTH sides ────────────────
        for side in ("yes", "no"):
            position_shares = self.position_tracker.get_shares(condition_id, side)
            actual_balance = self.verify_token_balance(side)
            actual_balances[side] = actual_balance

            if actual_balance < 0:
                continue

            if actual_balance >= MIN_UNWIND_SHARES and position_shares < MIN_UNWIND_SHARES:
                yes_price = self.market.get("yes_price") or 0.50
                est_price = yes_price
                log.warning(
                    f"UNTRACKED POSITION DISCOVERED | {side.upper()} | "
                    f"tracker={position_shares:.2f} | "
                    f"actual={actual_balance:.2f} shares | "
                    f"est_price={est_price:.4f} | "
                    f"market={self.market['question'][:40]}"
                )
                self.position_tracker.record_fill(
                    condition_id, side, actual_balance, est_price,
                    question=self.market["question"],
                )

            elif actual_balance < MIN_UNWIND_SHARES and position_shares >= MIN_UNWIND_SHARES:
                log.warning(
                    f"POSITION CORRECTION | {side.upper()} | "
                    f"tracker={position_shares:.2f} shares | "
                    f"actual={actual_balance:.2f} shares | "
                    f"Resetting stale position | "
                    f"market={self.market['question'][:40]}"
                )
                self.position_tracker.reset_side(condition_id, side)

            elif (actual_balance >= MIN_UNWIND_SHARES
                  and abs(actual_balance - position_shares) > 0.5):
                log.warning(
                    f"POSITION ADJUSTMENT | {side.upper()} | "
                    f"tracker={position_shares:.2f} | "
                    f"actual={actual_balance:.2f} | "
                    f"Correcting to match exchange | "
                    f"market={self.market['question'][:40]}"
                )
                self.position_tracker.set_shares(
                    condition_id, side, actual_balance
                )

        # ── Phase 2: Check for merge opportunity ──────────────────────────
        yes_shares = self.position_tracker.get_shares(condition_id, "yes")
        no_shares = self.position_tracker.get_shares(condition_id, "no")
        if yes_shares >= MIN_UNWIND_SHARES and no_shares >= MIN_UNWIND_SHARES:
            mergeable = min(yes_shares, no_shares)
            freed_usd = mergeable
            log.info(
                f"MERGE OPPORTUNITY | "
                f"YES={yes_shares:.2f} | NO={no_shares:.2f} | "
                f"mergeable={mergeable:.2f} pairs | "
                f"would_free=${freed_usd:.2f} USDC | "
                f"market={self.market['question'][:40]}"
            )
            merged = self._try_merge_positions(condition_id, mergeable)
            if merged:
                self.position_tracker.record_unwind(
                    condition_id, "yes", mergeable,
                    self.position_tracker.get_avg_price(condition_id, "yes"),
                )
                self.position_tracker.record_unwind(
                    condition_id, "no", mergeable,
                    self.position_tracker.get_avg_price(condition_id, "no"),
                )
                self.invalidate_balance_cache()
                yes_shares = self.position_tracker.get_shares(condition_id, "yes")
                no_shares = self.position_tracker.get_shares(condition_id, "no")

        # ── Phase 3: Consolidate & place unwind orders (with decay) ────────
        now = _time.time()
        for side in ("yes", "no"):
            position_shares = self.position_tracker.get_shares(condition_id, side)
            avg_price = self.position_tracker.get_avg_price(condition_id, side)

            if position_shares < MIN_UNWIND_SHARES or avg_price <= 0:
                continue

            tick = self.market.get("tick_size", 0.01)
            vwap_clob = self.round_down_to_tick(to_clob(avg_price, side))

            # ── Inline spread capture ──
            market_bid = self._last_market_bid(side)
            if market_bid > vwap_clob and market_bid > MIN_SELL_PRICE:
                profit_pct = (market_bid - vwap_clob) / vwap_clob if vwap_clob > 0 else 0
                has_unwind = any(u.side == side for u in self.unwind_orders.values())
                if profit_pct >= 0.005 and not has_unwind:
                    sell_price = self.round_down_to_tick(market_bid)
                    est_profit = (sell_price - vwap_clob) * position_shares
                    log.info(
                        f"SPREAD CAPTURE | {side.upper()} | "
                        f"cost={vwap_clob:.4f} | bid={market_bid:.4f} | "
                        f"profit={profit_pct:.2%} | shares={position_shares:.0f} | "
                        f"est=${est_profit:.2f} | "
                        f"market={self.market['question'][:40]}"
                    )
                    self.place_unwind_order(
                        side, avg_price, position_shares,
                        clob_price_override=sell_price,
                    )
                    continue

            # Check existing unwind orders — are they at the right price?
            stale_orders: list[str] = []
            covered_shares = 0.0

            # Reward-offset floor: never lose more than REWARD_LOSS_BUDGET_PCT
            # of the rewards earned while holding this position.
            reward_rate = self._get_reward_rate()

            for oid, uorder in self.unwind_orders.items():
                if uorder.side != side:
                    continue
                covered_shares += uorder.size

                base = uorder.base_clob_price or uorder.clob_price or vwap_clob
                created = uorder.created_at or uorder.placed_at or now
                elapsed = now - created
                check_ticks = self._tiered_decay_ticks(vwap_clob, side, created_at=created)
                decay_intervals = int(elapsed // UNWIND_DECAY_INTERVAL_SECS)
                decay_amount = decay_intervals * check_ticks * tick

                # Reward-offset: compute max acceptable loss per share
                # based on rewards earned during hold time
                reward_floor = MIN_SELL_PRICE
                if reward_rate > 0 and position_shares > 0:
                    reward_budget = (
                        reward_rate * (elapsed / 3600)
                        * REWARD_LOSS_BUDGET_PCT
                    )
                    max_loss_per_share = reward_budget / position_shares
                    reward_floor = max(MIN_SELL_PRICE, vwap_clob - max_loss_per_share)

                expected_clob = max(reward_floor, base - decay_amount)

                if abs(base - vwap_clob) >= tick:
                    log.info(
                        f"VWAP shifted | {side.upper()} | "
                        f"old_base={base:.4f} -> new_vwap={vwap_clob:.4f} | "
                        f"market={self.market['question'][:40]}"
                    )
                    stale_orders.append(oid)
                elif uorder.clob_price > expected_clob + tick * 0.5:
                    log.info(
                        f"PRICE DECAY | {side.upper()} | "
                        f"current={uorder.clob_price:.4f} -> "
                        f"decayed={expected_clob:.4f} | "
                        f"age={elapsed/60:.0f}min | "
                        f"market={self.market['question'][:40]}"
                    )
                    stale_orders.append(oid)

            # Log when sell order is held (not yet stale) for visibility
            if not stale_orders and covered_shares >= MIN_UNWIND_SHARES:
                for oid, uorder in self.unwind_orders.items():
                    if uorder.side != side:
                        continue
                    _elapsed = now - (uorder.created_at or now)
                    _next = UNWIND_DECAY_INTERVAL_SECS - (
                        _elapsed % UNWIND_DECAY_INTERVAL_SECS
                    )
                    log.info(
                        f"UNWIND HOLD | {side.upper()} | "
                        f"price={uorder.clob_price:.4f} | "
                        f"age={_elapsed/60:.1f}min | "
                        f"next_decay_in={_next:.0f}s | "
                        f"market={self.market['question'][:40]}"
                    )

            # Capture oldest created_at from stale orders BEFORE deleting
            oldest_created = now
            if stale_orders:
                for oid in stale_orders:
                    uo = self.unwind_orders.get(oid)
                    if uo:
                        c = uo.created_at or uo.placed_at or now
                        oldest_created = min(oldest_created, c)

                stale_prices = [
                    f"{self.unwind_orders[oid].clob_price:.4f}"
                    for oid in stale_orders
                    if oid in self.unwind_orders
                ]
                log.info(
                    f"UNWIND REFRESH | {side.upper()} | "
                    f"cancelling {len(stale_orders)} order(s) at "
                    f"[{', '.join(stale_prices)}] | "
                    f"market={self.market['question'][:40]}"
                )
                cancelled_oids = []
                for oid in stale_orders:
                    if self.cancel_order(oid, reason="decay_refresh"):
                        if oid in self.unwind_orders:
                            del self.unwind_orders[oid]
                        cancelled_oids.append(oid)
                    else:
                        log.warning(
                            f"Stale unwind cancel failed for {oid[:16]}... — "
                            f"keeping in tracker for retry next cycle"
                        )
                stale_orders = cancelled_oids

                # Recalculate covered after cancellations
                covered_shares = 0.0
                for uorder in self.unwind_orders.values():
                    if uorder.side == side:
                        covered_shares += uorder.size

            unhedged = position_shares - covered_shares
            if unhedged < MIN_UNWIND_SHARES:
                continue

            actual = actual_balances.get(side, -1)

            # Determine created_at for the replacement order
            carry_created = oldest_created
            for uorder in self.unwind_orders.values():
                if uorder.side == side:
                    c = uorder.created_at or now
                    carry_created = min(carry_created, c)

            decay_ticks = self._tiered_decay_ticks(vwap_clob, side, created_at=carry_created)

            elapsed = now - carry_created
            decay_intervals = int(elapsed // UNWIND_DECAY_INTERVAL_SECS)
            decay_amount = decay_intervals * decay_ticks * tick
            decayed_clob = max(MIN_SELL_PRICE, vwap_clob - decay_amount)

            log.info(
                f"UNWIND RECONCILIATION | {side.upper()} | "
                f"position={position_shares:.2f} | covered={covered_shares:.2f} | "
                f"unhedged={unhedged:.2f} | vwap={avg_price:.4f} | "
                f"sell_price={decayed_clob:.4f} | "
                f"decay={decay_intervals} intervals ({elapsed/60:.0f}min) | "
                f"actual_balance={actual:.2f} | "
                f"market={self.market['question'][:40]}"
            )

            self.place_unwind_order(
                side, avg_price, unhedged,
                clob_price_override=decayed_clob,
                created_at_override=carry_created,
            )

    def check_stop_loss(self, best_bid: float, best_ask: float) -> None:
        """Check if any position has breached the stop-loss threshold.

        If unrealized loss >= STOP_LOSS_PCT, cancel existing unwind orders
        for that side and place an immediate sell at the current market bid.
        """
        condition_id = self.market["condition_id"]
        tick = self.market.get("tick_size", 0.01)

        for side in ("yes", "no"):
            position_shares = self.position_tracker.get_shares(condition_id, side)
            avg_price = self.position_tracker.get_avg_price(condition_id, side)

            if position_shares < MIN_UNWIND_SHARES or avg_price <= 0:
                continue

            our_cost = self.round_down_to_tick(to_clob(avg_price, side))
            if side == "yes":
                market_bid = best_bid
            else:
                market_bid = max(MIN_SELL_PRICE, round(to_clob(best_ask, "no"), 4))

            if our_cost <= 0:
                continue

            unrealized_loss_pct = (our_cost - market_bid) / our_cost
            unrealized_loss_usd = (our_cost - market_bid) * position_shares

            if our_cost < STOP_LOSS_MIN_PRICE:
                continue

            if unrealized_loss_pct < STOP_LOSS_PCT or unrealized_loss_usd < MIN_STOP_LOSS_USD:
                continue

            # ── STOP-LOSS TRIGGERED ──────────────────────────────────
            sell_price = self.round_down_to_tick(market_bid)
            if sell_price < MIN_SELL_PRICE:
                sell_price = MIN_SELL_PRICE

            log.warning(
                f"STOP-LOSS TRIGGERED | {side.upper()} | "
                f"cost={our_cost:.4f} | market={market_bid:.4f} | "
                f"loss={unrealized_loss_pct:.1%} / ${unrealized_loss_usd:.0f} "
                f"(threshold={STOP_LOSS_PCT:.0%} AND ${MIN_STOP_LOSS_USD:.0f}) | "
                f"selling {position_shares:.2f} shares @ {sell_price:.4f} | "
                f"market={self.market['question'][:40]}"
            )

            all_cancelled = True
            for oid in list(self.unwind_orders.keys()):
                if self.unwind_orders[oid].side == side:
                    if self.cancel_order(oid, reason="stop_loss"):
                        del self.unwind_orders[oid]
                    else:
                        all_cancelled = False
                        log.warning(
                            f"Stop-loss cancel failed for {oid[:16]}... — "
                            f"tokens may still be committed"
                        )

            if not all_cancelled:
                log.warning(
                    f"Skipping stop-loss sell — old orders still live on exchange"
                )
                continue

            self.place_unwind_order(
                side, avg_price, position_shares,
                clob_price_override=sell_price,
            )
            get_db().log_stop_loss(
                condition_id=condition_id, side=side,
                shares=position_shares, cost_price=our_cost,
                sell_price=sell_price, loss_usd=unrealized_loss_usd,
            )
            if self._reward_tracker and unrealized_loss_usd > 0:
                self._reward_tracker.record_stop_loss(
                    condition_id, unrealized_loss_usd
                )

    def _try_merge_positions(
        self, condition_id: str, amount: float
    ) -> bool:
        """Attempt to merge YES+NO token pairs back into USDC.

        On Polymarket, each YES+NO pair = $1 USDC. Merging avoids the
        need to sell both sides separately.

        Uses pre/post balance verification to guard against phantom merges
        (API returns success but exchange balance unchanged).
        """
        question = self.market["question"]
        yes_tid = self.market["token_ids"][0]

        try:
            merge_client = None
            if hasattr(self.client, 'merge_positions'):
                merge_client = self.client
            else:
                inner = getattr(self.client, '_client', None)
                if inner and hasattr(inner, 'merge_positions'):
                    merge_client = inner

            if merge_client is None:
                # No merge capability — fall back to manual alert
                now = _time.time()
                last_alert = getattr(self, '_last_merge_alert', 0)
                if now - last_alert > 1800:
                    self._last_merge_alert = now
                    yes_shares = self.position_tracker.get_shares(condition_id, "yes")
                    no_shares = self.position_tracker.get_shares(condition_id, "no")
                    alert_merge_needed(question, yes_shares, no_shares, amount, amount)
                log.warning(
                    f"MERGE NEEDED (manual) | {amount:.2f} YES+NO pairs | "
                    f"would free ${amount:.2f} USDC | "
                    f"market={question[:40]} | "
                    f"Use Polymarket UI to merge positions"
                )
                return False

            # ── Pre-merge balance snapshot ────────────────────────────
            pre_bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL, token_id=yes_tid
                )
            )
            pre_yes = float(pre_bal.get("balance", 0)) / 1e6

            # ── Execute merge ─────────────────────────────────────────
            result = merge_client.merge_positions(
                condition_id=condition_id,
                amount=amount,
            )

            # ── Post-merge balance verification ───────────────────────
            post_bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL, token_id=yes_tid
                )
            )
            post_yes = float(post_bal.get("balance", 0)) / 1e6

            if post_yes >= pre_yes - 0.5:
                log.critical(
                    f"PHANTOM MERGE: API returned but exchange YES balance unchanged "
                    f"(pre={pre_yes:.0f} post={post_yes:.0f}, expected -{amount:.0f}) | "
                    f"{question[:40]}"
                )
                return False

            log.info(
                f"MERGE SUCCESS | {amount:.2f} pairs merged | "
                f"freed=${amount:.2f} USDC | "
                f"market={question[:40]} | result={result}"
            )
            get_db().log_merge(condition_id, amount, amount)
            return True

        except Exception as e:
            log.error(
                f"Merge failed for {question[:40]}: {e} — "
                f"falling back to individual SELL orders"
            )
            return False

    def has_unhedged_position(self, side: str) -> bool:
        """Check if there's unhedged inventory on a side.

        Used by run_cycle to block new BUY orders when we have inventory
        that still needs unwinding.
        """
        condition_id = self.market["condition_id"]
        position_shares = self.position_tracker.get_shares(condition_id, side)

        if position_shares < MIN_UNWIND_SHARES:
            actual = self.verify_token_balance(side)
            if actual >= MIN_UNWIND_SHARES:
                log.info(
                    f"Blocking {side.upper()} BUY — exchange shows "
                    f"{actual:.2f} untracked shares"
                )
                return True
            return False

        covered = sum(
            u.size for u in self.unwind_orders.values() if u.side == side
        )
        return (position_shares - covered) >= MIN_UNWIND_SHARES
