"""simulation/audit_v4_scenarios.py — V4 audit scenario definitions.

Audit V4 tests INV3 / INV5 / INV7 under six deterministic scenarios:

    A (balanced)           — control; all invariants should PASS
    B (under_deployed)     — INV5 stress
    C (over_aggressive)    — INV3 stress
    D (regime_shift_3phase)— INV7 stress; three-phase rotation
    E (efficiency_collapse)— Part 4 / INV5 penalty trigger (mid-run step down)
    F (saturation_edge)    — INV3 stress; all markets in Patch 10's
                              low_ev_override zone, forcing Patch 13's
                              upsize-existing path to close the target

A, B, C reuse the base signal profiles from `simulation/market_env.py`
unchanged — we only wrap them so the V4 audit runner can route all six
through a single interface. D, E, F have V4-specific phase logic that
does not exist in the base MarketEnvironment.

Determinism: same (scenario, seed, cycle) → identical MarketSignals.
The base MarketEnvironment's per-environment RNG is preserved; any
extra state added here (phase tracking) is a pure function of cycle.
"""

from __future__ import annotations
import random
from dataclasses import dataclass

from .market_env import MarketEnvironment, MarketSignals, _BASE


# ── V4 SCENARIO IDENTIFIERS ─────────────────────────────────────
AUDIT_V4_SCENARIOS = (
    "balanced",                 # A
    "under_deployed",           # B  (reuses existing name)
    "over_aggressive",          # C  (reuses existing name)
    "regime_shift_3phase",      # D  (NEW — 3 phases instead of 2)
    "efficiency_collapse",      # E  (NEW)
    "saturation_edge",          # F  (NEW)
)


# ── V4-SPECIFIC BASE PROFILES ───────────────────────────────────
# Each entry is a dict compatible with MarketEnvironment._jitter's
# expected keys: p_fill, loss_per_fill, advertised_daily_rate,
# reward_rate, volatility.

# E (Efficiency Collapse) — phase 1 has moderate rewards; phase 2
# drops rewards sharply while keeping advertised rate high. The
# LearningController's 7-day-median baseline persists through phase 2's
# drop, so `reward_efficiency < baseline` triggers the Patch 13 Part 4
# penalty. The split is at 40% of total_cycles so ≥ 7 sim-days of phase
# 1 data anchor the baseline before the drop.
_E_PHASE_1 = {
    "p_fill": 0.12,
    "loss_per_fill": 0.20,
    "advertised_daily_rate": 1.50,
    "reward_rate": 0.15,
    "volatility": 0.02,
}
_E_PHASE_2 = {
    "p_fill": 0.12,
    "loss_per_fill": 0.20,
    "advertised_daily_rate": 1.50,       # unchanged → allocator keeps deploying
    "reward_rate": 0.020,                # 7.5× drop → eff collapses vs baseline
    "volatility": 0.02,
}

# F (Saturation Edge Case) — engineered so per-market raw EV sits in the
# Patch 10 low_ev_override band: NEGATIVE_EV_TOLERANCE (−0.02) ≤ ev <
# PATCH10_MIN_EV_THRESHOLD × LOW_EV_ALLOWANCE_FACTOR (0.05). Concretely:
#   predicted_reward ≈ advertised_daily_rate × q_share/100 ≈ 0.030
#   p_fill × loss_per_fill ≈ 0.035
#   raw_ev ≈ −0.005 per market → within tolerance, triggers
#   _low_ev_override=True on every market → no avoids for Patch 10's
#   forced-exposure block to promote → Patch 13 target-driven upsize
#   is the ONLY mechanism that can close the overcommit target.
_F_BASE = {
    "p_fill": 0.07,
    "loss_per_fill": 0.50,
    "advertised_daily_rate": 0.30,
    "reward_rate": 0.030,
    "volatility": 0.025,
}

# D Phase 1 / 2 / 3 — "low fill, high reward" then "high fill, low
# reward" then revert to phase 1. Exercises per-regime frontier memory
# + hysteresis under three distinct capital_scale pressures.
_D_PHASE_1 = {
    "p_fill": 0.08,
    "loss_per_fill": 0.20,
    "advertised_daily_rate": 1.20,
    "reward_rate": 0.12,
    "volatility": 0.02,
}
_D_PHASE_2 = {
    "p_fill": 0.45,
    "loss_per_fill": 1.60,
    "advertised_daily_rate": 0.40,
    "reward_rate": 0.030,
    "volatility": 0.04,
}
_D_PHASE_3 = _D_PHASE_1


