import logging
import requests as req
import json
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY, SELL
from config import (
    ORDER_SIZE, SPREAD_DEPTH, DANGER_ZONE_CENTS,
    DEAD_ZONE_BUFFER, MAX_ORDER_FAILURES, DRY_RUN
)
from alerts import (
    alert_order_failure, alert_danger_zone,
    log_order_placed, log_order_cancelled
)

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


class OrderManager:

    def __init__(self, client, market, position_tracker):
        self.client           = client
        self.market           = market
        self.position_tracker = position_tracker
        self.active_orders    = {}
        self.failure_counts   = {"yes": 0, "no": 0}

    # ── Fresh Price Fetch ─────────────────────────────────────────────────────
    def _fetch_fresh_yes_price(self):
        """
        Fetch the latest yes_price directly from Gamma API
        for this specific market. Called when order book is sparse.
        """
        try:
            condition_id = self.market["condition_id"]
            url          = f"{GAMMA_API}/markets"
            params       = {"conditionId": condition_id}
            response     = req.get(url, params=params, timeout=5)
            data         = response.json()
            if data and isinstance(data, list):
                prices = json.loads(data[0].get("outcomePrices", "[]"))
                if prices:
                    return float(prices[0])
        except Exception as e:
            log.debug(f"Could not fetch fresh yes_price: {e}")
        return None

    # ── Order Book ────────────────────────────────────────────────────────────
    def get_best_prices(self):
        """
        Fetch live order book. If sparse, fetch fresh yes_price
        from Gamma API rather than using stale stored value.
        """
        try:
            token_id   = self.market["token_ids"][0]
            order_book = self.client.get_order_book(token_id)

            best_bid = float(order_book.bids[0].price) if order_book.bids else None
            best_ask = float(order_book.asks[0].price) if order_book.asks else None

            # If spread wider than 50 cents, order book is too sparse to trust
            if best_bid and best_ask:
                if (best_ask - best_bid) > 0.50:
                    log.debug("Sparse order book — fetching fresh price")
                    best_bid = None
                    best_ask = None

            # Fall back to fresh yes_price from Gamma API
            if best_bid is None or best_ask is None:
                fresh_price = self._fetch_fresh_yes_price()
                if fresh_price:
                    self.market["yes_price"] = fresh_price
                    best_bid = round(fresh_price - 0.01, 4)
                    best_ask = round(fresh_price + 0.01, 4)
                    log.debug(
                        f"Fresh yes_price={fresh_price:.4f} | "
                        f"{self.market['question'][:40]}"
                    )
                else:
                    yes_price = self.market["yes_price"] or 0.50
                    best_bid  = round(yes_price - 0.01, 4)
                    best_ask  = round(yes_price + 0.01, 4)

            return best_bid, best_ask

        except Exception as e:
            log.error(f"Failed to fetch order book for "
                      f"{self.market['question'][:40]}: {e}")
            yes_price = self.market["yes_price"] or 0.50
            return round(yes_price - 0.01, 4), round(yes_price + 0.01, 4)

    # ── Price Calculation ─────────────────────────────────────────────────────
    def calculate_order_prices(self, best_bid, best_ask):
        """
        Calculate where to place our orders.
        Places orders at SPREAD_DEPTH fraction of max_spread from midpoint.
        """
        try:
            max_spread = self.market["max_spread"]
            midpoint   = (best_bid + best_ask) / 2

            offset  = max_spread * SPREAD_DEPTH
            our_bid = round(midpoint - offset, 4)
            our_ask = round(midpoint + offset, 4)

            # Safety clamps
            our_bid = max(0.01, min(our_bid, 0.98))
            our_ask = max(0.02, min(our_ask, 0.99))

            # Ensure bid is always below ask
            if our_bid >= our_ask:
                our_bid = round(our_ask - 0.01, 4)

            log.debug(
                f"Prices | midpoint={midpoint:.4f} | "
                f"offset={offset:.4f} | "
                f"bid={our_bid:.4f} | ask={our_ask:.4f}"
            )
            return our_bid, our_ask

        except Exception as e:
            log.error(f"Price calculation failed: {e}")
            return None, None

    # ── Zone Checking ─────────────────────────────────────────────────────────
    def check_order_zone(self, order_id, best_bid, best_ask):
        """
        Check which zone an order is in relative to the midpoint.

        DANGER → too close to midpoint (risk of fill)
        REWARD → inside max spread window (earning rewards)
        DEAD   → outside max spread window (earning nothing)
        """
        if order_id not in self.active_orders:
            return "UNKNOWN"

        order      = self.active_orders[order_id]
        price      = order["price"]
        side       = order["side"]
        max_spread = self.market["max_spread"]

        # Use fresh yes_price as midpoint reference
        midpoint = self.market["yes_price"]
        if midpoint is None:
            midpoint = (best_bid + best_ask) / 2

        gap = abs(price - midpoint)

        # DANGER ZONE — order too close to midpoint
        if gap < DANGER_ZONE_CENTS:
            alert_danger_zone(
                self.market["question"], side.upper(),
                price, midpoint
            )
            return "DANGER"

        # DEAD ZONE — order outside the rewards window
        if gap > max_spread + DEAD_ZONE_BUFFER:
            return "DEAD"

        return "REWARD"

    # ── Order Placement ───────────────────────────────────────────────────────
    def place_order(self, side, price, size=None):
        """Place a single limit order on one side."""
        condition_id = self.market["condition_id"]
        question     = self.market["question"]
        size         = size or max(ORDER_SIZE, self.market["min_size"])

        # Gate: check position limit before placing
        if not self.position_tracker.can_quote(condition_id, side):
            log.debug(f"Quoting halted on {side.upper()} — skipping")
            return None

        token_id  = self.market["token_ids"][0]
        clob_side = BUY if side == "yes" else SELL

        # ── Dry Run ───────────────────────────────────────────────────────────
        if DRY_RUN:
            dry_id = f"DRY-{side.upper()}-{int(price * 10000)}"
            self.active_orders[dry_id] = {
                "side":  side,
                "price": price,
                "size":  float(size)
            }
            log.info(
                f"[DRY RUN] Would place {side.upper()} | "
                f"price={price:.4f} | size={size} | "
                f"market={question[:40]}"
            )
            return dry_id

        # ── Live Trading ──────────────────────────────────────────────────────
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
                "side":  side,
                "price": price,
                "size":  float(size)
            }

            self.failure_counts[side] = 0
            log_order_placed(side.upper(), price, size, question, order_id)
            return order_id

        except Exception as e:
            self.failure_counts[side] += 1
            alert_order_failure(
                question, side.upper(), str(e),
                self.failure_counts[side]
            )
            return None

    def place_both_sides(self, our_bid, our_ask):
        """Place orders on both Yes and No sides."""
        yes_id = self.place_order("yes", our_bid)
        no_id  = self.place_order("no",  our_ask)
        return yes_id, no_id

    # ── Order Cancellation ────────────────────────────────────────────────────
    def cancel_order(self, order_id, reason="manual"):
        """Cancel a single order."""
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

    def cancel_all(self, reason="refresh"):
        """Cancel all active orders for this market."""
        if not self.active_orders:
            return
        for order_id in list(self.active_orders.keys()):
            self.cancel_order(order_id, reason)
        log.info(f"Cancelled all for {self.market['question'][:40]}")

    # ── Fill Detection ────────────────────────────────────────────────────────
    def detect_fills(self):
        """
        Compare tracked orders against open orders on exchange.
        Any order no longer on exchange has been filled.
        Skipped in dry run mode.
        """
        if not self.active_orders or DRY_RUN:
            return

        try:
            open_orders = self.client.get_orders()
            open_ids    = {o.id for o in open_orders} if open_orders else set()

            filled = [
                (oid, order)
                for oid, order in self.active_orders.items()
                if oid not in open_ids
            ]

            for order_id, order in filled:
                side       = order["side"]
                filled_usd = order["price"] * order["size"]
                log.info(
                    f"FILL DETECTED | {side.upper()} | "
                    f"price={order['price']:.4f} | "
                    f"value=${filled_usd:.2f} | "
                    f"market={self.market['question'][:40]}"
                )
                self.position_tracker.record_fill(
                    self.market["condition_id"], side, filled_usd
                )
                del self.active_orders[order_id]

        except Exception as e:
            log.error(f"Fill detection error: {e}")

    # ── Full Cycle ────────────────────────────────────────────────────────────
    def run_cycle(self):
        """
        One complete order management cycle:
        1. Detect any fills
        2. Get current best prices
        3. Check zones — cancel DANGER and DEAD orders
        4. Place fresh orders where needed
        """
        question = self.market["question"]
        log.debug(f"Running cycle for: {question[:50]}")

        # Step 1: Detect fills
        self.detect_fills()

        # Step 2: Get best prices
        best_bid, best_ask = self.get_best_prices()
        if best_bid is None or best_ask is None:
            log.warning(f"No prices for {question[:40]} — skipping")
            return

        log.info(
            f"Market: {question[:45]} | "
            f"bid={best_bid:.4f} | ask={best_ask:.4f}"
        )

        # Step 3: Check zones and cancel bad orders
        for order_id in list(self.active_orders.keys()):
            zone = self.check_order_zone(order_id, best_bid, best_ask)
            if zone in ("DANGER", "DEAD"):
                self.cancel_order(order_id, reason=zone.lower())

        # Step 4: Place fresh orders where needed
        active_sides = {o["side"] for o in self.active_orders.values()}
        needs_yes    = "yes" not in active_sides
        needs_no     = "no"  not in active_sides

        if not needs_yes and not needs_no:
            log.debug("Both sides active — holding")
            return

        our_bid, our_ask = self.calculate_order_prices(best_bid, best_ask)
        if our_bid is None:
            return

        if needs_yes:
            self.place_order("yes", our_bid)
        if needs_no:
            self.place_order("no", our_ask)
