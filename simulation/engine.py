"""simulation/engine.py — top-level SimulationEngine.

Responsible for:

  - Setting up an isolated temp SQLite + alloc JSON for one scenario run
  - Constructing the real CalibrationManager and LearningController
  - Driving N cycles via runner.execute_cycle
  - Per-cycle invariant checks (collected, never silenced)
  - Producing a SimulationResult that downstream evaluators consume

Determinism contract:
  Same (seed, scenario, cycles) → identical SimulationResult.

Production logic is NEVER duplicated or modified. The engine only:
  * generates synthetic inputs
  * persists synthetic outcomes
  * collects observed behavior

If a production module raises, the engine reports the cycle's failure
and continues — fail-loudly mode is opt-in via raise_on_violation.
"""

from __future__ import annotations
import os
import random
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import patch

from calibration.manager import CalibrationManager
from profit.learning import (
    LearningController, LearningState, LearningStep,
    REGIME_SPIKE_PROBABILITY,
)

from .invariants import (
    InvariantViolation, check_per_cycle, check_post_run,
)
from .market_env import MarketEnvironment, MarketSignals
from .metrics import CycleMetric, MetricsTracker
from .runner import (
    execute_cycle, CycleOutcome, TOTAL_CAPITAL,
)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    condition_id TEXT NOT NULL, question TEXT DEFAULT '',
    side TEXT NOT NULL, fill_type TEXT NOT NULL,
    shares REAL NOT NULL, price REAL NOT NULL,
    clob_cost REAL NOT NULL, usd_value REAL NOT NULL,
    midpoint REAL DEFAULT 0, slippage REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS unwinds (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    condition_id TEXT NOT NULL, question TEXT DEFAULT '',
    side TEXT NOT NULL, shares REAL NOT NULL,
    sell_price REAL NOT NULL, usd_value REAL NOT NULL,
    vwap_cost REAL DEFAULT 0, pnl REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders_placed (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    condition_id TEXT NOT NULL, side TEXT NOT NULL,
    price REAL NOT NULL, size REAL NOT NULL,
    order_id TEXT DEFAULT '', order_type TEXT DEFAULT 'BUY'
);
CREATE TABLE IF NOT EXISTS orders_cancelled (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    order_id TEXT NOT NULL, reason TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS reward_attribution (
    market_id TEXT NOT NULL, date TEXT NOT NULL,
    reward_usd REAL NOT NULL, PRIMARY KEY(market_id, date)
);
CREATE TABLE IF NOT EXISTS reward_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    total_reward_usd REAL NOT NULL DEFAULT 0,
    total_rebate_usd REAL NOT NULL DEFAULT 0,
    total_combined_usd REAL NOT NULL DEFAULT 0,
    num_markets_active INTEGER NOT NULL DEFAULT 0,
    est_daily_total REAL NOT NULL DEFAULT 0,
    correction_factor REAL NOT NULL DEFAULT 0,
    UNIQUE(date)
);
CREATE TABLE IF NOT EXISTS reward_daily_markets (
    date TEXT NOT NULL, condition_id TEXT NOT NULL,
    scoring_seconds REAL DEFAULT 0, daily_rate REAL DEFAULT 0,
    PRIMARY KEY(date, condition_id)
);
CREATE TABLE IF NOT EXISTS book_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    condition_id TEXT NOT NULL, spread REAL DEFAULT 0,
    midpoint REAL DEFAULT 0.5, our_bid_depth_ahead REAL DEFAULT 0,
    our_ask_depth_ahead REAL DEFAULT 0, total_bid_depth REAL DEFAULT 0,
    total_ask_depth REAL DEFAULT 0, bid_depth_5c REAL DEFAULT 0,
    ask_depth_5c REAL DEFAULT 0, daily_rate REAL DEFAULT 0,
    agent_shares REAL DEFAULT 50
);
CREATE TABLE IF NOT EXISTS cycle_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    cycle_num INTEGER NOT NULL, condition_id TEXT NOT NULL
);
"""


@dataclass
class SimulationResult:
    """End-to-end output of one scenario run."""
    scenario: str
    cycles: int
    seed: int
    metrics: MetricsTracker
    learning_state_history: list[dict]
    per_cycle_violations: list[InvariantViolation]
    post_run_violations: list[InvariantViolation]
    cumulative_reward: float
    cumulative_loss: float
    final_learning_state: dict


@contextmanager
def _deterministic_environment(seed: int):
    """Patch all sources of non-determinism that production modules
    pull from at runtime, so a (seed, scenario) repro is bit-exact:

      - Python global `random` (used by Patch 5's regime spike)
      - profit.learning.REGIME_SPIKE_PROBABILITY: forced to 0 unless
        a test explicitly enables it
      - profit.bandit's wall-clock seed: re-seeded per call deterministically

    This DOES NOT change production behavior — only its environment.
    """
    saved = random.getstate()
    random.seed(seed)
    # Force-disable the stochastic spike for the audit run so the report
    # is reproducible. The spike's correctness is tested separately in
    # tests/test_regime_learning.py.
    p_patcher = patch("profit.learning.REGIME_SPIKE_PROBABILITY", 0.0)
    p_patcher.start()
    try:
        yield
    finally:
        p_patcher.stop()
        random.setstate(saved)


class SimulationEngine:
    """Drives one scenario end-to-end and emits a SimulationResult."""

    def __init__(self, seed: int = 42):
        self.seed = int(seed)

    def _create_db(self) -> str:
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        db = sqlite3.connect(f.name)
        db.executescript(_SCHEMA_SQL)
        db.commit()
        db.close()
        return f.name

    def _create_alloc_path(self) -> str:
        # Use a guaranteed-fresh temp path; the runner writes JSON here.
        return tempfile.mktemp(suffix=".json")

    def run(self, scenario: str, cycles: int) -> SimulationResult:
        if cycles <= 0:
            raise ValueError(f"cycles must be > 0, got {cycles}")

        db_path = self._create_db()
        alloc_path = self._create_alloc_path()

        try:
            with _deterministic_environment(self.seed):
                return self._run_inner(scenario, cycles, db_path, alloc_path)
        finally:
            for p in (db_path, alloc_path):
                if p and os.path.exists(p):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

    def _run_inner(
        self,
        scenario: str,
        cycles: int,
        db_path: str,
        alloc_path: str,
    ) -> SimulationResult:
        # Each concern gets its own RNG seeded off the master seed so
        # ordering of consumption doesn't leak between concerns.
        master = random.Random(self.seed)
        market_seed = master.randint(0, 2**31 - 1)
        fill_seed = master.randint(0, 2**31 - 1)
        env = MarketEnvironment(
            scenario=scenario,
            seed=market_seed,
            total_cycles=cycles,
        )
        fill_rng = random.Random(fill_seed)
        market_rng = random.Random(master.randint(0, 2**31 - 1))

        # Real production modules — never mocked.
        calibrator = CalibrationManager(db_path=db_path)
        learn_ctrl = LearningController(
            db_path=db_path, alloc_path=alloc_path,
        )

        metrics = MetricsTracker()
        learning_history: list[dict] = []
        per_cycle_violations: list[InvariantViolation] = []

        cumulative_reward = 0.0
        cumulative_loss = 0.0

        for cycle in range(cycles):
            signals = env.signals_for(cycle)
            outcome: CycleOutcome = execute_cycle(
                cycle=cycle,
                db_path=db_path,
                alloc_path=alloc_path,
                calibrator=calibrator,
                learn_ctrl=learn_ctrl,
                signals=signals,
                market_rng=market_rng,
                fill_rng=fill_rng,
                total_capital=TOTAL_CAPITAL,
            )

            cumulative_reward += outcome.reward
            cumulative_loss += outcome.loss
            net_profit = outcome.reward - outcome.loss
            reward_eff = (
                outcome.reward / outcome.capital_deployed
                if outcome.capital_deployed > 0 else None
            )
            fill_rate = (
                outcome.fill_count / outcome.order_count
                if outcome.order_count > 0 else None
            )

            applied = outcome.learning_step.applied_state
            metric = CycleMetric(
                cycle=cycle,
                capital_deployed=outcome.capital_deployed,
                reward=outcome.reward,
                loss=outcome.loss,
                net_profit=net_profit,
                reward_efficiency=reward_eff,
                fill_rate=fill_rate,
                learning_state={
                    "aggressiveness": applied.aggressiveness,
                    "capital_scale": applied.capital_scale,
                    "risk_multiplier": applied.risk_multiplier,
                    "reward_trust": applied.reward_trust,
                    "valid_cycles_observed": applied.valid_cycles_observed,
                    "mode": applied.mode,
                    "frontier_memory_size": len(applied.frontier_memory or {}),
                },
                mode=str(applied.mode),
                regime_id=outcome.learning_step.metrics.get("regime_id"),
                exploration_pct=outcome.exploration_pct,
                total_ev=outcome.total_ev,
                total_capital_budget=outcome.total_capital,
            )
            metrics.track(metric)
            learning_history.append({
                "valid_cycles_observed": applied.valid_cycles_observed,
                "mode": applied.mode,
                "capital_scale": applied.capital_scale,
                "aggressiveness": applied.aggressiveness,
                "risk_multiplier": applied.risk_multiplier,
                "reward_trust": applied.reward_trust,
                "frontier_memory_size": len(applied.frontier_memory or {}),
            })

            per_cycle_violations.extend(check_per_cycle(
                cycle=cycle,
                allocations=outcome.allocations,
                applied_state=applied,
                total_capital=outcome.total_capital,
                total_ev=outcome.total_ev,
                exploration_pct=outcome.exploration_pct,
            ))

        post_run_violations = check_post_run(metrics, learning_history)

        final_state = (
            learning_history[-1] if learning_history else {}
        )
        return SimulationResult(
            scenario=scenario,
            cycles=cycles,
            seed=self.seed,
            metrics=metrics,
            learning_state_history=learning_history,
            per_cycle_violations=per_cycle_violations,
            post_run_violations=post_run_violations,
            cumulative_reward=cumulative_reward,
            cumulative_loss=cumulative_loss,
            final_learning_state=final_state,
        )
