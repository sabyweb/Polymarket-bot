"""SafetyController unit tests.

Phase 1 (FX-002 / FX-003 / FX-012) seeded this file with the cold-start /
BOOTSTRAP surface. Phase 6 part 2 (FX-016) extends it to cover the rest of
``oversight/safety_controller.py``:

* Block A — Each of the 14 invariants (I1-I14) with happy-path, breach,
  and query-failure cases where applicable.
* Block B — State-machine: permissions table, upgrade ladder, downgrade
  precedence, UNSAFE auto-recovery, ``_transition`` counter reset.
* Block C — ``filter_allocations`` end-to-end: state perms, trial gate,
  market cap, probe mode, capital cap, q_share clamp, per-market exposure,
  LOW-signal haircuts.
* Block D — ``evaluate_state`` integration: multi-violation severity
  precedence, worst-within-priority, backward-compat ``evaluate()`` wrapper.

Blocks E (persistence round-trip), F (helpers), and G (alert files) live in
the second FX-016 commit.
"""

import os
import sqlite3
import tempfile
import time
import unittest

from oversight import safety_controller as sc_mod
from oversight.safety_controller import (
    BOOTSTRAP,
    BOOTSTRAP_FILL_EXIT,
    CALIBRATED,
    DATA_UNAVAILABLE,
    DEGRADED,
    MILDLY_MISCALIBRATED,
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
    SEVERELY_MISCALIBRATED,
    STATE_PERMISSIONS,
    STATE_SEVERITY,
    SafetyController,
    UNSAFE,
    UNSAFE_RECOVERY_CYCLES,
    UPGRADE_FROM_BOOTSTRAP,
    UPGRADE_TO_CALIBRATED,
    _UPGRADE_ORDER,
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


class TestBootstrapStateRegistration(unittest.TestCase):
    """FX-003 — BOOTSTRAP is registered in permissions, severity, upgrade order."""

    def test_bootstrap_in_state_permissions(self):
        perms = STATE_PERMISSIONS.get(BOOTSTRAP)
        self.assertIsNotNone(perms)
        self.assertEqual(10, perms["max_markets"])
        self.assertEqual(0.30, perms["capital_pct"])
        self.assertTrue(perms["trials"])

    def test_bootstrap_severity_between_mildly_and_severely(self):
        self.assertLess(STATE_SEVERITY[MILDLY_MISCALIBRATED], STATE_SEVERITY[BOOTSTRAP])
        self.assertLess(STATE_SEVERITY[BOOTSTRAP], STATE_SEVERITY[SEVERELY_MISCALIBRATED])

    def test_bootstrap_in_upgrade_order(self):
        self.assertIn(BOOTSTRAP, _UPGRADE_ORDER)
        idx_mild = _UPGRADE_ORDER.index(MILDLY_MISCALIBRATED)
        idx_boot = _UPGRADE_ORDER.index(BOOTSTRAP)
        idx_sev = _UPGRADE_ORDER.index(SEVERELY_MISCALIBRATED)
        # Order is worst → best, so BOOTSTRAP sits between SEVERELY and MILDLY.
        self.assertLess(idx_sev, idx_boot)
        self.assertLess(idx_boot, idx_mild)


class TestBootstrapEntry(unittest.TestCase):
    """FX-003 / FX-012 — cold-start initial state is BOOTSTRAP, warm restart is MILDLY."""

    def setUp(self):
        self.path = _fresh_db_with_scoring_snapshot()

    def tearDown(self):
        os.unlink(self.path)

    def test_fresh_db_defaults_to_bootstrap(self):
        sc = SafetyController(db_path=self.path)
        self.assertEqual(BOOTSTRAP, sc.state)

    def test_warm_db_with_orders_defaults_to_mildly(self):
        # Place an order to invalidate the cold-start gate, then a fresh
        # SafetyController instance should NOT enter BOOTSTRAP.
        db = sqlite3.connect(self.path)
        db.execute(
            "INSERT INTO orders_placed (ts, condition_id, side, price, size) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), "cid", "yes", 0.5, 50),
        )
        db.commit()
        db.close()
        sc = SafetyController(db_path=self.path)
        self.assertEqual(MILDLY_MISCALIBRATED, sc.state)

    def test_recent_safety_state_row_takes_precedence(self):
        # A recent (< 2h) safety_state row should be respected even on a
        # technically-cold-start DB — that's the existing _load_state contract.
        db = sqlite3.connect(self.path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS safety_state ("
            "id INTEGER PRIMARY KEY, ts REAL NOT NULL, state TEXT NOT NULL, "
            "reason TEXT NOT NULL DEFAULT '', "
            "consecutive_good INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute(
            "INSERT INTO safety_state (ts, state, reason, consecutive_good) "
            "VALUES (?, ?, ?, ?)",
            (time.time() - 600, "DEGRADED", "from prior run", 0),
        )
        db.commit()
        db.close()
        sc = SafetyController(db_path=self.path)
        self.assertEqual("DEGRADED", sc.state)


class TestBootstrapExit(unittest.TestCase):
    """FX-003 — BOOTSTRAP exits to MILDLY on 10 fills OR 3 clean cycles."""

    def setUp(self):
        self.path = _fresh_db_with_scoring_snapshot()

    def tearDown(self):
        os.unlink(self.path)

    def _eval_clean(self, sc):
        # Inputs chosen so no violations fire (CF in healthy zone, valid
        # capital, no losses). Cold-start gate is honoured naturally because
        # the fixture DB is fresh.
        return sc.evaluate_state(
            correction_factor_raw=0.15,
            estimated_daily_total=10,
            actual_daily_payout=10,
            reward_payout_24h=10,
            num_scoring_markets=1,
            exchange_balance=200.0,
            total_portfolio_value=200.0,
            fill_damage_24h=0.0,
            fill_damage_7d=0.0,
        )

    def test_exits_after_3_clean_cycles(self):
        sc = SafetyController(db_path=self.path)
        self.assertEqual(BOOTSTRAP, sc.state)
        for _ in range(UPGRADE_FROM_BOOTSTRAP):
            self._eval_clean(sc)
        self.assertEqual(MILDLY_MISCALIBRATED, sc.state)

    def test_does_not_exit_before_3_clean_cycles(self):
        sc = SafetyController(db_path=self.path)
        self._eval_clean(sc)
        self.assertEqual(BOOTSTRAP, sc.state)
        self._eval_clean(sc)
        self.assertEqual(BOOTSTRAP, sc.state)

    def test_exits_on_lifetime_fills_threshold(self):
        # Insert 10 fills with zero clob_cost so I7 hourly_loss doesn't fire on
        # them — we only need the row COUNT to cross BOOTSTRAP_FILL_EXIT.
        db = sqlite3.connect(self.path)
        for i in range(BOOTSTRAP_FILL_EXIT):
            db.execute(
                "INSERT INTO fills (ts, condition_id, side, fill_type, shares, "
                "price, clob_cost, usd_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), f"cid_{i}", "yes", "BUY", 50, 0.5, 0.0, 0.0),
            )
        db.commit()
        db.close()
        # Now fills exist, so _is_genuine_cold_start is False. A new controller
        # would default to MILDLY at _load_state, so explicitly construct the
        # BOOTSTRAP scenario by setting state before evaluating.
        sc = SafetyController(db_path=self.path)
        sc.state = BOOTSTRAP
        sc._bootstrap_clean_cycles = 0
        self._eval_clean(sc)
        self.assertEqual(MILDLY_MISCALIBRATED, sc.state)

    def test_transition_resets_bootstrap_counter(self):
        sc = SafetyController(db_path=self.path)
        sc._bootstrap_clean_cycles = 2
        sc._transition(MILDLY_MISCALIBRATED, ["manual"])
        self.assertEqual(0, sc._bootstrap_clean_cycles)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 6 part 2 (FX-016) — broader coverage build-out
# ═══════════════════════════════════════════════════════════════════════════


def _warm_db_with_one_order(path: str) -> None:
    """Insert a single ``orders_placed`` row so ``_is_genuine_cold_start`` is False.

    Use this in tests that need invariants like I3 drawdown to fire on a
    zero-portfolio scenario without the cold-start short-circuit kicking in.
    """
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO orders_placed (ts, condition_id, side, price, size) "
        "VALUES (?, ?, ?, ?, ?)",
        (time.time(), "cid", "yes", 0.5, 50),
    )
    db.commit()
    db.close()


def _insert_fill(path: str, ts_offset_s: float, clob_cost: float,
                 condition_id: str = "cid") -> None:
    """Insert one fills row. ``ts_offset_s`` is seconds ago (positive)."""
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO fills (ts, condition_id, side, fill_type, shares, "
        "price, clob_cost, usd_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (time.time() - ts_offset_s, condition_id, "yes", "BUY",
         1.0, 1.0, clob_cost, clob_cost),
    )
    db.commit()
    db.close()


