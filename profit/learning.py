"""profit/learning.py — Real-time behavior correction layer.

This is NOT model retraining. It is a decoupled, observable feedback loop
that measures divergence between predictions and reality and emits four
scalar adjustments that the profit engine already knows how to consume:

    aggressiveness   scales final allocation score
    capital_scale    scales deployable capital
    risk_multiplier  inflates the loss term in RAS
    reward_trust     discounts the reward term in EV

Gate (STEP 0):
    OFF    — insufficient data; do nothing.
    SHADOW — enough data to measure; compute & log, DO NOT apply.
    ACTIVE — fully armed; compute, persist, apply.

Invariants:
  1. OFF and SHADOW applied_state is ALWAYS neutral (all 1.0).
     Only ACTIVE can influence decisions.
  2. Clamped scalars: aggressiveness ∈ [0.3,1.5], capital_scale ∈ [0.3,1.2],
     risk_multiplier ∈ [1.0,2.0], reward_trust ∈ [0.5,1.0].
  3. EMA-smoothed with alpha=0.2 — never overreact to one noisy cycle.
  4. Deterministic — no randomness, no wall-clock dependencies in the
     decision function (update_state is pure).
  5. Fail-closed — missing/None metrics → no state change, apply prev.
  6. No new raw data collection — reads only fills, unwinds,
     reward_attribution, reward_daily, orders_placed, book_snapshots,
     and market_allocations.json.
"""

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from oversight.data_collector import _connect_db

log = logging.getLogger("profit.learning")

# ── MODE CONSTANTS ─────────────────────────────────────────────
MODE_OFF = "OFF"
MODE_SHADOW = "SHADOW"
MODE_ACTIVE = "ACTIVE"

# ── STEP 0 GATE THRESHOLDS ─────────────────────────────────────
GATE_SHADOW_FILLS = 100
GATE_SHADOW_PAIRS = 50
GATE_SHADOW_DAYS = 3
GATE_ACTIVE_FILLS = 200
GATE_ACTIVE_PAIRS = 100
GATE_ACTIVE_DAYS = 5
GATE_ACTIVE_CYCLES = 50

# ── METRIC WINDOWS ─────────────────────────────────────────────
METRIC_WINDOW_SECS = 86400   # 24h
HOURLY_WINDOW_SECS = 3600    # 1h for regime signals

# ── STEP 3 RULE THRESHOLDS ─────────────────────────────────────
FILL_RATE_HIGH = 0.30                 # > 30% of orders filling
LOSS_PER_FILL_HIGH = 1.25             # > $1.25 avg loss per fill
# FIX 1: capital-normalized loss threshold. 5% daily burn of deployed
# capital is a structural red flag regardless of per-fill magnitude.
LOSS_PER_CAPITAL_HIGH = 0.05
# FIX 2: static target kept only as a pre-baseline seed for logging /
# backward-compat; rules now use the adaptive reward_efficiency_baseline.
REWARD_EFFICIENCY_TARGET = 0.0005     # seed value, not used by rules
REWARD_EFFICIENCY_GOOD = 1.3 * REWARD_EFFICIENCY_TARGET  # legacy alias
GLOBAL_FILL_RATE_HIGH = 0.50
REWARD_ERROR_OVERESTIMATE = 0.7
REWARD_ERROR_HEALTHY_LO = 0.9
REWARD_ERROR_HEALTHY_HI = 1.1
LOSS_ERROR_UNDERESTIMATE = 1.3
LOSS_ERROR_HEALTHY_LO = 0.8
LOSS_ERROR_HEALTHY_HI = 1.1

# FIX 2: adaptive baseline window (days) and minimum sample count.
BASELINE_WINDOW_DAYS = 7
BASELINE_MIN_DAYS = 3

# ── RULE ADJUSTMENT DELTAS (pre-EMA) ───────────────────────────
AGGR_DOWN = 0.80
AGGR_UP = 1.10
CAP_DOWN = 0.90
CAP_UP = 1.10
RISK_UP = 1.15
RISK_DOWN = 0.98
TRUST_DOWN = 0.90
TRUST_UP = 1.02

# FIX 5: reward_trust mean-reversion rate per cycle (before EMA).
# Each cycle, trust moves 2% of the distance toward 1.0 (neutral). This
# prevents trust from staying pessimistically low forever after a single
# bad period — if nothing goes wrong, it drifts back up.
TRUST_REVERSION_RATE = 0.02

# ── PATCH 3 (REWARD EXPANSION) CONSTANTS ───────────────────────
# PART 2: reward-growth expansion / contraction rule deltas.
EXPANSION_CAP_UP = 1.10          # legacy constant, kept for back-compat tests
EXPANSION_AGGR_UP = 1.05
EXPANSION_CAP_DOWN = 0.90
# PART 2 efficiency floor as fraction of baseline for expansion to fire.
EXPANSION_EFFICIENCY_FLOOR_FRAC = 0.7
# PART 3: frontier-probe cadence and amplitude.
PROBE_INTERVAL = 10
PROBE_SCALE = 1.10               # legacy constant, Patch 4 uses dynamic strength
# PART 5: recency weighting of current 24h vs trailing history.
RECENCY_WEIGHT = 0.7
# PART 6: frontier-overshoot contraction threshold on efficiency_delta.
EFFICIENCY_DELTA_COLLAPSE = -0.15
EFFICIENCY_DELTA_COLLAPSE_CAP = 0.90

# ── PATCH 4 (FRONTIER MEMORY) CONSTANTS ────────────────────────
# PART 3 — aggressive expansion is 12% above prev (was 10%) and is
# gated by a frontier_limit = best_capital_scale * FRONTIER_LIMIT_MULT.
FRONTIER_EXPANSION_CAP_UP = 1.12
FRONTIER_LIMIT_MULT = 1.25
# PART 4 — contraction floor as a fraction of the best capital scale.
# Final-guard guarantees u_cap never drops below this fraction of best.
FRONTIER_MIN_FLOOR_FRAC = 0.60
# PART 5 — probe only fires when recency-weighted efficiency has not
# moved more than this absolute delta vs the previous cycle's raw.
PROBE_STABILITY_DELTA = 0.05
# PART 6 — dynamic probe strength formula: 1.05 + 0.05 * min(1, cap).
PROBE_STRENGTH_BASE = 1.05
PROBE_STRENGTH_CAP_COEF = 0.05
# PART 7 — sharp efficiency collapse correction.
EFFICIENCY_DELTA_SHARP_COLLAPSE = -0.25
EFFICIENCY_DELTA_SHARP_CAP = 0.85

# ── PATCH 5 (REGIME-AWARE FRONTIER) CONSTANTS ──────────────────
# PART 2 — memory bounded size; on write we keep the N most recently
# updated regimes (PART 8 pruning). 20 is large enough to hold a few
# market phases without unbounded growth.
FRONTIER_MEMORY_MAX_SIZE = 20
# PART 6 — cold-start expansion ceiling when this regime is unseen.
# Slightly wider than 1.0 to allow first-time expansion but tighter
# than FRONTIER_LIMIT_MULT so unknown regimes don't inherit old ones.
COLD_START_FRONTIER_MULT = 1.10
# PART 10 — controlled aggressive spike. Probability per cycle and
# the multiplier applied. Gated on regime_id != None so cold-starts
# never spike. Tests override REGIME_SPIKE_PROBABILITY for determinism.
REGIME_SPIKE_PROBABILITY = 0.05
REGIME_SPIKE_CAP_UP = 1.20

# ── PATCH 6 (SAFE EXPANSION) CONSTANTS ─────────────────────────
# Fill + loss thresholds that characterise a "too passive" or "too
# aggressive" regime independent of reward baseline. When both the
# fill rate and the capital-normalised loss are below SAFE_*, the
# system has headroom to expand. When both exceed the upper band
# (fill > 2× SAFE_FILL_RATE AND loss > SAFE_LOSS_PER_CAPITAL) the
# system should tighten. EMA + clamp + min_floor still apply.
SAFE_FILL_RATE = 0.15
SAFE_LOSS_PER_CAPITAL = 0.01
EXPANSION_SCALE_UP = 1.05
EXPANSION_SCALE_DOWN = 0.97
# Aggressiveness nudge paired with the SAFE expansion path.
SAFE_EXPANSION_AGGR_UP = 1.02

