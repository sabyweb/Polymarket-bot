"""
Order management for the Polymarket market-making bot.

Handles order placement, cancellation, fill detection, and the core
quoting strategy. Orders are placed behind a configurable liquidity
buffer to minimise adverse fill risk.
"""

import logging
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY, SELL
from config import (
    ORDER_SIZE, DANGER_ZONE_CENTS, DEAD_ZONE_BUFFER,
    MAX_ORDER_FAILURES, DRY_RUN, MAX_ORDERBOOK_SPREAD,
    MIN_LIQUIDITY_BUFFER,
)
from alerts import (
    alert_order_failure, alert_danger_zone,
    alert_fill, log_order_placed, log_order_cancelled,
)

log = logging.getLogger(__name__)


class OrderManager:
    """Manages order lifecycle for a single market.

    Args:
        client: Authenticated ClobClient instance.
        market: Dict of market metadata (condition_id, token_ids, etc.).
        position_tracker: PositionTracker instance for fill accounting.
    """

    def __init__(self, client: object, market: dict, position_tracker: object) -> None:
        self.client = client
        self.market = market
        self.position_tracker = position_tracker
        self.active_orders: dict[str, dict] = {}
        self.failure_counts: dict[str, int] = {"yes": 0, "no": 0}

    # ── Tick Size Rounding ───────────────────────────────────────────────────
    def round_to_tick(self, price: float) -> float:
        """Round a price to the nearest valid tick size for this market.

        Args:
            price: Raw price to round.

        Returns:
            Price snapped to the nearest tick boundary.
        """
        tick = self.market.get("tick_size", 0.01)
        if tick <= 0:
            tick = 0.01
        rounded = round(round(price / tick) * tick, 10)
        decimal_places = len(str(tick).rstrip("0").split(".")[-1])
        return round(rounded, decimal_places)

    # ── Order Book ───────────────────────────────────────────────────────────
    def get_order_book(self) -> object | None:
        """Fetch the live order book and validate it has a usable spread.

        Returns:
            The order_book object if usable, or None if the book is
            empty, one-sided, or has a spread wider than MAX_ORDERBOOK_SPREAD.
        """
        try:
            token_id = self.market["token_ids"][0]
            order_book = self.client.get_order_book(token_id)

            if not order_book.bids or not order_book.asks:
                log.warning(
                    f"Empty orderbook for "
                    f"{self.market['question'][:40]} — skipping cycle"
                )
                return None

            best_bid = float(order_book.bids[0].price)
            best_ask = float(order_book.asks[0].price)
            spread = best_ask - best_bid

            if spread > MAX_ORDERBOOK_SPREAD:
                log.warning(
                    f"Spread too wide ({spread:.4f}) for "
                    f"{self.market['question'][:40]} — skipping cycle"
                )
                return None

            return order_book

        except Exception as e:
            log.error(f"Failed to fetch order book: {e}")
            return None

    # ── Price Calculation ────────────────────────────────────────────────────
    def calculate_order_prices(
        self, order_book: object
    ) -> tuple[float | None, float | None]:
        """Walk the orderbook to place orders behind a liquidity buffer.

        We accumulate dollar volume from the top of each side of the book
        until we reach MIN_LIQUIDITY_BUFFER, then place our order one tick
        beyond that level.  This ensures that at least $1000 (by default)
        of existing orders must be filled before ours are reached.

        Args:
            order_book: Order book object with .bids and .asks lists.

        Returns:
            (our_bid, our_ask) or (None, None) if conditions are not met.
        """
        try:
            tick = self.market.get("tick_size", 0.01)
            max_spread = self.market["max_spread"]

            # Walk bids top-down: accumulate $ volume until buffer met
            our_bid = None
            cumulative = 0.0
            for level in order_book.bids:
                price = float(level.price)
                size = float(level.size)
                cumulative += price * size
                if cumulative >= MIN_LIQUIDITY_BUFFER:
                    our_bid = self.round_to_tick(price - tick)
                    break

            # Walk asks bottom-up: accumulate $ volume until buffer met
            our_ask = None
            cumulative = 0.0
            for level in order_book.asks:
                price = float(level.price)
                size = float(level.size)
                cumulative += price * size
                if cumulative >= MIN_LIQUIDITY_BUFFER:
                    our_ask = self.round_to_tick(price + tick)
                    break

            if our_bid is None or our_ask is None:
                log.warning(
                    f"Not enough liquidity buffer (need ${MIN_LIQUIDITY_BUFFER}) "
                    f"for {self.market['question'][:40]} — skipping"
                )
                return None, None

            # Verify we are still inside the reward window
            best_bid = float(order_book.bids[0].price)
            best_ask = float(order_book.asks[0].price)
            midpoint = (best_bid + best_ask) / 2

            if (abs(our_bid - midpoint) > max_spread
                    or abs(our_ask - midpoint) > max_spread):
                log.warning(
                    f"Orders would fall outside reward window "
                    f"(max_spread={max_spread}) — skipping"
                )
                return None, None

            # Safety clamps
            our_bid = max(0.01, min(our_bid, 0.98))
            our_ask = max(0.02, min(our_ask, 0.99))

            # If bid >= ask, something is fundamentally wrong — abort
            if our_bid >= our_ask:
                log.error(
                    f"CRITICAL: bid ({our_bid}) >= ask ({our_ask}) — "
                    f"aborting cycle for {self.market['question'][:40]}"
                )
                return None, None

            log.debug(
                f"Prices | midpoint={midpoint:.4f} | "
                f"bid={our_bid:.4f} | ask={our_ask:.4f}"
            )
            return our_bid, our_ask

        except Exception as e:
            log.error(f"Price calculation failed: {e}")
            return None, None

    # ── Zone Checking ────────────────────────────────────────────────────────
    def check_order_zone(
        self, order_id: str, best_bid: float, best_ask: float
    ) -> str:
        """Check which zone an order occupies relative to the midpoint.

        Zones:
            DANGER — too close to midpoint (high fill risk).
            REWARD — inside the max spread window (earning rewards).
            DEAD   — outside max spread window (earning nothing).

        Args:
            order_id: Exchange order identifier.
            best_bid: Current best bid on the book.
            best_ask: Current best ask on the book.

        Returns:
            One of "DANGER", "REWARD", "DEAD", or "UNKNOWN".
        """
        if order_id not in self.active_orders:
            return "UNKNOWN"

        order = self.active_orders[order_id]
        price = order["price"]
        side = order["side"]
        max_spread = self.market["max_spread"]

        midpoint = self.market["yes_price"]
        if midpoint is None:
            midpoint = (best_bid + best_ask) / 2

        gap = abs(price - midpoint)

        if gap < DANGER_ZONE_CENTS:
            alert_danger_zone(
                self.market["question"], side.upper(), price, midpoint
            )
            return "DANGER"

        if gap > max_spread + DEAD_ZONE_BUFFER:
            return "DEAD"

        return "REWARD"

    # ── Order Placement ──────────────────────────────────────────────────────
    def place_order(
        self, side: str, price: float, size: float | None = None
    ) -> str | None:
        """Place a single limit order on one side.

        Args:
            side: "yes" or "no".
            price: Limit price for the order.
            size: Number of shares (auto-calculated if None).

        Returns:
            Order ID string, or None if placement failed or was skipped.
        """
        condition_id = self.market["condition_id"]
        question = self.market["question"]

        # Calculate order size in shares
        yes_price = self.market["yes_price"]
        if yes_price is None or yes_price <= 0:
            log.warning(
                f"No valid yes_price for {question[:40]} — "
                f"cannot calculate order size, skipping"
            )
            return None

        min_shares = self.market["min_size"]
        budget_shares = ORDER_SIZE / yes_price
        size = size or max(min_shares, budget_shares)
        size = round(size, 2)

        # Hard cap — limit cost to ORDER_SIZE * 1.5 USD
        # e.g. $150 when ORDER_SIZE=$100
        max_shares = (ORDER_SIZE * 1.5) / yes_price
        if size > max_shares:
            log.debug(
                f"Size capped from {size} to {max_shares:.2f} shares "
                f"(hard cap at ${ORDER_SIZE * 1.5:.0f})"
            )
            size = round(max_shares, 2)

        log.debug(
            f"Order size | min_shares={min_shares} | "
            f"budget_shares={budget_shares:.1f} | "
            f"final_size={size} | "
            f"est_cost=${size * yes_price:.2f}"
        )

        # Gate: check position limit before placing
        if not self.position_tracker.can_quote(condition_id, side):
            log.debug(f"Quoting halted on {side.upper()} — skipping")
            return None

        token_id = self.market["token_ids"][0]
        clob_side = BUY if side == "yes" else SELL

        # ── Dry Run ──────────────────────────────────────────────────────────
        if DRY_RUN:
            dry_id = f"DRY-{side.upper()}-{int(price * 10000)}"
            self.active_orders[dry_id] = {
                "side": side,
                "price": price,
                "size": float(size),
                "original_size": float(size),
            }
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
                price=price,
                size=float(size),
                side=clob_side,
            )
            response = self.client.create_and_post_order(order_args)
            order_id = response.orderID

            self.active_orders[order_id] = {
                "side": side,
                "price": price,
                "size": float(size),
                "original_size": float(size),
            }

            self.failure_counts[side] = 0
            log_order_placed(side.upper(), price, size, question, order_id)
            return order_id

        except Exception as e:
            self.failure_counts[side] += 1
            alert_order_failure(
                question, side.upper(), str(e),
                self.failure_counts[side],
            )
            return None

    def place_both_sides(
        self, our_bid: float, our_ask: float
    ) -> tuple[str | None, str | None]:
        """Place orders on both Yes and No sides.

        Args:
            our_bid: Price for the Yes (bid) side.
            our_ask: Price for the No (ask) side.

        Returns:
            Tuple of (yes_order_id, no_order_id).
        """
        yes_id = self.place_order("yes", our_bid)
        no_id = self.place_order("no", our_ask)
        return yes_id, no_id

    # ── Order Cancellation ───────────────────────────────────────────────────
    def cancel_order(self, order_id: str, reason: str = "manual") -> None:
        """Cancel a single order.

        Args:
            order_id: Exchange order identifier.
            reason: Human-readable cancellation reason for logging.
        """
        if DRY_RUN:
            log.info(
                f"[DRY RUN] Would cancel | "
                f"id={order_id} | reason={reason}"
            )
            if order_id in self.active_orders:
                del self.active_orders[order_id]
            return

        try:
            self.client.cancel(order_id)
            log_order_cancelled(order_id, reason)
            if order_id in self.active_orders:
                del self.active_orders[order_id]
        except Exception as e:
            log.error(f"Failed to cancel order {order_id}: {e}")

    def cancel_all(self, reason: str = "refresh") -> None:
        """Cancel all active orders for this market.

        Args:
            reason: Human-readable reason for logging.
        """
        if not self.active_orders:
            return
        for order_id in list(self.active_orders.keys()):
            self.cancel_order(order_id, reason)
        log.info(f"Cancelled all for {self.market['question'][:40]}")

    # ── Fill Detection ───────────────────────────────────────────────────────
    def detect_fills(self) -> None:
        """Compare tracked orders against open orders on exchange.

        Detects both full fills (order disappeared) and partial fills
        (remaining size smaller than original size).  Skipped in dry-run.
        """
        if not self.active_orders or DRY_RUN:
            return

        try:
            open_orders = self.client.get_orders()
            open_map: dict[str, object] = {}
            if open_orders:
                open_map = {o.id: o for o in open_orders}

            for oid in list(self.active_orders.keys()):
                order = self.active_orders[oid]
                side = order["side"]

                if oid not in open_map:
                    # Full fill — order no longer on exchange
                    filled_usd = order["price"] * order["original_size"]
                    log.info(
                        f"FILL (FULL) | {side.upper()} | "
                        f"price={order['price']:.4f} | "
                        f"value=${filled_usd:.2f} | "
                        f"market={self.market['question'][:40]}"
                    )
                    alert_fill(
                        fill_type="FULL",
                        side=side.upper(),
                        price=order["price"],
                        filled_shares=order["original_size"],
                        filled_usd=filled_usd,
                        market_question=self.market["question"],
                    )
                    self.position_tracker.record_fill(
                        self.market["condition_id"], side, filled_usd
                    )
                    del self.active_orders[oid]
                else:
                    # Check for partial fill
                    remaining = float(open_map[oid].size)
                    original = order["original_size"]
                    if remaining < original:
                        filled_shares = original - remaining
                        filled_usd = order["price"] * filled_shares
                        log.info(
                            f"FILL (PARTIAL) | {side.upper()} | "
                            f"filled={filled_shares:.2f} shares | "
                            f"remaining={remaining:.2f} | "
                            f"value=${filled_usd:.2f} | "
                            f"market={self.market['question'][:40]}"
                        )
                        alert_fill(
                            fill_type="PARTIAL",
                            side=side.upper(),
                            price=order["price"],
                            filled_shares=filled_shares,
                            filled_usd=filled_usd,
                            market_question=self.market["question"],
                            remaining_shares=remaining,
                        )
                        self.position_tracker.record_fill(
                            self.market["condition_id"], side, filled_usd
                        )
                        # Update tracked size to current remaining
                        self.active_orders[oid]["size"] = remaining
                        self.active_orders[oid]["original_size"] = remaining

        except Exception as e:
            log.error(f"Fill detection error: {e}")

    # ── Full Cycle ───────────────────────────────────────────────────────────
    def run_cycle(self) -> None:
        """Run one complete order management cycle.

        Steps:
            1. Detect any fills (full or partial).
            2. Fetch and validate the order book.
            3. Check zones — cancel DANGER and DEAD orders.
            4. Calculate prices using the liquidity-buffer strategy.
            5. Place fresh orders where needed.
        """
        question = self.market["question"]
        log.debug(f"Running cycle for: {question[:50]}")

        # Step 1: Detect fills
        self.detect_fills()

        # Step 2: Fetch and validate order book
        order_book = self.get_order_book()
        if order_book is None:
            return

        best_bid = float(order_book.bids[0].price)
        best_ask = float(order_book.asks[0].price)

        log.info(
            f"Market: {question[:45]} | "
            f"bid={best_bid:.4f} | ask={best_ask:.4f}"
        )

        # Step 3: Check zones and cancel bad orders
        for order_id in list(self.active_orders.keys()):
            zone = self.check_order_zone(order_id, best_bid, best_ask)
            if zone in ("DANGER", "DEAD"):
                self.cancel_order(order_id, reason=zone.lower())

        # Step 4: Calculate prices using liquidity buffer
        our_bid, our_ask = self.calculate_order_prices(order_book)
        if our_bid is None:
            return

        # Step 5: Place fresh orders where needed
        active_sides = {o["side"] for o in self.active_orders.values()}
        needs_yes = "yes" not in active_sides
        needs_no = "no" not in active_sides

        if not needs_yes and not needs_no:
            log.debug("Both sides active — holding")
            return

        if needs_yes:
            self.place_order("yes", our_bid)
        if needs_no:
            self.place_order("no", our_ask)
