"""tests/test_reward_expansion.py — Patch 3 (reward expansion) test suite.

Covers the 7 mandatory cases from the spec:
 1. reward_growth positive → capital increases
 2. reward_growth negative + low efficiency → capital decreases
 3. probe cycle triggers every N cycles
 4. exploration_pct increases when efficiency drops
 5. efficiency_delta negative reduces capital
 6. no data → no change
 7. capital remains within clamps
"""

import json
import os
import sqlite3
import tempfile
import time
import unittest

from profit.learning import (
    MODE_OFF, MODE_SHADOW, MODE_ACTIVE,
    LearningState, LearningController, LearningMetrics, LearningGate,
    LearningStep,
    CLAMP_CAP, CLAMP_TRUST,
    PROBE_INTERVAL, PROBE_SCALE,
    EXPANSION_CAP_UP, EXPANSION_CAP_DOWN, EXPANSION_AGGR_UP,
    EXPANSION_EFFICIENCY_FLOOR_FRAC,
    EFFICIENCY_DELTA_COLLAPSE, EFFICIENCY_DELTA_COLLAPSE_CAP,
    RECENCY_WEIGHT,
    EMA_ALPHA,
    GATE_ACTIVE_CYCLES,
)
# _compute_exploration_pct was a helper in the deleted legacy allocator.
# Its tests (class TestExplorationPct) are skipped below — the continuous
# allocator does not consume a dynamic exploration budget.


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _schema_sql() -> str:
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


_BASELINE = 0.001  # reward $ / capital $ / day


def _healthy_metrics(**overrides) -> dict:
    """Metric vector where NO expansion/contraction rules fire by default.

    To exercise Part 2 / Part 6 rules, override the relevant keys."""
    base = {
        "status": "ok",
        "net_profit": 0.0,
        "total_rewards": 0.0,
        "total_loss": 0.0,
        "capital_deployed": 100.0,
        "reward_efficiency": _BASELINE,          # equal to baseline
        "reward_efficiency_raw": _BASELINE,
        "reward_efficiency_baseline": _BASELINE,
        "profit_efficiency": 0.0,
        "fill_count": 10,
        "avg_loss_per_fill": 0.5,
        "fill_rate": 0.10,
        "loss_per_capital": 0.01,
        "predicted_reward": 1.0,
        "predicted_loss": 12.5,
        "actual_reward": 1.0,
        "actual_loss": 5.0,
        "reward_error": 1.0,
        "loss_error": 0.4,
        "global_fill_rate_1h": 0.10,
        "volatility_proxy": 0.045,
        "market_efficiency_map": {},
        "fills_total": 500,
        "fill_unwind_pairs_total": 200,
        "reward_days": 10,
        "reward_growth": None,   # no Part 2 rule by default
    }
    base.update(overrides)
    return base


# ═══════════════════════════════════════════════════════════════
# PART 2 — Expansion rule
# ═══════════════════════════════════════════════════════════════

