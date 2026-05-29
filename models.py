"""Shared dataclasses for the reward farming system."""

from dataclasses import dataclass, field


@dataclass
class OrderSlot:
    order_id: str | None = None
    price: float = 0.0
    shares: float = 0.0
    placed_at: float = 0.0
    last_stale_check: float = 0.0  # last time we force-checked this order for partial fills


@dataclass
class MarketState:
    """Per-market tracking."""
    cid: str
    question: str
    yes_tid: str
    no_tid: str
    daily_rate: float
    max_spread: float
    min_size: float
    tick_size: float
    yes_price: float | None
    orders: dict = field(default_factory=lambda: {"yes": OrderSlot(), "no": OrderSlot()})
    dump_orders: dict = field(default_factory=lambda: {"yes": None, "no": None})
    dump_state: dict = field(default_factory=lambda: {"yes": None, "no": None})
    dump_failures: int = 0
    unknown_count: dict = field(default_factory=lambda: {"yes": 0, "no": 0})
    last_book_fetch: float = 0.0
    cached_book: dict | None = None   # Last successful merged book; consumed by record_cycle via RF_BOOK_CACHE_TTL
    midpoint: float = 0.0
    last_fill_price: dict = field(default_factory=dict)
    agent_shares: float = 0
    agent_approved: bool = False
    fill_times: dict = field(default_factory=lambda: {"yes": [], "no": []})
    # FX-069: kill-switch fill history. SEPARATE from fill_times (which
    # can_place prunes to the 180s RF_FILL_BREAKER_WINDOW). This flat list of
    # unix timestamps is appended on the real fill-record path (handle_fill)
    # and pruned only to the 6h kill baseline (RF_KILL_FILL_HISTORY_SECS), so
    # the fill-rate spike kill can see slow bleed instead of degenerating to
    # ">=5 fills/180s".
    kill_fill_times: list = field(default_factory=list)
    end_date_iso: str = ""
    book_failures: int = 0  # consecutive get_merged_book failures (404/timeout)
