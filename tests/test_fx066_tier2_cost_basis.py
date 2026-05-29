"""FX-066 Tier 2 — reconstruct cost basis at orphan registration.

Orphans are discovered from on-chain balance and registered via set_shares,
which (pre-Tier-2) set shares but no price → avg_price=0 → vwap_cost=0 at
dump time (Tier 1 floors the pnl, but the magnitude is blind) AND
get_position()=0 (invisible to notional guardrails).

Tier 2 reconstructs avg_price from the fills table (db.fills_vwap) at
registration. The orphan scan only considers cids that HAVE fills in the
last 7d, so the cost basis is recoverable for scan-discovered orphans. This
fixes the corruption at the source — for dump pnl, ROI, AND notional.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import BotDatabase  # noqa: E402
import state  # noqa: E402


class TestFX066Tier2FillsVwap(unittest.TestCase):
    """db.fills_vwap reconstructs (total_shares, YES-equiv VWAP) from fills."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = BotDatabase(os.path.join(self.tmpdir, "t.db"))

    def tearDown(self):
        for f in os.listdir(self.tmpdir):
            os.unlink(os.path.join(self.tmpdir, f))

    def _fill(self, cid, side, shares, price, eid):
        self.db.log_fill(condition_id=cid, question="q", side=side,
                         fill_type="FULL", shares=shares, price=price,
                         clob_cost=price, usd_value=shares * price,
                         fill_event_id=eid)

    def test_weighted_vwap(self):
        """30sh @ 0.40 + 20sh @ 0.60 → VWAP 0.48 over 50 shares."""
        self._fill("0xa", "yes", 30, 0.40, "e1")
        self._fill("0xa", "yes", 20, 0.60, "e2")
        total, vwap = self.db.fills_vwap("0xa", "yes")
        self.assertEqual(50.0, total)
        self.assertAlmostEqual(0.48, vwap, places=6)

    def test_empty_returns_zero(self):
        self.assertEqual((0.0, 0.0), self.db.fills_vwap("0xnone", "yes"))

    def test_side_isolation(self):
        self._fill("0xa", "yes", 10, 0.30, "y1")
        self._fill("0xa", "no", 10, 0.70, "n1")
        _, vwap_yes = self.db.fills_vwap("0xa", "yes")
        _, vwap_no = self.db.fills_vwap("0xa", "no")
        self.assertAlmostEqual(0.30, vwap_yes, places=6)
        self.assertAlmostEqual(0.70, vwap_no, places=6)


class TestFX066Tier2SetShares(unittest.TestCase):
    """PositionStore.set_shares(avg_price=...) sets the cost basis."""

    def setUp(self):
        # Patch persistence so the test doesn't touch disk.
        self.p_load = patch.object(state.PositionStore, "_load", lambda self: None)
        self.p_save = patch.object(state.PositionStore, "_save", lambda self: None)
        self.p_load.start()
        self.p_save.start()
        self.ps = state.PositionStore()
        self.ps.register_market("0xa", "Q?")

    def tearDown(self):
        self.p_load.stop()
        self.p_save.stop()

    def test_avg_price_sets_cost_basis(self):
        """set_shares with avg_price → get_avg_price reflects it, and the
        position has non-zero USD exposure (visible to guardrails)."""
        self.ps.set_shares("0xa", "yes", 100, avg_price=0.48)
        self.assertAlmostEqual(0.48, self.ps.get_avg_price("0xa", "yes"), places=6)
        self.assertGreater(self.ps.get_position("0xa", "yes"), 0,
            "cost basis set → USD exposure visible (not $0)")

    def test_no_avg_price_leaves_zero(self):
        """set_shares without avg_price (the pre-Tier-2 orphan path) → cost
        basis stays 0 (this is the case the Tier 1 floor must catch)."""
        self.ps.set_shares("0xa", "yes", 100)
        self.assertEqual(0.0, self.ps.get_avg_price("0xa", "yes"))
        self.assertEqual(0.0, self.ps.get_position("0xa", "yes"))

    def test_none_preserves_existing_cost_basis(self):
        """A later share-only correction (avg_price=None) must NOT clobber a
        live VWAP — this is why site 441 (_reconcile_positions) is left as-is."""
        self.ps.record_fill("0xa", "yes", 50, 0.50)
        self.assertAlmostEqual(0.50, self.ps.get_avg_price("0xa", "yes"), places=6)
        self.ps.set_shares("0xa", "yes", 40)  # share-only correction
        self.assertAlmostEqual(0.50, self.ps.get_avg_price("0xa", "yes"), places=6,
            msg="avg_price=None must preserve existing VWAP")


class TestFX066Tier2EndToEnd(unittest.TestCase):
    """The orphan reconstruction: fills_vwap → set_shares → real cost basis,
    so a subsequent dump computes vwap_cost>0 (the FX-066 bug is closed)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = BotDatabase(os.path.join(self.tmpdir, "t.db"))
        self.p_load = patch.object(state.PositionStore, "_load", lambda self: None)
        self.p_save = patch.object(state.PositionStore, "_save", lambda self: None)
        self.p_load.start()
        self.p_save.start()
        self.ps = state.PositionStore()

    def tearDown(self):
        self.p_load.stop()
        self.p_save.stop()
        for f in os.listdir(self.tmpdir):
            os.unlink(os.path.join(self.tmpdir, f))

    def test_orphan_with_fills_recovers_cost_basis(self):
        # Simulate the orphan scan: fills exist for the cid, position was lost.
        self.db.log_fill(condition_id="0xa", question="q", side="yes",
                         fill_type="FULL", shares=200, price=0.45,
                         clob_cost=0.45, usd_value=90.0, fill_event_id="e1")
        total, vwap = self.db.fills_vwap("0xa", "yes")
        self.assertEqual(200.0, total)
        # Register the orphan WITH the reconstructed cost basis (the Tier 2 path).
        self.ps.register_market("0xa", "orphan")
        self.ps.set_shares("0xa", "yes", 200, avg_price=vwap if vwap > 0 else None)
        # A dump would now see avg_price=0.45 → vwap_cost = 200*0.45 = $90,
        # not 0 → real loss magnitude visible to the kill, not Tier-1-floored.
        self.assertAlmostEqual(0.45, self.ps.get_avg_price("0xa", "yes"), places=6)
        self.assertGreater(self.ps.get_position("0xa", "yes"), 0)


if __name__ == "__main__":
    unittest.main()
