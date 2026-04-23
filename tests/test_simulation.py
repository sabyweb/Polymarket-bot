"""tests/test_simulation.py — audit harness test suite.

Verifies the spec's three mandatory test categories:

  1. Deterministic output with seed
  2. Invariants trigger correctly (synthesized violations)
  3. Each scenario produces the expected DIRECTION of learning

We use a small cycle count for speed (50–80 cycles) but the same seed
mechanism the production audit uses; behavior direction is checked,
not absolute values.
"""

import json
import os
import unittest

from simulation.engine import SimulationEngine
from simulation.market_env import (
    MarketEnvironment, MarketSignals, SCENARIOS,
)
from simulation.metrics import CycleMetric, MetricsTracker
from simulation.invariants import (
    InvariantViolation, check_per_cycle, check_post_run,
    CAPITAL_OVERRUN_TOLERANCE,
)
from simulation.report import build_report
from profit.learning import LearningState


# ═══════════════════════════════════════════════════════════════
# 1. Deterministic output with seed
# ═══════════════════════════════════════════════════════════════

class TestDeterminism(unittest.TestCase):

    def test_market_env_repro_per_seed(self):
        """Same (seed, scenario, cycle) → identical signals."""
        e1 = MarketEnvironment("stable_optimal", seed=7, total_cycles=10)
        e2 = MarketEnvironment("stable_optimal", seed=7, total_cycles=10)
        for c in range(10):
            self.assertEqual(e1.signals_for(c), e2.signals_for(c))

    def test_market_env_diff_seed_diverges(self):
        """Different seeds → different sequences (sanity check on RNG)."""
        e1 = MarketEnvironment("stable_optimal", seed=1, total_cycles=10)
        e2 = MarketEnvironment("stable_optimal", seed=2, total_cycles=10)
        diffs = sum(
            1 for c in range(10) if e1.signals_for(c) != e2.signals_for(c)
        )
        self.assertGreater(diffs, 0)

    def test_engine_repro_per_seed(self):
        """Two SimulationEngine.run() calls with the same seed produce
        the same final learning state and cumulative reward."""
        e1 = SimulationEngine(seed=42)
        e2 = SimulationEngine(seed=42)
        r1 = e1.run("stable_optimal", cycles=50)
        r2 = e2.run("stable_optimal", cycles=50)
        # Final learning state matches (within float epsilon)
        self.assertAlmostEqual(
            r1.final_learning_state.get("capital_scale"),
            r2.final_learning_state.get("capital_scale"),
            places=5,
        )
        self.assertAlmostEqual(
            r1.cumulative_reward, r2.cumulative_reward, places=2,
        )
        self.assertAlmostEqual(
            r1.cumulative_loss, r2.cumulative_loss, places=2,
        )


# ═══════════════════════════════════════════════════════════════
# 2. Invariants trigger correctly
# ═══════════════════════════════════════════════════════════════

