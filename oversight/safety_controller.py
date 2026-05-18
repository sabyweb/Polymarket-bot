"""SafetyController — non-bypassable safety layer for reward farming.

6-state machine with graduated response:
  CALIBRATED              — full deployment permitted
  MILDLY_MISCALIBRATED    — conservative deployment, minor CF drift
  SEVERELY_MISCALIBRATED  — reduced deployment, significant CF error
  DEGRADED                — minimal deployment, operational issues
  DATA_UNAVAILABLE        — near-probe deployment, data pipeline broken
  UNSAFE                  — probe only (3 markets, min-size, 5% capital)

Invariant priority system:
  CRITICAL — proven risk only → UNSAFE; query failure → DEGRADED
  HIGH     — calibration/loss issues → SEVERELY_MISCALIBRATED or DEGRADED
  MEDIUM   — data quality → DATA_UNAVAILABLE or DEGRADED
  LOW      — informational/assistive → DEGRADED (+ capital haircut)

Rule: UNSAFE triggers ONLY when data EXISTS and PROVES risk.
Missing data → DATA_UNAVAILABLE or DEGRADED, never UNSAFE.
CF drift alone cannot trigger UNSAFE — requires corroboration from
losses AND est/actual ratio.
"""

import logging
import sqlite3
import time
from dataclasses import dataclass

from .data_collector import _connect_db, _query_with_retry

log = logging.getLogger("oversight.safety")


# ═══════════════════════════════════════════════════════════════════════════
# SYSTEM STATES
# ═══════════════════════════════════════════════════════════════════════════

CALIBRATED = "CALIBRATED"
MILDLY_MISCALIBRATED = "MILDLY_MISCALIBRATED"
SEVERELY_MISCALIBRATED = "SEVERELY_MISCALIBRATED"
DEGRADED = "DEGRADED"
DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
UNSAFE = "UNSAFE"

LEARNING = MILDLY_MISCALIBRATED

STATE_SEVERITY = {
    CALIBRATED: 0,
    MILDLY_MISCALIBRATED: 1,
    SEVERELY_MISCALIBRATED: 2,
    DEGRADED: 3,
    DATA_UNAVAILABLE: 4,
    UNSAFE: 5,
}

ALL_STATES = set(STATE_SEVERITY.keys())

_UPGRADE_ORDER = [
    UNSAFE, DATA_UNAVAILABLE, DEGRADED,
    SEVERELY_MISCALIBRATED, MILDLY_MISCALIBRATED, CALIBRATED,
]

_STATE_ALIASES = {"LEARNING": MILDLY_MISCALIBRATED}


def _map_state(name: str) -> str:
    if name in ALL_STATES:
        return name
    return _STATE_ALIASES.get(name, MILDLY_MISCALIBRATED)


# ═══════════════════════════════════════════════════════════════════════════
# INVARIANT PRIORITY LEVELS
# ═══════════════════════════════════════════════════════════════════════════

PRIORITY_CRITICAL = "CRITICAL"
PRIORITY_HIGH = "HIGH"
PRIORITY_MEDIUM = "MEDIUM"
PRIORITY_LOW = "LOW"

PRIORITY_ORDER = {
    PRIORITY_CRITICAL: 3,
    PRIORITY_HIGH: 2,
    PRIORITY_MEDIUM: 1,
    PRIORITY_LOW: 0,
}


# ═══════════════════════════════════════════════════════════════════════════
# STATE PERMISSIONS
# ═══════════════════════════════════════════════════════════════════════════

