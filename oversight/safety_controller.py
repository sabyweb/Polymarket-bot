"""SafetyController — sits above the oversight agent, enforces invariants.

Called AFTER the agent produces allocations but BEFORE they are written.
Can override, reduce, or zero out any allocation.

Design goals:
- Prevent catastrophic capital loss (priority 1)
- Maximize reward farming efficiency (priority 2)
- Never over-restrict in normal operation

The controller enforces a state machine with graduated response:
- CALIBRATED: full deployment permitted
- LEARNING: conservative deployment while model builds confidence
- DEGRADED: reduced deployment, data issues
- UNSAFE: zero deployment, circuit breaker active
"""

import logging
import sqlite3
import time

from .data_collector import _connect_db, _query_with_retry

log = logging.getLogger("oversight.safety")

# ── System states ──
CALIBRATED = "CALIBRATED"
LEARNING = "LEARNING"
DEGRADED = "DEGRADED"
UNSAFE = "UNSAFE"

# ── State permissions ──
# max_markets: hard cap on deploy count
# capital_pct: fraction of available capital usable
# trials: whether zero-data trial markets are allowed
STATE_PERMISSIONS = {
    CALIBRATED: {"max_markets": 60, "capital_pct": 1.0, "trials": True},
    LEARNING:   {"max_markets": 15, "capital_pct": 0.30, "trials": True},
    DEGRADED:   {"max_markets": 10, "capital_pct": 0.20, "trials": False},
    # GAP 2 FIX: UNSAFE allows 3 probe markets at 5% capital.
    # Without this, UNSAFE cancels all orders → no scoring data flows →
    # q_share never updates → system stays UNSAFE forever → deadlock.
    # Probe markets are min-size only, top 3 by score, purpose = data
    # collection. The 5% cap ($50 on a $1000 account) bounds probe risk.
    UNSAFE:     {"max_markets": 3,  "capital_pct": 0.05, "trials": False,
                 "probe_mode": True, "min_size_only": True},
}

# ── Thresholds ──
# Correction factor
CF_CIRCUIT_BREAK = 0.005        # raw CF < this → UNSAFE (estimates >200x reality)
CF_SEVERE_LOW = 0.02            # raw CF < this → DEGRADED
CF_CALIBRATED_LOW = 0.03        # raw CF must be above this for CALIBRATED
CF_CALIBRATED_HIGH = 3.0        # raw CF must be below this for CALIBRATED

# Estimate/actual ratio
EST_ACTUAL_UNSAFE = 50.0        # est/actual > 50x → UNSAFE
EST_ACTUAL_DEGRADED = 15.0      # est/actual > 15x → DEGRADED
EST_ACTUAL_CALIBRATED = 5.0     # est/actual must be below this for CALIBRATED

# Loss limits
MAX_DAILY_LOSS_USD = 150.0      # 24h net fill damage cap → UNSAFE (was 300; too high for <$2K accounts)
LOSS_REWARD_RATIO_MAX = 2.0     # 24h damage / 24h reward > 2x → DEGRADED (was 3; slow bleed escaped)
SLOW_BLEED_7D_USD = 500.0       # 7-day cumulative net loss → UNSAFE (catches $70/day bleed)

# Q-share sanity
Q_SHARE_MAX = 0.5               # no market can claim > 50% Q-share

# Upgrade thresholds (consecutive good cycles needed)
UPGRADE_TO_CALIBRATED = 3       # 3 consecutive good cycles
UPGRADE_TO_LEARNING = 2         # 2 good cycles from DEGRADED


