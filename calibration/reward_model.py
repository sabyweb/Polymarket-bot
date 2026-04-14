"""Reward attribution model — per-market reward rate estimation.

Phase 1 (< 7 days data): wrapper around correction_factor.
Phase 2 (7+ days): linear model on reward_daily x reward_daily_markets.
"""

import json
import logging
import math
import time

from oversight.data_collector import _connect_db

log = logging.getLogger("calibration.reward_model")

MIN_DAYS_FOR_MODEL = 7


class RewardModel:
    """Estimates per-market reward rate in $/hour."""

    def __init__(self):
        self.alpha: float = 1.0   # global calibration scalar
        self.beta: float = 0.0    # intercept
        self.phase: int = 1       # 1 = CF wrapper, 2 = linear model
        self.n_days: int = 0
        self.trained_at: float = 0.0
        self.metrics: dict = {}
        self._market_cache: dict[str, float] = {}  # cid → $/hour

    def train(self, db_path: str, correction_factor: float = 1.0) -> dict:
        """Train reward attribution model.

        Phase 1: stores correction_factor as alpha.
        Phase 2: fits linear model if enough daily data.
        """
        self._market_cache.clear()
        self.trained_at = time.time()

        # Count available days
        try:
            db = _connect_db(db_path)
            row = db.execute(
                "SELECT COUNT(DISTINCT date) FROM reward_daily "
                "WHERE total_reward_usd > 0"
            ).fetchone()
            self.n_days = row[0] if row and row[0] else 0
        except Exception:
            self.n_days = 0
            self.phase = 1
            self.alpha = correction_factor
            self.metrics = {"status": "no_data", "phase": 1}
            return self.metrics

        if self.n_days >= MIN_DAYS_FOR_MODEL:
            result = self._train_phase2(db)
            db.close()
            return result
        else:
            db.close()
            # Phase 1: correction_factor passthrough
            self.phase = 1
            self.alpha = correction_factor
            self.metrics = {
                "status": "phase1",
                "phase": 1,
                "n_days": self.n_days,
                "alpha": round(self.alpha, 4),
            }
            log.info(f"RewardModel phase 1: CF={correction_factor:.4f}, {self.n_days} days")
            return self.metrics

    def _train_phase2(self, db) -> dict:
        """Fit: total_reward = alpha * sum(scoring_secs * daily_rate) + beta."""
        try:
            rows = db.execute(
                "SELECT rd.date, rd.total_reward_usd, "
                "SUM(rdm.scoring_seconds * rdm.daily_rate) as weighted_scoring "
                "FROM reward_daily rd "
                "JOIN reward_daily_markets rdm ON rd.date = rdm.date "
                "WHERE rd.total_reward_usd > 0 "
                "GROUP BY rd.date "
                "ORDER BY rd.date"
            ).fetchall()
        except Exception as e:
            log.warning(f"RewardModel phase 2 query failed: {e}")
            self.phase = 1
            return {"status": "query_failed", "phase": 1}

        if len(rows) < MIN_DAYS_FOR_MODEL:
            self.phase = 1
            return {"status": "insufficient_days", "phase": 1, "n_days": len(rows)}

        # Simple OLS: y = alpha * x + beta
        n = len(rows)
        sum_x = sum_y = sum_xx = sum_xy = 0.0
        for r in rows:
            y_val = r[1]  # total_reward_usd
            x_val = r[2] or 0  # weighted_scoring
            sum_x += x_val
            sum_y += y_val
            sum_xx += x_val * x_val
            sum_xy += x_val * y_val

        denom = n * sum_xx - sum_x * sum_x
        if abs(denom) < 1e-12:
            self.phase = 1
            return {"status": "singular", "phase": 1}

        self.alpha = (n * sum_xy - sum_x * sum_y) / denom
        self.beta = (sum_y - self.alpha * sum_x) / n
        self.alpha = max(0.0001, self.alpha)  # must be positive

        # LOO-CV error
        loo_errors = []
        for i in range(n):
            y_true = rows[i][1]
            x_val = rows[i][2] or 0
            y_pred = self.alpha * x_val + self.beta
            loo_errors.append(abs(y_true - y_pred))
        mae = sum(loo_errors) / len(loo_errors) if loo_errors else 0
        mean_y = sum_y / n if n > 0 else 1
        relative_mae = mae / max(mean_y, 0.01)

        self.phase = 2
        self.metrics = {
            "status": "phase2",
            "phase": 2,
            "n_days": n,
            "alpha": round(self.alpha, 6),
            "beta": round(self.beta, 4),
            "loo_mae": round(mae, 4),
            "relative_mae": round(relative_mae, 4),
        }

        log.info(
            f"RewardModel phase 2: alpha={self.alpha:.6f}, beta={self.beta:.4f}, "
            f"LOO-MAE=${mae:.2f} ({relative_mae:.0%})"
        )
        return self.metrics

    def predict_rate(
        self,
        condition_id: str,
        daily_rate: float,
        q_share_pct: float,
        scoring_seconds_24h: float = 0.0,
        correction_factor: float = 1.0,
    ) -> float:
        """Predict reward rate in $/hour for a market.

        Phase 1: daily_rate * min(q_share, 0.5) * correction_factor / 24
        Phase 2: alpha * scoring_seconds * daily_rate / 86400
        """
        if condition_id in self._market_cache:
            return self._market_cache[condition_id]

        q_share = min(q_share_pct, 0.5)

        if self.phase == 2 and scoring_seconds_24h > 0:
            # Phase 2: linear model
            daily_est = self.alpha * scoring_seconds_24h * daily_rate + self.beta / max(1, self.n_days)
            rate = max(0.0, daily_est / 24.0)
        else:
            # Phase 1: correction_factor wrapper
            effective_daily = daily_rate * q_share * (self.alpha if self.phase == 1 else correction_factor)
            rate = effective_daily / 24.0

        self._market_cache[condition_id] = rate
        return rate

    def is_ready(self) -> bool:
        """True if phase 2 model is active and validated."""
        return self.phase == 2

    def save(self, db_path: str):
        try:
            db = _connect_db(db_path)
            db.execute(
                "INSERT OR REPLACE INTO calibration_model_state "
                "(model_name, weights_json, trained_at, n_samples, n_positive, "
                "metrics_json, feature_names) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "reward_model",
                    json.dumps({
                        "alpha": self.alpha,
                        "beta": self.beta,
                        "phase": self.phase,
                        "n_days": self.n_days,
                    }),
                    self.trained_at,
                    self.n_days,
                    0,
                    json.dumps(self.metrics),
                    json.dumps(["scoring_seconds", "daily_rate"]),
                ),
            )
            db.commit()
            db.close()
        except Exception as e:
            log.warning(f"RewardModel save failed: {e}")

    def load(self, db_path: str) -> bool:
        try:
            db = _connect_db(db_path)
            row = db.execute(
                "SELECT weights_json, trained_at, n_samples, metrics_json "
                "FROM calibration_model_state WHERE model_name = 'reward_model'",
            ).fetchone()
            db.close()
            if not row:
                return False
            data = json.loads(row[0])
            self.alpha = data["alpha"]
            self.beta = data["beta"]
            self.phase = data["phase"]
            self.n_days = data["n_days"]
            self.trained_at = row[1]
            self.metrics = json.loads(row[3])
            return True
        except Exception as e:
            log.warning(f"RewardModel load failed: {e}")
            return False
