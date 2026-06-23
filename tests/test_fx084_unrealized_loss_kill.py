"""FX-084 — held-inventory (unrealized) loss kill.

The pre-FX-084 kill limbs only saw REALIZED loss (unwinds.pnl<0, 10% of
capital) and wallet-CASH drawdown (15% from peak). Neither catches a marked-down
OPEN position or an FX-071 floored-but-unfilled dump that bleeds without ever
crystallizing a negative unwind or lowering the cash peak. _guardrail_unrealized_loss
marks every held leg to the market midpoint and trips the sticky kill when NET
unrealized loss exceeds RF_UNREALIZED_LOSS_KILL_FRAC of total_capital.

Mirrors tests/test_fx082_farmer_drawdown_backstop.py (bare-farmer + MagicMock db).
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reward_farmer  # noqa: E402


def _mkt(mid: float):
    """Minimal stand-in for MarketState — the limb only reads .midpoint."""
    return types.SimpleNamespace(midpoint=mid)


def _farmer(positions: dict, markets: dict):
    rf = reward_farmer.RewardFarmer.__new__(reward_farmer.RewardFarmer)
    rf.db = MagicMock()
    rf.db.load_all_positions.return_value = positions
    rf.markets = markets
    return rf


def _pos(yes_shares=0.0, yes_avg=0.0, no_shares=0.0, no_avg=0.0):
    return {
        "yes_shares": yes_shares, "yes_avg_price": yes_avg,
        "no_shares": no_shares, "no_avg_price": no_avg,
    }


# Default test frac/capital: 20% of $100 → kill above $20 net unrealized loss.
FRAC = 0.20
T = 100.0


class TestFX084UnrealizedLossKill(unittest.TestCase):

    def _call(self, rf, total_capital=T, frac=FRAC, unknown_floor=False):
        def _cfg(name):
            if name == "RF_FX084_UNKNOWN_COST_BASIS_FLOOR_ENABLED":
                return unknown_floor
            return frac
        with patch.object(reward_farmer, "cfg", _cfg):
            return rf._guardrail_unrealized_loss(total_capital)

    # ── Kill paths ──────────────────────────────────────────────────────────

    def test_kill_on_yes_markdown(self):
        # YES 100 @ $0.50, mid 0.20 → pnl = 100·(0.20-0.50) = -$30 loss > $20.
        rf = _farmer({"c": _pos(yes_shares=100, yes_avg=0.50)}, {"c": _mkt(0.20)})
        kill, reason = self._call(rf)
        self.assertTrue(kill)
        self.assertIn("unrealized_loss", reason)
        self.assertAlmostEqual(rf._last_unrealized_loss, 30.0, places=4)

    def test_kill_on_no_markdown(self):
        # NO 100 @ yes-equiv 0.29, mid 0.50 → loss $21 > $20 kill threshold.
        rf = _farmer({"c": _pos(no_shares=100, no_avg=0.29)}, {"c": _mkt(0.50)})
        kill, reason = self._call(rf)
        self.assertTrue(kill)
        self.assertAlmostEqual(rf._last_unrealized_loss, 21.0, places=1)

    # ── Below-threshold / net-offset ─────────────────────────────────────────

    def test_no_kill_below_threshold(self):
        # YES 100 @ $0.50, mid 0.40 → -$10 loss < $20.
        rf = _farmer({"c": _pos(yes_shares=100, yes_avg=0.50)}, {"c": _mkt(0.40)})
        kill, reason = self._call(rf)
        self.assertFalse(kill)
        self.assertEqual(reason, "")
        self.assertAlmostEqual(rf._last_unrealized_loss, 10.0, places=4)

    def test_net_gain_offsets_loss(self):
        # c1 YES underwater -$30; c2 NO in profit +$10 → net -$20, no kill at $20.
        rf = _farmer(
            {"c1": _pos(yes_shares=100, yes_avg=0.50),
             "c2": _pos(no_shares=100, no_avg=0.30)},
            {"c1": _mkt(0.20), "c2": _mkt(0.20)},
        )
        kill, _ = self._call(rf)
        self.assertFalse(kill)
        self.assertAlmostEqual(rf._last_unrealized_loss, 20.0, places=1)

    def test_hedged_pair_near_zero_unrealized(self):
        # Symmetric hedged pair @ mid → net unrealized ≈ 0.
        rf = _farmer(
            {"c": _pos(yes_shares=100, yes_avg=0.50, no_shares=100, no_avg=0.50)},
            {"c": _mkt(0.50)},
        )
        kill, _ = self._call(rf)
        self.assertFalse(kill)
        self.assertAlmostEqual(rf._last_unrealized_loss, 0.0, places=4)

    def test_no_overcount_capped_at_cost_basis(self):
        # NO markdown: loss capped at cost basis 100*(1-0.80)=20.
        rf = _farmer({"c": _pos(no_shares=100, no_avg=0.80)}, {"c": _mkt(0.95)})
        kill, _ = self._call(rf)
        self.assertAlmostEqual(rf._last_unrealized_loss, 15.0, places=1)
        self.assertLessEqual(rf._last_unrealized_loss, 20.0 + 0.1)

    # ── Fail-open paths (return (False, "") and never false-kill) ─────────────

    def test_failopen_total_capital_none(self):
        rf = _farmer({"c": _pos(yes_shares=100, yes_avg=0.50)}, {"c": _mkt(0.10)})
        self.assertEqual(self._call(rf, total_capital=None), (False, ""))
        self.assertIsNone(rf._last_unrealized_loss)

    def test_failopen_total_capital_zero(self):
        rf = _farmer({"c": _pos(yes_shares=100, yes_avg=0.50)}, {"c": _mkt(0.10)})
        self.assertEqual(self._call(rf, total_capital=0.0), (False, ""))

    def test_failopen_no_positions(self):
        rf = _farmer({}, {})
        self.assertEqual(self._call(rf), (False, ""))

    def test_failopen_market_not_tracked(self):
        # Position exists but its market isn't in self.markets → no mark → skip.
        rf = _farmer({"c": _pos(yes_shares=100, yes_avg=0.50)}, {})
        self.assertEqual(self._call(rf), (False, ""))
        self.assertIsNone(rf._last_unrealized_loss)

    def test_skip_unknown_cost_basis(self):
        # avg_price 0 (orphan/startup) → leg skipped even with a deep markdown.
        rf = _farmer({"c": _pos(yes_shares=100, yes_avg=0.0)}, {"c": _mkt(0.05)})
        self.assertEqual(self._call(rf), (False, ""))

    def test_unknown_cost_basis_floor_enabled(self):
        # avg_price 0 + flag ON → leg counted at midpoint floor.
        # YES 100 @ mid 0.05 (floor avg = 0.05) → pnl = 0, so unrealized loss = 0.
        # The leg is marked (not silently skipped) and will contribute if mid moves.
        rf = _farmer({"c": _pos(yes_shares=100, yes_avg=0.0)}, {"c": _mkt(0.05)})
        kill, reason = self._call(rf, frac=FRAC, unknown_floor=True)
        self.assertFalse(kill)
        self.assertAlmostEqual(rf._last_unrealized_loss, 0.0, places=4)

    def test_unknown_cost_basis_floor_counts_marked_leg(self):
        # avg_price 0 + flag ON → leg is marked even though current unrealized loss is 0.
        rf = _farmer({"c": _pos(yes_shares=100, yes_avg=0.0)}, {"c": _mkt(0.30)})
        kill, reason = self._call(rf, frac=FRAC, unknown_floor=True)
        self.assertFalse(kill)
        self.assertAlmostEqual(rf._last_unrealized_loss, 0.0, places=4)
        self.assertEqual(reason, "")

    def test_known_and_unknown_leg_together(self):
        # c1: known YES avg 0.50, mid 0.20 → -$30 loss.
        # c2: unknown NO avg, mid 0.20 → floor avg=0.20, pnl = 0.
        # Net loss = $30 > $20 threshold → kill fires.
        rf = _farmer(
            {"c1": _pos(yes_shares=100, yes_avg=0.50),
             "c2": _pos(no_shares=100, no_avg=0.0)},
            {"c1": _mkt(0.20), "c2": _mkt(0.20)},
        )
        kill, _ = self._call(rf, unknown_floor=True)
        self.assertTrue(kill)
        self.assertAlmostEqual(rf._last_unrealized_loss, 30.0, places=4)

    def test_skip_invalid_midpoint(self):
        for bad_mid in (0.0, 1.0, -0.1, 1.5):
            rf = _farmer({"c": _pos(yes_shares=100, yes_avg=0.50)}, {"c": _mkt(bad_mid)})
            self.assertEqual(self._call(rf), (False, ""), f"mid={bad_mid}")

    def test_disabled_when_frac_zero(self):
        rf = _farmer({"c": _pos(yes_shares=100, yes_avg=0.50)}, {"c": _mkt(0.01)})
        self.assertEqual(self._call(rf, frac=0.0), (False, ""))

    def test_failopen_on_db_error(self):
        rf = _farmer({}, {})
        rf.db.load_all_positions.side_effect = Exception("locked")
        self.assertEqual(self._call(rf), (False, ""))


if __name__ == "__main__":
    unittest.main()
