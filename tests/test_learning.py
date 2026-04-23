"""tests/test_learning.py — Learning loop test suite.

Post-β/η control-law rewrite. Covers the surviving levers:

    - Gate promotion (OFF / SHADOW / ACTIVE)
    - Metrics engine (pure DB-driven, no network)
    - Controller persistence roundtrip (including beta, eta)
    - Mode behaviour (OFF / SHADOW publish neutral; ACTIVE applies)
    - capital_scale rule dynamics (capital-scale rules survive)
    - reward_trust mean reversion + rule updates
    - Clamp invariants for the four live scalars
      (capital_scale / reward_trust / beta / eta)
    - Calibrator reward_trust integration hook
"""

import json
import os
import sqlite3
import tempfile
import time
import unittest

from profit.learning import (
    MODE_OFF, MODE_SHADOW, MODE_ACTIVE,
    LearningGate, LearningMetrics, LearningState, LearningController,
    LearningStep,
    CLAMP_CAP, CLAMP_TRUST, CLAMP_BETA, CLAMP_ETA,
    DEFAULT_BETA, DEFAULT_ETA,
    REWARD_EFFICIENCY_TARGET, REWARD_EFFICIENCY_GOOD,
    EMA_ALPHA, TRUST_DOWN, TRUST_REVERSION_RATE,
    PREDICTED_LOSS_PER_FILL_BASELINE,
    LOSS_PER_CAPITAL_HIGH, BASELINE_MIN_DAYS,
)


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _schema_sql() -> str:
    """Minimal schema for LearningMetrics SQL paths."""
    return """
    CREATE TABLE IF NOT EXISTS fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL, condition_id TEXT NOT NULL,
        question TEXT DEFAULT '', side TEXT NOT NULL,
        fill_type TEXT NOT NULL, shares REAL NOT NULL,
        price REAL NOT NULL, clob_cost REAL NOT NULL,
        usd_value REAL NOT NULL, midpoint REAL DEFAULT 0,
        slippage REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS unwinds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL, condition_id TEXT NOT NULL,
        question TEXT DEFAULT '', side TEXT NOT NULL,
        shares REAL NOT NULL, sell_price REAL NOT NULL,
        usd_value REAL NOT NULL, vwap_cost REAL DEFAULT 0,
        pnl REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS orders_placed (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL, condition_id TEXT NOT NULL,
        side TEXT NOT NULL, price REAL NOT NULL,
        size REAL NOT NULL, order_id TEXT DEFAULT '',
        order_type TEXT DEFAULT 'BUY'
    );
    CREATE TABLE IF NOT EXISTS reward_attribution (
        market_id TEXT NOT NULL, date TEXT NOT NULL,
        reward_usd REAL NOT NULL,
        PRIMARY KEY(market_id, date)
    );
    CREATE TABLE IF NOT EXISTS reward_daily (
        date TEXT PRIMARY KEY,
        total_combined_usd REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS book_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL, condition_id TEXT NOT NULL,
        spread REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS cycle_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL, cycle_num INTEGER NOT NULL,
        condition_id TEXT NOT NULL
    );
    """


def _make_healthy_metrics(**overrides) -> dict:
    """Neutral metric vector that passes _metrics_complete and triggers
    no rules by default."""
    base = {
        "status": "ok",
        "net_profit": 0.0,
        "total_rewards": 0.0,
        "total_loss": 0.0,
        "capital_deployed": 100.0,
        "reward_efficiency": REWARD_EFFICIENCY_TARGET,
        "profit_efficiency": 0.0,
        "reward_efficiency_baseline": REWARD_EFFICIENCY_TARGET,
        "fill_count": 10,
        "avg_loss_per_fill": 0.5,
        "fill_rate": 0.10,
        "loss_per_capital": 0.01,
        "predicted_reward": 1.0,
        "predicted_loss": 12.5,
        "actual_reward": 1.0,
        "actual_loss": 5.0,
        "reward_error": 1.0,
        "loss_error": 1.0,
        "global_fill_rate_1h": 0.10,
        "volatility_proxy": 0.045,
        "market_efficiency_map": {},
        "fills_total": 500,
        "fill_unwind_pairs_total": 200,
        "reward_days": 10,
    }
    base.update(overrides)
    return base


# ═══════════════════════════════════════════════════════════════
# STEP 0 — Activation Gate
# ═══════════════════════════════════════════════════════════════

