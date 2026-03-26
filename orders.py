"""
Order management for the Polymarket market-making bot.

Handles order placement, cancellation, fill detection, and the core
quoting strategy. Orders are placed behind a configurable liquidity
buffer to minimise adverse fill risk.
"""

import logging
import time as _time
from py_clob_client.clob_types import OrderArgs, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY, SELL
from config import (
    ORDER_SIZE, MAX_ORDER_BUDGET, ORDER_REFRESH_SECS,
    DANGER_ZONE_CENTS, DEAD_ZONE_BUFFER,
    MAX_ORDER_FAILURES, DRY_RUN, MAX_ORDERBOOK_SPREAD,
    MIN_LIQUIDITY_BUFFER, MIN_UNWIND_SHARES,
    UNWIND_DECAY_INTERVAL_SECS, UNWIND_DECAY_TICKS,
    MIN_SELL_PRICE, STOP_LOSS_PCT, MIN_STOP_LOSS_USD, STOP_LOSS_MIN_PRICE,
    UNWIND_ACCEL_TIERS,
    UNWIND_AGE_ACCEL_HOURS, UNWIND_AGE_ACCEL_TICKS,
    UNWIND_AGE_MAX_HOURS, UNWIND_AGE_MAX_TICKS,
    CHEAP_TOKEN_THRESHOLD, CHEAP_TOKEN_SCALE,
    SPREAD_EDGE_PCT, MIN_EDGE_TICKS, USE_SPREAD_PRICING,
    MIN_PRICE_DRIFT_TICKS,
    INVENTORY_SKEW_ENABLED, INVENTORY_SKEW_TICKS, INVENTORY_SKEW_THRESHOLD,
    POST_FILL_COOLDOWN_SECS, POST_FILL_WIDEN_TICKS,
    MIN_BID_DEPTH_USD,
)
from alerts import (
    alert_order_failure, alert_danger_zone,
    alert_fill, alert_unwind, alert_merge_needed,
    log_order_placed, log_order_cancelled,
)
from price import to_clob, to_yes_equiv

log = logging.getLogger(__name__)


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
        self._depleted_until: float = 0  # timestamp — skip orders until then

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
        """Check if we likely have enough balance for this order.

        Returns True if we can't determine (don't block on errors).
        Returns False if balance is known to be insufficient or
        we're in a depleted cooldown from a recent exchange rejection.
        """
        now = _time.time()
        if now < self._depleted_until:
            return False
        balance = self.get_balance()
        if balance is None:
            return True  # Don't block on API errors
        return est_cost <= balance

    def mark_depleted(self, cooldown_secs: float = 30.0) -> None:
        """Mark balance as depleted — skip all order attempts for cooldown.

        Called when the exchange rejects with 'not enough balance / allowance'.
        All managers sharing this gate will skip orders until cooldown expires
        or invalidate() is called.
        """
        self._depleted_until = _time.time() + cooldown_secs
        log.info(
            f"Balance gate DEPLETED — skipping new orders for "
            f"{cooldown_secs:.0f}s"
        )

    def invalidate(self) -> None:
        """Force a fresh balance check and clear depleted state.

        Called when collateral is freed (order cancelled/filled).
        """
        self._cache_time = 0
        self._depleted_until = 0

    @property
    def is_depleted(self) -> bool:
        return _time.time() < self._depleted_until


