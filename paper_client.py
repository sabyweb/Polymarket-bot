"""
Paper trading client — drop-in replacement for RateLimitedClient.

Intercepts all WRITE operations (order placement, cancellation, merges)
while passing through all READ operations (order book, market data) to
the real CLOB API.  The bot's entire logic runs unmodified.

Usage:
    real_client = RateLimitedClient(raw_clob_client)
    paper = PaperClient(real_client, initial_balance=1000.0)
    bot.client = paper  # inject
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# ── Data classes ─────────────────────────────────────────────────────────

@dataclass
class PaperOrder:
    """A simulated order on the paper exchange."""
    order_id: str
    token_id: str
    side: str  # "BUY" or "SELL" (CLOB side)
    price: float
    size: float  # remaining shares
    size_matched: float
    original_size: float
    status: str  # "LIVE", "MATCHED", "CANCELLED"
    created_at: float

    @property
    def remaining(self) -> float:
        return max(0.0, self.original_size - self.size_matched)

    def to_api_dict(self) -> dict:
        """Format matching the real CLOB get_orders() response."""
        return {
            "id": self.order_id,
            "asset_id": self.token_id,
            "price": str(self.price),
            "size": str(self.remaining),
            "original_size": str(self.original_size),
            "size_matched": str(self.size_matched),
            "side": self.side,
            "status": self.status,
        }


# ── Cached order book provider ──────────────────────────────────────────

class CachedOrderBookProvider:
    """Shares order book fetches across multiple PaperClients.

    Caches get_order_book results for `ttl` seconds so multiple strategies
    don't duplicate API calls in the same cycle.
    """

    def __init__(self, real_client, ttl_secs: float = 25.0):
        self._client = real_client
        self._cache: dict[str, tuple[float, object]] = {}
        self._ttl = ttl_secs
        self._lock = threading.Lock()

    def get_order_book(self, token_id: str):
        now = time.time()
        with self._lock:
            if token_id in self._cache:
                ts, book = self._cache[token_id]
                if now - ts < self._ttl:
                    return book
        # Fetch outside the lock to avoid blocking
        book = self._client.get_order_book(token_id)
        with self._lock:
            self._cache[token_id] = (now, book)
        return book

    def invalidate(self, token_id: str | None = None):
        with self._lock:
            if token_id:
                self._cache.pop(token_id, None)
            else:
                self._cache.clear()


# ── Paper client ─────────────────────────────────────────────────────────

class PaperClient:
    """Drop-in replacement for RateLimitedClient that simulates trades.

    READ calls (get_order_book, etc.) pass through to the real API.
    WRITE calls (create_and_post_order, cancel, etc.) are simulated
    internally.
    """

    # Methods we intercept (must match what bot code calls)
    _INTERCEPTED = {
        "create_and_post_order",
        "cancel",
        "get_orders",
        "get_order",
        "get_balance_allowance",
        "update_balance_allowance",
        "merge_positions",
    }

    def __init__(
        self,
        real_client,
        initial_balance: float = 1000.0,
        fill_model: str = "cross_through",
        queue_position_factor: float = 0.5,
        label: str = "paper",
        book_cache: "CachedOrderBookProvider | None" = None,
    ):
        self._real_client = real_client
        self._book_cache = book_cache
        self._label = label
        self._lock = threading.Lock()

        # Simulated state
        self._orders: dict[str, PaperOrder] = {}
        self._usdc_balance: float = initial_balance
        self._token_balances: dict[str, float] = {}  # token_id → shares
        self._condition_to_tokens: dict[str, tuple[str, str]] = {}  # cid → (yes_tid, no_tid)

        # Fill engine
        self._fill_engine = FillEngine(fill_model, queue_position_factor)

    # ── Attribute delegation ──────────────────────────────────────────

    def __getattr__(self, name: str):
        if name in self._INTERCEPTED:
            return getattr(self, f"_paper_{name}")
        # Pass through to real client (includes get_order_book, etc.)
        attr = getattr(self._real_client, name)
        # If this is get_order_book and we have a cache, use it
        if name == "get_order_book" and self._book_cache is not None:
            return self._book_cache.get_order_book
        return attr

    # ── Market registration ───────────────────────────────────────────

    def register_market(self, condition_id: str, yes_token: str, no_token: str):
        """Map condition_id to token IDs for merge support."""
        self._condition_to_tokens[condition_id] = (yes_token, no_token)

    # ── Simulated API methods ─────────────────────────────────────────

    def _paper_create_and_post_order(self, order_args) -> dict:
        """Simulate order placement."""
        with self._lock:
            token_id = order_args.token_id
            price = float(order_args.price)
            size = float(order_args.size)
            side = order_args.side  # "BUY" or "SELL"

            # Balance check
            if side == "BUY":
                cost = price * size
                if cost > self._usdc_balance + 0.01:  # small tolerance
                    raise Exception(
                        f"Paper: insufficient USDC balance "
                        f"(need ${cost:.2f}, have ${self._usdc_balance:.2f})"
                    )
                self._usdc_balance -= cost
            else:  # SELL
                available = self._token_balances.get(token_id, 0.0)
                if size > available + 1.0:  # generous tolerance for float rounding
                    raise Exception(
                        f"Paper: insufficient token balance "
                        f"(need {size:.1f}, have {available:.1f})"
                    )
                self._token_balances[token_id] = available - size

            oid = f"paper_{uuid.uuid4().hex[:16]}"
            order = PaperOrder(
                order_id=oid,
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                size_matched=0.0,
                original_size=size,
                status="LIVE",
                created_at=time.time(),
            )
            self._orders[oid] = order

            log.debug(
                f"[{self._label}] PAPER ORDER | {side} {size:.0f} "
                f"@ {price:.4f} | token={token_id[:12]}... | "
                f"balance=${self._usdc_balance:.2f}"
            )
            return {"success": True, "orderID": oid}

    def _paper_cancel(self, order_id: str) -> None:
        """Simulate order cancellation with collateral refund."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None or order.status != "LIVE":
                return

            remaining = order.remaining
            if remaining > 0:
                if order.side == "BUY":
                    self._usdc_balance += order.price * remaining
                else:
                    self._token_balances[order.token_id] = (
                        self._token_balances.get(order.token_id, 0.0) + remaining
                    )
            order.status = "CANCELLED"

    def _paper_get_orders(self) -> list[dict]:
        """Return all LIVE paper orders in CLOB API format."""
        with self._lock:
            return [
                o.to_api_dict()
                for o in self._orders.values()
                if o.status == "LIVE"
            ]

    def _paper_get_order(self, order_id: str) -> dict:
        """Return single order status."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                return {"status": "UNKNOWN"}
            return {
                "status": order.status,
                "size_matched": str(order.size_matched),
                "original_size": str(order.original_size),
                "price": str(order.price),
            }

    def _paper_get_balance_allowance(self, params) -> dict:
        """Return simulated balance in CLOB API format (raw units, /1e6)."""
        with self._lock:
            asset_type = getattr(params, "asset_type", None)
            token_id = getattr(params, "token_id", None)
            # Check if CONDITIONAL — compare by name or value to handle enum
            is_conditional = (
                token_id is not None
                and asset_type is not None
                and "CONDITIONAL" in str(asset_type).upper()
            )
            if is_conditional:
                shares = self._token_balances.get(token_id, 0.0)
                raw = str(int(shares * 1e6))
                return {"balance": raw, "allowance": raw}
            else:
                # COLLATERAL (USDC)
                raw = str(int(self._usdc_balance * 1e6))
                return {"balance": raw, "allowance": raw}

    def _paper_update_balance_allowance(self, params) -> None:
        """No-op — paper exchange has unlimited allowances."""
        pass

    def _paper_merge_positions(self, condition_id: str, amount: float) -> dict:
        """Simulate YES+NO merge → USDC."""
        with self._lock:
            tokens = self._condition_to_tokens.get(condition_id)
            if not tokens:
                log.warning(f"[{self._label}] Paper merge: unknown condition {condition_id[:16]}...")
                return {"success": False}

            yes_tid, no_tid = tokens
            yes_bal = self._token_balances.get(yes_tid, 0.0)
            no_bal = self._token_balances.get(no_tid, 0.0)

            if yes_bal < amount or no_bal < amount:
                log.warning(
                    f"[{self._label}] Paper merge: insufficient tokens "
                    f"(YES={yes_bal:.1f}, NO={no_bal:.1f}, need={amount:.1f})"
                )
                return {"success": False}

            self._token_balances[yes_tid] = yes_bal - amount
            self._token_balances[no_tid] = no_bal - amount
            self._usdc_balance += amount  # 1 YES + 1 NO = $1
            log.info(
                f"[{self._label}] PAPER MERGE | {amount:.0f} pairs | "
                f"+${amount:.2f} USDC"
            )
            return {"success": True}

    # ── Fill simulation ───────────────────────────────────────────────

    def simulate_fills(self, book_provider: "CachedOrderBookProvider | None" = None):
        """Run fill engine on all live orders."""
        provider = book_provider or self._book_cache
        if provider is None:
            return
        self._fill_engine.simulate_fills(self, provider)

    # ── Reporting ─────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        """Return current paper trading state for reporting."""
        with self._lock:
            live_orders = [o for o in self._orders.values() if o.status == "LIVE"]
            filled_orders = [o for o in self._orders.values() if o.status == "MATCHED"]
            return {
                "label": self._label,
                "usdc_balance": self._usdc_balance,
                "token_balances": dict(self._token_balances),
                "live_orders": len(live_orders),
                "filled_orders": len(filled_orders),
                "total_orders": len(self._orders),
            }


# ── Fill engine ──────────────────────────────────────────────────────────

class FillEngine:
    """Determines which paper orders fill based on real order book data.

    Uses a conservative model: price must cross THROUGH our level
    (not just touch), with queue position penalty.
    """

    def __init__(
        self,
        fill_model: str = "cross_through",
        queue_position_factor: float = 0.5,
    ):
        self.fill_model = fill_model
        self.queue_position_factor = queue_position_factor

    def simulate_fills(
        self,
        paper_client: PaperClient,
        book_provider: CachedOrderBookProvider,
    ):
        """Check all LIVE orders against real order books."""
        with paper_client._lock:
            live_orders = [
                o for o in paper_client._orders.values()
                if o.status == "LIVE"
            ]

        for order in live_orders:
            try:
                book = book_provider.get_order_book(order.token_id)
                if book is None:
                    continue
                self._check_order(paper_client, order, book)
            except Exception as e:
                log.debug(f"Fill sim error for {order.order_id[:16]}: {e}")

    def _check_order(self, paper_client: PaperClient, order: PaperOrder, book):
        """Check if a single order would fill given the current book."""
        if order.side == "BUY":
            self._check_buy(paper_client, order, book)
        else:
            self._check_sell(paper_client, order, book)

    def _check_buy(self, paper_client: PaperClient, order: PaperOrder, book):
        """Check if a BUY order fills (asks crossing our bid)."""
        asks = getattr(book, "asks", [])
        if not asks:
            return

        # Calculate crossing volume
        crossing_volume = 0.0
        for ask in asks:
            ask_price = float(ask.price)
            if self.fill_model == "cross_through":
                if ask_price < order.price:  # strictly less
                    crossing_volume += float(ask.size)
            else:  # "touch"
                if ask_price <= order.price:
                    crossing_volume += float(ask.size)

        if crossing_volume <= 0:
            return

        # Queue position: how many shares ahead of us at our price level?
        bids = getattr(book, "bids", [])
        queue_ahead = 0.0
        for bid in bids:
            if abs(float(bid.price) - order.price) < 0.002:
                queue_ahead += float(bid.size)

        # Our share of fills at this level
        total_at_level = queue_ahead + order.remaining
        if total_at_level <= 0:
            our_share = 1.0
        else:
            our_share = order.remaining / total_at_level

        our_share *= (1.0 - self.queue_position_factor)

        fill_amount = min(order.remaining, crossing_volume * our_share)
        if fill_amount < 1.0:
            return

        self._execute_fill(paper_client, order, fill_amount)

    def _check_sell(self, paper_client: PaperClient, order: PaperOrder, book):
        """Check if a SELL order fills (bids crossing our ask)."""
        bids = getattr(book, "bids", [])
        if not bids:
            return

        crossing_volume = 0.0
        for bid in bids:
            bid_price = float(bid.price)
            if self.fill_model == "cross_through":
                if bid_price > order.price:  # strictly greater
                    crossing_volume += float(bid.size)
            else:
                if bid_price >= order.price:
                    crossing_volume += float(bid.size)

        if crossing_volume <= 0:
            return

        asks = getattr(book, "asks", [])
        queue_ahead = 0.0
        for ask in asks:
            if abs(float(ask.price) - order.price) < 0.002:
                queue_ahead += float(ask.size)

        total_at_level = queue_ahead + order.remaining
        our_share = (order.remaining / total_at_level) if total_at_level > 0 else 1.0
        our_share *= (1.0 - self.queue_position_factor)

        fill_amount = min(order.remaining, crossing_volume * our_share)
        if fill_amount < 1.0:
            return

        self._execute_fill(paper_client, order, fill_amount)

    def _execute_fill(
        self, paper_client: PaperClient, order: PaperOrder, fill_amount: float
    ):
        """Execute a simulated fill."""
        with paper_client._lock:
            order.size_matched += fill_amount
            order.size = order.remaining

            if order.side == "BUY":
                # Tokens credited (collateral was already reserved at placement)
                paper_client._token_balances[order.token_id] = (
                    paper_client._token_balances.get(order.token_id, 0.0)
                    + fill_amount
                )
            else:  # SELL
                # USDC credited (tokens were already reserved at placement)
                paper_client._usdc_balance += fill_amount * order.price

            if order.remaining < 1.0:
                order.status = "MATCHED"
                # Refund dust collateral
                dust = order.remaining
                if dust > 0:
                    if order.side == "BUY":
                        paper_client._usdc_balance += dust * order.price
                    else:
                        paper_client._token_balances[order.token_id] = (
                            paper_client._token_balances.get(order.token_id, 0.0)
                            + dust
                        )

            log.info(
                f"[{paper_client._label}] PAPER FILL | "
                f"{order.side} {fill_amount:.0f} shares @ {order.price:.4f} | "
                f"matched={order.size_matched:.0f}/{order.original_size:.0f} | "
                f"balance=${paper_client._usdc_balance:.2f}"
            )
