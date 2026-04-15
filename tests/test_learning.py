"""tests/test_learning.py — Learning loop test suite.

Covers the 7 mandatory cases from the spec plus gate/mode/integration
tests. Uses unittest + tempfile pattern to match the rest of the suite.
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
    CLAMP_AGGR, CLAMP_CAP, CLAMP_RISK, CLAMP_TRUST,
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
    no rules by default. Post-FIX 2 includes reward_efficiency_baseline
    so efficiency rules can fire when the test wants them to."""
    base = {
        "status": "ok",
        "net_profit": 0.0,
        "total_rewards": 0.0,
        "total_loss": 0.0,
        "capital_deployed": 100.0,
        "reward_efficiency": REWARD_EFFICIENCY_TARGET,
        "profit_efficiency": 0.0,
        "reward_efficiency_baseline": REWARD_EFFICIENCY_TARGET,  # FIX 2
        "fill_count": 10,
        "avg_loss_per_fill": 0.5,
        "fill_rate": 0.10,
        "loss_per_capital": 0.01,  # FIX 1: below threshold by default
        "predicted_reward": 1.0,
        "predicted_loss": 12.5,
        "actual_reward": 1.0,
        "actual_loss": 5.0,
        "reward_error": 1.0,
        "loss_error": 0.4,  # avg_loss_per_fill / baseline = 0.5/1.25 = 0.4
        "global_fill_rate_1h": 0.10,
        "volatility_proxy": 0.045,
        "market_efficiency_map": {},  # FIX 4
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
        # exactly at SHADOW boundary
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

    def test_active_well_above_thresholds(self):
        m = {"fills_total": 1000, "fill_unwind_pairs_total": 500,
             "reward_days": 30, "valid_cycles_observed": 500}
        self.assertEqual(LearningGate.evaluate_activation(m), MODE_ACTIVE)


# ═══════════════════════════════════════════════════════════════
# STEP 3 — Decision Logic (7 mandatory cases)
# ═══════════════════════════════════════════════════════════════

class TestDecisionLogic(unittest.TestCase):

    def setUp(self):
        self.prev = LearningState()

    # Case 1: High loss → aggressiveness decreases
    def test_high_loss_decreases_aggressiveness(self):
        m = _make_healthy_metrics(
            fill_rate=0.5,              # > FILL_RATE_HIGH (0.30)
            avg_loss_per_fill=2.5,      # > LOSS_PER_FILL_HIGH (1.25)
            net_profit=-5.0,            # <= 0
        )
        new = LearningController.update_state(m, self.prev)
        self.assertLess(new.aggressiveness, self.prev.aggressiveness)
        # Rule A also bumps risk_multiplier up
        self.assertGreater(new.risk_multiplier, self.prev.risk_multiplier)

    # Case 2: High reward efficiency → capital increases
    def test_high_efficiency_profitable_increases_capital(self):
        # FIX 3: net_profit no longer required for positive scaling.
        m = _make_healthy_metrics(
            net_profit=10.0,
            reward_efficiency=REWARD_EFFICIENCY_TARGET * 2.0,
            reward_efficiency_baseline=REWARD_EFFICIENCY_TARGET,
        )
        new = LearningController.update_state(m, self.prev)
        self.assertGreater(new.capital_scale, self.prev.capital_scale)

    # Case 3: Reward overestimate → trust decreases
    def test_reward_overestimate_decreases_trust(self):
        m = _make_healthy_metrics(reward_error=0.5)  # < 0.7
        new = LearningController.update_state(m, self.prev)
        self.assertLess(new.reward_trust, self.prev.reward_trust)

    # Case 4: Loss underestimate → risk increases
    def test_loss_underestimate_increases_risk(self):
        m = _make_healthy_metrics(loss_error=1.5)  # > 1.3
        new = LearningController.update_state(m, self.prev)
        self.assertGreater(new.risk_multiplier, self.prev.risk_multiplier)

    # Case 5: Values always clamped
    def test_values_always_clamped_after_many_cycles(self):
        """Push every rule hard in the same direction for 200 cycles.
        State must remain inside the hard-constraint intervals."""
        # Hostile metrics (everything bad)
        m_bad = _make_healthy_metrics(
            fill_rate=0.99,
            avg_loss_per_fill=100.0,
            net_profit=-1000.0,
            reward_error=0.1,
            loss_error=10.0,
            reward_efficiency=0.0,
            global_fill_rate_1h=0.99,
        )
        s = LearningState()
        for _ in range(200):
            s = LearningController.update_state(m_bad, s)
            self.assertGreaterEqual(s.aggressiveness, CLAMP_AGGR[0])
            self.assertLessEqual(s.aggressiveness, CLAMP_AGGR[1])
            self.assertGreaterEqual(s.capital_scale, CLAMP_CAP[0])
            self.assertLessEqual(s.capital_scale, CLAMP_CAP[1])
            self.assertGreaterEqual(s.risk_multiplier, CLAMP_RISK[0])
            self.assertLessEqual(s.risk_multiplier, CLAMP_RISK[1])
            self.assertGreaterEqual(s.reward_trust, CLAMP_TRUST[0])
            self.assertLessEqual(s.reward_trust, CLAMP_TRUST[1])

        # Equally: friendly metrics → clamps on the upper side
        m_good = _make_healthy_metrics(
            net_profit=1000.0,
            reward_efficiency=REWARD_EFFICIENCY_GOOD * 100.0,
            reward_error=1.0,
            loss_error=1.0,
        )
        s = LearningState()
        for _ in range(200):
            s = LearningController.update_state(m_good, s)
            self.assertLessEqual(s.aggressiveness, CLAMP_AGGR[1])
            self.assertLessEqual(s.capital_scale, CLAMP_CAP[1])
            self.assertLessEqual(s.reward_trust, CLAMP_TRUST[1])

    # Case 6: EMA smoothing works
    def test_ema_smoothing_slows_change(self):
        """One cycle of TRUST_DOWN must move trust by the smoothed EMA of
        the post-reversion raw update — NOT the full raw step.

        FIX 5 adds mean reversion after the rule delta, before EMA:
            raw        = prev * TRUST_DOWN                # 0.9
            reverted   = raw + 0.02 * (1 - raw)           # 0.902
            ema_out    = alpha * reverted + (1-alpha) * prev  # 0.9804
        """
        m = _make_healthy_metrics(reward_error=0.5)  # triggers TRUST_DOWN
        raw = 1.0 * TRUST_DOWN
        reverted = raw + TRUST_REVERSION_RATE * (1.0 - raw)
        expected = EMA_ALPHA * reverted + (1 - EMA_ALPHA) * 1.0
        new = LearningController.update_state(m, self.prev)
        self.assertAlmostEqual(new.reward_trust, expected, places=6)

    # Case 7: Missing data → no update
    def test_incomplete_metrics_is_not_complete(self):
        """If any driver metric is None, _metrics_complete must return False.
        The controller.step() path then skips update (fail-closed)."""
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

        # Also: status != ok is incomplete
        m = _make_healthy_metrics()
        m["status"] = "error"
        self.assertFalse(LearningController._metrics_complete(m))

    # Extra: symmetry — healthy error ratios drive recovery
    def test_healthy_loss_error_recovers_risk(self):
        """loss_error in [0.8, 1.1] should not increase risk_multiplier."""
        prev = LearningState(risk_multiplier=1.5)
        m = _make_healthy_metrics(loss_error=1.0)  # healthy band
        new = LearningController.update_state(m, prev)
        # Should not go up
        self.assertLessEqual(new.risk_multiplier, prev.risk_multiplier)

    def test_healthy_reward_error_recovers_trust(self):
        """reward_error in [0.9, 1.1] should not decrease reward_trust."""
        prev = LearningState(reward_trust=0.7)
        m = _make_healthy_metrics(reward_error=1.0)
        new = LearningController.update_state(m, prev)
        # Should not go down
        self.assertGreaterEqual(new.reward_trust, prev.reward_trust)

    def test_determinism(self):
        """Same inputs → same outputs (no wall-clock leak affects decisions)."""
        m = _make_healthy_metrics(reward_error=0.5, loss_error=1.5)
        prev = LearningState()
        s1 = LearningController.update_state(m, prev)
        s2 = LearningController.update_state(m, prev)
        self.assertEqual(s1.aggressiveness, s2.aggressiveness)
        self.assertEqual(s1.capital_scale, s2.capital_scale)
        self.assertEqual(s1.risk_multiplier, s2.risk_multiplier)
        self.assertEqual(s1.reward_trust, s2.reward_trust)


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
        self.alloc_path = tempfile.mktemp(suffix=".json")

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)
        if os.path.exists(self.alloc_path):
            os.unlink(self.alloc_path)

    def test_empty_db_returns_zeros_and_nones(self):
        lm = LearningMetrics(self.db_path, self.alloc_path)
        m = lm.compute_metrics()
        self.assertEqual(m["status"], "ok")
        self.assertEqual(m["fill_count"], 0)
        self.assertEqual(m["total_rewards"], 0.0)
        self.assertIsNone(m["fill_rate"])         # no orders → None, not 0.0
        self.assertIsNone(m["reward_efficiency"]) # no capital → None
        self.assertIsNone(m["predicted_reward"])  # no alloc file
        self.assertIsNone(m["avg_loss_per_fill"]) # no fills
        self.assertIsNone(m["loss_error"])

    def test_missing_alloc_file_returns_none_predicted(self):
        lm = LearningMetrics(self.db_path, "/nonexistent/path.json")
        m = lm.compute_metrics()
        self.assertIsNone(m["predicted_reward"])
        self.assertEqual(m["capital_deployed"], 0.0)

    def test_alloc_file_parses_deploy_entries_only(self):
        with open(self.alloc_path, "w") as f:
            json.dump({"allocations": [
                {"action": "deploy", "daily_rate": 2.0, "q_share_pct": 10.0,
                 "est_capital_cost": 50.0},
                {"action": "deploy", "daily_rate": 4.0, "q_share_pct": 5.0,
                 "est_capital_cost": 80.0},
                {"action": "avoid", "daily_rate": 100.0, "q_share_pct": 50.0,
                 "est_capital_cost": 200.0},  # must be skipped
            ]}, f)
        lm = LearningMetrics(self.db_path, self.alloc_path)
        m = lm.compute_metrics()
        # predicted = 2.0 * 0.10 + 4.0 * 0.05 = 0.4
        self.assertAlmostEqual(m["predicted_reward"], 0.4, places=5)
        self.assertAlmostEqual(m["capital_deployed"], 130.0, places=5)

    def test_fills_and_unwinds_produce_avg_loss(self):
        now = time.time()
        db = sqlite3.connect(self.db_path)
        db.execute(
            "INSERT INTO fills (ts, condition_id, side, fill_type, shares, "
            "price, clob_cost, usd_value) VALUES (?, 'X', 'yes', 'FULL', "
            "50, 0.5, 0.5, 25.0)", (now - 100,),
        )
        db.execute(
            "INSERT INTO fills (ts, condition_id, side, fill_type, shares, "
            "price, clob_cost, usd_value) VALUES (?, 'X', 'yes', 'FULL', "
            "50, 0.5, 0.5, 25.0)", (now - 200,),
        )
        db.execute(
            "INSERT INTO unwinds (ts, condition_id, side, shares, "
            "sell_price, usd_value, vwap_cost, pnl) VALUES "
            "(?, 'X', 'yes', 50, 0.45, 22.5, 25.0, -2.5)", (now - 50,),
        )
        db.execute(
            "INSERT INTO orders_placed (ts, condition_id, side, price, size) "
            "VALUES (?, 'X', 'yes', 0.5, 50)", (now - 300,),
        )
        db.commit()
        db.close()
        lm = LearningMetrics(self.db_path, "/nonexistent.json")
        m = lm.compute_metrics()
        self.assertEqual(m["fill_count"], 2)
        # fill_cost = 50, unwind_revenue = 22.5, net_loss = 27.5
        self.assertAlmostEqual(m["total_loss"], 27.5, places=5)
        # avg_loss_per_fill = 27.5 / 2 = 13.75
        self.assertAlmostEqual(m["avg_loss_per_fill"], 13.75, places=5)
        # loss_error = 13.75 / PREDICTED_LOSS_PER_FILL_BASELINE
        self.assertAlmostEqual(
            m["loss_error"], 13.75 / PREDICTED_LOSS_PER_FILL_BASELINE, places=5,
        )

    def test_safe_div_no_crash_on_zero_capital(self):
        """Invariant: never divide by zero even with all-zero state."""
        lm = LearningMetrics(self.db_path, "/nonexistent.json")
        m = lm.compute_metrics()
        # These must not raise and must be None (not inf/nan)
        self.assertIsNone(m["reward_efficiency"])
        self.assertIsNone(m["profit_efficiency"])


