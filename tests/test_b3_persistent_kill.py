"""B-3 — Persistent kill switch (RF_KILL_PERSISTENT_ENABLED).

The farmer's sticky kill switch is RAM-only. When the flag is enabled, a
farmer-own guard kill in LIVE mode is written to a single-row kill_state table
in bot_history.db and reloaded on startup. Oversight-sourced kills and
DRY_RUN/SHADOW kills are intentionally NOT persisted. A DB read error at
startup fails safe to halted.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "py_clob_client_v2" not in sys.modules:
    _m = MagicMock()
    sys.modules["py_clob_client_v2"] = _m
    sys.modules["py_clob_client_v2.clob_types"] = _m.clob_types
    sys.modules["py_clob_client_v2.client"] = _m.client
    sys.modules["py_clob_client_v2.order_builder"] = _m.order_builder
    sys.modules["py_clob_client_v2.order_builder.constants"] = _m.order_builder.constants
    _m.order_builder.constants.BUY = "BUY"
    _m.order_builder.constants.SELL = "SELL"

from config import BotConfig  # noqa: E402
from database import BotDatabase  # noqa: E402
from reward_farmer import (  # noqa: E402
    MODE_LIVE, MODE_DRY_RUN, MODE_SHADOW, RewardFarmer, clear_persistent_kill_switch,
)


@pytest.fixture
def cfg_overrides():
    bc = BotConfig.instance()
    saved = dict(bc._overrides)
    try:
        yield bc._overrides
    finally:
        bc._overrides.clear()
        bc._overrides.update(saved)


@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    db = BotDatabase(path)
    try:
        yield db
    finally:
        db.close()
        os.close(fd)
        os.unlink(path)


def _make_farmer_stub(mode: str = MODE_LIVE):
    """Minimal stub for exercising B-3 paths in isolation."""
    rf = RewardFarmer.__new__(RewardFarmer)
    rf.mode = mode
    rf.dry_run = (mode == MODE_DRY_RUN)
    rf.markets = {}
    rf.cycle_count = 0
    rf._kill_switch_active = False
    rf._kill_switch_reason = ""
    rf._kill_switch_triggered_at = 0.0
    rf._gated_cancel_order = MagicMock(return_value=True)
    rf.order_lifecycle = MagicMock()
    rf.db = MagicMock()
    rf.db.set_kill_switch.return_value = True
    rf.db.get_kill_switch.return_value = None
    rf.db.clear_kill_switch.return_value = True
    return rf


# ═══════════════════════════════════════════════════════════════════════
# DB methods
# ═══════════════════════════════════════════════════════════════════════


def test_db_set_get_clear_roundtrip(tmp_db):
    assert tmp_db.get_kill_switch() is None

    ts = time.time()
    assert tmp_db.set_kill_switch(True, "fill_rate_kill", ts) is True
    state = tmp_db.get_kill_switch()
    assert state is not None
    assert state["active"] is True
    assert state["reason"] == "fill_rate_kill"
    assert state["triggered_at"] == pytest.approx(ts, abs=0.001)

    assert tmp_db.set_kill_switch(False, "cleared", ts + 1) is True
    state = tmp_db.get_kill_switch()
    assert state["active"] is False
    assert state["reason"] == "cleared"

    assert tmp_db.clear_kill_switch() is True
    assert tmp_db.get_kill_switch() is None


def test_db_reason_truncation(tmp_db):
    long_reason = "x" * 1000
    tmp_db.set_kill_switch(True, long_reason, time.time())
    state = tmp_db.get_kill_switch()
    assert len(state["reason"]) == 500
    assert state["reason"] == "x" * 500


# ═══════════════════════════════════════════════════════════════════════
# Byte-identical when disabled
# ═══════════════════════════════════════════════════════════════════════


def test_off_activate_does_not_touch_db(cfg_overrides):
    cfg_overrides["RF_KILL_PERSISTENT_ENABLED"] = False
    rf = _make_farmer_stub(MODE_LIVE)
    rf._activate_kill_switch("fill_rate_kill")
    assert rf._kill_switch_active is True
    rf.db.set_kill_switch.assert_not_called()


def test_off_load_does_not_read_db(cfg_overrides):
    cfg_overrides["RF_KILL_PERSISTENT_ENABLED"] = False
    rf = _make_farmer_stub(MODE_LIVE)
    rf._load_persistent_kill_switch()
    assert rf._kill_switch_active is False
    rf.db.get_kill_switch.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# Enabled: persist/ignore behavior
# ═══════════════════════════════════════════════════════════════════════


def test_on_persists_farmer_own_kill_in_live(cfg_overrides):
    cfg_overrides["RF_KILL_PERSISTENT_ENABLED"] = True
    rf = _make_farmer_stub(MODE_LIVE)
    rf._activate_kill_switch("fill_rate_ratio=3.60 > 3.0x")
    assert rf._kill_switch_active is True
    rf.db.set_kill_switch.assert_called_once()
    args = rf.db.set_kill_switch.call_args
    assert args.args[0] is True
    assert "fill_rate_ratio" in args.args[1]
    assert args.args[2] > 0.0


def test_on_does_not_persist_in_dry_run(cfg_overrides):
    cfg_overrides["RF_KILL_PERSISTENT_ENABLED"] = True
    rf = _make_farmer_stub(MODE_DRY_RUN)
    rf._activate_kill_switch("fill_rate_kill")
    assert rf._kill_switch_active is True
    rf.db.set_kill_switch.assert_not_called()


def test_on_does_not_persist_in_shadow(cfg_overrides):
    cfg_overrides["RF_KILL_PERSISTENT_ENABLED"] = True
    rf = _make_farmer_stub(MODE_SHADOW)
    rf._activate_kill_switch("fill_rate_kill")
    assert rf._kill_switch_active is True
    rf.db.set_kill_switch.assert_not_called()


def test_on_ignores_oversight_kill(cfg_overrides):
    cfg_overrides["RF_KILL_PERSISTENT_ENABLED"] = True
    rf = _make_farmer_stub(MODE_LIVE)
    rf._activate_kill_switch("oversight:24h realized loss $130.00 > 10% of wallet")
    assert rf._kill_switch_active is True
    rf.db.set_kill_switch.assert_not_called()


@pytest.mark.parametrize("prefix", ["oversight:", "OVERSIGHT:", "Oversight:"])
def test_on_ignores_oversight_kill_case_variants(cfg_overrides, prefix):
    cfg_overrides["RF_KILL_PERSISTENT_ENABLED"] = True
    rf = _make_farmer_stub(MODE_LIVE)
    rf._activate_kill_switch(f"{prefix}drawdown breach")
    rf.db.set_kill_switch.assert_not_called()


def test_on_logs_critical_when_db_write_fails(cfg_overrides, caplog):
    cfg_overrides["RF_KILL_PERSISTENT_ENABLED"] = True
    rf = _make_farmer_stub(MODE_LIVE)
    rf.db.set_kill_switch.return_value = False
    rf._activate_kill_switch("fill_rate_kill")
    assert rf._kill_switch_active is True
    assert any("FAILED to write" in r.message for r in caplog.records)


# ═══════════════════════════════════════════════════════════════════════
# Enabled: startup load behavior
# ═══════════════════════════════════════════════════════════════════════


def test_on_startup_loads_active_kill(cfg_overrides):
    cfg_overrides["RF_KILL_PERSISTENT_ENABLED"] = True
    rf = _make_farmer_stub(MODE_LIVE)
    rf.db.get_kill_switch.return_value = {
        "active": True,
        "reason": "fill_rate_kill",
        "triggered_at": 12345.0,
    }
    rf._load_persistent_kill_switch()
    assert rf._kill_switch_active is True
    assert rf._kill_switch_reason == "fill_rate_kill"
    assert rf._kill_switch_triggered_at == 12345.0


def test_on_startup_inactive_is_noop(cfg_overrides):
    cfg_overrides["RF_KILL_PERSISTENT_ENABLED"] = True
    rf = _make_farmer_stub(MODE_LIVE)
    rf.db.get_kill_switch.return_value = {
        "active": False,
        "reason": "",
        "triggered_at": 0.0,
    }
    rf._load_persistent_kill_switch()
    assert rf._kill_switch_active is False


def test_on_startup_no_row_is_noop(cfg_overrides):
    cfg_overrides["RF_KILL_PERSISTENT_ENABLED"] = True
    rf = _make_farmer_stub(MODE_LIVE)
    rf.db.get_kill_switch.return_value = None
    rf._load_persistent_kill_switch()
    assert rf._kill_switch_active is False


def test_on_fail_safe_read_error(cfg_overrides):
    cfg_overrides["RF_KILL_PERSISTENT_ENABLED"] = True
    rf = _make_farmer_stub(MODE_LIVE)
    rf.db.get_kill_switch.side_effect = RuntimeError("disk I/O error")
    rf._load_persistent_kill_switch()
    assert rf._kill_switch_active is True
    assert rf._kill_switch_reason == "persistent_kill_read_error"
    assert rf._kill_switch_triggered_at > 0.0


# ═══════════════════════════════════════════════════════════════════════
# CLI helper
# ═══════════════════════════════════════════════════════════════════════


def test_clear_helper(tmp_db, monkeypatch):
    tmp_db.set_kill_switch(True, "test", time.time())

    def _fake_get_db():
        return tmp_db

    monkeypatch.setattr("database._instance", tmp_db)
    assert clear_persistent_kill_switch() is True
    assert tmp_db.get_kill_switch() is None
