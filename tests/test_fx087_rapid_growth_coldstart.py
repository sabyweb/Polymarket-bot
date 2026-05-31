"""FX-087 — rapid-growth (acceleration) kill must not false-fire on cold start.

Surfaced LIVE by the first canary: the FX-058 rapid-growth kill computes
max/min of notional_ratio over a 300s window. On the first live placement the
window minimum is ~0 (the bot was flat), so 0 -> 0.16x read as a 1578x "burst"
and sticky-killed the farmer, cancelling its orders. Because the bot had never
run live, this had never manifested — it would have fired on EVERY live restart
ramp-up, blocking the entire deployment, not just the canary.

Fix: the kill only ARMS once the window minimum is an established operating
baseline (>= RF_RAPID_GROWTH_MIN_BASELINE_RATIO, default 0.5x). Ramps from ~0
are normal startup, bounded by the static soft/hard notional caps. A genuine
burst from an established baseline still kills (FX-058 preserved).

Mirrors tests/test_p1_farmer_retune.py::TestAT_B_RapidGrowthKill.
"""

from __future__ import annotations

import collections
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reward_farmer  # noqa: E402
from config import cfg as _real_cfg  # noqa: E402


def _stub():
    rf = reward_farmer.RewardFarmer.__new__(reward_farmer.RewardFarmer)
    rf._notional_ratio_samples = collections.deque()
    return rf


def _prime(rf, prior_vals, now=None):
    """Seed the window with prior samples (most-recent last), within 300s."""
    now = now or time.time()
    n = len(prior_vals)
    for i, v in enumerate(prior_vals):
        rf._notional_ratio_samples.append((now - (n - i) * 5.0, float(v)))


class TestFX087ColdStartNoFalseKill(unittest.TestCase):
    # Real cfg defaults: kill_ratio=5.0, window=300s, baseline_floor=0.5.

    def test_canary_first_placement_does_not_kill(self):
        """THE canary repro: flat (0,0,0) then first orders -> 0.158x.
        Pre-FX-087 this fired a 1578x burst kill; now it must NOT kill."""
        rf = _stub()
        _prime(rf, [0.0, 0.0, 0.0])
        kill, mult = rf._guardrail_rapid_notional_growth(0.158)
        self.assertFalse(kill)
        self.assertIsNone(mult)

    def test_sub_baseline_steady_never_kills(self):
        """A bounded canary holding ~0.16x (below the 0.5x baseline) never
        arms the acceleration kill — the static caps bound this range."""
        rf = _stub()
        _prime(rf, [0.158] * 10)
        kill, _ = rf._guardrail_rapid_notional_growth(0.158)
        self.assertFalse(kill)

    def test_established_baseline_burst_still_kills(self):
        """FX-058 preserved: from an established 1.0x baseline, a jump to 6.0x
        (6x > 5x) over the window still trips the kill."""
        rf = _stub()
        _prime(rf, [1.0])
        kill, mult = rf._guardrail_rapid_notional_growth(6.0)
        self.assertTrue(kill)
        self.assertGreater(mult, 5.0)

    def test_established_baseline_gradual_no_kill(self):
        rf = _stub()
        _prime(rf, [3.0, 3.5])
        kill, _ = rf._guardrail_rapid_notional_growth(4.0)  # 1.33x < 5x
        self.assertFalse(kill)

    def test_baseline_boundary(self):
        # min exactly at the 0.5x floor -> armed -> kills on a 6x burst
        rf = _stub()
        _prime(rf, [0.5])
        kill, _ = rf._guardrail_rapid_notional_growth(3.0)  # 6x
        self.assertTrue(kill)
        # just below the floor -> disarmed
        rf2 = _stub()
        _prime(rf2, [0.49])
        kill2, _ = rf2._guardrail_rapid_notional_growth(3.0)
        self.assertFalse(kill2)

    def test_all_zero_window_no_crash_no_kill(self):
        rf = _stub()
        _prime(rf, [0.0, 0.0])
        try:
            kill, _ = rf._guardrail_rapid_notional_growth(0.0)
        except ZeroDivisionError:
            self.fail("must not raise on all-zero window")
        self.assertFalse(kill)

    def test_disabled_baseline_floor_no_divzero(self):
        """Escape hatch: RF_RAPID_GROWTH_MIN_BASELINE_RATIO=0 keeps the kill
        always-armed (legacy) but must still never divide by ~0."""
        def _cfg(k):
            return 0.0 if k == "RF_RAPID_GROWTH_MIN_BASELINE_RATIO" else _real_cfg(k)
        rf = _stub()
        _prime(rf, [0.0])
        with patch.object(reward_farmer, "cfg", _cfg):
            try:
                kill, mult = rf._guardrail_rapid_notional_growth(0.5)
            except ZeroDivisionError:
                self.fail("must not raise even with baseline guard disabled")
        self.assertFalse(kill)  # genuine ~0 min -> undefined burst -> no kill


if __name__ == "__main__":
    unittest.main()