# ═══════════════════════════════════════════════════════════════
# Controller persistence & mode-behavior integration
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

    def test_load_state_returns_neutral_when_no_row(self):
        ctrl = LearningController(self.db_path, self.alloc_path)
        s = ctrl.load_state()
        self.assertEqual(s.aggressiveness, 1.0)
        self.assertEqual(s.capital_scale, 1.0)
        self.assertEqual(s.risk_multiplier, 1.0)
        self.assertEqual(s.reward_trust, 1.0)
        self.assertEqual(s.valid_cycles_observed, 0)

    def test_persist_and_reload_roundtrip(self):
        ctrl = LearningController(self.db_path, self.alloc_path)
        s_in = LearningState(
            aggressiveness=0.7, capital_scale=0.8,
            risk_multiplier=1.4, reward_trust=0.6,
            valid_cycles_observed=42, updated_at=time.time(),
            mode=MODE_ACTIVE,
        )
        ctrl.persist_state(s_in, MODE_ACTIVE)
        s_out = ctrl.load_state()
        self.assertAlmostEqual(s_out.aggressiveness, 0.7)
        self.assertAlmostEqual(s_out.capital_scale, 0.8)
        self.assertAlmostEqual(s_out.risk_multiplier, 1.4)
        self.assertAlmostEqual(s_out.reward_trust, 0.6)
        self.assertEqual(s_out.valid_cycles_observed, 42)
        self.assertEqual(s_out.mode, MODE_ACTIVE)


