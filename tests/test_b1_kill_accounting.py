"""B-1 — finish the realized-loss-kill accounting family. Two independent flag-gated fixes:

1. RF_KILL_ACCT_STARTUP_DUMP_ENABLED — a startup-recovered dump SELL was logged with no vwap_cost =>
   pnl=+proceeds (a forced loss-exit booked as PROFIT, invisible to the realized-loss kill). ON => true
   cost basis (pnl<=0), captured BEFORE record_unwind zeroes avg_price, idempotent via an order-keyed
   unwind_event_id. OFF => byte-identical (+proceeds, no event_id).
2. RF_GUARDRAIL_DUMP_NOTIONAL_FIX_ENABLED — _guardrail_live_notional_per_market read the dead "price"
   key (always 0) instead of "fill_price", so resting-dump exposure was invisible to the notional/
   cluster/rapid-growth kills. ON => reads "fill_price". OFF => byte-identical (0).
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock the SDK before importing reward_farmer (it has method-local SDK imports).
if "py_clob_client_v2" not in sys.modules:
    _m = MagicMock()
    sys.modules["py_clob_client_v2"] = _m
    sys.modules["py_clob_client_v2.clob_types"] = _m.clob_types
    sys.modules["py_clob_client_v2.client"] = _m.client
    sys.modules["py_clob_client_v2.order_builder"] = _m.order_builder
    sys.modules["py_clob_client_v2.order_builder.constants"] = _m.order_builder.constants
    _m.order_builder.constants.BUY = "BUY"
    _m.order_builder.constants.SELL = "SELL"

import state  # noqa: E402
from config import BotConfig  # noqa: E402
from models import MarketState  # noqa: E402
from price import to_clob  # noqa: E402
from reward_farmer import RewardFarmer  # noqa: E402


@pytest.fixture
def cfg_overrides():
    bc = BotConfig.instance()
    saved = dict(bc._overrides)
    try:
        yield bc._overrides
    finally:
        bc._overrides.clear()
        bc._overrides.update(saved)


# ── startup-dump fix ──────────────────────────────────────────────────────────

def _startup_stub(positions):
    class _Stub:
        pass
    s = _Stub()
    s.dry_run = False
    s.client = MagicMock()
    s.db = MagicMock()
    s.db.log_unwind.return_value = True
    s.positions = positions
    return s


def _run_startup_dump(stub, fill_price=0.40, matched=50, oid="oid_dump", cid="cid_001", side="yes"):
    stub.db.load_active_orders.return_value = [{
        "order_id": oid, "condition_id": cid, "side": side,
        "order_type": "dump_sell", "price": fill_price, "shares": matched, "placed_at": 0,
    }]
    stub.client.get_open_orders.return_value = []  # gone from exchange (fully matched offline)
    stub.client.get_order.return_value = {"status": "MATCHED", "size_matched": matched, "price": str(fill_price)}
    RewardFarmer._reconcile_on_startup(stub)
    assert stub.db.log_unwind.called, "startup dump must log an unwind"
    kw = stub.db.log_unwind.call_args.kwargs
    vwap = kw.get("vwap_cost", 0.0)
    return kw, kw["usd_value"] - vwap


def _real_store(monkeypatch):
    monkeypatch.setattr(state.PositionStore, "_load", lambda self: None)
    monkeypatch.setattr(state.PositionStore, "_save", lambda self: None)
    return state.PositionStore()


def test_startup_dump_off_is_byte_identical(cfg_overrides):
    """OFF: vwap_cost=0 + no event_id => pnl=+proceeds (today). get_avg_price NOT called."""
    cfg_overrides["RF_KILL_ACCT_STARTUP_DUMP_ENABLED"] = False
    positions = MagicMock()
    stub = _startup_stub(positions)
    kw, pnl = _run_startup_dump(stub, fill_price=0.40, matched=50)
    assert kw.get("vwap_cost", 0.0) == 0.0
    assert kw.get("unwind_event_id", "") == ""
    assert pnl == pytest.approx(20.0)             # +proceeds (50 * 0.40)
    positions.get_avg_price.assert_not_called()   # off path does no extra work


def test_startup_dump_on_real_basis_books_loss(cfg_overrides, monkeypatch):
    """ON: cost basis 0.50, sold at 0.40 => pnl<0, kill-visible. Cost captured BEFORE record_unwind
    zeroes avg_price (proven by a real PositionStore). Event_id keyed on the order id."""
    cfg_overrides["RF_KILL_ACCT_STARTUP_DUMP_ENABLED"] = True
    ps = _real_store(monkeypatch)
    ps.register_market("cid_001", "Q")
    ps.record_fill("cid_001", "yes", 50, 0.50)    # avg 0.50 yes-equiv
    stub = _startup_stub(ps)
    kw, pnl = _run_startup_dump(stub, fill_price=0.40, matched=50, oid="oid_dump")
    assert kw["vwap_cost"] == pytest.approx(50 * to_clob(0.50, "yes"))   # = 25.0
    assert pnl == pytest.approx(-5.0)             # 20 proceeds - 25 cost
    assert kw["unwind_event_id"] == "startup_unwind:oid_dump"
    assert ps.get_avg_price("cid_001", "yes") == 0.0   # record_unwind zeroed it (so capture WAS first)


def test_startup_dump_on_unknown_basis_floors_pnl_zero(cfg_overrides, monkeypatch):
    """ON but no cost basis (orphan, avg=0): floor vwap_cost to proceeds => pnl=0, never a profit."""
    cfg_overrides["RF_KILL_ACCT_STARTUP_DUMP_ENABLED"] = True
    ps = _real_store(monkeypatch)
    ps.register_market("cid_001", "Q")            # registered but no fill => avg 0
    stub = _startup_stub(ps)
    kw, pnl = _run_startup_dump(stub, fill_price=0.40, matched=50)
    assert kw["vwap_cost"] == pytest.approx(20.0)  # floored to proceeds
    assert pnl == pytest.approx(0.0)


# ── dump-notional typo fix ────────────────────────────────────────────────────

def _ms_with_dump(cid="cid_001", fill_price=0.50, shares=50):
    ms = MarketState(
        cid=cid, question="Q?", yes_tid="ytid", no_tid="ntid",
        daily_rate=20.0, max_spread=0.10, min_size=10, tick_size=0.01,
        yes_price=0.50, agent_shares=50, agent_approved=True,
    )
    ms.dump_orders["yes"] = "oid_dump"
    ms.dump_state["yes"] = {"fill_price": fill_price, "shares": shares}
    return ms


def _notional_farmer(markets):
    rf = RewardFarmer.__new__(RewardFarmer)
    rf.markets = markets
    return rf


def test_dump_notional_off_is_byte_identical(cfg_overrides):
    """OFF: reads the dead 'price' key => dp=0 => dump contributes 0 (today)."""
    cfg_overrides["RF_GUARDRAIL_DUMP_NOTIONAL_FIX_ENABLED"] = False
    rf = _notional_farmer({"cid_001": _ms_with_dump(fill_price=0.50, shares=50)})
    out = rf._guardrail_live_notional_per_market()
    assert out == {}   # notional 0 => cid excluded


def test_dump_notional_on_includes_fill_price(cfg_overrides):
    """ON: reads 'fill_price' => dump exposure visible (0.50 * 50 = 25)."""
    cfg_overrides["RF_GUARDRAIL_DUMP_NOTIONAL_FIX_ENABLED"] = True
    rf = _notional_farmer({"cid_001": _ms_with_dump(fill_price=0.50, shares=50)})
    out = rf._guardrail_live_notional_per_market()
    assert out.get("cid_001") == pytest.approx(25.0)