STATE_PERMISSIONS = {
    CALIBRATED: {
        "max_markets": 60, "capital_pct": 1.0, "trials": True,
    },
    MILDLY_MISCALIBRATED: {
        "max_markets": 40, "capital_pct": 0.70, "trials": True,
    },
    SEVERELY_MISCALIBRATED: {
        "max_markets": 20, "capital_pct": 0.40, "trials": False,
    },
    DEGRADED: {
        "max_markets": 10, "capital_pct": 0.20, "trials": False,
    },
    DATA_UNAVAILABLE: {
        "max_markets": 5, "capital_pct": 0.10, "trials": False,
    },
    # UNSAFE: mandatory probe mode — prevents deadlock.
    UNSAFE: {
        "max_markets": 3, "capital_pct": 0.05, "trials": False,
        "probe_mode": True, "min_size_only": True,
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# INVARIANT THRESHOLDS
#
# ╔════╦═══════════════════╦══════════╦════════════════════╦═══════════════════════════════╗
# ║ ID ║ Invariant          ║ Priority ║ Threshold          ║ Breach → State                ║
# ╠════╬═══════════════════╬══════════╬════════════════════╬═══════════════════════════════╣
# ║  1 ║ daily_loss         ║ CRITICAL ║ $150               ║ UNSAFE (data) / DEGRADED (NA) ║
# ║  2 ║ slow_bleed_7d      ║ CRITICAL ║ $500               ║ UNSAFE (data) / DEGRADED (NA) ║
# ║  3 ║ drawdown           ║ CRITICAL ║ 15%                ║ UNSAFE (data) / DATA_UNAVAIL  ║
# ║  4 ║ capital_floor      ║ CRITICAL ║ $50                ║ UNSAFE / DATA_UNAVAIL / UNSAFE║
# ║  5 ║ cf_drift           ║ HIGH     ║ 0.005/0.02/0.03    ║ SEVERE / SEVERE / MILD        ║
# ║ 5b ║ cf_corroborated    ║ CRITICAL ║ CF+est+loss agree  ║ UNSAFE                        ║
# ║  6 ║ est_actual         ║ HIGH     ║ 50×/15×            ║ SEVERE / SEVERE               ║
# ║  7 ║ hourly_loss        ║ HIGH     ║ $60/$30            ║ DEGRADED / DEGRADED           ║
# ║  8 ║ capital_at_risk    ║ HIGH     ║ 90%/80%            ║ DEGRADED / DEGRADED           ║
# ║  9 ║ data_freshness     ║ MEDIUM   ║ None/2h/30m        ║ DATA_UNAVAIL / DATA / MILD    ║
# ║ 10 ║ data_completeness  ║ MEDIUM   ║ 50%/80%            ║ DATA_UNAVAIL / DEGRADED       ║
# ║ 11 ║ loss_reward        ║ HIGH     ║ 2.0×/1.5×          ║ SEVERE / MILD                 ║
# ║ 12 ║ clob_rate_drop     ║ MEDIUM   ║ −30%               ║ DEGRADED                      ║
# ║ 13 ║ fill_storm         ║ LOW      ║ query_fail/≥1      ║ DEGRADED (+ 20% haircut)      ║
# ║ 14 ║ cf_at_floor        ║ LOW      ║ ≥3 cycles          ║ SEVERE (+ 10% haircut)        ║
# ╚════╩═══════════════════╩══════════╩════════════════════╩═══════════════════════════════╝
# ═══════════════════════════════════════════════════════════════════════════

# ── Loss limits ──
MAX_DAILY_LOSS_USD = 150.0
MAX_HOURLY_LOSS_USD = 30.0
SLOW_BLEED_7D_USD = 500.0

# ── Loss/reward ratio ──
LOSS_REWARD_RATIO_SEVERE = 2.0
LOSS_REWARD_RATIO_MILD = 1.5

# ── Portfolio drawdown ──
MAX_DRAWDOWN_PCT = 0.15

# ── Capital protection ──
CAPITAL_FLOOR_USD = 50.0
MAX_CAPITAL_AT_RISK_PCT = 0.80
MAX_CAPITAL_AT_RISK_UNSAFE_PCT = 0.90

# ── Per-market exposure ──
MAX_PER_MARKET_EXPOSURE_USD = 200.0

# ── Correction factor ──
CF_CIRCUIT_BREAK = 0.005
CF_SEVERE_LOW = 0.02
CF_MILD_LOW = 0.03
CF_CALIBRATED_LOW = 0.05
CF_CALIBRATED_HIGH = 3.0
CF_CORROBORATION_LOSS_USD = 50.0  # losses required to corroborate CF drift

# ── Est/actual ratio ──
EST_ACTUAL_UNSAFE = 50.0
EST_ACTUAL_SEVERE = 15.0
EST_ACTUAL_CALIBRATED = 5.0

# ── Data quality ──
DATA_STALE_WARN_SECS = 1800
DATA_STALE_CRITICAL_SECS = 7200
DATA_COMPLETENESS_WARN = 0.80
DATA_COMPLETENESS_CRITICAL = 0.50

# ── CLOB rate change ──
CLOB_RATE_DROP_THRESHOLD = -0.30

# ── Q-share ──
Q_SHARE_MAX = 0.5

# ── Fill storm sentinel lookback ──
FILL_STORM_LOOKBACK_SECS = 3600

# ── Upgrade thresholds ──
UPGRADE_TO_CALIBRATED = 3
UPGRADE_STEP = 2

# ── UNSAFE auto-recovery ──
UNSAFE_RECOVERY_CYCLES = 3

# ── LOW signal capital haircuts ──
FILL_STORM_HAIRCUT = 0.20    # 20% capital reduction
CF_AT_FLOOR_HAIRCUT = 0.10   # 10% capital reduction
MAX_LOW_HAIRCUT = 0.50       # cap at 50%


# ═══════════════════════════════════════════════════════════════════════════
# VIOLATION
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Violation:
    invariant: str
    priority: str
    severity: str
    value: float
    threshold: float
    message: str


# ═══════════════════════════════════════════════════════════════════════════
# SAFETY CONTROLLER
# ═══════════════════════════════════════════════════════════════════════════

class SafetyController:

    def __init__(self, db_path: str = "bot_history.db"):
        self.db_path = db_path
        self.state = MILDLY_MISCALIBRATED
        self.consecutive_good = 0
        self._last_state_change = time.time()
        self._last_violations: list[Violation] = []
        self._portfolio_peak: float = 0.0
        self._unsafe_no_critical_count: int = 0

        self._ensure_tables()
        self._load_state()
        self._load_portfolio_peak()

    def _ensure_tables(self):
        try:
            db = _connect_db(self.db_path)
            db.execute(
                """CREATE TABLE IF NOT EXISTS safety_state (
                    id               INTEGER PRIMARY KEY,
                    ts               REAL NOT NULL,
                    state            TEXT NOT NULL,
                    reason           TEXT NOT NULL DEFAULT '',
                    consecutive_good INTEGER NOT NULL DEFAULT 0
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts               REAL NOT NULL,
                    total_value      REAL NOT NULL,
                    exchange_balance REAL NOT NULL DEFAULT 0,
                    locked_capital   REAL NOT NULL DEFAULT 0,
                    peak_value       REAL NOT NULL DEFAULT 0
                )"""
            )
            db.commit()
            db.close()
        except Exception as e:
            log.warning(f"Safety table init failed: {e}")

    # ═══════════════════════════════════════════════════
    # PER-VIOLATION LOGGING
    # ═══════════════════════════════════════════════════

    def _log_violation(self, v: Violation):
        level = (
            logging.WARNING if v.priority == PRIORITY_CRITICAL
            else logging.INFO if v.priority in (PRIORITY_HIGH, PRIORITY_MEDIUM)
            else logging.DEBUG
        )
        log.log(
            level,
            f"VIOLATION: {v.invariant} | PRIORITY={v.priority} | "
            f"value={v.value} | threshold={v.threshold} | -> {v.severity}",
        )

    # ═══════════════════════════════════════════════════
    # STATE EVALUATION
    # ═══════════════════════════════════════════════════

    def evaluate_state(
        self,
        correction_factor_raw: float,
        estimated_daily_total: float,
        actual_daily_payout: float,
        reward_payout_24h: float,
        num_scoring_markets: int,
        data_completeness: float = 1.0,
        clob_rate_delta_pct: float = 0.0,
        cf_at_floor_cycles: int = 0,
        exchange_balance: float = 0.0,
        total_portfolio_value: float = 0.0,
        fill_damage_24h: float | None = None,
        fill_damage_7d: float | None = None,
    ) -> str:
        violations: list[Violation] = []

        # ── Pre-compute shared values ──────────────────────────────
        _fd24 = fill_damage_24h if fill_damage_24h is not None else self._query_fill_damage(hours=24)

        est_actual_ratio = 0.0
        if actual_daily_payout > 0 and estimated_daily_total > 0:
            est_actual_ratio = estimated_daily_total / actual_daily_payout

        # ── I1: Daily loss (CRITICAL) ──────────────────────────────
        if _fd24 is None:
            violations.append(Violation(
                "daily_loss", PRIORITY_CRITICAL, DEGRADED, 0, MAX_DAILY_LOSS_USD,
                "Cannot compute 24h fill damage — DB query failed",
            ))
        elif _fd24 > MAX_DAILY_LOSS_USD:
            violations.append(Violation(
                "daily_loss", PRIORITY_CRITICAL, UNSAFE, _fd24, MAX_DAILY_LOSS_USD,
                f"24h fill damage ${_fd24:.0f} > ${MAX_DAILY_LOSS_USD}",
            ))

        # ── I2: Slow bleed (CRITICAL) ─────────────────────────────
        _fd7d = fill_damage_7d if fill_damage_7d is not None else self._query_fill_damage(hours=168)
        if _fd7d is None:
            violations.append(Violation(
                "slow_bleed_7d", PRIORITY_CRITICAL, DEGRADED, 0, SLOW_BLEED_7D_USD,
                "Cannot compute 7d fill damage — DB query failed",
            ))
        elif _fd7d > SLOW_BLEED_7D_USD:
            violations.append(Violation(
                "slow_bleed_7d", PRIORITY_CRITICAL, UNSAFE, _fd7d, SLOW_BLEED_7D_USD,
                f"7d loss ${_fd7d:.0f} > ${SLOW_BLEED_7D_USD}",
            ))

        # ── I3: Drawdown (CRITICAL) with portfolio fallback ───────
        _portfolio_val = total_portfolio_value
        if _portfolio_val <= 0:
            _portfolio_val = exchange_balance
        if _portfolio_val <= 0:
            # Cold-start branch (FX-002): no orders ever placed, no fills observed,
            # so by definition no drawdown can have occurred. Skip I3 silently — the
            # DATA_UNAVAILABLE violation here is what locks fresh-DB bootstraps in
            # the state machine while the wallet read propagates (§4.14 chicken-and-
            # egg). Once the bot places its first order ever, this branch never
            # fires again.
            if self._is_genuine_cold_start():
                log.info("I3 skipped on genuine cold start (no portfolio history yet)")
            else:
                violations.append(Violation(
                    "drawdown", PRIORITY_CRITICAL, DATA_UNAVAILABLE,
                    0, MAX_DRAWDOWN_PCT,
                    "Portfolio value unavailable — no fallback",
                ))
        else:
            self._update_portfolio_peak(_portfolio_val, exchange_balance)
            if self._portfolio_peak > 0:
                drawdown = (self._portfolio_peak - _portfolio_val) / self._portfolio_peak
                if drawdown > MAX_DRAWDOWN_PCT:
                    violations.append(Violation(
                        "drawdown", PRIORITY_CRITICAL, UNSAFE,
                        drawdown, MAX_DRAWDOWN_PCT,
                        f"Drawdown {drawdown:.1%} > {MAX_DRAWDOWN_PCT:.0%} "
                        f"(peak=${self._portfolio_peak:.0f}, now=${_portfolio_val:.0f})",
                    ))

        # ── I4: Capital floor (CRITICAL) ──────────────────────────
        # balance > 0 but < floor → UNSAFE (proven).
        # balance <= 0: check history to distinguish API failure vs real loss.
        if exchange_balance <= 0:
            last_known = self._query_last_known_balance()
            if last_known is not None:
                violations.append(Violation(
                    "capital_floor", PRIORITY_CRITICAL, UNSAFE,
                    exchange_balance, CAPITAL_FLOOR_USD,
                    f"Balance dropped to $0 (was ${last_known:.0f}) — sustained zero",
                ))
            else:
                violations.append(Violation(
                    "capital_floor", PRIORITY_CRITICAL, DATA_UNAVAILABLE,
                    exchange_balance, CAPITAL_FLOOR_USD,
                    "Exchange balance unavailable — no recent history",
                ))
        elif exchange_balance < CAPITAL_FLOOR_USD:
            violations.append(Violation(
                "capital_floor", PRIORITY_CRITICAL, UNSAFE,
                exchange_balance, CAPITAL_FLOOR_USD,
                f"Balance ${exchange_balance:.0f} < floor ${CAPITAL_FLOOR_USD}",
            ))

        # ── I5: CF drift (HIGH — model-derived, not ground truth) ─
        if correction_factor_raw > 0:
            if correction_factor_raw < CF_CIRCUIT_BREAK:
                violations.append(Violation(
                    "cf_drift", PRIORITY_HIGH, SEVERELY_MISCALIBRATED,
                    correction_factor_raw, CF_CIRCUIT_BREAK,
                    f"CF {correction_factor_raw:.6f} < {CF_CIRCUIT_BREAK}",
                ))
            elif correction_factor_raw < CF_SEVERE_LOW:
                violations.append(Violation(
                    "cf_drift", PRIORITY_HIGH, SEVERELY_MISCALIBRATED,
                    correction_factor_raw, CF_SEVERE_LOW,
                    f"CF {correction_factor_raw:.4f} < {CF_SEVERE_LOW}",
                ))
            elif correction_factor_raw < CF_MILD_LOW:
                violations.append(Violation(
                    "cf_drift", PRIORITY_HIGH, MILDLY_MISCALIBRATED,
                    correction_factor_raw, CF_MILD_LOW,
                    f"CF {correction_factor_raw:.4f} < {CF_MILD_LOW}",
                ))

        # ── I5b: Corroborated CF drift (CRITICAL) ────────────────
        # CF alone is model-derived. Only escalate to UNSAFE when
        # CF + est/actual + actual losses ALL agree.
        if (correction_factor_raw > 0
                and correction_factor_raw < CF_CIRCUIT_BREAK
                and est_actual_ratio > EST_ACTUAL_SEVERE
                and _fd24 is not None
                and _fd24 > CF_CORROBORATION_LOSS_USD):
            violations.append(Violation(
                "cf_corroborated", PRIORITY_CRITICAL, UNSAFE,
                correction_factor_raw, CF_CIRCUIT_BREAK,
                f"CF drift corroborated: CF={correction_factor_raw:.6f}, "
                f"est/actual={est_actual_ratio:.0f}x, losses=${_fd24:.0f}",
            ))

        # ── I6: Est/actual ratio (HIGH) ───────────────────────────
        if est_actual_ratio > EST_ACTUAL_UNSAFE:
            violations.append(Violation(
                "est_actual", PRIORITY_HIGH, SEVERELY_MISCALIBRATED,
                est_actual_ratio, EST_ACTUAL_UNSAFE,
                f"Est/actual {est_actual_ratio:.0f}x > {EST_ACTUAL_UNSAFE}x",
            ))
        elif est_actual_ratio > EST_ACTUAL_SEVERE:
            violations.append(Violation(
                "est_actual", PRIORITY_HIGH, SEVERELY_MISCALIBRATED,
                est_actual_ratio, EST_ACTUAL_SEVERE,
                f"Est/actual {est_actual_ratio:.0f}x > {EST_ACTUAL_SEVERE}x",
            ))

        # ── I7: Hourly loss (HIGH) ────────────────────────────────
        _fd1h = self._query_fill_damage(hours=1)
        if _fd1h is None:
            violations.append(Violation(
                "hourly_loss", PRIORITY_HIGH, DEGRADED,
                0, MAX_HOURLY_LOSS_USD,
                "Cannot compute 1h fill damage — DB query failed",
            ))
        elif _fd1h > MAX_HOURLY_LOSS_USD * 2:
            violations.append(Violation(
                "hourly_loss", PRIORITY_HIGH, DEGRADED,
                _fd1h, MAX_HOURLY_LOSS_USD * 2,
                f"1h damage ${_fd1h:.0f} > ${MAX_HOURLY_LOSS_USD * 2}",
            ))
        elif _fd1h > MAX_HOURLY_LOSS_USD:
            violations.append(Violation(
                "hourly_loss", PRIORITY_HIGH, DEGRADED,
                _fd1h, MAX_HOURLY_LOSS_USD,
                f"1h damage ${_fd1h:.0f} > ${MAX_HOURLY_LOSS_USD}",
            ))

        # ── I8: Capital at risk (HIGH) ────────────────────────────
        _at_risk_pct = 0.0
        if _portfolio_val > 0:
            _at_risk_pct = max(0.0, 1.0 - (exchange_balance / _portfolio_val))
        if _at_risk_pct > MAX_CAPITAL_AT_RISK_UNSAFE_PCT:
            violations.append(Violation(
                "capital_at_risk", PRIORITY_HIGH, DEGRADED,
                _at_risk_pct, MAX_CAPITAL_AT_RISK_UNSAFE_PCT,
                f"Capital at risk {_at_risk_pct:.0%} > {MAX_CAPITAL_AT_RISK_UNSAFE_PCT:.0%}",
            ))
        elif _at_risk_pct > MAX_CAPITAL_AT_RISK_PCT:
            violations.append(Violation(
                "capital_at_risk", PRIORITY_HIGH, DEGRADED,
                _at_risk_pct, MAX_CAPITAL_AT_RISK_PCT,
                f"Capital at risk {_at_risk_pct:.0%} > {MAX_CAPITAL_AT_RISK_PCT:.0%}",
            ))

        # ── I9: Data freshness (MEDIUM) ───────────────────────────
        _data_age = self._query_data_freshness()
        if _data_age is None:
            violations.append(Violation(
                "data_freshness", PRIORITY_MEDIUM, DATA_UNAVAILABLE,
                0, 0, "Cannot determine data freshness",
            ))
        elif _data_age > DATA_STALE_CRITICAL_SECS:
            violations.append(Violation(
                "data_freshness", PRIORITY_MEDIUM, DATA_UNAVAILABLE,
                _data_age, DATA_STALE_CRITICAL_SECS,
                f"Data age {_data_age/3600:.1f}h > {DATA_STALE_CRITICAL_SECS/3600:.0f}h",
            ))
        elif _data_age > DATA_STALE_WARN_SECS:
            violations.append(Violation(
                "data_freshness", PRIORITY_MEDIUM, MILDLY_MISCALIBRATED,
                _data_age, DATA_STALE_WARN_SECS,
                f"Data age {_data_age/60:.0f}m > {DATA_STALE_WARN_SECS/60:.0f}m",
            ))

        # ── I10: Data completeness (MEDIUM) ───────────────────────
        if data_completeness < DATA_COMPLETENESS_CRITICAL:
            violations.append(Violation(
                "data_completeness", PRIORITY_MEDIUM, DATA_UNAVAILABLE,
                data_completeness, DATA_COMPLETENESS_CRITICAL,
                f"Completeness {data_completeness:.0%} < {DATA_COMPLETENESS_CRITICAL:.0%}",
            ))
        elif data_completeness < DATA_COMPLETENESS_WARN:
            violations.append(Violation(
                "data_completeness", PRIORITY_MEDIUM, DEGRADED,
                data_completeness, DATA_COMPLETENESS_WARN,
                f"Completeness {data_completeness:.0%} < {DATA_COMPLETENESS_WARN:.0%}",
            ))

        # ── I11: Loss/reward ratio (HIGH) ─────────────────────────
        if reward_payout_24h > 0 and _fd24 is not None and _fd24 > 0:
            _lr = _fd24 / reward_payout_24h
            if _lr > LOSS_REWARD_RATIO_SEVERE:
                violations.append(Violation(
                    "loss_reward", PRIORITY_HIGH, SEVERELY_MISCALIBRATED,
                    _lr, LOSS_REWARD_RATIO_SEVERE,
                    f"Loss/reward {_lr:.1f}x > {LOSS_REWARD_RATIO_SEVERE}x",
                ))
            elif _lr > LOSS_REWARD_RATIO_MILD:
                violations.append(Violation(
                    "loss_reward", PRIORITY_HIGH, MILDLY_MISCALIBRATED,
                    _lr, LOSS_REWARD_RATIO_MILD,
                    f"Loss/reward {_lr:.1f}x > {LOSS_REWARD_RATIO_MILD}x",
                ))

        # ── I12: CLOB rate drop (MEDIUM) ──────────────────────────
        if clob_rate_delta_pct < CLOB_RATE_DROP_THRESHOLD:
            violations.append(Violation(
                "clob_rate_drop", PRIORITY_MEDIUM, DEGRADED,
                clob_rate_delta_pct, CLOB_RATE_DROP_THRESHOLD,
                f"CLOB rates dropped {clob_rate_delta_pct:.0%}",
            ))

        # ── I13: Fill storm (LOW + capital haircut) ───────────────
        _storms = self._query_recent_fill_storms()
        if _storms is None:
            violations.append(Violation(
                "fill_storm", PRIORITY_LOW, DEGRADED, 0, 0,
                "Cannot query fill storms — DB query failed",
            ))
        elif _storms > 0:
            violations.append(Violation(
                "fill_storm", PRIORITY_LOW, DEGRADED, float(_storms), 0,
                f"{_storms} fill storm(s) in last hour",
            ))

        # ── I14: CF at floor (LOW + capital haircut) ──────────────
        if cf_at_floor_cycles >= 3:
            violations.append(Violation(
                "cf_at_floor", PRIORITY_LOW, SEVERELY_MISCALIBRATED,
                float(cf_at_floor_cycles), 3,
                f"CF at floor for {cf_at_floor_cycles} cycles",
            ))

        # ──────────────────────────────────────────
        # LOG EACH VIOLATION
        # ──────────────────────────────────────────
        for v in violations:
            self._log_violation(v)

        self._last_violations = violations

        # ──────────────────────────────────────────
        # UNSAFE AUTO-RECOVERY TRACKING
        # Requires: no CRITICAL-UNSAFE AND valid data
        # ──────────────────────────────────────────
        critical_unsafe = [
            v for v in violations
            if v.priority == PRIORITY_CRITICAL and v.severity == UNSAFE
        ]
        _has_valid_data = (
            (_data_age is not None and _data_age < DATA_STALE_CRITICAL_SECS)
            or actual_daily_payout > 0
        )
        if self.state == UNSAFE:
            if not critical_unsafe and _has_valid_data:
                self._unsafe_no_critical_count += 1
            elif critical_unsafe:
                self._unsafe_no_critical_count = 0
            # else: no valid data → don't increment (stay cautious)
        else:
            self._unsafe_no_critical_count = 0

        # ──────────────────────────────────────────
        # DETERMINE STATE USING PRIORITY SYSTEM
        # ──────────────────────────────────────────

        if violations:
            by_priority: dict[str, list[Violation]] = {}
            for v in violations:
                by_priority.setdefault(v.priority, []).append(v)

            highest_prio = max(
                by_priority.keys(),
                key=lambda p: PRIORITY_ORDER[p],
            )
            worst_in_group = max(
                by_priority[highest_prio],
                key=lambda v: STATE_SEVERITY[v.severity],
            )
            target_state = worst_in_group.severity

            # UNSAFE auto-recovery cap
            if (self.state == UNSAFE
                    and self._unsafe_no_critical_count >= UNSAFE_RECOVERY_CYCLES
                    and STATE_SEVERITY.get(target_state, 0) > STATE_SEVERITY[DEGRADED]):
                target_state = DEGRADED
                log.info(
                    f"UNSAFE recovery: no CRITICAL-UNSAFE + valid data for "
                    f"{self._unsafe_no_critical_count} cycles -> capping at DEGRADED"
                )

            reasons = [v.message for v in by_priority[highest_prio]]
            self.consecutive_good = 0
            self._transition(target_state, reasons)
        else:
            if (self.state == UNSAFE
                    and self._unsafe_no_critical_count >= UNSAFE_RECOVERY_CYCLES):
                self._transition(
                    DEGRADED,
                    [f"UNSAFE recovery: no CRITICAL-UNSAFE for "
                     f"{self._unsafe_no_critical_count} cycles"],
                )
                self._unsafe_no_critical_count = 0
                self.consecutive_good = 0
            else:
                self._handle_upgrade(
                    num_scoring_markets, correction_factor_raw,
                    est_actual_ratio, _fd24 if _fd24 is not None else 0.0,
                    reward_payout_24h,
                )

        # Log confidence score
        log.info(f"Confidence score: {self.confidence_score:.2f}")

        return self.state

    # ── Backward-compatible wrapper ──

    def evaluate(
        self,
        correction_factor_raw: float,
        estimated_daily_total: float,
        actual_daily_payout: float,
        fill_damage_24h: float,
        reward_payout_24h: float,
        num_scoring_markets: int,
        cf_at_floor_cycles: int = 0,
        fill_damage_7d: float = 0.0,
        clob_rate_delta_pct: float = 0.0,
        data_completeness: float = 1.0,
        exchange_balance: float = 0.0,
        total_portfolio_value: float = 0.0,
    ) -> str:
        return self.evaluate_state(
            correction_factor_raw=correction_factor_raw,
            estimated_daily_total=estimated_daily_total,
            actual_daily_payout=actual_daily_payout,
            reward_payout_24h=reward_payout_24h,
            num_scoring_markets=num_scoring_markets,
            data_completeness=data_completeness,
            clob_rate_delta_pct=clob_rate_delta_pct,
            cf_at_floor_cycles=cf_at_floor_cycles,
            fill_damage_24h=fill_damage_24h,
            fill_damage_7d=fill_damage_7d,
            exchange_balance=exchange_balance,
            total_portfolio_value=total_portfolio_value,
        )

    # ═══════════════════════════════════════════════════
    # UPGRADE LOGIC
    # ═══════════════════════════════════════════════════

    def _handle_upgrade(self, num_scoring, cf_raw, est_actual_ratio,
                        fill_damage_24h, reward_24h):
        is_fully_calibrated = (
            (cf_raw == 0 or CF_CALIBRATED_LOW <= cf_raw <= CF_CALIBRATED_HIGH)
            and est_actual_ratio < EST_ACTUAL_CALIBRATED
            and num_scoring >= 5
            and fill_damage_24h <= max(reward_24h * 2, 50)
        )
        if is_fully_calibrated:
            self.consecutive_good += 1
            if self.state == CALIBRATED:
                return
            if self.state == MILDLY_MISCALIBRATED:
                if self.consecutive_good >= UPGRADE_TO_CALIBRATED:
                    self._transition(
                        CALIBRATED,
                        [f"Good for {self.consecutive_good} consecutive cycles"],
                    )
            else:
                if self.consecutive_good >= UPGRADE_STEP:
                    self._transition(
                        MILDLY_MISCALIBRATED,
                        [f"Upgrading from {self.state} after {self.consecutive_good} clean cycles"],
                    )
                    self.consecutive_good = 0
        else:
            self.consecutive_good = 0
            if self.state == CALIBRATED:
                self._transition(MILDLY_MISCALIBRATED,
                                 ["Calibration criteria no longer met"])

    # ═══════════════════════════════════════════════════
    # ALLOCATION FILTERING — FINAL GATE
    # Enforces probe mode + LOW signal capital haircuts
    # ═══════════════════════════════════════════════════

    def filter_allocations(self, allocations: list[dict],
                           available_capital: float) -> list[dict]:
        perms = STATE_PERMISSIONS[self.state]
        is_probe = perms.get("probe_mode", False)
        min_size_only = perms.get("min_size_only", False)
        max_capital = available_capital * perms["capital_pct"]

        # LOW signal dynamic capital haircut
        low_viols = [v for v in self._last_violations if v.priority == PRIORITY_LOW]
        low_haircut = 0.0
        for v in low_viols:
            if v.invariant == "fill_storm":
                low_haircut = max(low_haircut, FILL_STORM_HAIRCUT)
            elif v.invariant == "cf_at_floor":
                low_haircut = max(low_haircut, CF_AT_FLOOR_HAIRCUT)
        low_haircut = min(low_haircut, MAX_LOW_HAIRCUT)
        if low_haircut > 0:
            max_capital *= (1.0 - low_haircut)
            log.info(f"LOW signal haircut: {low_haircut:.0%} -> effective capital ${max_capital:.0f}")

        deploys = [a for a in allocations if a["action"] == "deploy"]
        deploys.sort(key=lambda a: a.get("score", 0), reverse=True)

        if not perms["trials"]:
            for a in deploys:
                if a.get("score", 0) <= 0:
                    a["action"] = "avoid"
                    a["shares_per_side"] = 0
                    a["reason"] = f"SafetyController: no trials in {self.state}"

        deploy_count = 0
        for a in allocations:
            if a["action"] != "deploy":
                continue
            deploy_count += 1
            if deploy_count > perms["max_markets"]:
                a["action"] = "avoid"
                a["shares_per_side"] = 0
                a["reason"] = f"SafetyController: market cap ({perms['max_markets']})"

        if min_size_only:
            for a in allocations:
                if a["action"] == "deploy":
                    min_sz = int(a.get("min_size", 50))
                    a["shares_per_side"] = min_sz
                    spread = a.get("max_spread", 0.045)
                    est_price = max(0.10, (1.0 - 2 * spread) / 2)
                    a["est_capital_cost"] = round(min_sz * est_price * 2, 2)
                    a["reason"] = f"PROBE: min_size={min_sz} (data collection only)"

        running_cost = 0.0
        for a in allocations:
            if a["action"] != "deploy":
                continue
            est_cost = a.get("est_capital_cost", 0)
            if est_cost <= 0:
                spread = a.get("max_spread", 0.045)
                est_price = max(0.10, (1.0 - 2 * spread) / 2)
                est_cost = a.get("shares_per_side", 50) * est_price * 2
            if running_cost + est_cost > max_capital:
                a["action"] = "avoid"
                a["shares_per_side"] = 0
                a["reason"] = f"SafetyController: capital cap ${max_capital:.0f}"
            else:
                running_cost += est_cost

        for a in allocations:
            if a["action"] == "deploy" and a.get("q_share_pct", 0) > Q_SHARE_MAX:
                a["q_share_pct"] = Q_SHARE_MAX

        for a in allocations:
            if a["action"] == "deploy":
                est_cost = a.get("est_capital_cost", 0)
                if est_cost > MAX_PER_MARKET_EXPOSURE_USD:
                    scale = MAX_PER_MARKET_EXPOSURE_USD / est_cost
                    a["shares_per_side"] = max(
                        int(a.get("min_size", 50)),
                        int(a["shares_per_side"] * scale),
                    )
                    spread = a.get("max_spread", 0.045)
                    est_price = max(0.10, (1.0 - 2 * spread) / 2)
                    a["est_capital_cost"] = round(a["shares_per_side"] * est_price * 2, 2)

        final_deploy = sum(1 for a in allocations if a["action"] == "deploy")
        final_capital = sum(
            a.get("est_capital_cost", 0) for a in allocations if a["action"] == "deploy"
        )
        if is_probe and final_deploy > 0:
            log.warning(
                f"SafetyController [PROBE MODE]: {final_deploy} probe markets, "
                f"${final_capital:.0f} capital (data collection only)"
            )
        elif final_deploy < len(deploys):
            log.info(
                f"SafetyController [{self.state}]: {final_deploy}/{len(deploys)} markets, "
                f"${final_capital:.0f}/${available_capital:.0f} capital"
            )

        return allocations

    # ═══════════════════════════════════════════════════
    # CONFIDENCE SCORE (V2 foundation)
    # ═══════════════════════════════════════════════════

    @property
    def confidence_score(self) -> float:
        """System confidence [0,1].

        Components:
          data_quality (0.40):       data_freshness + data_completeness
          cf_stability (0.30):       cf_drift / cf_corroborated severity
          payout_consistency (0.30): est_actual ratio
        """
        dq = 0.40
        cf = 0.30
        pc = 0.30

        for v in self._last_violations:
            if v.invariant in ("data_freshness", "data_completeness"):
                if STATE_SEVERITY.get(v.severity, 0) >= STATE_SEVERITY[DATA_UNAVAILABLE]:
                    dq = 0.0
                elif STATE_SEVERITY.get(v.severity, 0) >= STATE_SEVERITY[DEGRADED]:
                    dq = min(dq, 0.15)
                else:
                    dq = min(dq, 0.25)
            elif v.invariant in ("cf_drift", "cf_corroborated"):
                if v.severity == UNSAFE:
                    cf = 0.0
                elif STATE_SEVERITY.get(v.severity, 0) >= STATE_SEVERITY[SEVERELY_MISCALIBRATED]:
                    cf = min(cf, 0.10)
                else:
                    cf = min(cf, 0.20)
            elif v.invariant == "est_actual":
                if v.value > EST_ACTUAL_UNSAFE:
                    pc = 0.0
                elif v.value > EST_ACTUAL_SEVERE:
                    pc = min(pc, 0.10)

        return round(dq + cf + pc, 2)

    # ═══════════════════════════════════════════════════
    # QUERY HELPERS
    # ═══════════════════════════════════════════════════

    def _query_fill_damage(self, hours: float) -> float | None:
        cutoff = time.time() - hours * 3600
        try:
            db = _connect_db(self.db_path)
            fill_row = db.execute(
                "SELECT COALESCE(SUM(shares * clob_cost), 0) FROM fills WHERE ts > ?",
                (cutoff,),
            ).fetchone()
            dump_row = db.execute(
                "SELECT COALESCE(SUM(usd_value), 0) FROM unwinds WHERE ts > ?",
                (cutoff,),
            ).fetchone()
            stop_row = db.execute(
                "SELECT COALESCE(SUM(loss_usd), 0) FROM stop_losses WHERE ts > ?",
                (cutoff,),
            ).fetchone()
            db.close()
            return max(0.0,
                       (fill_row[0] if fill_row else 0)
                       - (dump_row[0] if dump_row else 0)
                       + (stop_row[0] if stop_row else 0))
        except Exception as e:
            log.warning(f"Fill damage query failed ({hours}h): {e}")
            return None

    def _query_data_freshness(self) -> float | None:
        row = _query_with_retry(
            self.db_path,
            "SELECT MAX(ts) FROM scoring_snapshots",
            fetch="one",
        )
        if row is None:
            return None
        latest_ts = row[0] if row else None
        if latest_ts is None or latest_ts == 0:
            # Bootstrap distinction: an empty scoring_snapshots can mean either
            #   (a) cold-start DB — no orders have ever been placed → freshness N/A,
            #   (b) orders exist but the scoring pipeline is broken → real failure.
            # On (a) treat as fresh so I9 doesn't deadlock the SafetyController in
            # DATA_UNAVAILABLE; on (b) preserve the defensive None. Once the bot
            # places its first order ever, _is_genuine_cold_start returns False and
            # this branch reverts to original behaviour.
            if self._is_genuine_cold_start():
                return 0.0
            return None
        return time.time() - latest_ts

    def _is_genuine_cold_start(self) -> bool:
        # True iff the bot has never placed an order AND has never observed a fill
        # in this DB's lifetime. Drives two cold-start branches:
        #   I9 data_freshness (FX-001) and I3 drawdown (FX-002).
        # Returns False on query failure — conservative default treats unknown
        # state as a warm DB so existing defences still fire.
        try:
            orders = _query_with_retry(
                self.db_path,
                "SELECT COUNT(*) FROM orders_placed",
                fetch="one",
            )
            if orders is None or orders[0] > 0:
                return False
            fills = _query_with_retry(
                self.db_path,
                "SELECT COUNT(*) FROM fills",
                fetch="one",
            )
            if fills is None or fills[0] > 0:
                return False
            return True
        except Exception:
            return False

    def _query_recent_fill_storms(self) -> int | None:
        cutoff = time.time() - FILL_STORM_LOOKBACK_SECS
        row = _query_with_retry(
            self.db_path,
            "SELECT COUNT(*) FROM fills WHERE condition_id = '__FILL_STORM__' AND ts > ?",
            (cutoff,),
            fetch="one",
        )
        if row is None:
            return None
        return row[0] if row[0] else 0

    def _query_last_known_balance(self) -> float | None:
        """Check portfolio_snapshots for recent positive balance.

        Returns the last known positive balance within 6h, or None.
        Distinguishes API failure (had money recently) from actual zero.
        """
        row = _query_with_retry(
            self.db_path,
            "SELECT exchange_balance FROM portfolio_snapshots "
            "WHERE ts > ? AND exchange_balance > ? "
            "ORDER BY ts DESC LIMIT 1",
            (time.time() - 6 * 3600, CAPITAL_FLOOR_USD),
            fetch="one",
        )
        return row[0] if row and row[0] else None

    def _compute_portfolio_value(self, exchange_balance: float) -> float:
        if exchange_balance <= 0:
            return 0.0
        locked = 0.0
        try:
            db = _connect_db(self.db_path)
            try:
                pos_row = db.execute(
                    "SELECT COALESCE(SUM(shares * avg_cost_per_share), 0) "
                    "FROM positions WHERE shares > 0"
                ).fetchone()
                locked += pos_row[0] if pos_row else 0
            except Exception:
                pass
            try:
                dump_row = db.execute(
                    "SELECT COALESCE(SUM(remaining_shares * target_price), 0) "
                    "FROM dump_states WHERE status IN ('aggressive', 'passive')"
                ).fetchone()
                locked += dump_row[0] if dump_row else 0
            except Exception:
                pass
            db.close()
        except Exception:
            pass
        return exchange_balance + locked

    def _update_portfolio_peak(self, total_value: float, exchange_balance: float):
        if total_value <= 0:
            return
        try:
            db = _connect_db(self.db_path)
            now = time.time()
            db.execute(
                "INSERT INTO portfolio_snapshots "
                "(ts, total_value, exchange_balance, locked_capital, peak_value) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, total_value, exchange_balance,
                 max(0, total_value - exchange_balance), self._portfolio_peak),
            )
            db.execute("DELETE FROM portfolio_snapshots WHERE ts < ?",
                       (now - 7 * 86400,))
            peak_row = db.execute(
                "SELECT MAX(total_value) FROM portfolio_snapshots WHERE ts > ?",
                (now - 7 * 86400,),
            ).fetchone()
            if peak_row and peak_row[0]:
                self._portfolio_peak = peak_row[0]
            else:
                self._portfolio_peak = total_value
            db.commit()
            db.close()
        except Exception as e:
            log.debug(f"Portfolio peak update failed: {e}")
            self._portfolio_peak = max(self._portfolio_peak, total_value)

    def _load_portfolio_peak(self):
        try:
            row = _query_with_retry(
                self.db_path,
                "SELECT MAX(total_value) FROM portfolio_snapshots WHERE ts > ?",
                (time.time() - 7 * 86400,),
                fetch="one",
            )
            if row and row[0]:
                self._portfolio_peak = row[0]
        except Exception:
            pass

    # ═══════════════════════════════════════════════════
    # STATE MANAGEMENT
    # ═══════════════════════════════════════════════════

    def _transition(self, new_state, reasons):
        if new_state == self.state:
            return
        old_state = self.state
        self.state = new_state
        self.consecutive_good = 0
        self._last_state_change = time.time()
        level = (
            logging.CRITICAL if new_state == UNSAFE
            else logging.WARNING if STATE_SEVERITY.get(new_state, 0) >= STATE_SEVERITY[DEGRADED]
            else logging.INFO
        )
        log.log(level,
                f"SAFETY STATE: {old_state} -> {new_state} | {'; '.join(reasons)}")
        self._persist_state(reasons)
        if STATE_SEVERITY.get(new_state, 0) >= STATE_SEVERITY[DEGRADED]:
            self._write_alert_file(old_state, new_state, reasons)
        elif new_state == CALIBRATED:
            self._clear_alert_file()

    def _persist_state(self, reasons=None):
        try:
            db = _connect_db(self.db_path)
            db.execute(
                """CREATE TABLE IF NOT EXISTS safety_state (
                    id INTEGER PRIMARY KEY, ts REAL NOT NULL,
                    state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                    consecutive_good INTEGER NOT NULL DEFAULT 0)""")
            db.execute(
                "INSERT INTO safety_state (ts, state, reason, consecutive_good) "
                "VALUES (?, ?, ?, ?)",
                (time.time(), self.state, "; ".join(reasons or []),
                 self.consecutive_good))
            db.execute(
                "DELETE FROM safety_state WHERE id NOT IN "
                "(SELECT id FROM safety_state ORDER BY ts DESC LIMIT 100)")
            db.commit()
            db.close()
        except Exception as e:
            log.debug(f"Safety state persist failed: {e}")

    def _load_state(self):
        self._unsafe_no_critical_count = 0
        try:
            db = _connect_db(self.db_path)
            db.execute(
                """CREATE TABLE IF NOT EXISTS safety_state (
                    id INTEGER PRIMARY KEY, ts REAL NOT NULL,
                    state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                    consecutive_good INTEGER NOT NULL DEFAULT 0)""")
            row = db.execute(
                "SELECT state, consecutive_good, ts FROM safety_state "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            db.close()
            if row:
                stored_state = _map_state(row[0])
                stored_good = row[1]
                age_hours = (time.time() - row[2]) / 3600
                if age_hours < 2:
                    self.state = stored_state
                    self.consecutive_good = max(0, stored_good - 1)
                elif age_hours < 6:
                    self.state = MILDLY_MISCALIBRATED
                    if STATE_SEVERITY.get(stored_state, 0) >= STATE_SEVERITY[DEGRADED]:
                        self.consecutive_good = 0
                    else:
                        self.consecutive_good = max(0, stored_good - 1)
                else:
                    self.state = MILDLY_MISCALIBRATED
                    self.consecutive_good = 0
            else:
                self.state = MILDLY_MISCALIBRATED
                self.consecutive_good = 0
        except Exception as e:
            log.debug(f"Safety state load failed: {e}")
            self.state = MILDLY_MISCALIBRATED

    def _write_alert_file(self, old_state, new_state, reasons):
        import os, datetime
        alert_path = os.path.join(
            os.path.dirname(self.db_path) or ".", "SAFETY_ALERT.txt")
        try:
            with open(alert_path, "w") as f:
                f.write(f"SAFETY ALERT — {new_state}\n")
                f.write(f"Time: {datetime.datetime.now().isoformat()}\n")
                f.write(f"Transition: {old_state} -> {new_state}\n")
                f.write(f"Confidence: {self.confidence_score:.2f}\n")
                f.write("Reasons:\n")
                for r in reasons:
                    f.write(f"  - {r}\n")
                if self._last_violations:
                    f.write("\nAll violations:\n")
                    for v in self._last_violations:
                        f.write(f"  [{v.priority}:{v.severity}] {v.invariant}: "
                                f"{v.message}\n")
            log.warning(f"Alert file written: {alert_path}")
        except Exception as e:
            log.debug(f"Alert file write failed: {e}")

    def _clear_alert_file(self):
        import os
        alert_path = os.path.join(
            os.path.dirname(self.db_path) or ".", "SAFETY_ALERT.txt")
        try:
            if os.path.exists(alert_path):
                os.remove(alert_path)
                log.info("Safety alert cleared — CALIBRATED")
        except Exception:
            pass

    # ═══════════════════════════════════════════════════
    # PUBLIC QUERY METHODS
    # ═══════════════════════════════════════════════════

    def query_24h_fill_damage(self) -> float:
        result = self._query_fill_damage(hours=24)
        return result if result is not None else 0.0

    def query_7d_fill_damage(self) -> float:
        result = self._query_fill_damage(hours=168)
        return result if result is not None else 0.0

    def count_scoring_markets(self, window_hours: float = 4.0) -> int:
        cutoff = time.time() - window_hours * 3600
        row = _query_with_retry(
            self.db_path,
            "SELECT COUNT(DISTINCT condition_id) FROM scoring_snapshots WHERE ts > ?",
            (cutoff,), fetch="one")
        return row[0] if row and row[0] else 0

    @property
    def violations(self) -> list[Violation]:
        return list(self._last_violations)