class TestStepModeBehavior(unittest.TestCase):
    """End-to-end mode enforcement — invariant 1 is critical:
    OFF and SHADOW MUST publish neutral applied_state."""

    def _fresh_db(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        db = sqlite3.connect(f.name)
        db.executescript(_schema_sql())
        db.commit()
        db.close()
        return f.name

    def _seed_for_gate(self, db_path, n_fills, n_unwinds, n_days):
        """Insert rows to satisfy gate thresholds."""
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

    def test_off_applied_state_is_always_neutral(self):
        """Invariant 1: OFF never influences decisions."""
        db_path = self._fresh_db()
        try:
            ctrl = LearningController(db_path, "/nonexistent.json")
            r = ctrl.step()
            self.assertEqual(r.mode, MODE_OFF)
            self.assertEqual(r.applied_state.aggressiveness, 1.0)
            self.assertEqual(r.applied_state.capital_scale, 1.0)
            self.assertEqual(r.applied_state.risk_multiplier, 1.0)
            self.assertEqual(r.applied_state.reward_trust, 1.0)
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

    def test_shadow_applied_state_is_always_neutral(self):
        """Invariant 1: SHADOW never influences decisions, only logs."""
        db_path = self._fresh_db()
        try:
            self._seed_for_gate(db_path, n_fills=150, n_unwinds=75, n_days=4)
            ctrl = LearningController(db_path, "/nonexistent.json")
            r = ctrl.step()
            self.assertEqual(r.mode, MODE_SHADOW)
            # applied MUST be neutral regardless of what was computed
            self.assertEqual(r.applied_state.aggressiveness, 1.0)
            self.assertEqual(r.applied_state.capital_scale, 1.0)
            self.assertEqual(r.applied_state.risk_multiplier, 1.0)
            self.assertEqual(r.applied_state.reward_trust, 1.0)
        finally:
            os.unlink(db_path)

    def test_shadow_preserves_scalars_and_gates_counter_on_valid_cycle(self):
        """SHADOW must NOT touch behavioral scalars. FIX 6: the counter
        increments only when metrics_complete. Here the alloc file is
        missing so predicted_reward=None → metrics incomplete → counter
        stays put."""
        db_path = self._fresh_db()
        try:
            self._seed_for_gate(db_path, n_fills=150, n_unwinds=75, n_days=4)
            ctrl = LearningController(db_path, "/nonexistent.json")
            ctrl.persist_state(
                LearningState(
                    aggressiveness=0.5, capital_scale=0.5,
                    risk_multiplier=1.5, reward_trust=0.6,
                    valid_cycles_observed=3, updated_at=time.time(),
                    mode=MODE_SHADOW,
                ),
                MODE_SHADOW,
            )
            ctrl.step()
            s = ctrl.load_state()
            # Scalars must be preserved
            self.assertAlmostEqual(s.aggressiveness, 0.5)
            self.assertAlmostEqual(s.capital_scale, 0.5)
            self.assertAlmostEqual(s.risk_multiplier, 1.5)
            self.assertAlmostEqual(s.reward_trust, 0.6)
            # FIX 6: metrics were incomplete — counter does NOT increment
            self.assertEqual(s.valid_cycles_observed, 3)
        finally:
            os.unlink(db_path)

    def test_active_applies_and_persists(self):
        db_path = self._fresh_db()
        alloc_path = tempfile.mktemp(suffix=".json")
        try:
            # Seed past ACTIVE thresholds AND cycles_observed >= 50
            self._seed_for_gate(db_path, n_fills=300, n_unwinds=150, n_days=10)
            ctrl = LearningController(db_path, alloc_path)
            ctrl.persist_state(
                LearningState(
                    aggressiveness=1.0, capital_scale=1.0,
                    risk_multiplier=1.0, reward_trust=1.0,
                    valid_cycles_observed=60, updated_at=time.time(),
                ),
                MODE_SHADOW,
            )
            # Minimal alloc file so predicted_reward is defined (not None)
            with open(alloc_path, "w") as f:
                json.dump({"allocations": [
                    {"condition_id": "M0", "action": "deploy",
                     "daily_rate": 1.0, "q_share_pct": 10.0,
                     "est_capital_cost": 50.0},
                ]}, f)
            # Add an order_placed so fill_rate is computable
            db = sqlite3.connect(db_path)
            db.execute(
                "INSERT INTO orders_placed (ts, condition_id, side, price, size) "
                "VALUES (?, 'M0', 'yes', 0.5, 50)", (time.time() - 100,),
            )
            db.commit()
            db.close()

            r = ctrl.step()
            self.assertEqual(r.mode, MODE_ACTIVE)
            # applied_state is the COMPUTED state (not neutral)
            s = ctrl.load_state()
            self.assertEqual(s.valid_cycles_observed, 61)
            # computed_state matches applied_state in ACTIVE
            self.assertEqual(
                r.applied_state.aggressiveness, r.computed_state.aggressiveness
            )
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)
            if os.path.exists(alloc_path):
                os.unlink(alloc_path)