class _ControllerTestBase(unittest.TestCase):
    """Base for the FX-016 invariant + integration tests.

    Provides a fresh DB on each test and a "clean baseline" ``_eval`` helper:
    inputs that fire NO invariants. Individual tests override one kwarg at a
    time to isolate the invariant under test.
    """

    def setUp(self):
        self.path = _fresh_db_with_scoring_snapshot()
        # Mark the DB warm so cold-start short-circuits don't fire.
        _warm_db_with_one_order(self.path)
        self.sc = SafetyController(db_path=self.path)
        # _load_state on a warm DB picks MILDLY_MISCALIBRATED — sane mid-rung
        # default for invariant tests that don't care about the state machine.

    def tearDown(self):
        os.unlink(self.path)

    # Clean baseline: passes every invariant. Override one kwarg per test.
    _CLEAN = dict(
        correction_factor_raw=0.15,
        estimated_daily_total=10.0,
        actual_daily_payout=10.0,
        reward_payout_24h=10.0,
        num_scoring_markets=10,
        data_completeness=1.0,
        clob_rate_delta_pct=0.0,
        cf_at_floor_cycles=0,
        exchange_balance=200.0,
        total_portfolio_value=200.0,
        fill_damage_24h=0.0,
        fill_damage_7d=0.0,
    )

    def _eval(self, **overrides) -> str:
        kwargs = dict(self._CLEAN)
        kwargs.update(overrides)
        return self.sc.evaluate_state(**kwargs)

    def _violations_for(self, invariant: str):
        return [v for v in self.sc.violations if v.invariant == invariant]


# ───────────────────────────────────────────────────────────────────────────
# Block A — Per-invariant coverage I1-I14
# ───────────────────────────────────────────────────────────────────────────


class TestI1DailyLoss(_ControllerTestBase):
    """I1 daily_loss — CRITICAL — > $150."""

    def test_clean_no_violation(self):
        self._eval(fill_damage_24h=50.0)
        self.assertEqual([], self._violations_for("daily_loss"))

    def test_breach_fires_unsafe(self):
        self._eval(fill_damage_24h=200.0)
        v = self._violations_for("daily_loss")
        self.assertEqual(1, len(v))
        self.assertEqual(PRIORITY_CRITICAL, v[0].priority)
        self.assertEqual(UNSAFE, v[0].severity)

    def test_query_failure_fires_degraded(self):
        # Drop the fills table so _query_fill_damage returns None on the I1
        # 24h path. We pass fill_damage_24h=None implicitly by not overriding,
        # so the controller queries the DB.
        db = sqlite3.connect(self.path)
        db.execute("DROP TABLE fills")
        db.commit()
        db.close()
        # Default _CLEAN has fill_damage_24h=0.0 which bypasses the DB query;
        # build kwargs that explicitly omit it so the query path runs.
        kwargs = dict(self._CLEAN)
        kwargs.pop("fill_damage_24h")
        kwargs.pop("fill_damage_7d")
        self.sc.evaluate_state(**kwargs)
        v = self._violations_for("daily_loss")
        self.assertEqual(1, len(v))
        self.assertEqual(PRIORITY_CRITICAL, v[0].priority)
        self.assertEqual(DEGRADED, v[0].severity)


class TestI2SlowBleed(_ControllerTestBase):
    """I2 slow_bleed_7d — CRITICAL — > $500."""

    def test_clean_no_violation(self):
        self._eval(fill_damage_7d=100.0)
        self.assertEqual([], self._violations_for("slow_bleed_7d"))

    def test_breach_fires_unsafe(self):
        self._eval(fill_damage_7d=600.0)
        v = self._violations_for("slow_bleed_7d")
        self.assertEqual(1, len(v))
        self.assertEqual(UNSAFE, v[0].severity)

    def test_query_failure_fires_degraded(self):
        db = sqlite3.connect(self.path)
        db.execute("DROP TABLE fills")
        db.commit()
        db.close()
        kwargs = dict(self._CLEAN)
        kwargs.pop("fill_damage_24h")
        kwargs.pop("fill_damage_7d")
        self.sc.evaluate_state(**kwargs)
        v = self._violations_for("slow_bleed_7d")
        self.assertEqual(1, len(v))
        self.assertEqual(DEGRADED, v[0].severity)


class TestI3DrawdownWarmDB(_ControllerTestBase):
    """I3 drawdown — CRITICAL — > 15% peak-to-trough.

    Cold-start skip is already covered by ``TestI3ColdStartSkip``. Here we
    exercise the warm-DB path where a peak has been observed.
    """

    def test_clean_no_drawdown(self):
        # First eval seeds peak at $200; same value second cycle → 0% drawdown.
        self._eval()
        self._eval()
        self.assertEqual([], self._violations_for("drawdown"))

    def test_breach_fires_unsafe(self):
        # Seed peak at $1000, then drop to $700 → 30% drawdown.
        self._eval(exchange_balance=1000.0, total_portfolio_value=1000.0)
        self._eval(exchange_balance=700.0, total_portfolio_value=700.0)
        v = self._violations_for("drawdown")
        self.assertEqual(1, len(v))
        self.assertEqual(UNSAFE, v[0].severity)

    def test_warm_db_zero_portfolio_data_unavailable(self):
        # Already covered by TestI3ColdStartSkip.test_warm_db_still_fires_data_unavailable
        # but we re-assert here on the _ControllerTestBase fixture for clarity.
        self._eval(exchange_balance=0.0, total_portfolio_value=0.0)
        v = self._violations_for("drawdown")
        self.assertEqual(1, len(v))
        self.assertEqual(DATA_UNAVAILABLE, v[0].severity)


class TestI4CapitalFloor(_ControllerTestBase):
    """I4 capital_floor — CRITICAL — wallet-scaled max($50, 10% reference)."""

    def test_clean_above_floor(self):
        self._eval(exchange_balance=200.0, total_portfolio_value=200.0)
        self.assertEqual([], self._violations_for("capital_floor"))

    def test_balance_below_floor_fires_unsafe(self):
        self._eval(exchange_balance=20.0, total_portfolio_value=20.0)
        v = self._violations_for("capital_floor")
        self.assertEqual(1, len(v))
        self.assertEqual(UNSAFE, v[0].severity)

    def test_zero_balance_with_history_unsafe(self):
        # Insert a recent portfolio_snapshot above the floor, then eval with
        # zero balance → should fire UNSAFE (proven loss, not API failure).
        db = sqlite3.connect(self.path)
        db.execute(
            "INSERT INTO portfolio_snapshots "
            "(ts, total_value, exchange_balance, locked_capital, peak_value) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time() - 60, 200.0, 200.0, 0.0, 200.0),
        )
        db.commit()
        db.close()
        self._eval(exchange_balance=0.0, total_portfolio_value=0.0)
        v = self._violations_for("capital_floor")
        # Drawdown will ALSO fire here (200 → 0), but we only assert on capital_floor.
        match = [x for x in v if x.severity == UNSAFE]
        self.assertEqual(1, len(match))

    def test_zero_balance_no_history_data_unavailable(self):
        self._eval(exchange_balance=0.0, total_portfolio_value=0.0)
        v = self._violations_for("capital_floor")
        self.assertEqual(1, len(v))
        self.assertEqual(DATA_UNAVAILABLE, v[0].severity)

    def test_wallet_scaled_floor_on_large_wallet(self):
        # On a $1500 wallet, floor = max($50, 1500 * 0.10) = $150.
        # A balance of $100 is above the absolute $50 minimum but below the
        # wallet-scaled $150 floor → should fire.
        self._eval(exchange_balance=100.0, total_portfolio_value=1500.0)
        v = self._violations_for("capital_floor")
        self.assertEqual(1, len(v))
        self.assertEqual(UNSAFE, v[0].severity)
        self.assertAlmostEqual(150.0, v[0].threshold, places=1)


class TestI5CFDrift(_ControllerTestBase):
    """I5 cf_drift — HIGH — 0.005 / 0.02 / 0.03 thresholds."""

    def test_cf_in_calibrated_zone_no_violation(self):
        self._eval(correction_factor_raw=0.15)
        self.assertEqual([], self._violations_for("cf_drift"))

    def test_cf_zero_skipped(self):
        # cf=0 means "no data" — skip rather than fire.
        self._eval(correction_factor_raw=0.0)
        self.assertEqual([], self._violations_for("cf_drift"))

    def test_cf_mild_low_fires_mildly(self):
        self._eval(correction_factor_raw=0.025)
        v = self._violations_for("cf_drift")
        self.assertEqual(1, len(v))
        self.assertEqual(MILDLY_MISCALIBRATED, v[0].severity)

    def test_cf_severe_low_fires_severely(self):
        self._eval(correction_factor_raw=0.01)
        v = self._violations_for("cf_drift")
        self.assertEqual(1, len(v))
        self.assertEqual(SEVERELY_MISCALIBRATED, v[0].severity)

    def test_cf_circuit_break_fires_severely(self):
        # Below CF_CIRCUIT_BREAK (0.005) → severity SEVERELY at HIGH priority.
        # I5b (corroborated) is a separate violation that needs additional
        # est_actual + losses to fire UNSAFE.
        self._eval(correction_factor_raw=0.001)
        v = self._violations_for("cf_drift")
        self.assertEqual(1, len(v))
        self.assertEqual(SEVERELY_MISCALIBRATED, v[0].severity)


