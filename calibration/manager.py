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


# ── PATCH 7 PART 2: COLD-START P_FILL FALLBACK ─────────────────
# Book-aware heuristic used when the trained FillModel is unavailable
# OR returns a too-small prediction (< 0.005). Keeps p_fill strictly
# non-zero so expected_capital and V3 audit metrics remain defined in
# the early run before the model has enough training data.
#
# Heuristic:
#   base = 0.02
#   + 0.05 if spread <  0.02 (very tight → fills fast)
#   + 0.02 if spread <  0.05 (moderately tight)
#   + 0.02 if depth  <  500  (shallow book → fewer shares ahead of us)
#   × max(0.5, 1.0 - queue_position)   (worse queue position → slower)
# Clamped to [0.01, 0.15].
def _fallback_p_fill_cold_start(
    spread: float, depth: float, position: float,
) -> float:
    spread = float(spread if spread is not None else 0.05)
    depth = float(depth if depth is not None else 0.0)
    position = float(position if position is not None else 0.0)
    base = 0.02
    if spread < 0.02:
        base += 0.05
    elif spread < 0.05:
        base += 0.02
    if depth < 500:
        base += 0.02
    base *= max(0.5, 1.0 - position)
    return min(0.15, max(0.01, base))

# Fix 4: Safety bias — cap reward term at 80% of naive estimate
# Reward attribution is the noisiest model; never trust it fully
REWARD_SAFETY_BIAS = 0.80

# Fix 5: Minimum EV to deploy ($/day) — prevents deploying marginal markets
MIN_EV_THRESHOLD = 0.10

# PART 4: dynamic confidence floor (replaces the static UNCERTAINTY_FLOOR
# baseline). The constant is retained as the floor used when raw EV is
# positive — older callers/tests still import it, and it equals
# CONFIDENCE_FLOOR_PROFITABLE so semantics are preserved.
UNCERTAINTY_FLOOR = 0.3
CONFIDENCE_FLOOR_PROFITABLE = 0.2  # raw_ev > 0
CONFIDENCE_FLOOR_UNPROFITABLE = 0.5  # raw_ev <= 0  → demand more certainty

# PART 3: per-market reliability degrade levels. A model that fell back to
# defaults for THIS market is treated as low reliability for THIS market
# regardless of the model's global training quality.
PER_MARKET_FALLBACK_RELIABILITY = 0.3
PER_MARKET_BOOK_STALE_RELIABILITY_CAP = 0.5

# Per-model importance weights for confidence (sum to 1.0)
# Fill + loss drive the risk term; hazard + reward drive the reward term
MODEL_WEIGHTS = {
    "p_fill": 0.35,
    "e_loss": 0.30,
    "e_time": 0.20,
    "reward": 0.15,
}

# STEP 10 — Attribution error → confidence penalty.
# If the attribution reconciliation drifts more than 30% from the
# recorded total payout, the per-market reward signal is unreliable
# and we soften the whole confidence vector by 0.8×.
ATTRIBUTION_ERROR_THRESHOLD = 0.30
ATTRIBUTION_CONFIDENCE_PENALTY = 0.80


