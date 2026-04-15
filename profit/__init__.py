"""Phase 3 + 4 Profit / Alpha Engine — portfolio-level capital optimization.

Phase 3 — Profit Engine:
  - Risk-adjusted scoring  RAS = EV / (1 + p_fill * loss)
  - Portfolio-level capital budgeting
  - Depth-aware position sizing
  - Rebalance decisions with churn control
  - Correlation cluster caps

Phase 4 — Alpha Layer:
  - Thompson-sampling bandit multiplier (profit/bandit.py)
  - Hostile-regime detection (profit/regime.py)
"""

from .allocator import allocate_portfolio
from .bandit import Bandit
from .regime import detect_regime

__all__ = ["allocate_portfolio", "Bandit", "detect_regime"]