class TestLearningGate(unittest.TestCase):

    def test_off_when_insufficient_fills(self):
        m = {"fills_total": 50, "fill_unwind_pairs_total": 100,
             "reward_days": 10, "valid_cycles_observed": 100}
        self.assertEqual(LearningGate.evaluate_activation(m), MODE_OFF)

    def test_off_when_insufficient_pairs(self):
        m = {"fills_total": 500, "fill_unwind_pairs_total": 20,
             "reward_days": 10, "valid_cycles_observed": 100}
        self.assertEqual(LearningGate.evaluate_activation(m), MODE_OFF)

    def test_off_when_insufficient_days(self):
        m = {"fills_total": 500, "fill_unwind_pairs_total": 100,
             "reward_days": 2, "valid_cycles_observed": 100}
        self.assertEqual(LearningGate.evaluate_activation(m), MODE_OFF)

    def test_shadow_when_minimums_met(self):
        m = {"fills_total": 100, "fill_unwind_pairs_total": 50,
             "reward_days": 3, "valid_cycles_observed": 0}
        self.assertEqual(LearningGate.evaluate_activation(m), MODE_SHADOW)

    def test_shadow_when_cycles_insufficient_for_active(self):
        m = {"fills_total": 300, "fill_unwind_pairs_total": 150,
             "reward_days": 10, "valid_cycles_observed": 49}
        self.assertEqual(LearningGate.evaluate_activation(m), MODE_SHADOW)

    def test_active_at_exact_boundary(self):
        m = {"fills_total": 200, "fill_unwind_pairs_total": 100,
             "reward_days": 5, "valid_cycles_observed": 50}
        self.assertEqual(LearningGate.evaluate_activation(m), MODE_ACTIVE)


# ═══════════════════════════════════════════════════════════════
# STEP 3 — Decision Logic (surviving rules: capital_scale, reward_trust)
# ═══════════════════════════════════════════════════════════════