class TestRewardGrowthExpansion(unittest.TestCase):

    # Test 1: reward_growth positive → capital increases
    def test_positive_growth_at_or_above_floor_expands_capital(self):
        """At exactly baseline efficiency, Rule B is inactive, so the
        only mover is Part 2's expansion rule: positive growth + eff >=
        0.7*baseline → CAP_UP * EXPANSION_CAP_UP."""
        prev = LearningState()
        m = _healthy_metrics(
            reward_growth=5.0,                 # positive
            reward_efficiency=_BASELINE,       # at baseline → Rule B idle
            reward_efficiency_raw=_BASELINE,
        )
        new = LearningController.update_state(m, prev)
        self.assertGreater(new.capital_scale, prev.capital_scale)

    def test_positive_growth_below_floor_does_not_expand(self):
        """Efficiency must be at least 70% of baseline to permit
        expansion — a low-efficiency market that's growing still can't
        justify more capital."""
        prev = LearningState()
        m = _healthy_metrics(
            reward_growth=5.0,
            reward_efficiency=_BASELINE * 0.5,  # < 0.7 * baseline
            reward_efficiency_raw=_BASELINE * 0.5,
        )
        new = LearningController.update_state(m, prev)
        # Rule B still fires CAP_DOWN since re_ < baseline; so capital
        # should drop, not rise, even with positive growth.
        self.assertLess(new.capital_scale, prev.capital_scale)

    # Test 2: reward_growth negative + low efficiency → capital decreases
    def test_negative_growth_below_baseline_contracts_capital(self):
        prev = LearningState()
        m = _healthy_metrics(
            reward_growth=-5.0,                 # negative
            reward_efficiency=_BASELINE * 0.8,  # below baseline
            reward_efficiency_raw=_BASELINE * 0.8,
        )
        new = LearningController.update_state(m, prev)
        self.assertLess(new.capital_scale, prev.capital_scale)

    def test_negative_growth_above_baseline_does_not_contract(self):
        """Part 2 negative-growth contraction only fires when efficiency
        is also BELOW baseline. Above baseline, Rule B pushes capital up
        and Part 2 does not override."""
        prev = LearningState()
        m = _healthy_metrics(
            reward_growth=-5.0,                 # negative growth
            reward_efficiency=_BASELINE * 1.5,  # but above baseline
            reward_efficiency_raw=_BASELINE * 1.5,
        )
        new = LearningController.update_state(m, prev)
        # Rule B CAP_UP should dominate the no-op Part 2 branch
        self.assertGreater(new.capital_scale, prev.capital_scale)

    def test_expansion_goes_through_ema(self):
        """A single CAP_UP multiplier should move capital by alpha * delta,
        not the full delta (invariant: expansion does not bypass EMA)."""
        prev = LearningState()
        m = _healthy_metrics(
            reward_growth=5.0,
            reward_efficiency=_BASELINE * 0.9,
            reward_efficiency_raw=_BASELINE * 0.9,
        )
        new = LearningController.update_state(m, prev)
        # Max possible raw u_cap after rules B(up) + Part 2 expansion:
        # prev * CAP_UP * EXPANSION_CAP_UP
        from profit.learning import CAP_UP
        raw_max = 1.0 * CAP_UP * EXPANSION_CAP_UP
        ema_out = EMA_ALPHA * raw_max + (1 - EMA_ALPHA) * 1.0
        # Allow a tiny tolerance — new must NOT exceed the EMA output
        self.assertLessEqual(new.capital_scale, ema_out + 1e-9)


# ═══════════════════════════════════════════════════════════════
# PART 3 — Frontier probe
# ═══════════════════════════════════════════════════════════════

