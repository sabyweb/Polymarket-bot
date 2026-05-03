"""simulation/runner.py — single-cycle execution helper.

Wires the production modules into a deterministic per-cycle pipeline:

    1. Read prior LearningStep state -> applied_state (real LearningController)
    2. Build a synthetic ScoredMarket list from MarketSignals
    3. Set calibrator.reward_trust = applied_state.reward_trust
    4. allocate_portfolio(...) (real allocator)
    5. Write market_allocations.json (so next cycle's metrics see it)
    6. Simulate fill outcomes (seeded Bernoulli per market)
    7. Insert rows into fills, unwinds, orders_placed, reward_attribution,
       reward_daily so the LearningController has fresh data to read

NO production logic is duplicated here. We only generate inputs and
persist outputs into the same DB schema the production code reads.
"""

from __future__ import annotations
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from oversight.market_scorer import ScoredMarket
from profit.allocator import allocate_portfolio
from profit.learning import LearningController, LearningStep, LearningState

from .market_env import MarketSignals


@dataclass
class CycleOutcome:
    """Per-cycle outputs needed by metrics + invariants."""
    cycle: int
    learning_step: LearningStep
    allocations: list[dict]
    total_capital: float
    total_ev: float
    exploration_pct: float
    reward: float
    loss: float
    capital_deployed: float
    fill_count: int
    order_count: int


# Number of synthetic markets per cycle. Raised from 8 → 30 so the
# allocator has room to deploy past the 8×$200 = $1600 ceiling that
# was binding the v2 audit's deploy_ratio (SIM PATCH PART 4).
N_SYNTHETIC_MARKETS = 30

# Per-side share count for synthetic deploy markets. Drives fill cost
# (shares × clob_cost) and unwind revenue (shares × sell_price).
SHARES_PER_SIDE = 30

# Total deployable capital used by every cycle of the audit. Kept at
# $2000 so per_market_cap = min($200, budget × 0.15) = $200 remains
# the binding per-market limit. 30 markets × $200 = $6000 possible,
# so the 75%/85% utilisation targets become physically reachable.
TOTAL_CAPITAL = 2000.0


def _build_scored_markets(
    signals: MarketSignals,
    rng,
) -> list[ScoredMarket]:
    """Generate `N_SYNTHETIC_MARKETS` ScoredMarkets with signals
    consistent with the scenario. Question groups spread across a few
    buckets so the allocator's per-group cap is exercised.

    SIM PATCH PART 4 — per-market diversity. Each market index gets a
    STABLE deterministic offset on q_share, spread, and daily_rate so
    markets are not identical clones. Offsets are index-derived (no
    RNG dependency) so the sequence is reproducible across runs."""
    out: list[ScoredMarket] = []
    n_groups = 5
    n = max(1, N_SYNTHETIC_MARKETS)
    for i in range(N_SYNTHETIC_MARKETS):
        cid = f"SIM_{signals.regime_tag}_{i}"
        # Per-market stable jitter in [-1, +1] space, evenly spaced.
        j = (i / max(1, n - 1)) * 2.0 - 1.0  # -1 .. +1
        q_share_i = max(0.5, 10.0 * (1.0 + 0.30 * j))          # 7–13%
        spread_i = max(0.005, 0.045 * (1.0 + 0.20 * j))        # 3.6–5.4%
        rate_i = max(0.0, signals.advertised_daily_rate
                     * (1.0 + 0.15 * j))                       # ±15%
        sm = ScoredMarket(
            condition_id=cid,
            question=f"Synthetic {signals.regime_tag} {i}",
            score=1.0 + rng.uniform(-0.05, 0.05),
            action="deploy",
            recommended_shares=SHARES_PER_SIDE,
            reason="simulator",
            confidence="high",
            actual_reward_total=0.0,
            fill_damage=0.0,
            fill_count=0,
            daily_rate=rate_i,
            min_size=float(SHARES_PER_SIDE),
            max_spread=spread_i,
            est_capital_cost=0.0,
            locked_position_usd=0.0,
            question_group=f"grp_{i % n_groups}",
            q_share_pct=q_share_i,
            end_date_iso="",
        )
        out.append(sm)
    return out


def _now_ts(cycle: int) -> float:
    """Per-cycle timestamp. Returns current (patched) `time.time()`
    plus a 1-second offset.

    The engine patches `time.time()` to `SIM_EPOCH + cycle * 3600`
    (one simulated hour per cycle). Without the +1 offset, the next
    cycle's 1h cutoff = `SIM_EPOCH + k*3600 - 3600` equals exactly
    the prior cycle's ts, and production's `ts > cutoff` strict-greater
    excludes it — `global_fill_rate_1h` would then come back None and
    block `_metrics_complete`.

    With +1s, the prior cycle's ts = SIM_EPOCH + (k-1)*3600 + 1 is
    strictly greater than the next cycle's 1h cutoff, so it's captured;
    the 24h window similarly retains the expected ~24 cycles of fills.
    """
    return time.time() + 1.0


