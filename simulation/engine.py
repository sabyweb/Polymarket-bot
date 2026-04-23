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
    InvariantViolation, check_per_cycle, check_post_run, check_sim_sanity,
)
from .v3_metrics import compute_v3_cycle
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
    # SIM PATCH PART 8 — harness sanity diagnostics
    sim_sanity_violations: list[InvariantViolation] = field(default_factory=list)
    # V3 — per-cycle expected-capital / overcommit / tail-risk snapshots
    v3_per_cycle: list[dict] = field(default_factory=list)


# SIM PATCH PART 2 — fixed simulated-time epoch. Chosen to be "now-ish"
# so that datetime.fromtimestamp(sim_now) renders a real-looking date,
# but far enough in the past that no real-world calendar boundaries
# interact with the sim's 1 hr/cycle advance across a 200-cycle run.
_SIM_EPOCH = 2000000000.0  # 2033-05-18 UTC


class _SimClock:
    """Mutable holder used to advance the simulated wall-clock per cycle.

    The engine patches `time.time` globally to call `_SimClock.now()`, so
    every module that does `import time; time.time()` — including
    production learning/calibration/allocator — sees the same simulated
    time. `advance_to_cycle(k)` pins the clock to SIM_EPOCH + k * 3600
    seconds, giving "one simulated hour per cycle" semantics.
    """

    def __init__(self):
        self._cycle = 0

    def advance_to_cycle(self, cycle: int) -> None:
        self._cycle = int(cycle)

    def now(self) -> float:
        return _SIM_EPOCH + self._cycle * 3600.0


@contextmanager
def _deterministic_environment(seed: int):
    """Patch all sources of non-determinism that production modules
    pull from at runtime, so a (seed, scenario) repro is bit-exact:

      - Python global `random` (used by Patch 5's regime spike)
      - numpy.random global (seeded if numpy is importable; some
        calibration models use it for feature scaling / shuffles)
      - `time.time()` — patched globally to return simulated seconds
        so 24h-windowed SQL in LearningMetrics captures only the last
        24 cycles' worth of inserts (see _SimClock)
      - profit.learning.REGIME_SPIKE_PROBABILITY: forced to 0 unless
        a test explicitly enables it
      - profit.bandit's wall-clock seed: re-seeded per call deterministically

    SIM PATCH PART 7 — numpy seeding + time-clock patching are new.
    Without the time patch, the sim's 200 cycles execute in a few real
    seconds and production's `ts > time.time() - 86400` window includes
    everything — forcing loss_per_capital to read against cumulative
    loss but per-cycle capital (the bug Patch 6 couldn't beat).

    Production behaviour is unchanged — only the wall-clock the tests
    feed it.
    """
    saved = random.getstate()
    random.seed(seed)
    # numpy seeding (best-effort — numpy is optional for some paths).
    np_saved_state = None
    try:
        import numpy as _np
        np_saved_state = _np.random.get_state()
        _np.random.seed(seed & 0xFFFFFFFF)
    except Exception:
        _np = None
    # Force-disable the stochastic spike for the audit run so the report
    # is reproducible. The spike's correctness is tested separately in
    # tests/test_regime_learning.py.
    p_patcher = patch("profit.learning.REGIME_SPIKE_PROBABILITY", 0.0)
    p_patcher.start()
    # SIM PATCH PART 2 — install the simulated clock.
    sim_clock = _SimClock()
    time_patcher = patch("time.time", side_effect=sim_clock.now)
    time_patcher.start()
    try:
        yield sim_clock
    finally:
        time_patcher.stop()
        p_patcher.stop()
        random.setstate(saved)
        if np_saved_state is not None and _np is not None:
            _np.random.set_state(np_saved_state)


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
            with _deterministic_environment(self.seed) as sim_clock:
                return self._run_inner(
                    scenario, cycles, db_path, alloc_path, sim_clock,
                )
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
        sim_clock: _SimClock,
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

        # Real production modules — never mocked. The calibrator is
        # wrapped by the sim-only bootstrap shim so p_fill is non-zero
        # while FillModel is still training (prevents expected_capital
        # from collapsing to ~0). Production calibration is untouched;
        # the wrapper is a transparent pass-through once the model
        # trains. See simulation/bootstrap_calibrator.py.
        from .bootstrap_calibrator import make_sim_calibrator
        calibrator = make_sim_calibrator(db_path=db_path)
        learn_ctrl = LearningController(
            db_path=db_path, alloc_path=alloc_path,
        )

        metrics = MetricsTracker()
        learning_history: list[dict] = []
        per_cycle_violations: list[InvariantViolation] = []
        # SIM PATCH PART 8 — collected per-cycle sanity inputs
        sanity_fill_rates: list = []
        sanity_loss_per_capital: list = []
        sanity_cycle_losses: list = []
        # V3 per-cycle snapshots (expected capital, overcommit, tail risk…)
        v3_per_cycle: list[dict] = []

        cumulative_reward = 0.0
        cumulative_loss = 0.0

        for cycle in range(cycles):
            # SIM PATCH PART 2 — advance the simulated wall-clock BEFORE
            # the cycle runs, so every `time.time()` call inside step()
            # and allocate_portfolio sees the post-advance value.
            sim_clock.advance_to_cycle(cycle)
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
                    "capital_scale": applied.capital_scale,
                    "reward_trust": applied.reward_trust,
                    "lambda_1": applied.lambda_1,
                    "lambda_2": applied.lambda_2,
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
                "reward_trust": applied.reward_trust,
                "lambda_1": applied.lambda_1,
                "lambda_2": applied.lambda_2,
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

            # SIM PATCH PART 8 — collect inputs for post-run sanity
            sanity_fill_rates.append(fill_rate)
            lpc = (
                outcome.loss / outcome.capital_deployed
                if outcome.capital_deployed > 0 else None
            )
            sanity_loss_per_capital.append(lpc)
            sanity_cycle_losses.append(outcome.loss)

            # V3 per-cycle metrics
            v3_snap = compute_v3_cycle(
                outcome=outcome,
                total_capital=outcome.total_capital,
                applied_state=applied,
            )
            v3_per_cycle.append(v3_snap)

        post_run_violations = check_post_run(metrics, learning_history)
        sim_sanity_violations = check_sim_sanity(
            metrics_tracker=metrics,
            per_cycle_fill_rates=sanity_fill_rates,
            per_cycle_loss_per_capital=sanity_loss_per_capital,
            per_cycle_losses=sanity_cycle_losses,
        )

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
            sim_sanity_violations=sim_sanity_violations,
            v3_per_cycle=v3_per_cycle,
        )