def _jitter(rng: random.Random, base: dict, regime_tag: str,
            frac: float = 0.10) -> MarketSignals:
    """Same ±10% multiplicative jitter MarketEnvironment uses, but
    takes an explicit RNG so AuditV4Environment can drive its own
    deterministic sequence without touching the base class's state."""
    def _j(x: float) -> float:
        return float(x) * (1.0 + rng.uniform(-frac, frac))

    return MarketSignals(
        p_fill=max(0.0, min(1.0, _j(base["p_fill"]))),
        loss_per_fill=max(0.0, _j(base["loss_per_fill"])),
        reward_rate=max(0.0, _j(base["reward_rate"])),
        advertised_daily_rate=max(0.0, _j(base["advertised_daily_rate"])),
        volatility=max(0.0, _j(base["volatility"])),
        regime_tag=regime_tag,
    )


class AuditV4Environment:
    """Deterministic per-cycle MarketSignals for the six V4 scenarios.

    A / B / C delegate to the unchanged base MarketEnvironment so we
    inherit its baseline + jitter exactly (same profile used by the
    V3.1 audit). D / E / F use phase-aware base profiles defined in
    this module.

    Determinism contract:
        For every (scenario, seed, cycle), `signals_for(cycle)`
        returns identical MarketSignals on repeat calls — no hidden
        state between cycles. The only per-call mutable state is the
        local `random.Random` instance, which is seeded once at
        construction and consumed in a deterministic order.
    """

    def __init__(self, scenario: str, seed: int, total_cycles: int):
        if scenario not in AUDIT_V4_SCENARIOS:
            raise ValueError(
                f"unknown V4 scenario {scenario!r}; "
                f"expected one of {AUDIT_V4_SCENARIOS}"
            )
        self.scenario = scenario
        self.total_cycles = int(total_cycles)
        self._rng = random.Random(seed)
        # A / B / C wrap the base MarketEnvironment with its own RNG
        # (so jitter sequences match the base audit exactly). D uses a
        # phase-aware override; E / F have their own phase tables.
        if scenario == "balanced":
            self._base = MarketEnvironment(
                scenario="stable_optimal",
                seed=seed,
                total_cycles=total_cycles,
            )
        elif scenario == "under_deployed":
            self._base = MarketEnvironment(
                scenario="under_deployed",
                seed=seed,
                total_cycles=total_cycles,
            )
        elif scenario == "over_aggressive":
            self._base = MarketEnvironment(
                scenario="over_aggressive",
                seed=seed,
                total_cycles=total_cycles,
            )
        else:
            # D / E / F — V4-specific. No base-MarketEnvironment reuse.
            self._base = None

    # ── Phase boundaries scaled to the run length ──────────────

    def _d_phase_boundaries(self) -> tuple[int, int]:
        """D splits 0-150 / 150-300 / 300-end at a 500-cycle baseline.
        Scale proportionally so shorter/longer runs still exercise all
        three phases."""
        n = self.total_cycles
        p1_end = int(round(n * 150 / 500))
        p2_end = int(round(n * 300 / 500))
        return p1_end, p2_end

    def _e_phase_boundary(self) -> int:
        """E drops rewards at 40% into the run — leaves ≥ 7 sim-days
        of baseline anchor before the drop, and ≥ 7 sim-days of
        post-drop penalty firing."""
        return int(round(self.total_cycles * 0.40))

    # ── Dispatch ────────────────────────────────────────────────

    def signals_for(self, cycle: int) -> MarketSignals:
        if self._base is not None:
            return self._base.signals_for(cycle)

        if self.scenario == "regime_shift_3phase":
            p1_end, p2_end = self._d_phase_boundaries()
            if cycle < p1_end:
                return _jitter(self._rng, _D_PHASE_1, "d_phase_1")
            if cycle < p2_end:
                return _jitter(self._rng, _D_PHASE_2, "d_phase_2")
            return _jitter(self._rng, _D_PHASE_3, "d_phase_3")

        if self.scenario == "efficiency_collapse":
            boundary = self._e_phase_boundary()
            if cycle < boundary:
                return _jitter(self._rng, _E_PHASE_1, "e_phase_1")
            return _jitter(self._rng, _E_PHASE_2, "e_phase_2")

        if self.scenario == "saturation_edge":
            return _jitter(self._rng, _F_BASE, "f_saturation")

        raise RuntimeError(  # unreachable
            f"AuditV4Environment: no handler for {self.scenario!r}"
        )
