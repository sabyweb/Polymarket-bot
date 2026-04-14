"""Hazard model — expected time on book before fill.

Empirical survival curve (Kaplan-Meier style, pure Python).
Segmented by queue depth. Recency-weighted: recent observations
count more than old ones (markets are non-stationary).
"""

import json
import logging
import math
import time

from oversight.data_collector import _connect_db

from .features import OrderFeatures, build_training_set

log = logging.getLogger("calibration.hazard_model")

MIN_SAMPLES = 100
DEFAULT_TIME_HOURS = 12.0  # conservative fallback
DECAY_HALF_LIFE_DAYS = 5.0  # recent observations weighted more

# Time bins in seconds
TIME_BINS = [
    (0, 300),       # 0-5min
    (300, 900),     # 5-15min
    (900, 1800),    # 15-30min
    (1800, 3600),   # 30min-1h
    (3600, 7200),   # 1-2h
    (7200, 14400),  # 2-4h
    (14400, 28800), # 4-8h
    (28800, 86400), # 8-24h
]

# Queue depth segments
DEPTH_FRONT = 50     # shares
DEPTH_MID = 200      # shares


def _segment_name(depth_ahead: float) -> str:
    if depth_ahead < DEPTH_FRONT:
        return "front"
    elif depth_ahead < DEPTH_MID:
        return "mid"
    else:
        return "back"


