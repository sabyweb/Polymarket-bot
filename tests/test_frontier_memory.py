"""tests/test_frontier_memory.py — Patch 4 tests, rewritten for Patch 5.

Patch 4 introduced global (best_reward, best_capital_scale) anchors.
Patch 5 replaced them with a regime-keyed frontier_memory dict, so these
tests are rewritten to use memory[regime_id]["best_reward"] etc. while
preserving the original behavioral coverage:

 1. best_reward updates when reward improves (now per-regime)
 2. best_capital_scale tracks correct value (now per-regime)
 3. expansion stops at frontier_limit (regime-gated)
 4. contraction never drops below min_floor (regime-gated)
 5. probe only triggers when stable
 6. aggressive expansion increases capital faster than Patch 3
 7. sharp efficiency drop triggers stronger contraction
 8. backward compatibility (no learning_state → no change)
"""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from profit.learning import (
    MODE_OFF, MODE_SHADOW, MODE_ACTIVE,
    LearningState, LearningController, LearningMetrics,
    CLAMP_CAP,
    EMA_ALPHA,
    FRONTIER_EXPANSION_CAP_UP, FRONTIER_LIMIT_MULT,
    FRONTIER_MIN_FLOOR_FRAC,
    PROBE_INTERVAL, PROBE_STABILITY_DELTA,
    PROBE_STRENGTH_BASE, PROBE_STRENGTH_CAP_COEF,
    EFFICIENCY_DELTA_SHARP_COLLAPSE, EFFICIENCY_DELTA_SHARP_CAP,
    EFFICIENCY_DELTA_COLLAPSE_CAP,
    EXPANSION_CAP_UP, EXPANSION_AGGR_UP,
    EXPANSION_EFFICIENCY_FLOOR_FRAC,
    COLD_START_FRONTIER_MULT,
    GATE_ACTIVE_CYCLES,
)


