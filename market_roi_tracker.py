"""market_roi_tracker.py — FX-051 / Ground Rule 3 foundation.

Per-market rolling ROI tracking with 1h / 24h / 7d windows.

This module is the DATA layer. The DECISION layer (`decision_policy.py`)
consumes its output to make per-market cooldown / reactivation calls. The
SimpleAllocator does not consult this module directly — it consults the
policy's `get_excluded_cids()` set.

## Inputs

- `fills` table (BUY-side cost-out events; one row per fill)
- `unwinds` table (SELL-side cash-in events; `pnl < 0` means realized loss)
- `capital_committed_snapshots` table (this module writes; one row per
  alloc-cycle per market)
- `/rewards/user/markets?date=YYYY-MM-DD` API (per-market reward attribution;
  cached in `daily_reward_cache` table to avoid refetch)

## Per-window metric definitions

For each (condition_id, window) ∈ markets × {1h, 24h, 7d}:

- `reward_earned`         — best-effort cumulative reward for the window:
                            for 24h we read today's API row (and yesterday's
                            if the current UTC day is < 4h old, since
                            Polymarket pays in single daily batches at ~00:20
                            UTC). For 1h the 24h value is scaled by 1/24
                            (approximate; the API doesn't expose hourly).
                            For 7d we sum 7 daily rows. API failures fail
                            quiet to 0 — the decision policy interprets this
                            as "no reward signal", which biases toward
                            cooldown (safer).
- `fill_loss`             — SUM(-pnl) from `unwinds` for this cid in window
                            where pnl < 0. Strictly positive USD.
- `capital_committed_avg` — time-weighted average of `est_capital_cost`
                            recorded in `capital_committed_snapshots`. If
                            we have N snapshots within the window and the
                            allocator runs every ~30 min, each snapshot is
                            weighted by its dwell time until the next
                            snapshot (or the window end for the last one).
- `roi`                   — `(reward_earned - fill_loss) / max(capital_committed_avg, 0.01)`.
                            Negative ROI triggers cooldown via the policy.
- `fill_count`            — COUNT(*) from `fills` for this cid in window
- `fill_rate_per_hour`    — `fill_count / window_hours`
- `samples`               — alias for `fill_count`; named "samples" in the
                            decision-policy's confidence gate to match
                            ground_rules.md's `samples ≥ N` rule.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional, Callable

import requests

log = logging.getLogger("market_roi_tracker")


WINDOWS: dict[str, float] = {
    "1h": 3600.0,
    "24h": 86400.0,
    "7d": 604800.0,
}

CLOB_HOST = "https://clob.polymarket.com"
USER_MARKETS_PATH = "/rewards/user/markets"
# FX-088: authoritative public source for actual reward/rebate CREDITS (on-chain,
# no auth). The CLOB /rewards/user/markets endpoint returned a {"data":[...]}
# envelope the old parser mis-read (looked for "markets"), so reward_earned was
# always 0 — blinding the bot to its own rewards and poisoning the ROI loop.
DATA_API_HOST = "https://data-api.polymarket.com"

# How old a daily_reward_cache entry can be (in seconds) before we re-fetch.
# Polymarket pays daily batches at ~00:20 UTC, so we re-fetch once per cycle
# for current-day; older days are stable so cache them aggressively.
_FRESH_TODAY_SEC = 1500.0  # 25 min — within one oversight cycle
_FRESH_PAST_SEC = 86400.0 * 7  # past days: refresh once per week

# FX-057: minimum capital_committed_avg required to compute a meaningful
# ROI. Below this, the (reward - loss) / capital ratio becomes wildly
# unstable; we treat it as "no signal" (roi=0). DecisionPolicy's triggers
# read `fill_loss` directly so cooldown decisions are unaffected when
# this guard fires.
CAPITAL_AVG_MIN_FOR_ROI = 0.10  # USD


@dataclass
class MarketROISnapshot:
    """One row of `market_roi`."""
    condition_id: str
    window: str
    window_end_ts: float
    reward_earned: float
    fill_loss: float
    capital_committed_avg: float
    roi: float
    fill_count: int
    fill_rate_per_hour: float
    samples: int
    last_updated: float

    @classmethod
    def from_row(cls, row: tuple) -> "MarketROISnapshot":
        return cls(
            condition_id=row[0], window=row[1], window_end_ts=row[2],
            reward_earned=row[3], fill_loss=row[4], capital_committed_avg=row[5],
            roi=row[6], fill_count=row[7], fill_rate_per_hour=row[8],
            samples=row[9], last_updated=row[10],
        )


class MarketROITracker:
    """Per-market rolling ROI tracker.

    Thread-safety: not safe for concurrent ticks on the same DB. The bot's
    oversight cycle is single-threaded so this is fine. Tests wrap each
    operation in its own connection.

    Failure mode: fail-quiet. Any internal exception is logged at WARNING
    and the tick returns a partial result. The caller (simple_oversight)
    must wrap the whole tick in its own try/except to avoid breaking the
    oversight loop on tracker errors.
    """

    def __init__(
        self,
        db_path: str,
        funder: str,
        *,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        wallet_address: str = "",
        _http: Optional[Callable] = None,
        _now: Optional[Callable] = None,
    ):
        self.db_path = db_path
        self.funder = funder
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.wallet_address = wallet_address
        self._http = _http or requests.get
        self._now = _now or time.time

    # ── Capital snapshot ──

    def snapshot_capital(self, alloc_result) -> int:
        """Record per-market est_capital_cost for the current deploys.

        Called from simple_oversight.run_once() after allocator.compute().
        `alloc_result` is an AllocationResult with .deploys: list of
        CandidateMarket each carrying .target_capital.

        Returns count of rows inserted (one per deploy).
        """
        if not alloc_result or not getattr(alloc_result, "deploys", None):
            return 0
        now = self._now()
        rows = [(now, m.condition_id, float(m.target_capital))
                for m in alloc_result.deploys
                if getattr(m, "condition_id", None)]
        if not rows:
            return 0
        try:
            conn = sqlite3.connect(self.db_path)
            conn.executemany(
                "INSERT INTO capital_committed_snapshots (ts, condition_id, est_capital_cost) "
                "VALUES (?, ?, ?)",
                rows,
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning(f"[ROI_TRACKER] snapshot_capital failed: {e}")
            return 0
        return len(rows)

    # ── Reward API + cache ──

    def _utc_date_str(self, ts: float) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(ts))

    REWARD_TOTAL_KEY = "__TOTAL__"

    def _fetch_rewards_for_date(self, date: str) -> dict[str, float]:
        """AGGREGATE maker earnings (liquidity REWARD + MAKER_REBATE) credited
        on one UTC `date`, from the public data-api /activity feed.

        FX-088: the prior CLOB /rewards/user/markets parser looked for a
        "markets" key (the API returns "data") and a flat `earnings` field
        (it's nested per-asset), so reward_earned was ALWAYS 0 — blinding the
        bot to its own rewards and zeroing capital_efficiency. We now source
        from the data-api /activity feed (public, no auth, authoritative
        on-chain credits — the same source the wallet reconciler uses). These
        REWARD/REBATE credits are AGGREGATE daily lumps (their conditionId is
        EMPTY — Polymarket does not break the credit out per market here), so
        we return the single-day total under the sentinel key REWARD_TOTAL_KEY.
        tick() attributes it across markets proportional to committed capital.
        Fail-open to {} (caller falls through to cache/zero).
        """
        if not self.funder:
            log.debug("[ROI_TRACKER] reward fetch skipped: no funder")
            return {}
        try:
            import calendar
            day_start = float(calendar.timegm(time.strptime(date, "%Y-%m-%d")))
        except (ValueError, TypeError) as e:
            log.warning(f"[ROI_TRACKER] reward fetch bad date={date}: {e}")
            return {}
        day_end = day_start + 86400.0
        total = 0.0
        got_any = False
        try:
            for ptype in ("REWARD", "MAKER_REBATE"):
                offset = 0
                while True:
                    r = self._http(
                        f"{DATA_API_HOST}/activity",
                        params={"user": self.funder, "type": ptype,
                                "limit": 500, "offset": offset},
                        timeout=15,
                    )
                    if r.status_code != 200:
                        log.warning(
                            f"[ROI_TRACKER] data-api {ptype} date={date} "
                            f"status={r.status_code}"
                        )
                        return {}  # fail-open: don't cache a partial 0
                    data = r.json() or []
                    got_any = True
                    if not data:
                        break
                    for it in data:
                        ts = float(it.get("timestamp", 0) or 0)
                        if day_start <= ts < day_end:
                            total += float(it.get("usdcSize", 0) or it.get("amount", 0) or 0)
                    oldest = min(
                        (float(it.get("timestamp", 0) or 0) for it in data),
                        default=0.0,
                    )
                    # data-api returns newest-first; stop once a full page is
                    # older than the window (or the page is short).
                    if len(data) < 500 or oldest < day_start:
                        break
                    offset += 500
            # Record the day's total (even 0.0) so the cache is truthful.
            return {self.REWARD_TOTAL_KEY: total} if got_any else {}
        except Exception as e:
            log.warning(
                f"[ROI_TRACKER] reward fetch date={date} "
                f"error: {type(e).__name__}: {e}"
            )
            return {}

    def _read_total_reward_for_window(self, window: str, window_end: float) -> float:
        """FX-088: sum the AGGREGATE daily reward (sentinel REWARD_TOTAL_KEY)
        across the date(s) covered by `window`. tick() splits this across
        markets proportional to committed capital. 1h is approximated as the
        day total / 24 (the data-api credits daily, not hourly)."""
        now = window_end
        try:
            conn = sqlite3.connect(self.db_path)
            if window == "7d":
                dates = [self._utc_date_str(now - 86400 * i) for i in range(7)]
            else:
                dates = [self._utc_date_str(now)]
            ph = ",".join("?" * len(dates))
            row = conn.execute(
                f"SELECT COALESCE(SUM(reward_earned), 0) FROM daily_reward_cache "
                f"WHERE condition_id = ? AND date IN ({ph})",
                (self.REWARD_TOTAL_KEY, *dates),
            ).fetchone()
            conn.close()
            val = float(row[0]) if row and row[0] is not None else 0.0
            return (val / 24.0) if window == "1h" else val
        except Exception as e:
            log.debug(f"[ROI_TRACKER] total reward read failed/{window}: {e}")
            return 0.0

    def _ensure_reward_cache_fresh(self, date: str) -> None:
        """Refresh daily_reward_cache for one date if stale or missing.

        Today's row is considered stale after _FRESH_TODAY_SEC. Past dates
        are considered stale after _FRESH_PAST_SEC.
        """
        now = self._now()
        today = self._utc_date_str(now)
        threshold = _FRESH_TODAY_SEC if date == today else _FRESH_PAST_SEC
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT MAX(fetched_at) FROM daily_reward_cache WHERE date = ?",
                (date,),
            ).fetchone()
            conn.close()
            last_fetch = float(row[0]) if row and row[0] is not None else 0.0
            if now - last_fetch < threshold:
                return  # cache fresh
        except Exception as e:
            log.debug(f"[ROI_TRACKER] cache freshness check failed: {e}")

        # Fetch + upsert
        data = self._fetch_rewards_for_date(date)
        if not data:
            return
        try:
            conn = sqlite3.connect(self.db_path)
            for cid, reward in data.items():
                conn.execute(
                    "INSERT INTO daily_reward_cache (date, condition_id, reward_earned, fetched_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(date, condition_id) DO UPDATE SET "
                    "reward_earned=excluded.reward_earned, fetched_at=excluded.fetched_at",
                    (date, cid, reward, now),
                )
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning(f"[ROI_TRACKER] cache upsert failed: {e}")

    def _read_reward_for_window(self, cid: str, window: str, window_end: float) -> float:
        """Return best-effort reward_earned for (cid, window) ending at window_end.

        24h: read today's cached row (refreshed if needed).
        7d:  sum the last 7 daily-cache rows (today + 6 prior dates).
        1h:  approximate as (24h reward) / 24.
        """
        now = window_end
        try:
            conn = sqlite3.connect(self.db_path)
            if window == "24h":
                date = self._utc_date_str(now)
                row = conn.execute(
                    "SELECT reward_earned FROM daily_reward_cache "
                    "WHERE date = ? AND condition_id = ?",
                    (date, cid),
                ).fetchone()
                val = float(row[0]) if row and row[0] is not None else 0.0
            elif window == "7d":
                # Sum across 7 daily rows. Each date string is YYYY-MM-DD.
                dates = [self._utc_date_str(now - 86400 * i) for i in range(7)]
                placeholders = ",".join("?" * len(dates))
                row = conn.execute(
                    f"SELECT COALESCE(SUM(reward_earned), 0) FROM daily_reward_cache "
                    f"WHERE condition_id = ? AND date IN ({placeholders})",
                    (cid, *dates),
                ).fetchone()
                val = float(row[0]) if row else 0.0
            elif window == "1h":
                # Approximate as (today's reward) / 24. The API doesn't expose
                # hourly granularity; this is acceptable for cooldown decisions
                # which use 24h as the primary signal.
                date = self._utc_date_str(now)
                row = conn.execute(
                    "SELECT reward_earned FROM daily_reward_cache "
                    "WHERE date = ? AND condition_id = ?",
                    (date, cid),
                ).fetchone()
                val = (float(row[0]) / 24.0) if row and row[0] is not None else 0.0
            else:
                val = 0.0
            conn.close()
            return val
        except Exception as e:
            log.debug(f"[ROI_TRACKER] reward read failed for {cid[:12]}/{window}: {e}")
            return 0.0

    # ── Loss + fill_count queries ──

    def _fill_loss_for_window(self, conn, cid: str, since_ts: float) -> float:
        """SUM(-pnl) from unwinds where cid AND pnl<0 AND ts>since_ts."""
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN pnl < 0 THEN -pnl ELSE 0 END), 0) "
            "FROM unwinds WHERE condition_id = ? AND ts > ?",
            (cid, since_ts),
        ).fetchone()
        return float(row[0]) if row else 0.0

    def _fill_count_for_window(self, conn, cid: str, since_ts: float) -> int:
        """COUNT(*) from fills where cid AND ts>since_ts."""
        row = conn.execute(
            "SELECT COUNT(*) FROM fills WHERE condition_id = ? AND ts > ?",
            (cid, since_ts),
        ).fetchone()
        return int(row[0]) if row else 0

    def _capital_committed_avg(
        self, conn, cid: str, since_ts: float, until_ts: float
    ) -> float:
        """Time-weighted avg of est_capital_cost in [since_ts, until_ts].

        Algorithm:
          1. Fetch in-window snapshots (ts ≥ since_ts AND ts ≤ until_ts).
          2. FX-057: also fetch the latest snapshot BEFORE the window. This
             value reflects the capital that was committed at window_start
             but never re-stamped. Without this lookback, a 1h window with
             a single snapshot at minute 59 yields capital_avg ≈ $0.83
             instead of $50 — the pre-snapshot interval was unattributed.
          3. The "initial value" for the segment from since_ts to the first
             in-window snapshot is:
               • the pre-window snapshot's capital if one exists, else
               • the first in-window snapshot's capital (extrapolated back),
               • else 0.0 (nothing known).
          4. Integrate the step function across the window and divide by
             window length.
        """
        rows = conn.execute(
            "SELECT ts, est_capital_cost FROM capital_committed_snapshots "
            "WHERE condition_id = ? AND ts >= ? AND ts <= ? "
            "ORDER BY ts ASC",
            (cid, since_ts, until_ts),
        ).fetchall()

        # FX-057: look back before the window for the most recent prior
        # snapshot. This captures capital committed at window_start that
        # wasn't re-stamped inside the window.
        prior = conn.execute(
            "SELECT est_capital_cost FROM capital_committed_snapshots "
            "WHERE condition_id = ? AND ts < ? "
            "ORDER BY ts DESC LIMIT 1",
            (cid, since_ts),
        ).fetchone()

        if not rows and not prior:
            return 0.0
        if not rows:
            # Only a prior snapshot exists; assume it held for the whole window.
            return float(prior[0])

        # Pick the initial value: prior snapshot if present, else extrapolate
        # the first in-window snapshot backwards. The extrapolation can
        # over-count for genuinely new positions, but only for the small
        # window-start-to-first-snapshot interval and only on the very first
        # cycle a market is observed; on later cycles `prior` is populated.
        initial_capital = float(prior[0]) if prior else float(rows[0][1])

        window_len = until_ts - since_ts
        if window_len <= 0:
            return 0.0

        # Integrate. First segment: from since_ts to rows[0].ts at
        # initial_capital. Subsequent segments: each row's capital held until
        # the next row's ts. Final segment: from rows[-1].ts to until_ts at
        # rows[-1].capital (handled by virtual zero-capital sentinel).
        total_capital_time = initial_capital * max(0.0, rows[0][0] - since_ts)
        rows_with_end = list(rows) + [(until_ts, 0.0)]
        for i in range(len(rows_with_end) - 1):
            ts_i, cap_i = rows_with_end[i]
            ts_next = rows_with_end[i + 1][0]
            dwell = max(0.0, ts_next - ts_i)
            total_capital_time += cap_i * dwell
        return total_capital_time / window_len

    # ── Active-market discovery ──

    def _active_cids(self, conn, since_ts: float) -> list[str]:
        """Markets that have ANY activity (fill, unwind, or capital snapshot)
        within the lookback window. Used to scope `tick` to active markets
        only — irrelevant markets don't get a row in market_roi."""
        rows = conn.execute(
            """
            SELECT DISTINCT condition_id FROM (
                SELECT condition_id FROM fills WHERE ts > :ts
                UNION
                SELECT condition_id FROM unwinds WHERE ts > :ts
                UNION
                SELECT condition_id FROM capital_committed_snapshots WHERE ts > :ts
            )
            """,
            {"ts": since_ts},
        ).fetchall()
        return [r[0] for r in rows if r and r[0]]

    # ── Main tick ──

    def tick(self, *, skip_reward_api: bool = False) -> dict:
        """Recompute rolling-window snapshots for every active market.

        Args:
            skip_reward_api: when True, don't call /rewards/user/markets
              (use only cached values). Useful for tests and back-fill
              scenarios where the bot is halted and the API is irrelevant.

        Returns dict with keys: markets_updated, windows_updated, errors,
        api_fetched (0 or 1 for each date we attempted).
        """
        now = self._now()
        summary = {
            "markets_updated": 0,
            "windows_updated": 0,
            "errors": [],
            "api_fetches": 0,
            "active_cids": 0,
        }

        # Refresh reward API cache (today + yesterday for 24h-window coverage
        # when current UTC day is young; 6 prior days for 7d completeness).
        if not skip_reward_api:
            dates_to_check = [self._utc_date_str(now - 86400 * i) for i in range(7)]
            for d in dates_to_check:
                self._ensure_reward_cache_fresh(d)
                summary["api_fetches"] += 1

        # Largest window determines which markets are "active".
        max_window = max(WINDOWS.values())
        since_max = now - max_window

        try:
            conn = sqlite3.connect(self.db_path)
            cids = self._active_cids(conn, since_max)
            summary["active_cids"] = len(cids)
        except Exception as e:
            log.warning(f"[ROI_TRACKER] active_cids query failed: {e}")
            return summary

        # FX-088: rewards are only available as a daily AGGREGATE (the data-api
        # credits carry no per-market conditionId), so we attribute the window's
        # total reward across markets PROPORTIONAL to time-integrated committed
        # capital (≈ liquidity provided). Phase 1 computes per-market loss/count/
        # capital and the capital totals; Phase 2 splits the aggregate reward and
        # writes each market_roi row. This makes capital_efficiency and ROI
        # reflect REAL rewards instead of the old always-0 (which had blinded
        # the bot to its own earnings and biased every market toward "pure loss").
        try:
            # ── Phase 1: per-(cid, window) loss/count/capital + capital totals ──
            stats: dict = {}
            total_capital = {w: 0.0 for w in WINDOWS}
            for cid in cids:
                for window_name, window_secs in WINDOWS.items():
                    since_ts = now - window_secs
                    try:
                        fill_loss = self._fill_loss_for_window(conn, cid, since_ts)
                        fill_count = self._fill_count_for_window(conn, cid, since_ts)
                        capital_avg = self._capital_committed_avg(
                            conn, cid, since_ts, now
                        )
                    except Exception as e:
                        summary["errors"].append(f"{cid[:12]}/{window_name}: {e}")
                        continue
                    stats[(cid, window_name)] = (fill_loss, fill_count, capital_avg)
                    if capital_avg >= CAPITAL_AVG_MIN_FOR_ROI:
                        total_capital[window_name] += capital_avg

            # Authoritative aggregate reward per window (data-api total).
            total_reward = {
                w: self._read_total_reward_for_window(w, now) for w in WINDOWS
            }

            # ── Phase 2: attribute reward ∝ capital, compute ROI, upsert ──
            for cid in cids:
                for window_name, window_secs in WINDOWS.items():
                    st = stats.get((cid, window_name))
                    if st is None:
                        continue
                    fill_loss, fill_count, capital_avg = st
                    tc = total_capital[window_name]
                    # FX-057: capital below the floor is "no signal" → reward
                    # share + ROI both 0 (avoids the old 100× ROI inflation on
                    # ~$0 capital). FX-088: otherwise this market earns its
                    # capital share of the window's aggregate reward.
                    if capital_avg >= CAPITAL_AVG_MIN_FOR_ROI and tc > 0:
                        reward_earned = total_reward[window_name] * (capital_avg / tc)
                        roi = (reward_earned - fill_loss) / capital_avg
                    else:
                        reward_earned = 0.0
                        roi = 0.0
                    fill_rate = fill_count / (window_secs / 3600.0)
                    try:
                        conn.execute(
                            "INSERT INTO market_roi (condition_id, window, window_end_ts, "
                            "reward_earned, fill_loss, capital_committed_avg, roi, "
                            "fill_count, fill_rate_per_hour, samples, last_updated) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(condition_id, window) DO UPDATE SET "
                            "window_end_ts=excluded.window_end_ts, "
                            "reward_earned=excluded.reward_earned, "
                            "fill_loss=excluded.fill_loss, "
                            "capital_committed_avg=excluded.capital_committed_avg, "
                            "roi=excluded.roi, "
                            "fill_count=excluded.fill_count, "
                            "fill_rate_per_hour=excluded.fill_rate_per_hour, "
                            "samples=excluded.samples, "
                            "last_updated=excluded.last_updated",
                            (cid, window_name, now, reward_earned, fill_loss,
                             capital_avg, roi, fill_count, fill_rate, fill_count, now),
                        )
                        summary["windows_updated"] += 1
                    except Exception as e:
                        summary["errors"].append(f"{cid[:12]}/{window_name}: {e}")
                summary["markets_updated"] += 1
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning(f"[ROI_TRACKER] tick aborted mid-loop: {e}")
            summary["errors"].append(f"tick_loop: {e}")

        return summary

    # ── Readers ──

    def get_roi(self, cid: str, window: str = "24h") -> Optional[MarketROISnapshot]:
        """Read most recent snapshot for (cid, window). None if unseen."""
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT condition_id, window, window_end_ts, reward_earned, "
                "fill_loss, capital_committed_avg, roi, fill_count, "
                "fill_rate_per_hour, samples, last_updated "
                "FROM market_roi WHERE condition_id = ? AND window = ?",
                (cid, window),
            ).fetchone()
            conn.close()
            return MarketROISnapshot.from_row(row) if row else None
        except Exception as e:
            log.debug(f"[ROI_TRACKER] get_roi failed: {e}")
            return None

    def get_all_for_window(self, window: str = "24h") -> list[MarketROISnapshot]:
        """Read every market's most recent snapshot for the given window."""
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                "SELECT condition_id, window, window_end_ts, reward_earned, "
                "fill_loss, capital_committed_avg, roi, fill_count, "
                "fill_rate_per_hour, samples, last_updated "
                "FROM market_roi WHERE window = ?",
                (window,),
            ).fetchall()
            conn.close()
            return [MarketROISnapshot.from_row(r) for r in rows]
        except Exception as e:
            log.debug(f"[ROI_TRACKER] get_all_for_window failed: {e}")
            return []

    def get_global_summary(self, window: str = "24h") -> dict:
        """Aggregate across all markets for the window.

        Returns dict with: total_reward, total_loss, total_capital,
        daily_roi (annualised — over window length), n_markets,
        n_loss_markets, n_reward_markets, fill_count_total.
        """
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT COALESCE(SUM(reward_earned), 0), "
                "       COALESCE(SUM(fill_loss), 0), "
                "       COALESCE(SUM(capital_committed_avg), 0), "
                "       COUNT(*), "
                "       SUM(CASE WHEN fill_loss > 0 THEN 1 ELSE 0 END), "
                "       SUM(CASE WHEN reward_earned > 0 THEN 1 ELSE 0 END), "
                "       COALESCE(SUM(fill_count), 0) "
                "FROM market_roi WHERE window = ?",
                (window,),
            ).fetchone()
            conn.close()
            if not row:
                return {}
            tr, tl, tc, nm, nlm, nrm, fct = row
            denom = max(float(tc), 0.01)
            return {
                "window": window,
                "total_reward": float(tr),
                "total_loss": float(tl),
                "total_capital": float(tc),
                "daily_roi": (float(tr) - float(tl)) / denom,
                # FX-085: Ground Rule 1 scorecard — GROSS rewards earned per $ of
                # capital committed over the window. Distinct from daily_roi
                # (which nets out losses): capital_efficiency answers "how much
                # reward is each committed dollar farming?", the metric Rule 1
                # optimizes. Was previously UNMEASURED (eval gap). Meaningful
                # only when total_capital is real (>0); the consumer guards on it.
                "capital_efficiency": float(tr) / denom,
                "n_markets": int(nm),
                "n_loss_markets": int(nlm or 0),
                "n_reward_markets": int(nrm or 0),
                "fill_count_total": int(fct),
            }
        except Exception as e:
            log.debug(f"[ROI_TRACKER] get_global_summary failed: {e}")
            return {}

    # ── Maintenance ──

    def prune_old_snapshots(self, retain_secs: float = 86400.0 * 14) -> int:
        """Delete capital_committed_snapshots older than `retain_secs`.

        Default 14 days — covers all WINDOWS plus a safety margin. Run
        periodically (e.g., once per oversight cycle) to keep the table bounded.
        Returns count of rows deleted.
        """
        try:
            cutoff = self._now() - retain_secs
            conn = sqlite3.connect(self.db_path)
            cur = conn.execute(
                "DELETE FROM capital_committed_snapshots WHERE ts < ?",
                (cutoff,),
            )
            deleted = cur.rowcount
            conn.commit()
            conn.close()
            return int(deleted)
        except Exception as e:
            log.debug(f"[ROI_TRACKER] prune failed: {e}")
            return 0
