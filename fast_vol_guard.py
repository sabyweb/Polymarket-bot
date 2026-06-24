"""FX-098: farmer-side fast-volatility timeout guard.

Detects rapid midpoint movement from ``book_snapshots`` and, when triggered,
cancels resting BUY orders and blocks new placements for a configurable
 cooldown. Fail-open by design: sparse snapshots or DB errors do NOT trigger
timeouts. Cohort-gated so the effect can be measured in an A/B experiment.
"""

import logging
import time

from config import cfg
from models import MarketState

log = logging.getLogger("reward_farmer")


def _guard_applies_to_market(cid: str) -> bool:
    """Return True when the fast-vol guard should run for this market.

    If ``RF_FAST_VOL_COHORT_ONLY`` is negative, or if the A/B experiment is
    disabled, the guard applies to every market. When A/B is enabled and a
    non-negative cohort is configured, only markets in that cohort are treated.
    """
    cohort_only = cfg("RF_FAST_VOL_COHORT_ONLY")
    if cohort_only is None or int(cohort_only) < 0:
        return True
    if not cfg("RF_AB_EXPERIMENT_ENABLED"):
        return True
    try:
        from ab.cohort import cohort
        n = int(cfg("RF_AB_COHORT_COUNT") or 1)
        return cohort(cid, n) == int(cohort_only)
    except Exception:
        # Cohort lookup failure must never disable the guard globally.
        return True


def check_fast_vol_timeout(ms: MarketState, db, now: float | None = None) -> bool:
    """Return True if ``ms`` is (or becomes) fast-volatility timed out.

    If a previous timeout is still active, returns True immediately without
    touching the DB. Otherwise, queries ``book_snapshots`` for ``ms.cid`` and
    sets ``ms.fast_vol_timeout_until`` when either:

      - midpoint range over the last 30s >= ``RF_FAST_VOL_30S_CENTS``
      - midpoint range over the last 60s >= ``RF_FAST_VOL_60S_CENTS``

    Requires at least two snapshots in a window to compute a range (fail-open
    on sparse data). DB errors are logged and treated as no-timeout.
    """
    if now is None:
        now = time.time()

    # Already under an active timeout — enforce it without querying.
    if ms.fast_vol_timeout_until > now:
        return True

    if not _guard_applies_to_market(ms.cid):
        return False

    threshold_30 = float(cfg("RF_FAST_VOL_30S_CENTS") or 0.0)
    threshold_60 = float(cfg("RF_FAST_VOL_60S_CENTS") or 0.0)
    if threshold_30 <= 0 and threshold_60 <= 0:
        return False

    timeout_secs = float(cfg("RF_FAST_VOL_TIMEOUT_SECS") or 0.0)
    if timeout_secs <= 0:
        return False

    t_30 = now - 30.0
    t_60 = now - 60.0

    try:
        row = db._get_conn().execute(
            "SELECT "
            "  COUNT(*) AS cnt_60, "
            "  MAX(midpoint) AS max_60, "
            "  MIN(midpoint) AS min_60, "
            "  SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS cnt_30, "
            "  MAX(CASE WHEN ts >= ? THEN midpoint ELSE NULL END) AS max_30, "
            "  MIN(CASE WHEN ts >= ? THEN midpoint ELSE NULL END) AS min_30 "
            "FROM book_snapshots "
            "WHERE condition_id = ? AND ts >= ?",
            (t_30, t_30, t_30, ms.cid, t_60),
        ).fetchone()
    except Exception as e:
        log.warning(
            f"[FAST_VOL] cid={ms.cid[:12]} query_failed err={type(e).__name__}: {e}"
        )
        return False

    if not row:
        return False

    cnt_60 = int(row[0] or 0)
    max_60 = row[1]
    min_60 = row[2]
    cnt_30 = int(row[3] or 0)
    max_30 = row[4]
    min_30 = row[5]

    range_60 = (max_60 - min_60) if cnt_60 >= 2 and max_60 is not None and min_60 is not None else 0.0
    range_30 = (max_30 - min_30) if cnt_30 >= 2 and max_30 is not None and min_30 is not None else 0.0

    triggered = False
    if threshold_60 > 0 and range_60 >= threshold_60:
        triggered = True
    if threshold_30 > 0 and range_30 >= threshold_30:
        triggered = True

    if triggered:
        ms.fast_vol_timeout_until = now + timeout_secs
        log.info(
            f"[FAST_VOL] TRIGGERED cid={ms.cid[:12]} "
            f"range_30s={range_30:.3f}/{threshold_30:.3f} "
            f"range_60s={range_60:.3f}/{threshold_60:.3f} "
            f"timeout_until={ms.fast_vol_timeout_until:.0f} | {ms.question[:30]}"
        )
        return True

    return False