class SafetyController:
    """Enforces portfolio-level safety invariants."""

    def __init__(self, db_path: str = "bot_history.db"):
        self.db_path = db_path
        self.state = LEARNING  # start conservative
        self.consecutive_good = 0
        self._last_state_change = time.time()

        # Try to load persisted state
        self._load_state()

    # ═══════════════════════════════════════════
    # STATE EVALUATION
    # ═══════════════════════════════════════════

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
    ) -> str:
        """Evaluate system state based on current metrics.

        Returns the current state string after evaluation.
        """
        reasons = []
        new_state = self.state

        # ── UNSAFE checks (instant downgrade) ──
        if correction_factor_raw > 0 and correction_factor_raw < CF_CIRCUIT_BREAK:
            reasons.append(
                f"CF raw={correction_factor_raw:.6f} < {CF_CIRCUIT_BREAK} "
                f"(estimates >{1/CF_CIRCUIT_BREAK:.0f}x reality)"
            )
            new_state = UNSAFE

        est_actual_ratio = 0.0
        if actual_daily_payout > 0 and estimated_daily_total > 0:
            est_actual_ratio = estimated_daily_total / actual_daily_payout

        if est_actual_ratio > EST_ACTUAL_UNSAFE:
            reasons.append(f"Est/actual ratio {est_actual_ratio:.0f}x > {EST_ACTUAL_UNSAFE}x")
            new_state = UNSAFE

        if fill_damage_24h > MAX_DAILY_LOSS_USD:
            reasons.append(f"24h fill damage ${fill_damage_24h:.0f} > limit ${MAX_DAILY_LOSS_USD}")
            new_state = UNSAFE

        # Issue 1: Slow bleed — 7-day cumulative loss check
        if fill_damage_7d > SLOW_BLEED_7D_USD:
            reasons.append(f"7d cumulative loss ${fill_damage_7d:.0f} > limit ${SLOW_BLEED_7D_USD}")
            new_state = UNSAFE

        if new_state == UNSAFE:
            self._transition(UNSAFE, reasons)
            return self.state

        # ── DEGRADED checks ──
        degraded_reasons = []

        if correction_factor_raw > 0 and correction_factor_raw < CF_SEVERE_LOW:
            degraded_reasons.append(f"CF raw={correction_factor_raw:.4f} < {CF_SEVERE_LOW}")

        if est_actual_ratio > EST_ACTUAL_DEGRADED:
            degraded_reasons.append(f"Est/actual ratio {est_actual_ratio:.0f}x > {EST_ACTUAL_DEGRADED}x")

        if reward_payout_24h > 0 and fill_damage_24h > reward_payout_24h * LOSS_REWARD_RATIO_MAX:
            degraded_reasons.append(
                f"Fill damage ${fill_damage_24h:.0f} > {LOSS_REWARD_RATIO_MAX}x reward ${reward_payout_24h:.0f}"
            )

        if cf_at_floor_cycles >= 3:
            degraded_reasons.append(f"CF at floor for {cf_at_floor_cycles} consecutive cycles")

        # Issue 2: Forward-looking rate change detection.
        # If CLOB rates dropped >30%, the 24h payout window is stale.
        # The CF will be wrong for up to 24h. Degrade immediately.
        if clob_rate_delta_pct < -0.30:
            degraded_reasons.append(
                f"CLOB rates dropped {clob_rate_delta_pct:.0%} — "
                f"payout window stale, CF unreliable"
            )

        # Issue 5+9: Data completeness check.
        # If CLOB returned <80% of expected markets, data is partial.
        if data_completeness < 0.80:
            degraded_reasons.append(
                f"Data completeness {data_completeness:.0%} — "
                f"partial API response"
            )

        if degraded_reasons:
            self.consecutive_good = 0
            self._transition(DEGRADED, degraded_reasons)
            return self.state

        # ── CALIBRATED checks ──
        is_calibrated = (
            (correction_factor_raw == 0 or CF_CALIBRATED_LOW <= correction_factor_raw <= CF_CALIBRATED_HIGH)
            and est_actual_ratio < EST_ACTUAL_CALIBRATED
            and num_scoring_markets >= 5
            and fill_damage_24h <= max(reward_payout_24h * 2, 50)
        )

        if is_calibrated:
            self.consecutive_good += 1
            threshold = UPGRADE_TO_CALIBRATED if self.state == LEARNING else UPGRADE_TO_LEARNING
            if self.consecutive_good >= threshold and self.state != CALIBRATED:
                self._transition(CALIBRATED, [f"Good for {self.consecutive_good} consecutive cycles"])
        else:
            self.consecutive_good = 0
            # If currently CALIBRATED, downgrade to LEARNING
            if self.state == CALIBRATED:
                self._transition(LEARNING, ["Calibration criteria no longer met"])

        return self.state

    # ═══════════════════════════════════════════
    # ALLOCATION FILTERING
    # ═══════════════════════════════════════════

    def filter_allocations(
        self,
        allocations: list[dict],
        available_capital: float,
    ) -> list[dict]:
        """Apply state-based constraints to allocations.

        Only REDUCES allocations, never increases. This is the last gate
        before allocations are written to disk.
        """
        perms = STATE_PERMISSIONS[self.state]
        is_probe = perms.get("probe_mode", False)
        min_size_only = perms.get("min_size_only", False)
        max_capital = available_capital * perms["capital_pct"]

        # Sort deploys by score descending (highest-value markets first)
        deploys = [a for a in allocations if a["action"] == "deploy"]
        deploys.sort(key=lambda a: a.get("score", 0), reverse=True)

        # Remove trials if state forbids
        if not perms["trials"]:
            for a in deploys:
                if a.get("score", 0) <= 0:
                    a["action"] = "avoid"
                    a["shares_per_side"] = 0
                    a["reason"] = f"SafetyController: no trials in {self.state}"

        # Enforce market count cap
        deploy_count = 0
        for a in allocations:
            if a["action"] != "deploy":
                continue
            deploy_count += 1
            if deploy_count > perms["max_markets"]:
                a["action"] = "avoid"
                a["shares_per_side"] = 0
                a["reason"] = f"SafetyController: market cap ({perms['max_markets']})"

        # GAP 2: In probe mode (UNSAFE), force min_size on all deployed markets.
        # Probe markets exist ONLY to collect scoring data, not to earn rewards.
        if min_size_only:
            for a in allocations:
                if a["action"] == "deploy":
                    min_sz = int(a.get("min_size", 50))
                    a["shares_per_side"] = min_sz
                    # Recompute est_capital_cost at min_size
                    spread = a.get("max_spread", 0.045)
                    est_price = max(0.10, (1.0 - 2 * spread) / 2)
                    a["est_capital_cost"] = round(min_sz * est_price * 2, 2)
                    a["reason"] = f"PROBE: min_size={min_sz} (data collection only)"

        # Enforce capital cap
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

        # Per-market q_share cap enforcement
        for a in allocations:
            if a["action"] == "deploy" and a.get("q_share_pct", 0) > Q_SHARE_MAX:
                a["q_share_pct"] = Q_SHARE_MAX

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

    # ═══════════════════════════════════════════
    # STATE MANAGEMENT
    # ═══════════════════════════════════════════

    def _transition(self, new_state: str, reasons: list[str]):
        if new_state == self.state:
            return

        old_state = self.state
        self.state = new_state
        self.consecutive_good = 0
        self._last_state_change = time.time()

        level = logging.CRITICAL if new_state == UNSAFE else (
            logging.WARNING if new_state == DEGRADED else logging.INFO
        )
        log.log(
            level,
            f"SAFETY STATE: {old_state} -> {new_state} | {'; '.join(reasons)}"
        )

        self._persist_state(reasons)

        # Reporting fix: write file-based alert for UNSAFE/DEGRADED.
        # This works even if the logging system is misconfigured, the
        # dashboard is down, or no one is watching stdout.
        # The file's existence = system needs attention.
        if new_state in (UNSAFE, DEGRADED):
            self._write_alert_file(old_state, new_state, reasons)
        elif new_state == CALIBRATED:
            self._clear_alert_file()

    def _write_alert_file(self, old_state: str, new_state: str, reasons: list[str]):
        """Write SAFETY_ALERT.txt — human-readable, machine-parseable."""
        import os
        import datetime
        alert_path = os.path.join(os.path.dirname(self.db_path) or ".", "SAFETY_ALERT.txt")
        try:
            with open(alert_path, "w") as f:
                f.write(f"SAFETY ALERT — {new_state}\n")
                f.write(f"Time: {datetime.datetime.now().isoformat()}\n")
                f.write(f"Transition: {old_state} -> {new_state}\n")
                f.write(f"Reasons:\n")
                for r in reasons:
                    f.write(f"  - {r}\n")
                f.write(f"\nAction required: check logs and system state.\n")
            log.warning(f"Alert file written: {alert_path}")
        except Exception as e:
            log.debug(f"Alert file write failed: {e}")

    def _clear_alert_file(self):
        """Remove alert file when system returns to CALIBRATED."""
        import os
        alert_path = os.path.join(os.path.dirname(self.db_path) or ".", "SAFETY_ALERT.txt")
        try:
            if os.path.exists(alert_path):
                os.remove(alert_path)
                log.info("Safety alert file cleared — system CALIBRATED")
        except Exception:
            pass

    def _persist_state(self, reasons: list[str] | None = None):
        """Write state to DB so it survives restarts."""
        try:
            db = _connect_db(self.db_path)
            db.execute(
                """CREATE TABLE IF NOT EXISTS safety_state (
                    id       INTEGER PRIMARY KEY,
                    ts       REAL NOT NULL,
                    state    TEXT NOT NULL,
                    reason   TEXT NOT NULL DEFAULT '',
                    consecutive_good INTEGER NOT NULL DEFAULT 0
                )"""
            )
            db.execute(
                "INSERT INTO safety_state (ts, state, reason, consecutive_good) "
                "VALUES (?, ?, ?, ?)",
                (time.time(), self.state, "; ".join(reasons or []), self.consecutive_good),
            )
            # Keep last 100 entries
            db.execute(
                """DELETE FROM safety_state WHERE id NOT IN (
                    SELECT id FROM safety_state ORDER BY ts DESC LIMIT 100
                )"""
            )
            db.commit()
            db.close()
        except Exception as e:
            log.debug(f"Safety state persist failed: {e}")

    def _load_state(self):
        """Load last persisted state on startup.

        Issue 7 fix: If state was CALIBRATED and age < 2h, restore as
        CALIBRATED with consecutive_good - 1 (trust but verify: one good
        cycle re-confirms). If age 2-6h, restore as LEARNING with
        consecutive_good = max(0, stored - 1) so upgrade is 1-2 cycles
        instead of 3. Beyond 6h, start from scratch.
        """
        try:
            db = _connect_db(self.db_path)
            db.execute(
                """CREATE TABLE IF NOT EXISTS safety_state (
                    id       INTEGER PRIMARY KEY,
                    ts       REAL NOT NULL,
                    state    TEXT NOT NULL,
                    reason   TEXT NOT NULL DEFAULT '',
                    consecutive_good INTEGER NOT NULL DEFAULT 0
                )"""
            )
            row = db.execute(
                "SELECT state, consecutive_good, ts FROM safety_state ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            db.close()

            if row:
                stored_state = row[0] if row[0] in STATE_PERMISSIONS else LEARNING
                stored_good = row[1]
                age_hours = (time.time() - row[2]) / 3600

                if age_hours < 2:
                    # Fresh: trust previous state, reduce consecutive_good by 1
                    self.state = stored_state
                    self.consecutive_good = max(0, stored_good - 1)
                    log.info(
                        f"Loaded safety state: {self.state} (age={age_hours:.1f}h, "
                        f"consecutive_good={self.consecutive_good})"
                    )
                elif age_hours < 6:
                    # Moderately stale: drop to LEARNING but carry partial credit
                    if stored_state == UNSAFE:
                        self.state = LEARNING
                        self.consecutive_good = 0
                    else:
                        self.state = LEARNING
                        self.consecutive_good = max(0, stored_good - 1)
                    log.info(
                        f"Safety state semi-stale ({age_hours:.1f}h) — "
                        f"LEARNING with consecutive_good={self.consecutive_good}"
                    )
                else:
                    # Very stale: no credit
                    self.state = LEARNING
                    self.consecutive_good = 0
                    log.info(f"Safety state stale ({age_hours:.1f}h) — starting LEARNING fresh")
            else:
                self.state = LEARNING
                self.consecutive_good = 0
        except Exception as e:
            log.debug(f"Safety state load failed: {e}")
            self.state = LEARNING

    # ═══════════════════════════════════════════
    # QUERY HELPERS
    # ═══════════════════════════════════════════

    def query_24h_fill_damage(self) -> float:
        """Query net fill damage in last 24h."""
        cutoff = time.time() - 86400
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
            fill_cost = fill_row[0] if fill_row else 0
            dump_rev = dump_row[0] if dump_row else 0
            stop_loss = stop_row[0] if stop_row else 0
            return max(0, fill_cost - dump_rev + stop_loss)
        except Exception:
            return 0.0

    def query_7d_fill_damage(self) -> float:
        """Query net fill damage over rolling 7 days. Catches slow bleeds."""
        cutoff = time.time() - 7 * 86400
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
            fill_cost = fill_row[0] if fill_row else 0
            dump_rev = dump_row[0] if dump_row else 0
            stop_loss = stop_row[0] if stop_row else 0
            return max(0, fill_cost - dump_rev + stop_loss)
        except Exception:
            return 0.0

    def count_scoring_markets(self, window_hours: float = 4.0) -> int:
        """Count markets with recent scoring data."""
        cutoff = time.time() - window_hours * 3600
        row = _query_with_retry(
            self.db_path,
            "SELECT COUNT(DISTINCT condition_id) FROM scoring_snapshots WHERE ts > ?",
            (cutoff,),
            fetch="one",
        )
        return row[0] if row and row[0] else 0
