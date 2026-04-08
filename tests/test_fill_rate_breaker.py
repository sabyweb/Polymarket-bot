"""Tests for the fill-rate breaker in OrderLifecycle.can_place()."""

import time
import unittest
from unittest.mock import MagicMock, patch

from models import MarketState, OrderSlot


def _make_ms(cid="test_cid"):
    """Create a minimal MarketState for fill-rate breaker testing."""
    return MarketState(
        cid=cid, question="Test?", yes_tid="y", no_tid="n",
        daily_rate=20.0, max_spread=0.10, min_size=10, tick_size=0.01,
        yes_price=0.50,
    )


def _make_lifecycle(ms):
    """Create an OrderLifecycle with minimal mocks, wired to the given MarketState."""
    from order_lifecycle import OrderLifecycle

    positions = MagicMock()
    positions.get_shares.return_value = 0
    positions.can_quote.return_value = True

    ol = OrderLifecycle(
        client=MagicMock(), db=MagicMock(), positions=positions,
        rewards=MagicMock(), markets={ms.cid: ms}, dry_run=True,
    )
    ol.capital_ceiling = None
    return ol


class TestFillRateBreaker(unittest.TestCase):
    """Fill-rate breaker: per-side and total thresholds."""

    def test_one_fill_per_side_not_blocked(self):
        """1 YES fill + 1 NO fill = 2 total, below threshold of 3 → allowed."""
        ms = _make_ms()
        now = time.time()
        ms.fill_times["yes"] = [now - 10]
        ms.fill_times["no"] = [now - 20]
        ol = _make_lifecycle(ms)
        ok, reason = ol.can_place(ms.cid, "yes", 5.0)
        self.assertTrue(ok, f"Should not be blocked, got reason={reason}")

    def test_two_fills_same_side_blocked(self):
        """2 fills on YES side → per-side threshold (2) hit → blocked."""
        ms = _make_ms()
        now = time.time()
        ms.fill_times["yes"] = [now - 10, now - 30]
        ms.fill_times["no"] = []
        ol = _make_lifecycle(ms)
        ok, reason = ol.can_place(ms.cid, "yes", 5.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "fill_rate_breaker")

    def test_three_total_fills_blocked(self):
        """1 YES + 2 NO = 3 total → total threshold (3) hit → blocked."""
        ms = _make_ms()
        now = time.time()
        ms.fill_times["yes"] = [now - 10]
        ms.fill_times["no"] = [now - 20, now - 40]
        ol = _make_lifecycle(ms)
        ok, reason = ol.can_place(ms.cid, "yes", 5.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "fill_rate_breaker")

    def test_expired_fills_not_counted(self):
        """Fills outside the window are pruned and don't count."""
        ms = _make_ms()
        now = time.time()
        # All fills are older than the default 180s window
        ms.fill_times["yes"] = [now - 200, now - 250]
        ms.fill_times["no"] = [now - 300]
        ol = _make_lifecycle(ms)
        ok, reason = ol.can_place(ms.cid, "yes", 5.0)
        self.assertTrue(ok, f"Expired fills should not block, got reason={reason}")

    def test_zero_fills_allowed(self):
        """No fills at all → allowed."""
        ms = _make_ms()
        ol = _make_lifecycle(ms)
        ok, reason = ol.can_place(ms.cid, "yes", 5.0)
        self.assertTrue(ok, f"Zero fills should not block, got reason={reason}")

    def test_per_side_triggers_before_total(self):
        """2 fills on one side triggers even if total is only 2 (below total threshold)."""
        ms = _make_ms()
        now = time.time()
        ms.fill_times["no"] = [now - 5, now - 10]
        ms.fill_times["yes"] = []
        ol = _make_lifecycle(ms)
        ok, reason = ol.can_place(ms.cid, "no", 5.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "fill_rate_breaker")


if __name__ == "__main__":
    unittest.main()