class OrderManager:
    """Manages order lifecycle for a single market.

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
        self.active_orders: dict[str, dict] = {}
        self.unwind_orders: dict[str, dict] = {}  # SELL orders to offload inventory
        self.failure_counts: dict[str, int] = {"yes": 0, "no": 0}
        self._balance_cache: float | None = None
        self._balance_cache_time: float = 0
        # Short-lived cache for token balances (avoids hitting API
        # multiple times per cycle for the same token)
        self._token_balance_cache: dict[str, tuple[float, float]] = {}  # side → (balance, timestamp)
        # Post-fill cooldown: track last BUY fill time per side
        self._last_fill_time: dict[str, float] = {"yes": 0.0, "no": 0.0}

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

    def _last_market_bid(self, side: str) -> float:
        """Return the last known market bid for the given side."""
        bid = getattr(self, "_cached_best_bid", 0)
        ask = getattr(self, "_cached_best_ask", 1)
        if side == "yes":
            return bid
        # NO market bid = complement of YES best ask
        return max(MIN_SELL_PRICE, round(to_clob(ask, "no"), 4))

    def refresh_cached_book(self) -> None:
        """Fetch order book and update cached bid/ask for decay calculations.

        Used for unwind-only markets that don't go through run_cycle()
        (which normally fetches the book). Without this, _last_market_bid()
        returns defaults (bid=0, ask=1) causing wrong acceleration.
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
        """Round a price DOWN to the nearest valid tick (floor).

        Used for SELL unwind orders so we don't overprice relative to
        the acquisition cost.  E.g. acquired at 0.235 on a 0.01 tick
        → SELL at 0.23 (not 0.24).
        """
        import math
        tick = self.market.get("tick_size", 0.01)
        if tick <= 0:
            tick = 0.01
        decimal_places = len(str(tick).rstrip("0").split(".")[-1])
        floored = math.floor(price / tick) * tick
        return round(floored, decimal_places)

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
        if self.balance_gate:
            self.balance_gate.invalidate()

    # ── Token Balance Verification ────────────────────────────────────────────
    def verify_token_balance(self, side: str) -> float:
        """Query actual on-exchange token balance for a side.

        Uses the CONDITIONAL asset type to check how many tokens
        we actually hold, regardless of what positions.json says.
        Results are cached for 15 seconds to avoid duplicate API calls
        within the same cycle (reconcile_unwinds + has_unhedged_position).

        Args:
            side: "yes" or "no".

        Returns:
            Actual token balance (in shares), or -1 if the check fails
            (so we don't block operations on API errors).
        """
        # Check short-lived cache first
        now = _time.time()
        if side in self._token_balance_cache:
            cached_bal, cached_at = self._token_balance_cache[side]
            if now - cached_at < 15:  # 15-second cache
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
            # Conditional tokens use 6 decimal places (like USDC)
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
            return -1  # Unknown — don't block

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

                # NO asks → derived YES bids (NO CLOB price → YES-equiv)
                for a in ob_no.asks:
                    derived_price = round(to_yes_equiv(float(a.price), "no"), 4)
                    if derived_price > 0:
                        all_bids.append((derived_price, float(a.size)))

                # NO bids → derived YES asks (NO CLOB price → YES-equiv)
                for b in ob_no.bids:
                    derived_price = round(to_yes_equiv(float(b.price), "no"), 4)
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
        """Calculate bid and ask prices using the active pricing strategy.

        Two strategies available (controlled by USE_SPREAD_PRICING):

        1. **Spread-relative** (default, new): Place orders at a configurable
           fraction of the reward window from the midpoint.  This ensures
           orders land inside the reward window (earning rewards) while
           capturing meaningful spread.  Inventory skew tilts quotes to
           naturally unwind positions through the spread.

        2. **Liquidity-buffer** (legacy): Walk the book to find the price
           level with $2000 of cumulative volume in front.

        Args:
            order_book: Dict with 'bids' and 'asks' lists.

        Returns:
            (our_bid, our_ask) or (None, None) if conditions are not met.
        """
        try:
            tick = self.market.get("tick_size", 0.01)
            max_spread = self.market["max_spread"]
            best_bid = float(order_book["bids"][0]["price"])
            best_ask = float(order_book["asks"][0]["price"])
            midpoint = (best_bid + best_ask) / 2

            if USE_SPREAD_PRICING:
                our_bid, our_ask = self._spread_relative_prices(
                    best_bid, best_ask, midpoint, max_spread, tick, order_book
                )
            else:
                our_bid, our_ask = self._liquidity_buffer_prices(
                    order_book, midpoint, max_spread
                )

            if our_bid is None or our_ask is None:
                return None, None

            # ── Inventory skew ──────────────────────────────────────────
            if INVENTORY_SKEW_ENABLED:
                our_bid, our_ask = self._apply_inventory_skew(
                    our_bid, our_ask, tick, midpoint, max_spread
                )

            # ── Post-fill cooldown ─────────────────────────────────────
            # After a BUY fills, widen quotes on that side to avoid
            # repeated adverse selection.  Clamped to reward window.
            now = _time.time()
            if now - self._last_fill_time.get("yes", 0) < POST_FILL_COOLDOWN_SECS:
                widened = self.round_to_tick(our_bid - POST_FILL_WIDEN_TICKS * tick)
                # Clamp: don't push outside reward window
                min_bid = self.round_to_tick(midpoint - max_spread)
                our_bid = max(widened, min_bid)
                log.info(
                    f"POST-FILL COOLDOWN | YES bid widened to {our_bid:.4f} | "
                    f"market={self.market['question'][:40]}"
                )
            if now - self._last_fill_time.get("no", 0) < POST_FILL_COOLDOWN_SECS:
                widened = self.round_to_tick(our_ask + POST_FILL_WIDEN_TICKS * tick)
                # Clamp: don't push outside reward window
                max_ask = self.round_to_tick(midpoint + max_spread)
                our_ask = min(widened, max_ask)
                log.info(
                    f"POST-FILL COOLDOWN | NO ask widened to {our_ask:.4f} | "
                    f"market={self.market['question'][:40]}"
                )

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
                f"bid={our_bid:.4f} | ask={our_ask:.4f} | "
                f"strategy={'spread' if USE_SPREAD_PRICING else 'buffer'}"
            )
            return our_bid, our_ask

        except Exception as e:
            log.error(f"Price calculation failed: {e}")
            return None, None

    def _spread_relative_prices(
        self, best_bid: float, best_ask: float,
        midpoint: float, max_spread: float, tick: float,
        order_book: dict,
    ) -> tuple[float | None, float | None]:
        """Spread-relative pricing: place orders inside the reward window.

        Strategy: our bid = midpoint - (max_spread * SPREAD_EDGE_PCT)
                  our ask = midpoint + (max_spread * SPREAD_EDGE_PCT)

        This earns rewards (inside max_spread) while capturing spread
        and staying far enough from the midpoint to avoid being adversely
        selected on every small price move.

        Also checks bid-side depth — if the book is too thin to unwind,
        we skip this market for this cycle.
        """
        question = self.market["question"]

        # Check bid-side depth — avoid markets where we can't sell
        bid_depth = sum(
            float(level["price"]) * float(level["size"])
            for level in order_book["bids"][:5]
        )
        if bid_depth < MIN_BID_DEPTH_USD:
            log.warning(
                f"Bid depth too thin (${bid_depth:.0f} < "
                f"${MIN_BID_DEPTH_USD:.0f}) for {question[:40]} — "
                f"skipping to avoid unsellable inventory"
            )
            return None, None

        # Core calculation: place at SPREAD_EDGE_PCT of max_spread from mid
        edge_offset = max_spread * SPREAD_EDGE_PCT
        our_bid = self.round_to_tick(midpoint - edge_offset)
        our_ask = self.round_to_tick(midpoint + edge_offset)

        # Enforce minimum distance from best bid/ask (avoid being first in line)
        min_gap = MIN_EDGE_TICKS * tick
        if our_bid > best_bid - min_gap:
            our_bid = self.round_to_tick(best_bid - min_gap)
        if our_ask < best_ask + min_gap:
            our_ask = self.round_to_tick(best_ask + min_gap)

        # Verify still inside reward window
        if abs(our_bid - midpoint) > max_spread:
            log.warning(
                f"Bid would fall outside reward window "
                f"(bid={our_bid:.4f}, mid={midpoint:.4f}, max_spread={max_spread}) "
                f"for {question[:40]} — skipping bid"
            )
            return None, None
        if abs(our_ask - midpoint) > max_spread:
            log.warning(
                f"Ask would fall outside reward window "
                f"(ask={our_ask:.4f}, mid={midpoint:.4f}, max_spread={max_spread}) "
                f"for {question[:40]} — skipping ask"
            )
            return None, None

        log.debug(
            f"Spread pricing | mid={midpoint:.4f} | edge={edge_offset:.4f} | "
            f"bid={our_bid:.4f} | ask={our_ask:.4f} | "
            f"bid_depth=${bid_depth:.0f}"
        )
        return our_bid, our_ask

    def _liquidity_buffer_prices(
        self, order_book: dict, midpoint: float, max_spread: float,
    ) -> tuple[float | None, float | None]:
        """Legacy liquidity-buffer pricing (kept as fallback).

        Walk the book to find price level with MIN_LIQUIDITY_BUFFER of
        cumulative volume in front.
        """
        our_bid = None
        cumulative = 0.0
        for level in order_book["bids"]:
            price = float(level["price"])
            size = float(level["size"])
            cumulative += price * size
            if cumulative >= MIN_LIQUIDITY_BUFFER:
                our_bid = self.round_to_tick(price)
                break

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

        if (abs(our_bid - midpoint) > max_spread
                or abs(our_ask - midpoint) > max_spread):
            log.warning(
                f"Orders would fall outside reward window "
                f"(max_spread={max_spread}) — skipping"
            )
            return None, None

        return our_bid, our_ask

    def _apply_inventory_skew(
        self, our_bid: float, our_ask: float,
        tick: float, midpoint: float, max_spread: float,
    ) -> tuple[float, float]:
        """Skew quotes based on current inventory to unwind naturally.

        When holding YES inventory: tighten the ask (sell YES faster),
        widen the bid (buy YES slower).
        When holding NO inventory: tighten the bid (which is the NO sell
        equivalent), widen the ask.

        The skew amount scales with inventory size: larger positions get
        more aggressive skew.
        """
        condition_id = self.market["condition_id"]
        yes_usd = self.position_tracker.get_position(condition_id, "yes")
        no_usd = self.position_tracker.get_position(condition_id, "no")

        if yes_usd < INVENTORY_SKEW_THRESHOLD and no_usd < INVENTORY_SKEW_THRESHOLD:
            return our_bid, our_ask

        # Calculate skew steps — each $100 of inventory = 1 step
        yes_steps = int(yes_usd / 100) if yes_usd >= INVENTORY_SKEW_THRESHOLD else 0
        no_steps = int(no_usd / 100) if no_usd >= INVENTORY_SKEW_THRESHOLD else 0
        net_skew = yes_steps - no_steps  # positive = long YES, negative = long NO

        skew_amount = abs(net_skew) * INVENTORY_SKEW_TICKS * tick

        if net_skew > 0:
            # Long YES → want to SELL YES → tighten ask, widen bid
            new_ask = self.round_to_tick(our_ask - skew_amount)
            new_bid = self.round_to_tick(our_bid - skew_amount)
            # Don't cross midpoint or push outside reward window
            new_ask = max(new_ask, self.round_to_tick(midpoint + tick))
            new_bid = max(new_bid, self.round_to_tick(midpoint - max_spread))
            log.info(
                f"INVENTORY SKEW | long YES ${yes_usd:.0f} | "
                f"ask {our_ask:.4f}→{new_ask:.4f} | "
                f"bid {our_bid:.4f}→{new_bid:.4f} | "
                f"market={self.market['question'][:40]}"
            )
            our_ask, our_bid = new_ask, new_bid
        elif net_skew < 0:
            # Long NO → want to SELL NO → in YES-equiv terms, tighten bid, widen ask
            new_bid = self.round_to_tick(our_bid + skew_amount)
            new_ask = self.round_to_tick(our_ask + skew_amount)
            # Don't cross midpoint or push outside reward window
            new_bid = min(new_bid, self.round_to_tick(midpoint - tick))
            new_ask = min(new_ask, self.round_to_tick(midpoint + max_spread))
            log.info(
                f"INVENTORY SKEW | long NO ${no_usd:.0f} | "
                f"bid {our_bid:.4f}→{new_bid:.4f} | "
                f"ask {our_ask:.4f}→{new_ask:.4f} | "
                f"market={self.market['question'][:40]}"
            )
            our_bid, our_ask = new_bid, new_ask

        return our_bid, our_ask

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
        effective_order_size = ORDER_SIZE
        if clob_price < CHEAP_TOKEN_THRESHOLD:
            effective_order_size = ORDER_SIZE * CHEAP_TOKEN_SCALE
        budget_shares = effective_order_size / clob_price
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

            # Extract POST response orderID as fallback identifier
            post_order_id = None
            if isinstance(response, dict):
                post_order_id = response.get("orderID")

            # Try to find the exchange-assigned order ID by matching
            # asset + price + side in the open orders list.
            exchange_id = self._find_exchange_order_id(
                token_id, str(clob_price), clob_side
            )

            # Use exchange ID if found, otherwise fall back to POST orderID.
            # An untracked order is the worst outcome — it can get filled
            # without the bot ever knowing, leaving inventory with no SELL.
            order_id = exchange_id or post_order_id

            if order_id:
                self.active_orders[order_id] = {
                    "side": side,
                    "price": price,
                    "size": float(size),
                    "original_size": float(size),
                    "placed_at": _time.time(),
                    "from_post_response": exchange_id is None,
                }
                self.failure_counts[side] = 0
                self.invalidate_balance_cache()
                if exchange_id is None:
                    log.warning(
                        f"Tracking {side.upper()} order via POST orderID "
                        f"(exchange lookup failed) — id={order_id[:16]}..."
                    )
                log_order_placed(side.upper(), price, size, question, order_id)
                return order_id
            else:
                log.error(
                    f"Order placed but BOTH exchange lookup AND POST response "
                    f"returned no ID for {side.upper()} at {price:.4f} on "
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
                    self.balance_gate.mark_depleted(cooldown_secs=ORDER_REFRESH_SECS)

            alert_order_failure(
                question, side.upper(), str(e),
                self.failure_counts[side],
            )
            return None

    def _find_exchange_order_id(
        self, token_id: str, price: str, side: str
    ) -> str | None:
        """Fetch open orders and find the exchange ID for a just-placed order.

        Retries twice with increasing delays to handle exchange propagation
        lag.  Uses tick-sized tolerance for price matching to avoid floating
        point mismatches.

        Args:
            token_id: The token/asset ID of the order.
            price: The order price as a string.
            side: BUY or SELL constant.

        Returns:
            The exchange order ID, or None if not found.
        """
        tick = self.market.get("tick_size", 0.01)
        price_tolerance = float(tick) / 2  # Half a tick

        for attempt, delay in enumerate([0.5, 1.5, 3.0]):
            _time.sleep(delay)
            try:
                open_orders = self.client.get_orders()
                if not open_orders:
                    continue
                for o in open_orders:
                    o_id = o["id"]
                    if o_id in self.active_orders or o_id in self.unwind_orders:
                        continue
                    if (o["asset_id"] == token_id
                            and abs(float(o["price"]) - float(price)) < price_tolerance
                            and o["side"] == side):
                        return o_id
            except Exception as e:
                log.debug(
                    f"Exchange order lookup attempt {attempt + 1}/3 failed: {e}"
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

    # ── Inventory Unwinding ─────────────────────────────────────────────────
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
        # This catches stale tracker data and helps diagnose approval issues.
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

            # Extract POST orderID as fallback
            post_order_id = None
            if isinstance(response, dict):
                post_order_id = response.get("orderID")

            exchange_id = self._find_exchange_order_id(
                token_id, str(clob_price), SELL
            )

            order_id = exchange_id or post_order_id

            if order_id:
                # base_clob_price: the VWAP-based sell price (before decay).
                # Used to calculate how much the price has decayed.
                if side == "yes":
                    base = self.round_down_to_tick(fill_price)
                else:
                    base = self.round_down_to_tick(to_clob(fill_price, "no"))

                self.unwind_orders[order_id] = {
                    "side": side,
                    "price": fill_price,
                    "clob_price": clob_price,
                    "size": float(fill_size),
                    "placed_at": _time.time(),
                    "created_at": created_at_override or _time.time(),
                    "base_clob_price": base,
                    "from_post_response": exchange_id is None,
                }

                if exchange_id is None:
                    log.warning(
                        f"Tracking unwind via POST orderID "
                        f"(exchange lookup failed) — id={order_id[:16]}..."
                    )
                log.info(
                    f"UNWIND ORDER PLACED | SELL {side.upper()} | "
                    f"price={clob_price:.4f} | size={fill_size:.2f} | "
                    f"market={question[:40]} | id={order_id}"
                )
                return order_id
            else:
                log.error(
                    f"Unwind order placed but BOTH exchange lookup AND POST "
                    f"response returned no ID for {side.upper()} on "
                    f"{question[:40]} — will be reconciled next cycle"
                )
                return None

        except Exception as e:
            error_msg = str(e).lower()
            if "not enough balance" in error_msg or "allowance" in error_msg:
                log.warning(
                    f"SELL rejected (balance/allowance) for {side.upper()} "
                    f"on {question[:40]} — attempting to fix CONDITIONAL allowance"
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
                        f"{token_id[:16]}... — waiting for confirmation then retrying"
                    )
                    # Wait for the allowance tx to confirm on-chain
                    _time.sleep(5)
                    # Retry once after fixing allowance
                    order_args = OrderArgs(
                        token_id=token_id,
                        price=clob_price,
                        size=float(fill_size),
                        side=SELL,
                    )
                    response = self.client.create_and_post_order(order_args)

                    if isinstance(response, dict) and not response.get("success", True):
                        raise Exception(
                            f"Retry rejected: {response.get('errorMsg', response)}"
                        )

                    post_order_id = None
                    if isinstance(response, dict):
                        post_order_id = response.get("orderID")

                    exchange_id = self._find_exchange_order_id(
                        token_id, str(clob_price), SELL
                    )
                    order_id = exchange_id or post_order_id
                    if order_id:
                        if side == "yes":
                            base = self.round_down_to_tick(fill_price)
                        else:
                            base = self.round_down_to_tick(to_clob(fill_price, "no"))
                        self.unwind_orders[order_id] = {
                            "side": side,
                            "price": fill_price,
                            "clob_price": clob_price,
                            "size": float(fill_size),
                            "placed_at": _time.time(),
                            "created_at": created_at_override or _time.time(),
                            "base_clob_price": base,
                            "from_post_response": exchange_id is None,
                        }
                        log.info(
                            f"UNWIND ORDER PLACED (after allowance fix) | "
                            f"SELL {side.upper()} | price={clob_price:.4f} | "
                            f"size={fill_size:.2f} | market={question[:40]} | "
                            f"id={order_id}"
                        )
                        return order_id
                except Exception as retry_err:
                    log.warning(
                        f"SELL retry #1 failed for {side.upper()} "
                        f"on {question[:40]}: {retry_err} — waiting 10s for retry #2"
                    )
                    # Second retry with longer wait
                    try:
                        _time.sleep(10)
                        order_args = OrderArgs(
                            token_id=token_id,
                            price=clob_price,
                            size=float(fill_size),
                            side=SELL,
                        )
                        response = self.client.create_and_post_order(order_args)
                        if isinstance(response, dict) and not response.get("success", True):
                            raise Exception(
                                f"Retry #2 rejected: {response.get('errorMsg', response)}"
                            )
                        post_order_id = None
                        if isinstance(response, dict):
                            post_order_id = response.get("orderID")
                        exchange_id = self._find_exchange_order_id(
                            token_id, str(clob_price), SELL
                        )
                        order_id = exchange_id or post_order_id
                        if order_id:
                            if side == "yes":
                                base = self.round_down_to_tick(fill_price)
                            else:
                                base = self.round_down_to_tick(to_clob(fill_price, "no"))
                            self.unwind_orders[order_id] = {
                                "side": side,
                                "price": fill_price,
                                "clob_price": clob_price,
                                "size": float(fill_size),
                                "placed_at": _time.time(),
                                "created_at": created_at_override or _time.time(),
                                "base_clob_price": base,
                                "from_post_response": exchange_id is None,
                            }
                            log.info(
                                f"UNWIND ORDER PLACED (retry #2) | "
                                f"SELL {side.upper()} | price={clob_price:.4f} | "
                                f"size={fill_size:.2f} | market={question[:40]} | "
                                f"id={order_id}"
                            )
                            return order_id
                    except Exception as retry2_err:
                        log.error(
                            f"SELL retry #2 also failed for {side.upper()} "
                            f"on {question[:40]}: {retry2_err}"
                        )
            else:
                log.error(
                    f"Failed to place unwind order for {side.upper()} "
                    f"on {question[:40]}: {e}"
                )
            return None

    def _tiered_decay_ticks(
        self, vwap_clob: float, side: str, created_at: float | None = None,
    ) -> int:
        """Calculate decay ticks based on loss severity AND position age.

        Two independent accelerators stack:
        1. Loss tiers: 5% → 2x, 10% → 3x, 15% → 4x
        2. Age: >24h → +2 ticks, >48h → +4 ticks

        A position at 3% loss for 30 hours gets: 1 (base) + 2 (age) = 3 ticks.
        A position at 12% loss for 30 hours gets: 3 (tier) + 2 (age) = 5 ticks.
        This frees trapped capital without panic-selling.

        Args:
            vwap_clob: Our VWAP cost in CLOB price terms.
            side: "yes" or "no".
            created_at: When the unwind was first created (epoch seconds).

        Returns:
            Number of ticks to decay per interval.
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

        Key improvement: even when the tracker shows 0 shares, we verify
        against the exchange. This catches fills from orders where
        _find_exchange_order_id failed (order placed but not tracked).
        """
        condition_id = self.market["condition_id"]
        actual_balances: dict[str, float] = {}

        # ── Phase 1: Verify actual balances for BOTH sides ────────────────
        for side in ("yes", "no"):
            position_shares = self.position_tracker.get_shares(condition_id, side)
            actual_balance = self.verify_token_balance(side)
            actual_balances[side] = actual_balance

            if actual_balance < 0:
                # API check failed — use tracker as-is, don't block
                continue

            if actual_balance >= MIN_UNWIND_SHARES and position_shares < MIN_UNWIND_SHARES:
                # DISCOVERY: Exchange has tokens the tracker doesn't know about.
                # This happens when _find_exchange_order_id fails and the fill
                # is never recorded. Use current market price as estimate.
                #
                # IMPORTANT: avg_price must always be stored as YES-equivalent
                # because reconcile_unwinds computes the CLOB sell price via:
                #   YES → sell at avg_price
                #   NO  → sell at (1 - avg_price)
                # Using `yes_price` for both sides keeps the convention consistent
                # with how detect_fills records prices (order["price"] = YES-side
                # price for both YES and NO orders).
                yes_price = self.market.get("yes_price") or 0.50
                est_price = yes_price  # Always YES-equivalent for both sides
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
                # Tracker says we have shares, exchange says we don't
                # → stale data (manual close, external sale, etc.)
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
                # Mismatch — trust the exchange, correct tracker
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
        # If we hold both YES and NO, merging min(yes, no) pairs returns
        # $1 per pair as USDC — far more capital-efficient than selling both.
        yes_shares = self.position_tracker.get_shares(condition_id, "yes")
        no_shares = self.position_tracker.get_shares(condition_id, "no")
        if yes_shares >= MIN_UNWIND_SHARES and no_shares >= MIN_UNWIND_SHARES:
            mergeable = min(yes_shares, no_shares)
            freed_usd = mergeable  # Each YES+NO pair = $1
            log.info(
                f"MERGE OPPORTUNITY | "
                f"YES={yes_shares:.2f} | NO={no_shares:.2f} | "
                f"mergeable={mergeable:.2f} pairs | "
                f"would_free=${freed_usd:.2f} USDC | "
                f"market={self.market['question'][:40]}"
            )
            # Attempt automatic merge
            merged = self._try_merge_positions(condition_id, mergeable)
            if merged:
                # Merge succeeded — reduce both sides in tracker
                self.position_tracker.record_unwind(
                    condition_id, "yes", mergeable,
                    self.position_tracker.get_avg_price(condition_id, "yes"),
                )
                self.position_tracker.record_unwind(
                    condition_id, "no", mergeable,
                    self.position_tracker.get_avg_price(condition_id, "no"),
                )
                self.invalidate_balance_cache()
                # Re-read shares after merge
                yes_shares = self.position_tracker.get_shares(condition_id, "yes")
                no_shares = self.position_tracker.get_shares(condition_id, "no")

        # ── Phase 3: Consolidate & place unwind orders (with decay) ────────
        # Sell orders start at VWAP acquisition cost. Over time, the sell
        # price decays by 1 tick per UNWIND_DECAY_INTERVAL_SECS to ensure
        # positions eventually unwind rather than sitting forever.
        now = _time.time()
        for side in ("yes", "no"):
            position_shares = self.position_tracker.get_shares(condition_id, side)
            avg_price = self.position_tracker.get_avg_price(condition_id, side)

            if position_shares < MIN_UNWIND_SHARES or avg_price <= 0:
                continue

            tick = self.market.get("tick_size", 0.01)

            # Calculate current base clob price from VWAP
            vwap_clob = self.round_down_to_tick(to_clob(avg_price, side))

            # ── Inline spread capture: sell above VWAP if market is favorable ──
            # Uses cached best bid/ask from this cycle — zero extra API calls.
            market_bid = self._last_market_bid(side)
            if market_bid > vwap_clob and market_bid > MIN_SELL_PRICE:
                profit_pct = (market_bid - vwap_clob) / vwap_clob if vwap_clob > 0 else 0
                has_unwind = any(u["side"] == side for u in self.unwind_orders.values())
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
                    continue  # Skip normal decay logic — selling at profit

            # Check existing unwind orders — are they at the right price?
            stale_orders: list[str] = []
            covered_shares = 0.0
            for oid, uorder in self.unwind_orders.items():
                if uorder["side"] != side:
                    continue
                covered_shares += uorder["size"]

                # Calculate expected decayed price for this order.
                # Accelerate when position is significantly underwater.
                base = uorder.get("base_clob_price", uorder.get("clob_price", vwap_clob))
                created = uorder.get("created_at", uorder.get("placed_at", now))
                elapsed = now - created
                check_ticks = self._tiered_decay_ticks(vwap_clob, side, created_at=created)
                decay_intervals = int(elapsed // UNWIND_DECAY_INTERVAL_SECS)
                decay_amount = decay_intervals * check_ticks * tick
                expected_clob = max(MIN_SELL_PRICE, base - decay_amount)

                # Also check if VWAP changed (new fills shifted the average)
                if abs(base - vwap_clob) >= tick:
                    # VWAP changed — need to rebase
                    log.info(
                        f"VWAP shifted | {side.upper()} | "
                        f"old_base={base:.4f} → new_vwap={vwap_clob:.4f} | "
                        f"market={self.market['question'][:40]}"
                    )
                    stale_orders.append(oid)
                elif uorder.get("clob_price", 0) > expected_clob + tick * 0.5:
                    # Price needs to decay further (only lower, never raise).
                    # This preserves stop-loss sells which are already below
                    # the decayed price.
                    log.info(
                        f"PRICE DECAY | {side.upper()} | "
                        f"current={uorder.get('clob_price', 0):.4f} → "
                        f"decayed={expected_clob:.4f} | "
                        f"age={elapsed/60:.0f}min | "
                        f"market={self.market['question'][:40]}"
                    )
                    stale_orders.append(oid)

            # Log when sell order is held (not yet stale) for visibility
            if not stale_orders and covered_shares >= MIN_UNWIND_SHARES:
                for oid, uorder in self.unwind_orders.items():
                    if uorder["side"] != side:
                        continue
                    _elapsed = now - uorder.get("created_at", now)
                    _next = UNWIND_DECAY_INTERVAL_SECS - (
                        _elapsed % UNWIND_DECAY_INTERVAL_SECS
                    )
                    log.info(
                        f"UNWIND HOLD | {side.upper()} | "
                        f"price={uorder.get('clob_price', 0):.4f} | "
                        f"age={_elapsed/60:.1f}min | "
                        f"next_decay_in={_next:.0f}s | "
                        f"market={self.market['question'][:40]}"
                    )

            # Capture oldest created_at from stale orders BEFORE deleting
            # so we can preserve the decay clock on the replacement order.
            oldest_created = now
            if stale_orders:
                for oid in stale_orders:
                    uo = self.unwind_orders.get(oid, {})
                    c = uo.get("created_at", uo.get("placed_at", now))
                    oldest_created = min(oldest_created, c)

                stale_prices = [
                    f"{self.unwind_orders[oid].get('clob_price', 0):.4f}"
                    for oid in stale_orders
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
                stale_orders = cancelled_oids  # Only count successfully cancelled

                # Recalculate covered after cancellations
                covered_shares = 0.0
                for uorder in self.unwind_orders.values():
                    if uorder["side"] == side:
                        covered_shares += uorder["size"]

            unhedged = position_shares - covered_shares
            if unhedged < MIN_UNWIND_SHARES:
                continue  # Fully covered

            actual = actual_balances.get(side, -1)

            # Determine the created_at for the replacement order:
            # 1. If we cancelled stale orders, carry forward their oldest created_at
            # 2. If there are other unwinds on this side, use oldest of those
            # 3. Otherwise this is a new position — start fresh
            carry_created = oldest_created
            for uorder in self.unwind_orders.values():
                if uorder["side"] == side:
                    c = uorder.get("created_at", now)
                    carry_created = min(carry_created, c)

            # Calculate decay from the carried creation time.
            # Graduated decay acceleration based on loss severity.
            # Replaces the old 5x panic multiplier with tiered steps.
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

        Args:
            best_bid: Current best YES bid price.
            best_ask: Current best YES ask price.
        """
        condition_id = self.market["condition_id"]
        tick = self.market.get("tick_size", 0.01)

        for side in ("yes", "no"):
            position_shares = self.position_tracker.get_shares(condition_id, side)
            avg_price = self.position_tracker.get_avg_price(condition_id, side)

            if position_shares < MIN_UNWIND_SHARES or avg_price <= 0:
                continue

            # Calculate our cost and current market value per share
            our_cost = self.round_down_to_tick(to_clob(avg_price, side))
            if side == "yes":
                market_bid = best_bid  # what we'd get selling YES
            else:
                # NO token value ≈ complement of YES ask
                market_bid = max(MIN_SELL_PRICE, round(to_clob(best_ask, "no"), 4))

            if our_cost <= 0:
                continue

            unrealized_loss_pct = (our_cost - market_bid) / our_cost
            unrealized_loss_usd = (our_cost - market_bid) * position_shares

            # Skip stop-loss on cheap tokens — decay handles them better
            # because small absolute price moves cause huge % swings
            if our_cost < STOP_LOSS_MIN_PRICE:
                continue

            if unrealized_loss_pct < STOP_LOSS_PCT or unrealized_loss_usd < MIN_STOP_LOSS_USD:
                continue

            # ── STOP-LOSS TRIGGERED ──────────────────────────────────
            # Sell at market to prevent further damage
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

            # Cancel existing unwinds for this side — replacing with market sell
            all_cancelled = True
            for oid in list(self.unwind_orders.keys()):
                if self.unwind_orders[oid]["side"] == side:
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

    def _try_merge_positions(
        self, condition_id: str, amount: float
    ) -> bool:
        """Attempt to merge YES+NO token pairs back into USDC.

        On Polymarket, each YES+NO pair = $1 USDC. Merging avoids the
        need to sell both sides separately (which requires counterparties
        and leaves capital locked in open orders).

        Uses the CLOB API's merge endpoint if available, otherwise
        logs the opportunity for manual action.

        Args:
            condition_id: Market condition ID.
            amount: Number of pairs to merge (shares).

        Returns:
            True if merge succeeded, False otherwise.
        """
        question = self.market["question"]
        try:
            # The py_clob_client doesn't expose merge directly.
            # Try calling it — if the method exists, use it.
            if hasattr(self.client, 'merge_positions'):
                result = self.client.merge_positions(
                    condition_id=condition_id,
                    amount=amount,
                )
                log.info(
                    f"MERGE SUCCESS | {amount:.2f} pairs merged | "
                    f"freed=${amount:.2f} USDC | "
                    f"market={question[:40]} | result={result}"
                )
                return True

            # Fallback: try the underlying client if wrapped
            inner = getattr(self.client, '_client', None)
            if inner and hasattr(inner, 'merge_positions'):
                result = inner.merge_positions(
                    condition_id=condition_id,
                    amount=amount,
                )
                log.info(
                    f"MERGE SUCCESS | {amount:.2f} pairs merged | "
                    f"freed=${amount:.2f} USDC | "
                    f"market={question[:40]} | result={result}"
                )
                return True

            # No merge API available — alert user for manual action.
            # Throttle alerts to once per 30 minutes per market.
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

        except Exception as e:
            log.error(
                f"Merge failed for {question[:40]}: {e} — "
                f"falling back to individual SELL orders"
            )
            return False

    def has_unhedged_position(self, side: str) -> bool:
        """Check if there's unhedged inventory on a side.

        Used by run_cycle to block new BUY orders when we have inventory
        that still needs unwinding.  Also checks actual exchange balance
        when the tracker shows 0, catching fills from untracked orders.
        """
        condition_id = self.market["condition_id"]
        position_shares = self.position_tracker.get_shares(condition_id, side)

        # If tracker shows nothing, do a quick exchange check
        # to catch fills from orders that weren't tracked
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
            u["size"] for u in self.unwind_orders.values() if u["side"] == side
        )
        return (position_shares - covered) >= MIN_UNWIND_SHARES

    # ── Order Cancellation ───────────────────────────────────────────────────
    def cancel_order(self, order_id: str, reason: str = "manual") -> bool:
        """Cancel a single order.

        Args:
            order_id: Exchange order identifier.
            reason: Human-readable cancellation reason for logging.

        Returns:
            True if the cancel succeeded (or dry-run), False on failure.
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
            self.invalidate_balance_cache()  # Collateral freed
            return True
        except Exception as e:
            log.error(f"Failed to cancel order {order_id}: {e}")
            return False

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
        """Check if this manager has unwind orders or unhedged position.

        Used by the bot to decide whether to keep the manager alive
        when a market is removed from the active set.
        """
        if self.unwind_orders:
            return True
        condition_id = self.market["condition_id"]
        for side in ("yes", "no"):
            if self.position_tracker.get_shares(condition_id, side) >= MIN_UNWIND_SHARES:
                return True
        return False

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
                        # CONFIRMED fill — record position only;
                        # reconcile_unwinds() handles unwind placement
                        try:
                            filled_shares = order["original_size"]
                            # order["price"] is YES-equivalent for BOTH sides.
                            # Actual cost: YES = price, NO = 1-price (CLOB cost).
                            clob_cost = to_clob(order["price"], side)
                            filled_usd = clob_cost * filled_shares
                            log.info(
                                f"FILL (FULL) | {side.upper()} | "
                                f"price={clob_cost:.4f} | "
                                f"shares={filled_shares:.2f} | "
                                f"value=${filled_usd:.2f} | "
                                f"market={self.market['question'][:40]}"
                            )
                            alert_fill(
                                fill_type="FULL",
                                side=side.upper(),
                                price=clob_cost,
                                filled_shares=filled_shares,
                                filled_usd=filled_usd,
                                market_question=self.market["question"],
                            )
                            self.position_tracker.record_fill(
                                self.market["condition_id"], side,
                                filled_shares, order["price"],
                                question=self.market["question"],
                            )
                            self._last_fill_time[side] = _time.time()
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
                        # order["price"] is YES-equivalent for BOTH sides.
                        clob_cost = to_clob(order["price"], side)
                        filled_usd = clob_cost * filled_shares
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
                            price=clob_cost,
                            filled_shares=filled_shares,
                            filled_usd=filled_usd,
                            market_question=self.market["question"],
                            remaining_shares=remaining,
                        )
                        self.position_tracker.record_fill(
                            self.market["condition_id"], side,
                            filled_shares, order["price"],
                            question=self.market["question"],
                        )
                        self._last_fill_time[side] = _time.time()
                        # Update tracked size so next partial detection
                        # only captures the NEW delta, not the same fill.
                        # Keep original_size as the LAST known baseline
                        # for delta calculation.
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
                            unwound_shares = uorder["size"]
                            # uorder["price"] is YES-equivalent; show actual CLOB price
                            clob_sell = to_clob(uorder["price"], side)
                            unwound_usd = clob_sell * unwound_shares
                            log.info(
                                f"INVENTORY UNWOUND | {side.upper()} | "
                                f"price={clob_sell:.4f} | "
                                f"size={unwound_shares:.2f} | "
                                f"value=${unwound_usd:.2f} | "
                                f"market={self.market['question'][:40]}"
                            )
                            alert_unwind(
                                side=side.upper(),
                                price=clob_sell,
                                size=unwound_shares,
                                usd_value=unwound_usd,
                                market_question=self.market["question"],
                            )
                            self.position_tracker.record_unwind(
                                self.market["condition_id"], side,
                                unwound_shares, uorder["price"]
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
                            # reconcile_unwinds will handle replacement next cycle
                            log.warning(
                                f"Unwind order {oid[:16]}... gone "
                                f"(status={status}) | {side.upper()} | "
                                f"market={self.market['question'][:40]} — "
                                f"will be reconciled next cycle"
                            )
                            del self.unwind_orders[oid]
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
                            clob_sell = to_clob(uorder["price"], side)
                            unwound_usd = clob_sell * unwound_shares
                            log.info(
                                f"UNWIND (PARTIAL) | {side.upper()} | "
                                f"sold={unwound_shares:.2f} shares | "
                                f"remaining={u_remaining:.2f} | "
                                f"value=${unwound_usd:.2f} | "
                                f"market={self.market['question'][:40]}"
                            )
                            self.position_tracker.record_unwind(
                                self.market["condition_id"], side,
                                unwound_shares, uorder["price"]
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

        # Step 1: Detect fills (record-only, no unwind placement)
        self.detect_fills()

        # Step 1b: Reconcile unwinds (position-based)
        self.reconcile_unwinds()

        # Step 1c: Cancel active BUY orders on any halted side
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
                if not self.cancel_order(oid, reason="position_halted"):
                    log.warning(
                        f"Failed to cancel halted {side.upper()} order "
                        f"{oid[:16]}... — will retry next cycle"
                    )

        # Step 2: Fetch and validate order book
        order_book = self.get_order_book()
        if order_book is None:
            return

        best_bid = float(order_book["bids"][0]["price"])
        best_ask = float(order_book["asks"][0]["price"])
        self._cached_best_bid = best_bid
        self._cached_best_ask = best_ask

        log.info(
            f"Market: {question[:45]} | "
            f"bid={best_bid:.4f} | ask={best_ask:.4f}"
        )

        # Step 2b: Stop-loss check — sell at market if loss exceeds threshold
        self.check_stop_loss(best_bid, best_ask)

        # Step 3: Check zones and cancel bad orders
        for order_id in list(self.active_orders.keys()):
            zone = self.check_order_zone(order_id, best_bid, best_ask)
            if zone in ("DANGER", "DEAD"):
                if not self.cancel_order(order_id, reason=zone.lower()):
                    log.warning(
                        f"Failed to cancel {zone} order {order_id[:16]}... "
                        f"— will retry next cycle"
                    )

        # Step 4: Calculate prices using liquidity buffer
        our_bid, our_ask = self.calculate_order_prices(order_book)
        if our_bid is None:
            return

        # Step 5: Cancel stale orders whose price has drifted from optimal.
        # Use MIN_PRICE_DRIFT_TICKS (2 ticks) to avoid cancel+replace on
        # small oscillations — keeps orders alive longer → more reward time.
        tick = self.market.get("tick_size", 0.01)
        drift_threshold = MIN_PRICE_DRIFT_TICKS * tick
        for order_id in list(self.active_orders.keys()):
            order = self.active_orders[order_id]
            side = order["side"]
            optimal_price = our_bid if side == "yes" else our_ask
            if abs(order["price"] - optimal_price) >= drift_threshold:
                log.info(
                    f"Price drifted {abs(order['price'] - optimal_price)/tick:.0f} ticks: "
                    f"{side.upper()} {order['price']:.4f} -> {optimal_price:.4f} "
                    f"— refreshing"
                )
                if not self.cancel_order(order_id, reason="price_refresh"):
                    log.warning(
                        f"Failed to cancel stale {side.upper()} order "
                        f"{order_id[:16]}... — will retry next cycle"
                    )

        # Step 6: Place fresh orders where needed
        #         BLOCK new BUY orders if there's an unwind pending/active
        #         on that side — we need to sell inventory first, not buy more
        active_sides = {o["side"] for o in self.active_orders.values()}
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

        # ── Soft opposite-side guard ──────────────────────────────────
        # Market makers quote both sides — that's fundamental. But if we
        # already hold LARGE inventory on one side AND the opposite side,
        # we're accumulating unmergeable dual positions. Only block when
        # BOTH sides have significant inventory (merge-deadlock scenario).
        yes_inv = self.position_tracker.get_shares(condition_id, "yes")
        no_inv = self.position_tracker.get_shares(condition_id, "no")
        if yes_inv >= MIN_UNWIND_SHARES and no_inv >= MIN_UNWIND_SHARES:
            # Already holding both sides — stop adding to either until
            # one side is unwound or positions are merged.
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
            self.place_order("yes", our_bid)
        if needs_no:
            self.place_order("no", our_ask)