# ── PATCH 11 (OSCILLATION DAMPING) CONSTANTS ───────────────────
# When the capital_scale trace shows ≥ OSCILLATION_THRESHOLD direction
# flips in the last OSCILLATION_WINDOW valid updates, dampen the raw
# u_cap by OSCILLATION_DAMPEN_FACTOR BEFORE EMA smoothing. Targets
# the V3.1 audit INV7 finding: learning loop oscillates under
# overcommit in adversarial regimes.
OSCILLATION_WINDOW = 20
OSCILLATION_THRESHOLD = 6
OSCILLATION_DAMPEN_FACTOR = 0.85
# Cap stored per state; beyond this, oldest samples drop off.
CAPITAL_HISTORY_MAX = 100

# ── STEP 5 EMA SMOOTHING ───────────────────────────────────────
EMA_ALPHA = 0.20

# ── STEP 3 CLAMPS ──────────────────────────────────────────────
CLAMP_AGGR = (0.30, 1.50)
CLAMP_CAP = (0.30, 1.20)
CLAMP_RISK = (1.00, 2.00)
CLAMP_TRUST = (0.50, 1.00)

# ── BASELINES ──────────────────────────────────────────────────
# Predicted loss per fill when no historical model signal exists.
# 50 shares × $0.025 = $1.25. Updates to this constant change the
# loss_error dynamic range — keep stable unless you retune rules too.
PREDICTED_LOSS_PER_FILL_BASELINE = 1.25


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ═══════════════════════════════════════════════════════════════
# PATCH 11 — Oscillation detection + in-memory history cache
# ═══════════════════════════════════════════════════════════════
#
# The LearningController is instantiated per-cycle by oversight_agent
# (see oversight_agent.py:372), so an instance attribute would not
# survive across cycles. A module-level cache DOES survive the
# long-running agent process — matching the user's "in-memory only,
# 20-cycle rebuild on restart" intent. The cache is populated from the
# LearningState returned by update_state() during persist_state, and
# injected back during load_state. No DB migration, no state bloat.
_CAPITAL_HISTORY_CACHE: list[float] = []


def _reset_capital_history_cache() -> None:
    """Test helper — clears the module-level capital_history cache so
    tests that exercise oscillation damping start from a known state.
    No production caller touches this."""
    global _CAPITAL_HISTORY_CACHE
    _CAPITAL_HISTORY_CACHE = []


def _detect_oscillation(history: list) -> bool:
    """Return True when `history` shows ≥ OSCILLATION_THRESHOLD direction
    flips. A flip is a sign change in the first difference between
    consecutive samples. With `range(2, len(history))` we safely index
    `i-2` without Python's negative-index wrap. Equal consecutive values
    produce a zero-difference and contribute no flip."""
    flips = 0
    for i in range(2, len(history)):
        if (history[i] - history[i - 1]) * (history[i - 1] - history[i - 2]) < 0:
            flips += 1
    return flips >= OSCILLATION_THRESHOLD


def _safe_div(num: Optional[float], den: Optional[float]) -> Optional[float]:
    """Invariant 5: never silently return 0. None propagates as None."""
    if num is None or den is None:
        return None
    if den == 0:
        return None
    return num / den


# ═══════════════════════════════════════════════════════════════
# PATCH 5 — Frontier memory helpers
# ═══════════════════════════════════════════════════════════════

def _serialize_memory(memory: dict) -> str:
    """JSON-encode a regime_id→entry dict. Tuple keys are encoded via
    json.dumps(list(key)) so the resulting string is a valid JSON
    object key. Returns '{}' for empty memory."""
    if not memory:
        return "{}"
    serializable = {}
    for key, value in memory.items():
        try:
            str_key = json.dumps(list(key))
            serializable[str_key] = value
        except Exception:
            continue
    try:
        return json.dumps(serializable)
    except Exception:
        return "{}"


def _deserialize_memory(s: Optional[str]) -> dict:
    """Parse a JSON-encoded memory dict back into {tuple_key: entry}.
    Returns {} on None, empty, or malformed input (fail-closed)."""
    if not s:
        return {}
    try:
        raw = json.loads(s)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for str_key, value in raw.items():
        try:
            parsed = json.loads(str_key)
            if not isinstance(parsed, list):
                continue
            out[tuple(parsed)] = value
        except Exception:
            continue
    return out


def _prune_memory(memory: dict, max_size: int = FRONTIER_MEMORY_MAX_SIZE) -> dict:
    """PART 8 — keep only the `max_size` most recently updated regimes.

    Entries are ranked by their `last_updated` timestamp descending.
    Malformed entries (missing key) sort to the end. Returns a NEW dict
    so callers can persist it without mutating the live copy."""
    if len(memory) <= max_size:
        return dict(memory)
    ranked = sorted(
        memory.items(),
        key=lambda kv: kv[1].get("last_updated", 0.0) if isinstance(kv[1], dict) else 0.0,
        reverse=True,
    )
    return dict(ranked[:max_size])


# ═══════════════════════════════════════════════════════════════
# STEP 2 — STATE
# ═══════════════════════════════════════════════════════════════

@dataclass
class LearningState:
    """Four behavioral scalars broadcast to the profit engine each cycle.

    Defaults are neutral (no influence on decisions). OFF/SHADOW modes
    always publish the neutral state — scalars all 1.0, market_efficiency_map
    empty, mode reflecting the gate's decision.

    FIX 4 — market_efficiency_map: per-market reward/capital ratio. The
    allocator applies ±10% / −20% score multipliers based on quintile
    position. Empty in OFF/SHADOW so it has no effect there.

    FIX 6 — valid_cycles_observed replaces cycles_observed. Increments
    ONLY on cycles where _metrics_complete returned True, so the gate
    can't be tricked into promoting on cycles that failed to produce
    usable signal.

    FIX 7 — mode: the allocator reads this to decide whether to reserve
    5% of deployable capital for exploration (only in ACTIVE).
    """
    aggressiveness: float = 1.0
    capital_scale: float = 1.0
    risk_multiplier: float = 1.0
    reward_trust: float = 1.0
    valid_cycles_observed: int = 0
    updated_at: float = 0.0
    mode: str = MODE_OFF
    market_efficiency_map: dict = field(default_factory=dict)
    # PART 3 — cycle index of the last frontier-probe fire. Persisted so
    # probes fire at PROBE_INTERVAL spacing across process restarts.
    last_probe_cycle: int = 0
    # PART 6 — efficiency from the previous cycle, used to compute
    # efficiency_delta. Persisted so the delta survives restarts.
    prev_reward_efficiency: Optional[float] = None
    # PART 4 — informational, forwarded from the controller to the
    # allocator so _compute_exploration_pct can read them. Not persisted
    # (recomputed each cycle from fresh metrics).
    reward_efficiency: Optional[float] = None
    reward_efficiency_baseline: Optional[float] = None
    # PATCH 5 PART 2 — REGIME-SPECIFIC frontier memory. Replaces the
    # Patch-4 global (best_reward, best_capital_scale) pair. Keyed by
    # regime_id (tuple) whose entries have the shape:
    #   {"best_reward": float,
    #    "best_capital_scale": float,
    #    "last_updated": float}
    # Persisted as a JSON string in the DB. Pruned to
    # FRONTIER_MEMORY_MAX_SIZE entries on write.
    frontier_memory: dict = field(default_factory=dict)
    # PATCH 11 — rolling trace of post-clamp capital_scale values used by
    # _detect_oscillation. NOT DB-persisted; the LearningController
    # injects this from a module-level cache during load_state and
    # writes back during persist_state. Bounded at CAPITAL_HISTORY_MAX.
    capital_history: list = field(default_factory=list)


@dataclass
class LearningStep:
    """Output of a single learning cycle iteration."""
    mode: str                      # OFF | SHADOW | ACTIVE
    applied_state: LearningState   # SAFE to apply (neutral unless ACTIVE)
    computed_state: LearningState  # what rules produced (for observability)
    metrics: dict                  # full metrics vector


