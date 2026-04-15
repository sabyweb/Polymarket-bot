"""Per-market reward attribution.

Polymarket's /rewards/earned endpoint returns an aggregate daily number —
it doesn't tell us which market earned which dollar. Without attribution
we can't train per-market reward models, can't compute per-market PnL,
and can't feed the bandit signal.

Attribution formula (STEP 7):

    contribution[m] = scoring_seconds[m] * daily_rate[m]
    total           = sum(contribution[m] for m in active_markets)
    share[m]        = contribution[m] / total
    reward[m]       = share[m] * total_reward_payout

This uses `reward_daily_markets.scoring_seconds` as the per-market
time-on-book signal and `daily_rate` as the intensity signal — their
product is the market's proportional claim on the daily reward pot.
The portfolio sum matches the raw total payout exactly (STEP 12 #3).

Storage: `reward_attribution(market_id, date, reward_usd)` with a
composite primary key so re-running the computation is idempotent.

Invariants:
  3. sum(reward_usd for date D) == total_reward_payout(D) (up to float eps)
  5. Missing data → return empty result; never crash.
  6. Deterministic — no random sampling.
"""

import logging
from datetime import datetime, timezone

from oversight.data_collector import _connect_db

log = logging.getLogger("calibration.attribution")

# Invariant 3 tolerance: floating-point comparison for sum-preservation
SUM_CHECK_TOLERANCE = 1e-6


def _ensure_table(db_path: str) -> None:
    """Create reward_attribution table if missing."""
    try:
        db = _connect_db(db_path)
        db.execute(
            """CREATE TABLE IF NOT EXISTS reward_attribution (
                market_id   TEXT NOT NULL,
                date        TEXT NOT NULL,
                reward_usd  REAL NOT NULL,
                PRIMARY KEY (market_id, date)
            )"""
        )
        db.commit()
        db.close()
    except Exception as e:
        log.warning(f"reward_attribution table init failed: {e}")


def compute_attribution(
    db_path: str,
    date_str: str | None = None,
) -> dict[str, float]:
    """Compute and persist per-market reward attribution for `date_str`.

    Args:
        db_path: SQLite path.
        date_str: YYYY-MM-DD; defaults to today UTC.

    Returns {market_id: reward_usd}. Empty on missing data.

    Never raises — all failures are logged and the function returns {}.
    """
    _ensure_table(db_path)

    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        db = _connect_db(db_path)

        # Total payout for the day (REWARD + MAKER_REBATE combined).
        total_row = db.execute(
            "SELECT total_combined_usd FROM reward_daily WHERE date = ?",
            (date_str,),
        ).fetchone()
        if not total_row or not total_row[0]:
            db.close()
            log.info(f"[ATTRIBUTION] No payout recorded for {date_str}")
            return {}
        total_reward = float(total_row[0])

        # Per-market contribution = scoring_seconds * daily_rate
        rows = db.execute(
            "SELECT condition_id, scoring_seconds, daily_rate "
            "FROM reward_daily_markets "
            "WHERE date = ? AND scoring_seconds > 0 AND daily_rate > 0",
            (date_str,),
        ).fetchall()
        db.close()
    except Exception as e:
        log.warning(f"[ATTRIBUTION] query failed: {e}")
        return {}

    if not rows:
        log.info(f"[ATTRIBUTION] No active markets on {date_str}")
        return {}

    contributions: dict[str, float] = {}
    for r in rows:
        cid = r[0]
        secs = float(r[1] or 0)
        rate = float(r[2] or 0)
        contrib = secs * rate
        if contrib > 0:
            contributions[cid] = contributions.get(cid, 0.0) + contrib

    total_contrib = sum(contributions.values())
    if total_contrib <= 0:
        log.info(f"[ATTRIBUTION] Zero total contribution on {date_str}")
        return {}

    # Proportional split. Invariant 3: sum MUST equal total_reward exactly
    # (up to float eps). We assign the residual to the top contributor so
    # the sum is preserved under float rounding.
    attribution: dict[str, float] = {}
    accumulated = 0.0
    sorted_cids = sorted(contributions, key=contributions.get, reverse=True)
    for cid in sorted_cids[1:]:
        share = contributions[cid] / total_contrib
        reward = share * total_reward
        attribution[cid] = reward
        accumulated += reward
    # Top contributor receives exactly the residual
    top_cid = sorted_cids[0]
    attribution[top_cid] = total_reward - accumulated

    # Persist
    try:
        db = _connect_db(db_path)
        db.executemany(
            "INSERT INTO reward_attribution (market_id, date, reward_usd) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(market_id, date) DO UPDATE SET "
            "reward_usd=excluded.reward_usd",
            [(cid, date_str, reward) for cid, reward in attribution.items()],
        )
        db.commit()
        db.close()
    except Exception as e:
        log.warning(f"[ATTRIBUTION] persist failed: {e}")
        return {}

    # Invariant 3 check (logged as warning if somehow violated)
    sum_attributed = sum(attribution.values())
    if abs(sum_attributed - total_reward) > SUM_CHECK_TOLERANCE:
        log.warning(
            f"[ATTRIBUTION] sum mismatch: ${sum_attributed:.6f} vs "
            f"total ${total_reward:.6f} (diff {sum_attributed-total_reward:.2e})"
        )

    log.info(
        f"[ATTRIBUTION] {date_str}: ${total_reward:.2f} split across "
        f"{len(attribution)} markets"
    )
    return attribution


def get_attribution_error(
    db_path: str,
    date_str: str | None = None,
) -> float:
    """Estimate attribution error as relative sum-mismatch.

    Returns a number in [0, 1]. 0 means perfect reconciliation,
    higher means the attributed sum drifted from the recorded total.
    Used by the optional confidence-adjustment hook (STEP 10).
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        db = _connect_db(db_path)
        attr_row = db.execute(
            "SELECT SUM(reward_usd) FROM reward_attribution WHERE date = ?",
            (date_str,),
        ).fetchone()
        total_row = db.execute(
            "SELECT total_combined_usd FROM reward_daily WHERE date = ?",
            (date_str,),
        ).fetchone()
        db.close()
    except Exception as e:
        log.debug(f"[ATTRIBUTION] error query failed: {e}")
        return 0.0

    attr_sum = float(attr_row[0]) if attr_row and attr_row[0] else 0.0
    total = float(total_row[0]) if total_row and total_row[0] else 0.0

    if total <= 0:
        return 0.0
    return abs(attr_sum - total) / total