class HazardModel:
    """Empirical survival model for time-on-book estimation."""

    def __init__(self):
        # survival_curves[segment] = list of survival probabilities per bin
        self.survival_curves: dict[str, list[float]] = {}
        self.expected_times: dict[str, float] = {}  # segment → E[time] in hours
        self.global_expected: float = DEFAULT_TIME_HOURS
        self.trained_at: float = 0.0
        self.n_samples: int = 0
        self.metrics: dict = {}

    def train(self, db_path: str) -> dict:
        """Train survival curves from order lifetime data."""
        dataset = build_training_set(db_path)
        if len(dataset) < MIN_SAMPLES:
            log.info(f"HazardModel: insufficient data ({len(dataset)}, need {MIN_SAMPLES})")
            return {"status": "insufficient", "n": len(dataset)}

        # Segment by queue depth, with recency weights
        now = time.time()
        decay_rate = math.log(2) / (DECAY_HALF_LIFE_DAYS * 86400)
        segments: dict[str, list[tuple[float, str, float]]] = {
            "front": [], "mid": [], "back": [],
        }
        all_obs: list[tuple[float, str, float]] = []

        for feat in dataset:
            seg = _segment_name(feat.depth_ahead)
            age = max(0, now - feat.placed_ts)
            weight = math.exp(-decay_rate * age)
            obs = (feat.duration_secs, feat.outcome, weight)
            segments[seg].append(obs)
            all_obs.append(obs)

        # Compute survival curve for each segment
        for seg_name, obs_list in segments.items():
            if len(obs_list) < 20:
                continue
            curve = self._compute_survival(obs_list)
            self.survival_curves[seg_name] = curve
            self.expected_times[seg_name] = self._integrate_survival(curve) / 3600.0

        # Global curve
        if len(all_obs) >= MIN_SAMPLES:
            global_curve = self._compute_survival(all_obs)
            self.survival_curves["global"] = global_curve
            self.global_expected = self._integrate_survival(global_curve) / 3600.0

        self.trained_at = time.time()
        self.n_samples = len(dataset)

        self.metrics = {
            "n_samples": self.n_samples,
            "segments": {
                seg: {
                    "n": len(obs),
                    "e_time_hours": round(self.expected_times.get(seg, DEFAULT_TIME_HOURS), 2),
                }
                for seg, obs in segments.items()
            },
            "global_e_time_hours": round(self.global_expected, 2),
        }

        log.info(
            f"HazardModel trained: {self.n_samples} observations, "
            f"E[time]={self.global_expected:.1f}h global"
        )
        return {"status": "trained", **self.metrics}

    def _compute_survival(
        self, observations: list[tuple[float, str, float]],
    ) -> list[float]:
        """Compute weighted Kaplan-Meier survival curve over TIME_BINS.

        observations: list of (duration_secs, outcome, recency_weight).
        Recent observations weighted more heavily (non-stationary markets).

        Returns list of S(t) values, one per bin boundary.
        """
        n_bins = len(TIME_BINS)
        survival = [1.0] * (n_bins + 1)

        at_risk = sum(w for _, _, w in observations)
        if at_risk <= 0:
            return survival

        for i, (t_start, t_end) in enumerate(TIME_BINS):
            fills_weight = 0.0
            exit_weight = 0.0

            for dur, outcome, w in observations:
                if dur < t_start:
                    continue
                if dur < t_end:
                    if outcome == "filled":
                        fills_weight += w
                    exit_weight += w

            if at_risk <= 0:
                survival[i + 1] = survival[i]
            else:
                hazard = fills_weight / at_risk
                survival[i + 1] = survival[i] * (1.0 - hazard)

            at_risk -= exit_weight
            at_risk = max(at_risk, 0)

        return survival

    def _integrate_survival(self, survival: list[float]) -> float:
        """Integrate survival curve to get expected time in seconds.

        E[T] = integral of S(t) dt, approximated by bin widths.
        """
        total = 0.0
        for i, (t_start, t_end) in enumerate(TIME_BINS):
            bin_width = t_end - t_start
            # Trapezoidal rule
            s_avg = (survival[i] + survival[i + 1]) / 2.0
            total += s_avg * bin_width

        return total

    def predict(self, depth_ahead: float = 0.0) -> float:
        """Predict E[time on book] in hours.

        Uses segment-specific curve if available, else global, else fallback.
        """
        if self.n_samples < MIN_SAMPLES:
            return DEFAULT_TIME_HOURS

        seg = _segment_name(depth_ahead)
        if seg in self.expected_times:
            return self.expected_times[seg]
        elif "global" in self.survival_curves:
            return self.global_expected
        else:
            return DEFAULT_TIME_HOURS

    def get_survival_probability(self, hours: float,
                                 depth_ahead: float = 0.0) -> float:
        """P(still on book after `hours` hours)."""
        if self.n_samples < MIN_SAMPLES:
            return 0.5  # uncertain fallback

        seg = _segment_name(depth_ahead)
        curve = self.survival_curves.get(seg) or self.survival_curves.get("global")
        if not curve:
            return 0.5

        target_secs = hours * 3600.0
        for i, (t_start, t_end) in enumerate(TIME_BINS):
            if target_secs <= t_end:
                # Linear interpolation within bin
                frac = (target_secs - t_start) / (t_end - t_start)
                return curve[i] * (1 - frac) + curve[i + 1] * frac
        return curve[-1]

    def is_ready(self) -> bool:
        return self.n_samples >= MIN_SAMPLES

    def save(self, db_path: str):
        try:
            db = _connect_db(db_path)
            db.execute(
                "INSERT OR REPLACE INTO calibration_model_state "
                "(model_name, weights_json, trained_at, n_samples, n_positive, "
                "metrics_json, feature_names) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "hazard_model",
                    json.dumps({
                        "survival_curves": self.survival_curves,
                        "expected_times": self.expected_times,
                        "global_expected": self.global_expected,
                    }),
                    self.trained_at,
                    self.n_samples,
                    0,
                    json.dumps(self.metrics),
                    json.dumps(["depth_ahead"]),
                ),
            )
            db.commit()
            db.close()
        except Exception as e:
            log.warning(f"HazardModel save failed: {e}")

    def load(self, db_path: str) -> bool:
        try:
            db = _connect_db(db_path)
            row = db.execute(
                "SELECT weights_json, trained_at, n_samples, metrics_json "
                "FROM calibration_model_state WHERE model_name = 'hazard_model'",
            ).fetchone()
            db.close()
            if not row:
                return False
            data = json.loads(row[0])
            self.survival_curves = data["survival_curves"]
            self.expected_times = data["expected_times"]
            self.global_expected = data["global_expected"]
            self.trained_at = row[1]
            self.n_samples = row[2]
            self.metrics = json.loads(row[3])
            return True
        except Exception as e:
            log.warning(f"HazardModel load failed: {e}")
            return False
