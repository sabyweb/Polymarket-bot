"""Calibration Manager — orchestrates all models, exposes unified predictions.

Called by oversight_agent.py before scoring. Retrains models from DB data,
produces CalibrationPredictions per market, computes EV.
"""

import logging
import math
import time
from dataclasses import dataclass

from oversight.data_collector import _connect_db

from .fill_model import FillModel
from .loss_model import LossModel
from .hazard_model import HazardModel
from .reward_model import RewardModel
from .features import OrderFeatures, NUM_FEATURES

log = logging.getLogger("calibration.manager")

# Retrain cooldown: don't retrain if last train was < 30 min ago
RETRAIN_COOLDOWN_SECS = 1800

# Fix 4: Safety bias — cap reward term at 80% of naive estimate
# Reward attribution is the noisiest model; never trust it fully
REWARD_SAFETY_BIAS = 0.80

# Fix 5: Minimum EV to deploy ($/day) — prevents deploying marginal markets
MIN_EV_THRESHOLD = 0.10

# Fix 6: Uncertainty penalty — scale EV by confidence
# Early models are wrong; discount their outputs
UNCERTAINTY_FLOOR = 0.3  # minimum confidence multiplier


@dataclass
class CalibrationPredictions:
    """Unified predictions for a single market."""
    condition_id: str
    p_fill_24h: float             # P(fill within 24h)
    e_loss_given_fill: float      # E[$ loss per share | fill]
    e_time_on_book_hours: float   # E[hours on book]
    reward_rate_per_hour: float   # $/hour this market earns
    ev_per_day: float             # final EV in $/day
    confidence: str               # "model" or "fallback"
    model_versions: dict          # which model produced each estimate


