"""tests/test_regime_learning.py — Patch 5 (regime-aware frontier) suite.

Covers the 8 mandatory cases from the spec:
 1. New regime creates new memory entry
 2. Same regime updates best_reward correctly
 3. Different regimes maintain separate memory
 4. frontier_limit uses the correct regime entry
 5. Fallback works when regime is unseen
 6. Pruning keeps max 20 regimes
 7. Persistence roundtrip works
 8. Aggressive spike stays within clamp
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
    CLAMP_CAP, EMA_ALPHA,
    FRONTIER_EXPANSION_CAP_UP, FRONTIER_LIMIT_MULT,
    FRONTIER_MIN_FLOOR_FRAC,
    COLD_START_FRONTIER_MULT,
    FRONTIER_MEMORY_MAX_SIZE,
    REGIME_SPIKE_PROBABILITY, REGIME_SPIKE_CAP_UP,
    _serialize_memory, _deserialize_memory, _prune_memory,
)


_BASELINE = 0.001
_R1 = (0.1, 0.001)
_R2 = (0.3, 0.002)


def _healthy_metrics(**overrides) -> dict:
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
        "regime_id": _R1,
    }
    base.update(overrides)
    return base


def _no_spike():
    return patch("profit.learning.REGIME_SPIKE_PROBABILITY", 0.0)


# ═══════════════════════════════════════════════════════════════
# Test 1 — new regime creates new memory entry
# ═══════════════════════════════════════════════════════════════

class TestNewRegimeCreation(unittest.TestCase):

    def test_new_regime_creates_entry(self):
        with _no_spike():
            prev = LearningState()  # empty memory
            m = _healthy_metrics(regime_id=_R1, actual_reward_24h=5.0)
            new = LearningController.update_state(m, prev)
        self.assertIn(_R1, new.frontier_memory)
        entry = new.frontier_memory[_R1]
        self.assertEqual(entry["best_reward"], 5.0)
        self.assertEqual(entry["best_capital_scale"], prev.capital_scale)
        self.assertGreater(entry["last_updated"], 0.0)

    def test_no_regime_does_not_mutate_memory(self):
        """Invariant 1: no regime_id → memory stays unchanged."""
        with _no_spike():
            prev = LearningState(
                frontier_memory={_R1: {"best_reward": 5.0,
                                        "best_capital_scale": 0.8,
                                        "last_updated": 100.0}},
            )
            m = _healthy_metrics(regime_id=None, actual_reward_24h=100.0)
            new = LearningController.update_state(m, prev)
        self.assertEqual(new.frontier_memory[_R1]["best_reward"], 5.0)


# ═══════════════════════════════════════════════════════════════
# Test 2 — same regime updates best_reward correctly
# ═══════════════════════════════════════════════════════════════

class TestSameRegimeUpdate(unittest.TestCase):

    def test_improvement_replaces_best(self):
        with _no_spike():
            prev = LearningState(
                capital_scale=1.1,
                frontier_memory={_R1: {"best_reward": 5.0,
                                        "best_capital_scale": 0.8,
                                        "last_updated": 100.0}},
            )
            m = _healthy_metrics(regime_id=_R1, actual_reward_24h=10.0)
            new = LearningController.update_state(m, prev)
        entry = new.frontier_memory[_R1]
        self.assertEqual(entry["best_reward"], 10.0)
        self.assertAlmostEqual(entry["best_capital_scale"], 1.1, places=6)

    def test_no_improvement_keeps_best(self):
        with _no_spike():
            prev = LearningState(
                capital_scale=1.1,
                frontier_memory={_R1: {"best_reward": 10.0,
                                        "best_capital_scale": 0.8,
                                        "last_updated": 100.0}},
            )
            m = _healthy_metrics(regime_id=_R1, actual_reward_24h=7.0)
            new = LearningController.update_state(m, prev)
        entry = new.frontier_memory[_R1]
        self.assertEqual(entry["best_reward"], 10.0)
        self.assertEqual(entry["best_capital_scale"], 0.8)


# ═══════════════════════════════════════════════════════════════
# Test 3 — different regimes maintain separate memory
# ═══════════════════════════════════════════════════════════════

class TestRegimeSeparation(unittest.TestCase):

    def test_two_regimes_tracked_independently(self):
        with _no_spike():
            s = LearningState()
            # Cycle in regime R1
            s = LearningController.update_state(
                _healthy_metrics(regime_id=_R1, actual_reward_24h=5.0), s,
            )
            # Cycle in regime R2 — doesn't touch R1
            s = LearningController.update_state(
                _healthy_metrics(regime_id=_R2, actual_reward_24h=3.0), s,
            )
            # Another cycle in R1 with lower reward — stays at R1's best
            s = LearningController.update_state(
                _healthy_metrics(regime_id=_R1, actual_reward_24h=2.0), s,
            )
        self.assertIn(_R1, s.frontier_memory)
        self.assertIn(_R2, s.frontier_memory)
        self.assertEqual(s.frontier_memory[_R1]["best_reward"], 5.0)
        self.assertEqual(s.frontier_memory[_R2]["best_reward"], 3.0)

    def test_new_regime_does_not_overwrite_other(self):
        """Invariant 2: no cross-regime contamination."""
        with _no_spike():
            prev = LearningState(
                frontier_memory={
                    _R1: {"best_reward": 100.0, "best_capital_scale": 1.2,
                          "last_updated": 50.0},
                },
            )
            m = _healthy_metrics(regime_id=_R2, actual_reward_24h=999.0)
            new = LearningController.update_state(m, prev)
        # R1 untouched
        self.assertEqual(new.frontier_memory[_R1]["best_reward"], 100.0)
        self.assertAlmostEqual(new.frontier_memory[_R1]["best_capital_scale"], 1.2)
        # R2 created
        self.assertEqual(new.frontier_memory[_R2]["best_reward"], 999.0)


# ═══════════════════════════════════════════════════════════════
# Test 4 — frontier_limit uses correct regime entry
# ═══════════════════════════════════════════════════════════════

class TestRegimeSpecificFrontierLimit(unittest.TestCase):

    def test_frontier_limit_derives_from_current_regime_entry(self):
        """Memory holds R1=1.0 and R2=2.0 anchors. For a cycle in R2
        frontier_limit = 2.0 * 1.25 = 2.5 — but the upper clamp is
        1.2, so expansion is still possible from any current cap < 1.2.
        To verify the regime-specific path, probe a cycle in R1 where
        frontier_limit = 1.25 and prev.capital_scale is 1.24: just
        below, so expansion should still fire."""
        with _no_spike():
            prev = LearningState(
                capital_scale=1.0,
                frontier_memory={
                    _R1: {"best_reward": 100.0, "best_capital_scale": 1.0,
                          "last_updated": 0.0},
                    _R2: {"best_reward": 100.0, "best_capital_scale": 0.5,
                          "last_updated": 0.0},
                },
            )
            # Cycle in R2 — frontier_limit = 0.5 * 1.25 = 0.625. prev=1.0
            # is ABOVE frontier, so expansion should NOT fire.
            m_r2 = _healthy_metrics(
                regime_id=_R2,
                reward_growth=5.0,
                reward_efficiency=_BASELINE,
                reward_efficiency_raw=_BASELINE,
                actual_reward_24h=0.0,
            )
            new_r2 = LearningController.update_state(m_r2, prev)
            # Cycle in R1 — frontier_limit = 1.0 * 1.25 = 1.25. prev=1.0
            # is BELOW, so expansion fires.
            m_r1 = _healthy_metrics(
                regime_id=_R1,
                reward_growth=5.0,
                reward_efficiency=_BASELINE,
                reward_efficiency_raw=_BASELINE,
                actual_reward_24h=0.0,
            )
            new_r1 = LearningController.update_state(m_r1, prev)
        # R1 expansion fired; R2 did not
        self.assertGreater(new_r1.capital_scale, new_r2.capital_scale)


# ═══════════════════════════════════════════════════════════════
# Test 5 — fallback works when regime unseen
# ═══════════════════════════════════════════════════════════════

class TestColdStartFallback(unittest.TestCase):

    def test_unseen_regime_uses_cold_start_frontier(self):
        """Invariant 1 fallback: regime not in memory → frontier_limit
        = prev.capital_scale * COLD_START_FRONTIER_MULT (1.10).
        With prev=1.0, frontier_limit=1.10; prev=1.0 < 1.10 so
        expansion fires."""
        with _no_spike():
            prev = LearningState(
                capital_scale=1.0,
                frontier_memory={},  # empty → cold start
            )
            m = _healthy_metrics(
                regime_id=(0.5, 0.005),  # unseen regime
                reward_growth=5.0,
                reward_efficiency=_BASELINE,
                reward_efficiency_raw=_BASELINE,
                actual_reward_24h=0.0,  # keep memory untouched
            )
            new = LearningController.update_state(m, prev)
        # Expansion fires: post-EMA = 0.2 * 1.12 + 0.8 * 1.0 = 1.024
        # BUT a new memory entry is created for this cycle since
        # actual_reward_24h=0.0 > 0 (prev entry None). Let's verify
        # the capital_scale change only.
        expected = EMA_ALPHA * FRONTIER_EXPANSION_CAP_UP + (1 - EMA_ALPHA) * 1.0
        self.assertAlmostEqual(new.capital_scale, expected, places=6)

    def test_unseen_regime_min_floor_is_hard_clamp(self):
        """Invariant 1 fallback: regime not in memory → min_floor =
        CLAMP_CAP[0] (no anchor to derive a higher floor from)."""
        with _no_spike():
            prev = LearningState(
                capital_scale=0.35,
                prev_reward_efficiency=0.40,  # huge delta incoming
                frontier_memory={},
            )
            m = _healthy_metrics(
                regime_id=(0.9, 0.009),  # unseen
                reward_growth=-10.0,
                reward_efficiency=0.01,
                reward_efficiency_raw=0.01,
                reward_efficiency_baseline=_BASELINE,
                actual_reward_24h=0.0,
            )
            new = LearningController.update_state(m, prev)
        # Floor is CLAMP_CAP[0] = 0.30. Hard clamp enforces this.
        self.assertGreaterEqual(new.capital_scale, CLAMP_CAP[0])


# ═══════════════════════════════════════════════════════════════
# Test 6 — pruning keeps max 20 regimes
# ═══════════════════════════════════════════════════════════════

class TestMemoryPruning(unittest.TestCase):

    def test_prune_caps_at_max_size(self):
        # 25 entries with ascending last_updated
        mem = {
            (0.1 * i, 0.001): {
                "best_reward": float(i),
                "best_capital_scale": 1.0,
                "last_updated": float(i),
            }
            for i in range(25)
        }
        pruned = _prune_memory(mem)
        self.assertEqual(len(pruned), FRONTIER_MEMORY_MAX_SIZE)
        # Most-recently-updated entries survive; last_updated values
        # should be the top 20 (5..24).
        survived_ts = sorted(e["last_updated"] for e in pruned.values())
        self.assertEqual(survived_ts[0], 5.0)
        self.assertEqual(survived_ts[-1], 24.0)

    def test_update_state_prunes_live_memory(self):
        """update_state must return a state whose frontier_memory is
        bounded at FRONTIER_MEMORY_MAX_SIZE."""
        with _no_spike():
            # Seed 22 regimes, then push a cycle that creates one more
            big = {
                (0.1 * i, 0.001): {
                    "best_reward": float(i),
                    "best_capital_scale": 1.0,
                    "last_updated": float(i),
                }
                for i in range(22)
            }
            prev = LearningState(frontier_memory=big)
            m = _healthy_metrics(
                regime_id=(9.9, 0.009),  # new unique regime
                actual_reward_24h=1.0,
            )
            new = LearningController.update_state(m, prev)
        self.assertLessEqual(
            len(new.frontier_memory), FRONTIER_MEMORY_MAX_SIZE,
        )

    def test_prune_preserves_recent_over_high_reward(self):
        """Pruning is ranked by last_updated, NOT by best_reward. An
        old entry with a high reward gets evicted in favor of a recent
        one with a low reward."""
        mem = {
            ("old", "high"): {"best_reward": 1000.0,
                              "best_capital_scale": 1.2,
                              "last_updated": 1.0},
        }
        for i in range(FRONTIER_MEMORY_MAX_SIZE):
            mem[(float(i), float(i))] = {
                "best_reward": 0.01,
                "best_capital_scale": 1.0,
                "last_updated": 100.0 + i,
            }
        pruned = _prune_memory(mem)
        self.assertEqual(len(pruned), FRONTIER_MEMORY_MAX_SIZE)
        self.assertNotIn(("old", "high"), pruned)


# ═══════════════════════════════════════════════════════════════
# Test 7 — persistence roundtrip works
# ═══════════════════════════════════════════════════════════════

class TestPersistenceRoundtrip(unittest.TestCase):

    def test_frontier_memory_roundtrip_via_ctrl(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        try:
            ctrl = LearningController(f.name)
            s_in = LearningState(
                frontier_memory={
                    (0.1, 0.001): {"best_reward": 5.0,
                                    "best_capital_scale": 1.2,
                                    "last_updated": 12345.0},
                    (0.3, 0.002): {"best_reward": 7.0,
                                    "best_capital_scale": 0.9,
                                    "last_updated": 67890.0},
                    (0.5, 0.005): {"best_reward": 0.5,
                                    "best_capital_scale": 1.0,
                                    "last_updated": 111.0},
                },
                valid_cycles_observed=11,
                mode=MODE_ACTIVE,
            )
            ctrl.persist_state(s_in, MODE_ACTIVE)
            s_out = ctrl.load_state()
        finally:
            os.unlink(f.name)
        self.assertEqual(len(s_out.frontier_memory), 3)
        for key, entry in s_in.frontier_memory.items():
            self.assertIn(key, s_out.frontier_memory)
            loaded = s_out.frontier_memory[key]
            self.assertAlmostEqual(loaded["best_reward"], entry["best_reward"])
            self.assertAlmostEqual(
                loaded["best_capital_scale"], entry["best_capital_scale"],
            )
            self.assertAlmostEqual(
                loaded["last_updated"], entry["last_updated"],
            )

    def test_serialize_deserialize_symmetric(self):
        mem = {
            (0.1, 0.001): {"best_reward": 5.0,
                            "best_capital_scale": 1.2,
                            "last_updated": 123.0},
        }
        dumped = _serialize_memory(mem)
        loaded = _deserialize_memory(dumped)
        self.assertEqual(loaded, mem)

    def test_empty_memory_serializes_to_empty_object(self):
        self.assertEqual(_serialize_memory({}), "{}")
        self.assertEqual(_deserialize_memory("{}"), {})
        self.assertEqual(_deserialize_memory(""), {})
        self.assertEqual(_deserialize_memory(None), {})

    def test_malformed_json_returns_empty(self):
        self.assertEqual(_deserialize_memory("{not-json"), {})
        self.assertEqual(_deserialize_memory('"a-string"'), {})


# ═══════════════════════════════════════════════════════════════
# Test 8 — aggressive spike stays within clamp
# ═══════════════════════════════════════════════════════════════

class TestAggressiveSpike(unittest.TestCase):

    def test_spike_fires_with_forced_probability(self):
        """With REGIME_SPIKE_PROBABILITY=1.0, every cycle spikes. The
        post-EMA capital_scale MUST still respect the upper clamp."""
        with patch("profit.learning.REGIME_SPIKE_PROBABILITY", 1.0):
            s = LearningState(capital_scale=1.0)
            m = _healthy_metrics(
                regime_id=_R1,
                actual_reward_24h=0.0,
                reward_efficiency=_BASELINE,
                reward_efficiency_raw=_BASELINE,
            )
            for _ in range(200):
                s = LearningController.update_state(m, s)
                self.assertGreaterEqual(s.capital_scale, CLAMP_CAP[0])
                self.assertLessEqual(s.capital_scale, CLAMP_CAP[1])

    def test_spike_blocked_when_regime_none(self):
        """Invariant 4: spike is regime-gated. regime_id=None → no
        spike even at probability 1.0."""
        with patch("profit.learning.REGIME_SPIKE_PROBABILITY", 1.0):
            s = LearningState(capital_scale=1.0)
            m = _healthy_metrics(
                regime_id=None,
                actual_reward_24h=None,
                reward_efficiency=_BASELINE,
                reward_efficiency_raw=_BASELINE,
            )
            # Without spike, no rules fire here (reward_growth None,
            # target_eff present but re_ == target, reward_error=1.0
            # healthy, loss_error=0.4 healthy, gfr low). Capital stays
            # at 1.0 via EMA.
            out = LearningController.update_state(m, s)
        # Capital should be very close to 1.0 (no spike influence)
        self.assertAlmostEqual(out.capital_scale, 1.0, places=3)

    def test_spike_disabled_by_default_probability(self):
        """With default probability (0.05), over 1000 cycles the system
        stays firmly inside the clamp band."""
        s = LearningState(capital_scale=1.0)
        m = _healthy_metrics(
            regime_id=_R1,
            actual_reward_24h=0.0,
            reward_efficiency=_BASELINE,
            reward_efficiency_raw=_BASELINE,
        )
        # Seed random for reproducibility of this one test
        import random as _random
        _random.seed(42)
        for _ in range(1000):
            s = LearningController.update_state(m, s)
            self.assertGreaterEqual(s.capital_scale, CLAMP_CAP[0])
            self.assertLessEqual(s.capital_scale, CLAMP_CAP[1])


if __name__ == "__main__":
    unittest.main()