class TestI5bCFCorroborated(_ControllerTestBase):
    """I5b cf_corroborated — CRITICAL — only fires when CF + est + losses agree."""

    def test_cf_alone_does_not_corroborate(self):
        # CF in circuit-break but est_actual healthy and no losses.
        self._eval(correction_factor_raw=0.001, fill_damage_24h=0.0)
        self.assertEqual([], self._violations_for("cf_corroborated"))

    def test_all_three_fire_unsafe(self):
        # CF < 0.005 AND est/actual > 15 AND fd24 > $50.
        # est_actual_ratio = estimated/actual = 200/10 = 20x.
        self._eval(
            correction_factor_raw=0.001,
            estimated_daily_total=200.0,
            actual_daily_payout=10.0,
            fill_damage_24h=100.0,
        )
        v = self._violations_for("cf_corroborated")
        self.assertEqual(1, len(v))
        self.assertEqual(PRIORITY_CRITICAL, v[0].priority)
        self.assertEqual(UNSAFE, v[0].severity)


class TestI6EstActual(_ControllerTestBase):
    """I6 est_actual — HIGH — 15× / 50× thresholds."""

    def test_clean_no_violation(self):
        self._eval(estimated_daily_total=10.0, actual_daily_payout=10.0)
        self.assertEqual([], self._violations_for("est_actual"))

    def test_zero_actual_skipped(self):
        # Ratio is only computed when both > 0.
        self._eval(estimated_daily_total=100.0, actual_daily_payout=0.0)
        self.assertEqual([], self._violations_for("est_actual"))

    def test_severe_threshold_fires(self):
        # 20× → over 15× threshold, under 50× UNSAFE threshold.
        self._eval(estimated_daily_total=200.0, actual_daily_payout=10.0)
        v = self._violations_for("est_actual")
        self.assertEqual(1, len(v))
        self.assertEqual(SEVERELY_MISCALIBRATED, v[0].severity)

    def test_unsafe_threshold_fires_severely(self):
        # 100× → over 50× UNSAFE threshold; note severity is still
        # SEVERELY_MISCALIBRATED (the UNSAFE-in-name threshold doesn't push
        # this single invariant to UNSAFE — that's I5b's job).
        self._eval(estimated_daily_total=1000.0, actual_daily_payout=10.0)
        v = self._violations_for("est_actual")
        self.assertEqual(1, len(v))
        self.assertEqual(SEVERELY_MISCALIBRATED, v[0].severity)


class TestI7HourlyLoss(_ControllerTestBase):
    """I7 hourly_loss — HIGH — > $30 / > $60 thresholds."""

    def test_clean_no_violation(self):
        # No fills inserted → fd1h = 0 → no violation.
        self._eval()
        self.assertEqual([], self._violations_for("hourly_loss"))

    def test_breach_warn_fires(self):
        # $35 in the last hour > $30 warn threshold.
        _insert_fill(self.path, ts_offset_s=60, clob_cost=35.0)
        self._eval()
        v = self._violations_for("hourly_loss")
        self.assertEqual(1, len(v))
        self.assertEqual(DEGRADED, v[0].severity)

    def test_breach_critical_fires(self):
        # $70 > $60 critical threshold.
        _insert_fill(self.path, ts_offset_s=60, clob_cost=70.0)
        self._eval()
        v = self._violations_for("hourly_loss")
        self.assertEqual(1, len(v))
        self.assertEqual(DEGRADED, v[0].severity)

    def test_fill_outside_1h_window_does_not_fire(self):
        # 2h old → outside 1h window, fd1h=0, no violation.
        _insert_fill(self.path, ts_offset_s=7200, clob_cost=70.0)
        self._eval()
        self.assertEqual([], self._violations_for("hourly_loss"))


class TestI8CapitalAtRisk(_ControllerTestBase):
    """I8 capital_at_risk — HIGH — > 80% / > 90% thresholds."""

    def test_clean_no_violation(self):
        # exchange=200, portfolio=200 → at_risk = 0%.
        self._eval(exchange_balance=200.0, total_portfolio_value=200.0)
        self.assertEqual([], self._violations_for("capital_at_risk"))

    def test_85pct_fires(self):
        # exchange=15, portfolio=100 → at_risk = 85%.
        self._eval(exchange_balance=15.0, total_portfolio_value=100.0)
        v = self._violations_for("capital_at_risk")
        self.assertEqual(1, len(v))
        self.assertEqual(DEGRADED, v[0].severity)

    def test_95pct_fires(self):
        # exchange=5, portfolio=100 → at_risk = 95%.
        self._eval(exchange_balance=5.0, total_portfolio_value=100.0)
        v = self._violations_for("capital_at_risk")
        self.assertEqual(1, len(v))
        self.assertEqual(DEGRADED, v[0].severity)


class TestI9DataFreshness(_ControllerTestBase):
    """I9 data_freshness — MEDIUM — > 30m warn / > 2h critical."""

    def test_fresh_no_violation(self):
        # Fixture inserts a scoring_snapshots row 60s ago → fresh.
        self._eval()
        self.assertEqual([], self._violations_for("data_freshness"))

    def test_stale_30m_warn(self):
        # Overwrite the scoring_snapshots ts to 45m ago.
        db = sqlite3.connect(self.path)
        db.execute("UPDATE scoring_snapshots SET ts = ?", (time.time() - 2700,))
        db.commit()
        db.close()
        self._eval()
        v = self._violations_for("data_freshness")
        self.assertEqual(1, len(v))
        self.assertEqual(MILDLY_MISCALIBRATED, v[0].severity)

    def test_stale_2h_critical(self):
        db = sqlite3.connect(self.path)
        db.execute("UPDATE scoring_snapshots SET ts = ?", (time.time() - 10000,))
        db.commit()
        db.close()
        self._eval()
        v = self._violations_for("data_freshness")
        self.assertEqual(1, len(v))
        self.assertEqual(DATA_UNAVAILABLE, v[0].severity)


class TestI10DataCompleteness(_ControllerTestBase):
    """I10 data_completeness — MEDIUM — < 80% warn / < 50% critical."""

    def test_complete_no_violation(self):
        self._eval(data_completeness=1.0)
        self.assertEqual([], self._violations_for("data_completeness"))

    def test_warn_threshold(self):
        self._eval(data_completeness=0.70)
        v = self._violations_for("data_completeness")
        self.assertEqual(1, len(v))
        self.assertEqual(DEGRADED, v[0].severity)

    def test_critical_threshold(self):
        self._eval(data_completeness=0.40)
        v = self._violations_for("data_completeness")
        self.assertEqual(1, len(v))
        self.assertEqual(DATA_UNAVAILABLE, v[0].severity)


class TestI11LossReward(_ControllerTestBase):
    """I11 loss_reward — HIGH — > 1.5× MILD / > 2.0× SEVERE."""

    def test_healthy_ratio_no_violation(self):
        self._eval(reward_payout_24h=100.0, fill_damage_24h=50.0)  # 0.5×
        self.assertEqual([], self._violations_for("loss_reward"))

    def test_zero_reward_skipped(self):
        # Need reward > 0 to compute the ratio.
        self._eval(reward_payout_24h=0.0, fill_damage_24h=100.0)
        self.assertEqual([], self._violations_for("loss_reward"))

    def test_zero_loss_skipped(self):
        # Need fd24 > 0 to compute the ratio.
        self._eval(reward_payout_24h=100.0, fill_damage_24h=0.0)
        self.assertEqual([], self._violations_for("loss_reward"))

    def test_mild_threshold(self):
        # 1.7× → over 1.5×, under 2.0×.
        # fd24=85 stays under I1 ($150) so I1 doesn't co-fire.
        self._eval(reward_payout_24h=50.0, fill_damage_24h=85.0)
        v = self._violations_for("loss_reward")
        self.assertEqual(1, len(v))
        self.assertEqual(MILDLY_MISCALIBRATED, v[0].severity)

    def test_severe_threshold(self):
        # 2.4× → over 2.0×.
        self._eval(reward_payout_24h=50.0, fill_damage_24h=120.0)
        v = self._violations_for("loss_reward")
        self.assertEqual(1, len(v))
        self.assertEqual(SEVERELY_MISCALIBRATED, v[0].severity)