class CalibrationManager:
    """Orchestrates calibration models and exposes predictions."""

    def __init__(self, db_path: str = "bot_history.db"):
        self.db_path = db_path
        self.fill_model = FillModel()
        self.loss_model = LossModel()
        self.hazard_model = HazardModel()
        self.reward_model = RewardModel()
        self._last_retrain: float = 0.0
        self._book_cache: dict[str, dict] = {}

        # Try loading persisted models
        self._ensure_table()
        self._load_all()

    def _ensure_table(self):
        """Create calibration_model_state table if missing."""
        try:
            db = _connect_db(self.db_path)
            db.execute(
                """CREATE TABLE IF NOT EXISTS calibration_model_state (
                    model_name    TEXT PRIMARY KEY,
                    weights_json  TEXT NOT NULL,
                    trained_at    REAL NOT NULL,
                    n_samples     INTEGER NOT NULL,
                    n_positive    INTEGER NOT NULL DEFAULT 0,
                    metrics_json  TEXT NOT NULL DEFAULT '{}',
                    feature_names TEXT NOT NULL DEFAULT '[]'
                )"""
            )
            db.commit()
            db.close()
        except Exception as e:
            log.warning(f"Calibration table init failed: {e}")

    def _load_all(self):
        """Load all persisted models."""
        self.fill_model.load(self.db_path)
        self.loss_model.load(self.db_path)
        self.hazard_model.load(self.db_path)
        self.reward_model.load(self.db_path)

    def retrain(self, correction_factor: float = 1.0) -> dict:
        """Retrain all models from DB data.

        Respects cooldown: won't retrain if last train was < 30 min ago.
        Returns dict of training metrics per model.
        """
        now = time.time()
        if now - self._last_retrain < RETRAIN_COOLDOWN_SECS:
            log.debug("Calibration retrain skipped (cooldown)")
            return {"status": "cooldown"}

        self._book_cache.clear()

        results = {}

        try:
            results["fill_model"] = self.fill_model.train(self.db_path)
            if self.fill_model.is_ready():
                self.fill_model.save(self.db_path)
        except Exception as e:
            log.warning(f"FillModel training failed: {e}")
            results["fill_model"] = {"status": "error", "error": str(e)}

        try:
            results["loss_model"] = self.loss_model.train(self.db_path)
            if self.loss_model.is_ready():
                self.loss_model.save(self.db_path)
        except Exception as e:
            log.warning(f"LossModel training failed: {e}")
            results["loss_model"] = {"status": "error", "error": str(e)}

        try:
            results["hazard_model"] = self.hazard_model.train(self.db_path)
            if self.hazard_model.is_ready():
                self.hazard_model.save(self.db_path)
        except Exception as e:
            log.warning(f"HazardModel training failed: {e}")
            results["hazard_model"] = {"status": "error", "error": str(e)}

        try:
            results["reward_model"] = self.reward_model.train(
                self.db_path, correction_factor=correction_factor,
            )
            self.reward_model.save(self.db_path)
        except Exception as e:
            log.warning(f"RewardModel training failed: {e}")
            results["reward_model"] = {"status": "error", "error": str(e)}

        self._last_retrain = now
        self._preload_book_cache()

        log.info(f"Calibration retrain complete: {results}")
        return results

    def _preload_book_cache(self):
        """Cache latest book_snapshot per market for fast prediction."""
        try:
            db = _connect_db(self.db_path)
            rows = db.execute(
                "SELECT bs.condition_id, bs.spread, bs.midpoint, "
                "bs.our_bid_depth_ahead, bs.our_ask_depth_ahead, "
                "bs.total_bid_depth, bs.total_ask_depth, "
                "bs.bid_depth_5c, bs.ask_depth_5c, "
                "bs.daily_rate, bs.agent_shares "
                "FROM book_snapshots bs "
                "INNER JOIN ("
                "  SELECT condition_id, MAX(ts) as max_ts "
                "  FROM book_snapshots GROUP BY condition_id"
                ") latest ON bs.condition_id = latest.condition_id "
                "AND bs.ts = latest.max_ts"
            ).fetchall()
            db.close()
            for r in rows:
                self._book_cache[r[0]] = {
                    "spread": r[1] or 0.045,
                    "midpoint": r[2] or 0.5,
                    "bid_depth_ahead": r[3] or 0,
                    "ask_depth_ahead": r[4] or 0,
                    "total_bid": r[5] or 0,
                    "total_ask": r[6] or 0,
                    "bid_depth_5c": r[7] or 0,
                    "ask_depth_5c": r[8] or 0,
                    "daily_rate": r[9] or 0,
                    "agent_shares": r[10] or 50,
                }
        except Exception as e:
            log.debug(f"Book cache preload failed: {e}")

    def get_predictions(
        self,
        condition_id: str,
        daily_rate: float = 0.0,
        q_share_pct: float = 0.0,
        on_book_hours: float = 0.0,
        fill_count_recent: int = 0,
        fill_cost_recent: float = 0.0,
        dump_revenue_recent: float = 0.0,
        agent_shares: float = 50.0,
        correction_factor: float = 1.0,
    ) -> CalibrationPredictions:
        """Get all calibration predictions for a market.

        Uses model predictions when ready, else conservative fallbacks.
        """
        book = self._book_cache.get(condition_id, {})
        spread = book.get("spread", 0.045)
        midpoint = book.get("midpoint", 0.5)
        depth_ahead = book.get("bid_depth_ahead", 0)
        total_same = book.get("total_bid", 0)
        opp_depth = book.get("ask_depth_5c", 0)
        dr = book.get("daily_rate", daily_rate)
        ag_sh = book.get("agent_shares", agent_shares)

        versions: dict = {}

        # P(fill)
        if self.fill_model.is_ready():
            order_price = midpoint - spread / 2  # typical bid placement
            p_fill = self.fill_model.predict_from_book(
                spread=spread, midpoint=midpoint,
                depth_ahead=depth_ahead, total_same_depth=total_same,
                opposite_depth_5c=opp_depth, daily_rate=dr,
                agent_shares=ag_sh, order_price=order_price,
            )
            if p_fill < 0:
                p_fill = self._fallback_p_fill(fill_count_recent, on_book_hours)
                versions["p_fill"] = "fallback"
            else:
                versions["p_fill"] = "model"
        else:
            p_fill = self._fallback_p_fill(fill_count_recent, on_book_hours)
            versions["p_fill"] = "fallback"

        # E[loss | fill]
        if self.loss_model.is_ready():
            e_loss = self.loss_model.predict_total_loss(
                shares=ag_sh, slippage=0.0, spread=spread,
            )
            versions["e_loss"] = "model"
        else:
            e_loss = self._fallback_e_loss(
                fill_cost_recent, dump_revenue_recent, fill_count_recent,
            )
            versions["e_loss"] = "fallback"

        # E[time on book]
        if self.hazard_model.is_ready():
            e_time = self.hazard_model.predict(depth_ahead=depth_ahead)
            versions["e_time"] = "model"
        else:
            e_time = min(on_book_hours, 24.0) if on_book_hours > 0 else 12.0
            versions["e_time"] = "fallback"

        # Reward rate — apply safety bias (Fix 4)
        # Reward attribution is the noisiest component; cap at 80% of estimate
        reward_rate_raw = self.reward_model.predict_rate(
            condition_id=condition_id,
            daily_rate=daily_rate,
            q_share_pct=q_share_pct,
            correction_factor=correction_factor,
        )
        reward_rate = reward_rate_raw * REWARD_SAFETY_BIAS
        versions["reward"] = f"phase{self.reward_model.phase}"

        # EV = (reward_rate * E[time]) - (P_fill * E[loss])
        raw_ev = (reward_rate * min(e_time, 24.0)) - (p_fill * e_loss)

        # Fix 6: Uncertainty penalty — scale EV by confidence_score
        # from the safety controller (if available) or model confidence.
        # Early models produce noisy estimates; discount them.
        confidence_label = "model" if all(
            v != "fallback" for v in versions.values()
        ) else "fallback"

        n_model = sum(1 for v in versions.values() if v not in ("fallback",))
        n_total = len(versions)
        model_confidence = max(UNCERTAINTY_FLOOR, n_model / max(n_total, 1))
        ev = raw_ev * model_confidence

        return CalibrationPredictions(
            condition_id=condition_id,
            p_fill_24h=p_fill,
            e_loss_given_fill=e_loss,
            e_time_on_book_hours=e_time,
            reward_rate_per_hour=reward_rate,
            ev_per_day=ev,
            confidence=confidence_label,
            model_versions=versions,
        )

    def get_ev(self, condition_id: str, **kwargs) -> float:
        """Compute EV in $/day. Convenience wrapper."""
        return self.get_predictions(condition_id, **kwargs).ev_per_day

    def is_ready(self) -> bool:
        """True if at least fill_model and loss_model are trained.

        The hazard and reward models have sensible fallbacks,
        but fill + loss are the core EV components.
        """
        return self.fill_model.is_ready() and self.loss_model.is_ready()

    @property
    def model_info(self) -> dict:
        """Training metrics for logging."""
        return {
            "fill_model": self.fill_model.metrics,
            "loss_model": self.loss_model.metrics,
            "hazard_model": self.hazard_model.metrics,
            "reward_model": self.reward_model.metrics,
            "is_ready": self.is_ready(),
        }

    @staticmethod
    def _fallback_p_fill(fill_count: int, on_book_hours: float) -> float:
        """Fallback fill probability from observed fill rate."""
        hours = max(on_book_hours, 1.0)
        rate_per_hour = fill_count / hours
        # Project to 24h, cap at 0.5
        return min(0.5, rate_per_hour * 24.0)

    @staticmethod
    def _fallback_e_loss(
        fill_cost: float, dump_revenue: float, fill_count: int,
    ) -> float:
        """Fallback expected loss from observed fill damage."""
        if fill_count <= 0:
            return 0.02 * 50  # default: $0.02/share * 50 shares = $1
        net_damage = max(0, fill_cost - dump_revenue)
        return net_damage / fill_count