class TestInvariants(unittest.TestCase):

    def _state(self, **kw):
        defaults = dict(
            capital_scale=1.0, reward_trust=1.0,
            beta=0.75, eta=0.0,
        )
        defaults.update(kw)
        return LearningState(**defaults)

    def test_capital_overrun_caught(self):
        """PATCH 7 — expected_capital exceeding the tolerance must trigger
        `expected_capital_overrun`. Notional alone no longer triggers it;
        under Polymarket mechanics notional can legitimately overcommit."""
        allocs = [{
            "action": "deploy", "condition_id": "X",
            "est_capital_cost": 3000.0, "_p_fill": 0.8,  # exp = $2400
        }]
        viols = check_per_cycle(
            cycle=0, allocations=allocs, applied_state=self._state(),
            total_capital=1000.0, total_ev=10.0, exploration_pct=0.05,
        )
        names = {v.name for v in viols}
        self.assertIn("expected_capital_overrun", names)

    def test_legacy_capital_overrun_triggers_without_p_fill(self):
        """When no `_p_fill` is stamped (legacy allocation dicts), the
        invariant falls back to the naive notional check. Required for
        backward compat with older test fixtures."""
        allocs = [{
            "action": "deploy", "condition_id": "X",
            "est_capital_cost": 2000.0,  # no _p_fill key
        }]
        viols = check_per_cycle(
            cycle=0, allocations=allocs, applied_state=self._state(),
            total_capital=1000.0, total_ev=10.0, exploration_pct=0.05,
        )
        names = {v.name for v in viols}
        self.assertIn("capital_overrun", names)

    def test_capital_under_tolerance_passes(self):
        """Notional may exceed budget under Patch 7, but expected_capital
        staying within tolerance must NOT trigger the overrun check."""
        allocs = [{
            "action": "deploy", "condition_id": "X",
            "est_capital_cost": 5000.0, "_p_fill": 0.10,  # exp = $500
        }]
        viols = check_per_cycle(
            cycle=0, allocations=allocs, applied_state=self._state(),
            total_capital=1000.0, total_ev=10.0, exploration_pct=0.05,
        )
        names = {v.name for v in viols}
        self.assertNotIn("capital_overrun", names)
        self.assertNotIn("expected_capital_overrun", names)

    def test_ev_negative_with_deployment_caught(self):
        allocs = [{
            "action": "deploy", "condition_id": "X",
            "est_capital_cost": 50.0,
        }]
        viols = check_per_cycle(
            cycle=0, allocations=allocs, applied_state=self._state(),
            total_capital=1000.0, total_ev=-5.0, exploration_pct=0.05,
        )
        names = {v.name for v in viols}
        self.assertIn("ev_negative_with_deployment", names)

    def test_ev_negative_no_deployment_passes(self):
        viols = check_per_cycle(
            cycle=0, allocations=[], applied_state=self._state(),
            total_capital=1000.0, total_ev=-5.0, exploration_pct=0.05,
        )
        names = {v.name for v in viols}
        self.assertNotIn("ev_negative_with_deployment", names)

    def test_clamp_violation_caught(self):
        """Out-of-clamp scalar must trigger {name}_out_of_clamp. With
        aggressiveness/risk_multiplier deleted, this exercises lambda_1
        instead — its clamp is [0.5, 5.0]."""
        bad_state = LearningState(
            capital_scale=1.0, reward_trust=1.0,
            lambda_1=10.0,  # > 5.0
            lambda_2=0.5,
        )
        viols = check_per_cycle(
            cycle=0, allocations=[], applied_state=bad_state,
            total_capital=1000.0, total_ev=0.0, exploration_pct=0.05,
        )
        names = {v.name for v in viols}
        self.assertIn("lambda_1_out_of_clamp", names)

    def test_exploration_pct_overrun_caught(self):
        viols = check_per_cycle(
            cycle=0, allocations=[], applied_state=self._state(),
            total_capital=1000.0, total_ev=0.0, exploration_pct=0.20,
        )
        names = {v.name for v in viols}
        self.assertIn("exploration_pct_overrun", names)

    def test_cluster_overconcentration_caught(self):
        """All allocations in the same cluster, totaling > 30% of budget."""
        allocs = [
            {"action": "deploy", "condition_id": f"M{i}",
             "_cluster_id": "C1", "est_capital_cost": 80.0}
            for i in range(5)
        ]
        viols = check_per_cycle(
            cycle=0, allocations=allocs, applied_state=self._state(),
            total_capital=1000.0, total_ev=10.0, exploration_pct=0.05,
        )
        names = {v.name for v in viols}
        self.assertIn("cluster_overconcentration", names)

    def test_post_run_oscillation_detection(self):
        """A capital_scale series that flips direction every cycle must
        be flagged as oscillating."""
        history = []
        for i in range(60):
            cap = 0.5 + 0.4 * (1 if i % 2 == 0 else -1)
            history.append({
                "valid_cycles_observed": i,
                "mode": "ACTIVE",
                "capital_scale": cap,
            })
        # MetricsTracker only used for capital_deployed (we provide empty)
        mt = MetricsTracker()
        viols = check_post_run(mt, history)
        names = {v.name for v in viols}
        self.assertIn("oscillation_persistent", names)

    def test_post_run_monotone_transition_not_flagged(self):
        """A monotone decline (cap 1.0 → 0.30 over many cycles) must
        NOT be flagged as oscillating."""
        history = []
        for i in range(100):
            cap = max(0.3, 1.0 - 0.01 * i)
            history.append({
                "valid_cycles_observed": i,
                "mode": "ACTIVE",
                "capital_scale": cap,
            })
        mt = MetricsTracker()
        viols = check_post_run(mt, history)
        names = {v.name for v in viols}
        self.assertNotIn("oscillation_persistent", names)