class TestDecisionLogic(unittest.TestCase):

    def setUp(self):
        self.prev = LearningState()

    def test_high_efficiency_increases_capital(self):
        """Rule B — reward_efficiency > baseline should push capital up."""
        m = _make_healthy_metrics(
            reward_efficiency=REWARD_EFFICIENCY_TARGET * 2.0,
            reward_efficiency_baseline=REWARD_EFFICIENCY_TARGET,
        )
        new = LearningController.update_state(m, self.prev)
        self.assertGreater(new.capital_scale, self.prev.capital_scale)

    def test_low_efficiency_decreases_capital(self):
        """Rule B — reward_efficiency < baseline should pull capital down."""
        m = _make_healthy_metrics(
            reward_efficiency=REWARD_EFFICIENCY_TARGET * 0.25,
            reward_efficiency_baseline=REWARD_EFFICIENCY_TARGET,
        )
        new = LearningController.update_state(m, self.prev)
        self.assertLess(new.capital_scale, self.prev.capital_scale)

    def test_reward_overestimate_decreases_trust(self):
        """Rule C — reward_error < 0.7 triggers TRUST_DOWN."""
        m = _make_healthy_metrics(reward_error=0.5)
        new = LearningController.update_state(m, self.prev)
        self.assertLess(new.reward_trust, self.prev.reward_trust)

    def test_healthy_reward_error_does_not_reduce_trust(self):
        """reward_error in [0.9, 1.1] is the healthy band — trust does
        not go down (reversion may still nudge it up)."""
        prev = LearningState(reward_trust=0.7)
        m = _make_healthy_metrics(reward_error=1.0)
        new = LearningController.update_state(m, prev)
        self.assertGreaterEqual(new.reward_trust, prev.reward_trust)

    def test_global_fill_rate_spike_contracts_capital(self):
        """Rule D — global_fill_rate_1h > threshold pulls capital down."""
        m = _make_healthy_metrics(global_fill_rate_1h=0.99)
        new = LearningController.update_state(m, self.prev)
        self.assertLess(new.capital_scale, self.prev.capital_scale)

    def test_values_always_clamped_after_many_cycles(self):
        """Push rules hard in both directions for 200 cycles; state must
        stay inside every clamp, including the new λ bounds."""
        m_bad = _make_healthy_metrics(
            fill_rate=0.99,
            avg_loss_per_fill=100.0,
            net_profit=-1000.0,
            reward_error=0.1,
            reward_efficiency=0.0,
            global_fill_rate_1h=0.99,
        )
        s = LearningState()
        for _ in range(200):
            s = LearningController.update_state(m_bad, s)
            self.assertGreaterEqual(s.capital_scale, CLAMP_CAP[0])
            self.assertLessEqual(s.capital_scale, CLAMP_CAP[1])
            self.assertGreaterEqual(s.reward_trust, CLAMP_TRUST[0])
            self.assertLessEqual(s.reward_trust, CLAMP_TRUST[1])
            self.assertGreaterEqual(s.beta, CLAMP_BETA[0])
            self.assertLessEqual(s.beta, CLAMP_BETA[1])
            self.assertGreaterEqual(s.eta, CLAMP_ETA[0])
            self.assertLessEqual(s.eta, CLAMP_ETA[1])

        m_good = _make_healthy_metrics(
            net_profit=1000.0,
            reward_efficiency=REWARD_EFFICIENCY_GOOD * 100.0,
            reward_error=1.0,
        )
        s = LearningState()
        for _ in range(200):
            s = LearningController.update_state(m_good, s)
            self.assertLessEqual(s.capital_scale, CLAMP_CAP[1])
            self.assertLessEqual(s.reward_trust, CLAMP_TRUST[1])

    def test_ema_smoothing_slows_change(self):
        """One cycle of TRUST_DOWN must move trust by the smoothed EMA of
        the post-reversion raw update — NOT the full raw step."""
        m = _make_healthy_metrics(reward_error=0.5)
        raw = 1.0 * TRUST_DOWN
        reverted = raw + TRUST_REVERSION_RATE * (1.0 - raw)
        expected = EMA_ALPHA * reverted + (1 - EMA_ALPHA) * 1.0
        new = LearningController.update_state(m, self.prev)
        self.assertAlmostEqual(new.reward_trust, expected, places=6)

    def test_incomplete_metrics_is_not_complete(self):
        for missing in [
            "net_profit", "total_rewards", "total_loss",
            "fill_count", "fill_rate", "avg_loss_per_fill",
            "reward_efficiency", "global_fill_rate_1h",
        ]:
            m = _make_healthy_metrics()
            m[missing] = None
            self.assertFalse(
                LearningController._metrics_complete(m),
                f"should be incomplete when {missing} is None",
            )

        m = _make_healthy_metrics()
        m["status"] = "error"
        self.assertFalse(LearningController._metrics_complete(m))

    def test_determinism(self):
        """Same inputs → same outputs (no wall-clock leak affects decisions)."""
        m = _make_healthy_metrics(reward_error=0.5)
        prev = LearningState()
        s1 = LearningController.update_state(m, prev)
        s2 = LearningController.update_state(m, prev)
        self.assertEqual(s1.capital_scale, s2.capital_scale)
        self.assertEqual(s1.reward_trust, s2.reward_trust)
        self.assertEqual(s1.beta, s2.beta)
        self.assertEqual(s1.eta,  s2.eta)

    def test_beta_eta_passthrough_when_signals_missing(self):
        """β / η have no input signals in the default metrics fixture
        (expected_util / coverage_ratio are None) — they should pass
        through unchanged."""
        prev = LearningState(beta=0.6, eta=2.0)
        m = _make_healthy_metrics()   # no expected_util / coverage_ratio
        new = LearningController.update_state(m, prev)
        self.assertAlmostEqual(new.beta, 0.6, places=6)
        self.assertAlmostEqual(new.eta,  2.0, places=6)


# ═══════════════════════════════════════════════════════════════
# STEP 1 — Metrics Engine
# ═══════════════════════════════════════════════════════════════

class TestLearningMetrics(unittest.TestCase):

    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        self.db_path = f.name
        db = sqlite3.connect(self.db_path)
        db.executescript(_schema_sql())
        db.commit()
        db.close()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_empty_db_returns_zeros_and_nones(self):
        me = LearningMetrics(self.db_path, "/nonexistent.json")
        m = me.compute_metrics()
        self.assertEqual(m["status"], "ok")
        self.assertEqual(m.get("fills_total", 0), 0)
        self.assertEqual(m.get("fill_count", 0), 0)
        # With no data, derived ratios should be None
        self.assertIsNone(m.get("reward_efficiency"))


# ═══════════════════════════════════════════════════════════════
# STEP 4 — Controller persistence
# ═══════════════════════════════════════════════════════════════

