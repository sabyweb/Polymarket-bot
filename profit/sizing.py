"""Depth-aware position sizing.

Converts capital allocation to share count, respecting:
  - exchange min_size
  - per-market capital cap
  - queue depth (avoid Q-score dilution)
"""

import logging

log = logging.getLogger("profit.sizing")

# Reuse the same cost formula from allocation_writer
def _cost_per_share_both(spread: float) -> float:
    """Estimated cost for one share on both sides (YES + NO)."""
    s = spread if spread > 0 else 0.045
    return 2 * max(0.05, (1.0 - 2 * s) / 2)


def compute_shares(
    allocated_capital: float,
    spread: float,
    min_size: float,
    depth_ahead: float = 0.0,
    max_per_market: float = 200.0,
) -> tuple[int, float]:
    """Convert capital allocation to share count.

    Returns (shares_per_side, est_capital_cost).

    Sizing logic:
    1. Base: allocated_capital / cost_per_share
    2. Floor: min_size
    3. Cap: max_per_market / cost_per_share
    4. Depth-aware: reduce if shares > 2× depth_ahead (Q-score dilution)
    """
    cpb = _cost_per_share_both(spread)
    if cpb <= 0 or allocated_capital <= 0:
        shares = int(min_size)
        return shares, shares * _cost_per_share_both(spread)

    # Base sizing from allocated capital
    raw_shares = int(allocated_capital / cpb)

    # Floor: must cover exchange minimum
    shares = max(raw_shares, int(min_size))

    # Cap: per-market exposure limit
    max_shares = int(max_per_market / cpb)
    shares = min(shares, max(max_shares, int(min_size)))

    # Depth-aware reduction: placing more than 2× depth ahead means
    # our order sits deep in the queue → earns less reward per share
    if depth_ahead > 0 and shares > depth_ahead * 2:
        depth_cap = max(int(min_size), int(depth_ahead * 2))
        if depth_cap < shares:
            log.debug(
                f"Depth cap: {shares} → {depth_cap} "
                f"(depth_ahead={depth_ahead:.0f})"
            )
            shares = depth_cap

    est_cost = round(shares * cpb, 2)
    return shares, est_cost


def estimate_slippage(
    shares: int,
    total_same_depth: float,
) -> float:
    """Estimate fill slippage as fraction (0.0–1.0).

    Used by allocator to discount capital for thin markets.
    """
    if total_same_depth <= 0:
        return 0.10  # conservative default
    return min(0.5, shares / (total_same_depth + shares))
