"""
Fill detection logic for the Polymarket market-making bot.

Detects full and partial fills for both BUY and SELL (unwind) orders
by comparing tracked orders against the exchange order list.
"""

import logging
import time as _time
from config import DRY_RUN
from alerts import alert_fill, alert_unwind
from price import to_clob
from database import get_db

log = logging.getLogger(__name__)


class FillsMixin:
    """Mixin providing fill detection methods for OrderManager."""

    def _get_order_status(self, order_id: str) -> str:
        """Query the exchange for an individual order's status.

        Uses GET /data/order/{order_id} which returns the order with a
        'status' field: "MATCHED" for fills, "CANCELLED" for cancels, etc.

        Args:
            order_id: Exchange order identifier.

        Returns:
            Status string (e.g. "MATCHED", "CANCELLED"), or "UNKNOWN".
        """
        try:
            order_data = self.client.get_order(order_id)
            if isinstance(order_data, dict):
                return order_data.get("status", "UNKNOWN")
            return "UNKNOWN"
        except Exception as e:
            log.debug(f"Could not fetch status for order {order_id[:16]}...: {e}")
            return "UNKNOWN"

    def detect_fills(self, exchange_orders: list[dict] | None = None) -> None:
        """Compare tracked orders against open orders on exchange.

        When an order disappears from the exchange order list, we verify
        its status using get_order(id).  Only orders with status "MATCHED"
        are treated as fills — everything else (CANCELLED, expired, etc.)
        is silently removed from tracking.

        Also adopts any untracked exchange orders belonging to this market
        (replaces the old resync block in run_cycle).

        Args:
            exchange_orders: Pre-fetched list from get_orders() (passed by
                bot.py to avoid per-manager API calls). Falls back to
                fetching directly if None.
        """
        if DRY_RUN:
            return

        try:
            # Build map of this market's exchange orders
            market_tokens = set(self.market["token_ids"])
            if exchange_orders is None:
                exchange_orders = self.client.get_orders() or []
            open_map: dict[str, dict] = {
                o["id"]: o for o in exchange_orders
                if o.get("asset_id") in market_tokens
            }

            # ── Adopt untracked exchange orders ─────────────────────────
            # If the exchange has orders for our market that we're not
            # tracking, adopt them. This catches orders where the POST
            # response ID didn't match the exchange ID, or orders placed
            # by a previous session that weren't cancelled.
            yes_token = self.market["token_ids"][0]
            for oid, o in open_map.items():
                if oid in self.active_orders or oid in self.unwind_orders:
                    continue
                o_price = float(o.get("price", 0))
                o_size = float(o.get("original_size", o.get("size", 0)))
                mapped_side = "yes" if o.get("asset_id") == yes_token else "no"
                log.warning(
                    f"ADOPT | Found untracked {mapped_side.upper()} order "
                    f"on exchange: {oid[:16]}... @ {o_price:.4f} / "
                    f"{o_size:.1f} shares — adopting into tracker"
                )
                from order_manager import TrackedOrder
                self.active_orders[oid] = TrackedOrder(
                    side=mapped_side,
                    price=o_price,
                    size=o_size,
                    original_size=o_size,
                    placed_at=_time.time(),
                )

            if not self.active_orders and not self.unwind_orders:
                return

            log.debug(
                f"Fill check | tracked={len(self.active_orders)} | "
                f"on_exchange={len(open_map)} | "
                f"market={self.market['question'][:30]}"
            )

            for oid in list(self.active_orders.keys()):
                order = self.active_orders[oid]

                if oid not in open_map:
                    # Order gone — check WHY via individual status query
                    status = self._get_order_status(oid)

                    if status == "MATCHED":
                        # CONFIRMED fill — record position only;
                        # reconcile_unwinds() handles unwind placement
                        try:
                            filled_shares = order.original_size
                            # order.price is YES-equivalent for BOTH sides.
                            # Actual cost: YES = price, NO = 1-price (CLOB cost).
                            clob_cost = to_clob(order.price, order.side)
                            filled_usd = clob_cost * filled_shares

                            # M3: Fill quality — compare fill price to EMA midpoint
                            ema_mid = getattr(self, "_ema_mid", 0)
                            if ema_mid > 0 and getattr(self, "_ema_initialized", False):
                                # For BUY: slippage > 0 means we overpaid (adverse)
                                # For YES: clob_cost is the YES price, compare to midpoint
                                # For NO: clob_cost is the NO price (1-yes_equiv),
                                #   midpoint equivalent = 1 - ema_mid
                                if order.side == "yes":
                                    fill_midpoint = ema_mid
                                    slippage = clob_cost - fill_midpoint
                                else:
                                    fill_midpoint = 1.0 - ema_mid
                                    slippage = clob_cost - fill_midpoint
                            else:
                                fill_midpoint = 0.0
                                slippage = 0.0

                            slip_label = "ADVERSE" if slippage > 0 else "FAVOURABLE"
                            log.info(
                                f"FILL (FULL) | {order.side.upper()} | "
                                f"price={clob_cost:.4f} | "
                                f"shares={filled_shares:.2f} | "
                                f"value=${filled_usd:.2f} | "
                                f"mid={fill_midpoint:.4f} | "
                                f"slip={slippage:+.4f} ({slip_label}) | "
                                f"market={self.market['question'][:40]}"
                            )
                            alert_fill(
                                fill_type="FULL",
                                side=order.side.upper(),
                                price=clob_cost,
                                filled_shares=filled_shares,
                                filled_usd=filled_usd,
                                market_question=self.market["question"],
                            )
                            self.position_tracker.record_fill(
                                self.market["condition_id"], order.side,
                                filled_shares, order.price,
                                question=self.market["question"],
                            )
                            self._last_fill_time[order.side] = _time.time()
                            # Record to reward tracker
                            if self._reward_tracker:
                                self._reward_tracker.record_buy_fill(
                                    self.market["condition_id"],
                                    filled_shares, filled_usd,
                                )
                                # M3: Record fill quality
                                if fill_midpoint > 0:
                                    self._reward_tracker.record_fill_quality(
                                        self.market["condition_id"], slippage,
                                    )
                            # Record to history database with enrichment context
                            _cid = self.market["condition_id"]
                            _order_age = _time.time() - order.placed_at
                            _pos_after = self.position_tracker.get_position(
                                _cid, order.side
                            )
                            _rr = getattr(self, "_get_reward_rate", lambda: 0.0)()
                            get_db().log_fill(
                                condition_id=_cid,
                                question=self.market["question"],
                                side=order.side, fill_type="FULL",
                                shares=filled_shares, price=order.price,
                                clob_cost=clob_cost, usd_value=filled_usd,
                                midpoint=fill_midpoint, slippage=slippage,
                                order_age_secs=_order_age,
                                position_usd_after=_pos_after,
                                reward_rate_hr=_rr,
                            )
                        except Exception as e:
                            log.error(
                                f"Error processing fill for {oid[:16]}... "
                                f"({order.side.upper()}): {e} — continuing to "
                                f"next order"
                            )
                    else:
                        # NOT a fill — cancelled, expired, or unknown
                        log.info(
                            f"Order {oid[:16]}... removed (status={status}) "
                            f"— NOT a fill | {order.side.upper()} | "
                            f"market={self.market['question'][:40]}"
                        )

                    del self.active_orders[oid]
                else:
                    # Order still on exchange — check for partial fill
                    exchange_order = open_map[oid]
                    orig = float(exchange_order["original_size"])
                    matched = float(exchange_order["size_matched"])
                    remaining = orig - matched
                    if remaining < order.original_size:
                        filled_shares = order.original_size - remaining
                        # order.price is YES-equivalent for BOTH sides.
                        clob_cost = to_clob(order.price, order.side)
                        filled_usd = clob_cost * filled_shares

                        # M3: Fill quality for partial fills
                        ema_mid = getattr(self, "_ema_mid", 0)
                        if ema_mid > 0 and getattr(self, "_ema_initialized", False):
                            if order.side == "yes":
                                fill_midpoint = ema_mid
                                slippage = clob_cost - fill_midpoint
                            else:
                                fill_midpoint = 1.0 - ema_mid
                                slippage = clob_cost - fill_midpoint
                        else:
                            fill_midpoint = 0.0
                            slippage = 0.0

                        slip_label = "ADVERSE" if slippage > 0 else "FAVOURABLE"
                        log.info(
                            f"FILL (PARTIAL) | {order.side.upper()} | "
                            f"filled={filled_shares:.2f} shares | "
                            f"remaining={remaining:.2f} | "
                            f"value=${filled_usd:.2f} | "
                            f"mid={fill_midpoint:.4f} | "
                            f"slip={slippage:+.4f} ({slip_label}) | "
                            f"market={self.market['question'][:40]}"
                        )
                        alert_fill(
                            fill_type="PARTIAL",
                            side=order.side.upper(),
                            price=clob_cost,
                            filled_shares=filled_shares,
                            filled_usd=filled_usd,
                            market_question=self.market["question"],
                            remaining_shares=remaining,
                        )
                        self.position_tracker.record_fill(
                            self.market["condition_id"], order.side,
                            filled_shares, order.price,
                            question=self.market["question"],
                        )
                        self._last_fill_time[order.side] = _time.time()
                        # Record to reward tracker
                        if self._reward_tracker:
                            self._reward_tracker.record_buy_fill(
                                self.market["condition_id"],
                                filled_shares, filled_usd,
                            )
                            # M3: Record fill quality
                            if fill_midpoint > 0:
                                self._reward_tracker.record_fill_quality(
                                    self.market["condition_id"], slippage,
                                )
                        # Record to history database with enrichment context
                        _cid = self.market["condition_id"]
                        _order_age = _time.time() - order.placed_at
                        _pos_after = self.position_tracker.get_position(
                            _cid, order.side
                        )
                        _rr = getattr(self, "_get_reward_rate", lambda: 0.0)()
                        get_db().log_fill(
                            condition_id=_cid,
                            question=self.market["question"],
                            side=order.side, fill_type="PARTIAL",
                            shares=filled_shares, price=order.price,
                            clob_cost=clob_cost, usd_value=filled_usd,
                            midpoint=fill_midpoint, slippage=slippage,
                            order_age_secs=_order_age,
                            position_usd_after=_pos_after,
                            reward_rate_hr=_rr,
                        )
                        # Update tracked size so next partial detection
                        # only captures the NEW delta, not the same fill.
                        self.active_orders[oid].size = remaining
                        self.active_orders[oid].original_size = remaining

            # ── Check unwind (SELL) orders ────────────────────────────────
            if self.unwind_orders:
                # Use all exchange orders (not just this market's) for unwind
                # tracking — unwind order IDs may not match market token filter
                full_open_map = {o["id"]: o for o in exchange_orders}

                for oid in list(self.unwind_orders.keys()):
                    uorder = self.unwind_orders[oid]

                    # Grace period: don't check orders < 90s old
                    age = _time.time() - uorder.placed_at
                    if age < 90:
                        log.debug(
                            f"Skipping unwind check for {oid[:16]}... "
                            f"(age={age:.0f}s < 90s)"
                        )
                        continue

                    if oid not in full_open_map:
                        status = self._get_order_status(oid)

                        if status == "MATCHED":
                            unwound_shares = uorder.size
                            # uorder.price is YES-equivalent; show actual CLOB price
                            clob_sell = to_clob(uorder.price, uorder.side)
                            unwound_usd = clob_sell * unwound_shares
                            log.info(
                                f"INVENTORY UNWOUND | {uorder.side.upper()} | "
                                f"price={clob_sell:.4f} | "
                                f"size={unwound_shares:.2f} | "
                                f"value=${unwound_usd:.2f} | "
                                f"market={self.market['question'][:40]}"
                            )
                            alert_unwind(
                                side=uorder.side.upper(),
                                price=clob_sell,
                                size=unwound_shares,
                                usd_value=unwound_usd,
                                market_question=self.market["question"],
                            )
                            # Compute VWAP cost BEFORE record_unwind (which may zero avg_price)
                            cid = self.market["condition_id"]
                            avg_p = self.position_tracker.get_avg_price(cid, uorder.side)
                            vwap_cost = 0.0
                            if avg_p > 0:
                                vwap_cost = to_clob(avg_p, uorder.side) * unwound_shares
                            self.position_tracker.record_unwind(
                                self.market["condition_id"], uorder.side,
                                unwound_shares, uorder.price
                            )
                            # Record sell fill to reward tracker with VWAP cost
                            if self._reward_tracker:
                                self._reward_tracker.record_sell_fill(
                                    cid, unwound_shares, unwound_usd,
                                    vwap_cost_usd=vwap_cost,
                                )
                            # Record to history database with hold context
                            _hold_secs = _time.time() - uorder.placed_at
                            _rr = getattr(self, "_get_reward_rate", lambda: 0.0)()
                            _reward_est = _rr * (_hold_secs / 3600)
                            get_db().log_unwind(
                                condition_id=cid,
                                question=self.market["question"],
                                side=uorder.side, shares=unwound_shares,
                                sell_price=clob_sell,
                                usd_value=unwound_usd,
                                vwap_cost=vwap_cost,
                                hold_duration_secs=_hold_secs,
                                unwind_type="FULL",
                                reward_earned_est=_reward_est,
                            )
                            del self.unwind_orders[oid]
                        elif status == "UNKNOWN":
                            # API error — keep tracking, count consecutive failures
                            uorder.unknown_count += 1
                            if uorder.unknown_count >= 5:
                                log.warning(
                                    f"Unwind order {oid[:16]}... status UNKNOWN "
                                    f"for {uorder.unknown_count} consecutive "
                                    f"checks — removing from tracking | "
                                    f"{uorder.side.upper()} | "
                                    f"market={self.market['question'][:40]}"
                                )
                                del self.unwind_orders[oid]
                            else:
                                log.info(
                                    f"Unwind order {oid[:16]}... status UNKNOWN "
                                    f"(#{uorder.unknown_count}/5) — keeping | "
                                    f"{uorder.side.upper()}"
                                )
                        else:
                            # Definitive non-fill status (CANCELLED, INVALID, etc.)
                            # reconcile_unwinds will handle replacement next cycle
                            log.warning(
                                f"Unwind order {oid[:16]}... gone "
                                f"(status={status}) | {uorder.side.upper()} | "
                                f"market={self.market['question'][:40]} — "
                                f"will be reconciled next cycle"
                            )
                            del self.unwind_orders[oid]
                    else:
                        # Order found on exchange — check for partial unwind fill
                        uorder.unknown_count = 0
                        exch = full_open_map[oid]
                        u_orig = float(exch["original_size"])
                        u_matched = float(exch["size_matched"])
                        u_remaining = u_orig - u_matched
                        if u_remaining < uorder.size - 0.01:
                            unwound_shares = uorder.size - u_remaining
                            clob_sell = to_clob(uorder.price, uorder.side)
                            unwound_usd = clob_sell * unwound_shares
                            log.info(
                                f"UNWIND (PARTIAL) | {uorder.side.upper()} | "
                                f"sold={unwound_shares:.2f} shares | "
                                f"remaining={u_remaining:.2f} | "
                                f"value=${unwound_usd:.2f} | "
                                f"market={self.market['question'][:40]}"
                            )
                            # Compute VWAP cost BEFORE record_unwind (which may zero avg_price)
                            cid = self.market["condition_id"]
                            avg_p = self.position_tracker.get_avg_price(
                                cid, uorder.side
                            )
                            vwap_cost = 0.0
                            if avg_p > 0:
                                vwap_cost = (
                                    to_clob(avg_p, uorder.side)
                                    * unwound_shares
                                )
                            self.position_tracker.record_unwind(
                                self.market["condition_id"], uorder.side,
                                unwound_shares, uorder.price
                            )
                            # Record to reward tracker (was missing — Bug #1)
                            if self._reward_tracker:
                                self._reward_tracker.record_sell_fill(
                                    cid, unwound_shares, unwound_usd,
                                    vwap_cost_usd=vwap_cost,
                                )
                            # Record to history database with hold context
                            _hold_secs = _time.time() - uorder.placed_at
                            _rr = getattr(self, "_get_reward_rate", lambda: 0.0)()
                            _reward_est = _rr * (_hold_secs / 3600)
                            get_db().log_unwind(
                                condition_id=cid,
                                question=self.market["question"],
                                side=uorder.side,
                                shares=unwound_shares,
                                sell_price=clob_sell,
                                usd_value=unwound_usd,
                                vwap_cost=vwap_cost,
                                hold_duration_secs=_hold_secs,
                                unwind_type="PARTIAL",
                                reward_earned_est=_reward_est,
                            )
                            uorder.size = u_remaining

        except Exception as e:
            log.error(f"Fill detection error: {e}")
