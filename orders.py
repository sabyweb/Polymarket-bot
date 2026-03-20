"""
Order management for the Polymarket market-making bot.

Handles order placement, cancellation, fill detection, and the core
quoting strategy. Orders are placed behind a configurable liquidity
buffer to minimise adverse fill risk.
"""

import logging
import time as _time
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY
from config import (
    ORDER_SIZE, MAX_ORDER_BUDGET, DANGER_ZONE_CENTS, DEAD_ZONE_BUFFER,
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
    def get_order_book(self) -> dict | None:
        """Fetch and merge the YES + NO order books into one combined view.

        In Neg Risk markets (most Polymarket markets), buying YES at
        price P is equivalent to selling NO at (1-P).  The Polymarket UI
        shows a merged view; the raw CLOB API returns separate books.

        This method combines both books so we see the *real* spread:
        - Combined bids = raw YES bids + derived bids from NO asks
        - Combined asks = raw YES asks + derived asks from NO bids

        Returns:
            A dict with 'bids' and 'asks' lists (each entry is a dict
            with 'price' and 'size' keys), sorted best-first.
            Returns None if the book is unusable.
        """
        try:
            yes_token = self.market["token_ids"][0]
            ob_yes = self.client.get_order_book(yes_token)

            # Start with raw YES book
            all_bids: list[tuple[float, float]] = []
            all_asks: list[tuple[float, float]] = []

            for b in ob_yes.bids:
                all_bids.append((float(b.price), float(b.size)))
            for a in ob_yes.asks:
                all_asks.append((float(a.price), float(a.size)))

            # Merge NO book if available (Neg Risk complement)
            if len(self.market["token_ids"]) > 1:
                no_token = self.market["token_ids"][1]
                ob_no = self.client.get_order_book(no_token)

                # NO asks → derived YES bids (price = 1 - NO_ask_price)
                for a in ob_no.asks:
                    derived_price = round(1 - float(a.price), 4)
                    if derived_price > 0:
                        all_bids.append((derived_price, float(a.size)))

                # NO bids → derived YES asks (price = 1 - NO_bid_price)
                for b in ob_no.bids:
                    derived_price = round(1 - float(b.price), 4)
                    if derived_price < 1:
                        all_asks.append((derived_price, float(b.size)))

            # Aggregate by price level (sum sizes at same price)
            bid_map: dict[float, float] = {}
            for price, size in all_bids:
                bid_map[price] = bid_map.get(price, 0) + size
            ask_map: dict[float, float] = {}
            for price, size in all_asks:
                ask_map[price] = ask_map.get(price, 0) + size

            # Sort: bids highest-first, asks lowest-first
            sorted_bids = sorted(bid_map.items(), key=lambda x: -x[0])
            sorted_asks = sorted(ask_map.items(), key=lambda x: x[0])

            if not sorted_bids or not sorted_asks:
                log.warning(
                    f"Empty combined orderbook for "
                    f"{self.market['question'][:40]} — skipping cycle"
                )
                return None

            best_bid = sorted_bids[0][0]
            best_ask = sorted_asks[0][0]
            spread = best_ask - best_bid

            if spread > MAX_ORDERBOOK_SPREAD:
                log.warning(
                    f"Spread too wide ({spread:.4f}) for "
                    f"{self.market['question'][:40]} — skipping cycle"
                )
                return None

            # Return as a dict with list-of-dicts structure
            combined = {
                "bids": [{"price": p, "size": s} for p, s in sorted_bids],
                "asks": [{"price": p, "size": s} for p, s in sorted_asks],
            }

            log.debug(
                f"Combined book | best_bid={best_bid:.4f} | "
                f"best_ask={best_ask:.4f} | spread={spread:.4f} | "
                f"{len(sorted_bids)} bid levels, {len(sorted_asks)} ask levels"
            )

            return combined

        except Exception as e:
            log.error(f"Failed to fetch order book: {e}")
            return None

    # ── Price Calculation ────────────────────────────────────────────────────
    def calculate_order_prices(
        self, order_book: dict
    ) -> tuple[float | None, float | None]:
        """Walk the orderbook to place orders behind a liquidity buffer.

        We accumulate dollar volume from the top of each side of the book
        until we reach MIN_LIQUIDITY_BUFFER, then place our order one tick
        beyond that level.  This ensures that at least $1000 (by default)
        of existing orders must be filled before ours are reached.

        Args:
            order_book: Dict with 'bids' and 'asks' lists. Each entry is
                a dict with 'price' and 'size' keys.

        Returns:
            (our_bid, our_ask) or (None, None) if conditions are not met.
        """
        try:
            tick = self.market.get("tick_size", 0.01)
            max_spread = self.market["max_spread"]

            # Walk bids top-down: accumulate $ volume until buffer met.
            # We join the price level where $1000 of cumulative liquidity
            # sits at equal-or-better prices.  Orders at the same price
            # fill FIFO, so existing orders at that level are ahead of us.
            our_bid = None
            cumulative = 0.0
            for level in order_book["bids"]:
                price = float(level["price"])
                size = float(level["size"])
                cumulative += price * size
                if cumulative >= MIN_LIQUIDITY_BUFFER:
                    our_bid = self.round_to_tick(price)
                    break

            # Walk asks bottom-up: accumulate $ volume until buffer met
            our_ask = None
            cumulative = 0.0
            for level in order_book["asks"]:
                price = float(level["price"])
                size = float(level["size"])
                cumulative += price * size
                if cumulative >= MIN_LIQUIDITY_BUFFER:
                    our_ask = self.round_to_tick(price)
                    break

            if our_bid is None or our_ask is None:
                log.warning(
                    f"Not enough liquidity buffer (need ${MIN_LIQUIDITY_BUFFER}) "
                    f"for {self.market['question'][:40]} — skipping"
                )
                return None, None

            # Verify we are still inside the reward window
            best_bid = float(order_book["bids"][0]["price"])
            best_ask = float(order_book["asks"][0]["price"])
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
            clob_price = self.round_to_tick(1 - price)

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
        budget_shares = ORDER_SIZE / clob_price
        min_cost = min_shares * clob_price

        # If the rewards minimum exceeds our hard cap, skip this side
        if min_cost > MAX_ORDER_BUDGET:
            log.warning(
                f"Min order ({min_shares} shares × ${clob_price:.2f} "
                f"= ${min_cost:.0f}) exceeds hard cap "
                f"${MAX_ORDER_BUDGET} — skipping {side.upper()}"
            )
            return None

        # Always place at least min_shares to qualify for rewards.
        # Use ORDER_SIZE as preferred budget, but allow up to
        # MAX_ORDER_BUDGET if the rewards minimum demands it.
        if size is None:
            size = max(min_shares, budget_shares)
        size = round(size, 2)

        # Hard cap — never exceed MAX_ORDER_BUDGET
        max_shares = MAX_ORDER_BUDGET / clob_price
        if size > max_shares:
            log.debug(
                f"Size capped from {size} to {max_shares:.2f} shares "
                f"(hard cap at ${MAX_ORDER_BUDGET})"
            )
            size = round(max_shares, 2)

        est_cost = size * clob_price
        log.info(
            f"Order size | {side.upper()} | clob_price={clob_price:.4f} | "
            f"min_shares={min_shares} | budget_shares={budget_shares:.1f} | "
            f"final_size={size} | est_cost=${est_cost:.2f}"
            + (f" (above target ${ORDER_SIZE}, needed for rewards min)"
               if est_cost > ORDER_SIZE else "")
        )

        # Gate: check position limit before placing
        if not self.position_tracker.can_quote(condition_id, side):
            log.debug(f"Quoting halted on {side.upper()} — skipping")
            return None

        # ── Dry Run ──────────────────────────────────────────────────────────
        if DRY_RUN:
            dry_id = f"DRY-{side.upper()}-{int(price * 10000)}"
            self.active_orders[dry_id] = {
                "side": side,
                "price": price,
                "size": float(size),
                "original_size": float(size),
                "placed_at": _time.time(),
                "miss_count": 0,
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

            # The POST response 'orderID' differs from the exchange 'id'.
            # Fetch open orders and find ours by matching asset + price + side.
            exchange_id = self._find_exchange_order_id(
                token_id, str(clob_price), clob_side
            )

            if exchange_id:
                order_id = exchange_id
                self.active_orders[order_id] = {
                    "side": side,
                    "price": price,
                    "size": float(size),
                    "original_size": float(size),
                    "placed_at": _time.time(),
                    "miss_count": 0,
                }
                self.failure_counts[side] = 0
                log_order_placed(side.upper(), price, size, question, order_id)
                return order_id
            else:
                # Do NOT track with POST orderID — it causes false fill alerts.
                # The order is live on the exchange but we can't reliably track it.
                # Next cycle will see no active order on this side and place a new one.
                log.warning(
                    f"Could not find exchange order ID for {side.upper()} "
                    f"at {price:.4f} — order placed but NOT tracked "
                    f"(avoids false fill alerts)"
                )
                self.failure_counts[side] = 0
                return None

        except Exception as e:
            self.failure_counts[side] += 1
            alert_order_failure(
                question, side.upper(), str(e),
                self.failure_counts[side],
            )
            return None

    def _find_exchange_order_id(
        self, token_id: str, price: str, side: str
    ) -> str | None:
        """Fetch open orders and find the exchange ID for a just-placed order.

        The POST response 'orderID' is not the same as the exchange 'id'
        used for cancellation and tracking.  This method matches by
        asset_id, price, and side to find the correct exchange ID.

        Args:
            token_id: The token/asset ID of the order.
            price: The order price as a string.
            side: BUY constant (we always buy tokens).

        Returns:
            The exchange order ID, or None if not found.
        """
        _time.sleep(0.5)  # Brief pause to let the exchange register the order
        try:
            open_orders = self.client.get_orders()
            if not open_orders:
                return None
            for o in open_orders:
                o_id = o["id"]
                # Skip orders we already track
                if o_id in self.active_orders:
                    continue
                # Match on asset_id + price + side
                if (o["asset_id"] == token_id
                        and abs(float(o["price"]) - float(price)) < 1e-9
                        and o["side"] == side):
                    return o_id
        except Exception as e:
            log.debug(f"Could not fetch exchange order ID: {e}")
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
            # Only consider orders belonging to this market's tokens
            market_tokens = set(self.market["token_ids"])
            open_orders = self.client.get_orders()
            open_map: dict[str, dict] = {}
            if open_orders:
                open_map = {
                    o["id"]: o for o in open_orders
                    if o.get("asset_id") in market_tokens
                }

            log.debug(
                f"Fill check | tracked={len(self.active_orders)} | "
                f"on_exchange={len(open_map)} | "
                f"market={self.market['question'][:30]}"
            )

            # Safety: if ALL tracked orders are missing, it's likely
            # an external cancel (another bot instance, manual cancel),
            # not simultaneous fills. Clear tracker and skip.
            now = _time.time()
            missing = [
                oid for oid in self.active_orders
                if oid not in open_map
                and now - self.active_orders[oid].get("placed_at", 0) >= 90
            ]
            eligible_count = sum(
                1 for o in self.active_orders.values()
                if now - o.get("placed_at", 0) >= 90
            )
            if len(missing) == eligible_count and eligible_count > 1:
                log.warning(
                    f"ALL {eligible_count} orders missing from exchange "
                    f"— likely external cancel, NOT fills. "
                    f"Clearing tracker for {self.market['question'][:40]}"
                )
                self.active_orders.clear()
                return

            for oid in list(self.active_orders.keys()):
                order = self.active_orders[oid]
                side = order["side"]

                # Grace period: skip fill detection for orders < 90s old
                age = now - order.get("placed_at", 0)
                if age < 90:
                    log.debug(
                        f"Skipping fill check for {side.upper()} "
                        f"order (age={age:.0f}s < 90s)"
                    )
                    continue

                if oid not in open_map:
                    # Order missing from exchange — increment miss counter.
                    # Only declare a fill after 3 consecutive misses (90s)
                    # to tolerate transient API hiccups.
                    order["miss_count"] = order.get("miss_count", 0) + 1
                    if order["miss_count"] < 3:
                        log.info(
                            f"Order {oid[:16]}... missing from exchange "
                            f"(miss #{order['miss_count']}/3) — "
                            f"waiting to confirm | {side.upper()} | "
                            f"market={self.market['question'][:40]}"
                        )
                        continue

                    # 3 consecutive misses — confirmed full fill
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
                    # Order found on exchange — reset miss counter
                    order["miss_count"] = 0

                    # Check for partial fill
                    exchange_order = open_map[oid]
                    orig = float(exchange_order["original_size"])
                    matched = float(exchange_order["size_matched"])
                    remaining = orig - matched
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

        best_bid = float(order_book["bids"][0]["price"])
        best_ask = float(order_book["asks"][0]["price"])

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

        # Step 5: Cancel stale orders whose price has drifted from optimal
        tick = self.market.get("tick_size", 0.01)
        for order_id in list(self.active_orders.keys()):
            order = self.active_orders[order_id]
            side = order["side"]
            optimal_price = our_bid if side == "yes" else our_ask
            if abs(order["price"] - optimal_price) >= tick:
                log.info(
                    f"Price moved: {side.upper()} "
                    f"{order['price']:.4f} -> {optimal_price:.4f} "
                    f"— refreshing"
                )
                self.cancel_order(order_id, reason="price_refresh")

        # Step 6: Place fresh orders where needed
        active_sides = {o["side"] for o in self.active_orders.values()}
        needs_yes = "yes" not in active_sides
        needs_no = "no" not in active_sides

        if not needs_yes and not needs_no:
            log.debug("Both sides at optimal prices — holding")
            return

        if needs_yes:
            self.place_order("yes", our_bid)
        if needs_no:
            self.place_order("no", our_ask)
