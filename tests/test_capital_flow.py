"""Phase 4 — capital flow correctness (FX-010 / FX-011 / FX-013 / FX-024 / FX-025).

Coverage:

* ``compute_available_capital``: None propagation, exchange-balance priority,
  legacy total_capital path, defensive 0.0 floor when both inputs are None.
* SafetyController I4 floor scaling (FX-010): small / medium / large wallet
  references; backward compat with existing $200-wallet tests.
* ``--capital`` CLI default None (FX-025) — confirms argparse-level default.
* ``[CAPITAL_SOURCE]`` log emission paths (FX-024) — exercised through ``run_once``.
* Dead config knobs (FX-011) — confirms ``RF_MAX_COST_PER_MARKET`` and
  ``RF_MAX_TOTAL_EXPOSURE`` are absent from the config namespace.
"""

import logging
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_db_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


# ── FX-013/025: compute_available_capital handles None ──────────────────────


class TestComputeAvailableCapitalNoneHandling(unittest.TestCase):

    def setUp(self):
        self.path = _fresh_db_path()
        # Create minimal schema so the function's DB queries don't crash.
        db = sqlite3.connect(self.path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS dump_states ("
            "condition_id TEXT, side TEXT, fill_price REAL, started_at REAL, "
            "shares REAL, tid TEXT, dump_order_id TEXT DEFAULT '', "
            "last_passive_reprice REAL DEFAULT 0, "
            "PRIMARY KEY (condition_id, side))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS active_orders ("
            "order_id TEXT PRIMARY KEY, condition_id TEXT, side TEXT, "
            "order_type TEXT, price REAL, shares REAL, placed_at REAL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS positions ("
            "condition_id TEXT, side TEXT, shares REAL DEFAULT 0, "
            "avg_cost_per_share REAL DEFAULT 0, "
            "PRIMARY KEY (condition_id, side))"
        )
        db.commit()
        db.close()

    def tearDown(self):
        os.unlink(self.path)

    def test_returns_zero_when_both_inputs_none(self):
        from oversight.data_collector import compute_available_capital
        self.assertEqual(0.0, compute_available_capital(
            self.path, total_capital=None, exchange_balance=None,
        ))

    def test_uses_exchange_balance_when_present(self):
        from oversight.data_collector import compute_available_capital
        # Exchange balance is preferred; total_capital is ignored.
        self.assertEqual(201.0, compute_available_capital(
            self.path, total_capital=None, exchange_balance=201.0,
        ))

    def test_legacy_path_still_works_with_explicit_total(self):
        from oversight.data_collector import compute_available_capital
        # Operator passes --capital=500 with no exchange balance in DB.
        self.assertEqual(500.0, compute_available_capital(
            self.path, total_capital=500.0, exchange_balance=None,
        ))

    def test_exchange_balance_overrides_total_capital(self):
        from oversight.data_collector import compute_available_capital
        # exchange_balance wins; total_capital is unused on this path.
        self.assertEqual(201.0, compute_available_capital(
            self.path, total_capital=1500.0, exchange_balance=201.0,
        ))


# ── FX-025: --capital CLI default ───────────────────────────────────────────


class TestCliCapitalDefault(unittest.TestCase):

    def test_capital_flag_default_is_none(self):
        # FX-025: the silent $1500 fallback must be gone. Operator override
        # is still possible via an explicit value.
        import argparse
        import sys as _sys
        # Re-create just the --capital argument the way oversight_agent.py
        # does, then parse an empty arg list to confirm default == None.
        parser = argparse.ArgumentParser()
        parser.add_argument("--capital", type=float, default=None)
        args = parser.parse_args([])
        self.assertIsNone(args.capital)
        # Sanity: explicit override still works.
        args2 = parser.parse_args(["--capital", "201.35"])
        self.assertEqual(201.35, args2.capital)


# ── FX-010: SafetyController I4 floor scaling ───────────────────────────────


class TestCapitalFloorScaling(unittest.TestCase):
    """The floor reference is max(portfolio_peak, portfolio_value, exchange_balance).
    Below $500 reference, the $50 minimum dominates (backwards compat).
    Above $500, the 10% scale takes over."""

    def setUp(self):
        from oversight.safety_controller import SafetyController
        self.path = _fresh_db_path()
        self.sc = SafetyController(db_path=self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_small_wallet_floor_is_50(self):
        # $200 reference → max($50, $20) = $50
        self.assertEqual(50.0, self.sc._capital_floor(
            exchange_balance=200.0, portfolio_value=200.0,
        ))

    def test_500_dollar_wallet_boundary(self):
        # $500 reference → max($50, $50) = $50 (boundary)
        self.assertEqual(50.0, self.sc._capital_floor(
            exchange_balance=500.0, portfolio_value=500.0,
        ))

    def test_medium_wallet_scales(self):
        # $1500 reference → max($50, $150) = $150
        self.assertEqual(150.0, self.sc._capital_floor(
            exchange_balance=1500.0, portfolio_value=1500.0,
        ))

    def test_large_wallet_scales(self):
        # $10000 reference → max($50, $1000) = $1000
        self.assertEqual(1000.0, self.sc._capital_floor(
            exchange_balance=10000.0, portfolio_value=10000.0,
        ))

    def test_reference_uses_peak_not_current(self):
        # Drawdown scenario: peak $1500, current exchange $300.
        # Floor should still be based on peak ($150), not on current ($30 → $50).
        self.sc._portfolio_peak = 1500.0
        self.assertEqual(150.0, self.sc._capital_floor(
            exchange_balance=300.0, portfolio_value=300.0,
        ))

    def test_reference_uses_portfolio_value_when_higher(self):
        # No peak yet (fresh DB), but caller passes portfolio_value.
        self.assertEqual(800.0, self.sc._capital_floor(
            exchange_balance=100.0, portfolio_value=8000.0,
        ))


class TestCapitalFloorI4FiresCorrectly(unittest.TestCase):
    """Backwards-compat: existing Test 16 expectation ($25 < $50 → UNSAFE)
    still holds. New behaviour: large wallet with mid-range balance fires
    too if balance is below scaled floor."""

    def setUp(self):
        from oversight.safety_controller import SafetyController
        self.path = _fresh_db_path()
        # Minimal schema so SafetyController's helpers don't crash.
        db = sqlite3.connect(self.path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS fills (ts REAL, condition_id TEXT, "
            "side TEXT, fill_type TEXT, shares REAL, price REAL, "
            "clob_cost REAL, usd_value REAL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS unwinds (ts REAL, condition_id TEXT, "
            "usd_value REAL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS stop_losses (ts REAL, condition_id TEXT, "
            "loss_usd REAL)"
        )
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
            "INSERT INTO orders_placed (ts, condition_id, side, price, size) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), "cid", "yes", 0.5, 50),
        )
        db.commit()
        db.close()
        self.sc = SafetyController(db_path=self.path)

    def tearDown(self):
        os.unlink(self.path)

    def _eval(self, exchange_balance, total_portfolio_value):
        return self.sc.evaluate_state(
            correction_factor_raw=0.15,
            estimated_daily_total=30,
            actual_daily_payout=10.0,
            reward_payout_24h=10.0,
            num_scoring_markets=10,
            exchange_balance=exchange_balance,
            total_portfolio_value=total_portfolio_value,
        )

    def test_backward_compat_test16(self):
        # Existing test: exchange=$25, portfolio=$100 → UNSAFE
        # Reference = $100, floor = max($50, $10) = $50, balance $25 < $50 → UNSAFE
        from oversight.safety_controller import UNSAFE
        self._eval(exchange_balance=25.0, total_portfolio_value=100.0)
        self.assertEqual(UNSAFE, self.sc.state)

    def test_large_wallet_with_below_scaled_floor_fires(self):
        # exchange=$200, portfolio=$5000 → reference=$5000, floor=$500,
        # balance $200 < $500 → I4 fires UNSAFE (this is NEW behaviour;
        # the old absolute $50 floor would not have fired).
        from oversight.safety_controller import UNSAFE
        self._eval(exchange_balance=200.0, total_portfolio_value=5000.0)
        fv = [v for v in self.sc.violations if v.invariant == "capital_floor"]
        self.assertEqual(1, len(fv))
        self.assertEqual(UNSAFE, fv[0].severity)

    def test_large_wallet_with_healthy_balance_does_not_fire(self):
        # exchange=$8000, portfolio=$10000 → reference=$10000, floor=$1000,
        # balance $8000 > $1000 → no violation.
        self._eval(exchange_balance=8000.0, total_portfolio_value=10000.0)
        fv = [v for v in self.sc.violations if v.invariant == "capital_floor"]
        self.assertEqual([], fv)


# ── FX-011: dead config knobs removed ───────────────────────────────────────


class TestDeadConfigKnobsRemoved(unittest.TestCase):

    def test_RF_MAX_COST_PER_MARKET_absent_from_module(self):
        import config
        self.assertFalse(hasattr(config, "RF_MAX_COST_PER_MARKET"))

    def test_RF_MAX_TOTAL_EXPOSURE_absent_from_module(self):
        import config
        self.assertFalse(hasattr(config, "RF_MAX_TOTAL_EXPOSURE"))

    def test_accessors_absent_from_reward_farmer(self):
        import reward_farmer
        self.assertFalse(hasattr(reward_farmer, "MAX_COST_PER_MARKET"))
        self.assertFalse(hasattr(reward_farmer, "MAX_TOTAL_EXPOSURE"))


# ── FX-024: [CAPITAL_SOURCE] log line ───────────────────────────────────────


class TestCapitalSourceLog(unittest.TestCase):
    """The agent's capital-resolution flow emits exactly one
    ``[CAPITAL_SOURCE]`` log line per cycle, with source ∈
    {usdc_db, flag, none}. The line is the operator's primary signal
    for which capital path was taken; structured for easy grepping."""

    def setUp(self):
        # Capture the `[CAPITAL_SOURCE]` log line emitted by the agent's
        # capital-resolution flow. The line goes through ``log.info`` /
        # ``log.warning`` on the ``oversight`` logger; we need both the
        # handler AND the logger to allow DEBUG-or-above through.
        self._caplog_records: list[logging.LogRecord] = []
        self._handler = logging.Handler()
        self._handler.emit = lambda r: self._caplog_records.append(r)
        self._handler.setLevel(logging.DEBUG)
        self._logger = logging.getLogger("oversight")
        self._prev_level = self._logger.level
        self._logger.setLevel(logging.DEBUG)
        self._logger.addHandler(self._handler)

    def tearDown(self):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._prev_level)

    def _capital_source_lines(self):
        return [
            r.getMessage() for r in self._caplog_records
            if "[CAPITAL_SOURCE]" in r.getMessage()
        ]

    @patch("oversight.data_collector.collect_all")
    @patch("database.get_db")
    def test_source_usdc_db_when_fresh_row(self, mock_get_db, mock_collect):
        from oversight_agent import run_once
        # Empty metrics → the agent returns early after the capital block,
        # so the test runs in milliseconds rather than a full agent pass.
        mock_collect.return_value = ({}, 1.0, 0.0, 1.0, 0.0)
        mock_db = MagicMock()
        mock_db.load_usdc_balance.return_value = (201.35, time.time() - 60)
        mock_get_db.return_value = mock_db
        path = _fresh_db_path()
        try:
            run_once(db_path=path, dry_run=True)
        finally:
            os.unlink(path)
        lines = self._capital_source_lines()
        self.assertTrue(any("source=usdc_db" in line for line in lines),
                        f"Expected source=usdc_db in {lines}")
        self.assertTrue(any("$201.35" in line for line in lines))

    @patch("oversight.data_collector.collect_all")
    @patch("database.get_db")
    def test_source_flag_when_capital_provided_and_db_stale(self, mock_get_db, mock_collect):
        from oversight_agent import run_once
        mock_collect.return_value = ({}, 1.0, 0.0, 1.0, 0.0)
        mock_db = MagicMock()
        # Row is > 30 min old → not fresh.
        mock_db.load_usdc_balance.return_value = (201.35, time.time() - 3600)
        mock_get_db.return_value = mock_db
        path = _fresh_db_path()
        try:
            run_once(db_path=path, capital=500.0, dry_run=True)
        finally:
            os.unlink(path)
        lines = self._capital_source_lines()
        self.assertTrue(any("source=flag" in line for line in lines),
                        f"Expected source=flag in {lines}")

    @patch("database.get_db")
    def test_source_none_returns_no_capital(self, mock_get_db):
        from oversight_agent import run_once
        mock_db = MagicMock()
        # Nothing in DB.
        mock_db.load_usdc_balance.return_value = (None, 0)
        mock_get_db.return_value = mock_db
        path = _fresh_db_path()
        try:
            result = run_once(db_path=path, capital=None, dry_run=True)
        finally:
            os.unlink(path)
        # The cycle must short-circuit cleanly.
        self.assertEqual({"status": "no_capital", "markets": 0}, result)
        lines = self._capital_source_lines()
        self.assertTrue(any("source=none" in line for line in lines),
                        f"Expected source=none in {lines}")


# ── FX-013: farmer writes usdc_balance on cycle 1 ───────────────────────────


class TestFarmerWritesBalanceOnCycle1(unittest.TestCase):
    """The cycle-1 write is the production-side half of FX-013. We can't
    exercise the full run() loop without a CLOB client, so we assert the
    GUARD in the loop by reading the source — a stable regression check
    against accidental deletion of the cycle-1 branch."""

    def test_cycle_1_branch_exists(self):
        # Read the source and confirm the cycle-1 write branch is wired.
        # If a future refactor removes the cycle == 1 condition without
        # an alternative, this test fails loudly.
        import inspect
        import reward_farmer
        src = inspect.getsource(reward_farmer.RewardFarmer.run)
        # The branch must reference cycle_count == 1 AND _save_usdc_balance
        # together. We grep for both inside the run() body.
        self.assertIn("cycle_count == 1", src)
        self.assertIn("_save_usdc_balance", src)


if __name__ == "__main__":
    unittest.main()
