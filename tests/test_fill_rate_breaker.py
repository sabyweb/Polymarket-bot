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
        yes_price=0.50, agent_approved=True,
    )


def _make_lifecycle(ms):
    """Create an OrderLifecycle with minimal mocks, wired to the given MarketState.

    DB mock is configured with ``is_unliquidatable -> False`` so the new
    FX-007 gate doesn't short-circuit these tests.
    """
    from order_lifecycle import OrderLifecycle

    positions = MagicMock()
    positions.get_shares.return_value = 0
    positions.can_quote.return_value = True

    db = MagicMock()
    db.is_unliquidatable.return_value = False
    ol = OrderLifecycle(
        client=MagicMock(), db=db, positions=positions,
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


class TestKillFillHistorySeparation(unittest.TestCase):
    """FX-069: the fill-rate spike KILL reads MarketState.kill_fill_times
    (pruned to the 6h baseline), SEPARATE from fill_times (which can_place
    prunes to the 180s breaker window). Previously the kill read the
    180s-pruned fill_times, so its 6h baseline could never accumulate and it
    degenerated to '>=5 fills/180s', blind to slow bleed."""

    def test_can_place_prune_does_not_truncate_kill_history(self):
        ms = _make_ms()
        now = time.time()
        # Kill history spans ~5h; fill_times empty so can_place won't block.
        ms.kill_fill_times = [now - 10, now - 200, now - 3700, now - 18000]
        ms.fill_times = {"yes": [], "no": []}
        ol = _make_lifecycle(ms)
        ok, reason = ol.can_place(ms.cid, "yes", 5.0)
        self.assertTrue(ok, f"unexpected block: {reason}")
        self.assertEqual(
            ms.kill_fill_times, [now - 10, now - 200, now - 3700, now - 18000],
            "can_place must NOT prune kill_fill_times (FX-069 separation)",
        )

    def test_handle_fill_feeds_kill_history_and_prunes_to_6h(self):
        ms = _make_ms()
        now = time.time()
        # One stale (>6h) entry to prune + one fresh to keep.
        ms.kill_fill_times = [now - 25000, now - 100]
        ol = _make_lifecycle(ms)
        ol._dump_mgr = MagicMock()
        ol.positions.get_shares.return_value = 0       # no merge → dump path
        ol.positions.get_avg_price.return_value = 0.5
        slot = ms.orders["yes"]
        slot.shares = 10
        slot.price = 0.5
        with patch("alerts.alert_fill"):
            ol.handle_fill(ms, "yes", slot, actual_shares=10, actual_price=0.5)
        # Fresh fill appended to BOTH lists (same ts); stale >6h entry pruned.
        self.assertEqual(len(ms.fill_times["yes"]), 1)
        self.assertTrue(
            all(now - t < 21600 for t in ms.kill_fill_times),
            f"stale >6h entry should be pruned: {ms.kill_fill_times}",
        )
        self.assertEqual(len(ms.kill_fill_times), 2,
                         "fresh(now-100) + new(now) kept, stale(now-25000) pruned")
        self.assertIn(ms.fill_times["yes"][0], ms.kill_fill_times)


class TestGuardrailFillRateKill(unittest.TestCase):
    """FX-069: _guardrail_fill_rate_ratio reads kill_fill_times (full 6h),
    so it can see slow bleed AND real spikes — and ignores the 180s-pruned
    fill_times entirely."""

    def _farmer_with(self, ms):
        import reward_farmer as rf
        farmer = rf.RewardFarmer.__new__(rf.RewardFarmer)
        farmer.markets = {ms.cid: ms}
        return farmer

    def test_reads_full_6h_history_not_180s(self):
        from reward_farmer import FILL_RATE_SPIKE_FACTOR
        ms = _make_ms()
        now = time.time()
        # 13 fills across 6h, sparse in the last hour → NOT a spike, but the
        # baseline must reflect the full 6h (impossible to express pre-FX-069).
        ms.kill_fill_times = [now - s for s in
                              (20000, 18000, 16000, 14000, 12000, 10000,
                               8000, 6000, 5000, 4000, 3700, 1800, 600)]
        ms.fill_times = {"yes": [], "no": []}   # 180s-pruned → must NOT matter
        farmer = self._farmer_with(ms)
        ratio, short_count, base_count = farmer._guardrail_fill_rate_ratio()
        self.assertEqual(base_count, 13, "baseline must span the full 6h history")
        self.assertIsNotNone(ratio)
        self.assertLessEqual(ratio, FILL_RATE_SPIKE_FACTOR)

    def test_detects_spike(self):
        from reward_farmer import FILL_RATE_SPIKE_FACTOR
        ms = _make_ms()
        now = time.time()
        old = [now - s for s in (20000, 16000, 12000, 8000, 5000)]        # 5 over 6h
        recent = [now - s for s in (300, 250, 200, 150, 100, 60, 30, 10)]  # 8 in last 1h
        ms.kill_fill_times = old + recent
        farmer = self._farmer_with(ms)
        ratio, short_count, base_count = farmer._guardrail_fill_rate_ratio()
        self.assertIsNotNone(ratio)
        self.assertGreater(ratio, FILL_RATE_SPIKE_FACTOR,
                           f"clustered recent fills should trip the spike; ratio={ratio}")

    def test_min_baseline_failopen(self):
        ms = _make_ms()
        now = time.time()
        ms.kill_fill_times = [now - 100, now - 200]   # < MIN_FILL_BASELINE (5)
        farmer = self._farmer_with(ms)
        with self.assertLogs("reward_farmer", level="WARNING") as cap:
            ratio, short_count, base_count = farmer._guardrail_fill_rate_ratio()
        self.assertIsNone(ratio)
        self.assertTrue(any("missing_signal=fill_rate" in line for line in cap.output))


if __name__ == "__main__":
    unittest.main()
