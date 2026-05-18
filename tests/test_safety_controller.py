"""SafetyController unit tests for Phase 1 bootstrap correctness.

Coverage targets (FX-002 / FX-003 / FX-012, see ``fixit.md``):

* ``_is_genuine_cold_start`` — empty DB, populated DB, missing-table behaviour.
* I3 drawdown — skipped on genuine cold start, fires on warm DB with zero portfolio.

Phase 6 (FX-016) is the broader SafetyController coverage build-out; this file
locks the Phase 1 surface only.
"""

import os
import sqlite3
import tempfile
import time
import unittest

from oversight import safety_controller as sc_mod
from oversight.safety_controller import (
    DATA_UNAVAILABLE,
    PRIORITY_CRITICAL,
    SafetyController,
)


def _fresh_db_with_scoring_snapshot() -> str:
    """Build a DB that has the tables the SafetyController touches.

    ``scoring_snapshots`` is populated so I9 freshness doesn't dominate.
    ``orders_placed`` and ``fills`` are created empty so ``_is_genuine_cold_start``
    returns True. ``portfolio_snapshots`` is created empty.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS scoring_snapshots ("
        "id INTEGER PRIMARY KEY, ts REAL, order_id TEXT, condition_id TEXT, "
        "side TEXT, scoring INTEGER, price REAL, shares REAL)"
    )
    db.execute(
        "INSERT INTO scoring_snapshots (ts, order_id, condition_id, side, "
        "scoring, price, shares) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (time.time() - 60, "t", "t", "yes", 1, 0.5, 100),
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS orders_placed ("
        "id INTEGER PRIMARY KEY, ts REAL, condition_id TEXT, side TEXT, "
        "price REAL, size REAL, order_id TEXT DEFAULT '', "
        "order_type TEXT DEFAULT 'BUY')"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS fills ("
        "ts REAL, condition_id TEXT, side TEXT, fill_type TEXT, "
        "shares REAL, price REAL, clob_cost REAL, usd_value REAL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS unwinds (ts REAL, condition_id TEXT, usd_value REAL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS stop_losses (ts REAL, condition_id TEXT, loss_usd REAL)"
    )
    db.commit()
    db.close()
    return path


class TestIsGenuineColdStart(unittest.TestCase):

    def setUp(self):
        self.path = _fresh_db_with_scoring_snapshot()
        self.sc = SafetyController(db_path=self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_empty_orders_and_fills_returns_true(self):
        self.assertTrue(self.sc._is_genuine_cold_start())

    def test_orders_present_returns_false(self):
        db = sqlite3.connect(self.path)
        db.execute(
            "INSERT INTO orders_placed (ts, condition_id, side, price, size) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), "cid", "yes", 0.5, 50),
        )
        db.commit()
        db.close()
        self.assertFalse(self.sc._is_genuine_cold_start())

    def test_fills_present_returns_false(self):
        db = sqlite3.connect(self.path)
        db.execute(
            "INSERT INTO fills (ts, condition_id, side, fill_type, shares, "
            "price, clob_cost, usd_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), "cid", "yes", "BUY", 50, 0.5, 0.5, 25.0),
        )
        db.commit()
        db.close()
        self.assertFalse(self.sc._is_genuine_cold_start())

    def test_missing_orders_table_returns_false(self):
        # Conservative default: when we can't query, assume warm DB so existing
        # defences (I3 → DATA_UNAVAILABLE, I9 → None) still fire.
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        sc = SafetyController(db_path=path)
        try:
            self.assertFalse(sc._is_genuine_cold_start())
        finally:
            os.unlink(path)


class TestI3ColdStartSkip(unittest.TestCase):
    """FX-002 — I3 drawdown skipped on genuine cold start, otherwise unchanged."""

    def setUp(self):
        self.path = _fresh_db_with_scoring_snapshot()

    def tearDown(self):
        os.unlink(self.path)

    def _eval_zero_portfolio(self, sc):
        return sc.evaluate_state(
            correction_factor_raw=0.15,
            estimated_daily_total=30,
            actual_daily_payout=10.0,
            reward_payout_24h=10.0,
            num_scoring_markets=10,
            exchange_balance=0.0,
            total_portfolio_value=0.0,
        )

    def test_cold_start_no_drawdown_violation(self):
        sc = SafetyController(db_path=self.path)
        self._eval_zero_portfolio(sc)
        drawdown_violations = [
            v for v in sc.violations if v.invariant == "drawdown"
        ]
        self.assertEqual(
            [], drawdown_violations,
            f"Expected no drawdown violation on cold start, got {drawdown_violations}",
        )

    def test_cold_start_state_not_data_unavailable_from_i3(self):
        # On a true cold start with no other CRITICAL violations, I3 must not
        # be the reason state slips to DATA_UNAVAILABLE. (Other invariants may
        # still place state elsewhere — we only assert I3 isn't the driver.)
        sc = SafetyController(db_path=self.path)
        self._eval_zero_portfolio(sc)
        drawdown_critical = [
            v for v in sc.violations
            if v.invariant == "drawdown" and v.priority == PRIORITY_CRITICAL
        ]
        self.assertEqual([], drawdown_critical)

    def test_warm_db_still_fires_data_unavailable(self):
        # When orders_placed has rows, I3 must still demote to DATA_UNAVAILABLE
        # on zero portfolio — this is the genuine API-failure case.
        db = sqlite3.connect(self.path)
        db.execute(
            "INSERT INTO orders_placed (ts, condition_id, side, price, size) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), "cid", "yes", 0.5, 50),
        )
        db.commit()
        db.close()
        sc = SafetyController(db_path=self.path)
        self._eval_zero_portfolio(sc)
        drawdown_violations = [
            v for v in sc.violations if v.invariant == "drawdown"
        ]
        self.assertEqual(1, len(drawdown_violations))
        self.assertEqual(DATA_UNAVAILABLE, drawdown_violations[0].severity)


if __name__ == "__main__":
    unittest.main()