class TestControllerPersistence(unittest.TestCase):

    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        self.db_path = f.name
        db = sqlite3.connect(self.db_path)
        db.executescript(_schema_sql())
        db.commit()
        db.close()
        self.alloc_path = tempfile.mktemp(suffix=".json")

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)
        if os.path.exists(self.alloc_path):
            os.unlink(self.alloc_path)

    def test_load_state_returns_defaults_when_no_row(self):
        ctrl = LearningController(self.db_path, self.alloc_path)
        s = ctrl.load_state()
        self.assertEqual(s.capital_scale, 1.0)
        self.assertEqual(s.reward_trust, 1.0)
        self.assertEqual(s.beta, DEFAULT_BETA)
        self.assertEqual(s.eta,  DEFAULT_ETA)
        self.assertEqual(s.valid_cycles_observed, 0)

    def test_persist_and_reload_roundtrip(self):
        ctrl = LearningController(self.db_path, self.alloc_path)
        s_in = LearningState(
            capital_scale=0.8,
            reward_trust=0.6,
            beta=0.4,
            eta=2.5,
            valid_cycles_observed=42,
            updated_at=time.time(),
            mode=MODE_ACTIVE,
        )
        ctrl.persist_state(s_in, MODE_ACTIVE)
        s_out = ctrl.load_state()
        self.assertAlmostEqual(s_out.capital_scale, 0.8)
        self.assertAlmostEqual(s_out.reward_trust, 0.6)
        self.assertAlmostEqual(s_out.beta, 0.4)
        self.assertAlmostEqual(s_out.eta,  2.5)
        self.assertEqual(s_out.valid_cycles_observed, 42)
        self.assertEqual(s_out.mode, MODE_ACTIVE)


# ═══════════════════════════════════════════════════════════════
# STEP 8 — OFF / SHADOW / ACTIVE mode enforcement
# ═══════════════════════════════════════════════════════════════

