"""
Order management for the Polymarket market-making bot.

Handles order placement, cancellation, fill detection, and the core
quoting strategy. Orders are placed behind a configurable liquidity
buffer to minimise adverse fill risk.
"""

import logging
import time as _time
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY, SELL
from config import (
    ORDER_SIZE, MAX_ORDER_BUDGET, DANGER_ZONE_CENTS, DEAD_ZONE_BUFFER,
    MAX_ORDER_FAILURES, DRY_RUN, MAX_ORDERBOOK_SPREAD,
    MIN_LIQUIDITY_BUFFER, MIN_UNWIND_SIZE, MAX_UNWIND_RETRIES,
    MAX_UNWIND_AGE_SECS,
)
from alerts import (
    alert_order_failure, alert_danger_zone,
    alert_fill, alert_unwind, log_order_placed, log_order_cancelled,
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
        self.unwind_orders: dict[str, dict] = {}  # SELL orders to offload inventory
        self.pending_unwinds: list[dict] = []      # Failed unwinds awaiting retry
        self.failure_counts: dict[str, int] = {"yes": 0, "no": 0}
        self._balance_cache: float | None = None
        self._balance_cache_time: float = 0
        self._current_retry_count: int = 0
        self._current_queued_at: float | None = None

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

    # ── Balance Cache ─────────────────────────────────────────────────────────
    def _get_cached_balance(self) -> float | None:
        """Return available USDC balance, cached for 60 seconds.

        Avoids hitting the balance API on every order attempt.
        Returns None if the balance cannot be fetched (don't block on error).
        """
        now = _time.time()
        if now - self._balance_cache_time < 60:
            return self._balance_cache

        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            self._balance_cache = float(bal.get("balance", 0)) / 1e6  # USDC has 6 decimals
            self._balance_cache_time = now
            return self._balance_cache
        except Exception:
            # Don't block order placement if balance check fails
            return None

    def invalidate_balance_cache(self) -> None:
        """Force a fresh balance check on next call."""
        self._balance_cache_time = 0

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

        # Gate: check USDC balance before placing — avoid doomed API calls.
        # Uses cached balance (refreshed once per cycle) to avoid extra API hits.
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
            dry_id = f"DRY-{side.upper()}-{int(price * 10000)}"
            self.active_orders[dry_id] = {
                "side": side,
                "price": price,
                "size": float(size),
                "original_size": float(size),
                "placed_at": _time.time(),
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
                }
                self.failure_counts[side] = 0
                self.invalidate_balance_cache()  # Balance changed
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
                # Skip orders we already track (active OR unwind)
                if o_id in self.active_orders or o_id in self.unwind_orders:
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

    # ── Inventory Unwinding ─────────────────────────────────────────────────
    def place_unwind_order(
        self, side: str, fill_price: float, fill_size: float
    ) -> str | None:
        """Place a SELL limit order to offload filled inventory at acquisition price.

        When a BUY order fills, we hold tokens we don't want. This places
        a SELL order at the same price to break even. Profit comes from
        liquidity rewards, not from holding inventory.

        Args:
            side: "yes" or "no" — which side was filled.
            fill_price: The YES-equivalent price we paid.
            fill_size: Number of shares to sell.

        Returns:
            Exchange order ID of the unwind order, or None on failure.
        """
        question = self.market["question"]

        if side == "yes":
            token_id = self.market["token_ids"][0]
            clob_price = fill_price
        else:
            token_id = self.market["token_ids"][1]
            clob_price = self.round_to_tick(1 - fill_price)

        if clob_price is None or clob_price <= 0:
            log.warning(
                f"Invalid unwind price ({clob_price}) for {side.upper()} "
                f"on {question[:40]} — skipping unwind"
            )
            return None

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

            exchange_id = self._find_exchange_order_id(
                token_id, str(clob_price), SELL
            )

            if exchange_id:
                self.unwind_orders[exchange_id] = {
                    "side": side,
                    "price": fill_price,
                    "clob_price": clob_price,
                    "size": float(fill_size),
                    "placed_at": _time.time(),
                }
                log.info(
                    f"UNWIND ORDER PLACED | SELL {side.upper()} | "
                    f"price={clob_price:.4f} | size={fill_size:.2f} | "
                    f"market={question[:40]} | id={exchange_id}"
                )
                return exchange_id
            else:
                log.warning(
                    f"Unwind order placed but could not find exchange ID "
                    f"for {side.upper()} on {question[:40]} — queuing for retry"
                )
                self._queue_pending_unwind(
                    side, fill_price, fill_size,
                    retry_count=getattr(self, "_current_retry_count", 0),
                    queued_at=getattr(self, "_current_queued_at", None),
                )
                return None

        except Exception as e:
            log.error(
                f"Failed to place unwind order for {side.upper()} "
                f"on {question[:40]}: {e} — queuing for retry"
            )
            self._queue_pending_unwind(
                side, fill_price, fill_size,
                retry_count=getattr(self, "_current_retry_count", 0),
                queued_at=getattr(self, "_current_queued_at", None),
            )
            return None

    def _queue_pending_unwind(
        self, side: str, fill_price: float, fill_size: float,
        retry_count: int = 0, queued_at: float | None = None,
    ) -> None:
        """Queue an unwind that failed to place, for retry next cycle.

        Drops dust fills below MIN_UNWIND_SIZE.
        Drops unwinds that have exceeded MAX_UNWIND_RETRIES or MAX_UNWIND_AGE_SECS.
        Deduplicates: if there's already a pending unwind for the same side,
        merge the size instead of adding a duplicate entry.
        """
        # Drop dust — below exchange minimum
        if fill_size < MIN_UNWIND_SIZE:
            log.info(
                f"DROPPING DUST UNWIND | {side.upper()} | "
                f"size={fill_size:.2f} < {MIN_UNWIND_SIZE} min | "
                f"market={self.market['question'][:40]}"
            )
            return

        now = _time.time()
        first_queued = queued_at or now

        # Drop if exceeded max retries
        if retry_count >= MAX_UNWIND_RETRIES:
            log.warning(
                f"ABANDONING UNWIND after {retry_count} retries | "
                f"{side.upper()} | size={fill_size:.2f} | "
                f"market={self.market['question'][:40]}"
            )
            return

        # Drop if exceeded max age
        age_secs = now - first_queued
        if age_secs > MAX_UNWIND_AGE_SECS:
            log.warning(
                f"ABANDONING UNWIND after {age_secs / 60:.1f}m | "
                f"{side.upper()} | size={fill_size:.2f} | "
                f"market={self.market['question'][:40]}"
            )
            return

        for pending in self.pending_unwinds:
            if (pending["side"] == side
                    and abs(pending["fill_price"] - fill_price) < 1e-9):
                pending["fill_size"] += fill_size
                pending["retry_count"] = max(
                    pending["retry_count"], retry_count
                )
                log.info(
                    f"PENDING UNWIND MERGED | {side.upper()} | "
                    f"total_size={pending['fill_size']:.2f} | "
                    f"market={self.market['question'][:40]}"
                )
                return
        self.pending_unwinds.append({
            "side": side,
            "fill_price": fill_price,
            "fill_size": fill_size,
            "queued_at": first_queued,
            "retry_count": retry_count,
        })
        log.info(
            f"PENDING UNWIND QUEUED | {side.upper()} | "
            f"price={fill_price:.4f} | size={fill_size:.2f} | "
            f"retry={retry_count} | "
            f"market={self.market['question'][:40]}"
        )

    def retry_pending_unwinds(self) -> None:
        """Attempt to place any queued unwind orders.

        Called at the start of each cycle.  Successfully placed unwinds
        are removed from the queue; failures stay for the next cycle.

        To avoid circular re-queueing (place_unwind_order adds to
        pending_unwinds on failure), we snapshot and clear the list
        first, then attempt each one.  Failures will be re-added
        by _queue_pending_unwind (which checks dust/retry/age limits).
        """
        if not self.pending_unwinds:
            return

        # Snapshot and clear — _queue_pending_unwind will re-queue failures
        # (with incremented retry count) only if within limits
        to_retry = self.pending_unwinds[:]
        self.pending_unwinds.clear()

        for pending in to_retry:
            side = pending["side"]
            retry_count = pending.get("retry_count", 0) + 1
            age_min = (_time.time() - pending["queued_at"]) / 60

            # Check limits before even attempting
            if pending["fill_size"] < MIN_UNWIND_SIZE:
                log.info(
                    f"DROPPING DUST UNWIND | {side.upper()} | "
                    f"size={pending['fill_size']:.2f} | "
                    f"market={self.market['question'][:40]}"
                )
                continue
            if retry_count > MAX_UNWIND_RETRIES:
                log.warning(
                    f"ABANDONING UNWIND after {retry_count} retries | "
                    f"{side.upper()} | size={pending['fill_size']:.2f} | "
                    f"market={self.market['question'][:40]}"
                )
                continue
            if age_min * 60 > MAX_UNWIND_AGE_SECS:
                log.warning(
                    f"ABANDONING UNWIND after {age_min:.1f}m | "
                    f"{side.upper()} | size={pending['fill_size']:.2f} | "
                    f"market={self.market['question'][:40]}"
                )
                continue

            log.info(
                f"RETRYING PENDING UNWIND | {side.upper()} | "
                f"price={pending['fill_price']:.4f} | "
                f"size={pending['fill_size']:.2f} | "
                f"retry={retry_count}/{MAX_UNWIND_RETRIES} | "
                f"queued {age_min:.1f}m ago | "
                f"market={self.market['question'][:40]}"
            )
            # Cancel BUY orders on this side to free collateral
            self._cancel_buy_orders_for_side(side)
            # Stash retry metadata so place_unwind_order -> _queue_pending_unwind
            # preserves it on failure
            self._current_retry_count = retry_count
            self._current_queued_at = pending["queued_at"]
            self.place_unwind_order(
                side, pending["fill_price"], pending["fill_size"]
            )
            self._current_retry_count = 0
            self._current_queued_at = None

    def has_pending_unwind(self, side: str) -> bool:
        """Check if there's a pending (failed) unwind for a given side.

        Used by run_cycle to block new BUY orders when we have inventory
        that still needs unwinding.
        """
        for pending in self.pending_unwinds:
            if pending["side"] == side:
                return True
        # Also check if there's an active unwind order on this side
        for uorder in self.unwind_orders.values():
            if uorder["side"] == side:
                return True
        return False

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
            self.invalidate_balance_cache()  # Collateral freed
        except Exception as e:
            log.error(f"Failed to cancel order {order_id}: {e}")

    def cancel_all(
        self, reason: str = "refresh", include_unwinds: bool = False
    ) -> None:
        """Cancel all active orders for this market.

        Args:
            reason: Human-readable reason for logging.
            include_unwinds: If True, also cancel unwind (SELL) orders
                and clear pending unwinds.  Only set True on bot
                shutdown — normally unwind orders should persist.
        """
        for order_id in list(self.active_orders.keys()):
            self.cancel_order(order_id, reason)

        if include_unwinds:
            for order_id in list(self.unwind_orders.keys()):
                try:
                    self.client.cancel(order_id)
                    log_order_cancelled(order_id, f"unwind-{reason}")
                except Exception as e:
                    log.error(f"Failed to cancel unwind order {order_id}: {e}")
                del self.unwind_orders[order_id]
            self.pending_unwinds.clear()

        if self.active_orders or (include_unwinds and self.unwind_orders):
            log.info(f"Cancelled all for {self.market['question'][:40]}")
        self.active_orders.clear()

    def _cancel_buy_orders_for_side(self, side: str) -> None:
        """Cancel all active BUY orders on the given side.

        Called before placing a SELL (unwind) order so that collateral
        locked by open BUY orders is freed.  Without this, the exchange
        rejects SELL orders with 'not enough balance / allowance'.

        Args:
            side: "yes" or "no" — cancel BUY orders on this side.
        """
        cancelled = 0
        for oid in list(self.active_orders.keys()):
            order = self.active_orders[oid]
            if order["side"] == side:
                log.info(
                    f"Cancelling {side.upper()} BUY to free collateral "
                    f"for unwind | id={oid[:16]}..."
                )
                self.cancel_order(oid, reason="free_collateral_for_unwind")
                cancelled += 1
        if cancelled:
            log.info(
                f"Freed collateral: cancelled {cancelled} {side.upper()} "
                f"BUY order(s) | market={self.market['question'][:40]}"
            )

    def has_open_obligations(self) -> bool:
        """Check if this manager has any unwind orders or pending unwinds.

        Used by the bot to decide whether to keep the manager alive
        when a market is removed from the active set.
        """
        return bool(self.unwind_orders) or bool(self.pending_unwinds)

    # ── Fill Detection ───────────────────────────────────────────────────────
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

    def detect_fills(self) -> None:
        """Compare tracked orders against open orders on exchange.

        When an order disappears from get_orders(), we verify its status
        using get_order(id).  Only orders with status "MATCHED" are
        treated as fills — everything else (CANCELLED, expired, etc.)
        is silently removed from tracking.

        Also detects partial fills (remaining size < original) and
        tracks unwind (SELL) orders.  Skipped in dry-run.
        """
        if DRY_RUN:
            return
        if not self.active_orders and not self.unwind_orders:
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

            for oid in list(self.active_orders.keys()):
                order = self.active_orders[oid]
                side = order["side"]

                if oid not in open_map:
                    # Order gone — check WHY via individual status query
                    status = self._get_order_status(oid)

                    if status == "MATCHED":
                        # CONFIRMED fill — isolate so one failure
                        # doesn't prevent processing remaining fills
                        try:
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
                            # Cancel any remaining BUY orders on this side
                            # to free collateral for the SELL unwind
                            self._cancel_buy_orders_for_side(side)
                            self.place_unwind_order(
                                side, order["price"], order["original_size"]
                            )
                        except Exception as e:
                            log.error(
                                f"Error processing fill for {oid[:16]}... "
                                f"({side.upper()}): {e} — continuing to "
                                f"next order"
                            )
                    else:
                        # NOT a fill — cancelled, expired, or unknown
                        log.info(
                            f"Order {oid[:16]}... removed (status={status}) "
                            f"— NOT a fill | {side.upper()} | "
                            f"market={self.market['question'][:40]}"
                        )

                    del self.active_orders[oid]
                else:
                    # Order still on exchange — check for partial fill
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
                        # Cancel any BUY order on this side to free
                        # collateral BEFORE placing the SELL unwind.
                        self._cancel_buy_orders_for_side(side)
                        # Place unwind for the partially filled shares
                        self.place_unwind_order(
                            side, order["price"], filled_shares
                        )
                        # Update tracked size so next partial detection
                        # only captures the NEW delta, not the same fill.
                        self.active_orders[oid]["size"] = remaining
                        self.active_orders[oid]["original_size"] = remaining

            # ── Check unwind (SELL) orders ────────────────────────────────
            if self.unwind_orders and open_orders is not None:
                full_open_map = {o["id"]: o for o in open_orders}

                for oid in list(self.unwind_orders.keys()):
                    uorder = self.unwind_orders[oid]
                    side = uorder["side"]

                    # Grace period: don't check orders < 90s old
                    age = _time.time() - uorder.get("placed_at", 0)
                    if age < 90:
                        log.debug(
                            f"Skipping unwind check for {oid[:16]}... "
                            f"(age={age:.0f}s < 90s)"
                        )
                        continue

                    if oid not in full_open_map:
                        status = self._get_order_status(oid)

                        if status == "MATCHED":
                            unwound_usd = uorder["price"] * uorder["size"]
                            log.info(
                                f"INVENTORY UNWOUND | {side.upper()} | "
                                f"price={uorder['price']:.4f} | "
                                f"size={uorder['size']:.2f} | "
                                f"value=${unwound_usd:.2f} | "
                                f"market={self.market['question'][:40]}"
                            )
                            alert_unwind(
                                side=side.upper(),
                                price=uorder["price"],
                                size=uorder["size"],
                                usd_value=unwound_usd,
                                market_question=self.market["question"],
                            )
                            self.position_tracker.record_unwind(
                                self.market["condition_id"], side, unwound_usd
                            )
                            del self.unwind_orders[oid]
                        elif status == "UNKNOWN":
                            # API error — keep tracking, count consecutive failures
                            uorder["unknown_count"] = uorder.get("unknown_count", 0) + 1
                            if uorder["unknown_count"] >= 5:
                                log.warning(
                                    f"Unwind order {oid[:16]}... status UNKNOWN "
                                    f"for {uorder['unknown_count']} consecutive "
                                    f"checks — removing from tracking | "
                                    f"{side.upper()} | "
                                    f"market={self.market['question'][:40]}"
                                )
                                del self.unwind_orders[oid]
                            else:
                                log.info(
                                    f"Unwind order {oid[:16]}... status UNKNOWN "
                                    f"(#{uorder['unknown_count']}/5) — keeping | "
                                    f"{side.upper()}"
                                )
                        else:
                            # Definitive non-fill status (CANCELLED, INVALID, etc.)
                            # Retry the unwind — the position still needs unwinding
                            log.warning(
                                f"Unwind order {oid[:16]}... failed "
                                f"(status={status}) | {side.upper()} | "
                                f"market={self.market['question'][:40]} — "
                                f"retrying unwind"
                            )
                            del self.unwind_orders[oid]
                            # Re-place the unwind order so inventory isn't stranded
                            self.place_unwind_order(
                                side, uorder["price"], uorder["size"]
                            )
                    else:
                        # Order found on exchange — check for partial unwind fill
                        uorder["unknown_count"] = 0
                        exch = full_open_map[oid]
                        u_orig = float(exch["original_size"])
                        u_matched = float(exch["size_matched"])
                        u_remaining = u_orig - u_matched
                        u_tracked = uorder["size"]
                        if u_remaining < u_tracked - 0.01:
                            unwound_shares = u_tracked - u_remaining
                            unwound_usd = uorder["price"] * unwound_shares
                            log.info(
                                f"UNWIND (PARTIAL) | {side.upper()} | "
                                f"sold={unwound_shares:.2f} shares | "
                                f"remaining={u_remaining:.2f} | "
                                f"value=${unwound_usd:.2f} | "
                                f"market={self.market['question'][:40]}"
                            )
                            self.position_tracker.record_unwind(
                                self.market["condition_id"], side, unwound_usd
                            )
                            uorder["size"] = u_remaining

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

        # Step 0: Retry any pending unwinds before anything else
        self.retry_pending_unwinds()

        # Step 1: Detect fills
        self.detect_fills()

        # Step 1b: Cancel active BUY orders on any halted side
        #          (prevents position overshoot — existing orders can
        #           fill AFTER the limit is hit if not cancelled)
        condition_id = self.market["condition_id"]
        for oid in list(self.active_orders.keys()):
            order = self.active_orders[oid]
            side = order["side"]
            if not self.position_tracker.can_quote(condition_id, side):
                log.info(
                    f"Cancelling {side.upper()} order {oid[:16]}... "
                    f"(position halted — prevent overshoot)"
                )
                self.cancel_order(oid, reason="position_halted")

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
        #         BLOCK new BUY orders if there's an unwind pending/active
        #         on that side — we need to sell inventory first, not buy more
        active_sides = {o["side"] for o in self.active_orders.values()}
        needs_yes = "yes" not in active_sides
        needs_no = "no" not in active_sides

        if needs_yes and self.has_pending_unwind("yes"):
            log.info(
                f"Blocking YES BUY — unwind pending/active | "
                f"market={question[:40]}"
            )
            needs_yes = False
        if needs_no and self.has_pending_unwind("no"):
            log.info(
                f"Blocking NO BUY — unwind pending/active | "
                f"market={question[:40]}"
            )
            needs_no = False

        if not needs_yes and not needs_no:
            log.debug("Both sides covered or blocked — holding")
            return

        if needs_yes:
            self.place_order("yes", our_bid)
        if needs_no:
            self.place_order("no", our_ask)