# 1 cycle == 1 simulated hour. Cycles 0..23 fall on day 0; 24..47 on
# day 1; and so on. This makes the LearningGate's `reward_days >= 3`
# check reachable within the audit horizon (200 cycles ≈ 8 days).
SIM_HOURS_PER_CYCLE = 1


def _sim_date_for_cycle(cycle: int) -> str:
    """Return YYYY-MM-DD for the cycle, treating the simulation as
    running 1 hour per cycle starting from a fixed virtual epoch."""
    sim_day = cycle // (24 // SIM_HOURS_PER_CYCLE)
    # Offset off a fixed simulated start so all dates are valid YYYY-MM-DD.
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sim_dt = epoch + timedelta(days=sim_day)
    return sim_dt.strftime("%Y-%m-%d")


def _today_str() -> str:
    """Real today's date — used by reward_attribution INSERT, which is
    queried by LearningMetrics with today+yesterday for actual_reward_24h."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _insert_outcomes(
    db_path: str,
    cycle: int,
    allocations: list[dict],
    signals: MarketSignals,
    rng,
) -> tuple[float, float, int, int]:
    """Insert orders_placed / fills / unwinds / reward_attribution for
    the cycle. Returns (reward, loss, fill_count, order_count).

    SIM PATCH PART 1 — at start of cycle, DELETE the per-cycle tables
    so metrics computed from them reflect ONLY this cycle's behaviour.
    Without this, fills/unwinds/orders accumulate over 200 cycles and
    loss_per_capital / fill_rate are read against cumulative totals
    vs. a per-cycle capital_deployed — a time-scale mismatch that
    masked Rule E's SAFE expansion gate.

    PRESERVED (not deleted): reward_daily (historical, feeds gate's
    reward_days counter) and learning_state (controller persistence).
    """
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    # ── SIM PATCH PART 1: 24h WINDOWING (see engine._time_simulator) ──
    # We do NOT delete fills/unwinds/orders_placed here. Instead, the
    # engine patches `time.time()` to advance 1 simulated hour per cycle
    # (SIM PATCH PART 2's CYCLE_DURATION_HOURS). Rows are inserted with
    # ts == that simulated wall-clock, so production's 24h-window query
    # `ts > time.time() - 86400` naturally captures only the last 24
    # cycles. Lifetime `COUNT(*) FROM fills` still sees every row, so
    # LearningGate's `fills_total >= 100` trigger progresses as before.
    #
    # reward_attribution is date-keyed (today/yesterday) and the UPSERT
    # already overwrites today's per-market row each cycle — no DELETE
    # needed; the per-cycle per-market value is what actual_reward_24h
    # reads on the next step() call.
    now = _now_ts(cycle)
    today = _today_str()
    # Simulated calendar date for reward_daily — drives the gate's
    # `reward_days >= 3` check across the audit horizon.
    sim_date = _sim_date_for_cycle(cycle)

    total_reward = 0.0
    total_loss = 0.0
    fill_count = 0
    order_count = 0

    for a in allocations:
        if a.get("action") != "deploy":
            continue
        cid = a.get("condition_id")
        shares = int(a.get("shares_per_side") or 0)
        if shares <= 0:
            continue
        order_count += 2  # one BUY per side
        # Two orders per market (yes + no side), placed at a midpoint-ish
        # price. We use 0.50 +/- spread/2 as a stand-in.
        for side, price in (("yes", 0.50), ("no", 0.50)):
            db.execute(
                "INSERT INTO orders_placed (ts, condition_id, side, price, "
                "size, order_type) VALUES (?, ?, ?, ?, ?, 'BUY')",
                (now, cid, side, price, shares),
            )

        # Bernoulli fill outcome per side using p_fill from signals.
        for side in ("yes", "no"):
            if rng.random() >= signals.p_fill:
                continue
            fill_count += 1
            fill_price = 0.50 + rng.uniform(
                -signals.volatility, signals.volatility,
            )
            fill_price = max(0.01, min(0.99, fill_price))
            usd_value = shares * fill_price
            db.execute(
                "INSERT INTO fills (ts, condition_id, side, fill_type, "
                "shares, price, clob_cost, usd_value, midpoint, slippage) "
                "VALUES (?, ?, ?, 'FULL', ?, ?, ?, ?, ?, ?)",
                (now, cid, side, shares, fill_price, fill_price,
                 usd_value, 0.50, fill_price - 0.50),
            )
            # Simulate the unwind. loss_per_fill defines the dollar gap
            # between fill cost and unwind revenue — we materialise it
            # by lowering sell_price proportionally.
            loss_per_fill = signals.loss_per_fill * (
                1.0 + rng.uniform(-0.1, 0.1)
            )
            sell_price = fill_price - (loss_per_fill / max(1.0, shares))
            sell_price = max(0.01, min(0.99, sell_price))
            unwind_value = shares * sell_price
            pnl = unwind_value - usd_value
            db.execute(
                "INSERT INTO unwinds (ts, condition_id, side, shares, "
                "sell_price, usd_value, vwap_cost, pnl) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now, cid, side, shares, sell_price, unwind_value,
                 usd_value, pnl),
            )
            total_loss += max(0.0, usd_value - unwind_value)

        # Reward attribution per market for THIS cycle. We OVERWRITE
        # today's row (not accumulate) so actual_reward_24h read by the
        # metrics engine reflects only this cycle's payout — same time
        # scale as predicted_reward (alloc file is also per-cycle). This
        # makes reward_error a clean per-cycle ratio.
        per_market_reward = signals.reward_rate * (
            1.0 + rng.uniform(-0.05, 0.05)
        )
        db.execute(
            "INSERT INTO reward_attribution (market_id, date, reward_usd) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(market_id, date) DO UPDATE SET "
            "reward_usd = excluded.reward_usd",
            (cid, today, per_market_reward),
        )
        total_reward += per_market_reward

    # reward_daily — used by Patch 3's reward_growth and the gate's
    # reward_days count. Also used by profit/efficiency.get_efficiency
    # which requires est_daily_total > 0 to lift eff_scale above the
    # 0.30 cold-start floor — we set it to today's deployed capital
    # (a reasonable proxy for "what the system bet on this day").
    capital_today = sum(
        float(a.get("est_capital_cost") or 0.0)
        for a in allocations if a.get("action") == "deploy"
    )
    db.execute(
        "INSERT INTO reward_daily "
        "(date, total_combined_usd, total_reward_usd, est_daily_total, "
        "num_markets_active) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(date) DO UPDATE SET "
        "total_combined_usd = total_combined_usd + excluded.total_combined_usd, "
        "total_reward_usd   = total_reward_usd   + excluded.total_reward_usd, "
        "est_daily_total    = MAX(est_daily_total, excluded.est_daily_total), "
        "num_markets_active = MAX(num_markets_active, excluded.num_markets_active)",
        (sim_date, total_reward, total_reward, capital_today, len(allocations)),
    )
    db.commit()
    db.close()
    return total_reward, total_loss, fill_count, order_count


def _write_alloc_file(alloc_path: str, allocations: list[dict]) -> None:
    """Atomic JSON write used by LearningMetrics._read_alloc_file.

    Key matches the production writer (oversight/allocation_writer.py:275)
    so the reader (profit/learning.py:_read_alloc_file) sees the same shape
    in simulation as in production.
    """
    payload = {"markets": allocations}
    with open(alloc_path, "w") as f:
        json.dump(payload, f)


def execute_cycle(
    cycle: int,
    db_path: str,
    alloc_path: str,
    calibrator,
    learn_ctrl: LearningController,
    signals: MarketSignals,
    market_rng,
    fill_rng,
    total_capital: float = TOTAL_CAPITAL,
) -> CycleOutcome:
    """Run one full cycle and return the outcome.

    The LearningController.step() is called BEFORE allocation so the
    applied_state reflects what the production loop would see. After
    allocation, simulated fills/unwinds/rewards are inserted into the DB
    so the next cycle's step() reads them as fresh metrics.
    """
    # 1. Real LearningController step using PREVIOUS cycle's persisted DB
    learn_step: LearningStep = learn_ctrl.step()
    applied = learn_step.applied_state

    # 2. Build scored markets for this cycle
    markets = _build_scored_markets(signals, market_rng)

    # 3. Forward reward_trust into the real CalibrationManager
    calibrator.reward_trust = float(applied.reward_trust)

    # 4. Real allocate_portfolio
    allocations = allocate_portfolio(
        scored_markets=markets,
        total_capital=total_capital,
        calibrator=calibrator,
        db_path=db_path,
        learning_state=applied,
    )

    # 5. Persist alloc JSON for the NEXT step()'s metrics read.
    _write_alloc_file(alloc_path, allocations)

    # 6+7. Simulate outcomes and insert rows.
    reward, loss, fill_count, order_count = _insert_outcomes(
        db_path, cycle, allocations, signals, fill_rng,
    )

    capital_deployed = sum(
        float(a.get("est_capital_cost") or 0.0)
        for a in allocations if a.get("action") == "deploy"
    )
    total_ev = sum(
        float(a.get("_ev_per_day") or 0.0)
        for a in allocations if a.get("action") == "deploy"
    )
    # The actual exploration pct used this cycle is stamped on every
    # deploy allocation by the allocator (single value per cycle).
    exploration_pct = 0.05
    for a in allocations:
        v = a.get("_exploration_pct")
        if v is not None:
            exploration_pct = float(v)
            break

    return CycleOutcome(
        cycle=cycle,
        learning_step=learn_step,
        allocations=allocations,
        total_capital=total_capital,
        total_ev=total_ev,
        exploration_pct=exploration_pct,
        reward=reward,
        loss=loss,
        capital_deployed=capital_deployed,
        fill_count=fill_count,
        order_count=order_count,
    )
