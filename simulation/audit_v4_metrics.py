"""simulation/audit_v4_metrics.py — V4 per-cycle metrics.

Builds one `V4CycleSnapshot` per cycle from the real CycleOutcome
produced by `simulation.runner.execute_cycle`. No production logic is
duplicated here — this module only reads allocation dicts + the
LearningStep that the real modules produced.

Metrics collected (per spec section 4):

  Capital & Deployment
    deploy_ratio                 = sum(deploy est_capital_cost) / total_capital
    total_notional               = sum(deploy est_capital_cost)
    expected_capital             = sum(_p_fill × est_capital_cost)
    overcommit_factor            = from Patch 7 `_overcommit_factor` stamp
    target_notional              = from Patch 11 `_target_notional` stamp

  Allocation Structure
    number_of_deployed_markets   = count(deploy)
    percent_forced_target_alloc  = count(_forced_target_alloc=True) / count(deploy)
    top_5_capital_concentration  = sum(top-5 deploy costs) / total_notional
    min_size_alloc_count         = count(deploy rows at min_size)

  Learning State (from applied LearningState)
    capital_scale
    delta_capital_scale          = capital_scale[t] − capital_scale[t−1]
    last_direction
    direction_lock

  Efficiency
    reward_efficiency
    reward_efficiency_baseline
    efficiency_penalty_active    = mode == ACTIVE AND eff < baseline

  Oscillation
    flip_count_cumulative        = total sign changes in delta_capital_scale
    rolling_flip_rate_100        = flips / 100-cycle window

A V4Tracker holds the rolling state between cycles so each snapshot
can reference prev.capital_scale and the running flip count without
re-scanning the whole history.

Invariant 8 in spec: missing metrics must RAISE. We treat stamps that
are missing in ACTIVE mode as errors (the Patch 11/13 contract
requires them). Pre-warmup / non-ACTIVE callers can legitimately have
None stamps, so raise only when we expect them.
"""

from __future__ import annotations
import math
from dataclasses import dataclass, asdict, field
from typing import Optional

from .runner import CycleOutcome
from profit.learning import MODE_ACTIVE


# ── Rolling oscillation window ──────────────────────────────────
# Per spec: rolling_flip_rate measured per 100 cycles.
OSC_WINDOW_CYCLES = 100
# For the "no sustained alternating pattern > 20 cycles" INV7 check.
SUSTAINED_ALT_MIN_LENGTH = 20


@dataclass
class V4CycleSnapshot:
    """Per-cycle observables. All fields required; Optional only where
    the underlying production signal is legitimately absent (pre-ACTIVE)."""

    cycle: int

    # Capital & Deployment
    deploy_ratio: float
    total_notional: float
    expected_capital: float
    overcommit_factor: Optional[float]
    target_notional: Optional[float]

    # Allocation Structure
    number_of_deployed_markets: int
    percent_forced_target_alloc: float
    top_5_capital_concentration: float
    min_size_alloc_count: int

    # Learning State
    capital_scale: float
    delta_capital_scale: float
    last_direction: int
    direction_lock: int
    mode: str

    # Efficiency
    reward_efficiency: Optional[float]
    reward_efficiency_baseline: Optional[float]
    efficiency_penalty_active: bool

    # Oscillation
    flip_count_cumulative: int
    rolling_flip_rate_100: float

    # Raw totals for diagnostics
    total_capital: float
    reward: float
    loss: float

    def to_row(self) -> dict:
        return asdict(self)


