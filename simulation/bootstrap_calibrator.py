"""simulation/bootstrap_calibrator.py — sim-only p_fill bootstrap fix.

Problem: while `FillModel.is_ready() == False`, `CalibrationManager.
get_predictions()` returns `p_fill_24h = 0.0` on sim cycles that have
no prior book state. The allocator floors this to 1e-4, which drives
`expected_capital = Σ(p·C)` to ~0 and invalidates the utilisation
invariants V4 INV3 and V5 INV3_new.

Fix (strict scope): a simulation-side wrapper around `CalibrationManager`
that substitutes a deterministic, bounded, state-dependent `p_fill_24h`
on the returned predictions whenever the fill model is not ready. The
production `CalibrationManager`, the allocator, the learning loop, and
the audit logic are all untouched.

Contract per the simulation-bootstrap fix spec:
    §5.1  p_fill > 0
    §5.2  0.02 ≤ p_fill ≤ 0.15
    §5.3  deterministic — same inputs → same output
    §5.4  depends on at least one existing market variable
    §7    fallback = 0.05 when required inputs are missing
    §6    simple scaling only — no new models / heuristics

When `FillModel.is_ready()` flips to True (trained), the wrapper becomes
a no-op pass-through so steady-state sim cycles see production semantics.
"""

from __future__ import annotations
from dataclasses import replace
from typing import Optional

from calibration.manager import CalibrationManager, CalibrationPredictions


# Spec §5.2 — bounds on the sim-bootstrap p_fill substitution.
SIM_P_FILL_MIN = 0.02
SIM_P_FILL_MAX = 0.15
# Spec §7 — fallback when the required inputs are missing / invalid.
SIM_P_FILL_DEFAULT = 0.05

# Scaling coefficients (simple linear shaping — spec §6 bans heuristics).
# Chosen so the reachable output on the sim's typical input ranges
# (daily_rate ≈ 5–30, q_share_pct ≈ 5–15) sits inside [SIM_P_FILL_MIN,
# SIM_P_FILL_MAX] with headroom on both ends.
_BASE = 0.03
_COEF_DAILY_RATE = 0.001   # +0.001 per $/day of advertised rate
_COEF_Q_SHARE = 0.004      # +0.004 per % of our Q-share pool


def sim_fallback_p_fill(
    daily_rate: Optional[float],
    q_share_pct: Optional[float],
) -> float:
    """Deterministic, bounded, state-dependent p_fill for sim bootstrap.

    The two inputs are both market variables already available on every
    `get_predictions` call: `daily_rate` (the market's advertised reward
    pool size) and `q_share_pct` (our expected share of that pool).
    Both vary per-market and per-cycle in the sim, so the function is
    genuinely state-dependent (spec §5.4).

    Returns SIM_P_FILL_DEFAULT (= 0.05) when either input is missing,
    non-numeric, or negative — that's the spec §7 fallback path."""
    if daily_rate is None or q_share_pct is None:
        return SIM_P_FILL_DEFAULT
    try:
        dr = float(daily_rate)
        qs = float(q_share_pct)
    except (TypeError, ValueError):
        return SIM_P_FILL_DEFAULT
    if dr < 0 or qs < 0:
        return SIM_P_FILL_DEFAULT
    p = _BASE + _COEF_DAILY_RATE * dr + _COEF_Q_SHARE * qs
    # Clamp to the spec §5.2 band.
    return max(SIM_P_FILL_MIN, min(SIM_P_FILL_MAX, p))


class SimBootstrapCalibrator:
    """Wraps a `CalibrationManager` so the sim bootstrap path produces
    a non-zero `p_fill_24h`. All other attributes pass through unchanged.

    Once the underlying model trains (`fill_model.is_ready() == True`),
    this wrapper becomes a transparent pass-through — no substitution
    occurs, and production semantics resume for steady-state cycles.

    This class lives entirely inside `simulation/` and is never imported
    by the production code path."""

    __slots__ = ("_real",)

    def __init__(self, real: CalibrationManager):
        object.__setattr__(self, "_real", real)

    # ── Intercept the one method that needs substitution. ─────────

    def get_predictions(
        self, condition_id: str, **kwargs,
    ) -> CalibrationPredictions:
        preds = self._real.get_predictions(condition_id, **kwargs)
        # When the fill model has trained, production fallback already
        # produces a meaningful number — no need to touch it.
        if self._real.fill_model.is_ready():
            return preds
        new_p = sim_fallback_p_fill(
            daily_rate=kwargs.get("daily_rate"),
            q_share_pct=kwargs.get("q_share_pct"),
        )
        # CalibrationPredictions is a frozen-shape @dataclass; replace
        # swaps p_fill_24h while preserving every other field (reward,
        # loss, confidence, model_versions, raw_reward_per_day, ...).
        return replace(preds, p_fill_24h=new_p)

    # ── Forward all other attribute access to the real calibrator. ─
    #
    # The sim runner sets `calibrator.reward_trust = ...` each cycle,
    # and the V4/V5 audits read `calibrator.fill_model` / `is_ready()`.
    # __getattr__ covers reads; __setattr__ covers writes. __init__ uses
    # `object.__setattr__` to bootstrap `_real` without recursion.

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    def __setattr__(self, name: str, value) -> None:
        setattr(self._real, name, value)


def make_sim_calibrator(db_path: str) -> SimBootstrapCalibrator:
    """Factory used by audit runners. Returns a wrapped calibrator that
    behaves identically to `CalibrationManager` except during
    bootstrap, where `p_fill_24h` is substituted with
    `sim_fallback_p_fill(daily_rate, q_share_pct)`."""
    return SimBootstrapCalibrator(CalibrationManager(db_path=db_path))