# ═══════════════════════════════════════════════════════════════
# STEP 0 — ACTIVATION GATE
# ═══════════════════════════════════════════════════════════════

class LearningGate:
    """Classifies system maturity into OFF / SHADOW / ACTIVE.

    The ladder is strict: ALL SHADOW thresholds must be met to leave OFF,
    and ALL ACTIVE thresholds (including cycles_observed) to leave SHADOW.
    """

    @staticmethod
    def evaluate_activation(metrics: dict) -> str:
        fills = int(metrics.get("fills_total", 0) or 0)
        pairs = int(metrics.get("fill_unwind_pairs_total", 0) or 0)
        days = int(metrics.get("reward_days", 0) or 0)
        # FIX 6: use valid_cycles_observed — only cycles with complete
        # metrics count toward promotion. A noisy 50 half-broken cycles
        # should NOT trigger ACTIVE.
        cycles = int(metrics.get("valid_cycles_observed", 0) or 0)

        if (fills < GATE_SHADOW_FILLS
                or pairs < GATE_SHADOW_PAIRS
                or days < GATE_SHADOW_DAYS):
            return MODE_OFF

        if (fills >= GATE_ACTIVE_FILLS
                and pairs >= GATE_ACTIVE_PAIRS
                and days >= GATE_ACTIVE_DAYS
                and cycles >= GATE_ACTIVE_CYCLES):
            return MODE_ACTIVE

        return MODE_SHADOW


# ═══════════════════════════════════════════════════════════════
# STEP 1 — METRICS ENGINE
# ═══════════════════════════════════════════════════════════════