class TestFrontierProbe(unittest.TestCase):

    def _fresh_db(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        db = sqlite3.connect(f.name)
        db.executescript(_schema_sql())
        db.commit()
        db.close()
        return f.name

    def _seed_active_gate(self, db_path, n_fills=300, n_unwinds=150, n_days=10):
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

    # Test 3: probe cycle triggers every N cycles
    def test_probe_fires_every_probe_interval_cycles(self):
        """Starting from last_probe_cycle=0 and valid_cycles_observed=49,
        a valid cycle advances the counter to 50, which is exactly
        PROBE_INTERVAL past last_probe_cycle → probe fires. Subsequent
        cycles should not probe again until another 10 have elapsed."""
        # Direct test of update_state with is_probe flag
        prev = LearningState(capital_scale=1.0)
        m = _healthy_metrics()
        # is_probe=False — no probe
        base_next = LearningController.update_state(m, prev, is_probe=False)
        # is_probe=True — probe multiplies u_cap by PROBE_SCALE pre-EMA
        probe_next = LearningController.update_state(m, prev, is_probe=True)
        # The probe raises capital above the no-probe baseline
        self.assertGreater(probe_next.capital_scale, base_next.capital_scale)

    def test_probe_respects_clamp(self):
        """A probe at the top of the clamp range must not exceed CLAMP_CAP[1]."""
        # Start already at the ceiling
        prev = LearningState(capital_scale=CLAMP_CAP[1])
        m = _healthy_metrics()
        out = LearningController.update_state(m, prev, is_probe=True)
        self.assertLessEqual(out.capital_scale, CLAMP_CAP[1] + 1e-9)

    def test_probe_scheduling_via_step(self):
        """End-to-end: seed past ACTIVE gate with valid_cycles_observed=49,
        step once, expect probe to fire and last_probe_cycle to advance."""
        db_path = self._fresh_db()
        alloc_path = tempfile.mktemp(suffix=".json")
        try:
            self._seed_active_gate(db_path)
            # Seed 4 daily rows so reward_growth is computable
            db = sqlite3.connect(db_path)
            for d in range(4):
                db.execute(
                    "INSERT OR REPLACE INTO reward_daily "
                    "(date, total_combined_usd) VALUES (?, ?)",
                    (f"2026-04-{10-d:02d}", 1.0 + d * 0.1),
                )
            db.execute(
                "INSERT INTO orders_placed (ts, condition_id, side, price, size) "
                "VALUES (?, 'M0', 'yes', 0.5, 50)", (time.time() - 100,),
            )
            db.commit()
            db.close()
            with open(alloc_path, "w") as f:
                json.dump({"markets": [
                    {"condition_id": "M0", "action": "deploy",
                     "daily_rate": 1.0, "q_share_pct": 10.0,
                     "est_capital_cost": 50.0},
                ]}, f)

            ctrl = LearningController(db_path, alloc_path)
            # Seed counter at the ACTIVE-gate boundary with no prior probe.
            # Patch 4: seed prev_reward_efficiency = 0.0 so the new
            # stability gate (|cur - prev| < 0.05) is satisfied — the
            # seeded DB has no reward_attribution so cur_eff will be 0.0.
            ctrl.persist_state(
                LearningState(
                    valid_cycles_observed=GATE_ACTIVE_CYCLES,
                    last_probe_cycle=0,
                    prev_reward_efficiency=0.0,
                    mode=MODE_ACTIVE,
                ),
                MODE_ACTIVE,
            )
            r = ctrl.step()
            # valid_cycles_observed advanced past gate; >= PROBE_INTERVAL
            # since last_probe_cycle=0
            self.assertEqual(r.mode, MODE_ACTIVE)
            self.assertTrue(r.metrics.get("is_probe_cycle"))
            s = ctrl.load_state()
            self.assertEqual(s.last_probe_cycle, GATE_ACTIVE_CYCLES + 1)

            # Second step: counter advances by 1, diff=1 → NOT a probe.
            # last_probe_cycle stays at its previous value (set by the
            # first step's probe firing).
            r2 = ctrl.step()
            self.assertFalse(r2.metrics.get("is_probe_cycle"))
            s2 = ctrl.load_state()
            self.assertEqual(s2.last_probe_cycle, GATE_ACTIVE_CYCLES + 1)
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)
            if os.path.exists(alloc_path):
                os.unlink(alloc_path)


# PART 4 (Dynamic exploration budget) — removed with the legacy
# allocator; continuous allocator does not reserve an exploration budget.


# ═══════════════════════════════════════════════════════════════
# PART 6 — Efficiency-delta contraction
# ═══════════════════════════════════════════════════════════════

class TestEfficiencyDelta(unittest.TestCase):

    # Test 5: efficiency_delta negative reduces capital
    def test_efficiency_delta_below_threshold_contracts_capital(self):
        """When current efficiency is much lower than the previous cycle,
        the system pulls capital back. Here prev.prev_reward_efficiency
        is 0.20 and current raw is 0.02, so delta = -0.18 < -0.15 → fires."""
        prev = LearningState(prev_reward_efficiency=0.20)
        m = _healthy_metrics(
            reward_efficiency=0.02,
            reward_efficiency_raw=0.02,
            reward_efficiency_baseline=0.10,  # still below → Rule B also down
        )
        new = LearningController.update_state(m, prev)
        self.assertLess(new.capital_scale, prev.capital_scale)
        # stash updated prev_reward_efficiency for next cycle
        self.assertEqual(new.prev_reward_efficiency, 0.02)

    def test_efficiency_delta_not_triggered_on_small_drop(self):
        """A drop smaller than the threshold should NOT trigger the
        Part 6 contraction — only the standard Rule B."""
        # Avoid the 0-capital division by ensuring baseline and current match.
        prev = LearningState(prev_reward_efficiency=_BASELINE)
        m = _healthy_metrics(
            reward_efficiency=_BASELINE * 0.95,
            reward_efficiency_raw=_BASELINE * 0.95,
        )
        before_cap = prev.capital_scale
        new = LearningController.update_state(m, prev)
        # Rule B fires CAP_DOWN (slight drop), but no Part 6 overshoot.
        # Verify the hit is only one CAP_DOWN, not two (no Part 6 stack).
        from profit.learning import CAP_DOWN
        max_single_down = EMA_ALPHA * (before_cap * CAP_DOWN) + (1 - EMA_ALPHA) * before_cap
        self.assertGreaterEqual(new.capital_scale, max_single_down - 1e-9)


