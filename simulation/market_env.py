"""simulation/market_env.py — synthetic market signal generator.

Produces deterministic-but-realistic per-cycle market parameters across
five adversarial scenarios. Each cycle returns:

    {
        "p_fill": float,
        "loss_per_fill": float,
        "reward_rate": float,        # actual paid reward per market per cycle
        "advertised_daily_rate": float,  # what calibrator sees pre-attribution
        "volatility": float,
    }

The "high_reward_fake" scenario produces advertised >> actual to drive
reward_error < 0.7 in the learning loop. The "regime_shift" scenario
swaps mid-run to verify per-regime frontier separation.

All randomness is sourced from a `random.Random(seed)` instance owned by
the engine — never from `random` module globals.
"""

from __future__ import annotations
from dataclasses import dataclass
import random
from typing import Optional


SCENARIOS = (
    "over_aggressive",
    "under_deployed",
    "high_reward_fake",
    "regime_shift",
    "stable_optimal",
)


@dataclass
class MarketSignals:
    """Per-cycle exogenous signals that drive simulated outcomes."""
    p_fill: float
    loss_per_fill: float            # $ loss per filled order (already negative-PnL)
    reward_rate: float              # actual per-market reward this cycle
    advertised_daily_rate: float    # what the market broadcasts (pre-attribution)
    volatility: float               # noise scale for fill / unwind prices
    # Regime tag visible to the engine for synthetic regime_id construction.
    # Two distinct strings during regime_shift; constant for other scenarios.
    regime_tag: str


# Base parameters per scenario.
#
# Unit convention (CRITICAL — the runner reads these expecting it):
#   advertised_daily_rate : per-day reward rate the calibrator sees
#                           (input to predicted_reward in the alloc file).
#                           Predicted reward across our 8 deploy markets
#                           per cycle = 8 × advertised × q_share/100.
#   reward_rate           : per-cycle actual reward paid per market.
#                           Runner OVERWRITES today's attribution row
#                           each cycle (not accumulating) so the metric
#                           engine's actual_reward_24h reflects this
#                           cycle's payout, on the same time-scale as
#                           predicted_reward.
#
# Therefore:  actual / predicted  =  reward_rate / (advertised × 0.10)
#
# Per-scenario intent for the reward_error ratio:
#   stable_optimal   :  1.0     (calibrator is honest)
#   over_aggressive  :  1.0     (rewards honest, but fills hurt)
#   under_deployed   :  1.0     (small but consistent)
#   high_reward_fake :  0.20    (advertised = 5 × actual → trust ↓)
#   regime_shift     :  per-half (stable_optimal → over_aggressive)
_BASE: dict[str, dict] = {
    "stable_optimal": {
        "p_fill": 0.10,
        "loss_per_fill": 0.20,
        # 0.80 × 0.10 = 0.080
        "advertised_daily_rate": 0.80,
        "reward_rate": 0.080,
        "volatility": 0.02,
    },
    "over_aggressive": {
        "p_fill": 0.55,
        "loss_per_fill": 2.20,
        # 0.50 × 0.10 = 0.050
        "advertised_daily_rate": 0.50,
        "reward_rate": 0.050,
        "volatility": 0.05,
    },
    "under_deployed": {
        "p_fill": 0.04,
        "loss_per_fill": 0.10,
        # 0.10 × 0.10 = 0.010
        "advertised_daily_rate": 0.10,
        "reward_rate": 0.010,
        "volatility": 0.01,
    },
    "high_reward_fake": {
        "p_fill": 0.20,
        "loss_per_fill": 0.50,
        # Honest reward_rate would be 2.00 × 0.10 = 0.200; we pay 0.20×
        # of that → 0.040. So actual/predicted ≈ 0.20 < 0.7 → TRUST_DOWN.
        "advertised_daily_rate": 2.00,
        "reward_rate": 0.040,
        "volatility": 0.03,
    },
}


class MarketEnvironment:
    """Generates per-cycle MarketSignals for a chosen scenario.

    Deterministic given the same seed and cycle index — re-runs of an
    identical (seed, scenario, total_cycles) triple produce identical
    sequences.
    """

    def __init__(
        self,
        scenario: str,
        seed: int,
        total_cycles: int,
    ):
        if scenario not in SCENARIOS:
            raise ValueError(
                f"unknown scenario {scenario!r}; expected one of {SCENARIOS}"
            )
        self.scenario = scenario
        self.total_cycles = int(total_cycles)
        # Per-environment RNG: never touch `random` module globals.
        self._rng = random.Random(seed)

    def signals_for(self, cycle: int) -> MarketSignals:
        """Return the signals for the given 0-indexed cycle."""
        if self.scenario == "regime_shift":
            return self._regime_shift_signals(cycle)
        base = _BASE[self.scenario]
        regime_tag = self.scenario
        return self._jitter(base, regime_tag)

    def _regime_shift_signals(self, cycle: int) -> MarketSignals:
        """Half-and-half: stable_optimal then over_aggressive.

        The shift point is `total_cycles // 2`; before it we use
        stable_optimal, after it we use over_aggressive. The regime_tag
        toggles so the engine builds a different regime_id for each half,
        which exercises Patch 5's per-regime frontier memory.
        """
        midpoint = self.total_cycles // 2
        if cycle < midpoint:
            base = _BASE["stable_optimal"]
            regime_tag = "regime_shift_a"
        else:
            base = _BASE["over_aggressive"]
            regime_tag = "regime_shift_b"
        return self._jitter(base, regime_tag)

    def _jitter(self, base: dict, regime_tag: str) -> MarketSignals:
        """Add small deterministic noise so cycle outcomes are not
        bit-identical, but the seeded RNG keeps each (seed, cycle) pair
        reproducible."""
        # +/- 10% multiplicative noise on each scalar
        def _j(x: float, frac: float = 0.10) -> float:
            return float(x) * (1.0 + self._rng.uniform(-frac, frac))

        return MarketSignals(
            p_fill=max(0.0, min(1.0, _j(base["p_fill"]))),
            loss_per_fill=max(0.0, _j(base["loss_per_fill"])),
            reward_rate=max(0.0, _j(base["reward_rate"])),
            advertised_daily_rate=max(0.0, _j(base["advertised_daily_rate"])),
            volatility=max(0.0, _j(base["volatility"])),
            regime_tag=regime_tag,
        )