# ═══════════════════════════════════════════════════════════════
# 3. Each scenario produces the expected direction of learning
# ═══════════════════════════════════════════════════════════════

class TestScenarioDirections(unittest.TestCase):
    """Run a moderate-length sim per scenario and assert the direction
    of learning matches the spec. We don't require fine-grained values —
    only that the system MOVED THE RIGHT WAY."""

    @classmethod
    def setUpClass(cls):
        # 150 cycles is enough to: clear SHADOW (>= 50 valid cycles
        # post-gate-open), accumulate >= 60 ACTIVE updates, and let
        # the slow scalars (reward_trust drifts ~2% per cycle, EMA
        # alpha=0.2 on contractions) actually move the needle.
        cls.engine = SimulationEngine(seed=42)
        cls.results = {
            s: cls.engine.run(s, cycles=150) for s in SCENARIOS
        }

    def test_over_aggressive_contracts_capital(self):
        r = self.results["over_aggressive"]
        cap_init = r.learning_state_history[10]["capital_scale"]
        cap_last = r.learning_state_history[-1]["capital_scale"]
        self.assertLess(
            cap_last, cap_init,
            f"over_aggressive must contract: {cap_init} -> {cap_last}",
        )

    def test_under_deployed_does_not_over_expand(self):
        r = self.results["under_deployed"]
        cap_max = max(h["capital_scale"] for h in r.learning_state_history)
        cap_init = r.learning_state_history[0]["capital_scale"]
        self.assertLess(
            cap_max, cap_init * 1.30,
            f"under_deployed over-expanded: max={cap_max} init={cap_init}",
        )

    def test_high_reward_fake_drops_trust(self):
        r = self.results["high_reward_fake"]
        back = r.learning_state_history[len(r.learning_state_history) // 2:]
        avg_trust = sum(h["reward_trust"] for h in back) / len(back)
        self.assertLess(
            avg_trust, 0.99,
            f"high_reward_fake trust did not drop: avg back-half={avg_trust}",
        )

    def test_regime_shift_creates_multiple_regimes(self):
        r = self.results["regime_shift"]
        # End-of-run frontier_memory size OR distinct regime_ids in the
        # observed metric stream — at least one should reflect the shift.
        end_size = r.learning_state_history[-1].get(
            "frontier_memory_size", 0,
        )
        distinct_regimes = {m.regime_id for m in r.metrics.history() if m.regime_id}
        self.assertTrue(
            end_size >= 2 or len(distinct_regimes) >= 2,
            f"regime_shift not detected: mem_size={end_size} "
            f"distinct_regimes={len(distinct_regimes)}",
        )

    def test_stable_optimal_no_strong_decline(self):
        r = self.results["stable_optimal"]
        slope = r.metrics.rolling_trend_slope(
            "reward_efficiency", window=80,
        )
        self.assertIsNotNone(slope)
        self.assertGreater(
            slope, -1e-4,
            f"stable_optimal eff sharply declining: slope={slope}",
        )

    def test_no_invariant_violations_in_any_scenario(self):
        for name, r in self.results.items():
            if r.per_cycle_violations:
                self.fail(
                    f"{name} had per-cycle invariant violations: "
                    f"{[v.name for v in r.per_cycle_violations[:5]]}"
                )


# ═══════════════════════════════════════════════════════════════
# Report builder
# ═══════════════════════════════════════════════════════════════

class TestReport(unittest.TestCase):

    def test_report_status_pass_when_no_failures(self):
        engine = SimulationEngine(seed=42)
        results = [
            engine.run(s, cycles=80) for s in (
                "stable_optimal", "under_deployed",
            )
        ]
        rep = build_report(results)
        self.assertEqual(rep["status"], "PASS")
        self.assertEqual(rep["failures"], [])

    def test_report_serializes_to_json(self):
        engine = SimulationEngine(seed=42)
        rs = [engine.run("stable_optimal", cycles=60)]
        rep = build_report(rs)
        s = json.dumps(rep, default=str)
        # Roundtrip parses
        parsed = json.loads(s)
        self.assertIn("status", parsed)
        self.assertIn("scenarios", parsed)
        self.assertIn("global_summary", parsed)


if __name__ == "__main__":
    unittest.main()