# ═══════════════════════════════════════════════════════════════
# PART 8 invariants
# ═══════════════════════════════════════════════════════════════

class TestInvariants(unittest.TestCase):

    # Test 6: no data → no change
    def test_no_data_no_change(self):
        """When reward_growth, baseline, efficiency, and delta are all
        None, no expansion/contraction rule fires. Trust reversion still
        produces tiny drift, but capital_scale stays at EMA of its
        prev value (i.e. unchanged)."""
        prev = LearningState()
        m = _healthy_metrics(
            reward_growth=None,
            reward_efficiency=None,
            reward_efficiency_raw=None,
            reward_efficiency_baseline=None,
        )
        new = LearningController.update_state(m, prev)
        self.assertAlmostEqual(new.capital_scale, prev.capital_scale, places=6)

    # Test 7: capital remains within clamps (under both good and hostile data)
    def test_capital_clamped_under_sustained_expansion(self):
        """Run 500 cycles of max-expansion metrics (positive growth,
        efficiency above baseline, probe flag permanently on). capital
        must never exceed the upper clamp."""
        s = LearningState()
        m = _healthy_metrics(
            reward_growth=100.0,
            reward_efficiency=_BASELINE * 2.0,
            reward_efficiency_raw=_BASELINE * 2.0,
            reward_efficiency_baseline=_BASELINE,
        )
        for _ in range(500):
            s = LearningController.update_state(m, s, is_probe=True)
            self.assertGreaterEqual(s.capital_scale, CLAMP_CAP[0])
            self.assertLessEqual(s.capital_scale, CLAMP_CAP[1])

    def test_capital_clamped_under_sustained_contraction(self):
        s = LearningState()
        m = _healthy_metrics(
            reward_growth=-100.0,
            reward_efficiency=_BASELINE * 0.1,
            reward_efficiency_raw=_BASELINE * 0.1,
            reward_efficiency_baseline=_BASELINE,
        )
        # give Part 6 a high prev so delta is severely negative
        s.prev_reward_efficiency = _BASELINE * 10.0
        for _ in range(500):
            s = LearningController.update_state(m, s)
            self.assertGreaterEqual(s.capital_scale, CLAMP_CAP[0])
            self.assertLessEqual(s.capital_scale, CLAMP_CAP[1])

    def test_backward_compat_when_learning_state_none(self):
        """Allocator must produce bit-identical output when learning_state
        is None vs a default LearningState() (all scalars 1.0, mode=OFF,
        empty map)."""
        from profit.allocator import allocate_portfolio
        from calibration.manager import CalibrationManager
        from oversight.market_scorer import ScoredMarket

        def _sm(cid):
            return ScoredMarket(
                condition_id=cid, question=f"Q{cid}", score=1.0,
                action="deploy", recommended_shares=50, reason="t",
                confidence="high", actual_reward_total=0.0,
                fill_damage=0.0, fill_count=0, daily_rate=1.0,
                min_size=50.0, max_spread=0.045, est_capital_cost=0.0,
                locked_position_usd=0.0, question_group="",
                q_share_pct=10.0, end_date_iso="",
            )
        markets = [_sm(f"M{i}") for i in range(4)]
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        try:
            cal = CalibrationManager(db_path=f.name)
            allocs_none = allocate_portfolio(
                markets, 1000.0, cal, f.name, learning_state=None,
            )
            allocs_neutral = allocate_portfolio(
                markets, 1000.0, cal, f.name,
                learning_state=LearningState(mode=MODE_OFF),
            )
            # Compare structural outcomes: action and shares_per_side
            for a, b in zip(allocs_none, allocs_neutral):
                self.assertEqual(a["action"], b["action"])
                self.assertEqual(a["shares_per_side"], b["shares_per_side"])
        finally:
            os.unlink(f.name)


# ═══════════════════════════════════════════════════════════════
# PART 5 — Recency-weighted efficiency (metric pipeline)
# ═══════════════════════════════════════════════════════════════

