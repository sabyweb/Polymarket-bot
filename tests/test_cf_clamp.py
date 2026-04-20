"""Tests for the CF smoothing clamp in _smooth_correction_factor.

Fix 2 lowered the floor from 0.001 to 1e-6. The floor's purpose is to keep
CF "effectively nonzero" for downstream logs/formatting; it no longer
serves as a division-by-zero guard because no consumer divides by CF
(audited 2026-04-20).
"""

import os
import sys
import sqlite3
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oversight.data_collector import _smooth_correction_factor


class TestCFClampFloor(unittest.TestCase):
    """Verify the new [1e-6, 10.0] clamp on smoothed correction factor."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_smooth_clamp_allows_values_below_0_001(self):
        """Raw = 0.0002 (the production miscalibration) no longer gets
        inflated to 0.001. Result must be <= raw (circuit breaker path
        uses raw directly when raw < 0.01)."""
        raw = 0.0002
        smoothed = _smooth_correction_factor(
            raw_factor=raw, db_path=self.db_path,
            estimated_daily=57973.0, actual_daily=11.59, deployed_count=40,
        )
        self.assertLess(smoothed, 0.001,
                        f"Clamp still at 0.001 — got {smoothed}")
        self.assertGreaterEqual(smoothed, 1e-6,
                                f"Below new 1e-6 floor — got {smoothed}")
        # Circuit breaker at data_collector.py:1158: raw < 0.01 → use raw.
        self.assertAlmostEqual(smoothed, raw, places=10)

    def test_smooth_clamp_prevents_exact_zero(self):
        """Raw = 0.0 → smoothed clamped to 1e-6, never exactly zero.
        Preserves "effectively nonzero" semantics for multiplicands."""
        smoothed = _smooth_correction_factor(
            raw_factor=0.0, db_path=self.db_path,
            estimated_daily=100.0, actual_daily=0.0, deployed_count=10,
        )
        self.assertEqual(smoothed, 1e-6,
                         f"Zero not clamped to 1e-6 — got {smoothed}")

    def test_smooth_clamp_ceiling_unchanged(self):
        """Raw = 100 still gets clamped to 10.0 (upper bound unchanged)."""
        smoothed = _smooth_correction_factor(
            raw_factor=100.0, db_path=self.db_path,
            estimated_daily=0.5, actual_daily=50.0, deployed_count=10,
        )
        self.assertEqual(smoothed, 10.0,
                         f"Upper clamp not at 10.0 — got {smoothed}")

    def test_healthy_cf_unchanged(self):
        """Values in the healthy range (e.g. 1.0) are passed through
        modulo EMA smoothing; floor/ceiling never kick in."""
        smoothed = _smooth_correction_factor(
            raw_factor=1.0, db_path=self.db_path,
            estimated_daily=100.0, actual_daily=100.0, deployed_count=10,
        )
        self.assertGreater(smoothed, 0.1)
        self.assertLess(smoothed, 10.0)


if __name__ == "__main__":
    unittest.main()
