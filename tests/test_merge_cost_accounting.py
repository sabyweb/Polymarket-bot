"""Realized-loss-kill accounting — merge cost basis (RF_KILL_ACCT_MERGE_COST_ENABLED).

The realized-loss kill (10%/24h) sums `unwinds.pnl<0` as its SOLE input. The both-sides
MERGE exit (`dump_manager.try_merge`) logs `usd_value` with NO `vwap_cost` => `pnl=+amount`,
so a pair acquired >$1 (an adverse merge) books a REAL LOSS as a PROFIT — invisible to the
kill (memory `realized_loss_kill_bypass`).

When `RF_KILL_ACCT_MERGE_COST_ENABLED` is ON, `try_merge` derives `vwap_cost` from the
per-leg cost basis so an adverse merge records `pnl<0`. OFF = byte-identical (`vwap_cost=0`
=> `pnl=+amount`, the pre-fix path).

Economics: a complete set (1 YES + 1 NO) redeems to $1 (no taker fee), so usd_value=amount;
cost = amount*(yes_clob + no_clob) where yes_clob=to_clob(yes_avg,"yes")=yes_avg and
no_clob=to_clob(no_avg,"no")=1-no_avg. pnl<0 iff yes_clob+no_clob > 1 (pair cost >$1).
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state  # noqa: E402
from config import BotConfig  # noqa: E402
from models import MarketState  # noqa: E402


def _ensure_clob_types_mock():
    """Mock py_clob_client_v2 so try_merge's method-local SDK import works
    without the real SDK installed (mirrors tests/test_critical_fixes.py)."""
    if "py_clob_client_v2" not in sys.modules:
        mock_clob = MagicMock()
        sys.modules["py_clob_client_v2"] = mock_clob
        sys.modules["py_clob_client_v2.clob_types"] = mock_clob.clob_types
        sys.modules["py_clob_client_v2.order_builder"] = mock_clob.order_builder
        sys.modules["py_clob_client_v2.order_builder.constants"] = mock_clob.order_builder.constants
        mock_clob.order_builder.constants.BUY = "BUY"
        mock_clob.order_builder.constants.SELL = "SELL"


_ensure_clob_types_mock()


@pytest.fixture
def cfg_overrides():
    """Set BotConfig overrides for one test; restore afterwards (mirrors test_ab_cohorts)."""
    bc = BotConfig.instance()
    saved = dict(bc._overrides)
    try:
        yield bc._overrides
    finally:
        bc._overrides.clear()
        bc._overrides.update(saved)


def _make_ms(cid="cid_001"):
    return MarketState(
        cid=cid, question="Test market?", yes_tid="ytid", no_tid="ntid",
        daily_rate=20.0, max_spread=0.10, min_size=10, tick_size=0.01,
        yes_price=0.50, agent_shares=50, agent_approved=True,
    )


def _make_dump_manager(positions):
    from dump_manager import DumpManager
    db = MagicMock()
    db.is_unliquidatable.return_value = False
    db.log_unwind.return_value = True
    return DumpManager(
        client=MagicMock(), db=db, positions=positions,
        cancel_fn=MagicMock(), dry_run=False,
    )


def _mock_positions(yes_avg, no_avg, shares=100.0):
    positions = MagicMock()
    positions.get_shares.return_value = shares
    positions.get_avg_price.side_effect = (
        lambda cid, side: yes_avg if side == "yes" else no_avg
    )
    return positions


def _run_merge(dm, ms, amount=100.0):
    """Run try_merge with a mocked successful on-chain merge; return
    (vwap_cost, pnl) derived from the db.log_unwind call kwargs."""
    with patch("ctf_merge.try_merge_positions", return_value=(True, "")):
        dm.try_merge(ms, amount)
    assert dm.db.log_unwind.called, "a successful merge must write an unwind row"
    kw = dm.db.log_unwind.call_args.kwargs
    vwap_cost = kw.get("vwap_cost", 0.0)
    return vwap_cost, kw["usd_value"] - vwap_cost


# ── Flag OFF: byte-identical to the pre-fix behaviour ─────────────────────────

def test_off_is_byte_identical(cfg_overrides):
    """Flag OFF (default): vwap_cost=0 => pnl=+amount (pre-fix). And get_avg_price
    is NOT called on the off path — proves zero added work / no behaviour change."""
    cfg_overrides["RF_KILL_ACCT_MERGE_COST_ENABLED"] = False
    positions = _mock_positions(0.60, 0.40)  # adverse pair — must be ignored when off
    dm = _make_dump_manager(positions)
    vwap_cost, pnl = _run_merge(dm, _make_ms())
    assert vwap_cost == 0.0
    assert pnl == 100.0
    positions.get_avg_price.assert_not_called()


# ── Flag ON: the kill now SEES the real merge pnl ─────────────────────────────

def test_on_adverse_pair_books_loss(cfg_overrides):
    """ON: pair cost $1.20 (yes_clob .60 + no_clob 1-.40=.60) => pnl=-20 (kill-visible)."""
    cfg_overrides["RF_KILL_ACCT_MERGE_COST_ENABLED"] = True
    dm = _make_dump_manager(_mock_positions(0.60, 0.40))
    vwap_cost, pnl = _run_merge(dm, _make_ms())
    assert vwap_cost == pytest.approx(120.0)
    assert pnl == pytest.approx(-20.0)


def test_on_favourable_pair_books_profit(cfg_overrides):
    """ON: pair cost $0.90 (yes_clob .45 + no_clob 1-.55=.45) => pnl=+10."""
    cfg_overrides["RF_KILL_ACCT_MERGE_COST_ENABLED"] = True
    dm = _make_dump_manager(_mock_positions(0.45, 0.55))
    vwap_cost, pnl = _run_merge(dm, _make_ms())
    assert vwap_cost == pytest.approx(90.0)
    assert pnl == pytest.approx(10.0)


# ── Hardening edge cases (RT-M2 / RT-M3) ──────────────────────────────────────

def test_on_unknown_basis_floors_pnl_to_zero(cfg_overrides):
    """ON but a leg avg=0 (orphan/startup hedge): floor vwap_cost=usd => pnl=0,
    never a phantom profit. (RT-M3)"""
    cfg_overrides["RF_KILL_ACCT_MERGE_COST_ENABLED"] = True
    dm = _make_dump_manager(_mock_positions(0.0, 0.55))
    vwap_cost, pnl = _run_merge(dm, _make_ms())
    assert vwap_cost == pytest.approx(100.0)
    assert pnl == pytest.approx(0.0)


def test_on_corrupt_avg_does_not_crash(cfg_overrides):
    """ON with avg>1 (corrupt): to_clob would raise ValueError; the 0<avg<=1 guard
    routes to the conservative floor instead of crashing the merge write. (RT-M2)"""
    cfg_overrides["RF_KILL_ACCT_MERGE_COST_ENABLED"] = True
    dm = _make_dump_manager(_mock_positions(1.5, 0.55))
    vwap_cost, pnl = _run_merge(dm, _make_ms())
    assert pnl == pytest.approx(0.0)  # floored, no exception


# ── RT-M1: the core ordering bug, exercised with a REAL PositionStore ─────────

def test_on_captures_basis_before_record_unwind(cfg_overrides, monkeypatch):
    """record_unwind zeroes avg_price at 0 shares; the fix MUST read the basis
    first. With a real PositionStore, reading after would floor to pnl=0, so
    asserting pnl=-20 proves the capture happens before record_unwind. (RT-M1)"""
    monkeypatch.setattr(state.PositionStore, "_load", lambda self: None)
    monkeypatch.setattr(state.PositionStore, "_save", lambda self: None)
    ps = state.PositionStore()
    ps.register_market("cid_001", "Q?")
    ps.set_shares("cid_001", "yes", 100, avg_price=0.60)
    ps.set_shares("cid_001", "no", 100, avg_price=0.40)  # pair cost $1.20

    cfg_overrides["RF_KILL_ACCT_MERGE_COST_ENABLED"] = True
    dm = _make_dump_manager(ps)
    vwap_cost, pnl = _run_merge(dm, _make_ms("cid_001"))

    assert vwap_cost == pytest.approx(120.0), "must use the avg BEFORE record_unwind zeroed it"
    assert pnl == pytest.approx(-20.0)
    # sanity: record_unwind really did zero the basis (so reading after would have floored)
    assert ps.get_avg_price("cid_001", "yes") == 0.0
