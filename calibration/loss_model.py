"""Loss model — E[loss | fill] in $/share.

Recency-weighted rolling average segmented by slippage bucket.
When 100+ samples available, fits OLS on (slippage, spread, fill_size).
"""

import json
import logging
import math
import time

from oversight.data_collector import _connect_db

from .features import FillLossFeatures, build_loss_training_set

log = logging.getLogger("calibration.loss_model")

MIN_SAMPLES = 20
MIN_SAMPLES_OLS = 100
DEFAULT_LOSS_PER_SHARE = 0.02  # $0.02 conservative default
TAIL_RISK_MULTIPLIER = 1.5    # E_loss = mean + 1.5 * std (tail-aware)

# Slippage bucket boundaries
ADVERSE_THRESHOLD = 0.01
FAVORABLE_THRESHOLD = -0.01


class LossModel:
    """Estimates expected loss per share given a fill."""

    def __init__(self):
        # Bucket averages: adverse, neutral, favorable
        self.bucket_avg: dict[str, float] = {
            "adverse": DEFAULT_LOSS_PER_SHARE,
            "neutral": DEFAULT_LOSS_PER_SHARE,
            "favorable": DEFAULT_LOSS_PER_SHARE / 2,
        }
        self.bucket_std: dict[str, float] = {
            "adverse": 0.0, "neutral": 0.0, "favorable": 0.0,
        }
        self.bucket_counts: dict[str, int] = {
            "adverse": 0, "neutral": 0, "favorable": 0,
        }
        # OLS coefficients (slippage, spread, fill_size, intercept)
        self.ols_weights: list[float] | None = None
        self.global_avg: float = DEFAULT_LOSS_PER_SHARE
        self.global_std: float = 0.0
        self.trained_at: float = 0.0
        self.n_samples: int = 0
        self.metrics: dict = {}

    def train(self, db_path: str) -> dict:
        """Train from DB data."""
        dataset = build_loss_training_set(db_path)
        if len(dataset) < MIN_SAMPLES:
            log.info(
                f"LossModel: insufficient data ({len(dataset)} pairs, need {MIN_SAMPLES})"
            )
            return {"status": "insufficient", "n": len(dataset)}

        # Compute weighted bucket averages
        buckets: dict[str, tuple[float, float]] = {
            "adverse": (0.0, 0.0),
            "neutral": (0.0, 0.0),
            "favorable": (0.0, 0.0),
        }

        total_weighted_loss = 0.0
        total_weight = 0.0

        for r in dataset:
            w = r.recency_weight
            loss = r.loss_per_share

            if r.slippage > ADVERSE_THRESHOLD:
                bucket = "adverse"
            elif r.slippage < FAVORABLE_THRESHOLD:
                bucket = "favorable"
            else:
                bucket = "neutral"

            prev = buckets[bucket]
            buckets[bucket] = (prev[0] + loss * w, prev[1] + w)
            total_weighted_loss += loss * w
            total_weight += w

        for b in buckets:
            wsum, wcount = buckets[b]
            if wcount > 0:
                self.bucket_avg[b] = wsum / wcount
                self.bucket_counts[b] = int(wcount)
            else:
                self.bucket_avg[b] = DEFAULT_LOSS_PER_SHARE

        self.global_avg = total_weighted_loss / total_weight if total_weight > 0 else DEFAULT_LOSS_PER_SHARE
        self.n_samples = len(dataset)
        self.trained_at = time.time()

        # Compute std per bucket for tail-risk adjustment
        bucket_sq: dict[str, tuple[float, float]] = {
            "adverse": (0.0, 0.0), "neutral": (0.0, 0.0), "favorable": (0.0, 0.0),
        }
        global_sq_sum = 0.0
        for r in dataset:
            w = r.recency_weight
            loss = r.loss_per_share
            if r.slippage > ADVERSE_THRESHOLD:
                bucket = "adverse"
            elif r.slippage < FAVORABLE_THRESHOLD:
                bucket = "favorable"
            else:
                bucket = "neutral"
            mean = self.bucket_avg[bucket]
            prev = bucket_sq[bucket]
            bucket_sq[bucket] = (prev[0] + (loss - mean) ** 2 * w, prev[1] + w)
            global_sq_sum += (loss - self.global_avg) ** 2 * w

        for b in bucket_sq:
            sq_sum, sq_w = bucket_sq[b]
            self.bucket_std[b] = math.sqrt(sq_sum / sq_w) if sq_w > 0 else 0.0
        self.global_std = math.sqrt(global_sq_sum / total_weight) if total_weight > 0 else 0.0

        # Try OLS if enough data
        if len(dataset) >= MIN_SAMPLES_OLS:
            self.ols_weights = self._fit_ols(dataset)

        self.metrics = {
            "n_samples": self.n_samples,
            "global_avg_loss": round(self.global_avg, 6),
            "global_std_loss": round(self.global_std, 6),
            "global_tail_loss": round(self.global_avg + TAIL_RISK_MULTIPLIER * self.global_std, 6),
            "bucket_adverse": round(self.bucket_avg["adverse"], 6),
            "bucket_neutral": round(self.bucket_avg["neutral"], 6),
            "bucket_favorable": round(self.bucket_avg["favorable"], 6),
            "has_ols": self.ols_weights is not None,
        }

        log.info(
            f"LossModel trained: {self.n_samples} pairs, "
            f"global_avg=${self.global_avg:.4f}/share, "
            f"OLS={'yes' if self.ols_weights else 'no'}"
        )
        return {"status": "trained", **self.metrics}

    def _fit_ols(self, dataset: list[FillLossFeatures]) -> list[float] | None:
        """Fit OLS: loss = w0*slippage + w1*spread + w2*fill_size + w3.

        Closed-form solution: w = (X^T X)^{-1} X^T y.
        """
        n = len(dataset)
        # Build X (n x 4) and y (n x 1)
        X = []
        y = []
        for r in dataset:
            X.append([r.slippage, r.spread_at_fill, r.fill_size_shares, 1.0])
            y.append(r.loss_per_share)

        d = 4
        # X^T X (d x d)
        XtX = [[0.0] * d for _ in range(d)]
        # X^T y (d x 1)
        Xty = [0.0] * d

        for i in range(n):
            for j in range(d):
                Xty[j] += X[i][j] * y[i]
                for k in range(d):
                    XtX[j][k] += X[i][j] * X[i][k]

        # Add ridge penalty for numerical stability
        ridge = 1e-6
        for j in range(d):
            XtX[j][j] += ridge

        # Solve via Gaussian elimination
        try:
            w = self._solve_linear(XtX, Xty)
            return w
        except Exception:
            return None

    @staticmethod
    def _solve_linear(A: list[list[float]], b: list[float]) -> list[float]:
        """Solve Ax = b via Gaussian elimination with partial pivoting."""
        n = len(b)
        # Augmented matrix
        M = [A[i][:] + [b[i]] for i in range(n)]

        for col in range(n):
            # Partial pivot
            max_row = col
            for row in range(col + 1, n):
                if abs(M[row][col]) > abs(M[max_row][col]):
                    max_row = row
            M[col], M[max_row] = M[max_row], M[col]

            if abs(M[col][col]) < 1e-12:
                raise ValueError("Singular matrix")

            # Eliminate below
            for row in range(col + 1, n):
                factor = M[row][col] / M[col][col]
                for j in range(col, n + 1):
                    M[row][j] -= factor * M[col][j]

        # Back substitution
        x = [0.0] * n
        for i in range(n - 1, -1, -1):
            x[i] = M[i][n]
            for j in range(i + 1, n):
                x[i] -= M[i][j] * x[j]
            x[i] /= M[i][i]

        return x

    def predict(self, slippage: float = 0.0, spread: float = 0.045,
                fill_size: float = 50.0, tail_aware: bool = True) -> float:
        """Predict E[loss per share | fill].

        When tail_aware=True (default), returns mean + 1.5*std to account
        for fat-tailed loss distributions. This prevents the system from
        looking profitable until a tail event.

        Returns dollar loss per share (>= 0).
        """
        if self.n_samples < MIN_SAMPLES:
            return DEFAULT_LOSS_PER_SHARE

        # Use OLS if available (already captures variance via regression)
        if self.ols_weights is not None:
            pred = (self.ols_weights[0] * slippage
                    + self.ols_weights[1] * spread
                    + self.ols_weights[2] * fill_size
                    + self.ols_weights[3])
            if tail_aware:
                pred += TAIL_RISK_MULTIPLIER * self.global_std
            return max(0.0, pred)

        # Bucket average + tail adjustment
        if slippage > ADVERSE_THRESHOLD:
            bucket = "adverse"
        elif slippage < FAVORABLE_THRESHOLD:
            bucket = "favorable"
        else:
            bucket = "neutral"

        base = self.bucket_avg[bucket]
        if tail_aware:
            base += TAIL_RISK_MULTIPLIER * self.bucket_std.get(bucket, 0.0)
        return max(0.0, base)

    def predict_total_loss(self, shares: float = 50.0,
                           slippage: float = 0.0,
                           spread: float = 0.045) -> float:
        """Predict total $ loss for a fill of given size."""
        return self.predict(slippage, spread, shares) * shares

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
                    "loss_model",
                    json.dumps({
                        "bucket_avg": self.bucket_avg,
                        "bucket_std": self.bucket_std,
                        "bucket_counts": self.bucket_counts,
                        "global_avg": self.global_avg,
                        "global_std": self.global_std,
                        "ols_weights": self.ols_weights,
                    }),
                    self.trained_at,
                    self.n_samples,
                    0,
                    json.dumps(self.metrics),
                    json.dumps(["slippage", "spread", "fill_size"]),
                ),
            )
            db.commit()
            db.close()
        except Exception as e:
            log.warning(f"LossModel save failed: {e}")

    def load(self, db_path: str) -> bool:
        try:
            db = _connect_db(db_path)
            row = db.execute(
                "SELECT weights_json, trained_at, n_samples, metrics_json "
                "FROM calibration_model_state WHERE model_name = 'loss_model'",
            ).fetchone()
            db.close()
            if not row:
                return False
            data = json.loads(row[0])
            self.bucket_avg = data["bucket_avg"]
            self.bucket_std = data.get("bucket_std", {"adverse": 0, "neutral": 0, "favorable": 0})
            self.bucket_counts = data.get("bucket_counts", {})
            self.global_avg = data["global_avg"]
            self.global_std = data.get("global_std", 0.0)
            self.ols_weights = data.get("ols_weights")
            self.trained_at = row[1]
            self.n_samples = row[2]
            self.metrics = json.loads(row[3])
            return True
        except Exception as e:
            log.warning(f"LossModel load failed: {e}")
            return False
