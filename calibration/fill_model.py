"""Fill probability model — P(fill within T hours).

Pure-Python logistic regression with L2 regularization.
Global model pooled across all markets. No market-specific params.
"""

import json
import logging
import math
import time

from oversight.data_collector import _connect_db

from .features import (
    FEATURE_NAMES, NUM_FEATURES,
    OrderFeatures, build_training_set, features_to_vector,
)

log = logging.getLogger("calibration.fill_model")

# Activation thresholds
MIN_SAMPLES = 50
MIN_POSITIVES = 15

# Training hyperparameters
LEARNING_RATE = 0.01
L2_LAMBDA = 0.01
MAX_EPOCHS = 200
CONVERGENCE_TOL = 1e-6


def _sigmoid(z: float) -> float:
    """Numerically stable sigmoid."""
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _standardize(
    X: list[list[float]],
) -> tuple[list[list[float]], list[float], list[float]]:
    """Standardize features to zero mean, unit variance.

    Returns (X_standardized, means, stds).
    """
    n = len(X)
    if n == 0:
        return X, [0.0] * NUM_FEATURES, [1.0] * NUM_FEATURES

    d = len(X[0])
    means = [0.0] * d
    for row in X:
        for j in range(d):
            means[j] += row[j]
    means = [m / n for m in means]

    stds = [0.0] * d
    for row in X:
        for j in range(d):
            stds[j] += (row[j] - means[j]) ** 2
    stds = [max(math.sqrt(s / n), 1e-8) for s in stds]

    X_std = []
    for row in X:
        X_std.append([(row[j] - means[j]) / stds[j] for j in range(d)])

    return X_std, means, stds


