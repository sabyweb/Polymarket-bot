"""
Order manager orchestration for the Polymarket market-making bot.

OrderManager composes pricing, placement, fill detection, and unwind
logic via mixins. Each concern lives in its own module; this file holds
the shared state (__init__), utility methods, cancellation, and the
run_cycle orchestrator.
"""

import logging
import math
import threading
import time as _time
from dataclasses import dataclass, field
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
from config import (
    ORDER_REFRESH_SECS,
    MAX_ORDER_FAILURES, DRY_RUN, MAX_ORDERBOOK_SPREAD,
    MIN_UNWIND_SHARES,
    MIN_SELL_PRICE,
    MIN_PRICE_DRIFT_TICKS,
)
from alerts import (
    alert_danger_zone,
    log_order_cancelled,
)
from price import to_clob, to_yes_equiv

from pricing import PricingMixin
from placement import PlacementMixin
from fills import FillsMixin
from unwind import UnwindMixin
from database import get_db

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Typed order structs — replace fragile dict[str, Any] access everywhere
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TrackedOrder:
    """An active BUY-side order we are tracking on the exchange."""
    side: str                   # "yes" or "no"
    price: float                # YES-equivalent price
    size: float                 # Current remaining size (shares)
    original_size: float        # Size at placement time
    placed_at: float = 0.0     # Unix timestamp of placement

    def to_dict(self) -> dict:
        """Legacy dict representation for backward compatibility."""
        return {
            "side": self.side,
            "price": self.price,
            "size": self.size,
            "original_size": self.original_size,
            "placed_at": self.placed_at,
        }


@dataclass
class UnwindOrder:
    """A SELL-side unwind order tracking a position we need to exit."""
    side: str                   # "yes" or "no"
    price: float                # YES-equivalent VWAP cost basis
    clob_price: float           # Current CLOB ask price (decays over time)
    size: float                 # Current remaining size (shares)
    placed_at: float = 0.0     # When the SELL order was posted
    created_at: float = 0.0    # When the original BUY fill happened (for decay)
    base_clob_price: float = 0.0  # Initial CLOB price before any decay
    unknown_count: int = 0      # Consecutive API failures during status checks

    def to_dict(self) -> dict:
        """Legacy dict representation for backward compatibility."""
        return {
            "side": self.side,
            "price": self.price,
            "clob_price": self.clob_price,
            "size": self.size,
            "placed_at": self.placed_at,
            "created_at": self.created_at,
            "base_clob_price": self.base_clob_price,
            "unknown_count": self.unknown_count,
        }


class BalanceGate:
    """Shared USDC balance tracker across all OrderManagers.

    Solves the problem where each manager independently queries the total
    USDC balance without knowing about collateral locked by other managers'
    orders.  When any manager gets a "not enough balance / allowance"
    rejection from the exchange, the gate is marked depleted and all
    managers skip order placement until collateral is freed (order cancel)
    or the next refresh window.
    """

    def __init__(self, client: object) -> None:
        self._client = client
        self._raw_balance: float | None = None
        self._cache_time: float = 0
        self._depleted_until: float = 0

    def get_balance(self) -> float | None:
        """Return cached USDC balance, refreshed every 60 seconds."""
        now = _time.time()
        if now - self._cache_time < 60:
            return self._raw_balance
        try:
            bal = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            self._raw_balance = float(bal.get("balance", 0)) / 1e6
            self._cache_time = now
            return self._raw_balance
        except Exception:
            return None

    def can_afford(self, est_cost: float) -> bool:
        """Check if we likely have enough balance for this order."""
        now = _time.time()
        if now < self._depleted_until:
            return False
        balance = self.get_balance()
        if balance is None:
            return True
        return est_cost <= balance

    def mark_depleted(self, cooldown_secs: float = 30.0) -> None:
        """Mark balance as depleted — skip all order attempts for cooldown."""
        self._depleted_until = _time.time() + cooldown_secs
        log.info(
            f"Balance gate DEPLETED — skipping new orders for "
            f"{cooldown_secs:.0f}s"
        )

    def invalidate(self) -> None:
        """Force a fresh balance check and clear depleted state."""
        self._cache_time = 0
        self._depleted_until = 0

    @property
    def is_depleted(self) -> bool:
        return _time.time() < self._depleted_until


