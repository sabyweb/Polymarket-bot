"""Rebalance engine — churn control for allocation changes.

Compares new allocations against current positions and suppresses
small changes that would cause unnecessary order cancellation/replacement.
"""

import logging

from oversight.data_collector import _connect_db

log = logging.getLogger("profit.rebalance")

# Minimum change thresholds to trigger rebalance
MIN_REBALANCE_PCT = 0.15      # 15% relative change
MIN_REBALANCE_SHARES = 10     # or 10 shares absolute
MIN_REBALANCE_USD = 5.0       # or $5 capital change


def compute_deltas(
    new_allocations: list[dict],
    db_path: str,
) -> list[dict]:
    """Compare new allocations against current deployed state.

    For each deploy market, checks if the change is significant enough
    to warrant rebalancing. Small deltas get reverted to current values
    to prevent order churn.

    Attaches metadata fields for logging:
      _rebalance_action: "enter" | "increase" | "decrease" | "exit" | "hold"
      _rebalance_delta: shares change

    Returns the same list with metadata attached and small-delta markets
    reverted to current shares.
    """
    # Load current positions from DB
    current_shares = _load_current_shares(db_path)

    for alloc in new_allocations:
        cid = alloc["condition_id"]
        new_shares = alloc.get("shares_per_side", 0)
        cur_shares = current_shares.get(cid, 0)

        if alloc.get("action") != "deploy":
            if cur_shares > 0:
                alloc["_rebalance_action"] = "exit"
                alloc["_rebalance_delta"] = -cur_shares
            else:
                alloc["_rebalance_action"] = "none"
                alloc["_rebalance_delta"] = 0
            continue

        if cur_shares == 0:
            # New market — always enter
            alloc["_rebalance_action"] = "enter"
            alloc["_rebalance_delta"] = new_shares
            continue

        delta = new_shares - cur_shares
        abs_delta = abs(delta)
        pct_delta = abs_delta / cur_shares if cur_shares > 0 else 1.0

        # Check if change is significant enough
        if (pct_delta < MIN_REBALANCE_PCT
                and abs_delta < MIN_REBALANCE_SHARES):
            # Below threshold — keep current allocation (no churn)
            alloc["shares_per_side"] = cur_shares
            # Recalculate est_capital_cost for consistency
            spread = alloc.get("max_spread", 0.045)
            s = spread if spread > 0 else 0.045
            cpb = 2 * max(0.05, (1.0 - 2 * s) / 2)
            alloc["est_capital_cost"] = round(cur_shares * cpb, 2)
            alloc["_rebalance_action"] = "hold"
            alloc["_rebalance_delta"] = 0
        elif delta > 0:
            alloc["_rebalance_action"] = "increase"
            alloc["_rebalance_delta"] = delta
        else:
            alloc["_rebalance_action"] = "decrease"
            alloc["_rebalance_delta"] = delta

    # Log rebalance summary
    actions = {}
    for a in new_allocations:
        act = a.get("_rebalance_action", "none")
        actions[act] = actions.get(act, 0) + 1
    if any(v > 0 for k, v in actions.items() if k != "none"):
        log.info(f"Rebalance: {dict(actions)}")

    return new_allocations


def _load_current_shares(db_path: str) -> dict[str, int]:
    """Load current deployed shares per market from active_orders table.

    Falls back to positions table if active_orders not available.
    """
    result: dict[str, int] = {}
    try:
        db = _connect_db(db_path)
        # Try active_orders first (most accurate — what's actually on exchange)
        try:
            rows = db.execute(
                "SELECT condition_id, SUM(shares) "
                "FROM active_orders "
                "WHERE order_type = 'buy' "
                "GROUP BY condition_id"
            ).fetchall()
            for r in rows:
                if r[0] and r[1]:
                    result[r[0]] = int(r[1])
        except Exception:
            pass  # table may not exist

        # Fall back to positions if active_orders empty
        if not result:
            try:
                rows = db.execute(
                    "SELECT condition_id, yes_shares, no_shares "
                    "FROM positions "
                    "WHERE yes_shares > 0 OR no_shares > 0"
                ).fetchall()
                for r in rows:
                    cid = r[0]
                    # Use max of yes/no as proxy for deployed size per side
                    result[cid] = int(max(r[1] or 0, r[2] or 0))
            except Exception:
                pass

        db.close()
    except Exception as e:
        log.debug(f"Current shares load failed: {e}")

    return result
