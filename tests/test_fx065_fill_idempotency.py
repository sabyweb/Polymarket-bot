"""FX-065 — handle_fill must not double-count positions on a duplicate fill.

Pre-FX-065, OrderLifecycle.handle_fill called positions.record_fill BEFORE
and OUTSIDE the log_fill INSERT-OR-IGNORE idempotency boundary. The fills
table dedup'd on fill_event_id, but PositionStore did not — so a re-handled
fill (network retry, SDK-detect then stale-check on a grown partial,
drift-sweep overlap) added the shares to the position a second time,
inflating shares + corrupting VWAP. That corrupt cost basis then fed the
dump pnl (FX-066) and the 24h-loss kill math.

FX-065: guard record_fill with db.fill_event_exists(fill_event_id) — the
same key the fills table uses. Only the positions mutation is guarded; the
[FILL_WRITE] instrumentation and dump re-attempt are unchanged.
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import order_lifecycle  # noqa: E402


def _make_ms(cid="0xabc"):
    ms = types.SimpleNamespace()
    ms.cid = cid
    ms.question = "Will X happen?"
    ms.midpoint = 0.50
    ms.daily_rate = 100.0
    ms.last_fill_price = {"yes": 0.0, "no": 0.0}
    ms.fill_times = {"yes": [], "no": []}
    ms.kill_fill_times = []  # FX-069: mirror the real MarketState field
    return ms


def _make_slot():
    slot = types.SimpleNamespace()
    slot.shares = 50.0
    slot.price = 0.50
    slot.placed_at = 0.0
    slot.order_id = "oid-1"
    return slot


def _make_ol(fill_exists: bool) -> order_lifecycle.OrderLifecycle:
    """OrderLifecycle with __init__ bypassed; db/positions/dump mocked."""
    ol = order_lifecycle.OrderLifecycle.__new__(order_lifecycle.OrderLifecycle)
    ol.db = MagicMock()
    ol.db.fill_event_exists.return_value = fill_exists
    ol.db.log_fill.return_value = (not fill_exists)  # insert on new, collide on dup
    ol.db._get_conn.return_value.execute.return_value.fetchone.return_value = (1,)
    ol.positions = MagicMock()
    ol.positions.get_shares.return_value = 0.0      # → merge_qty 0 → dump path
    ol.positions.get_avg_price.return_value = 0.0
    ol._dump_mgr = MagicMock()
    return ol


class TestFX065FillIdempotency(unittest.TestCase):

    def test_new_fill_records_position(self):
        """fill_event_id not yet in fills → record_fill called exactly once."""
        ol = _make_ol(fill_exists=False)
        ol.handle_fill(_make_ms(), "yes", _make_slot(),
                       actual_shares=50.0, actual_price=0.50,
                       fill_event_id="sdk:oid-1:50")
        ol.positions.record_fill.assert_called_once()

    def test_duplicate_fill_skips_position(self):
        """fill_event_id ALREADY in fills → record_fill NOT called (no
        double-count). This is the core FX-065 guarantee."""
        ol = _make_ol(fill_exists=True)
        ol.handle_fill(_make_ms(), "yes", _make_slot(),
                       actual_shares=50.0, actual_price=0.50,
                       fill_event_id="sdk:oid-1:50")
        ol.positions.record_fill.assert_not_called()

    def test_empty_event_id_records_position(self):
        """Legacy caller with no event_id → append-only, record_fill called
        (fill_event_exists short-circuits False on empty id)."""
        ol = _make_ol(fill_exists=False)
        ol.handle_fill(_make_ms(), "yes", _make_slot(),
                       actual_shares=50.0, actual_price=0.50,
                       fill_event_id="")
        # With empty id, the guard's `fill_event_id and ...` short-circuits,
        # so fill_event_exists is never consulted and record_fill runs.
        ol.db.fill_event_exists.assert_not_called()
        ol.positions.record_fill.assert_called_once()

    def test_duplicate_still_attempts_dump(self):
        """A duplicate skips record_fill but the dump re-attempt is unchanged
        (balance-clamped downstream). Confirms FX-065 is surgical — it only
        removes the double-count, not the dump path."""
        ol = _make_ol(fill_exists=True)
        ol.handle_fill(_make_ms(), "yes", _make_slot(),
                       actual_shares=50.0, actual_price=0.50,
                       fill_event_id="sdk:oid-1:50")
        ol.positions.record_fill.assert_not_called()
        ol._dump_mgr.dump_position.assert_called_once()


if __name__ == "__main__":
    unittest.main()