class OrderManager(PricingMixin, PlacementMixin, FillsMixin, UnwindMixin):
    """Manages order lifecycle for a single market.

    Composes four concerns via mixins:
      - PricingMixin:   calculate_order_prices, co-best pricing, inventory skew
      - PlacementMixin: place_order
      - FillsMixin:     detect_fills, _get_order_status
      - UnwindMixin:    place_unwind_order, reconcile_unwinds, check_stop_loss

    This class holds all shared state and provides utility methods
    (rounding, balance, order book, cancellation) plus the run_cycle
    orchestrator.

    Args:
        client: Authenticated ClobClient instance.
        market: Dict of market metadata (condition_id, token_ids, etc.).
        position_tracker: PositionTracker instance for fill accounting.
        balance_gate: Shared BalanceGate for cross-manager balance awareness.
    """

    def __init__(
        self, client: object, market: dict, position_tracker: object,
        balance_gate: "BalanceGate | None" = None,
    ) -> None:
        self.client = client
        self.market = market
        self.position_tracker = position_tracker
        self.balance_gate = balance_gate
        self.active_orders: dict[str, TrackedOrder] = {}
        self.unwind_orders: dict[str, UnwindOrder] = {}
        self.failure_counts: dict[str, int] = {"yes": 0, "no": 0}
        self._balance_cache: float | None = None
        self._balance_cache_time: float = 0
        self._token_balance_cache: dict[str, tuple[float, float]] = {}
        self._last_fill_time: dict[str, float] = {"yes": 0.0, "no": 0.0}
        self._reward_tracker: object | None = None
        self._last_order_book: dict | None = None  # Cached for Q-score calculation
        self._unwind_lock = threading.Lock()  # A2: thread safety for Timer-based retries

        # M1: EMA fair value tracking
        self._ema_mid: float = 0.0
        self._ema_initialized: bool = False

        # M2: Volatility tracking (recent midpoint history)
        self._midpoint_history: list[float] = []  # Last N midpoints

    # ── Tick Size Rounding ───────────────────────────────────────────────────
    def round_to_tick(self, price: float) -> float:
        """Round a price to the nearest valid tick size for this market."""
        tick = self.market.get("tick_size", 0.01)
        if tick <= 0:
            tick = 0.01
        rounded = round(round(price / tick) * tick, 10)
        decimal_places = len(str(tick).rstrip("0").split(".")[-1])
        return round(rounded, decimal_places)

    def _last_market_bid(self, side: str) -> float:
        """Return the last known market bid for the given side."""
        bid = getattr(self, "_cached_best_bid", 0)
        ask = getattr(self, "_cached_best_ask", 1)
        if side == "yes":
            return bid
        return max(MIN_SELL_PRICE, round(to_clob(ask, "no"), 4))

    def refresh_cached_book(self) -> None:
        """Fetch order book and update cached bid/ask for decay calculations.

        Used for unwind-only markets that don't go through run_cycle().
        """
        try:
            book = self.client.get_order_book(
                self.market["condition_id"]
            )
            if book.get("bids") and book.get("asks"):
                self._cached_best_bid = float(book["bids"][0]["price"])
                self._cached_best_ask = float(book["asks"][0]["price"])
        except Exception as e:
            log.warning(
                f"Could not refresh book for unwind market "
                f"{self.market.get('question', '?')[:30]}: {e}"
            )

    def round_down_to_tick(self, price: float) -> float:
        """Round a price DOWN to the nearest valid tick (floor)."""
        tick = self.market.get("tick_size", 0.01)
        if tick <= 0:
            tick = 0.01
        decimal_places = len(str(tick).rstrip("0").split(".")[-1])
        floored = math.floor(price / tick) * tick
        return round(floored, decimal_places)

    # ── Balance Cache ─────────────────────────────────────────────────────────
    def _get_cached_balance(self) -> float | None:
        """Return available USDC balance, cached for 60 seconds."""
        now = _time.time()
        if now - self._balance_cache_time < 60:
            return self._balance_cache
        try:
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            self._balance_cache = float(bal.get("balance", 0)) / 1e6
            self._balance_cache_time = now
            return self._balance_cache
        except Exception:
            return None

    def invalidate_balance_cache(self) -> None:
        """Force a fresh balance check on next call."""
        self._balance_cache_time = 0
        if self.balance_gate:
            self.balance_gate.invalidate()

    # ── Token Balance Verification ────────────────────────────────────────────
    def verify_token_balance(self, side: str) -> float:
        """Query actual on-exchange token balance for a side.

        Results are cached for 15 seconds to avoid duplicate API calls.
        Returns -1 if the check fails (don't block operations on errors).
        """
        now = _time.time()
        if side in self._token_balance_cache:
            cached_bal, cached_at = self._token_balance_cache[side]
            if now - cached_at < 15:
                return cached_bal

        try:
            token_id = self.market["token_ids"][0 if side == "yes" else 1]
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                )
            )
            raw_balance = float(bal.get("balance", 0))
            raw_allowance = float(bal.get("allowance", 0))
            actual_shares = raw_balance / 1e6
            actual_allowance = raw_allowance / 1e6
            self._token_balance_cache[side] = (actual_shares, now)

            if actual_shares > 0 and actual_allowance < actual_shares:
                log.warning(
                    f"TOKEN ALLOWANCE LOW | {side.upper()} | "
                    f"balance={actual_shares:.2f} | "
                    f"allowance={actual_allowance:.2f} | "
                    f"market={self.market['question'][:40]} — "
                    f"auto-setting CONDITIONAL allowance for token"
                )
                try:
                    self.client.update_balance_allowance(
                        BalanceAllowanceParams(
                            asset_type=AssetType.CONDITIONAL,
                            token_id=token_id,
                        )
                    )
                    log.info(
                        f"CONDITIONAL allowance updated for "
                        f"{side.upper()} token {token_id[:16]}..."
                    )
                except Exception as ae:
                    log.error(f"Failed to auto-set CONDITIONAL allowance: {ae}")

            return actual_shares
        except Exception as e:
            log.warning(
                f"Could not verify {side.upper()} token balance for "
                f"{self.market['question'][:40]}: {e}"
            )
            return -1

    # ── Order Book ───────────────────────────────────────────────────────────
    def get_order_book(self) -> dict | None:
        """Fetch and merge the YES + NO order books into one combined view.

        In Neg Risk markets, buying YES at price P is equivalent to
        selling NO at (1-P). This merges both books so we see the real
        spread.
        """
        try:
            yes_token = self.market["token_ids"][0]
            ob_yes = self.client.get_order_book(yes_token)

            all_bids: list[tuple[float, float]] = []
            all_asks: list[tuple[float, float]] = []

            for b in ob_yes.bids:
                all_bids.append((float(b.price), float(b.size)))
            for a in ob_yes.asks:
                all_asks.append((float(a.price), float(a.size)))

            if len(self.market["token_ids"]) > 1:
                no_token = self.market["token_ids"][1]
                ob_no = self.client.get_order_book(no_token)

                for a in ob_no.asks:
                    derived_price = round(to_yes_equiv(float(a.price), "no"), 4)
                    if derived_price > 0:
                        all_bids.append((derived_price, float(a.size)))

                for b in ob_no.bids:
                    derived_price = round(to_yes_equiv(float(b.price), "no"), 4)
                    if derived_price < 1:
                        all_asks.append((derived_price, float(b.size)))

            all_bids.sort(key=lambda x: x[0], reverse=True)
            all_asks.sort(key=lambda x: x[0])

            if not all_bids or not all_asks:
                log.warning(
                    f"Empty order book for "
                    f"{self.market['question'][:40]} — skipping"
                )
                return None

            best_bid = all_bids[0][0]
            best_ask = all_asks[0][0]
            spread = best_ask - best_bid

            if spread > MAX_ORDERBOOK_SPREAD:
                log.warning(
                    f"Spread too wide ({spread:.4f} > "
                    f"{MAX_ORDERBOOK_SPREAD}) for "
                    f"{self.market['question'][:40]} — skipping"
                )
                return None

            if spread < 0:
                log.warning(
                    f"Negative spread (bid={best_bid:.4f} > "
                    f"ask={best_ask:.4f}) for "
                    f"{self.market['question'][:40]} — skipping"
                )
                return None

            return {
                "bids": [{"price": p, "size": s} for p, s in all_bids],
                "asks": [{"price": p, "size": s} for p, s in all_asks],
            }

        except Exception as e:
            log.error(
                f"Order book fetch failed for "
                f"{self.market['question'][:40]}: {e}"
            )
            return None

    # ── Order Cancellation ───────────────────────────────────────────────────
    def cancel_order(self, order_id: str, reason: str = "manual") -> bool:
        """Cancel a single order.

        Returns True if the cancel succeeded (or dry-run), False on failure.
        """
        if DRY_RUN:
            log.info(
                f"[DRY RUN] Would cancel | "
                f"id={order_id} | reason={reason}"
            )
            if order_id in self.active_orders:
                del self.active_orders[order_id]
            return True

        try:
            self.client.cancel(order_id)
            log_order_cancelled(order_id, reason)
            if order_id in self.active_orders:
                del self.active_orders[order_id]
            self.invalidate_balance_cache()
            if self._reward_tracker:
                self._reward_tracker.record_order_cancelled(
                    self.market["condition_id"], reason
                )
            get_db().log_order_cancelled(order_id, reason)
            return True
        except Exception as e:
            log.error(f"Failed to cancel order {order_id}: {e}")
            return False

    def cancel_all(
        self, reason: str = "refresh", include_unwinds: bool = False
    ) -> None:
        """Cancel all active orders for this market."""
        for order_id in list(self.active_orders.keys()):
            self.cancel_order(order_id, reason)

        if include_unwinds:
            for order_id in list(self.unwind_orders.keys()):
                try:
                    self.client.cancel(order_id)
                    log_order_cancelled(order_id, f"unwind-{reason}")
                    del self.unwind_orders[order_id]
                except Exception as e:
                    log.error(
                        f"Failed to cancel unwind order {order_id}: {e} "
                        f"— keeping in tracker for retry"
                    )

        if self.active_orders or (include_unwinds and self.unwind_orders):
            log.info(f"Cancelled all for {self.market['question'][:40]}")
        self.active_orders.clear()

    def has_open_obligations(self) -> bool:
        """Check if this manager has unwind orders or unhedged position."""
        if self.unwind_orders:
            return True
        condition_id = self.market["condition_id"]
        for side in ("yes", "no"):
            if self.position_tracker.get_shares(condition_id, side) >= MIN_UNWIND_SHARES:
                return True
        return False

    # ── Full Cycle ───────────────────────────────────────────────────────────
    def run_cycle(self, exchange_orders: list[dict] | None = None) -> None:
        """Run one complete order management cycle.

        Args:
            exchange_orders: Pre-fetched list from get_orders() (passed by
                bot.py). Avoids per-manager API calls.

        Steps:
            1. Detect fills and adopt untracked orders.
            2. Reconcile unwinds (position-based).
            3. Fetch and validate the order book.
            4. Check zones — cancel DANGER and DEAD orders.
            5. Calculate co-best prices.
            6. Cancel stale orders with price drift.
            7. Place fresh orders where needed.
        """
        question = self.market["question"]
        log.debug(f"Running cycle for: {question[:50]}")

        # Step 1: Detect fills + adopt untracked exchange orders
        self.detect_fills(exchange_orders=exchange_orders)

        # Step 2: Reconcile unwinds (position-based)
        self.reconcile_unwinds()

        # Step 3: Cancel active BUY orders on any halted side
        condition_id = self.market["condition_id"]
        for oid in list(self.active_orders.keys()):
            order = self.active_orders[oid]
            if not self.position_tracker.can_quote(condition_id, order.side):
                log.info(
                    f"Cancelling {order.side.upper()} order {oid[:16]}... "
                    f"(position halted — prevent overshoot)"
                )
                if not self.cancel_order(oid, reason="position_halted"):
                    log.warning(
                        f"Failed to cancel halted {order.side.upper()} order "
                        f"{oid[:16]}... — will retry next cycle"
                    )

        # Step 4: Fetch and validate order book
        order_book = self.get_order_book()
        if order_book is None:
            return
        self._last_order_book = order_book  # Cache for Q-score estimation

        best_bid = float(order_book["bids"][0]["price"])
        best_ask = float(order_book["asks"][0]["price"])
        self._cached_best_bid = best_bid
        self._cached_best_ask = best_ask

        log.info(
            f"Market: {question[:45]} | "
            f"bid={best_bid:.4f} | ask={best_ask:.4f}"
        )

        # Step 5: Stop-loss check
        self.check_stop_loss(best_bid, best_ask)

        # Step 6: Check zones and cancel bad orders
        for order_id in list(self.active_orders.keys()):
            zone = self.check_order_zone(order_id, best_bid, best_ask)
            if zone in ("DANGER", "DEAD"):
                if not self.cancel_order(order_id, reason=zone.lower()):
                    log.warning(
                        f"Failed to cancel {zone} order {order_id[:16]}... "
                        f"— will retry next cycle"
                    )

        # Step 7: Calculate co-best prices
        our_bid, our_ask = self.calculate_order_prices(order_book)
        if our_bid is None:
            return

        # Step 7b: Calculate dynamic order size (M4)
        dynamic_budget = self.calculate_dynamic_size(order_book)

        # Step 8: Cancel stale orders whose price has drifted from optimal
        tick = self.market.get("tick_size", 0.01)
        drift_threshold = MIN_PRICE_DRIFT_TICKS * tick
        for order_id in list(self.active_orders.keys()):
            order = self.active_orders[order_id]
            optimal_price = our_bid if order.side == "yes" else our_ask
            if abs(order.price - optimal_price) >= drift_threshold:
                log.info(
                    f"Price drifted {abs(order.price - optimal_price)/tick:.0f} ticks: "
                    f"{order.side.upper()} {order.price:.4f} -> {optimal_price:.4f} "
                    f"— refreshing"
                )
                if not self.cancel_order(order_id, reason="price_refresh"):
                    log.warning(
                        f"Failed to cancel stale {order.side.upper()} order "
                        f"{order_id[:16]}... — will retry next cycle"
                    )

        # Step 9: Place fresh orders where needed
        active_sides = {o.side for o in self.active_orders.values()}
        needs_yes = "yes" not in active_sides
        needs_no = "no" not in active_sides

        if needs_yes and self.has_unhedged_position("yes"):
            log.info(
                f"Blocking YES BUY — unhedged position | "
                f"market={question[:40]}"
            )
            needs_yes = False
        if needs_no and self.has_unhedged_position("no"):
            log.info(
                f"Blocking NO BUY — unhedged position | "
                f"market={question[:40]}"
            )
            needs_no = False

        # Soft opposite-side guard: block when BOTH sides have inventory
        yes_inv = self.position_tracker.get_shares(condition_id, "yes")
        no_inv = self.position_tracker.get_shares(condition_id, "no")
        if yes_inv >= MIN_UNWIND_SHARES and no_inv >= MIN_UNWIND_SHARES:
            if needs_yes:
                log.info(
                    f"Blocking YES BUY — dual position "
                    f"(YES={yes_inv:.0f}, NO={no_inv:.0f}) | "
                    f"market={question[:40]}"
                )
                needs_yes = False
            if needs_no:
                log.info(
                    f"Blocking NO BUY — dual position "
                    f"(YES={yes_inv:.0f}, NO={no_inv:.0f}) | "
                    f"market={question[:40]}"
                )
                needs_no = False

        if not needs_yes and not needs_no:
            log.debug("Both sides covered or blocked — holding")
            return

        if needs_yes:
            self.place_order("yes", our_bid, budget_usd=dynamic_budget)
        if needs_no:
            self.place_order("no", our_ask, budget_usd=dynamic_budget)