@dataclass
class CalibrationPredictions:
    """Unified predictions for a single market."""
    condition_id: str
    p_fill_24h: float             # P(fill within 24h)
    e_loss_given_fill: float      # E[$ total loss per fill]
    e_time_on_book_hours: float   # E[hours on book]
    reward_rate_per_hour: float   # $/hour this market earns
    ev_per_day: float             # final EV in $/day
    confidence: str               # "model" or "fallback"
    model_versions: dict          # which model produced each estimate
    model_confidence: float = 1.0 # numeric 0.3–1.0 weighted reliability
    raw_ev_per_day: float = 0.0   # pre-confidence EV (reward − p_fill·loss)


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
        # STEP 10: cache the attribution-error read so get_predictions()
        # doesn't hit the DB per market. Cleared on retrain().
        self._attribution_error_cache: float | None = None
        # Learning-loop reward_trust hook. Caller (oversight_agent) sets
        # this to ls.reward_trust ONLY when the LearningController is in
        # ACTIVE mode; OFF/SHADOW leave it at the neutral default 1.0.
        # Values outside [0.5, 1.0] are clamped by the learning module.
        self.reward_trust: float = 1.0

        # Try loading persisted models
        self._ensure_table()
        self._load_all()

    def _cached_attribution_error(self) -> float:
        """STEP 10 hook — lazy-load today's attribution reconciliation error.

        Cached for the lifetime of a prediction cycle (reset by retrain()).
        Returns 0.0 on any failure so the penalty is OFF by default.
        """
        if self._attribution_error_cache is not None:
            return self._attribution_error_cache
        try:
            from .attribution import get_attribution_error
            err = get_attribution_error(self.db_path)
        except Exception as e:
            log.debug(f"attribution_error load failed: {e}")
            err = 0.0
        self._attribution_error_cache = err
        return err

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
        # STEP 10: force a fresh attribution-error read on next get_predictions
        self._attribution_error_cache = None

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
        book_missing = not book
        spread = book.get("spread", 0.045)
        midpoint = book.get("midpoint", 0.5)
        depth_ahead = book.get("bid_depth_ahead", 0)
        total_same = book.get("total_bid", 0)
        opp_depth = book.get("ask_depth_5c", 0)
        dr = book.get("daily_rate", daily_rate)
        ag_sh = book.get("agent_shares", agent_shares)

        versions: dict = {}

        # P(fill) — PATCH 7 scope: book-aware fallback applies ONLY when
        # the FillModel has been trained (is_ready) but the prediction is
        # suspiciously low (< 0.005). When the model has no training data
        # yet, keep the legacy observation-based fallback so EV doesn't
        # collapse during bootstrap: a non-zero cold-start p_fill against
        # a non-zero cold-start e_loss yields negative raw_ev for almost
        # every market, the hard profit guard fires "NO DEPLOYMENT", and
        # the learning gate never leaves OFF.
        queue_pos = 0.0
        if total_same and total_same > 0:
            queue_pos = min(1.0, depth_ahead / max(1.0, total_same))

        if self.fill_model.is_ready():
            order_price = midpoint - spread / 2  # typical bid placement
            p_fill_model = self.fill_model.predict_from_book(
                spread=spread, midpoint=midpoint,
                depth_ahead=depth_ahead, total_same_depth=total_same,
                opposite_depth_5c=opp_depth, daily_rate=dr,
                agent_shares=ag_sh, order_price=order_price,
            )
            if p_fill_model is None or p_fill_model < 0.005:
                # PATCH 7 — trained model produced an unreliably-low value.
                # Use book-aware heuristic instead.
                p_fill = _fallback_p_fill_cold_start(
                    spread, total_same, queue_pos,
                )
                versions["p_fill"] = "fallback_fill"
            elif book_missing:
                # FIX 13: book cache miss → prediction ran on default features
                p_fill = p_fill_model
                versions["p_fill"] = "book_stale"
            else:
                p_fill = p_fill_model
                versions["p_fill"] = "model"
        else:
            # Model not trained yet — observation-based fallback keeps
            # bootstrap behavior. Returns 0 with no observations, which
            # intentionally lets EV stay positive so the system can deploy
            # and collect data. Non-zero observability floor is enforced
            # downstream by the allocator's PATCH 7 `_p_fill` stamp.
            p_fill = self._fallback_p_fill(
                fill_count_recent, on_book_hours,
            )
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
                agent_shares=ag_sh,
            )
            versions["e_loss"] = "fallback"

        # E[time on book]
        if self.hazard_model.is_ready():
            e_time = self.hazard_model.predict(depth_ahead=depth_ahead)
            versions["e_time"] = "model"
        else:
            # PART 5: no min(e_time, 24) cap — time horizon for EV is
            # enforced explicitly via reward = reward_rate * 24 below.
            # e_time is an observability signal only.
            e_time = on_book_hours if on_book_hours > 0 else 12.0
            versions["e_time"] = "fallback"

        # Reward rate — UNBIASED model output. The 0.80 safety bias is
        # applied explicitly in the EV pipeline (PART 6) so all reward-side
        # multipliers are visible in one place.
        reward_rate_unbiased = self.reward_model.predict_rate(
            condition_id=condition_id,
            daily_rate=daily_rate,
            q_share_pct=q_share_pct,
            correction_factor=correction_factor,
        )
        # FIX 4: phase 1 reward is a CF passthrough — mark it as fallback_cf
        versions["reward"] = (
            "fallback_cf" if self.reward_model.phase == 1 else "model"
        )

        # PART 5 + FIX 15: time horizon for reward is exactly 24h; the
        # baseline biased reward is what raw_ev/floor selection is computed
        # against (model_confidence is NOT applied here — that's PART 6).
        raw_reward = reward_rate_unbiased * 24 * REWARD_SAFETY_BIAS

        # PART 1 + FIX 14: raw EV uses the SAME p_fill_24h and the SAME
        # loss_per_fill that RAS uses downstream. No divergence allowed.
        raw_ev = raw_reward - (p_fill * e_loss)

        # Confidence label: any non-"model" version counts as fallback so
        # "fallback", "fallback_cf", and "book_stale" all degrade it.
        confidence_label = "model" if all(
            v == "model" for v in versions.values()
        ) else "fallback"

        # PART 3: per-market reliability. Start from the global per-model
        # reliability score, then degrade for THIS market based on:
        #   - feature/book availability ("book_stale" → cap at 0.5)
        #   - per-market model availability ("fallback"/"fallback_cf" → 0.3)
        # Models that produced a real model prediction keep their global
        # score. This is "true per-market" — two markets with different
        # versions get different model_confidence values.
        reliability = {
            "p_fill": self.fill_model.get_reliability_score(),
            "e_loss": self.loss_model.get_reliability_score(),
            "e_time": self.hazard_model.get_reliability_score(),
            "reward": self.reward_model.get_reliability_score(),
        }
        per_market_reliability: dict = {}
        for k in MODEL_WEIGHTS:
            v = versions.get(k, "fallback")
            base = reliability[k]
            if v == "model":
                per_market_reliability[k] = base
            elif v == "book_stale":
                per_market_reliability[k] = min(
                    base, PER_MARKET_BOOK_STALE_RELIABILITY_CAP,
                )
            else:  # "fallback" or "fallback_cf"
                per_market_reliability[k] = PER_MARKET_FALLBACK_RELIABILITY

        # Per-market weighted confidence. MODEL_WEIGHTS sums to 1.0 and
        # every key contributes at >= PER_MARKET_FALLBACK_RELIABILITY, so
        # weighted_conf is always in [0.3, 1.0] before the floor.
        weighted_conf = sum(
            MODEL_WEIGHTS[k] * per_market_reliability[k] for k in MODEL_WEIGHTS
        )

        # STEP 10: Optional attribution-error penalty. Cached so we don't
        # requery the DB on every market — the error is a daily statistic.
        attr_err = self._cached_attribution_error()
        if attr_err > ATTRIBUTION_ERROR_THRESHOLD:
            weighted_conf *= ATTRIBUTION_CONFIDENCE_PENALTY

        # PART 4: dynamic floor based on raw EV sign.
        #   raw_ev > 0  → market looks profitable; small floor allows the
        #                 EV/RAS pipeline to express the signal.
        #   raw_ev <= 0 → market looks unprofitable; require higher floor
        #                 so a low-confidence model can't accidentally
        #                 inflate the picture.
        floor = (
            CONFIDENCE_FLOOR_PROFITABLE if raw_ev > 0
            else CONFIDENCE_FLOOR_UNPROFITABLE
        )
        model_confidence = max(floor, weighted_conf)

        # PART 6: explicit reward pipeline. Four multipliers, no hidden
        # stacking elsewhere. reward_trust is the learning-loop feedback
        # multiplier — default 1.0 (neutral), reduced when observed reward
        # has been < 0.7× predicted over a window.
        reward = reward_rate_unbiased * 24
        reward *= REWARD_SAFETY_BIAS
        reward *= model_confidence
        reward *= self.reward_trust

        # PART 1 + PART 2: capped confidence asymmetry. Loss inflates by
        # at most 2× (when model_confidence == 0); reward shrinks by at
        # most model_confidence×. The SAME p_fill_24h and loss_per_fill
        # used in raw_ev / RAS appear here.
        loss_term = (p_fill * e_loss) * (1.0 + (1.0 - model_confidence))
        ev = reward - loss_term

        return CalibrationPredictions(
            condition_id=condition_id,
            p_fill_24h=p_fill,
            e_loss_given_fill=e_loss,
            e_time_on_book_hours=e_time,
            # Field semantics preserved: the BIASED rate, since the safety
            # bias is part of the rate the EV pipeline actually consumes.
            reward_rate_per_hour=reward_rate_unbiased * REWARD_SAFETY_BIAS,
            ev_per_day=ev,
            confidence=confidence_label,
            model_versions=versions,
            model_confidence=model_confidence,
            raw_ev_per_day=raw_ev,
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
        agent_shares: float = 50.0,
    ) -> float:
        """Fallback expected loss from observed fill damage."""
        if fill_count <= 0:
            return 0.02 * agent_shares
        net_damage = max(0, fill_cost - dump_revenue)
        return net_damage / fill_count
