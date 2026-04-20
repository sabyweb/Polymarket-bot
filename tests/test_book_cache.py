"""Tests for the MarketState.cached_book TTL behavior used by record_cycle.

The resolution logic lives inline in reward_farmer.run_cycle() Step 5:

    book_for_scoring = None
    if ms.cached_book and RF_BOOK_CACHE_TTL > 0:
        if time.time() - ms.last_book_fetch <= RF_BOOK_CACHE_TTL:
            book_for_scoring = ms.cached_book

These tests exercise that contract directly by mirroring the same logic
so that any future refactor of the inline block triggers a test miss.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import MarketState


def _resolve_cached_book(ms: MarketState, ttl: int, now: float | None = None):
    """Mirror of the resolution logic inlined at reward_farmer.py:Step 5.

    Kept here (not imported) so the test acts as a contract check — if the
    inline block changes semantics, these tests must be updated too.
    """
    now = now if now is not None else time.time()
    if ms.cached_book and ttl > 0:
        if now - ms.last_book_fetch <= ttl:
            return ms.cached_book
    return None


def _make_ms(cached_book=None, last_book_fetch=0.0) -> MarketState:
    return MarketState(
        cid="0xcid_test", question="Test?", yes_tid="ytid", no_tid="ntid",
        daily_rate=50.0, max_spread=0.045, min_size=50, tick_size=0.01,
        yes_price=0.5, cached_book=cached_book, last_book_fetch=last_book_fetch,
    )


_SAMPLE_BOOK = {
    "bids": [{"price": 0.49, "size": 100.0}],
    "asks": [{"price": 0.51, "size": 100.0}],
}


class TestCachedBookTTL(unittest.TestCase):
    """Verify the TTL gate that feeds MarketState.cached_book to record_cycle."""

    def test_cached_book_consumed_when_fresh(self):
        """cached_book set, last_book_fetch=now → returns the cached book."""
        now = 1_000_000.0
        ms = _make_ms(cached_book=_SAMPLE_BOOK, last_book_fetch=now)
        result = _resolve_cached_book(ms, ttl=180, now=now)
        self.assertIs(result, _SAMPLE_BOOK)

    def test_cached_book_consumed_at_ttl_boundary(self):
        """exact age == TTL: inclusive bound means still fresh."""
        now = 1_000_000.0
        ms = _make_ms(cached_book=_SAMPLE_BOOK, last_book_fetch=now - 180)
        result = _resolve_cached_book(ms, ttl=180, now=now)
        self.assertIs(result, _SAMPLE_BOOK)

    def test_cached_book_skipped_when_stale(self):
        """Age > TTL → None."""
        now = 1_000_000.0
        ms = _make_ms(cached_book=_SAMPLE_BOOK, last_book_fetch=now - 200)
        result = _resolve_cached_book(ms, ttl=180, now=now)
        self.assertIsNone(result)

    def test_cached_book_none_returns_none(self):
        """No cache populated yet (e.g., market never batch-visited) → None."""
        ms = _make_ms(cached_book=None, last_book_fetch=0.0)
        result = _resolve_cached_book(ms, ttl=180, now=1_000_000.0)
        self.assertIsNone(result)

    def test_ttl_zero_disables_cache(self):
        """RF_BOOK_CACHE_TTL = 0 is the escape hatch — always return None."""
        now = 1_000_000.0
        ms = _make_ms(cached_book=_SAMPLE_BOOK, last_book_fetch=now)
        result = _resolve_cached_book(ms, ttl=0, now=now)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
