"""
Order placement logic for the Polymarket market-making bot.

Handles BUY order placement with balance gating, sizing, and
dry-run support.
"""

import logging
import time as _time
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY
import config
from config import DRY_RUN  # truly immutable at runtime
from alerts import alert_order_failure, log_order_placed
from price import to_clob
from database import get_db

log = logging.getLogger(__name__)


class PlacementMixin:
    """Mixin providing order placement methods for OrderManager."""

    def place_order(
        self, side: str, price: float, size: float | None = None,
        budget_usd: float | None = None,
    ) -> str | None:
        """Place a single limit order on one side.

        Args:
            side: "yes" or "no".
            price: Limit price for the order.
            size: Number of shares (auto-calculated if None).
            budget_usd: Target dollar budget for the order.
                If None, uses static ORDER_SIZE.
                Used by M4 dynamic sizing to override the flat default.

        Returns:
            Order ID string, or None if placement failed or was skipped.
        """
        condition_id = self.market["condition_id"]
        question = self.market["question"]

        # In Neg Risk markets:
        #   YES side → BUY the YES token at our bid price
        #   NO side  → BUY the NO token at (1 - our ask price)
        # We never SELL tokens we don't own.
        if side == "yes":
            token_id = self.market["token_ids"][0]
            clob_side = BUY
            clob_price = price
        else:
            token_id = self.market["token_ids"][1]
            clob_side = BUY
            clob_price = self.round_to_tick(to_clob(price, "no"))

        # Calculate order size based on actual token cost (clob_price),
        # not yes_price.  For YES side clob_price == bid price; for NO
        # side clob_price == (1 - ask_price), which can be very different.
        if clob_price is None or clob_price <= 0:
            log.warning(
                f"Invalid clob_price ({clob_price}) for {side.upper()} "
                f"on {question[:40]} — skipping"
            )
            return None

        min_shares = self.market["min_size"]
        # Scale down order size for cheap tokens to limit adverse selection damage
        base_order_size = budget_usd if budget_usd is not None else config.ORDER_SIZE
        effective_order_size = base_order_size
        if clob_price < config.CHEAP_TOKEN_THRESHOLD:
            effective_order_size = base_order_size * config.CHEAP_TOKEN_SCALE
        budget_shares = effective_order_size / clob_price
        min_cost = min_shares * clob_price

        # If the rewards minimum exceeds our hard cap, skip this side
        if min_cost > config.MAX_ORDER_BUDGET:
            log.warning(
                f"Min order ({min_shares} shares × ${clob_price:.2f} "
                f"= ${min_cost:.0f}) exceeds hard cap "
                f"${config.MAX_ORDER_BUDGET} — skipping {side.upper()}"
            )
            return None

        # Always place at least min_shares to qualify for rewards.
        # Use ORDER_SIZE as preferred budget, but allow up to
        # MAX_ORDER_BUDGET if the rewards minimum demands it.
        if size is None:
            size = max(min_shares, budget_shares)
        size = round(size, 2)

        # Hard cap — never exceed MAX_ORDER_BUDGET
        max_shares = config.MAX_ORDER_BUDGET / clob_price
        if size > max_shares:
            log.debug(
                f"Size capped from {size} to {max_shares:.2f} shares "
                f"(hard cap at ${config.MAX_ORDER_BUDGET})"
            )
            size = round(max_shares, 2)

        est_cost = size * clob_price
        log.info(
            f"Order size | {side.upper()} | clob_price={clob_price:.4f} | "
            f"min_shares={min_shares} | budget_shares={budget_shares:.1f} | "
            f"final_size={size} | est_cost=${est_cost:.2f}"
            + (f" (above target ${effective_order_size:.0f}, needed for rewards min)"
               if est_cost > effective_order_size else "")
        )

        # Gate: check position limit before placing
        if not self.position_tracker.can_quote(condition_id, side):
            log.debug(f"Quoting halted on {side.upper()} — skipping")
            return None

        # Gate: check USDC balance before placing — avoid doomed API calls.
        # Uses shared BalanceGate if available (cross-manager awareness),
        # falls back to per-manager cache otherwise.
        if self.balance_gate:
            if not self.balance_gate.can_afford(est_cost):
                if self.balance_gate.is_depleted:
                    log.debug(
                        f"Balance gate depleted — skipping {side.upper()} on "
                        f"{question[:40]}"
                    )
                else:
                    log.warning(
                        f"Insufficient balance for ${est_cost:.2f} — "
                        f"skipping {side.upper()} on {question[:40]}"
                    )
                return None
        else:
            available = self._get_cached_balance()
            if available is not None and est_cost > available:
                log.warning(
                    f"Insufficient balance: need ${est_cost:.2f} but only "
                    f"${available:.2f} available — skipping {side.upper()} on "
                    f"{question[:40]}"
                )
                return None

        # ── Dry Run ──────────────────────────────────────────────────────────
        if DRY_RUN:
            from order_manager import TrackedOrder
            dry_id = f"DRY-{side.upper()}-{int(price * 10000)}"
            self.active_orders[dry_id] = TrackedOrder(
                side=side,
                price=price,
                size=float(size),
                original_size=float(size),
                placed_at=_time.time(),
            )
            log.info(
                f"[DRY RUN] Would place {side.upper()} | "
                f"price={price:.4f} | size={size} | "
                f"market={question[:40]}"
            )
            return dry_id

        # ── Live Trading ─────────────────────────────────────────────────────
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=clob_price,
                size=float(size),
                side=clob_side,
            )
            response = self.client.create_and_post_order(order_args)

            # Check the POST response for success
            if isinstance(response, dict):
                if not response.get("success", True):
                    raise Exception(
                        f"Order rejected: {response.get('errorMsg', response)}"
                    )

            # Use POST response orderID directly — no exchange lookup needed.
            # detect_fills() will adopt the correct exchange ID on the next
            # cycle when it reconciles tracked vs exchange orders.
            order_id = None
            if isinstance(response, dict):
                order_id = response.get("orderID")

            if order_id:
                from order_manager import TrackedOrder
                self.active_orders[order_id] = TrackedOrder(
                    side=side,
                    price=price,
                    size=float(size),
                    original_size=float(size),
                    placed_at=_time.time(),
                )
                self.failure_counts[side] = 0
                self.invalidate_balance_cache()
                if self._reward_tracker:
                    self._reward_tracker.record_order_placed(
                        self.market["condition_id"]
                    )
                log_order_placed(side.upper(), price, size, question, order_id)
                # Record to history database
                get_db().log_order_placed(
                    condition_id=condition_id, side=side,
                    price=price, size=float(size),
                    order_id=order_id, order_type="BUY",
                )
                return order_id
            else:
                log.error(
                    f"Order placed but POST response returned no ID "
                    f"for {side.upper()} at {price:.4f} on "
                    f"{question[:40]} — order is UNTRACKED"
                )
                self.failure_counts[side] = 0
                return None

        except Exception as e:
            self.failure_counts[side] += 1
            error_str = str(e).lower()

            # If the exchange says "not enough balance", mark the shared
            # gate as depleted so ALL managers skip orders this cycle
            # instead of each one hammering the API with doomed requests.
            if "not enough balance" in error_str or "allowance" in error_str:
                if self.balance_gate:
                    self.balance_gate.mark_depleted(cooldown_secs=config.ORDER_REFRESH_SECS)

            alert_order_failure(
                question, side.upper(), str(e),
                self.failure_counts[side],
            )
            return None

