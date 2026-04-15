"""Regime detection — classifies the current market environment.

A "hostile" regime is one where fills are arriving much faster than normal
— other resolvers are picking us off. In hostile regimes we reduce the
capital we deploy (the safety layer still overrides via SafetyController;
this is a softer, profit-side throttle).

Detection signal:
  fill_rate_per_hour := fills_in_last_1h / max(active_markets, 1)

  > HOSTILE_THRESHOLD → "hostile"
  otherwise            → "normal"

Invariants (STEP 12):
  4. Hostile scaling removes <= 30% of deployable capital (0.7× floor).
  5. Missing data → "normal" (fail open here, because the safety layer
     still protects us; we don't want regime detection to silently
     starve the bot when the fills table is empty).
  6. Deterministic — no random sampling.
"""

import logging
import time

from oversight.data_collector import _connect_db

log = logging.getLogger("profit.regime")

# STEP 8 threshold: fills-per-active-market-per-hour.
# Calibrated against observed behaviour: a healthy market typically
# fills <0.5 times/hour (one fill every 2h). Above 1.5 fills/hr/market
# we're systematically being picked off.
HOSTILE_THRESHOLD = 1.5

# 1-hour detection window
REGIME_WINDOW_SECS = 3600

# "Active" = market has placed an order within the last 6h.
# Using active_orders alone misses markets with in-flight unwinds; using
# orders_placed captures every market we've participated in recently.
ACTIVE_WINDOW_SECS = 6 * 3600

# STEP 9: Hostile scaling factor. Drops deployable capital to 70% —
# exactly 30% reduction, matching invariant #4.
HOSTILE_CAPITAL_SCALE = 0.70

REGIME_NORMAL = "normal"
REGIME_HOSTILE = "hostile"


def detect_regime(db_path: str) -> str:
    """Classify the current regime from recent fill activity.

    Returns "hostile" or "normal". Never raises.
    """
    now = time.time()
    fill_cutoff = now - REGIME_WINDOW_SECS
    active_cutoff = now - ACTIVE_WINDOW_SECS

    try:
        db = _connect_db(db_path)
        fills_1h_row = db.execute(
            "SELECT COUNT(*) FROM fills "
            "WHERE ts > ? AND condition_id != '__FILL_STORM__'",
            (fill_cutoff,),
        ).fetchone()
        active_row = db.execute(
            "SELECT COUNT(DISTINCT condition_id) FROM orders_placed "
            "WHERE ts > ?",
            (active_cutoff,),
        ).fetchone()
        db.close()
    except Exception as e:
        # Invariant 5 — missing data must not crash the allocator
        log.warning(f"regime detect failed (defaulting to normal): {e}")
        return REGIME_NORMAL

    fills_1h = int(fills_1h_row[0] if fills_1h_row else 0)
    active = int(active_row[0] if active_row else 0)

    if active <= 0:
        # No activity means no picking-off risk by definition
        log.debug(f"[REGIME] no active markets → normal (fills_1h={fills_1h})")
        return REGIME_NORMAL

    fill_rate = fills_1h / active
    regime = REGIME_HOSTILE if fill_rate > HOSTILE_THRESHOLD else REGIME_NORMAL

    log.info(
        f"[REGIME] fills_1h={fills_1h} active={active} "
        f"rate={fill_rate:.2f} → {regime}"
    )
    return regime
