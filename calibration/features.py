"""Feature extraction for calibration models.

Joins orders_placed to nearest book_snapshots and labels each order
as filled/cancelled/alive using fills and orders_cancelled tables.
All features are numeric with safe defaults. No None returns.
"""

import logging
import math
import time
from dataclasses import dataclass

from oversight.data_collector import _connect_db

log = logging.getLogger("calibration.features")


@dataclass
class OrderFeatures:
    """Feature vector for a single order placement event."""
    # Identity (not model inputs)
    condition_id: str
    side: str
    order_id: str
    placed_ts: float

    # Raw features
    order_price: float
    midpoint: float
    distance_from_mid: float
    spread: float
    depth_ahead: float
    total_same_depth: float
    opposite_depth_5c: float
    daily_rate: float
    agent_shares: float
    was_scoring: int  # 0 or 1

    # Normalized features (for cross-market pooling)
    rel_depth_ahead: float    # depth_ahead / max(agent_shares, 1)
    rel_spread: float         # spread / max(midpoint, 0.01)
    log_daily_rate: float     # log(1 + daily_rate)

    # Label (set after construction)
    outcome: str = "alive"    # "filled", "cancelled", "alive"
    duration_secs: float = 0.0  # time to event


@dataclass
class FillLossFeatures:
    """Feature vector for a fill-to-unwind pair."""
    condition_id: str
    side: str
    fill_ts: float
    slippage: float         # clob_cost - midpoint (>0 = adverse)
    spread_at_fill: float
    fill_size_shares: float
    midpoint_at_fill: float
    daily_rate: float
    loss_per_share: float   # clob_cost - sell_price (target)
    hold_duration_secs: float
    recency_weight: float   # exp(-age / half_life)


def build_training_set(
    db_path: str,
    since_ts: float = 0.0,
    max_age_days: float = 14.0,
) -> list[OrderFeatures]:
    """Build labeled training set from DB.

    For each order in orders_placed:
    1. Find nearest book_snapshot for features
    2. Check if filled (fills table) or cancelled (orders_cancelled)
    3. Compute duration to event

    Returns list of OrderFeatures with outcome and duration set.
    """
    cutoff = since_ts or (time.time() - max_age_days * 86400)
    try:
        db = _connect_db(db_path)
    except Exception as e:
        log.warning(f"Cannot open DB for training set: {e}")
        return []

    # Step 1: Load all BUY order placements
    try:
        orders = db.execute(
            "SELECT ts, condition_id, side, price, size, order_id "
            "FROM orders_placed WHERE order_type = 'BUY' AND ts > ? "
            "ORDER BY ts",
            (cutoff,),
        ).fetchall()
    except Exception as e:
        log.warning(f"Cannot query orders_placed: {e}")
        db.close()
        return []

    if not orders:
        db.close()
        return []

    # Step 2: Load fills and cancellations for matching
    try:
        fills = db.execute(
            "SELECT ts, condition_id, side, price, shares, clob_cost, "
            "midpoint, slippage, order_age_secs "
            "FROM fills WHERE ts > ? ORDER BY ts",
            (cutoff,),
        ).fetchall()
    except Exception:
        fills = []

    try:
        cancels = db.execute(
            "SELECT ts, order_id, condition_id, side, price, age_secs "
            "FROM orders_cancelled WHERE ts > ? ORDER BY ts",
            (cutoff,),
        ).fetchall()
    except Exception:
        cancels = []

    # Index cancels by order_id for O(1) lookup
    cancel_by_oid = {}
    for c in cancels:
        oid = c[1]
        if oid and oid not in cancel_by_oid:
            cancel_by_oid[oid] = c

    # Index fills by (condition_id, side) for matching
    fills_by_cs: dict[tuple, list] = {}
    for f in fills:
        key = (f[1], f[2])  # (condition_id, side)
        fills_by_cs.setdefault(key, []).append(f)

    # Step 3: For each order, find nearest book_snapshot and label
    results: list[OrderFeatures] = []
    now = time.time()

    for order in orders:
        op_ts, cid, side, price, size, oid = (
            order[0], order[1], order[2], order[3], order[4], order[5]
        )

        # Find nearest book_snapshot (within 2 min before or after)
        try:
            bs = db.execute(
                "SELECT spread, midpoint, bid_depth_5c, ask_depth_5c, "
                "total_bid_depth, total_ask_depth, "
                "our_bid_depth_ahead, our_ask_depth_ahead, "
                "daily_rate, agent_shares "
                "FROM book_snapshots "
                "WHERE condition_id = ? AND ts BETWEEN ? AND ? "
                "ORDER BY ABS(ts - ?) LIMIT 1",
                (cid, op_ts - 120, op_ts + 120, op_ts),
            ).fetchone()
        except Exception:
            bs = None

        # Extract features with safe defaults
        if bs:
            spread = max(bs[0] or 0, 0)
            mid = bs[1] or 0.5
            depth_ahead = (bs[6] if side == "yes" else bs[7]) or 0
            total_same = (bs[4] if side == "yes" else bs[5]) or 0
            opp_depth = (bs[3] if side == "yes" else bs[2]) or 0
            dr = bs[8] or 0
            ag_shares = bs[9] or size or 50
        else:
            spread = 0.045
            mid = 0.5
            depth_ahead = 0
            total_same = 0
            opp_depth = 0
            dr = 0
            ag_shares = size or 50

        dist_from_mid = abs(price - mid)

        # Check scoring status (within 5 min)
        was_scoring = 0
        try:
            ss = db.execute(
                "SELECT scoring FROM scoring_snapshots "
                "WHERE condition_id = ? AND ts BETWEEN ? AND ? "
                "ORDER BY ts DESC LIMIT 1",
                (cid, op_ts - 300, op_ts + 60),
            ).fetchone()
            if ss and ss[0]:
                was_scoring = 1
        except Exception:
            pass

        # Normalized features
        rel_da = depth_ahead / max(ag_shares, 1)
        rel_sp = spread / max(mid, 0.01)
        log_dr = math.log(1 + max(dr, 0))

        feat = OrderFeatures(
            condition_id=cid, side=side, order_id=oid or "",
            placed_ts=op_ts,
            order_price=price, midpoint=mid,
            distance_from_mid=dist_from_mid, spread=spread,
            depth_ahead=depth_ahead, total_same_depth=total_same,
            opposite_depth_5c=opp_depth,
            daily_rate=dr, agent_shares=ag_shares,
            was_scoring=was_scoring,
            rel_depth_ahead=rel_da, rel_spread=rel_sp,
            log_daily_rate=log_dr,
        )

        # Label: check if filled
        matched_fill = None
        cs_fills = fills_by_cs.get((cid, side), [])
        for f in cs_fills:
            f_ts, f_price = f[0], f[3]
            if f_ts > op_ts and f_ts < op_ts + 86400:
                if abs(f_price - price) < 0.03:
                    matched_fill = f
                    break

        if matched_fill:
            feat.outcome = "filled"
            feat.duration_secs = matched_fill[0] - op_ts
        elif oid and oid in cancel_by_oid:
            c = cancel_by_oid[oid]
            feat.outcome = "cancelled"
            c_age = c[5] or 0
            feat.duration_secs = c_age if c_age > 0 else max(0, c[0] - op_ts)
        else:
            feat.outcome = "alive"
            feat.duration_secs = now - op_ts

        results.append(feat)

    db.close()
    return results


