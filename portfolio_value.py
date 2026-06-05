"""FX-095 — cash + marked inventory portfolio value."""

from __future__ import annotations

from portfolio_mark import position_mark_value


def compute_portfolio_value(
    cash_usd: float,
    positions: dict,
    midpoints: dict[str, float],
) -> float:
    """Total portfolio = cash + Σ marked position value.

    Args:
        cash_usd: USDC/pUSD cash balance.
        positions: {cid: {yes_shares, yes_avg_price, no_shares, no_avg_price}}.
        midpoints: {cid: midpoint} — missing/invalid → cost-basis fallback per leg.
    """
    if cash_usd < 0:
        cash_usd = 0.0
    inventory = 0.0
    for cid, pos in positions.items():
        mid = float(midpoints.get(cid, 0.0) or 0.0)
        ys = float(pos.get("yes_shares", 0.0) or 0.0)
        ya = float(pos.get("yes_avg_price", 0.0) or 0.0)
        ns = float(pos.get("no_shares", 0.0) or 0.0)
        na = float(pos.get("no_avg_price", 0.0) or 0.0)
        inventory += position_mark_value(ys, ya, ns, na, mid)
    return cash_usd + inventory


def compute_drawdown(current: float, peak: float) -> float:
    """Drawdown fraction in [0, 1]. Returns 0 if peak <= 0."""
    if peak <= 0 or current <= 0:
        return 0.0 if current >= peak else 1.0
    if current >= peak:
        return 0.0
    return 1.0 - (current / peak)
