"""simulation/metrics.py — per-cycle metric recorder + trend analysis.

Captures one CycleMetric record per cycle and exposes:

    track(record)
    history()                  -> list[CycleMetric]
    series(field)              -> list[float]
    rolling_trend_slope(field) -> float (linear regression slope)
    cumulative_reward / cumulative_loss
    drawdown                   -> max peak-to-trough on cumulative net_profit
    is_oscillating(field, window, threshold) -> bool

These are the inputs the audit's PASS/FAIL evaluator consumes.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class CycleMetric:
    cycle: int
    capital_deployed: float
    reward: float
    loss: float
    net_profit: float
    reward_efficiency: Optional[float]   # None when capital_deployed == 0
    fill_rate: Optional[float]           # None when no orders this cycle
    learning_state: dict = field(default_factory=dict)
    mode: str = "OFF"
    regime_id: Optional[tuple] = None
    exploration_pct: float = 0.05
    total_ev: float = 0.0
    total_capital_budget: float = 0.0
    cluster_max_pct: float = 0.0


class MetricsTracker:
    """Append-only time series of CycleMetric entries."""

    def __init__(self):
        self._records: list[CycleMetric] = []

    # ── recording ──────────────────────────────────────────────

    def track(self, record: CycleMetric) -> None:
        self._records.append(record)

    def history(self) -> list[CycleMetric]:
        return list(self._records)

    # ── series helpers ─────────────────────────────────────────

    def series(self, field_name: str) -> list[float]:
        """Return the numeric time series for `field_name`. None values
        are skipped — callers must handle empty results."""
        out: list[float] = []
        for r in self._records:
            v = getattr(r, field_name, None)
            if v is None:
                continue
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
        return out

    # ── trend analysis ─────────────────────────────────────────

    def rolling_trend_slope(
        self,
        field_name: str,
        window: int = 50,
        offset_from_end: int = 0,
    ) -> Optional[float]:
        """Linear regression slope (y = a*x + b) of `field_name` over the
        last `window` entries (skipping `offset_from_end` from the end).
        Returns None if fewer than 5 valid points exist."""
        ys = self.series(field_name)
        if offset_from_end > 0:
            ys = ys[:-offset_from_end] if offset_from_end < len(ys) else []
        ys = ys[-window:]
        if len(ys) < 5:
            return None
        n = len(ys)
        xs = list(range(n))
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        den = sum((x - mean_x) ** 2 for x in xs)
        if den == 0:
            return 0.0
        return num / den

    def cumulative(self, field_name: str) -> float:
        return sum(self.series(field_name))

    def drawdown(self) -> float:
        """Max peak-to-trough decline of cumulative net_profit (always
        non-negative)."""
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in self._records:
            cum += float(r.net_profit or 0.0)
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
        return max_dd

    def is_oscillating(
        self,
        field_name: str,
        window: int = 20,
        rel_amplitude_threshold: float = 0.30,
    ) -> bool:
        """True when (max - min) / mean over the last `window` exceeds
        `rel_amplitude_threshold`. Returns False if the window is empty
        or mean is zero (can't define relative amplitude)."""
        ys = self.series(field_name)[-window:]
        if len(ys) < window:
            return False
        mn, mx = min(ys), max(ys)
        mean = sum(ys) / len(ys)
        if mean == 0:
            return False
        return (mx - mn) / abs(mean) > rel_amplitude_threshold

    def divergence_to_bounds_count(
        self,
        field_name: str,
        lo: float,
        hi: float,
        eps: float = 1e-6,
    ) -> int:
        """Count cycles where the field hugged either clamp boundary."""
        n = 0
        for r in self._records:
            v = getattr(r, field_name, None)
            if v is None:
                continue
            if abs(float(v) - lo) < eps or abs(float(v) - hi) < eps:
                n += 1
        return n

    def n_cycles(self) -> int:
        return len(self._records)