class FillModel:
    """Logistic regression for P(fill within 24h)."""

    def __init__(self):
        self.weights: list[float] = [0.0] * NUM_FEATURES
        self.bias: float = 0.0
        self.means: list[float] = [0.0] * NUM_FEATURES
        self.stds: list[float] = [1.0] * NUM_FEATURES
        self.trained_at: float = 0.0
        self.n_samples: int = 0
        self.n_positive: int = 0
        self.metrics: dict = {}

    def train(self, db_path: str) -> dict:
        """Train from DB data. Returns training metrics dict."""
        dataset = build_training_set(db_path)
        if not dataset:
            log.info("FillModel: no training data")
            return {"status": "no_data"}

        # Build X, y from labeled data
        X_raw: list[list[float]] = []
        y: list[float] = []
        for feat in dataset:
            if feat.outcome == "alive":
                continue  # exclude right-censored
            X_raw.append(features_to_vector(feat))
            y.append(1.0 if feat.outcome == "filled" else 0.0)

        n = len(y)
        n_pos = sum(1 for v in y if v > 0.5)

        if n < MIN_SAMPLES or n_pos < MIN_POSITIVES:
            log.info(
                f"FillModel: insufficient data ({n} samples, {n_pos} positive, "
                f"need {MIN_SAMPLES}/{MIN_POSITIVES})"
            )
            return {"status": "insufficient", "n": n, "n_pos": n_pos}

        # Standardize
        X, means, stds = _standardize(X_raw)
        d = NUM_FEATURES

        # Initialize
        w = [0.0] * d
        b = 0.0

        # Mini-batch gradient descent
        prev_loss = float("inf")
        for epoch in range(MAX_EPOCHS):
            total_loss = 0.0
            grad_w = [0.0] * d
            grad_b = 0.0

            for i in range(n):
                z = sum(w[j] * X[i][j] for j in range(d)) + b
                p = _sigmoid(z)
                err = p - y[i]

                for j in range(d):
                    grad_w[j] += err * X[i][j]
                grad_b += err

                # Log loss
                p_clamp = max(1e-7, min(1 - 1e-7, p))
                total_loss -= y[i] * math.log(p_clamp) + (1 - y[i]) * math.log(1 - p_clamp)

            # Add L2 penalty
            total_loss += 0.5 * L2_LAMBDA * sum(wj * wj for wj in w)

            # Update
            for j in range(d):
                w[j] -= LEARNING_RATE * (grad_w[j] / n + L2_LAMBDA * w[j])
            b -= LEARNING_RATE * (grad_b / n)

            avg_loss = total_loss / n
            if abs(prev_loss - avg_loss) < CONVERGENCE_TOL:
                break
            prev_loss = avg_loss

        self.weights = w
        self.bias = b
        self.means = means
        self.stds = stds
        self.trained_at = time.time()
        self.n_samples = n
        self.n_positive = n_pos

        # Compute metrics on training set
        correct = 0
        for i in range(n):
            z = sum(w[j] * X[i][j] for j in range(d)) + b
            pred = 1.0 if _sigmoid(z) > 0.5 else 0.0
            if pred == y[i]:
                correct += 1

        accuracy = correct / n if n > 0 else 0
        base_rate = n_pos / n if n > 0 else 0

        self.metrics = {
            "accuracy": round(accuracy, 4),
            "base_rate": round(base_rate, 4),
            "n_samples": n,
            "n_positive": n_pos,
            "epochs": epoch + 1,
            "final_loss": round(avg_loss, 6),
        }

        log.info(
            f"FillModel trained: {n} samples, {n_pos} positive, "
            f"accuracy={accuracy:.2%}, base_rate={base_rate:.2%}"
        )
        return {"status": "trained", **self.metrics}

    def predict(self, feat: OrderFeatures) -> float:
        """Predict P(fill within 24h) for a single order."""
        if self.n_samples < MIN_SAMPLES:
            return -1.0  # signal: use fallback

        vec = features_to_vector(feat)
        # Standardize using training means/stds
        x = [(vec[j] - self.means[j]) / self.stds[j] for j in range(NUM_FEATURES)]
        z = sum(self.weights[j] * x[j] for j in range(NUM_FEATURES)) + self.bias
        return _sigmoid(z)

    def predict_from_book(
        self,
        spread: float,
        midpoint: float,
        depth_ahead: float,
        total_same_depth: float,
        opposite_depth_5c: float,
        daily_rate: float,
        agent_shares: float,
        order_price: float,
        was_scoring: int = 1,
    ) -> float:
        """Predict P(fill) from raw book features without full OrderFeatures."""
        feat = OrderFeatures(
            condition_id="", side="", order_id="", placed_ts=0,
            order_price=order_price, midpoint=midpoint,
            distance_from_mid=abs(order_price - midpoint),
            spread=spread, depth_ahead=depth_ahead,
            total_same_depth=total_same_depth,
            opposite_depth_5c=opposite_depth_5c,
            daily_rate=daily_rate, agent_shares=agent_shares,
            was_scoring=was_scoring,
            rel_depth_ahead=depth_ahead / max(agent_shares, 1),
            rel_spread=spread / max(midpoint, 0.01),
            log_daily_rate=math.log(1 + max(daily_rate, 0)),
        )
        return self.predict(feat)

    def is_ready(self) -> bool:
        return self.n_samples >= MIN_SAMPLES and self.n_positive >= MIN_POSITIVES

    def get_reliability_score(self) -> float:
        """0.0 (not ready) to 1.0 (mature, high quality)."""
        if not self.is_ready():
            return 0.0
        size_score = min(1.0, self.n_samples / 250)
        epoch_frac = self.metrics.get("epochs", MAX_EPOCHS) / MAX_EPOCHS
        convergence = 1.0 if epoch_frac < 0.8 else 0.85
        br = self.metrics.get("base_rate", 0.5)
        balance = min(br, 1 - br) / 0.5
        balance_factor = 0.7 + 0.3 * balance
        return min(1.0, size_score * convergence * balance_factor)

    def save(self, db_path: str):
        """Persist model weights to calibration_model_state."""
        try:
            db = _connect_db(db_path)
            db.execute(
                "INSERT OR REPLACE INTO calibration_model_state "
                "(model_name, weights_json, trained_at, n_samples, n_positive, "
                "metrics_json, feature_names) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "fill_model",
                    json.dumps({
                        "weights": self.weights,
                        "bias": self.bias,
                        "means": self.means,
                        "stds": self.stds,
                    }),
                    self.trained_at,
                    self.n_samples,
                    self.n_positive,
                    json.dumps(self.metrics),
                    json.dumps(FEATURE_NAMES),
                ),
            )
            db.commit()
            db.close()
        except Exception as e:
            log.warning(f"FillModel save failed: {e}")

    def load(self, db_path: str) -> bool:
        """Load model weights from DB. Returns True if loaded."""
        try:
            db = _connect_db(db_path)
            row = db.execute(
                "SELECT weights_json, trained_at, n_samples, n_positive, metrics_json "
                "FROM calibration_model_state WHERE model_name = 'fill_model'",
            ).fetchone()
            db.close()
            if not row:
                return False
            data = json.loads(row[0])
            self.weights = data["weights"]
            self.bias = data["bias"]
            self.means = data["means"]
            self.stds = data["stds"]
            self.trained_at = row[1]
            self.n_samples = row[2]
            self.n_positive = row[3]
            self.metrics = json.loads(row[4])
            return True
        except Exception as e:
            log.warning(f"FillModel load failed: {e}")
            return False