# ═══════════════════════════════════════════════════════════════
# Allocator integration — learning_state flows through
# ═══════════════════════════════════════════════════════════════

class TestAllocatorIntegration(unittest.TestCase):
    """Verify the three allocator injection points:
       capital_scale, risk_multiplier, aggressiveness.
    reward_trust is tested separately via CalibrationManager."""

    def test_risk_multiplier_inflates_loss_term(self):
        from profit.allocator import _risk_adjusted_score
        ev = 1.0
        p = 0.3
        loss = 2.0
        base = _risk_adjusted_score(ev, p, loss, risk_multiplier=1.0)
        inflated = _risk_adjusted_score(ev, p, loss, risk_multiplier=2.0)
        self.assertLess(inflated, base)
        # Explicit: base = 1/(1 + 0.3*2) = 1/1.6 = 0.625
        #          inflated = 1/(1 + 0.3*2*2) = 1/2.2 ≈ 0.4545
        self.assertAlmostEqual(base, 1 / 1.6, places=5)
        self.assertAlmostEqual(inflated, 1 / 2.2, places=5)

    def test_risk_multiplier_below_1_clamped(self):
        from profit.allocator import _risk_adjusted_score
        # Even if caller passes 0.5, floor is 1.0 (invariant)
        base = _risk_adjusted_score(1.0, 0.3, 2.0, risk_multiplier=1.0)
        clamped = _risk_adjusted_score(1.0, 0.3, 2.0, risk_multiplier=0.5)
        self.assertAlmostEqual(base, clamped, places=6)

    def test_neutral_learning_state_preserves_legacy_behavior(self):
        """learning_state=None must be identical to passing all-neutral
        scalars, which must be identical to current production behavior."""
        from profit.allocator import _risk_adjusted_score
        # Same scores with rm=1.0 and rm=None (via default)
        with_default = _risk_adjusted_score(1.0, 0.3, 2.0)
        with_explicit = _risk_adjusted_score(1.0, 0.3, 2.0, risk_multiplier=1.0)
        self.assertAlmostEqual(with_default, with_explicit, places=6)


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
            # settable
            cm.reward_trust = 0.7
            self.assertEqual(cm.reward_trust, 0.7)
        finally:
            os.unlink(f.name)