def build_loss_training_set(
    db_path: str,
    since_ts: float = 0.0,
    max_age_days: float = 14.0,
    half_life_days: float = 7.0,
) -> list[FillLossFeatures]:
    """Build labeled training set for loss model.

    Joins fills to their nearest subsequent unwind on same (cid, side).
    """
    cutoff = since_ts or (time.time() - max_age_days * 86400)
    now = time.time()
    decay_rate = math.log(2) / (half_life_days * 86400)

    try:
        db = _connect_db(db_path)
    except Exception as e:
        log.warning(f"Cannot open DB for loss training: {e}")
        return []

    try:
        rows = db.execute(
            "SELECT f.condition_id, f.side, f.ts, f.shares, f.clob_cost, "
            "f.price, f.midpoint, f.slippage, "
            "u.sell_price, u.ts as unwind_ts, u.hold_duration_secs "
            "FROM fills f "
            "JOIN unwinds u ON u.condition_id = f.condition_id "
            "AND u.side = f.side AND u.ts > f.ts AND u.ts < f.ts + 86400 "
            "WHERE f.ts > ? "
            "ORDER BY f.ts",
            (cutoff,),
        ).fetchall()
    except Exception as e:
        log.warning(f"Loss training query failed: {e}")
        db.close()
        return []

    # Get spread at fill time from book_snapshots
    results: list[FillLossFeatures] = []
    for r in rows:
        cid, side, fill_ts = r[0], r[1], r[2]
        shares, clob_cost, fill_price = r[3], r[4], r[5]
        mid_at_fill, slippage = r[6] or 0.5, r[7] or 0.0
        sell_price = r[8]
        hold_dur = r[10] or 0

        # Get spread from nearest book_snapshot
        try:
            bs = db.execute(
                "SELECT spread, daily_rate FROM book_snapshots "
                "WHERE condition_id = ? AND ts BETWEEN ? AND ? "
                "ORDER BY ABS(ts - ?) LIMIT 1",
                (cid, fill_ts - 120, fill_ts + 120, fill_ts),
            ).fetchone()
            spread_at_fill = bs[0] if bs and bs[0] else 0.045
            dr = bs[1] if bs and bs[1] else 0
        except Exception:
            spread_at_fill = 0.045
            dr = 0

        loss_per_share = max(0, clob_cost - sell_price)
        age = now - fill_ts
        weight = math.exp(-decay_rate * age)

        results.append(FillLossFeatures(
            condition_id=cid, side=side, fill_ts=fill_ts,
            slippage=slippage, spread_at_fill=spread_at_fill,
            fill_size_shares=shares, midpoint_at_fill=mid_at_fill,
            daily_rate=dr, loss_per_share=loss_per_share,
            hold_duration_secs=hold_dur, recency_weight=weight,
        ))

    db.close()
    return results


def features_to_vector(feat: OrderFeatures) -> list[float]:
    """Convert OrderFeatures to a numeric vector for model input.

    Returns 10 features in fixed order. All guaranteed numeric.
    """
    return [
        feat.distance_from_mid,
        feat.spread,
        feat.depth_ahead,
        feat.opposite_depth_5c,
        feat.agent_shares,
        float(feat.was_scoring),
        feat.rel_depth_ahead,
        feat.rel_spread,
        feat.log_daily_rate,
        feat.order_price,
    ]


FEATURE_NAMES = [
    "distance_from_mid", "spread", "depth_ahead", "opposite_depth_5c",
    "agent_shares", "was_scoring", "rel_depth_ahead", "rel_spread",
    "log_daily_rate", "order_price",
]
NUM_FEATURES = len(FEATURE_NAMES)
