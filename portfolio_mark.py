"""Shared portfolio marking helpers (FX-084, FX-095, FX-096).

All avg_price values are YES-equivalent per state.py SidePosition convention.
"""

from __future__ import annotations


def clob_cost(side: str, yes_equiv_avg: float) -> float:
    """CLOB cost per share for one side."""
    if yes_equiv_avg <= 0:
        return 0.0
    return yes_equiv_avg if side == "yes" else (1.0 - yes_equiv_avg)


def mark_price(side: str, midpoint: float) -> float:
    """Mark-to-market price per share (CLOB terms)."""
    return midpoint if side == "yes" else (1.0 - midpoint)


def leg_unrealized_pnl(
    side: str,
    shares: float,
    yes_equiv_avg: float,
    midpoint: float,
) -> float:
    """Unrealized PnL for one leg (positive = gain, negative = loss)."""
    if shares <= 0 or yes_equiv_avg <= 0:
        return 0.0
    if midpoint <= 0.0 or midpoint >= 1.0:
        return 0.0
    cost = clob_cost(side, yes_equiv_avg)
    mkt = mark_price(side, midpoint)
    return shares * (mkt - cost)


def leg_max_loss(side: str, shares: float, yes_equiv_avg: float) -> float:
    """Maximum possible loss on a leg (cost basis)."""
    cost = clob_cost(side, yes_equiv_avg)
    if shares <= 0 or cost <= 0:
        return 0.0
    return shares * cost


def capped_leg_loss(
    side: str,
    shares: float,
    yes_equiv_avg: float,
    midpoint: float,
) -> float:
    """Positive loss amount for one leg, capped at cost basis."""
    pnl = leg_unrealized_pnl(side, shares, yes_equiv_avg, midpoint)
    if pnl >= 0:
        return 0.0
    return min(-pnl, leg_max_loss(side, shares, yes_equiv_avg))


def net_unrealized_loss(
    yes_shares: float,
    yes_avg: float,
    no_shares: float,
    no_avg: float,
    midpoint: float,
    unknown_floor: bool = False,
) -> tuple[float, int]:
    """Per-market net unrealized loss (positive = underwater) and marked legs.

    Args:
        unknown_floor: When True, legs with unknown cost basis (avg <= 0) are
            included using the current midpoint as a conservative placeholder
            (PnL = 0, max_loss = current value). This prevents orphan/startup
            positions from being invisible to the unrealized-loss kill.
    """
    if not (0 < midpoint < 1):
        return 0.0, 0
    marked = 0
    net_pnl = 0.0
    max_loss = 0.0
    legs = (
        ("yes", yes_shares, yes_avg),
        ("no", no_shares, no_avg),
    )
    for side, shares, avg in legs:
        if shares <= 0:
            continue
        if avg <= 0:
            if not unknown_floor:
                continue
            avg = midpoint
        net_pnl += leg_unrealized_pnl(side, shares, avg, midpoint)
        max_loss += leg_max_loss(side, shares, avg)
        marked += 1
    if net_pnl >= 0:
        return 0.0, marked
    return min(-net_pnl, max_loss), marked


def portfolio_unrealized_loss(
    legs: list[tuple[str, float, float, float]],
    unknown_floor: bool = False,
) -> tuple[float, int]:
    """Global net unrealized loss across all legs (gains offset losses).

    Each leg: (side, shares, yes_equiv_avg, midpoint).
    Returns (positive_loss, marked_leg_count).

    Args:
        unknown_floor: When True, legs with unknown cost basis (avg <= 0) are
            included using their own midpoint as a conservative placeholder.
    """
    net_pnl = 0.0
    max_loss = 0.0
    marked = 0
    for side, shares, avg, mid in legs:
        if shares <= 0 or not (0 < mid < 1):
            continue
        if avg <= 0:
            if not unknown_floor:
                continue
            avg = mid
        net_pnl += leg_unrealized_pnl(side, shares, avg, mid)
        max_loss += leg_max_loss(side, shares, avg)
        marked += 1
    if net_pnl >= 0:
        return 0.0, marked
    return min(-net_pnl, max_loss), marked


def position_mark_value(
    yes_shares: float,
    yes_avg: float,
    no_shares: float,
    no_avg: float,
    midpoint: float,
) -> float:
    """Mark-to-market USD value of held inventory."""
    total = 0.0
    if yes_shares > 0 and 0 < midpoint < 1:
        total += yes_shares * mark_price("yes", midpoint)
    if no_shares > 0 and 0 < midpoint < 1:
        total += no_shares * mark_price("no", midpoint)
    # Fail-open: unknown midpoint → cost basis fallback
    if total <= 0:
        if yes_shares > 0 and yes_avg > 0:
            total += yes_shares * clob_cost("yes", yes_avg)
        if no_shares > 0 and no_avg > 0:
            total += no_shares * clob_cost("no", no_avg)
    return total