# ═══════════════════════════════════════════════════════════════
# Patch 2 — 7 new tests for FIXES 1–7
# ═══════════════════════════════════════════════════════════════

class TestPatch2Fixes(unittest.TestCase):
    """Covers the 7 mandatory spec tests for the reward-efficiency patch."""

    # Test 1 — High loss_per_capital triggers aggressiveness reduction
    def test_high_loss_per_capital_triggers_aggressiveness_reduction(self):
        """FIX 1: loss_per_capital > threshold OR avg_loss_per_fill high
        BOTH trigger Rule A down-shift. This case proves it fires even
        when per-fill loss is NOT high — the new capital-normalized path."""
        prev = LearningState()
        m = _make_healthy_metrics(
            fill_rate=0.5,              # high
            avg_loss_per_fill=0.5,      # LOW (below LOSS_PER_FILL_HIGH=1.25)
            loss_per_capital=0.10,      # HIGH (> LOSS_PER_CAPITAL_HIGH=0.05)
            net_profit=-5.0,
        )
        new = LearningController.update_state(m, prev)
        self.assertLess(new.aggressiveness, prev.aggressiveness)
        self.assertGreater(new.risk_multiplier, prev.risk_multiplier)

    # Test 2 — Baseline missing → efficiency rules skipped
    def test_missing_baseline_skips_efficiency_rules(self):
        """FIX 2: when reward_efficiency_baseline is None (< 3 days of
        history), Rule B must not fire and Rule A positive branch must
        not fire. Capital and aggressiveness remain at EMA drift only."""
        prev = LearningState(capital_scale=1.0, aggressiveness=1.0)
        m_no_baseline = _make_healthy_metrics(
            reward_efficiency=REWARD_EFFICIENCY_TARGET * 10.0,  # way above
            reward_efficiency_baseline=None,
            net_profit=100.0,
        )
        new = LearningController.update_state(m_no_baseline, prev)
        # EMA of 1.0 * 1.0 = 1.0 — no rule fired
        self.assertAlmostEqual(new.capital_scale, 1.0, places=6)
        self.assertAlmostEqual(new.aggressiveness, 1.0, places=6)

    # Test 3 — Reward-first scaling ignores profit
    def test_reward_first_scaling_ignores_profit_sign(self):
        """FIX 3: positive scaling (aggr UP, cap UP) should fire on
        efficiency > baseline alone — net_profit sign is irrelevant."""
        prev = LearningState()
        m = _make_healthy_metrics(
            reward_efficiency=REWARD_EFFICIENCY_TARGET * 2.0,  # > baseline
            reward_efficiency_baseline=REWARD_EFFICIENCY_TARGET,
            net_profit=-50.0,  # NEGATIVE — old rule would block UP
        )
        new = LearningController.update_state(m, prev)
        self.assertGreater(new.capital_scale, prev.capital_scale)
        self.assertGreater(new.aggressiveness, prev.aggressiveness)

    # Test 4 — Market efficiency ranking adjusts scores
    def test_market_efficiency_quintile_multipliers(self):
        """FIX 4: bottom 20% of markets get 0.8×, top 20% get 1.1×.
        Verified via the helper functions the allocator uses."""
        from profit.allocator import (
            _efficiency_quintiles, _efficiency_multiplier,
        )
        eff_map = {
            "m1": 0.0001, "m2": 0.0005, "m3": 0.0010,
            "m4": 0.0020, "m5": 0.0050,
        }
        p20, p80 = _efficiency_quintiles(eff_map)
        self.assertIsNotNone(p20)
        self.assertIsNotNone(p80)
        # Bottom: m1 should be in bottom quintile
        self.assertEqual(_efficiency_multiplier("m1", eff_map, p20, p80), 0.8)
        # Top: m5 should be in top quintile
        self.assertEqual(_efficiency_multiplier("m5", eff_map, p20, p80), 1.1)
        # Middle: m3 should be neutral
        self.assertEqual(_efficiency_multiplier("m3", eff_map, p20, p80), 1.0)

        # Too few markets → rule disabled (neutral for all)
        small_map = {"a": 1.0, "b": 2.0}
        p20s, p80s = _efficiency_quintiles(small_map)
        self.assertIsNone(p20s)
        self.assertEqual(
            _efficiency_multiplier("a", small_map, p20s, p80s), 1.0,
        )

    # Test 5 — reward_trust reverts upward over time
    def test_reward_trust_mean_reversion(self):
        """FIX 5: in the absence of any reward_error signal, trust drifts
        UP toward 1.0 by TRUST_REVERSION_RATE * gap per cycle (before EMA).
        Combined with EMA the drift is intentionally gradual:
            per_cycle_delta ≈ EMA_ALPHA * TRUST_REVERSION_RATE * (1 - prev)
        With alpha=0.2 and rate=0.02 the time constant is ~250 cycles, so
        we verify (a) monotonic ascent and (b) substantial recovery over
        a long horizon (≥ 200 cycles)."""
        m = _make_healthy_metrics(reward_error=None)
        s = LearningState(reward_trust=0.6)
        trusts = [s.reward_trust]
        for _ in range(30):
            s = LearningController.update_state(m, s)
            trusts.append(s.reward_trust)
        # Monotonic ascent (within float epsilon)
        for a, b in zip(trusts, trusts[1:]):
            self.assertGreaterEqual(b, a - 1e-9)
        # Some recovery happened
        self.assertGreater(s.reward_trust, 0.6)
        # Long horizon: trust ascends well past start toward 1.0
        for _ in range(500):
            s = LearningController.update_state(m, s)
        self.assertGreater(s.reward_trust, 0.8)
        self.assertLessEqual(s.reward_trust, 1.0)

    # Bonus — explicit one-cycle reversion arithmetic
    def test_reward_trust_reversion_single_cycle_math(self):
        """Verify the exact one-cycle update against the formula.

            raw_after_rule = prev * 1.0  (no rule fires; reward_error=None)
            reverted       = raw + RATE * (1 - raw)
            ema_out        = alpha * reverted + (1-alpha) * prev
        """
        prev_t = 0.7
        prev = LearningState(reward_trust=prev_t)
        m = _make_healthy_metrics(reward_error=None)
        new = LearningController.update_state(m, prev)
        raw = prev_t  # no rule
        reverted = raw + TRUST_REVERSION_RATE * (1.0 - raw)
        expected = EMA_ALPHA * reverted + (1 - EMA_ALPHA) * prev_t
        self.assertAlmostEqual(new.reward_trust, expected, places=6)

    # Test 6 — cycles only increment on valid data
    def test_valid_cycles_only_increment_on_complete_metrics(self):
        """FIX 6: valid_cycles_observed counter must reflect only cycles
        with complete metrics. Incomplete cycles don't count toward the
        50-cycle ACTIVE promotion threshold."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        db = sqlite3.connect(f.name)
        db.executescript(_schema_sql())
        # Seed SHADOW gate thresholds
        for i in range(150):
            db.execute(
                "INSERT INTO fills (ts, condition_id, side, fill_type, "
                "shares, price, clob_cost, usd_value) VALUES "
                "(?, ?, 'yes', 'FULL', 50, 0.5, 0.5, 25.0)",
                (time.time() - i * 10, f"M{i}"),
            )
        for i in range(75):
            db.execute(
                "INSERT INTO unwinds (ts, condition_id, side, shares, "
                "sell_price, usd_value, vwap_cost, pnl) VALUES "
                "(?, ?, 'yes', 50, 0.45, 22.5, 25.0, -2.5)",
                (time.time() - i * 10, f"M{i}"),
            )
        for i in range(4):
            db.execute(
                "INSERT INTO reward_daily (date, total_combined_usd) "
                "VALUES (?, 1.0)",
                (f"2026-04-{i+1:02d}",),
            )
        db.commit()
        db.close()

        alloc_path = tempfile.mktemp(suffix=".json")
        try:
            # No alloc file → predicted_reward = None → metrics incomplete
            ctrl = LearningController(f.name, alloc_path)
            r1 = ctrl.step()
            self.assertEqual(r1.mode, MODE_SHADOW)
            s1 = ctrl.load_state()
            self.assertEqual(s1.valid_cycles_observed, 0)
            # Second step: still no alloc file → still no increment
            r2 = ctrl.step()
            s2 = ctrl.load_state()
            self.assertEqual(s2.valid_cycles_observed, 0)

            # Now write a valid alloc file → metrics complete → increment
            with open(alloc_path, "w") as fh:
                json.dump({"allocations": [
                    {"condition_id": "M0", "action": "deploy",
                     "daily_rate": 1.0, "q_share_pct": 10.0,
                     "est_capital_cost": 50.0},
                ]}, fh)
            db = sqlite3.connect(f.name)
            db.execute(
                "INSERT INTO orders_placed (ts, condition_id, side, price, size) "
                "VALUES (?, 'M0', 'yes', 0.5, 50)", (time.time() - 100,),
            )
            db.commit()
            db.close()
            ctrl.step()
            s3 = ctrl.load_state()
            self.assertEqual(s3.valid_cycles_observed, 1)
        finally:
            if os.path.exists(f.name):
                os.unlink(f.name)
            if os.path.exists(alloc_path):
                os.unlink(alloc_path)

    # Test 7 — Exploration activates only in ACTIVE mode
    def test_exploration_only_active_mode(self):
        """FIX 7: _apply_micro_exploration only runs when learning_state.mode
        is ACTIVE. In OFF/SHADOW the full capital budget is used by the
        normal allocation path (no 5% reservation)."""
        from profit.allocator import allocate_portfolio
        from calibration.manager import CalibrationManager
        from oversight.market_scorer import ScoredMarket

        # Build a minimal scored-market set
        def _sm(cid, score=1.0, action="deploy"):
            return ScoredMarket(
                condition_id=cid,
                question=f"Q{cid}",
                score=score,
                action=action,
                recommended_shares=50,
                reason="test",
                confidence="high",
                actual_reward_total=0.0,
                fill_damage=0.0,
                fill_count=0,
                daily_rate=1.0,
                min_size=50.0,
                max_spread=0.045,
                est_capital_cost=0.0,
                locked_position_usd=0.0,
                question_group="",
                q_share_pct=10.0,
                end_date_iso="",
            )
        markets = [_sm(f"M{i}") for i in range(8)]

        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        try:
            cal = CalibrationManager(db_path=f.name)
            # Allocate with neutral (OFF mode) state — no exploration
            ls_off = LearningState(mode=MODE_OFF)
            allocs_off = allocate_portfolio(
                markets, 1000.0, cal, f.name, learning_state=ls_off,
            )
            self.assertFalse(
                any(a.get("_exploration") for a in allocs_off),
                "OFF mode must not trigger exploration",
            )

            # Same call with SHADOW mode — still no exploration
            ls_shadow = LearningState(mode=MODE_SHADOW)
            allocs_shadow = allocate_portfolio(
                markets, 1000.0, cal, f.name, learning_state=ls_shadow,
            )
            self.assertFalse(
                any(a.get("_exploration") for a in allocs_shadow),
                "SHADOW mode must not trigger exploration",
            )
        finally:
            os.unlink(f.name)


# ═══════════════════════════════════════════════════════════════
# Smoothing regression: scalars do not drift in an all-healthy world
# ═══════════════════════════════════════════════════════════════

class TestSmoothingRegression(unittest.TestCase):
    """Invariant: with all-healthy metrics and no baseline, the system is
    stable (no unbounded drift). This guards against rule interactions
    that could push state away from neutral in the steady state."""

    def test_steady_state_is_stable_without_baseline(self):
        m = _make_healthy_metrics(reward_efficiency_baseline=None)
        s = LearningState()
        for _ in range(50):
            s = LearningController.update_state(m, s)
        # Without baseline, only Rule C (healthy reward/loss bands) +
        # reversion act. None of them push strongly away from neutral.
        self.assertGreaterEqual(s.aggressiveness, 0.9)
        self.assertLessEqual(s.aggressiveness, 1.1)
        self.assertGreaterEqual(s.capital_scale, 0.9)
        self.assertLessEqual(s.capital_scale, 1.1)


if __name__ == "__main__":
    unittest.main()
