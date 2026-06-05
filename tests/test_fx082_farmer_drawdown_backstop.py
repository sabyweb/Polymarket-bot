"""FX-082 — farmer-side drawdown backstop for oversight silence.

The 15% drawdown kill normally lives ONLY in the oversight process
(simple_allocator.check_kill_switch, written into market_allocations.json). If
oversight dies/wedges (FX-080 showed it can for ~2 days) the alloc file stops
being rewritten; the farmer's stale-alloc handling only BLOCKS NEW orders,
leaving existing positions to ride an unmonitored drawdown.

_guardrail_oversight_silence_drawdown is the farmer's own backstop: it trips a
sticky kill when, AND ONLY when, all of — oversight silent past
RF_OVERSIGHT_SILENCE_KILL_HOURS (alloc mtime), live notional present, and a
farmer-computed drawdown (current wallet vs portfolio_snapshots peak) exceeds
RF_FARMER_DRAWDOWN_KILL_FRAC. Fail-open on ANY missing signal.

Mirrors tests/test_fx068_oversight_kill.py + tests/test_p1_farmer_retune.py.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reward_farmer  # noqa: E402


def _bare_farmer() -> "reward_farmer.RewardFarmer":
    """RewardFarmer with __init__ bypassed — only the attrs the path touches."""
    return reward_farmer.RewardFarmer.__new__(reward_farmer.RewardFarmer)


def _backstop_farmer(alloc_age_hours, peak, current):
    """Wire just what _guardrail_oversight_silence_drawdown reads: the
    oversight-silence signal (self._alloc_mtime) and the wallet probes
    (self.db.get_wallet_peak_usd / load_usdc_balance)."""
    rf = _bare_farmer()
    rf._alloc_mtime = (time.time() - alloc_age_hours * 3600.0) if alloc_age_hours is not None else 0.0
    rf.db = MagicMock()
    rf.db.get_wallet_peak_usd.return_value = peak
    rf.db.load_usdc_balance.return_value = (current, time.time())
    rf.db.load_all_positions.return_value = {}
    rf.markets = {}
    return rf


class TestFX082Backstop(unittest.TestCase):
    """Direct tests of the backstop. Defaults (config.py): silence 2.0h, frac 0.15."""

    def test_kills_on_silence_drawdown_exposure(self):
        rf = _backstop_farmer(alloc_age_hours=3.0, peak=1000.0, current=800.0)  # 20% dd
        kill, reason = rf._guardrail_oversight_silence_drawdown(500.0)
        self.assertTrue(kill)
        self.assertIn("oversight_silent", reason)
        self.assertIn("farmer_drawdown", reason)

    def test_no_kill_when_oversight_recent(self):
        rf = _backstop_farmer(alloc_age_hours=0.5, peak=1000.0, current=800.0)  # 0.5h < 2h
        self.assertEqual(rf._guardrail_oversight_silence_drawdown(500.0), (False, ""))

    def test_no_kill_when_no_exposure(self):
        rf = _backstop_farmer(alloc_age_hours=3.0, peak=1000.0, current=800.0)
        self.assertEqual(rf._guardrail_oversight_silence_drawdown(0.0), (False, ""))
        self.assertEqual(rf._guardrail_oversight_silence_drawdown(None), (False, ""))

    def test_no_kill_when_drawdown_below_threshold(self):
        rf = _backstop_farmer(alloc_age_hours=3.0, peak=1000.0, current=950.0)  # 5% dd
        self.assertEqual(rf._guardrail_oversight_silence_drawdown(500.0), (False, ""))

    def test_failopen_when_no_alloc_mtime_yet(self):
        rf = _backstop_farmer(alloc_age_hours=None, peak=1000.0, current=800.0)  # mtime 0.0
        self.assertEqual(rf._guardrail_oversight_silence_drawdown(500.0), (False, ""))

    def test_failopen_when_peak_missing(self):
        rf = _backstop_farmer(alloc_age_hours=3.0, peak=None, current=800.0)
        self.assertEqual(rf._guardrail_oversight_silence_drawdown(500.0), (False, ""))

    def test_failopen_when_current_wallet_missing(self):
        rf = _backstop_farmer(alloc_age_hours=3.0, peak=1000.0, current=None)
        self.assertEqual(rf._guardrail_oversight_silence_drawdown(500.0), (False, ""))

    def test_failopen_when_peak_nonpositive(self):
        rf = _backstop_farmer(alloc_age_hours=3.0, peak=0.0, current=800.0)
        self.assertEqual(rf._guardrail_oversight_silence_drawdown(500.0), (False, ""))

    def test_disabled_when_knob_zero(self):
        rf = _backstop_farmer(alloc_age_hours=3.0, peak=1000.0, current=800.0)
        with patch("reward_farmer.cfg") as mock_cfg:
            mock_cfg.side_effect = lambda k: 0.0 if k == "RF_OVERSIGHT_SILENCE_KILL_HOURS" else 0.15
            self.assertEqual(rf._guardrail_oversight_silence_drawdown(500.0), (False, ""))


def _guardrail_farmer(alloc_age_hours, peak, current):
    """Bare farmer set up to run the WHOLE _guardrail_check_and_log with every
    OTHER limb stubbed benign, so only the FX-082 backstop can produce a kill."""
    rf = _bare_farmer()
    rf.cycle_count = 1
    rf._rolling_stats = []
    rf._consecutive_hard_notional_breach_cycles = 0
    rf._alloc_mtime = (time.time() - alloc_age_hours * 3600.0) if alloc_age_hours is not None else 0.0
    rf.db = MagicMock()
    rf.db.get_wallet_peak_usd.return_value = peak
    rf.db.load_usdc_balance.return_value = (current, time.time())
    rf.db.load_all_positions.return_value = {}
    rf.markets = {}
    rf._guardrail_total_capital_from_alloc = MagicMock(return_value=1000.0)
    rf._guardrail_live_notional_per_market = MagicMock(return_value={"0xabc": 500.0})
    rf._guardrail_cluster_notional = MagicMock(return_value=({}, {}))  # (cluster_notional, cluster_by_cid) — both dicts
    rf._guardrail_current_cf = MagicMock(return_value=None)
    rf._guardrail_daily_realized_loss = MagicMock(return_value=0.0)
    rf._guardrail_fill_rate_ratio = MagicMock(return_value=(None, 0, 0))
    rf._guardrail_rapid_notional_growth = MagicMock(return_value=(False, 1.0))
    return rf


class TestFX082Integration(unittest.TestCase):
    """The backstop must be WIRED into _guardrail_check_and_log so a real
    silence+drawdown trips result['kill_switch'] — which run_cycle converts to
    _activate_kill_switch (reward_farmer.py:2177-2180)."""

    def test_silence_drawdown_trips_guardrail_kill(self):
        rf = _guardrail_farmer(alloc_age_hours=3.0, peak=1000.0, current=800.0)  # 20% dd
        result = rf._guardrail_check_and_log()
        self.assertTrue(result["kill_switch"], "silence+drawdown must trip the guardrail kill")
        self.assertIn("farmer_drawdown", result["kill_reason"])

    def test_fresh_oversight_no_kill(self):
        rf = _guardrail_farmer(alloc_age_hours=0.0, peak=1000.0, current=800.0)  # alloc just now
        result = rf._guardrail_check_and_log()
        self.assertFalse(result["kill_switch"], "fresh oversight → backstop silent, no other limb fires")


if __name__ == "__main__":
    unittest.main()