_BASELINE = 0.001
# Canonical regime id used across tests so frontier_memory entries are
# consistent between prev-state setup and metrics lookup.
_R = (0.1, 0.001)


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
        reward_usd REAL NOT NULL, PRIMARY KEY(market_id, date)
    );
    CREATE TABLE IF NOT EXISTS reward_daily (
        date TEXT PRIMARY KEY, total_combined_usd REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS book_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        condition_id TEXT NOT NULL, spread REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS cycle_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        cycle_num INTEGER NOT NULL, condition_id TEXT NOT NULL
    );
    """


def _healthy_metrics(**overrides) -> dict:
    """Baseline metric vector. Sets regime_id=_R by default so frontier
    logic uses the memory path; tests override regime_id=None to exercise
    the cold-start fallback."""
    base = {
        "status": "ok",
        "net_profit": 0.0,
        "total_rewards": 0.0,
        "total_loss": 0.0,
        "capital_deployed": 100.0,
        "reward_efficiency": _BASELINE,
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
        "actual_reward_24h": 1.0,
        "actual_loss": 5.0,
        "reward_error": 1.0,
        "loss_error": 0.4,
        "global_fill_rate_1h": 0.10,
        "volatility_proxy": 0.045,
        "market_efficiency_map": {},
        "fills_total": 500,
        "fill_unwind_pairs_total": 200,
        "reward_days": 10,
        "reward_growth": None,
        "regime_id": _R,
    }
    base.update(overrides)
    return base


def _mem_entry(best_reward=5.0, best_capital_scale=1.0, last_updated=0.0):
    return {
        "best_reward": best_reward,
        "best_capital_scale": best_capital_scale,
        "last_updated": last_updated,
    }


# Disable the stochastic spike in all tests — patched via the module
# constant. Tests that want to exercise spike behavior set this to 1.0.
def _no_spike():
    return patch("profit.learning.REGIME_SPIKE_PROBABILITY", 0.0)


# ═══════════════════════════════════════════════════════════════
# Tests 1 & 2 — frontier memory update (per-regime)
# ═══════════════════════════════════════════════════════════════

class TestFrontierMemoryUpdate(unittest.TestCase):

    def test_best_reward_updates_on_improvement(self):
        with _no_spike():
            prev = LearningState(
                frontier_memory={_R: _mem_entry(best_reward=5.0,
                                                best_capital_scale=0.8)},
            )
            m = _healthy_metrics(actual_reward_24h=10.0)
            new = LearningController.update_state(m, prev)
        entry = new.frontier_memory[_R]
        self.assertEqual(entry["best_reward"], 10.0)

    def test_best_reward_preserved_when_no_improvement(self):
        with _no_spike():
            prev = LearningState(
                capital_scale=1.0,
                frontier_memory={_R: _mem_entry(best_reward=10.0,
                                                best_capital_scale=0.8)},
            )
            m = _healthy_metrics(actual_reward_24h=7.0)
            new = LearningController.update_state(m, prev)
        entry = new.frontier_memory[_R]
        self.assertEqual(entry["best_reward"], 10.0)
        self.assertEqual(entry["best_capital_scale"], 0.8)

    def test_best_reward_preserved_when_none(self):
        """No current_reward → no memory mutation."""
        with _no_spike():
            prev = LearningState(
                frontier_memory={_R: _mem_entry(best_reward=10.0,
                                                best_capital_scale=0.8)},
            )
            m = _healthy_metrics(actual_reward_24h=None)
            new = LearningController.update_state(m, prev)
        entry = new.frontier_memory[_R]
        self.assertEqual(entry["best_reward"], 10.0)
        self.assertEqual(entry["best_capital_scale"], 0.8)

    def test_best_capital_scale_tracks_value_at_best(self):
        """On a new best reward, best_capital_scale snaps to
        prev.capital_scale — the commitment level that produced it."""
        with _no_spike():
            prev = LearningState(
                capital_scale=1.1,
                frontier_memory={_R: _mem_entry(best_reward=5.0,
                                                best_capital_scale=0.8)},
            )
            m = _healthy_metrics(actual_reward_24h=12.0)
            new = LearningController.update_state(m, prev)
        entry = new.frontier_memory[_R]
        self.assertEqual(entry["best_reward"], 12.0)
        self.assertAlmostEqual(entry["best_capital_scale"], 1.1, places=6)

    def test_frontier_memory_persist_and_load(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        try:
            ctrl = LearningController(f.name)
            s_in = LearningState(
                frontier_memory={
                    _R: _mem_entry(42.5, 1.18, 12345.0),
                    (0.3, 0.002): _mem_entry(7.0, 0.9, 11111.0),
                },
                valid_cycles_observed=5,
                mode=MODE_ACTIVE,
            )
            ctrl.persist_state(s_in, MODE_ACTIVE)
            s_out = ctrl.load_state()
            self.assertEqual(len(s_out.frontier_memory), 2)
            e = s_out.frontier_memory[_R]
            self.assertAlmostEqual(e["best_reward"], 42.5)
            self.assertAlmostEqual(e["best_capital_scale"], 1.18)
            self.assertAlmostEqual(e["last_updated"], 12345.0)
        finally:
            os.unlink(f.name)


# ═══════════════════════════════════════════════════════════════
# Test 3 — expansion stops at frontier_limit (regime-specific)
# ═══════════════════════════════════════════════════════════════

class TestFrontierGatedExpansion(unittest.TestCase):

    def test_expansion_fires_below_frontier(self):
        """memory[_R].best_capital_scale=1.0 → frontier_limit=1.25.
        prev=1.0 < 1.25 → expansion fires (u_cap *= 1.12)."""
        with _no_spike():
            prev = LearningState(
                capital_scale=1.0,
                frontier_memory={_R: _mem_entry(best_reward=100.0,
                                                best_capital_scale=1.0)},
            )
            m = _healthy_metrics(
                reward_growth=5.0,
                reward_efficiency=_BASELINE,
                reward_efficiency_raw=_BASELINE,
                actual_reward_24h=0.0,   # don't touch memory
            )
            new = LearningController.update_state(m, prev)
        expected = EMA_ALPHA * FRONTIER_EXPANSION_CAP_UP + (1 - EMA_ALPHA) * 1.0
        self.assertAlmostEqual(new.capital_scale, expected, places=6)

    def test_expansion_does_not_fire_at_or_above_frontier(self):
        """prev.capital_scale at frontier_limit → expansion multiplier
        skipped."""
        with _no_spike():
            prev = LearningState(
                capital_scale=1.25,
                frontier_memory={_R: _mem_entry(best_reward=100.0,
                                                best_capital_scale=1.0)},
            )
            m = _healthy_metrics(
                reward_growth=5.0,
                reward_efficiency=_BASELINE,
                reward_efficiency_raw=_BASELINE,
                actual_reward_24h=0.0,
            )
            new = LearningController.update_state(m, prev)
        self.assertAlmostEqual(
            new.capital_scale, min(1.25, CLAMP_CAP[1]), places=6,
        )


# ═══════════════════════════════════════════════════════════════
# Test 4 — contraction never drops below min_floor (regime-specific)
# ═══════════════════════════════════════════════════════════════

class TestContractionFloor(unittest.TestCase):

    def test_min_floor_prevents_over_contraction(self):
        """memory[_R].best_capital_scale=1.0 → min_floor=0.60."""
        with _no_spike():
            prev = LearningState(
                capital_scale=0.70,
                prev_reward_efficiency=0.50,
                frontier_memory={_R: _mem_entry(best_reward=100.0,
                                                best_capital_scale=1.0)},
            )
            m = _healthy_metrics(
                reward_growth=-5.0,
                reward_efficiency=0.01,
                reward_efficiency_raw=0.01,
                reward_efficiency_baseline=_BASELINE,
                actual_reward_24h=0.0,
            )
            new = LearningController.update_state(m, prev)
        # Floor = 0.60 * 1.0 = 0.60. EMA from prev=0.70: 0.2*0.60 + 0.8*0.70 = 0.68
        expected = EMA_ALPHA * 0.60 + (1 - EMA_ALPHA) * 0.70
        self.assertAlmostEqual(new.capital_scale, expected, places=5)

    def test_min_floor_respects_hard_clamp_lower(self):
        """memory entry with best_capital_scale=0.3 → min_floor=0.18,
        below CLAMP_CAP[0]=0.30. Hard clamp wins."""
        with _no_spike():
            prev = LearningState(
                capital_scale=0.31,
                frontier_memory={_R: _mem_entry(best_reward=10.0,
                                                best_capital_scale=0.3)},
            )
            m = _healthy_metrics(
                reward_growth=-5.0,
                reward_efficiency=0.01,
                reward_efficiency_raw=0.01,
                reward_efficiency_baseline=_BASELINE,
                actual_reward_24h=0.0,
            )
            new = LearningController.update_state(m, prev)
        self.assertGreaterEqual(new.capital_scale, CLAMP_CAP[0])


# ═══════════════════════════════════════════════════════════════
# Test 5 — probe only triggers when stable (unchanged behavior)
# ═══════════════════════════════════════════════════════════════

class TestStabilityGatedProbe(unittest.TestCase):

    def _fresh_db(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        db = sqlite3.connect(f.name)
        db.executescript(_schema_sql())
        db.commit()
        db.close()
        return f.name

    def _seed_active_gate(self, db_path):
        db = sqlite3.connect(db_path)
        now = time.time()
        for i in range(300):
            db.execute(
                "INSERT INTO fills (ts, condition_id, side, fill_type, "
                "shares, price, clob_cost, usd_value) VALUES "
                "(?, ?, 'yes', 'FULL', 50, 0.5, 0.5, 25.0)",
                (now - i * 10, f"M{i}"),
            )
        for i in range(150):
            db.execute(
                "INSERT INTO unwinds (ts, condition_id, side, shares, "
                "sell_price, usd_value, vwap_cost, pnl) VALUES "
                "(?, ?, 'yes', 50, 0.45, 22.5, 25.0, -2.5)",
                (now - i * 10, f"M{i}"),
            )
        for i in range(10):
            db.execute(
                "INSERT INTO reward_daily (date, total_combined_usd) "
                "VALUES (?, 1.0)", (f"2026-04-{i+1:02d}",),
            )
        db.execute(
            "INSERT INTO orders_placed (ts, condition_id, side, price, size) "
            "VALUES (?, 'M0', 'yes', 0.5, 50)", (now - 100,),
        )
        db.commit()
        db.close()

    def test_probe_blocked_when_unstable(self):
        db_path = self._fresh_db()
        alloc_path = tempfile.mktemp(suffix=".json")
        try:
            self._seed_active_gate(db_path)
            with open(alloc_path, "w") as f:
                json.dump({"markets": [
                    {"condition_id": "M0", "action": "deploy",
                     "daily_rate": 1.0, "q_share_pct": 10.0,
                     "est_capital_cost": 50.0},
                ]}, f)
            ctrl = LearningController(db_path, alloc_path)
            ctrl.persist_state(
                LearningState(
                    # Past the ACTIVE-cycles gate so step() actually evaluates
                    # the stability filter (otherwise it would short-circuit
                    # to SHADOW where probes never fire — making the test
                    # pass for the wrong reason).
                    valid_cycles_observed=GATE_ACTIVE_CYCLES + 10,
                    last_probe_cycle=0,
                    prev_reward_efficiency=None,   # instability
                    mode=MODE_ACTIVE,
                ),
                MODE_ACTIVE,
            )
            r = ctrl.step()
            self.assertFalse(r.metrics.get("is_probe_cycle"))
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)
            if os.path.exists(alloc_path):
                os.unlink(alloc_path)

    def test_probe_fires_when_stable_and_cadence_met(self):
        db_path = self._fresh_db()
        alloc_path = tempfile.mktemp(suffix=".json")
        try:
            self._seed_active_gate(db_path)
            with open(alloc_path, "w") as f:
                json.dump({"markets": [
                    {"condition_id": "M0", "action": "deploy",
                     "daily_rate": 1.0, "q_share_pct": 10.0,
                     "est_capital_cost": 50.0},
                ]}, f)
            ctrl = LearningController(db_path, alloc_path)
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
            self.assertTrue(r.metrics.get("is_probe_cycle"))
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)
            if os.path.exists(alloc_path):
                os.unlink(alloc_path)

    def test_probe_blocked_on_large_efficiency_swing(self):
        prev = LearningState(
            capital_scale=1.0,
            prev_reward_efficiency=0.01,
        )
        cur_eff = 0.20
        is_stable = abs(cur_eff - prev.prev_reward_efficiency) < PROBE_STABILITY_DELTA
        self.assertFalse(is_stable)


# ═══════════════════════════════════════════════════════════════
# Test 6 — aggressive expansion vs Patch 3
# ═══════════════════════════════════════════════════════════════

class TestAggressiveExpansionVsPatch3(unittest.TestCase):

    def test_patch4_multiplier_strictly_above_patch3(self):
        self.assertGreater(FRONTIER_EXPANSION_CAP_UP, EXPANSION_CAP_UP)

    def test_patch4_expansion_moves_capital_more(self):
        with _no_spike():
            prev = LearningState(
                capital_scale=1.0,
                frontier_memory={_R: _mem_entry(best_reward=100.0,
                                                best_capital_scale=1.0)},
            )
            m = _healthy_metrics(
                reward_growth=5.0,
                reward_efficiency=_BASELINE,
                reward_efficiency_raw=_BASELINE,
                actual_reward_24h=0.0,
            )
            new = LearningController.update_state(m, prev)
        patch4_val = new.capital_scale
        patch3_val = EMA_ALPHA * 1.10 + (1 - EMA_ALPHA) * 1.0
        self.assertGreater(patch4_val, patch3_val)


# ═══════════════════════════════════════════════════════════════
# Test 7 — sharp efficiency drop triggers stronger contraction
# ═══════════════════════════════════════════════════════════════

class TestSharpCollapseCorrection(unittest.TestCase):

    def test_sharp_collapse_multiplies_beyond_regular(self):
        with _no_spike():
            prev_a = LearningState(
                capital_scale=1.0,
                prev_reward_efficiency=0.30,
                frontier_memory={_R: _mem_entry(best_reward=100.0,
                                                best_capital_scale=1.0)},
            )
            m_a = _healthy_metrics(
                reward_efficiency=0.10,
                reward_efficiency_raw=0.10,
                reward_efficiency_baseline=_BASELINE,
                actual_reward_24h=0.0,
            )
            new_a = LearningController.update_state(m_a, prev_a)

            prev_b = LearningState(
                capital_scale=1.0,
                prev_reward_efficiency=0.40,
                frontier_memory={_R: _mem_entry(best_reward=100.0,
                                                best_capital_scale=1.0)},
            )
            m_b = _healthy_metrics(
                reward_efficiency=0.10,
                reward_efficiency_raw=0.10,
                reward_efficiency_baseline=_BASELINE,
                actual_reward_24h=0.0,
            )
            new_b = LearningController.update_state(m_b, prev_b)
        self.assertLess(new_b.capital_scale, new_a.capital_scale)

    def test_moderate_delta_only_part6_fires(self):
        with _no_spike():
            prev = LearningState(
                capital_scale=1.0,
                prev_reward_efficiency=0.25,
                frontier_memory={_R: _mem_entry(best_reward=100.0,
                                                best_capital_scale=1.0)},
            )
            m = _healthy_metrics(
                reward_efficiency=0.05,
                reward_efficiency_raw=0.05,
                reward_efficiency_baseline=0.05,
                reward_growth=0.0,
                actual_reward_24h=0.0,
            )
            new = LearningController.update_state(m, prev)
        expected = EMA_ALPHA * (1.0 * 0.9) + (1 - EMA_ALPHA) * 1.0
        self.assertAlmostEqual(new.capital_scale, expected, places=5)


# ═══════════════════════════════════════════════════════════════
# Test 8 — backward compatibility
# ═══════════════════════════════════════════════════════════════

class TestBackwardCompat(unittest.TestCase):

    def test_allocator_unchanged_with_none_learning_state(self):
        """learning_state=None must produce identical output to a default
        LearningState() under the continuous allocator."""
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
            a_none = allocate_portfolio(
                markets, 1000.0, cal, f.name, learning_state=None,
            )
            a_default = allocate_portfolio(
                markets, 1000.0, cal, f.name,
                learning_state=LearningState(),
            )
            for x, y in zip(a_none, a_default):
                self.assertEqual(x["action"], y["action"])
                self.assertEqual(x["shares_per_side"], y["shares_per_side"])
        finally:
            os.unlink(f.name)

    def test_default_state_has_empty_frontier_memory(self):
        """A fresh LearningState carries an empty frontier_memory dict
        — the cold-start posture where cold_start_fallback applies."""
        s = LearningState()
        self.assertEqual(s.frontier_memory, {})


# ═══════════════════════════════════════════════════════════════
# PART 6 — dynamic probe strength (unchanged)
# ═══════════════════════════════════════════════════════════════

class TestDynamicProbeStrength(unittest.TestCase):

    def test_probe_strength_scales_with_capital_scale(self):
        def strength(c):
            return PROBE_STRENGTH_BASE + PROBE_STRENGTH_CAP_COEF * min(1.0, c)
        self.assertAlmostEqual(strength(0.5), 1.075, places=6)
        self.assertAlmostEqual(strength(1.0), 1.10, places=6)
        self.assertAlmostEqual(strength(1.2), 1.10, places=6)

    def test_small_capital_gets_gentler_probe(self):
        with _no_spike():
            m = _healthy_metrics(
                reward_growth=0.0, reward_efficiency=_BASELINE,
                reward_efficiency_raw=_BASELINE, actual_reward_24h=0.0,
            )
            prev_small = LearningState(
                capital_scale=0.5,
                frontier_memory={_R: _mem_entry(best_reward=100.0,
                                                best_capital_scale=0.5)},
            )
            new_small = LearningController.update_state(
                m, prev_small, is_probe=True,
            )
            prev_large = LearningState(
                capital_scale=1.0,
                frontier_memory={_R: _mem_entry(best_reward=100.0,
                                                best_capital_scale=1.0)},
            )
            new_large = LearningController.update_state(
                m, prev_large, is_probe=True,
            )
        gain_small = (new_small.capital_scale - prev_small.capital_scale) / prev_small.capital_scale
        gain_large = (new_large.capital_scale - prev_large.capital_scale) / prev_large.capital_scale
        self.assertGreater(gain_large, gain_small)


if __name__ == "__main__":
    unittest.main()