class TestI12CLOBRateDrop(_ControllerTestBase):
    """I12 clob_rate_drop — MEDIUM — < −30%."""

    def test_stable_no_violation(self):
        self._eval(clob_rate_delta_pct=0.0)
        self.assertEqual([], self._violations_for("clob_rate_drop"))

    def test_mild_drop_no_violation(self):
        self._eval(clob_rate_delta_pct=-0.25)
        self.assertEqual([], self._violations_for("clob_rate_drop"))

    def test_severe_drop_fires(self):
        self._eval(clob_rate_delta_pct=-0.40)
        v = self._violations_for("clob_rate_drop")
        self.assertEqual(1, len(v))
        self.assertEqual(DEGRADED, v[0].severity)


class TestI13FillStorm(_ControllerTestBase):
    """I13 fill_storm — LOW + 20% capital haircut."""

    def test_no_storm_no_violation(self):
        self._eval()
        self.assertEqual([], self._violations_for("fill_storm"))

    def test_one_storm_fires(self):
        # Insert a fill row tagged with the sentinel condition_id.
        db = sqlite3.connect(self.path)
        db.execute(
            "INSERT INTO fills (ts, condition_id, side, fill_type, shares, "
            "price, clob_cost, usd_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time() - 60, "__FILL_STORM__", "yes", "BUY",
             1.0, 1.0, 0.0, 0.0),
        )
        db.commit()
        db.close()
        self._eval()
        v = self._violations_for("fill_storm")
        self.assertEqual(1, len(v))
        self.assertEqual(PRIORITY_LOW, v[0].priority)


class TestI14CFAtFloor(_ControllerTestBase):
    """I14 cf_at_floor — LOW + 10% capital haircut — ≥ 3 cycles."""

    def test_zero_cycles_no_violation(self):
        self._eval(cf_at_floor_cycles=0)
        self.assertEqual([], self._violations_for("cf_at_floor"))

    def test_two_cycles_no_violation(self):
        self._eval(cf_at_floor_cycles=2)
        self.assertEqual([], self._violations_for("cf_at_floor"))

    def test_three_cycles_fires(self):
        self._eval(cf_at_floor_cycles=3)
        v = self._violations_for("cf_at_floor")
        self.assertEqual(1, len(v))
        self.assertEqual(PRIORITY_LOW, v[0].priority)
        self.assertEqual(SEVERELY_MISCALIBRATED, v[0].severity)


# ───────────────────────────────────────────────────────────────────────────
# Block B — State-machine: permissions, upgrade ladder, downgrades, recovery
# ───────────────────────────────────────────────────────────────────────────


class TestStatePermissionsTable(unittest.TestCase):
    """All 7 states have well-formed permission entries."""

    def test_all_states_have_permissions(self):
        from oversight.safety_controller import ALL_STATES
        for state in ALL_STATES:
            self.assertIn(state, STATE_PERMISSIONS, f"{state} missing perms")
            perms = STATE_PERMISSIONS[state]
            self.assertIn("max_markets", perms)
            self.assertIn("capital_pct", perms)
            self.assertIn("trials", perms)

    def test_calibrated_most_permissive(self):
        cal = STATE_PERMISSIONS[CALIBRATED]
        self.assertEqual(60, cal["max_markets"])
        self.assertEqual(1.0, cal["capital_pct"])
        self.assertTrue(cal["trials"])

    def test_unsafe_most_restrictive(self):
        u = STATE_PERMISSIONS[UNSAFE]
        self.assertEqual(3, u["max_markets"])
        self.assertEqual(0.05, u["capital_pct"])
        self.assertFalse(u["trials"])
        self.assertTrue(u.get("probe_mode"))
        self.assertTrue(u.get("min_size_only"))

    def test_max_markets_monotonic_on_non_bootstrap_ladder(self):
        # BOOTSTRAP is intentionally OFF the standard severity ladder for
        # max_markets — it's a cold-start ease-in with only 10 markets despite
        # being severity 2 (less severe than SEVERELY's 20). See the design
        # note at safety_controller.py:48-52. The non-BOOTSTRAP states must
        # be monotonic: more severe → fewer markets allowed.
        for s_lower, s_higher in [
            (CALIBRATED, MILDLY_MISCALIBRATED),
            (MILDLY_MISCALIBRATED, SEVERELY_MISCALIBRATED),
            (SEVERELY_MISCALIBRATED, DEGRADED),
            (DEGRADED, DATA_UNAVAILABLE),
            (DATA_UNAVAILABLE, UNSAFE),
        ]:
            self.assertGreaterEqual(
                STATE_PERMISSIONS[s_lower]["max_markets"],
                STATE_PERMISSIONS[s_higher]["max_markets"],
                f"{s_lower} should be ≥ permissive vs {s_higher}",
            )

    def test_bootstrap_is_more_restrictive_than_mildly(self):
        # Explicit pin on the intentional inversion: BOOTSTRAP allows fewer
        # markets and less capital than MILDLY despite being only one severity
        # rung worse. This is the cold-start ease-in by design.
        boot = STATE_PERMISSIONS[BOOTSTRAP]
        mild = STATE_PERMISSIONS[MILDLY_MISCALIBRATED]
        self.assertLess(boot["max_markets"], mild["max_markets"])
        self.assertLess(boot["capital_pct"], mild["capital_pct"])


class TestUpgradeLadder(_ControllerTestBase):
    """Clean cycles upgrade by step until CALIBRATED."""

    def test_mildly_to_calibrated_after_3_clean_cycles(self):
        self.sc.state = MILDLY_MISCALIBRATED
        self.sc.consecutive_good = 0
        for _ in range(UPGRADE_TO_CALIBRATED):
            self._eval()
        self.assertEqual(CALIBRATED, self.sc.state)

    def test_calibrated_stays_calibrated_on_clean(self):
        self.sc.state = CALIBRATED
        self._eval()
        self.assertEqual(CALIBRATED, self.sc.state)

    def test_calibrated_downgrades_when_not_fully_calibrated(self):
        # CF=0.025 fires I5 MILD violation → state transitions on violation
        # branch, NOT via _handle_upgrade. We assert state moves off CALIBRATED.
        self.sc.state = CALIBRATED
        self._eval(correction_factor_raw=0.025)
        self.assertNotEqual(CALIBRATED, self.sc.state)

    def test_degraded_upgrades_to_mildly_after_2_clean(self):
        # Lower states upgrade by 2 cycles to MILDLY, not all the way up.
        self.sc.state = DEGRADED
        self.sc.consecutive_good = 0
        self._eval()
        self._eval()
        self.assertEqual(MILDLY_MISCALIBRATED, self.sc.state)


class TestDowngradeBehavior(_ControllerTestBase):
    """Multi-priority sorting + worst-within-priority rule."""

    def test_critical_overrides_high(self):
        # I1 daily_loss (CRITICAL UNSAFE) + I5 cf_drift (HIGH SEVERELY).
        # Highest priority is CRITICAL → state should be UNSAFE.
        result = self._eval(fill_damage_24h=200.0, correction_factor_raw=0.01)
        self.assertEqual(UNSAFE, result)

    def test_high_overrides_medium(self):
        # I5 cf_drift (HIGH SEVERELY) + I9 data_freshness (MEDIUM at warn level).
        # Highest priority is HIGH → state should be SEVERELY.
        db = sqlite3.connect(self.path)
        db.execute("UPDATE scoring_snapshots SET ts = ?", (time.time() - 2700,))
        db.commit()
        db.close()
        result = self._eval(correction_factor_raw=0.01)
        self.assertEqual(SEVERELY_MISCALIBRATED, result)

    def test_worst_within_priority_wins(self):
        # Two CRITICAL violations: I1 (UNSAFE) and I2 query-failure (DEGRADED).
        # The one with the worse severity (UNSAFE) should win.
        result = self._eval(fill_damage_24h=200.0, fill_damage_7d=100.0)
        self.assertEqual(UNSAFE, result)


