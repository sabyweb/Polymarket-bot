"""FX-068 — oversight-side kill switch must halt the farmer.

Pre-FX-068, simple_allocator.check_kill_switch wrote `kill_switch=True` +
`deploys=[]` into market_allocations.json, but the farmer's _load_allocations
only read `action=="deploy"` rows and never read the top-level `kill_switch`
field. The empty deploy list made the farmer stop placing NEW orders, but it
never cancelled EXISTING orders and never engaged the sticky halt — so a
genuine 24h-loss / drawdown kill from the oversight side rode through.

FX-068:
  - _load_allocations captures the alloc's `kill_switch` + `kill_reason` into
    self._alloc_kill_switch / self._alloc_kill_reason on every fresh load.
  - run_cycle, right after the existing sticky-halt short-circuit, calls
    _activate_kill_switch("oversight:<reason>") when the flag is set.

These tests exercise both halves in isolation.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reward_farmer  # noqa: E402


def _make_farmer_stub() -> reward_farmer.RewardFarmer:
    """RewardFarmer with __init__ bypassed — only the attrs the FX-068
    paths touch are set."""
    rf = reward_farmer.RewardFarmer.__new__(reward_farmer.RewardFarmer)
    rf.mode = "LIVE"
    rf.dry_run = False
    rf.markets = {}
    rf.cycle_count = 0
    rf._kill_switch_active = False
    rf._kill_switch_reason = ""
    rf._alloc_kill_switch = False
    rf._alloc_kill_reason = ""
    return rf


def _write_alloc(path: str, payload: dict) -> None:
    with open(path, "w") as f:
        json.dump(payload, f)


class TestFX092KillSwitchPagesDiscord(unittest.TestCase):
    """FX-092: _activate_kill_switch must page Discord. A kill leaves the
    process alive-but-idle, so the stale-heartbeat alert never covers it —
    without this page the operator only learns of a halt by noticing zero
    orders (which is exactly how the 2026-06-02 fill-rate kill was missed)."""

    def test_activate_kill_switch_pages_once(self):
        rf = _make_farmer_stub()
        with patch.object(reward_farmer.alerts, "alert_kill_switch") as mock_alert:
            rf._activate_kill_switch("fill_rate_ratio=3.60 > 3.0x")
        mock_alert.assert_called_once()
        passed_reason = (
            mock_alert.call_args.args[0] if mock_alert.call_args.args
            else mock_alert.call_args.kwargs.get("reason", "")
        )
        self.assertIn("fill_rate", passed_reason)
        self.assertTrue(rf._kill_switch_active)

    def test_kill_switch_alert_failure_is_non_fatal(self):
        """A Discord failure must NOT prevent the kill from completing."""
        rf = _make_farmer_stub()
        with patch.object(
            reward_farmer.alerts, "alert_kill_switch",
            side_effect=RuntimeError("discord down"),
        ):
            rf._activate_kill_switch("test")  # must not raise
        self.assertTrue(rf._kill_switch_active)


class TestFX068LoadCapturesKill(unittest.TestCase):
    """_load_allocations must capture the alloc kill_switch field even when
    deploys is empty (the exact shape simple_allocator writes on a kill)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.alloc_path = os.path.join(self.tmpdir, "market_allocations.json")
        self.rf = _make_farmer_stub()
        # _load_allocations locates the file via os.path.dirname(__file__)
        self.patcher = patch.object(
            reward_farmer.os.path, "dirname", return_value=self.tmpdir,
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        for f in os.listdir(self.tmpdir):
            os.unlink(os.path.join(self.tmpdir, f))

    def test_kill_true_empty_deploys_captured(self):
        """kill_switch=True + deploys=[] → flag captured, reason captured,
        return is None (no deploys). This is the oversight-kill shape."""
        _write_alloc(self.alloc_path, {
            "kill_switch": True,
            "kill_reason": "24h realized loss $130.00 > 10% of wallet $1201.76",
            "markets": [],  # deploys=[] on a kill
        })
        result = self.rf._load_allocations()
        self.assertIsNone(result, "empty deploys still returns None")
        self.assertTrue(self.rf._alloc_kill_switch, "kill flag must be captured")
        self.assertIn("24h realized loss", self.rf._alloc_kill_reason)

    def test_kill_false_healthy_alloc_clears_flag(self):
        """A healthy alloc (kill_switch=False, deploys present) must leave the
        flag False so a stale capture can't linger."""
        self.rf._alloc_kill_switch = True  # simulate a stale prior capture
        _write_alloc(self.alloc_path, {
            "kill_switch": False,
            "markets": [
                {"condition_id": "0xabc", "action": "deploy", "shares_per_side": 50},
            ],
        })
        result = self.rf._load_allocations()
        self.assertIsNotNone(result, "deploy present → returns the deploy list")
        self.assertFalse(self.rf._alloc_kill_switch, "healthy alloc must clear the flag")

    def test_kill_absent_field_treated_as_false(self):
        """Legacy alloc with no kill_switch field → flag False (backward compat)."""
        _write_alloc(self.alloc_path, {
            "markets": [
                {"condition_id": "0xabc", "action": "deploy", "shares_per_side": 50},
            ],
        })
        self.rf._load_allocations()
        self.assertFalse(self.rf._alloc_kill_switch)

    def test_reason_truncated_to_200(self):
        """A pathologically long kill_reason is bounded (log hygiene)."""
        _write_alloc(self.alloc_path, {
            "kill_switch": True,
            "kill_reason": "x" * 500,
            "markets": [],
        })
        self.rf._load_allocations()
        self.assertLessEqual(len(self.rf._alloc_kill_reason), 200)


class TestFX068RunCycleHalts(unittest.TestCase):
    """run_cycle must convert a captured alloc kill into a real farmer halt."""

    def _stub_for_run_cycle(self) -> reward_farmer.RewardFarmer:
        rf = _make_farmer_stub()
        rf.order_lifecycle = MagicMock()
        rf._activate_kill_switch = MagicMock()
        rf._emit_cycle_telemetry = MagicMock()
        return rf

    def test_flag_set_triggers_activate(self):
        """_alloc_kill_switch=True + not already halted → _activate_kill_switch
        called once with an oversight: reason, then cycle returns early."""
        rf = self._stub_for_run_cycle()
        rf._alloc_kill_switch = True
        rf._alloc_kill_reason = "drawdown 15.2% > 15% from peak $1400.00"
        rf.run_cycle()
        rf._activate_kill_switch.assert_called_once()
        reason = rf._activate_kill_switch.call_args.kwargs.get("reason") \
            or rf._activate_kill_switch.call_args.args[0]
        self.assertTrue(reason.startswith("oversight:"))
        self.assertIn("drawdown", reason)

    def test_flag_unset_does_not_trigger(self):
        """No alloc kill → the FX-068 path must not fire. (run_cycle proceeds
        past the check; order_lifecycle is mocked so Step 1 is harmless.)"""
        rf = self._stub_for_run_cycle()
        rf._alloc_kill_switch = False
        rf.dry_run = True  # Step 1 dry path: no real exchange call
        rf.client = MagicMock()
        rf.client.get_open_orders.return_value = []
        # Stub out everything run_cycle touches after the FX-068 check so it
        # can run without a full farmer. We only assert the kill did NOT fire.
        for attr in ("dump_mgr", "_sweep_expiring_markets", "_guardrail_check_and_log"):
            setattr(rf, attr, MagicMock())
        try:
            rf.run_cycle()
        except Exception:
            # We don't care if a later step trips on the bare stub — only that
            # the FX-068 kill path didn't fire before it.
            pass
        rf._activate_kill_switch.assert_not_called()

    def test_already_halted_short_circuits_before_check(self):
        """If the sticky halt is already active, the top-of-cycle short-circuit
        returns BEFORE the FX-068 check — _activate_kill_switch is not
        re-invoked via this path."""
        rf = self._stub_for_run_cycle()
        rf._kill_switch_active = True
        rf._kill_switch_reason = "prior kill"
        rf._alloc_kill_switch = True  # would fire if reached
        rf.run_cycle()
        rf._activate_kill_switch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