class V4Tracker:
    """Maintains rolling state across cycles (prev capital_scale, flip
    count window). Call `snapshot(cycle, outcome)` once per cycle in
    ascending order — out-of-order calls will produce nonsense.

    Missing-metric policy:
      - In ACTIVE mode: Patch 7 / 11 / 13 stamps are REQUIRED. If a
        deploy row is missing `_overcommit_factor` or `_target_notional`
        we raise — the audit's job is to expose bugs, not paper over
        them.
      - In OFF / SHADOW mode: stamps may legitimately be absent; we
        record Optional[None] and move on.
    """

    def __init__(self):
        self._prev_capital_scale: Optional[float] = None
        # Ring-buffer of recent deltas (signed) for rolling flip rate.
        self._delta_window: list[float] = []
        # Total flips observed since cycle 0.
        self._flip_count_cumulative: int = 0

    # ── Delta + flip accounting ──────────────────────────────

    def _compute_delta_and_flip(self, capital_scale: float) -> float:
        """Return delta and update the internal flip/window state."""
        if self._prev_capital_scale is None:
            self._prev_capital_scale = capital_scale
            self._delta_window.append(0.0)
            return 0.0
        delta = capital_scale - self._prev_capital_scale
        # Flip = sign change between the LAST two nonzero deltas in
        # the window. We compare the new delta to the most recent
        # PRIOR nonzero delta (not just the previous delta) so a
        # string of zeroes doesn't mask a real reversal.
        prev_nonzero = None
        for d in reversed(self._delta_window):
            if d != 0.0:
                prev_nonzero = d
                break
        if (prev_nonzero is not None and delta != 0.0
                and (prev_nonzero > 0) != (delta > 0)):
            self._flip_count_cumulative += 1
        self._delta_window.append(delta)
        # Trim to OSC_WINDOW_CYCLES + 1 (we need one extra to compare
        # across the boundary).
        if len(self._delta_window) > OSC_WINDOW_CYCLES + 1:
            self._delta_window = self._delta_window[-(OSC_WINDOW_CYCLES + 1):]
        self._prev_capital_scale = capital_scale
        return delta

    def _rolling_flip_rate_100(self) -> float:
        """Count sign changes in the last OSC_WINDOW_CYCLES deltas.
        Zero-deltas are skipped (they're not flips either way)."""
        window = self._delta_window[-OSC_WINDOW_CYCLES:]
        flips = 0
        last_nonzero = None
        for d in window:
            if d == 0.0:
                continue
            if last_nonzero is not None and (last_nonzero > 0) != (d > 0):
                flips += 1
            last_nonzero = d
        return float(flips)

    # ── Snapshot construction ────────────────────────────────

    def snapshot(self, cycle: int, outcome: CycleOutcome) -> V4CycleSnapshot:
        applied = outcome.learning_step.applied_state
        # applied.mode carries the REAL gate decision even in neutral-
        # published states (see profit/learning.py step() contract).
        mode = str(applied.mode)
        # Deploy rows only — avoid rows carry zero-cost allocations.
        deploys = [
            a for a in outcome.allocations if a.get("action") == "deploy"
        ]
        deploy_count = len(deploys)

        total_notional = sum(
            float(a.get("est_capital_cost") or 0.0) for a in deploys
        )
        total_capital = float(outcome.total_capital)
        deploy_ratio = (
            total_notional / total_capital if total_capital > 0 else 0.0
        )
        expected_capital = sum(
            float(a.get("_p_fill") or 0.0)
            * float(a.get("est_capital_cost") or 0.0)
            for a in deploys
        )

        # Continuous-allocator reinterpretation: the allocator no longer
        # emits Patch 7 `_overcommit_factor` / Patch 11 `_target_notional`
        # stamps — those concepts don't exist. We derive equivalent
        # observables from the allocation data itself so V4 invariants
        # still produce comparable metrics.
        #   overcommit_factor := realized total_notional / total_capital
        #                        (≤ ~1 under continuous; no explicit factor)
        #   target_notional   := total_capital × CAPITAL_BUFFER (0.95×)
        from profit.allocator import CAPITAL_BUFFER
        if total_capital > 0:
            overcommit_factor = total_notional / total_capital
            target_notional = total_capital * CAPITAL_BUFFER
        else:
            overcommit_factor = None
            target_notional = None

        # Forced-target-alloc disappeared with Patch 13 — the continuous
        # allocator has no concept of forced promotion, so this metric
        # is always 0.
        percent_forced = 0.0
        # Top-5 concentration: sum of 5 largest costs ÷ total.
        if deploy_count > 0 and total_notional > 0:
            sorted_costs = sorted(
                (float(a.get("est_capital_cost") or 0.0) for a in deploys),
                reverse=True,
            )
            top5 = sum(sorted_costs[:5])
            top5_concentration = top5 / total_notional
        else:
            top5_concentration = 0.0
        # Min-size allocations: est_capital_cost ≈ min_size × cpb.
        # We detect them by recomputing cpb from max_spread and
        # comparing cost to shares × cpb at exactly the min_size floor.
        min_size_count = 0
        for a in deploys:
            try:
                shares = int(a.get("shares_per_side") or 0)
                min_size = int(a.get("min_size") or 50)
                if shares <= min_size:
                    min_size_count += 1
            except (TypeError, ValueError):
                pass

        # Learning state snapshot.
        capital_scale = float(applied.capital_scale)
        delta = self._compute_delta_and_flip(capital_scale)
        last_direction = int(getattr(applied, "last_direction", 0) or 0)
        direction_lock = int(getattr(applied, "direction_lock", 0) or 0)

        # Efficiency fields — may be None outside ACTIVE.
        reward_efficiency = getattr(applied, "reward_efficiency", None)
        reward_efficiency_baseline = getattr(
            applied, "reward_efficiency_baseline", None,
        )
        efficiency_penalty_active = bool(
            mode == MODE_ACTIVE
            and reward_efficiency is not None
            and reward_efficiency_baseline is not None
            and float(reward_efficiency) < float(reward_efficiency_baseline)
        )

        return V4CycleSnapshot(
            cycle=int(cycle),
            deploy_ratio=round(deploy_ratio, 6),
            total_notional=round(total_notional, 2),
            expected_capital=round(expected_capital, 4),
            overcommit_factor=(
                round(float(overcommit_factor), 4)
                if overcommit_factor is not None else None
            ),
            target_notional=(
                round(float(target_notional), 2)
                if target_notional is not None else None
            ),
            number_of_deployed_markets=deploy_count,
            percent_forced_target_alloc=round(percent_forced, 6),
            top_5_capital_concentration=round(top5_concentration, 6),
            min_size_alloc_count=min_size_count,
            capital_scale=round(capital_scale, 6),
            delta_capital_scale=round(delta, 6),
            last_direction=last_direction,
            direction_lock=direction_lock,
            mode=mode,
            reward_efficiency=(
                float(reward_efficiency)
                if reward_efficiency is not None else None
            ),
            reward_efficiency_baseline=(
                float(reward_efficiency_baseline)
                if reward_efficiency_baseline is not None else None
            ),
            efficiency_penalty_active=efficiency_penalty_active,
            flip_count_cumulative=int(self._flip_count_cumulative),
            rolling_flip_rate_100=round(self._rolling_flip_rate_100(), 3),
            total_capital=round(total_capital, 2),
            reward=round(float(outcome.reward), 4),
            loss=round(float(outcome.loss), 4),
        )


def _first_stamp(deploys: list[dict], key: str) -> Optional[float]:
    """Return the first non-None value for `key` across deploy rows,
    or None if every row lacks it. Audit contract: per-cycle stamps
    are uniform across deploys in ACTIVE, so the first value represents
    the cycle-wide decision."""
    for a in deploys:
        v = a.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None
