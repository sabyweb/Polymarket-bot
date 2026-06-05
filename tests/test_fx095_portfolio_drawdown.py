"""FX-095 — cash+inventory drawdown kill (Becerra replay)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simple_allocator import SimpleAllocator, KILL_DRAWDOWN_FRAC  # noqa: E402
from portfolio_value import compute_portfolio_value, compute_drawdown  # noqa: E402


class TestFX095PortfolioDrawdown(unittest.TestCase):

    def test_becerra_replay_no_false_kill(self):
        """Cash drops $106, ~$100 hedged pair at par → drawdown < 1%."""
        cash = 1012.0
        peak = 1118.0
        positions = {
            "0xa5": {
                "yes_shares": 100.0, "yes_avg_price": 0.787,
                "no_shares": 100.0, "no_avg_price": 0.277,
            },
        }
        mids = {"0xa5": 0.50}
        portfolio = compute_portfolio_value(cash, positions, mids)
        dd = compute_drawdown(portfolio, peak)
        self.assertLess(dd, 0.01)
        a = SimpleAllocator.__new__(SimpleAllocator)
        kill, _ = a.check_kill_switch(
            wallet_usd=cash,
            portfolio_value_usd=portfolio,
            portfolio_peak_usd=peak,
            realized_loss_24h=0.0,
        )
        self.assertFalse(kill)

    def test_true_drawdown_kills(self):
        a = SimpleAllocator.__new__(SimpleAllocator)
        peak = 1000.0
        portfolio = peak * (1 - KILL_DRAWDOWN_FRAC) - 10
        kill, reason = a.check_kill_switch(
            wallet_usd=portfolio,
            portfolio_value_usd=portfolio,
            portfolio_peak_usd=peak,
            realized_loss_24h=0.0,
        )
        self.assertTrue(kill)
        self.assertIn("drawdown", reason.lower())

    def test_cash_only_fallback_when_no_positions(self):
        a = SimpleAllocator.__new__(SimpleAllocator)
        kill, _ = a.check_kill_switch(
            wallet_usd=500.0,
            portfolio_value_usd=500.0,
            portfolio_peak_usd=1000.0,
            realized_loss_24h=0.0,
        )
        self.assertTrue(kill)


if __name__ == "__main__":
    unittest.main()
