"""decision_policy.py — FX-051 / Ground Rule 3 consumer.

Reads `market_roi` snapshots written by MarketROITracker.tick() and the
`market_cooldowns` state table, applies the ground-rules §3 auto-correction
triggers, and exposes a single `get_excluded_cids()` set for the allocator
to filter on.

## Triggers implemented (from ground_rules.md §3 table)

| Trigger | Implementation |
|---|---|
| Per-market 24h ROI < threshold across ≥N samples | `evaluate_market` writes to `market_cooldowns` with `cooldown_until = now + COOLDOWN_PERIOD_SEC` |
| Per-market fill_rate > target by Y× | warning log only in v1; queue-cushion / size adjustment is Phase 3 OverCommitAllocator scope |
| Global 24h reward < target × Z | global `[LEARN_GLOBAL]` warn log only in v1; expansion / floor adjustment is Phase 3 scope |
| Global 24h loss > rewards | global warn log; tighter filters happen via the per-market cooldowns this module emits |

## What this module is NOT

- Not the allocator. It only outputs a `set[str]` of excluded cids and
  per-market status dicts. The allocator's eligibility filter consumes the set.
- Not a sizing engine. Per-market notional adjustment is OverCommitAllocator
  (Phase 3) work. This module only does cool / not-cool decisions.
- Not a kill switch. The kill switch is the LAST line of defense
  (ground_rules.md). This is the auto-correction layer that should prevent
  kill-switch conditions from arising.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional, Callable

from market_roi_tracker import MarketROITracker, MarketROISnapshot

log = logging.getLogger("decision_policy")


# ── Tuning knobs (defaults; override via constructor for tests / config) ──
#
# Calibrated for $1.2k wallet operating per ground_rules.md targets:
#   - daily ROI floor: 0.5% (rewards − losses) / wallet
#   - per-market fill rate: < 1/day
#   - per-market notional under overcommit: $10–$50 (wallet / target_market_count)
#
# Cooldown threshold of -5% is intentionally permissive — we don't want to
# cool down a market for a single noisy fill that will average out.
#
# FX-057 (audit response to FX-051): the original v1 thresholds
# (ABS_LOSS=$2.00, MIN_SAMPLES=3) left two gaps the adversarial audit
# tests in test_audit_cooldown_logic.py exercised:
#   1. Sample-gate of 3 is structurally unreachable inside a 24h window
#      when fill-rate is <1/day (the operating target). So roi-trigger
#      was effectively dead.
#   2. Fast-path of $2 missed slow-bleed markets producing $1–$1.99 of
#      losses per fill, indefinitely.
# Both retuned: sample gate → 1 (consistent with <1 fill/day target);
# fast-path → $1 (≈2–5% of per-market notional under overcommit).

ROI_COOLDOWN_THRESHOLD = -0.05         # 24h ROI < -5% triggers cooldown
ROI_COOLDOWN_MIN_SAMPLES = 1           # FX-057: was 3; ≥1 fill is enough confidence
                                       # at <1 fill/day target rate
ABS_LOSS_FAST_COOLDOWN_USD = 1.0       # FX-057: was $2; lowered to catch the slow-bleed
                                       # band ($1–$1.99 single-fill losses). Calibrated
                                       # against per-market notional of $10–$50.
COOLDOWN_PERIOD_SEC = 86400.0          # 24h cooldown duration
FILL_RATE_WARNING_PER_HOUR = 1.0       # > 1/hr is suspicious; warn (no auto-action in v1)
GLOBAL_LOSS_WARNING_RATIO = 0.5        # if total_loss > 0.5 × total_reward → warn


@dataclass
class MarketDecision:
    """Per-market decision produced by `evaluate_market`."""
    condition_id: str
    action: str                      # 'allow' | 'cool_down' | 'still_cooled' | 'reactivate'
    reason: str
    cooldown_until: Optional[float] = None
    roi_24h: Optional[float] = None
    fill_loss_24h: Optional[float] = None
    samples_24h: int = 0


class DecisionPolicy:
    """Cool-down / reactivate decisions driven by ROI snapshots."""

    def __init__(
        self,
        db_path: str,
        tracker: MarketROITracker,
        *,
        roi_threshold: float = ROI_COOLDOWN_THRESHOLD,
        roi_min_samples: int = ROI_COOLDOWN_MIN_SAMPLES,
        abs_loss_fast_threshold: float = ABS_LOSS_FAST_COOLDOWN_USD,
        cooldown_period_sec: float = COOLDOWN_PERIOD_SEC,
        fill_rate_warn_per_hour: float = FILL_RATE_WARNING_PER_HOUR,
        global_loss_warn_ratio: float = GLOBAL_LOSS_WARNING_RATIO,
        _now: Optional[Callable] = None,
    ):
        self.db_path = db_path
        self.tracker = tracker
        self.roi_threshold = roi_threshold
        self.roi_min_samples = roi_min_samples
        self.abs_loss_fast_threshold = abs_loss_fast_threshold
        self.cooldown_period_sec = cooldown_period_sec
        self.fill_rate_warn_per_hour = fill_rate_warn_per_hour
        self.global_loss_warn_ratio = global_loss_warn_ratio
        self._now = _now or time.time

    # ── Per-market evaluation ──

    def _is_roi_bad(self, roi: MarketROISnapshot) -> tuple[bool, str]:
        """Return (bad, reason_string) given a fresh ROI snapshot.

        Single source of truth for the cooldown triggers — used both on the
        first-cool path and on the FX-057 expired-but-still-bad re-cool path.
        """
        roi_bad = (
            roi.roi < self.roi_threshold and roi.samples >= self.roi_min_samples
        )
        loss_fast = roi.fill_loss >= self.abs_loss_fast_threshold
        if not (roi_bad or loss_fast):
            return False, ""
        bits: list[str] = []
        if roi_bad:
            bits.append(
                f"roi_24h={roi.roi:+.4f}<{self.roi_threshold:+.4f}"
                f" samples={roi.samples}≥{self.roi_min_samples}"
            )
        if loss_fast:
            bits.append(
                f"fill_loss_24h=${roi.fill_loss:.2f}>=${self.abs_loss_fast_threshold:.2f}"
            )
        return True, " | ".join(bits)

    def evaluate_market(self, cid: str) -> MarketDecision:
        """Look at this market's 24h ROI + current cooldown state, decide what to do.

        Decision matrix:
            cooled-down + not expired              → 'still_cooled'
            cooled-down + expired + ROI still bad  → 'cool_down'   (FX-057 re-cool)
            cooled-down + expired + ROI healthy    → 'reactivate'  (delete + allow)
            not cooled + ROI bad                   → 'cool_down'
            not cooled + ROI ok/unseen             → 'allow'

        ROI is "bad" when EITHER:
          (a) roi_24h < roi_threshold AND samples_24h ≥ roi_min_samples, OR
          (b) fill_loss_24h ≥ abs_loss_fast_threshold (single-event fast path)
        See `_is_roi_bad` for the shared trigger evaluator.
        """
        now = self._now()
        existing = self._load_cooldown(cid)
        if existing is not None:
            cooled_at, until_ts, reason, roi_at, loss_at, samp_at = existing
            if until_ts > now:
                return MarketDecision(
                    condition_id=cid, action="still_cooled",
                    reason=f"cooled until {time.strftime('%Y-%m-%d %H:%M', time.gmtime(until_ts))} ({reason})",
                    cooldown_until=until_ts,
                    roi_24h=roi_at, fill_loss_24h=loss_at, samples_24h=samp_at,
                )
            # Expired — remove the old row, then decide based on fresh ROI.
            # FX-057: if the fresh ROI is STILL bad, re-cool immediately
            # rather than reactivating for one farmer cycle and re-cooling
            # next oversight cycle (the original v1 behaviour leaked one
            # cycle of avoidable fills per expiry on every persistent loser).
            self._delete_cooldown(cid)
            roi_now = self.tracker.get_roi(cid, "24h")
            if roi_now is None:
                return MarketDecision(
                    condition_id=cid, action="reactivate",
                    reason="cooldown expired, no fresh ROI yet",
                    cooldown_until=None,
                )
            still_bad, bad_reason = self._is_roi_bad(roi_now)
            if still_bad:
                until = now + self.cooldown_period_sec
                full_reason = f"expired+still_bad: {bad_reason}"
                self._insert_cooldown(cid, now, until, full_reason, roi_now)
                return MarketDecision(
                    condition_id=cid, action="cool_down",
                    reason=full_reason, cooldown_until=until,
                    roi_24h=roi_now.roi, fill_loss_24h=roi_now.fill_loss,
                    samples_24h=roi_now.samples,
                )
            return MarketDecision(
                condition_id=cid, action="reactivate",
                reason=f"cooldown expired, ROI now {roi_now.roi:+.4f}",
                cooldown_until=None,
                roi_24h=roi_now.roi, fill_loss_24h=roi_now.fill_loss,
                samples_24h=roi_now.samples,
            )

        roi = self.tracker.get_roi(cid, "24h")
        if roi is None:
            return MarketDecision(
                condition_id=cid, action="allow",
                reason="no ROI snapshot yet",
            )

        bad, reason = self._is_roi_bad(roi)
        if bad:
            until = now + self.cooldown_period_sec
            self._insert_cooldown(cid, now, until, reason, roi)
            return MarketDecision(
                condition_id=cid, action="cool_down",
                reason=reason, cooldown_until=until,
                roi_24h=roi.roi, fill_loss_24h=roi.fill_loss,
                samples_24h=roi.samples,
            )

        return MarketDecision(
            condition_id=cid, action="allow",
            reason=f"roi_24h={roi.roi:+.4f} samples={roi.samples}",
            roi_24h=roi.roi, fill_loss_24h=roi.fill_loss,
            samples_24h=roi.samples,
        )

    # ── Per-cycle evaluation across all known markets ──

    def evaluate(self) -> dict:
        """Run evaluate_market over every market with a recent ROI snapshot
        OR an active cooldown row. Emit summary + structured log.

        Returns dict with:
          newly_cooled         : list of cids that just entered cooldown
          still_cooled         : list of cids in active cooldown (not changed)
          reactivated          : list of cids that just exited cooldown
          allowed              : list of cids with ROI snapshot but no cooldown
          global_summary       : from tracker.get_global_summary("24h")
          warnings             : list of structured warning strings

          P4 of 9/10 plan — new behavior-change outputs (ground_rules §3
          triggers 3 + 5 wired to actual action, not just logs):

          size_reduction_cids  : set of cids where fill_rate > target by Y×
                                (allocator halves shares_per_side for these)
          global_tighten       : bool — if True, allocator raises filter
                                floors this cycle (global loss > rewards
                                pattern detected; bias toward fewer / safer
                                deploys until the rolling metric recovers)
        """
        cids = set()
        for snap in self.tracker.get_all_for_window("24h"):
            cids.add(snap.condition_id)
        for cid in self._all_cooldown_cids():
            cids.add(cid)

        out = {
            "newly_cooled": [],
            "still_cooled": [],
            "reactivated": [],
            "allowed": [],
            "warnings": [],
            # P4: per-cycle behavior-change outputs (no DB persistence —
            # recomputed each cycle from raw signals so transient anomalies
            # self-resolve at the next evaluation without manual cleanup).
            "size_reduction_cids": set(),
            "global_tighten": False,
        }
        for cid in sorted(cids):
            d = self.evaluate_market(cid)
            if d.action == "cool_down":
                out["newly_cooled"].append(cid)
            elif d.action == "still_cooled":
                out["still_cooled"].append(cid)
            elif d.action == "reactivate":
                out["reactivated"].append(cid)
            else:
                out["allowed"].append(cid)
            # P4 — Trigger #3 (per-market fill_rate). Ground rules §3:
            # "Per-market fill_rate > target by Y× → reduce per-market size
            # OR cool". Cooling is the FX-051 path (ROI-driven). This path
            # handles the rate-driven response: when fill_rate exceeds the
            # warning threshold but ROI is fine, reduce per-market size so
            # we stay on the market with smaller exposure. Auto-action
            # (was just a warn log pre-P4).
            if d.samples_24h and (d.samples_24h / 24.0) > self.fill_rate_warn_per_hour:
                out["warnings"].append(
                    f"high_fill_rate cid={cid[:12]} fills_24h={d.samples_24h}"
                )
                # Only mark for size reduction if the market is NOT already
                # cooled (cooling is the stronger response — no need to
                # also reduce size on a market we're skipping entirely).
                if d.action not in ("cool_down", "still_cooled"):
                    out["size_reduction_cids"].add(cid)

        # Global summary + global warnings + P4 trigger #5
        global_summary = self.tracker.get_global_summary("24h")
        out["global_summary"] = global_summary
        tr = global_summary.get("total_reward", 0.0)
        tl = global_summary.get("total_loss", 0.0)
        if tr > 0 and tl > tr * self.global_loss_warn_ratio:
            out["warnings"].append(
                f"global_loss_ratio loss=${tl:.2f} > {self.global_loss_warn_ratio:.0%}×reward=${tr:.2f}"
            )
            # P4 — Trigger #5 (global loss > rewards). Ground rules §3:
            # "Global 24h loss > rewards → tighten filters; reduce per-market
            # exposure". Auto-action: signal allocator to raise the
            # MIN_DAILY_RATE_USD floor (skip lower-reward markets) and
            # apply size reduction globally. Allocator reads `global_tighten`
            # and applies a conservative-mode multiplier this cycle.
            out["global_tighten"] = True
        # Also tighten when loss exists but reward is 0 (worse-than-warn-ratio
        # case where we can't even compute the ratio). Bias toward safe.
        elif tl > 0 and tr == 0:
            out["warnings"].append(
                f"global_loss_no_reward loss=${tl:.2f} reward=$0 (tightening)"
            )
            out["global_tighten"] = True

        # Structured log line per ground_rules.md
        log.info(
            "[LEARN] "
            f"newly_cooled={len(out['newly_cooled'])} "
            f"still_cooled={len(out['still_cooled'])} "
            f"reactivated={len(out['reactivated'])} "
            f"allowed={len(out['allowed'])} "
            f"warnings={len(out['warnings'])} "
            f"daily_roi={global_summary.get('daily_roi', 0):+.4f} "
            f"total_reward=${tr:.2f} total_loss=${tl:.2f}"
        )
        for w in out["warnings"]:
            log.warning(f"[LEARN_WARN] {w}")
        return out

    # ── Allocator hook ──

    def get_excluded_cids(self) -> set[str]:
        """Set of condition_ids currently in cooldown (cooldown_until > now).

        Allocator filters its eligible set against this. Cheap O(N) read;
        called once per allocator cycle.
        """
        try:
            now = self._now()
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                "SELECT condition_id FROM market_cooldowns WHERE cooldown_until > ?",
                (now,),
            ).fetchall()
            conn.close()
            return {r[0] for r in rows if r and r[0]}
        except Exception as e:
            log.debug(f"[POLICY] get_excluded_cids failed: {e}")
            return set()

    def is_cooled_down(self, cid: str) -> bool:
        """Single-cid query; equivalent to `cid in get_excluded_cids()`."""
        try:
            now = self._now()
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT 1 FROM market_cooldowns "
                "WHERE condition_id = ? AND cooldown_until > ?",
                (cid, now),
            ).fetchone()
            conn.close()
            return row is not None
        except Exception:
            return False

    # ── Cooldown table helpers ──

    def _load_cooldown(self, cid: str) -> Optional[tuple]:
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT cooled_at, cooldown_until, reason, roi_at_cooldown, "
                "       fill_loss_at_cooldown, samples_at_cooldown "
                "FROM market_cooldowns WHERE condition_id = ?",
                (cid,),
            ).fetchone()
            conn.close()
            return row if row else None
        except Exception as e:
            log.debug(f"[POLICY] _load_cooldown failed: {e}")
            return None

    def _insert_cooldown(
        self, cid: str, cooled_at: float, until: float, reason: str,
        roi: MarketROISnapshot,
    ) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT INTO market_cooldowns (condition_id, cooled_at, cooldown_until, "
                "reason, roi_at_cooldown, fill_loss_at_cooldown, samples_at_cooldown) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(condition_id) DO UPDATE SET "
                "cooled_at=excluded.cooled_at, cooldown_until=excluded.cooldown_until, "
                "reason=excluded.reason, roi_at_cooldown=excluded.roi_at_cooldown, "
                "fill_loss_at_cooldown=excluded.fill_loss_at_cooldown, "
                "samples_at_cooldown=excluded.samples_at_cooldown",
                (cid, cooled_at, until, reason, roi.roi, roi.fill_loss, roi.samples),
            )
            conn.commit()
            conn.close()
            log.warning(
                f"[LEARN_COOLDOWN] cid={cid[:12]} until={time.strftime('%Y-%m-%d %H:%M', time.gmtime(until))} "
                f"reason={reason}"
            )
        except Exception as e:
            log.warning(f"[POLICY] _insert_cooldown failed for {cid[:12]}: {e}")

    def _delete_cooldown(self, cid: str) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("DELETE FROM market_cooldowns WHERE condition_id = ?", (cid,))
            conn.commit()
            conn.close()
            log.info(f"[LEARN_REACTIVATE] cid={cid[:12]}")
        except Exception as e:
            log.warning(f"[POLICY] _delete_cooldown failed for {cid[:12]}: {e}")

    def _all_cooldown_cids(self) -> list[str]:
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                "SELECT condition_id FROM market_cooldowns"
            ).fetchall()
            conn.close()
            return [r[0] for r in rows if r and r[0]]
        except Exception:
            return []