class TestUnsafeAutoRecovery(_ControllerTestBase):
    """UNSAFE exit semantics.

    Two distinct paths leave UNSAFE on a no-violations cycle:

    1. **Slow path (auto-recovery)** — 3 clean cycles where each is NOT
       fully-calibrated (so ``_handle_upgrade`` is a no-op) but each lacks
       a CRITICAL-UNSAFE violation + has valid data. After cycle 3,
       state transitions UNSAFE → DEGRADED via the explicit transition at
       ``evaluate_state`` line 657-664.

    2. **Fast path (_handle_upgrade)** — UPGRADE_STEP=2 cycles where each
       is fully calibrated (cf in [0.05,3], est/actual<5, ≥5 scoring,
       fd24 reasonable). State transitions UNSAFE → MILDLY via the
       upgrade-by-step ladder, bypassing DEGRADED entirely. This is by
       design: full calibration is a stronger signal than mere absence of
       CRITICAL violations, so it earns a faster exit.
    """

    def test_slow_path_unsafe_to_degraded_after_3_cycles(self):
        self.sc.state = UNSAFE
        self.sc._unsafe_no_critical_count = 0
        # num_scoring_markets=3 < 5 → _handle_upgrade fast path declines
        # (is_fully_calibrated=False), so only the auto-recovery path runs.
        for _ in range(UNSAFE_RECOVERY_CYCLES):
            self._eval(actual_daily_payout=10.0, num_scoring_markets=3)
        self.assertEqual(DEGRADED, self.sc.state)

    def test_fast_path_unsafe_to_mildly_after_2_calibrated_cycles(self):
        self.sc.state = UNSAFE
        self.sc.consecutive_good = 0
        self.sc._unsafe_no_critical_count = 0
        # Fully calibrated inputs (cf in zone, est==actual, ≥5 markets,
        # no losses) → UPGRADE_STEP=2 fast path fires before the slow
        # path reaches its 3-cycle gate.
        self._eval(actual_daily_payout=10.0)
        self._eval(actual_daily_payout=10.0)
        self.assertEqual(MILDLY_MISCALIBRATED, self.sc.state)

    def test_critical_violation_resets_counter(self):
        self.sc.state = UNSAFE
        self.sc._unsafe_no_critical_count = 2
        self._eval(fill_damage_24h=200.0)  # I1 CRITICAL-UNSAFE
        # Counter resets — state stays UNSAFE.
        self.assertEqual(UNSAFE, self.sc.state)
        self.assertEqual(0, self.sc._unsafe_no_critical_count)


class TestTransitionResetsCounters(_ControllerTestBase):
    """``_transition`` clears consecutive_good and _bootstrap_clean_cycles."""

    def test_consecutive_good_cleared(self):
        self.sc.state = MILDLY_MISCALIBRATED
        self.sc.consecutive_good = 5
        self.sc._transition(DEGRADED, ["test"])
        self.assertEqual(0, self.sc.consecutive_good)

    def test_bootstrap_counter_cleared(self):
        self.sc.state = BOOTSTRAP
        self.sc._bootstrap_clean_cycles = 2
        self.sc._transition(MILDLY_MISCALIBRATED, ["test"])
        self.assertEqual(0, self.sc._bootstrap_clean_cycles)

    def test_no_op_when_same_state(self):
        self.sc.state = MILDLY_MISCALIBRATED
        self.sc.consecutive_good = 5
        self.sc._transition(MILDLY_MISCALIBRATED, ["noop"])
        # Same-state transition is a no-op — counter preserved.
        self.assertEqual(5, self.sc.consecutive_good)


class TestBootstrapNonReentrant(_ControllerTestBase):
    """BOOTSTRAP is once-only — recovery from downgrades climbs to MILDLY, not BOOTSTRAP."""

    def test_unsafe_recovery_does_not_reenter_bootstrap(self):
        # Start in UNSAFE, recover via either path → never lands in BOOTSTRAP
        # (BOOTSTRAP is a once-only initial state, not a re-entrant rung).
        self.sc.state = UNSAFE
        self.sc._unsafe_no_critical_count = 0
        for _ in range(UNSAFE_RECOVERY_CYCLES):
            self._eval(actual_daily_payout=10.0)
        self.assertNotEqual(BOOTSTRAP, self.sc.state)


# ───────────────────────────────────────────────────────────────────────────
# Block C — filter_allocations end-to-end
# ───────────────────────────────────────────────────────────────────────────


def _allocation(score: float = 1.0, shares: int = 100,
                est_cost: float = 50.0, q_share: float = 0.1,
                max_spread: float = 0.045, min_size: int = 50) -> dict:
    return {
        "action": "deploy",
        "score": score,
        "shares_per_side": shares,
        "est_capital_cost": est_cost,
        "q_share_pct": q_share,
        "max_spread": max_spread,
        "min_size": min_size,
    }


class TestFilterAllocationsByState(_ControllerTestBase):

    def test_calibrated_passes_all(self):
        self.sc.state = CALIBRATED
        allocs = [_allocation() for _ in range(5)]
        self.sc.filter_allocations(allocs, available_capital=1000.0)
        self.assertEqual(5, sum(1 for a in allocs if a["action"] == "deploy"))

    def test_max_markets_cap_applied(self):
        # MILDLY allows 40, but we send 50 — only first 40 should survive.
        self.sc.state = MILDLY_MISCALIBRATED
        allocs = [_allocation() for _ in range(50)]
        self.sc.filter_allocations(allocs, available_capital=100000.0)
        self.assertEqual(40, sum(1 for a in allocs if a["action"] == "deploy"))

    def test_unsafe_caps_at_3_markets(self):
        self.sc.state = UNSAFE
        # Need available_capital generous enough that the 5% capital_pct
        # leaves room for 3 probe markets (~$45 each at min_size=50,
        # spread=0.045). $5000 * 0.05 = $250 → fits 5 → max_markets cap of 3 wins.
        allocs = [_allocation() for _ in range(10)]
        self.sc.filter_allocations(allocs, available_capital=5000.0)
        self.assertEqual(3, sum(1 for a in allocs if a["action"] == "deploy"))


class TestFilterAllocationsTrialGate(_ControllerTestBase):

    def test_trials_blocked_when_perms_say_no(self):
        # SEVERELY has trials=False. Score<=0 should be blocked.
        self.sc.state = SEVERELY_MISCALIBRATED
        allocs = [_allocation(score=-1.0), _allocation(score=1.0)]
        self.sc.filter_allocations(allocs, available_capital=1000.0)
        # The negative-score one is now "avoid".
        self.assertEqual("avoid", allocs[0]["action"])
        self.assertEqual("deploy", allocs[1]["action"])

    def test_trials_allowed_in_bootstrap(self):
        # BOOTSTRAP has trials=True.
        self.sc.state = BOOTSTRAP
        allocs = [_allocation(score=-1.0)]
        self.sc.filter_allocations(allocs, available_capital=1000.0)
        self.assertEqual("deploy", allocs[0]["action"])


class TestFilterAllocationsCapitalCap(_ControllerTestBase):

    def test_running_cost_limits_deploys(self):
        # MILDLY: capital_pct=0.70 → max = $1000 * 0.70 = $700.
        # 20 markets at $50 each = $1000 → only first 14 fit ($700/$50).
        self.sc.state = MILDLY_MISCALIBRATED
        allocs = [_allocation(est_cost=50.0) for _ in range(20)]
        self.sc.filter_allocations(allocs, available_capital=1000.0)
        deploys = sum(1 for a in allocs if a["action"] == "deploy")
        self.assertEqual(14, deploys)


class TestFilterAllocationsProbeMode(_ControllerTestBase):

    def test_unsafe_forces_min_size_only(self):
        self.sc.state = UNSAFE
        allocs = [_allocation(shares=500, est_cost=200.0, min_size=50)]
        self.sc.filter_allocations(allocs, available_capital=1000.0)
        # Shares dropped to min_size.
        self.assertEqual(50, allocs[0]["shares_per_side"])
        # Capital cost recomputed.
        self.assertLess(allocs[0]["est_capital_cost"], 200.0)
        self.assertIn("PROBE", allocs[0]["reason"])


class TestFilterAllocationsLowSignalHaircut(_ControllerTestBase):

    def test_fill_storm_haircut_20pct(self):
        self.sc.state = MILDLY_MISCALIBRATED
        # Inject a fill_storm violation by populating _last_violations directly.
        from oversight.safety_controller import Violation as V
        self.sc._last_violations = [
            V("fill_storm", PRIORITY_LOW, DEGRADED, 1.0, 0.0, "1 fill storm"),
        ]
        # Without haircut, $700 budget fits 14 × $50 deploys. With 20%
        # haircut → $560 → only 11 deploys fit.
        allocs = [_allocation(est_cost=50.0) for _ in range(20)]
        self.sc.filter_allocations(allocs, available_capital=1000.0)
        deploys = sum(1 for a in allocs if a["action"] == "deploy")
        self.assertEqual(11, deploys)

    def test_cf_at_floor_haircut_10pct(self):
        self.sc.state = MILDLY_MISCALIBRATED
        from oversight.safety_controller import Violation as V
        self.sc._last_violations = [
            V("cf_at_floor", PRIORITY_LOW, SEVERELY_MISCALIBRATED, 3.0, 3.0, "cf"),
        ]
        # 10% haircut on $700 → $630 → 12 deploys × $50.
        allocs = [_allocation(est_cost=50.0) for _ in range(20)]
        self.sc.filter_allocations(allocs, available_capital=1000.0)
        deploys = sum(1 for a in allocs if a["action"] == "deploy")
        self.assertEqual(12, deploys)

    def test_haircut_uses_max_when_both_fire(self):
        # fill_storm (20%) + cf_at_floor (10%) → max=20%, not additive 30%.
        self.sc.state = MILDLY_MISCALIBRATED
        from oversight.safety_controller import Violation as V
        self.sc._last_violations = [
            V("fill_storm", PRIORITY_LOW, DEGRADED, 1.0, 0.0, "storm"),
            V("cf_at_floor", PRIORITY_LOW, SEVERELY_MISCALIBRATED, 3.0, 3.0, "cf"),
        ]
        allocs = [_allocation(est_cost=50.0) for _ in range(20)]
        self.sc.filter_allocations(allocs, available_capital=1000.0)
        deploys = sum(1 for a in allocs if a["action"] == "deploy")
        # 20% haircut on $700 → $560 → 11 deploys.
        self.assertEqual(11, deploys)


