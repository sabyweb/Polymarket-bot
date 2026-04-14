"""Phase 3 Profit Engine — portfolio-level capital optimization.

Replaces per-market allocation with:
  - Risk-adjusted scoring (EV * confidence * (1 - P_fill))
  - Portfolio-level capital budgeting
  - Depth-aware position sizing
  - Rebalance decisions with churn control
"""

from .allocator import allocate_portfolio

__all__ = ["allocate_portfolio"]