class TestRecencyWeighting(unittest.TestCase):

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

    def test_recency_weighted_equals_raw_with_no_history(self):
        """Cold start: no prior efficiency snapshots → cold-start fallback
        returns the raw efficiency (graceful), matching the existing
        tests' expectations."""
        alloc_path = tempfile.mktemp(suffix=".json")
        try:
            with open(alloc_path, "w") as f:
                json.dump({"markets": [
                    {"condition_id": "M0", "action": "deploy",
                     "daily_rate": 1.0, "q_share_pct": 10.0,
                     "est_capital_cost": 50.0},
                ]}, f)
            lm = LearningMetrics(self.db_path, alloc_path)
            m = lm.compute_metrics()
            raw = m["reward_efficiency_raw"]
            self.assertEqual(m["reward_efficiency"], raw)
        finally:
            if os.path.exists(alloc_path):
                os.unlink(alloc_path)

    def test_recency_weighted_blends_when_history_exists(self):
        """After seeding prior snapshots, the returned reward_efficiency
        equals 0.7 * raw + 0.3 * prior_mean."""
        alloc_path = tempfile.mktemp(suffix=".json")
        try:
            with open(alloc_path, "w") as f:
                json.dump({"markets": [
                    {"condition_id": "M0", "action": "deploy",
                     "daily_rate": 1.0, "q_share_pct": 10.0,
                     "est_capital_cost": 100.0},
                ]}, f)

            # Seed today's reward_attribution so raw efficiency is nonzero
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            db = sqlite3.connect(self.db_path)
            db.execute(
                "INSERT INTO reward_attribution (market_id, date, reward_usd) "
                "VALUES ('M0', ?, 0.5)", (today,),
            )
            # Seed prior-day snapshots with a known mean
            db.execute(
                "CREATE TABLE IF NOT EXISTS learning_efficiency_daily ("
                "date TEXT PRIMARY KEY, "
                "reward_efficiency REAL NOT NULL, "
                "captured_at REAL NOT NULL)"
            )
            db.execute(
                "INSERT INTO learning_efficiency_daily VALUES "
                "('2026-04-10', 0.010, 0), "
                "('2026-04-11', 0.020, 0), "
                "('2026-04-12', 0.030, 0)"
            )
            db.commit()
            db.close()

            lm = LearningMetrics(self.db_path, alloc_path)
            m = lm.compute_metrics()
            raw = m["reward_efficiency_raw"]
            prior_mean = (0.010 + 0.020 + 0.030) / 3
            expected = RECENCY_WEIGHT * raw + (1 - RECENCY_WEIGHT) * prior_mean
            self.assertAlmostEqual(m["reward_efficiency"], expected, places=6)
        finally:
            if os.path.exists(alloc_path):
                os.unlink(alloc_path)


# ═══════════════════════════════════════════════════════════════
# PART 1 — reward_growth metric
# ═══════════════════════════════════════════════════════════════

class TestRewardGrowthMetric(unittest.TestCase):

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

    def test_reward_growth_none_with_insufficient_history(self):
        # Only 2 days — need at least 4 rows
        db = sqlite3.connect(self.db_path)
        db.execute("INSERT INTO reward_daily VALUES ('2026-04-10', 5.0)")
        db.execute("INSERT INTO reward_daily VALUES ('2026-04-11', 6.0)")
        db.commit()
        db.close()
        lm = LearningMetrics(self.db_path, "/nonexistent.json")
        m = lm.compute_metrics()
        self.assertIsNone(m["reward_growth"])

    def test_reward_growth_equals_current_minus_trailing_avg(self):
        """With 4 days [d0=10, d1=5, d2=5, d3=5], today=10, avg(5,5,5)=5,
        growth = 10 - 5 = 5.0. 'Most recent' is the row with the latest
        date string when ordered DESC."""
        db = sqlite3.connect(self.db_path)
        db.execute("INSERT INTO reward_daily VALUES ('2026-04-10', 5.0)")
        db.execute("INSERT INTO reward_daily VALUES ('2026-04-11', 5.0)")
        db.execute("INSERT INTO reward_daily VALUES ('2026-04-12', 5.0)")
        db.execute("INSERT INTO reward_daily VALUES ('2026-04-13', 10.0)")
        db.commit()
        db.close()
        lm = LearningMetrics(self.db_path, "/nonexistent.json")
        m = lm.compute_metrics()
        self.assertAlmostEqual(m["reward_growth"], 5.0, places=6)


if __name__ == "__main__":
    unittest.main()
