"""Tests for reward_tracker.record_cycle() Q-score accumulation.

Focus: the fix for the `max(market_q, our_q)` fallback bug that caused
q_share=1.0 saturation on every cycle where `order_book` was missing
(see memory file: project_market_q_fallback_bug.md).

After fix: samples only accumulate when BOTH our_q > 0 AND market_q > 0.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reward_tracker import RewardTracker, MarketStats


def _make_book(bids, asks):
    """Build an order-book dict in the shape produced by get_merged_book()."""
    return {
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
    }


class TestRecordCycleAccumulation(unittest.TestCase):
    """Verify the fix: record_cycle only accumulates when both q signals > 0."""

    def _make_tracker(self) -> RewardTracker:
        """Construct a RewardTracker with _load() no-op'd, then seed one market."""
        with patch.object(RewardTracker, "_load", lambda self: None):
            tracker = RewardTracker()
        tracker.markets["0xcid_test"] = MarketStats(
            condition_id="0xcid_test",
            question="Test market?",
            daily_rate=50.0,
            max_spread=0.045,
        )
        return tracker

    def _cycle_args(self, midpoint=0.50, bid_price=0.47, ask_price=0.53):
        """Standard arguments for record_cycle covering a two-sided market maker."""
        return dict(
            condition_id="0xcid_test",
            has_yes_order=True, has_no_order=True,
            bid_price=bid_price, ask_price=ask_price,
            inventory_usd=0.0, cooldown_active=False, skew_active=False,
            cycle_duration_secs=30.0,
            midpoint=midpoint,
            bid_size=50.0, ask_size=50.0,
        )

    def test_record_cycle_with_order_book_accumulates_correctly(self):
        """With a real book (market_q >> our_q), q_share = our_q / market_q < 1.0."""
        tracker = self._make_tracker()
        # Book with substantial competing depth at varying distances from midpoint.
        # Midpoint = 0.50, max_spread = 0.045. Competition: 1000 shares at each
        # of 0.495, 0.485, 0.475 on bid side (inside window), same on ask side.
        book = _make_book(
            bids=[(0.495, 1000.0), (0.485, 1000.0), (0.475, 1000.0)],
            asks=[(0.505, 1000.0), (0.515, 1000.0), (0.525, 1000.0)],
        )
        tracker.record_cycle(**self._cycle_args(), order_book=book)

        stats = tracker.markets["0xcid_test"]
        self.assertEqual(stats.q_score_samples, 1)
        self.assertGreater(stats.total_q_score, 0)
        self.assertGreater(stats.total_market_q, stats.total_q_score)  # we're NOT the whole pool
        q_share = stats.total_q_score / stats.total_market_q
        self.assertLess(q_share, 1.0)
        # With 50 shares vs ~6000 shares of competing depth, q_share should be small.
        self.assertLess(q_share, 0.1)

    def test_record_cycle_without_order_book_skips_sample(self):
        """The core fix: no book -> market_q=0 -> sample is skipped, NOT faked.

        Pre-fix code would have done: total_market_q += max(0, our_q) = our_q,
        producing q_share = 1.0. Post-fix: the `and market_q > 0` guard skips
        the sample entirely.
        """
        tracker = self._make_tracker()
        tracker.record_cycle(**self._cycle_args(), order_book=None)

        stats = tracker.markets["0xcid_test"]
        self.assertEqual(stats.q_score_samples, 0)
        self.assertEqual(stats.total_q_score, 0.0)
        self.assertEqual(stats.total_market_q, 0.0)

    def test_record_cycle_empty_order_book_skips_sample(self):
        """Empty book -> estimate_market_q returns 0 -> sample skipped."""
        tracker = self._make_tracker()
        tracker.record_cycle(
            **self._cycle_args(), order_book={"bids": [], "asks": []}
        )

        stats = tracker.markets["0xcid_test"]
        self.assertEqual(stats.q_score_samples, 0)
        self.assertEqual(stats.total_market_q, 0.0)

    def test_record_cycle_book_outside_reward_window_skips_sample(self):
        """Book whose levels are all OUTSIDE max_spread yields market_q=0."""
        tracker = self._make_tracker()
        # All levels well outside the ±0.045 reward window around midpoint 0.50.
        book = _make_book(
            bids=[(0.40, 1000.0), (0.30, 1000.0)],
            asks=[(0.60, 1000.0), (0.70, 1000.0)],
        )
        tracker.record_cycle(**self._cycle_args(), order_book=book)

        stats = tracker.markets["0xcid_test"]
        # Our orders at 0.47 / 0.53 are within 0.045 of midpoint,
        # so our_q > 0. But all book levels are outside -> market_q = 0.
        # Fix condition (both > 0) fails -> no sample accumulated.
        self.assertEqual(stats.q_score_samples, 0)

    def test_multiple_cycles_build_realistic_q_share(self):
        """Multiple samples accumulate correctly; q_share stays < 1.0."""
        tracker = self._make_tracker()
        book = _make_book(
            bids=[(0.495, 500.0), (0.485, 500.0)],
            asks=[(0.505, 500.0), (0.515, 500.0)],
        )
        # 5 cycles with book, 3 without (should skip).
        for _ in range(5):
            tracker.record_cycle(**self._cycle_args(), order_book=book)
        for _ in range(3):
            tracker.record_cycle(**self._cycle_args(), order_book=None)

        stats = tracker.markets["0xcid_test"]
        self.assertEqual(stats.q_score_samples, 5)  # only the 5 book cycles counted
        q_share = stats.total_q_score / stats.total_market_q
        self.assertLess(q_share, 1.0)


if __name__ == "__main__":
    unittest.main()
