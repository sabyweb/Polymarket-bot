"""Pydantic models for the dashboard API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Health(BaseModel):
    status: str = "ok"
    safety_state: str | None = None
    safety_reason: str | None = None
    safety_since: str | None = None
    last_fill: str | None = None
    last_order: str | None = None
    last_cycle: str | None = None
    last_agent: str | None = None
    active_orders: int = 0
    active_dumps: int = 0
    db_size_mb: float = 0.0
    db_updated: str | None = None
    kill_active: bool = False
    kill_reason: str | None = None
    kill_triggered_at: str | None = None
    heartbeats: dict[str, str | None] = Field(default_factory=dict)


class Portfolio(BaseModel):
    cash: float = 0.0
    inventory_value: float = 0.0
    unrealized: float = 0.0
    total: float = 0.0
    num_positions: int = 0
    drawdown_pct: float | None = None


class PnlSummary(BaseModel):
    realized_pnl: float = 0.0
    stop_loss_total: float = 0.0
    net: float = 0.0
    num_fills: int = 0
    num_unwinds: int = 0
    num_stops: int = 0


class DailyPnl(BaseModel):
    day: str
    gains: float
    losses: float
    net_pnl: float
    unwind_count: int


class FillRow(BaseModel):
    ts: float
    time: str
    condition_id: str
    question: str
    side: str
    shares: float
    price: float
    usd_value: float
    slippage: float
    fill_type: str
    cohort: int


class UnwindRow(BaseModel):
    ts: float
    time: str
    condition_id: str
    question: str
    side: str
    shares: float
    sell_price: float
    usd_value: float
    pnl: float
    hold_hours: float
    unwind_type: str


class ActiveOrder(BaseModel):
    condition_id: str
    side: str
    order_type: str
    price: float
    shares: float
    notional: float
    placed_at: str


class PositionRow(BaseModel):
    market: str
    side: str
    shares: float
    avg: float
    now: float
    value: float
    pnl: float
    pnl_pct: float
    expires: str | None = None


class CohortSnapshot(BaseModel):
    ts: float
    time: str
    cohort: int
    reward_earned: float
    unwind_pnl: float
    net_pnl: float
    fill_count: int
    deployed_markets: int
    target_capital: float


class CohortLatest(BaseModel):
    cohort: int
    reward_earned: float
    unwind_pnl: float
    net_pnl: float
    fill_count: int
    deployed_markets: int
    target_capital: float
    return_pct: float


class Allocation(BaseModel):
    num_deploy: int
    num_avoid: int
    total_capital_deployed: float
    generated_at: str
    deploys: list[dict[str, Any]]


class ConfigEntry(BaseModel):
    key: str
    default_value: Any
    override_value: Any | None = None
    effective_value: Any
    overridden: bool


class LogLine(BaseModel):
    service: str
    ts: str
    level: str
    message: str