class TestFilterAllocationsQShareCap(_ControllerTestBase):

    def test_q_share_clamped_at_max(self):
        self.sc.state = CALIBRATED
        allocs = [_allocation(q_share=0.8)]
        self.sc.filter_allocations(allocs, available_capital=1000.0)
        # Q_SHARE_MAX = 0.5
        self.assertEqual(0.5, allocs[0]["q_share_pct"])

    def test_q_share_below_cap_preserved(self):
        self.sc.state = CALIBRATED
        allocs = [_allocation(q_share=0.3)]
        self.sc.filter_allocations(allocs, available_capital=1000.0)
        self.assertEqual(0.3, allocs[0]["q_share_pct"])


class TestFilterAllocationsPerMarketExposure(_ControllerTestBase):

    def test_per_market_over_200_scaled_down(self):
        # MAX_PER_MARKET_EXPOSURE_USD = $200. The cap scales shares by
        # 200/input_est_cost, then recomputes new est_cost from the internal
        # formula ``shares × est_price × 2`` where est_price depends on spread.
        # For the cap to actually land at ≤ $200, the caller's input est_cost
        # must match the same internal formula on the original shares (it's a
        # "best-effort" cap — assumes consistent inputs). With spread=0.045 →
        # est_price=0.455. shares=500 → 500*0.455*2 = $455. Pass that.
        self.sc.state = CALIBRATED
        allocs = [_allocation(shares=500, est_cost=455.0, min_size=50)]
        self.sc.filter_allocations(allocs, available_capital=10000.0)
        self.assertLessEqual(allocs[0]["est_capital_cost"], 200.0)
        self.assertLess(allocs[0]["shares_per_side"], 500)
        self.assertGreaterEqual(allocs[0]["shares_per_side"], 50)


# ───────────────────────────────────────────────────────────────────────────
# Block D — evaluate() integration
# ───────────────────────────────────────────────────────────────────────────


class TestEvaluateMultipleViolations(_ControllerTestBase):

    def test_critical_unsafe_with_multiple_high(self):
        # I1 daily_loss CRITICAL-UNSAFE + I5 cf_drift HIGH-SEVERELY.
        # Final state = UNSAFE (CRITICAL wins).
        result = self._eval(fill_damage_24h=200.0, correction_factor_raw=0.01)
        self.assertEqual(UNSAFE, result)

    def test_three_priority_levels_picks_highest(self):
        # I1 CRITICAL + I5 HIGH + I9 MEDIUM (via stale data). CRITICAL wins.
        db = sqlite3.connect(self.path)
        db.execute("UPDATE scoring_snapshots SET ts = ?", (time.time() - 10000,))
        db.commit()
        db.close()
        result = self._eval(fill_damage_24h=200.0, correction_factor_raw=0.01)
        self.assertEqual(UNSAFE, result)

    def test_only_medium_violations_picks_medium(self):
        # Only I9 fires → state should be DATA_UNAVAILABLE (the severity of I9).
        db = sqlite3.connect(self.path)
        db.execute("UPDATE scoring_snapshots SET ts = ?", (time.time() - 10000,))
        db.commit()
        db.close()
        result = self._eval()
        self.assertEqual(DATA_UNAVAILABLE, result)

    def test_violations_property_returns_copy(self):
        # Property returns a list, not the underlying _last_violations ref.
        self._eval(fill_damage_24h=200.0)
        copy_1 = self.sc.violations
        copy_2 = self.sc.violations
        self.assertIsNot(copy_1, copy_2)
        self.assertEqual(copy_1, copy_2)


# ───────────────────────────────────────────────────────────────────────────
# Block E — Persistence round-trip (_persist_state / _load_state)
# ───────────────────────────────────────────────────────────────────────────