class LearningMetrics:
    """Computes the per-cycle metric vector from existing DB tables.

    Data sources (no new collection):
      - fills, unwinds, orders_placed (24h window)
      - reward_attribution (24h proxy: today + yesterday)
      - reward_daily (gate input: # distinct days recorded)
      - book_snapshots (1h regime signal)
      - cycle_snapshots (informational)
      - market_allocations.json (predicted reward, deployed capital)

    Missing data returns None in that field — NEVER silently defaults.
    """

    def __init__(self, db_path: str, alloc_path: str = "market_allocations.json"):
        self.db_path = db_path
        self.alloc_path = alloc_path

    def compute_metrics(self, valid_cycles_observed: int = 0) -> dict:
        now = time.time()
        cutoff_24h = now - METRIC_WINDOW_SECS
        cutoff_1h = now - HOURLY_WINDOW_SECS

        out: dict = {
            # FIX 6: renamed from cycles_observed
            "valid_cycles_observed": int(valid_cycles_observed),
            "status": "ok",
        }

        # ── SQL-derived metrics ────────────────────────────────
        try:
            db = _connect_db(self.db_path)

            row = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(usd_value), 0) "
                "FROM fills WHERE ts > ?", (cutoff_24h,),
            ).fetchone()
            fill_count_24h = int(row[0] or 0)
            fill_cost_24h = float(row[1] or 0.0)

            row = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(usd_value), 0) "
                "FROM unwinds WHERE ts > ?", (cutoff_24h,),
            ).fetchone()
            unwind_count_24h = int(row[0] or 0)
            unwind_revenue_24h = float(row[1] or 0.0)

            row = db.execute(
                "SELECT COUNT(*) FROM orders_placed WHERE ts > ?",
                (cutoff_24h,),
            ).fetchone()
            orders_24h = int(row[0] or 0)

            # GATE inputs — all-time counts
            fills_total = int(db.execute(
                "SELECT COUNT(*) FROM fills",
            ).fetchone()[0] or 0)
            pairs_total = int(db.execute(
                "SELECT COUNT(*) FROM unwinds",
            ).fetchone()[0] or 0)
            reward_days = int(db.execute(
                "SELECT COUNT(DISTINCT date) FROM reward_daily",
            ).fetchone()[0] or 0)

            # Actual rewards (24h proxy: today + yesterday attribution)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            yesterday = (
                datetime.now(timezone.utc) - timedelta(days=1)
            ).strftime("%Y-%m-%d")
            row = db.execute(
                "SELECT COALESCE(SUM(reward_usd), 0) FROM reward_attribution "
                "WHERE date IN (?, ?)", (today, yesterday),
            ).fetchone()
            actual_reward_24h = float(row[0] or 0.0)

            # FIX 4 — per-market rewards for efficiency ranking
            rows_mkt_reward = db.execute(
                "SELECT market_id, COALESCE(SUM(reward_usd), 0) "
                "FROM reward_attribution WHERE date IN (?, ?) "
                "GROUP BY market_id", (today, yesterday),
            ).fetchall()
            market_rewards_map = {
                r[0]: float(r[1] or 0.0) for r in rows_mkt_reward
            }

            # PART 1 — reward growth vs. trailing 3-day average.
            # Requires the last 4 daily totals (today + 3 prior).
            rows_daily = db.execute(
                "SELECT date, total_combined_usd FROM reward_daily "
                "ORDER BY date DESC LIMIT 4",
            ).fetchall()

            # Global fill rate (last 1h)
            fills_1h = int(db.execute(
                "SELECT COUNT(*) FROM fills WHERE ts > ?", (cutoff_1h,),
            ).fetchone()[0] or 0)
            orders_1h = int(db.execute(
                "SELECT COUNT(*) FROM orders_placed WHERE ts > ?",
                (cutoff_1h,),
            ).fetchone()[0] or 0)

            # Volatility proxy (avg book spread in last 1h)
            try:
                row = db.execute(
                    "SELECT AVG(spread) FROM book_snapshots WHERE ts > ?",
                    (cutoff_1h,),
                ).fetchone()
                if row and row[0] is not None:
                    volatility_proxy: Optional[float] = float(row[0])
                else:
                    volatility_proxy = None
            except Exception:
                volatility_proxy = None

            db.close()
        except Exception as e:
            log.warning(f"[LEARNING] metrics SQL failed: {e}")
            return {**out, "status": "error", "error": str(e)}

        # ── Allocation-file metrics ────────────────────────────
        predicted_reward_24h, capital_deployed, market_capital_map = (
            self._read_alloc_file()
        )

        # ── Derived ────────────────────────────────────────────
        fill_rate = _safe_div(fill_count_24h, orders_24h)
        global_fill_rate_1h = _safe_div(fills_1h, orders_1h)

        net_loss_24h = max(0.0, fill_cost_24h - unwind_revenue_24h)
        avg_loss_per_fill = _safe_div(net_loss_24h, fill_count_24h)
        net_profit_24h = actual_reward_24h - net_loss_24h

        # Raw efficiency (unweighted) — used for snapshot + as the anchor
        # for PART 5's recency-weighted blend.
        raw_reward_efficiency = _safe_div(actual_reward_24h, capital_deployed)
        profit_efficiency = _safe_div(net_profit_24h, capital_deployed)
        # FIX 1 — capital-normalized loss. 5% daily burn is a structural
        # signal independent of per-fill magnitude. None when no capital.
        loss_per_capital = _safe_div(net_loss_24h, capital_deployed)

        # PART 1 — reward growth: current reward (most recent day) minus
        # trailing 3-day average of the 3 prior days. Requires 4 rows.
        reward_growth: Optional[float] = None
        if len(rows_daily) >= 4:
            cur_daily = float(rows_daily[0][1] or 0.0)
            prior_3 = [float(r[1] or 0.0) for r in rows_daily[1:4]]
            reward_growth = cur_daily - (sum(prior_3) / 3.0)

        # FIX 4 — per-market efficiency map. Only include markets that
        # had real capital deployed (can't compute efficiency for $0).
        market_efficiency_map: dict = {}
        for mid, cap in market_capital_map.items():
            if cap and cap > 0:
                reward = market_rewards_map.get(mid, 0.0)
                market_efficiency_map[mid] = reward / cap

        # FIX 2 — adaptive baseline: snapshot today's RAW efficiency and
        # compute median over the trailing BASELINE_WINDOW_DAYS days. We
        # snapshot the raw value (not the recency-weighted one) so the
        # baseline isn't a recursive function of prior baselines.
        reward_efficiency_baseline = self._update_and_read_baseline(
            today_key=today,
            reward_efficiency=raw_reward_efficiency,
            now_ts=now,
        )

        # PART 5 — recency-weighted efficiency.
        #     reward_efficiency = 0.7 * today_raw + 0.3 * mean(prior_days)
        # When no prior history is available, we fall back to the raw
        # value so downstream rules still operate (cold-start). When even
        # raw is unavailable (no capital), the metric is None.
        prior_avg_eff: Optional[float] = None
        try:
            db = _connect_db(self.db_path)
            prior_rows = db.execute(
                "SELECT reward_efficiency FROM learning_efficiency_daily "
                "WHERE date != ? ORDER BY date DESC LIMIT ?",
                (today, BASELINE_WINDOW_DAYS),
            ).fetchall()
            db.close()
            if prior_rows:
                prior_avg_eff = sum(
                    float(r[0]) for r in prior_rows
                ) / len(prior_rows)
        except Exception as e:
            log.debug(f"[LEARNING] prior snapshot fetch failed: {e}")

        if raw_reward_efficiency is None:
            reward_efficiency: Optional[float] = None
        elif prior_avg_eff is not None:
            reward_efficiency = (
                RECENCY_WEIGHT * raw_reward_efficiency
                + (1.0 - RECENCY_WEIGHT) * prior_avg_eff
            )
        else:
            # Graceful cold-start: no prior history, report the raw value
            # rather than None so the cycle can still produce an update.
            reward_efficiency = raw_reward_efficiency

        # Prediction-error ratios
        reward_error: Optional[float] = None
        if predicted_reward_24h is not None and predicted_reward_24h > 0:
            reward_error = actual_reward_24h / predicted_reward_24h

        predicted_loss_24h: Optional[float] = (
            PREDICTED_LOSS_PER_FILL_BASELINE * fill_count_24h
            if fill_count_24h > 0 else None
        )
        actual_loss_24h: Optional[float] = (
            net_loss_24h if fill_count_24h > 0 else None
        )
        loss_error: Optional[float] = None
        if avg_loss_per_fill is not None and PREDICTED_LOSS_PER_FILL_BASELINE > 0:
            loss_error = avg_loss_per_fill / PREDICTED_LOSS_PER_FILL_BASELINE

        out.update({
            # Profitability
            "net_profit": net_profit_24h,
            "total_rewards": actual_reward_24h,
            "total_loss": net_loss_24h,
            # Efficiency
            "capital_deployed": capital_deployed,
            "reward_efficiency": reward_efficiency,
            "profit_efficiency": profit_efficiency,
            "reward_efficiency_baseline": reward_efficiency_baseline,  # FIX 2
            "reward_efficiency_raw": raw_reward_efficiency,            # PART 5
            "reward_growth": reward_growth,                            # PART 1
            # Fill behavior
            "fill_count": fill_count_24h,
            "avg_loss_per_fill": avg_loss_per_fill,
            "fill_rate": fill_rate,
            "loss_per_capital": loss_per_capital,  # FIX 1
            # Prediction error
            "predicted_reward": predicted_reward_24h,
            "predicted_loss": predicted_loss_24h,
            "actual_reward": actual_reward_24h,
            "actual_loss": actual_loss_24h,
            "reward_error": reward_error,
            "loss_error": loss_error,
            # PATCH 4 PART 2 — explicit 24h key consumed by update_state.
            "actual_reward_24h": actual_reward_24h,
            # Regime signals
            "global_fill_rate_1h": global_fill_rate_1h,
            "volatility_proxy": volatility_proxy,
            # Per-market signal (FIX 4)
            "market_efficiency_map": market_efficiency_map,
            # Gate inputs
            "fills_total": fills_total,
            "fill_unwind_pairs_total": pairs_total,
            "reward_days": reward_days,
        })

        # PATCH 5 PART 1 — regime identifier. Coarse-grained bucket
        # derived from the 1h fill rate and the recency-weighted
        # efficiency. Rounded to keep regime bins stable despite small
        # noise. None when either input is missing — downstream logic
        # falls back to cold-start paths.
        _gfr_for_regime = (
            global_fill_rate_1h if global_fill_rate_1h is not None
            else fill_rate
        )
        if _gfr_for_regime is not None and reward_efficiency is not None:
            out["regime_id"] = (
                round(float(_gfr_for_regime), 1),
                round(float(reward_efficiency), 3),
            )
        else:
            out["regime_id"] = None
        # FIX 6 — valid_cycle boolean: whether this cycle's metrics are
        # complete enough for the decision logic to run. Controller uses
        # this to decide whether to increment valid_cycles_observed.
        out["valid_cycle"] = LearningController._metrics_complete(out)
        return out

    def _update_and_read_baseline(
        self,
        today_key: str,
        reward_efficiency: Optional[float],
        now_ts: float,
    ) -> Optional[float]:
        """FIX 2 — snapshot today's reward_efficiency, return median of the
        trailing BASELINE_WINDOW_DAYS. Returns None when we have fewer than
        BASELINE_MIN_DAYS samples (fail-closed: rules skip the efficiency
        path rather than anchor on a single noisy day)."""
        try:
            db = _connect_db(self.db_path)
            db.execute(
                "CREATE TABLE IF NOT EXISTS learning_efficiency_daily ("
                "date TEXT PRIMARY KEY, "
                "reward_efficiency REAL NOT NULL, "
                "captured_at REAL NOT NULL)"
            )
            if reward_efficiency is not None:
                db.execute(
                    "INSERT INTO learning_efficiency_daily "
                    "(date, reward_efficiency, captured_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(date) DO UPDATE SET "
                    "reward_efficiency=excluded.reward_efficiency, "
                    "captured_at=excluded.captured_at",
                    (today_key, float(reward_efficiency), float(now_ts)),
                )
                db.commit()
            rows = db.execute(
                "SELECT reward_efficiency FROM learning_efficiency_daily "
                "ORDER BY date DESC LIMIT ?", (BASELINE_WINDOW_DAYS,),
            ).fetchall()
            db.close()
        except Exception as e:
            log.warning(f"[LEARNING] baseline snapshot failed: {e}")
            return None

        if len(rows) < BASELINE_MIN_DAYS:
            return None
        vals = sorted(float(r[0]) for r in rows)
        n = len(vals)
        if n % 2:
            return vals[n // 2]
        return (vals[n // 2 - 1] + vals[n // 2]) / 2.0

    def _read_alloc_file(self) -> tuple[Optional[float], float, dict]:
        """Returns (predicted_reward_24h, capital_deployed, per_market_capital).

        predicted_reward is None when file missing, unparseable, or has
        zero deploy entries — caller treats this as "cannot compute
        reward_error" rather than "predicted zero".

        per_market_capital: {condition_id: est_capital_cost} for deploy
        entries only. Used by FIX 4 to compute per-market efficiency.
        """
        try:
            with open(self.alloc_path, "r") as f:
                alloc = json.load(f)
        except FileNotFoundError:
            return None, 0.0, {}
        except Exception as e:
            log.warning(f"[LEARNING] alloc file parse failed: {e}")
            return None, 0.0, {}

        if isinstance(alloc, dict):
            items = alloc.get("allocations", [])
        elif isinstance(alloc, list):
            items = alloc
        else:
            items = []

        pred_reward = 0.0
        cap_deployed = 0.0
        per_market_capital: dict = {}
        n_deploy = 0
        for a in items:
            if not isinstance(a, dict):
                continue
            if a.get("action") != "deploy":
                continue
            n_deploy += 1
            dr = float(a.get("daily_rate") or 0.0)
            qp = float(a.get("q_share_pct") or 0.0)
            cap = float(a.get("est_capital_cost") or 0.0)
            cid = a.get("condition_id")
            pred_reward += dr * (qp / 100.0)
            cap_deployed += cap
            if cid:
                per_market_capital[cid] = per_market_capital.get(cid, 0.0) + cap

        if n_deploy == 0:
            return None, 0.0, {}
        return pred_reward, cap_deployed, per_market_capital


# ═══════════════════════════════════════════════════════════════
# STEP 3 — DECISION LOGIC & CONTROLLER
# ═══════════════════════════════════════════════════════════════

class LearningController:
    """Orchestrates load → compute → decide → persist → apply.

    Persists a single row (id=1) in the `learning_state` table. Safe to
    instantiate every cycle — _ensure_table is idempotent.
    """

    TABLE_NAME = "learning_state"

    def __init__(self, db_path: str, alloc_path: str = "market_allocations.json"):
        self.db_path = db_path
        self.alloc_path = alloc_path
        self.metrics_engine = LearningMetrics(db_path, alloc_path)
        self._ensure_table()

    # ── Persistence ────────────────────────────────────────────

    def _ensure_table(self) -> None:
        try:
            db = _connect_db(self.db_path)
            db.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    id                     INTEGER PRIMARY KEY,
                    mode                   TEXT NOT NULL,
                    aggressiveness         REAL NOT NULL,
                    capital_scale          REAL NOT NULL,
                    risk_multiplier        REAL NOT NULL,
                    reward_trust           REAL NOT NULL,
                    valid_cycles_observed  INTEGER NOT NULL,
                    updated_at             REAL NOT NULL,
                    last_probe_cycle       INTEGER NOT NULL DEFAULT 0,
                    prev_reward_efficiency REAL,
                    best_reward            REAL NOT NULL DEFAULT 0.0,
                    best_capital_scale     REAL NOT NULL DEFAULT 1.0,
                    frontier_memory        TEXT
                )"""
            )
            # FIX 6 + PATCH 3 + PATCH 4 migrations — ALTER TABLE for any
            # legacy schema. All migrations are idempotent no-ops when
            # the new columns already exist.
            try:
                cols = db.execute(
                    f"PRAGMA table_info({self.TABLE_NAME})"
                ).fetchall()
                col_names = [c[1] for c in cols]
                if ("cycles_observed" in col_names
                        and "valid_cycles_observed" not in col_names):
                    db.execute(
                        f"ALTER TABLE {self.TABLE_NAME} "
                        f"RENAME COLUMN cycles_observed "
                        f"TO valid_cycles_observed"
                    )
                if "last_probe_cycle" not in col_names:
                    db.execute(
                        f"ALTER TABLE {self.TABLE_NAME} "
                        f"ADD COLUMN last_probe_cycle INTEGER NOT NULL DEFAULT 0"
                    )
                if "prev_reward_efficiency" not in col_names:
                    db.execute(
                        f"ALTER TABLE {self.TABLE_NAME} "
                        f"ADD COLUMN prev_reward_efficiency REAL"
                    )
                # PATCH 4 PART 9: (legacy) frontier-memory columns.
                # Retained for legacy schemas; unused by Patch 5.
                if "best_reward" not in col_names:
                    db.execute(
                        f"ALTER TABLE {self.TABLE_NAME} "
                        f"ADD COLUMN best_reward REAL NOT NULL DEFAULT 0.0"
                    )
                if "best_capital_scale" not in col_names:
                    db.execute(
                        f"ALTER TABLE {self.TABLE_NAME} "
                        f"ADD COLUMN best_capital_scale REAL NOT NULL DEFAULT 1.0"
                    )
                # PATCH 5 PART 3 — frontier_memory TEXT (JSON-encoded).
                if "frontier_memory" not in col_names:
                    db.execute(
                        f"ALTER TABLE {self.TABLE_NAME} "
                        f"ADD COLUMN frontier_memory TEXT"
                    )
            except Exception as mig_e:
                log.warning(f"[LEARNING] column migration skipped: {mig_e}")
            db.commit()
            db.close()
        except Exception as e:
            log.warning(f"[LEARNING] table init failed: {e}")

    def load_state(self) -> LearningState:
        try:
            db = _connect_db(self.db_path)
            row = db.execute(
                f"SELECT aggressiveness, capital_scale, risk_multiplier, "
                f"reward_trust, valid_cycles_observed, updated_at, mode, "
                f"last_probe_cycle, prev_reward_efficiency, "
                f"frontier_memory "
                f"FROM {self.TABLE_NAME} WHERE id = 1",
            ).fetchone()
            db.close()
        except Exception as e:
            log.warning(f"[LEARNING] load_state failed: {e}")
            return LearningState()
        if row is None:
            return LearningState()
        # PATCH 5 PART 4 — deserialize frontier_memory JSON; fail-closed
        # to empty dict on any parse error so the controller can recover
        # gracefully.
        frontier_memory = _deserialize_memory(
            row[9] if row[9] is not None else ""
        )
        return LearningState(
            aggressiveness=float(row[0]),
            capital_scale=float(row[1]),
            risk_multiplier=float(row[2]),
            reward_trust=float(row[3]),
            valid_cycles_observed=int(row[4]),
            updated_at=float(row[5]),
            mode=str(row[6] or MODE_OFF),
            # market_efficiency_map is not persisted — it's recomputed
            # each cycle from fresh metrics. Load returns it empty.
            market_efficiency_map={},
            last_probe_cycle=int(row[7] or 0),
            prev_reward_efficiency=(
                float(row[8]) if row[8] is not None else None
            ),
            frontier_memory=frontier_memory,
            # PATCH 11 — inject the module-level history cache so
            # _detect_oscillation sees past capital_scale values across
            # cycles within the same process.
            capital_history=list(_CAPITAL_HISTORY_CACHE),
        )

    def persist_state(self, state: LearningState, mode: str) -> None:
        try:
            db = _connect_db(self.db_path)
            # PATCH 5 PART 4 + PART 8 — prune before serialize so disk
            # state is always bounded at FRONTIER_MEMORY_MAX_SIZE.
            pruned = _prune_memory(state.frontier_memory)
            memory_json = _serialize_memory(pruned)
            db.execute(
                f"INSERT INTO {self.TABLE_NAME} "
                f"(id, mode, aggressiveness, capital_scale, risk_multiplier, "
                f"reward_trust, valid_cycles_observed, updated_at, "
                f"last_probe_cycle, prev_reward_efficiency, "
                f"frontier_memory) "
                f"VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                f"ON CONFLICT(id) DO UPDATE SET "
                f"mode=excluded.mode, "
                f"aggressiveness=excluded.aggressiveness, "
                f"capital_scale=excluded.capital_scale, "
                f"risk_multiplier=excluded.risk_multiplier, "
                f"reward_trust=excluded.reward_trust, "
                f"valid_cycles_observed=excluded.valid_cycles_observed, "
                f"updated_at=excluded.updated_at, "
                f"last_probe_cycle=excluded.last_probe_cycle, "
                f"prev_reward_efficiency=excluded.prev_reward_efficiency, "
                f"frontier_memory=excluded.frontier_memory",
                (
                    mode,
                    float(state.aggressiveness),
                    float(state.capital_scale),
                    float(state.risk_multiplier),
                    float(state.reward_trust),
                    int(state.valid_cycles_observed),
                    float(state.updated_at),
                    int(state.last_probe_cycle),
                    (
                        float(state.prev_reward_efficiency)
                        if state.prev_reward_efficiency is not None else None
                    ),
                    memory_json,
                ),
            )
            db.commit()
            db.close()
        except Exception as e:
            log.warning(f"[LEARNING] persist_state failed: {e}")

        # PATCH 11 — refresh the module-level history cache so the next
        # load_state() in this process picks up the new sample. Kept
        # outside the try/except above so a DB-write failure doesn't
        # silently drop the in-memory trace.
        global _CAPITAL_HISTORY_CACHE
        _CAPITAL_HISTORY_CACHE = list(state.capital_history)[-CAPITAL_HISTORY_MAX:]

    # ── Pure decision function (no I/O) ────────────────────────

    @staticmethod
    def _metrics_complete(m: dict) -> bool:
        """STEP 7 fail-closed gate — all inputs that drive rules must exist."""
        if m.get("status") != "ok":
            return False
        required = [
            "net_profit", "total_rewards", "total_loss",
            "fill_count", "fill_rate", "avg_loss_per_fill",
            "reward_efficiency", "global_fill_rate_1h",
        ]
        for k in required:
            if m.get(k) is None:
                return False
        return True

    @staticmethod
    def update_state(
        metrics: dict, prev: LearningState, is_probe: bool = False,
    ) -> LearningState:
        """STEP 3 — deterministic rule application + EMA + clamp.

        Pure function: no I/O, no wall-clock reads except for the final
        `updated_at` stamp.

        PATCH 3 additions:
          PART 2 — reward-growth expansion. After Rule B, if
            reward_growth > 0 and efficiency >= 0.7 * baseline, push
            capital + aggressiveness up; if growth < 0 and efficiency
            below baseline, pull capital down.
          PART 3 — when `is_probe` is True, multiply the raw (pre-EMA)
            capital_scale by PROBE_SCALE. EMA and clamp then apply as
            usual — the probe is NEVER a clamp bypass.
          PART 6 — efficiency_delta = cur - prev_reward_efficiency; when
            < EFFICIENCY_DELTA_COLLAPSE, pull capital down.
        """
        u_aggr = prev.aggressiveness
        u_cap = prev.capital_scale
        u_risk = prev.risk_multiplier
        u_trust = prev.reward_trust

        fr = metrics.get("fill_rate") or 0.0
        apf = metrics.get("avg_loss_per_fill") or 0.0
        np_ = metrics.get("net_profit") or 0.0
        cur_eff_raw = metrics.get("reward_efficiency")
        re_ = cur_eff_raw if cur_eff_raw is not None else 0.0
        rerr = metrics.get("reward_error")
        lerr = metrics.get("loss_error")
        gfr = metrics.get("global_fill_rate_1h") or 0.0
        # FIX 1 — capital-normalized loss signal (None when no capital).
        lpc = metrics.get("loss_per_capital")
        # FIX 2 — adaptive efficiency target (None when < 3 days history).
        target_eff = metrics.get("reward_efficiency_baseline")
        # PART 1 — reward growth vs trailing 3-day average.
        reward_growth = metrics.get("reward_growth")
        # PART 6 — efficiency delta vs prior cycle.
        if (prev.prev_reward_efficiency is not None
                and cur_eff_raw is not None):
            eff_delta = cur_eff_raw - prev.prev_reward_efficiency
        else:
            eff_delta = None

        # ── PATCH 5 PART 5: REGIME-SPECIFIC FRONTIER UPDATE ────
        # Each regime bucket holds its own (best_reward, best_capital_scale).
        # An improvement updates only the current regime's entry, so
        # a fill-heavy high-efficiency regime cannot overwrite the
        # anchor of a slow quiet regime, and vice versa.
        regime_id = metrics.get("regime_id")
        current_reward = metrics.get("actual_reward_24h")
        # Copy so we don't mutate prev (dataclass dict is shared ref).
        memory: dict = dict(prev.frontier_memory)
        if regime_id is not None and current_reward is not None:
            entry = memory.get(regime_id)
            if entry is None or current_reward > float(
                entry.get("best_reward", 0.0)
            ):
                memory[regime_id] = {
                    "best_reward": float(current_reward),
                    "best_capital_scale": float(prev.capital_scale),
                    "last_updated": time.time(),
                }
        # The entry for THIS regime (may be the one we just wrote).
        regime_entry = (
            memory.get(regime_id) if regime_id is not None else None
        )

        # ── Rule Group A: AGGRESSION ───────────────────────────
        # FIX 1: loss condition is (loss_per_capital high) OR (per-fill high)
        loss_high = (
            (lpc is not None and lpc > LOSS_PER_CAPITAL_HIGH)
            or apf > LOSS_PER_FILL_HIGH
        )
        if fr > FILL_RATE_HIGH and loss_high and np_ <= 0:
            u_aggr *= AGGR_DOWN
            u_risk *= RISK_UP
        # FIX 3: reward-first positive scaling — efficiency drives aggression
        # up, regardless of profit sign. When baseline missing, skip.
        elif target_eff is not None and cur_eff_raw is not None and re_ > target_eff:
            u_aggr *= AGGR_UP

        # ── Rule Group B: CAPITAL EFFICIENCY ──────────────────
        # FIX 2+3: binary decision against adaptive baseline. Skip
        # entirely when baseline OR current efficiency is unknown.
        if target_eff is not None and cur_eff_raw is not None:
            if re_ < target_eff:
                u_cap *= CAP_DOWN
            elif re_ > target_eff:
                u_cap *= CAP_UP

        # ── PATCH 6 PART 1: SAFE EXPANSION TRIGGER ────────────
        # Fill+loss regime rule (independent of reward baseline). When
        # both signals say "idle with no damage", expand. When both say
        # "active and bleeding", tighten. Neutral otherwise. Both inputs
        # must be present (None → skip, fail-closed).
        fill_rate_raw = metrics.get("fill_rate")
        loss_per_capital_raw = metrics.get("loss_per_capital")
        if fill_rate_raw is not None and loss_per_capital_raw is not None:
            if (fill_rate_raw < SAFE_FILL_RATE
                    and loss_per_capital_raw < SAFE_LOSS_PER_CAPITAL):
                u_cap *= EXPANSION_SCALE_UP
                u_aggr *= SAFE_EXPANSION_AGGR_UP
            elif (fill_rate_raw > SAFE_FILL_RATE * 2
                    and loss_per_capital_raw > SAFE_LOSS_PER_CAPITAL):
                u_cap *= EXPANSION_SCALE_DOWN

        # ── PART 2: REWARD-GROWTH EXPANSION ───────────────────
        # Reward-maximization pressure: when rewards are growing AND
        # efficiency is at least EXPANSION_EFFICIENCY_FLOOR_FRAC of the
        # baseline, push capital and aggressiveness up. When rewards are
        # falling AND efficiency dropped below baseline, pull capital
        # down. Skipped entirely when reward_growth or baseline is None.
        #
        # PATCH 5 PART 6 — regime-specific frontier_limit.
        # When this regime has an entry, expansion ceiling is
        # entry.best_capital_scale * FRONTIER_LIMIT_MULT. When the
        # regime is unseen (cold start), use a conservative 1.10× of
        # the current capital_scale so we don't aggressively expand
        # into an untested bucket.
        if regime_entry is not None:
            frontier_limit = (
                float(regime_entry.get("best_capital_scale", 1.0))
                * FRONTIER_LIMIT_MULT
            )
        else:
            frontier_limit = prev.capital_scale * COLD_START_FRONTIER_MULT

        if (reward_growth is not None
                and target_eff is not None
                and cur_eff_raw is not None):
            if (reward_growth > 0
                    and re_ >= EXPANSION_EFFICIENCY_FLOOR_FRAC * target_eff):
                if prev.capital_scale < frontier_limit:
                    u_cap *= FRONTIER_EXPANSION_CAP_UP
                u_aggr *= EXPANSION_AGGR_UP
            if reward_growth < 0 and re_ < target_eff:
                u_cap *= EXPANSION_CAP_DOWN

        # ── PART 6: FRONTIER OVERSHOOT ────────────────────────
        # If efficiency dropped significantly from the prior cycle, we
        # pushed past the capacity frontier — pull capital back.
        if eff_delta is not None and eff_delta < EFFICIENCY_DELTA_COLLAPSE:
            u_cap *= EFFICIENCY_DELTA_COLLAPSE_CAP

        # ── PATCH 4 PART 7: SHARP-COLLAPSE CORRECTION ─────────
        # When efficiency_delta is severely negative, apply a stronger
        # correction ON TOP of the Part 6 multiplier. Combined effect
        # when both fire: 0.90 × 0.85 = 0.765× before EMA + clamp +
        # min_floor guard.
        if (eff_delta is not None
                and eff_delta < EFFICIENCY_DELTA_SHARP_COLLAPSE):
            u_cap *= EFFICIENCY_DELTA_SHARP_CAP

        # ── Rule Group C: MODEL CORRECTION ─────────────────────
        if rerr is not None:
            if rerr < REWARD_ERROR_OVERESTIMATE:
                u_trust *= TRUST_DOWN
            elif REWARD_ERROR_HEALTHY_LO <= rerr <= REWARD_ERROR_HEALTHY_HI:
                u_trust *= TRUST_UP

        if lerr is not None:
            if lerr > LOSS_ERROR_UNDERESTIMATE:
                u_risk *= RISK_UP
            elif LOSS_ERROR_HEALTHY_LO <= lerr <= LOSS_ERROR_HEALTHY_HI:
                u_risk *= RISK_DOWN

        # ── Rule Group D: GLOBAL REGIME ───────────────────────
        if gfr > GLOBAL_FILL_RATE_HIGH:
            u_aggr *= AGGR_DOWN
            u_cap *= CAP_DOWN

        # ── PART 3: FRONTIER PROBE ────────────────────────────
        # Applied to the pre-EMA capital scale so the probe is smoothed
        # (not a step change) and CANNOT bypass the clamp below.
        #
        # PATCH 4 PART 6: probe strength scales with current capital
        # commitment. Formula: 1.05 + 0.05 * min(1, capital_scale).
        # At cap_scale = 1.0+: strength = 1.10 (same as legacy).
        # At cap_scale = 0.5:  strength = 1.075 (gentler probe when small).
        # Encourages larger probes only when the system has earned
        # them, and avoids shocking an already-contracted portfolio.
        if is_probe:
            probe_strength = (
                PROBE_STRENGTH_BASE
                + PROBE_STRENGTH_CAP_COEF * min(1.0, prev.capital_scale)
            )
            u_cap *= probe_strength

        # ── PATCH 5 PART 10: CONTROLLED AGGRESSIVE SPIKE ──────
        # Small probability of a one-cycle 1.20× push on the raw u_cap.
        # Gated on regime_id != None so cold-start cycles never spike.
        # Still goes through EMA + clamp below.
        if (regime_id is not None
                and random.random() < REGIME_SPIKE_PROBABILITY):
            u_cap *= REGIME_SPIKE_CAP_UP

        # ── PATCH 5 PART 7: REGIME-SPECIFIC MIN_FLOOR GUARD ───
        # Floor tied to THIS regime's best_capital_scale so contraction
        # in a seen regime can't forget the commitment that earned its
        # best reward. Unseen regime → fall back to the hard clamp
        # lower bound (no memory of commitment to anchor against).
        if regime_entry is not None:
            min_floor = max(
                CLAMP_CAP[0],
                float(regime_entry.get("best_capital_scale", 1.0))
                * FRONTIER_MIN_FLOOR_FRAC,
            )
        else:
            min_floor = CLAMP_CAP[0]
        u_cap = max(u_cap, min_floor)

        # ── PATCH 11: OSCILLATION DAMPING ─────────────────────
        # Applied AFTER the min_floor guard and BEFORE EMA smoothing —
        # this way the damping nudges the raw u_cap the same way other
        # rules do, and the EMA + clamp pass still applies on top.
        # Fires only when prev has accumulated ≥ OSCILLATION_WINDOW
        # samples AND the last window shows ≥ OSCILLATION_THRESHOLD
        # direction flips. In-memory window — on a fresh process it
        # takes 20 cycles (≈ 10h at 30-min cadence) before damping can
        # engage.
        if len(prev.capital_history) >= OSCILLATION_WINDOW:
            recent = prev.capital_history[-OSCILLATION_WINDOW:]
            if _detect_oscillation(recent):
                u_cap *= OSCILLATION_DAMPEN_FACTOR

        # ── FIX 5: reward_trust mean reversion (after rules, before EMA) ─
        # Each cycle pull trust 2% of the distance toward the neutral 1.0.
        # When trust < 1.0 this is a small upward pressure that prevents
        # the system from staying pessimistic forever.
        u_trust += TRUST_REVERSION_RATE * (1.0 - u_trust)

        # ── EMA smoothing (STEP 5) ────────────────────────────
        a = EMA_ALPHA
        new_aggr = a * u_aggr + (1 - a) * prev.aggressiveness
        new_cap = a * u_cap + (1 - a) * prev.capital_scale
        new_risk = a * u_risk + (1 - a) * prev.risk_multiplier
        new_trust = a * u_trust + (1 - a) * prev.reward_trust

        # ── Clamp (STEP 3 hard constraints) ───────────────────
        new_cap_clamped = _clamp(new_cap, *CLAMP_CAP)
        return LearningState(
            aggressiveness=_clamp(new_aggr, *CLAMP_AGGR),
            capital_scale=new_cap_clamped,
            risk_multiplier=_clamp(new_risk, *CLAMP_RISK),
            reward_trust=_clamp(new_trust, *CLAMP_TRUST),
            # FIX 6 — increments only when called (i.e. only on valid cycles)
            valid_cycles_observed=prev.valid_cycles_observed + 1,
            updated_at=time.time(),
            mode=prev.mode,
            market_efficiency_map=prev.market_efficiency_map,
            # Preserve probe counter (step() updates it when a probe fires).
            last_probe_cycle=prev.last_probe_cycle,
            # PART 6: stash current raw efficiency for next cycle's delta.
            # Snapshot the raw (pre-weight) value so delta is comparable
            # to the raw-based baseline.
            prev_reward_efficiency=metrics.get(
                "reward_efficiency_raw", cur_eff_raw,
            ),
            # PATCH 5 PART 2/8 — carry the (possibly updated, possibly
            # pruned) regime memory forward.
            frontier_memory=_prune_memory(memory),
            # PATCH 11 — append the post-clamp value; bounded at
            # CAPITAL_HISTORY_MAX so memory never unboundedly grows.
            capital_history=(
                list(prev.capital_history) + [new_cap_clamped]
            )[-CAPITAL_HISTORY_MAX:],
        )

    # ── Full cycle ─────────────────────────────────────────────

    def step(self) -> LearningStep:
        """One learning cycle.

        Return contract:
          - mode            : OFF | SHADOW | ACTIVE
          - applied_state   : SAFE to apply to allocator/calibrator.
                              Neutral (all 1.0) scalars in OFF and SHADOW
                              with empty market_efficiency_map. Computed
                              state with populated map in ACTIVE (or prev
                              if metrics incomplete — fail-closed).
                              The `mode` field always reflects the real
                              gate decision so the allocator can gate
                              exploration on it.
          - computed_state  : the rule output, for observability/diff log.
          - metrics         : the full metric vector.

        FIX 6: valid_cycles_observed increments ONLY on cycles where
        metrics_ok is True. A half-broken cycle does not count toward
        gate promotion.
        """
        prev = self.load_state()
        metrics = self.metrics_engine.compute_metrics(
            valid_cycles_observed=prev.valid_cycles_observed,
        )

        mode = LearningGate.evaluate_activation({
            "fills_total": metrics.get("fills_total", 0),
            "fill_unwind_pairs_total": metrics.get("fill_unwind_pairs_total", 0),
            "reward_days": metrics.get("reward_days", 0),
            "valid_cycles_observed": prev.valid_cycles_observed,
        })

        log.info(
            f"[LEARNING_MODE] mode={mode} "
            f"fills={metrics.get('fills_total', 0)} "
            f"rewards_days={metrics.get('reward_days', 0)} "
            f"valid_cycles={prev.valid_cycles_observed}"
        )

        # Neutral "apply" state always carries the real mode so the
        # allocator can decide what's gated on mode (e.g. exploration).
        def _neutral(mode_: str) -> LearningState:
            return LearningState(mode=mode_)

        # ── OFF ─────────────────────────────────────────────
        if mode == MODE_OFF:
            self._log_cycle(metrics, _neutral(mode), mode)
            return LearningStep(
                mode=mode,
                applied_state=_neutral(mode),
                computed_state=prev,
                metrics=metrics,
            )

        metrics_ok = bool(metrics.get("valid_cycle", False))

        # ── SHADOW ──────────────────────────────────────────
        if mode == MODE_SHADOW:
            # FIX 6: only increment the valid counter when metrics were
            # complete. Broken/incomplete cycles don't count toward the
            # 50-cycle ACTIVE promotion threshold.
            # PATCH 11: preserve prev.capital_history so persist_state's
            # cache refresh writes the baseline trace (and a later
            # update_state success further advances the cache explicitly).
            counter_only = LearningState(
                aggressiveness=prev.aggressiveness,
                capital_scale=prev.capital_scale,
                risk_multiplier=prev.risk_multiplier,
                reward_trust=prev.reward_trust,
                valid_cycles_observed=(
                    prev.valid_cycles_observed + (1 if metrics_ok else 0)
                ),
                updated_at=time.time(),
                mode=mode,
                market_efficiency_map={},
                capital_history=list(prev.capital_history),
            )
            # Persist FIRST (matches pre-Patch-11 ordering) — if
            # update_state below raises, the counter + scalars still land.
            self.persist_state(counter_only, mode)

            if metrics_ok:
                computed = self.update_state(metrics, prev)
                # PATCH 11: explicitly advance the module cache so the
                # damping window warms up during SHADOW and is ready the
                # moment the gate promotes to ACTIVE. persist_state
                # above already refreshed the cache from
                # counter_only.capital_history (= prev); this overwrites
                # with the post-update trace.
                global _CAPITAL_HISTORY_CACHE
                _CAPITAL_HISTORY_CACHE = list(
                    computed.capital_history,
                )[-CAPITAL_HISTORY_MAX:]
                log.info(
                    f"[LEARNING_SHADOW] would_apply "
                    f"aggr {prev.aggressiveness:.3f}→{computed.aggressiveness:.3f} "
                    f"cap {prev.capital_scale:.3f}→{computed.capital_scale:.3f} "
                    f"risk {prev.risk_multiplier:.3f}→{computed.risk_multiplier:.3f} "
                    f"trust {prev.reward_trust:.3f}→{computed.reward_trust:.3f}"
                )
            else:
                log.warning(
                    "[LEARNING_SHADOW] metrics incomplete — counter NOT incremented"
                )
                computed = prev

            self._log_cycle(metrics, _neutral(mode), mode)
            return LearningStep(
                mode=mode,
                applied_state=_neutral(mode),
                computed_state=computed,
                metrics=metrics,
            )

        # ── ACTIVE ──────────────────────────────────────────
        if not metrics_ok:
            log.warning(
                "[LEARNING_ACTIVE] metrics incomplete — holding previous state"
            )
            # FIX 6: do NOT increment counter here; prev is reused as-is.
            # Carry the current mode through so allocator sees ACTIVE and
            # can still enable exploration on the prev scalars.
            # PATCH 11: preserve prev.capital_history so the module-level
            # damping cache isn't wiped by a single incomplete-metrics
            # cycle.
            held = LearningState(
                aggressiveness=prev.aggressiveness,
                capital_scale=prev.capital_scale,
                risk_multiplier=prev.risk_multiplier,
                reward_trust=prev.reward_trust,
                valid_cycles_observed=prev.valid_cycles_observed,
                updated_at=prev.updated_at,
                mode=mode,
                market_efficiency_map={},
                capital_history=list(prev.capital_history),
            )
            self._log_cycle(metrics, held, mode)
            return LearningStep(
                mode=mode,
                applied_state=held,
                computed_state=held,
                metrics=metrics,
            )

        # PART 3 — frontier probe scheduling. A probe fires when (a) we're
        # in ACTIVE, (b) the current cycle is at least PROBE_INTERVAL
        # valid cycles past the last probe. The new counter value is
        # prev.valid_cycles_observed + 1 (only valid cycles reach here).
        #
        # PATCH 4 PART 5: also require STABILITY — recency-weighted
        # efficiency must be within PROBE_STABILITY_DELTA of the prior
        # raw efficiency. Probing an unstable system would amplify the
        # instability; we only push the frontier when the last cycle's
        # signal was settled.
        new_counter = prev.valid_cycles_observed + 1
        cadence_ok = (new_counter - prev.last_probe_cycle) >= PROBE_INTERVAL
        cur_eff = metrics.get("reward_efficiency")
        is_stable = (
            cur_eff is not None
            and prev.prev_reward_efficiency is not None
            and abs(cur_eff - prev.prev_reward_efficiency)
                < PROBE_STABILITY_DELTA
        )
        is_probe = cadence_ok and is_stable
        metrics["is_probe_cycle"] = is_probe

        cap_scale_before = prev.capital_scale
        computed = self.update_state(metrics, prev, is_probe=is_probe)
        if is_probe:
            # Stamp the counter value at which the probe fired so the
            # next probe is gated PROBE_INTERVAL cycles from here.
            computed.last_probe_cycle = computed.valid_cycles_observed

        # Attach live per-market efficiency, mode, and PART 4 scalars
        # into the applied state. These ride ONLY with the ACTIVE path —
        # OFF/SHADOW leave them empty so the allocator applies no
        # per-market ranking, dynamic exploration, or probing.
        computed.mode = mode
        computed.market_efficiency_map = dict(
            metrics.get("market_efficiency_map") or {}
        )
        computed.reward_efficiency = metrics.get("reward_efficiency")
        computed.reward_efficiency_baseline = metrics.get(
            "reward_efficiency_baseline",
        )
        self.persist_state(computed, mode)
        self._log_cycle(metrics, computed, mode)
        # PART 7 — expansion-specific log line.
        self._log_expansion(metrics, cap_scale_before, computed, is_probe)
        # PATCH 6 PART 5 — safe-expansion observability line.
        self._log_patch6(metrics, computed)
        return LearningStep(
            mode=mode,
            applied_state=computed,
            computed_state=computed,
            metrics=metrics,
        )

    # ── STEP 8 logging ─────────────────────────────────────────

    def _log_cycle(self, m: dict, s: LearningState, mode: str) -> None:
        def _f(x, fmt=".4f"):
            if x is None:
                return "nan"
            return format(x, fmt)

        log.info(
            f"[LEARNING] mode={mode} "
            f"profit=${_f(m.get('net_profit'), fmt='.2f')} "
            f"reward=${_f(m.get('total_rewards'), fmt='.2f')} "
            f"efficiency={_f(m.get('reward_efficiency'))} "
            f"aggr={s.aggressiveness:.3f} "
            f"capital={s.capital_scale:.3f} "
            f"risk={s.risk_multiplier:.3f} "
            f"trust={s.reward_trust:.3f}"
        )

    def _log_expansion(
        self, m: dict, cap_before: float, s: LearningState, is_probe: bool,
    ) -> None:
        """Expansion log line. Patch 3 fields + Patch 5 regime fields."""
        def _f(x, fmt=".4f"):
            if x is None:
                return "nan"
            return format(x, fmt)

        regime_id = m.get("regime_id")
        entry = (
            s.frontier_memory.get(regime_id) if regime_id is not None else None
        )
        if entry is not None:
            regime_best_reward = float(entry.get("best_reward", 0.0))
            regime_best_cap = float(entry.get("best_capital_scale", 1.0))
        else:
            regime_best_reward = 0.0
            regime_best_cap = 1.0

        log.info(
            f"[LEARNING_EXPANSION] "
            f"reward_growth={_f(m.get('reward_growth'), fmt='.2f')} "
            f"efficiency={_f(m.get('reward_efficiency'))} "
            f"baseline={_f(m.get('reward_efficiency_baseline'))} "
            f"capital_scale_before={cap_before:.3f} "
            f"capital_scale_after={s.capital_scale:.3f} "
            f"is_probe_cycle={bool(is_probe)} "
            f"regime_id={regime_id} "
            f"regime_best_reward={regime_best_reward:.2f} "
            f"regime_best_capital_scale={regime_best_cap:.3f}"
        )

    def _log_patch6(self, m: dict, s: LearningState) -> None:
        """PATCH 6 PART 5 — safe-expansion observability. Emits the raw
        (None-safe) fill_rate and loss_per_capital that drive Rule E, plus
        the post-clamp capital_scale."""
        def _f(x, fmt=".4f"):
            if x is None:
                return "nan"
            return format(x, fmt)

        log.info(
            f"[LEARNING_P6] "
            f"fill_rate={_f(m.get('fill_rate'))} "
            f"loss_per_capital={_f(m.get('loss_per_capital'))} "
            f"capital_scale={s.capital_scale:.3f}"
        )
