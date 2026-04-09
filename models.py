"""Shared dataclasses for the reward farming system."""

from dataclasses import dataclass, field


@dataclass
class OrderSlot:
    order_id: str | None = None
    price: float = 0.0
    shares: float = 0.0
    placed_at: float = 0.0


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
    midpoint: float = 0.0
    last_fill_price: dict = field(default_factory=dict)
    agent_shares: float = 0
    agent_approved: bool = False
    fill_times: dict = field(default_factory=lambda: {"yes": [], "no": []})
    end_date_iso: str = ""
    book_failures: int = 0  # consecutive get_merged_book failures (404/timeout)