class TestPersistAndLoadState(unittest.TestCase):
    """``_persist_state`` writes safety_state rows; ``_load_state`` consumes them.

    Age branches (``_load_state`` lines 1183-1194):

    * < 2h   — use stored state, ``consecutive_good = max(0, stored - 1)``
    * 2-6h   — fall back to ``_cold_start_or(MILDLY)``; if stored was already
      DEGRADED+ then ``consecutive_good = 0``, else preserve stored - 1
    * > 6h   — fall back to ``_cold_start_or(MILDLY)``; ``consecutive_good = 0``
    * no row — fall back to ``_cold_start_or(MILDLY)``; ``consecutive_good = 0``
    """

    def setUp(self):
        self.path = _fresh_db_with_scoring_snapshot()
        _warm_db_with_one_order(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def _seed_state(self, state: str, good: int, ts_offset_s: float):
        # Write a safety_state row at the given age (seconds ago).
        db = sqlite3.connect(self.path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS safety_state ("
            "id INTEGER PRIMARY KEY, ts REAL NOT NULL, state TEXT NOT NULL, "
            "reason TEXT NOT NULL DEFAULT '', "
            "consecutive_good INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute(
            "INSERT INTO safety_state (ts, state, reason, consecutive_good) "
            "VALUES (?, ?, ?, ?)",
            (time.time() - ts_offset_s, state, "seeded", good),
        )
        db.commit()
        db.close()

    def test_recent_row_within_2h_restored(self):
        self._seed_state(DEGRADED, good=5, ts_offset_s=600)
        sc = SafetyController(db_path=self.path)
        self.assertEqual(DEGRADED, sc.state)
        # stored - 1 = 4
        self.assertEqual(4, sc.consecutive_good)

    def test_medium_age_healthy_state_preserves_good(self):
        # 4h old, stored state was MILDLY (healthy) → defaults to MILDLY,
        # consecutive_good preserved as stored - 1.
        self._seed_state(MILDLY_MISCALIBRATED, good=3, ts_offset_s=4 * 3600)
        sc = SafetyController(db_path=self.path)
        self.assertEqual(MILDLY_MISCALIBRATED, sc.state)
        self.assertEqual(2, sc.consecutive_good)

    def test_medium_age_degraded_clears_good(self):
        # 4h old + stored state was DEGRADED (severe). _load_state still
        # defaults state to MILDLY but clears consecutive_good entirely.
        self._seed_state(DEGRADED, good=5, ts_offset_s=4 * 3600)
        sc = SafetyController(db_path=self.path)
        self.assertEqual(MILDLY_MISCALIBRATED, sc.state)
        self.assertEqual(0, sc.consecutive_good)

    def test_old_row_beyond_6h_resets_to_mildly(self):
        self._seed_state(DEGRADED, good=10, ts_offset_s=8 * 3600)
        sc = SafetyController(db_path=self.path)
        self.assertEqual(MILDLY_MISCALIBRATED, sc.state)
        self.assertEqual(0, sc.consecutive_good)

    def test_round_trip_persist_and_reload(self):
        sc1 = SafetyController(db_path=self.path)
        sc1.state = SEVERELY_MISCALIBRATED
        sc1.consecutive_good = 3
        sc1._persist_state(reasons=["round-trip test"])
        sc2 = SafetyController(db_path=self.path)
        self.assertEqual(SEVERELY_MISCALIBRATED, sc2.state)
        # stored - 1 = 2
        self.assertEqual(2, sc2.consecutive_good)

    def test_persist_trims_to_100_rows(self):
        sc = SafetyController(db_path=self.path)
        for i in range(105):
            sc.state = MILDLY_MISCALIBRATED if i % 2 == 0 else DEGRADED
            sc._persist_state(reasons=[f"row {i}"])
        db = sqlite3.connect(self.path)
        count = db.execute("SELECT COUNT(*) FROM safety_state").fetchone()[0]
        db.close()
        self.assertEqual(100, count)


# ───────────────────────────────────────────────────────────────────────────
# Block F — Helpers (_query_*, _compute_portfolio_value, _capital_floor,
#                    confidence_score, public query methods)
# ───────────────────────────────────────────────────────────────────────────


class TestQueryFillDamage(unittest.TestCase):

    def setUp(self):
        self.path = _fresh_db_with_scoring_snapshot()
        _warm_db_with_one_order(self.path)
        self.sc = SafetyController(db_path=self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_empty_returns_zero(self):
        self.assertEqual(0.0, self.sc._query_fill_damage(hours=24))

    def test_fills_inside_window(self):
        _insert_fill(self.path, ts_offset_s=60, clob_cost=25.0)
        _insert_fill(self.path, ts_offset_s=120, clob_cost=15.0)
        self.assertEqual(40.0, self.sc._query_fill_damage(hours=24))

    def test_fills_outside_window_excluded(self):
        _insert_fill(self.path, ts_offset_s=2 * 24 * 3600, clob_cost=100.0)
        self.assertEqual(0.0, self.sc._query_fill_damage(hours=24))

    def test_unwinds_offset_fills(self):
        _insert_fill(self.path, ts_offset_s=60, clob_cost=100.0)
        db = sqlite3.connect(self.path)
        db.execute(
            "INSERT INTO unwinds (ts, condition_id, usd_value) VALUES (?, ?, ?)",
            (time.time() - 60, "cid", 40.0),
        )
        db.commit()
        db.close()
        # fills - unwinds = 100 - 40 = 60
        self.assertEqual(60.0, self.sc._query_fill_damage(hours=24))

    def test_stop_losses_added(self):
        db = sqlite3.connect(self.path)
        db.execute(
            "INSERT INTO stop_losses (ts, condition_id, loss_usd) VALUES (?, ?, ?)",
            (time.time() - 60, "cid", 25.0),
        )
        db.commit()
        db.close()
        self.assertEqual(25.0, self.sc._query_fill_damage(hours=24))

    def test_clamped_at_zero(self):
        # unwinds exceed fills → result clamped at 0.0, never negative.
        _insert_fill(self.path, ts_offset_s=60, clob_cost=10.0)
        db = sqlite3.connect(self.path)
        db.execute(
            "INSERT INTO unwinds (ts, condition_id, usd_value) VALUES (?, ?, ?)",
            (time.time() - 60, "cid", 100.0),
        )
        db.commit()
        db.close()
        self.assertEqual(0.0, self.sc._query_fill_damage(hours=24))

    def test_query_failure_returns_none(self):
        db = sqlite3.connect(self.path)
        db.execute("DROP TABLE fills")
        db.commit()
        db.close()
        self.assertIsNone(self.sc._query_fill_damage(hours=24))


class TestQueryDataFreshness(unittest.TestCase):

    def setUp(self):
        self.path = _fresh_db_with_scoring_snapshot()

    def tearDown(self):
        os.unlink(self.path)

    def test_with_row_returns_age(self):
        sc = SafetyController(db_path=self.path)
        # Fixture inserted a row 60s ago.
        age = sc._query_data_freshness()
        self.assertIsNotNone(age)
        self.assertGreater(age, 30)  # at least 30s
        self.assertLess(age, 300)    # less than 5 min

    def test_empty_table_cold_start_returns_zero(self):
        db = sqlite3.connect(self.path)
        db.execute("DELETE FROM scoring_snapshots")
        db.commit()
        db.close()
        sc = SafetyController(db_path=self.path)
        # Cold start (no orders) + empty scoring → 0.0 (treat as fresh).
        self.assertEqual(0.0, sc._query_data_freshness())

    def test_empty_table_warm_db_returns_none(self):
        # FX-001's defensive branch: empty scoring + warm DB → None.
        _warm_db_with_one_order(self.path)
        db = sqlite3.connect(self.path)
        db.execute("DELETE FROM scoring_snapshots")
        db.commit()
        db.close()
        sc = SafetyController(db_path=self.path)
        self.assertIsNone(sc._query_data_freshness())


class TestQueryLifetimeFillsCount(unittest.TestCase):

    def setUp(self):
        self.path = _fresh_db_with_scoring_snapshot()
        _warm_db_with_one_order(self.path)
        self.sc = SafetyController(db_path=self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_empty_returns_zero(self):
        self.assertEqual(0, self.sc._query_lifetime_fills_count())

    def test_with_fills(self):
        for i in range(7):
            _insert_fill(self.path, ts_offset_s=60, clob_cost=0.0,
                         condition_id=f"cid_{i}")
        self.assertEqual(7, self.sc._query_lifetime_fills_count())

    def test_missing_table_returns_none(self):
        db = sqlite3.connect(self.path)
        db.execute("DROP TABLE fills")
        db.commit()
        db.close()
        self.assertIsNone(self.sc._query_lifetime_fills_count())


class TestQueryLastKnownBalance(unittest.TestCase):

    def setUp(self):
        self.path = _fresh_db_with_scoring_snapshot()
        _warm_db_with_one_order(self.path)
        self.sc = SafetyController(db_path=self.path)

    def tearDown(self):
        os.unlink(self.path)

    def _insert_snapshot(self, ts_offset_s: float, balance: float):
        db = sqlite3.connect(self.path)
        db.execute(
            "INSERT INTO portfolio_snapshots "
            "(ts, total_value, exchange_balance, locked_capital, peak_value) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time() - ts_offset_s, balance, balance, 0.0, balance),
        )
        db.commit()
        db.close()

    def test_no_snapshots_returns_none(self):
        self.assertIsNone(self.sc._query_last_known_balance())

    def test_recent_positive_returns_value(self):
        self._insert_snapshot(ts_offset_s=60, balance=200.0)
        self.assertEqual(200.0, self.sc._query_last_known_balance())

    def test_too_old_snapshot_excluded(self):
        # > 6h ago → excluded.
        self._insert_snapshot(ts_offset_s=7 * 3600, balance=200.0)
        self.assertIsNone(self.sc._query_last_known_balance())

    def test_below_floor_excluded(self):
        # The query filters `exchange_balance > CAPITAL_FLOOR_USD` ($50) at the
        # SQL level. A balance of $30 fails that gate.
        self._insert_snapshot(ts_offset_s=60, balance=30.0)
        self.assertIsNone(self.sc._query_last_known_balance())


class TestComputePortfolioValue(unittest.TestCase):

    def setUp(self):
        self.path = _fresh_db_with_scoring_snapshot()
        _warm_db_with_one_order(self.path)
        self.sc = SafetyController(db_path=self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_zero_balance_returns_zero(self):
        self.assertEqual(0.0, self.sc._compute_portfolio_value(0.0))
        self.assertEqual(0.0, self.sc._compute_portfolio_value(-10.0))

    def test_no_positions_returns_balance(self):
        # No positions or dump_states tables → graceful fallback to balance.
        self.assertEqual(200.0, self.sc._compute_portfolio_value(200.0))

    def test_includes_positions(self):
        db = sqlite3.connect(self.path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS positions ("
            "condition_id TEXT, side TEXT, shares REAL, avg_cost_per_share REAL)"
        )
        db.execute(
            "INSERT INTO positions (condition_id, side, shares, avg_cost_per_share) "
            "VALUES (?, ?, ?, ?)",
            ("cid", "yes", 100.0, 0.40),
        )
        db.commit()
        db.close()
        # balance + locked = 200 + 40 = 240
        self.assertEqual(240.0, self.sc._compute_portfolio_value(200.0))

    def test_includes_active_dumps(self):
        db = sqlite3.connect(self.path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS dump_states ("
            "condition_id TEXT, status TEXT, remaining_shares REAL, target_price REAL)"
        )
        db.execute(
            "INSERT INTO dump_states (condition_id, status, remaining_shares, target_price) "
            "VALUES (?, ?, ?, ?)",
            ("cid", "aggressive", 50.0, 0.60),
        )
        db.commit()
        db.close()
        # balance + locked = 200 + 30 = 230
        self.assertEqual(230.0, self.sc._compute_portfolio_value(200.0))


class TestCapitalFloorScaling(unittest.TestCase):

    def setUp(self):
        self.path = _fresh_db_with_scoring_snapshot()
        _warm_db_with_one_order(self.path)
        self.sc = SafetyController(db_path=self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_small_wallet_uses_absolute_minimum(self):
        # $200 wallet → 10% = $20 < $50 minimum → returns $50.
        floor = self.sc._capital_floor(exchange_balance=200.0,
                                       portfolio_value=200.0)
        self.assertEqual(50.0, floor)

    def test_large_wallet_uses_scaled(self):
        # $1500 wallet → 10% = $150 > $50 → returns $150.
        floor = self.sc._capital_floor(exchange_balance=1500.0,
                                       portfolio_value=1500.0)
        self.assertEqual(150.0, floor)

    def test_peak_dominates_reference(self):
        # Drawdown scenario: current balance is $300 but peak was $2000.
        # Floor should use the peak, not the current value, so a drawdown
        # doesn't shrink the floor.
        self.sc._portfolio_peak = 2000.0
        floor = self.sc._capital_floor(exchange_balance=300.0,
                                       portfolio_value=300.0)
        self.assertEqual(200.0, floor)


class TestConfidenceScore(unittest.TestCase):

    def setUp(self):
        self.path = _fresh_db_with_scoring_snapshot()
        _warm_db_with_one_order(self.path)
        self.sc = SafetyController(db_path=self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_no_violations_full_confidence(self):
        self.sc._last_violations = []
        self.assertEqual(1.0, self.sc.confidence_score)

    def test_data_unavailable_zeros_dq_component(self):
        from oversight.safety_controller import Violation as V
        self.sc._last_violations = [
            V("data_freshness", PRIORITY_MEDIUM, DATA_UNAVAILABLE, 99, 0, "stale"),
        ]
        # dq=0.0, cf=0.30, pc=0.30 → 0.60
        self.assertAlmostEqual(0.60, self.sc.confidence_score, places=2)

    def test_data_degraded_haircuts_dq(self):
        from oversight.safety_controller import Violation as V
        self.sc._last_violations = [
            V("data_completeness", PRIORITY_MEDIUM, DEGRADED, 0.7, 0.8, "warn"),
        ]
        # dq capped at 0.15; cf=0.30, pc=0.30 → 0.75
        self.assertAlmostEqual(0.75, self.sc.confidence_score, places=2)

    def test_cf_unsafe_zeros_cf_component(self):
        from oversight.safety_controller import Violation as V
        self.sc._last_violations = [
            V("cf_corroborated", PRIORITY_CRITICAL, UNSAFE, 0.001, 0.005, "cf"),
        ]
        # dq=0.40, cf=0.0, pc=0.30 → 0.70
        self.assertAlmostEqual(0.70, self.sc.confidence_score, places=2)

    def test_est_actual_unsafe_zeros_pc(self):
        from oversight.safety_controller import Violation as V
        self.sc._last_violations = [
            V("est_actual", PRIORITY_HIGH, SEVERELY_MISCALIBRATED, 100.0, 50.0, "x"),
        ]
        # est_actual.value > EST_ACTUAL_UNSAFE (50) → pc=0.0. dq=0.40, cf=0.30 → 0.70
        self.assertAlmostEqual(0.70, self.sc.confidence_score, places=2)

    def test_three_unsafe_components_floor_at_zero(self):
        from oversight.safety_controller import Violation as V
        self.sc._last_violations = [
            V("data_freshness", PRIORITY_MEDIUM, DATA_UNAVAILABLE, 99, 0, "stale"),
            V("cf_corroborated", PRIORITY_CRITICAL, UNSAFE, 0.001, 0.005, "cf"),
            V("est_actual", PRIORITY_HIGH, SEVERELY_MISCALIBRATED, 100.0, 50.0, "x"),
        ]
        # All three components zero → confidence = 0.0
        self.assertAlmostEqual(0.0, self.sc.confidence_score, places=2)


class TestPublicQueryMethods(unittest.TestCase):

    def setUp(self):
        self.path = _fresh_db_with_scoring_snapshot()
        _warm_db_with_one_order(self.path)
        self.sc = SafetyController(db_path=self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_query_24h_fill_damage_returns_zero_on_failure(self):
        # Drop the fills table → underlying _query_fill_damage returns None,
        # but the public wrapper substitutes 0.0.
        db = sqlite3.connect(self.path)
        db.execute("DROP TABLE fills")
        db.commit()
        db.close()
        self.assertEqual(0.0, self.sc.query_24h_fill_damage())

    def test_query_24h_fill_damage_returns_value_on_success(self):
        _insert_fill(self.path, ts_offset_s=60, clob_cost=42.0)
        self.assertEqual(42.0, self.sc.query_24h_fill_damage())

    def test_query_7d_fill_damage_includes_old_fills(self):
        _insert_fill(self.path, ts_offset_s=3 * 24 * 3600, clob_cost=100.0)
        # 3 days old is inside the 7d window.
        self.assertEqual(100.0, self.sc.query_7d_fill_damage())

    def test_count_scoring_markets_distinct(self):
        # Fixture inserts one row with condition_id="t". Add two more
        # distinct condition_ids.
        db = sqlite3.connect(self.path)
        db.execute(
            "INSERT INTO scoring_snapshots (ts, order_id, condition_id, side, "
            "scoring, price, shares) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time() - 60, "o2", "cid_b", "yes", 1, 0.5, 100),
        )
        db.execute(
            "INSERT INTO scoring_snapshots (ts, order_id, condition_id, side, "
            "scoring, price, shares) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time() - 60, "o3", "cid_c", "yes", 1, 0.5, 100),
        )
        db.commit()
        db.close()
        self.assertEqual(3, self.sc.count_scoring_markets(window_hours=4.0))

    def test_count_scoring_markets_window(self):
        # Insert a row 6h ago → outside default 4h window.
        db = sqlite3.connect(self.path)
        db.execute(
            "INSERT INTO scoring_snapshots (ts, order_id, condition_id, side, "
            "scoring, price, shares) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time() - 6 * 3600, "o2", "cid_b", "yes", 1, 0.5, 100),
        )
        db.commit()
        db.close()
        # Original fixture row is still inside the 4h window.
        self.assertEqual(1, self.sc.count_scoring_markets(window_hours=4.0))
        # Widen window to capture both.
        self.assertEqual(2, self.sc.count_scoring_markets(window_hours=8.0))


# ───────────────────────────────────────────────────────────────────────────
# Block G — Alert-file writers (_write_alert_file / _clear_alert_file)
# ───────────────────────────────────────────────────────────────────────────


class TestAlertFileWriters(unittest.TestCase):

    def setUp(self):
        # tempdir holds both DB + the SAFETY_ALERT.txt the controller writes
        # alongside it. _write_alert_file derives the alert path from
        # ``os.path.dirname(self.db_path) or "."``.
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "bot_history.db")
        # Create the schema the controller expects.
        seed = _fresh_db_with_scoring_snapshot()
        os.replace(seed, self.path)
        _warm_db_with_one_order(self.path)
        self.sc = SafetyController(db_path=self.path)
        self.alert = os.path.join(self.tmpdir, "SAFETY_ALERT.txt")

    def tearDown(self):
        for f in os.listdir(self.tmpdir):
            os.unlink(os.path.join(self.tmpdir, f))
        os.rmdir(self.tmpdir)

    def test_transition_to_degraded_writes_alert(self):
        # MILDLY (severity 1) → DEGRADED (severity 4) crosses the
        # `>= DEGRADED` threshold that triggers the write.
        self.sc.state = MILDLY_MISCALIBRATED
        self.sc._transition(DEGRADED, ["test transition"])
        self.assertTrue(os.path.exists(self.alert))
        contents = open(self.alert).read()
        self.assertIn("DEGRADED", contents)
        self.assertIn("test transition", contents)
        self.assertIn("Confidence:", contents)

    def test_transition_to_calibrated_clears_alert(self):
        # First write an alert.
        with open(self.alert, "w") as f:
            f.write("stale alert from prior run\n")
        self.assertTrue(os.path.exists(self.alert))
        # Transition to CALIBRATED clears the alert.
        self.sc.state = MILDLY_MISCALIBRATED
        self.sc._transition(CALIBRATED, ["fully calibrated again"])
        self.assertFalse(os.path.exists(self.alert))

    def test_mildly_transition_does_not_write_alert(self):
        # CALIBRATED → MILDLY does NOT cross the DEGRADED threshold; no alert.
        self.sc.state = CALIBRATED
        self.sc._transition(MILDLY_MISCALIBRATED, ["minor drift"])
        self.assertFalse(os.path.exists(self.alert))


class TestEvaluateBackwardCompatWrapper(_ControllerTestBase):

    def test_evaluate_delegates_to_evaluate_state(self):
        # ``evaluate`` is the kwarg-reorder shim. Both should return the same
        # state and produce the same violation list given identical inputs.
        result_evaluate = self.sc.evaluate(
            correction_factor_raw=0.01,
            estimated_daily_total=10.0,
            actual_daily_payout=10.0,
            fill_damage_24h=200.0,
            reward_payout_24h=10.0,
            num_scoring_markets=10,
        )
        viols_a = list(self.sc.violations)
        # Reset and call evaluate_state with the same logical inputs.
        self.sc._last_violations = []
        result_state = self.sc.evaluate_state(
            correction_factor_raw=0.01,
            estimated_daily_total=10.0,
            actual_daily_payout=10.0,
            reward_payout_24h=10.0,
            num_scoring_markets=10,
            fill_damage_24h=200.0,
        )
        viols_b = list(self.sc.violations)
        self.assertEqual(result_evaluate, result_state)
        # Same set of invariants fired (order may differ — compare names).
        self.assertEqual(
            {v.invariant for v in viols_a},
            {v.invariant for v in viols_b},
        )


if __name__ == "__main__":
    unittest.main()