class TestStepModeBehavior(unittest.TestCase):

    def _fresh_db(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        db = sqlite3.connect(f.name)
        db.executescript(_schema_sql())
        db.commit()
        db.close()
        return f.name

    def _seed_for_gate(self, db_path, n_fills, n_unwinds, n_days):
        db = sqlite3.connect(db_path)
        now = time.time()
        for i in range(n_fills):
            db.execute(
                "INSERT INTO fills (ts, condition_id, side, fill_type, "
                "shares, price, clob_cost, usd_value) VALUES "
                "(?, ?, 'yes', 'FULL', 50, 0.5, 0.5, 25.0)",
                (now - i * 10, f"M{i}"),
            )
        for i in range(n_unwinds):
            db.execute(
                "INSERT INTO unwinds (ts, condition_id, side, shares, "
                "sell_price, usd_value, vwap_cost, pnl) VALUES "
                "(?, ?, 'yes', 50, 0.45, 22.5, 25.0, -2.5)",
                (now - i * 10, f"M{i}"),
            )
        for i in range(n_days):
            db.execute(
                "INSERT INTO reward_daily (date, total_combined_usd) "
                "VALUES (?, 1.0)",
                (f"2026-04-{i+1:02d}",),
            )
        db.commit()
        db.close()

    def test_off_applied_state_is_neutral(self):
        """Invariant 1: OFF never influences decisions."""
        db_path = self._fresh_db()
        try:
            ctrl = LearningController(db_path, "/nonexistent.json")
            r = ctrl.step()
            self.assertEqual(r.mode, MODE_OFF)
            self.assertEqual(r.applied_state.capital_scale, 1.0)
            self.assertEqual(r.applied_state.reward_trust, 1.0)
            self.assertEqual(r.applied_state.beta, DEFAULT_BETA)
            self.assertEqual(r.applied_state.eta,  DEFAULT_ETA)
        finally:
            os.unlink(db_path)

    def test_off_does_not_persist_state(self):
        db_path = self._fresh_db()
        try:
            ctrl = LearningController(db_path, "/nonexistent.json")
            ctrl.step()
            db = sqlite3.connect(db_path)
            n = db.execute("SELECT COUNT(*) FROM learning_state").fetchone()[0]
            db.close()
            self.assertEqual(n, 0, "OFF must not write learning_state rows")
        finally:
            os.unlink(db_path)

    def test_shadow_applied_state_is_neutral(self):
        """SHADOW publishes neutral state even when computed state would
        move — the applied_state is what downstream consumers see."""
        db_path = self._fresh_db()
        try:
            self._seed_for_gate(db_path, n_fills=150, n_unwinds=75, n_days=4)
            ctrl = LearningController(db_path, "/nonexistent.json")
            r = ctrl.step()
            self.assertEqual(r.mode, MODE_SHADOW)
            self.assertEqual(r.applied_state.capital_scale, 1.0)
            self.assertEqual(r.applied_state.reward_trust, 1.0)
            self.assertEqual(r.applied_state.beta, DEFAULT_BETA)
            self.assertEqual(r.applied_state.eta,  DEFAULT_ETA)
        finally:
            os.unlink(db_path)

    def test_shadow_preserves_scalars_on_incomplete_metrics(self):
        """SHADOW with missing metrics must not touch scalars, must not
        increment the valid-cycles counter."""
        db_path = self._fresh_db()
        try:
            self._seed_for_gate(db_path, n_fills=150, n_unwinds=75, n_days=4)
            ctrl = LearningController(db_path, "/nonexistent.json")
            ctrl.persist_state(
                LearningState(
                    capital_scale=0.5,
                    reward_trust=0.6,
                    beta=0.4, eta=1.5,
                    valid_cycles_observed=3,
                    updated_at=time.time(),
                    mode=MODE_SHADOW,
                ),
                MODE_SHADOW,
            )
            ctrl.step()
            s = ctrl.load_state()
            self.assertAlmostEqual(s.capital_scale, 0.5)
            self.assertAlmostEqual(s.reward_trust, 0.6)
            self.assertAlmostEqual(s.beta, 0.4)
            self.assertAlmostEqual(s.eta,  1.5)
            # metrics incomplete → counter does NOT move
            self.assertEqual(s.valid_cycles_observed, 3)
        finally:
            os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════
# Calibrator integration — reward_trust flows through
# ═══════════════════════════════════════════════════════════════

class TestCalibratorRewardTrust(unittest.TestCase):

    def test_calibrator_has_reward_trust_attribute(self):
        from calibration.manager import CalibrationManager
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        try:
            cm = CalibrationManager(db_path=f.name)
            self.assertTrue(hasattr(cm, "reward_trust"))
            self.assertEqual(cm.reward_trust, 1.0)
            cm.reward_trust = 0.7
            self.assertEqual(cm.reward_trust, 0.7)
        finally:
            os.unlink(f.name)


# ═══════════════════════════════════════════════════════════════
# Mean reversion — reward_trust drifts toward 1.0 when quiet
# ═══════════════════════════════════════════════════════════════

class TestRewardTrustReversion(unittest.TestCase):

    def test_reward_trust_mean_reversion(self):
        """With reward_error=None (no rule fires) trust should drift up
        toward 1.0 by TRUST_REVERSION_RATE per cycle, smoothed by EMA."""
        m = _make_healthy_metrics(reward_error=None)
        s = LearningState(reward_trust=0.6)
        trusts = [s.reward_trust]
        for _ in range(30):
            s = LearningController.update_state(m, s)
            trusts.append(s.reward_trust)
        for a, b in zip(trusts, trusts[1:]):
            self.assertGreaterEqual(b, a - 1e-9)
        self.assertGreater(s.reward_trust, 0.6)
        for _ in range(500):
            s = LearningController.update_state(m, s)
        self.assertGreater(s.reward_trust, 0.8)
        self.assertLessEqual(s.reward_trust, 1.0)

    def test_reward_trust_reversion_single_cycle_math(self):
        """Exact one-cycle arithmetic — rule-free + reversion + EMA."""
        prev_t = 0.7
        prev = LearningState(reward_trust=prev_t)
        m = _make_healthy_metrics(reward_error=None)
        new = LearningController.update_state(m, prev)
        raw = prev_t
        reverted = raw + TRUST_REVERSION_RATE * (1.0 - raw)
        expected = EMA_ALPHA * reverted + (1 - EMA_ALPHA) * prev_t
        self.assertAlmostEqual(new.reward_trust, expected, places=6)


# ═══════════════════════════════════════════════════════════════
# Smoothing regression — no drift in the all-healthy steady state
# ═══════════════════════════════════════════════════════════════

class TestSmoothingRegression(unittest.TestCase):

    def test_steady_state_is_stable_without_baseline(self):
        m = _make_healthy_metrics(reward_efficiency_baseline=None)
        s = LearningState()
        for _ in range(50):
            s = LearningController.update_state(m, s)
        # Without baseline, only Rule C (healthy reward band) + reversion
        # act. Neither pushes strongly away from neutral.
        self.assertGreaterEqual(s.capital_scale, 0.9)
        self.assertLessEqual(s.capital_scale, 1.1)
        self.assertGreaterEqual(s.reward_trust, 0.9)
        self.assertLessEqual(s.reward_trust, 1.1)


if __name__ == "__main__":
    unittest.main()
