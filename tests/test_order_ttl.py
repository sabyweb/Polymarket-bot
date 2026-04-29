"""Tests for order TTL / staleness check.

Covers:
- Orders in open_ids are force-checked after RF_ORDER_STALE_CHECK_SECS
- Partial fills on stale orders are detected, recorded, and order cancelled
- Clean orders (no fills) are left alone with updated last_stale_check
- Orders younger than TTL are not force-checked
- API errors during stale check don't crash the cycle
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock py_clob_client_v2 before importing order_lifecycle
if "py_clob_client_v2" not in sys.modules:
    mock_clob = MagicMock()
    sys.modules["py_clob_client_v2"] = mock_clob
    sys.modules["py_clob_client_v2.clob_types"] = mock_clob.clob_types
    sys.modules["py_clob_client_v2.order_builder"] = mock_clob.order_builder
    sys.modules["py_clob_client_v2.order_builder.constants"] = mock_clob.order_builder.constants
    mock_clob.order_builder.constants.BUY = "BUY"
    mock_clob.order_builder.constants.SELL = "SELL"

from models import MarketState, OrderSlot
from order_lifecycle import OrderLifecycle


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_ms(cid="cid_001", agent_approved=True):
    """Create a minimal MarketState."""
    return MarketState(
        cid=cid, question="Test market?", yes_tid="ytid", no_tid="ntid",
        daily_rate=20.0, max_spread=0.10, min_size=10, tick_size=0.01,
        yes_price=0.50, agent_shares=50, agent_approved=agent_approved,
    )


def _make_lifecycle(markets_dict):
    """Create an OrderLifecycle with minimal mocks."""
    positions = MagicMock()
    positions.get_shares.return_value = 0
    positions.can_quote.return_value = True
    positions.get_avg_price.return_value = 0.5

    ol = OrderLifecycle(
        client=MagicMock(), db=MagicMock(), positions=positions,
        rewards=MagicMock(), markets=markets_dict, dry_run=False,
    )
    ol.capital_ceiling = None
    # Provide a dump manager mock for handle_fill
    ol._dump_mgr = MagicMock()
    return ol


# ═══════════════════════════════════════════════════════════════════════
# Stale order checks
# ═══════════════════════════════════════════════════════════════════════

class TestStaleOrderCheck(unittest.TestCase):
    """_check_stale_order detects partial fills on orders still in open_ids."""

    def test_young_order_not_checked(self):
        """Order placed 60s ago (< 300s TTL) → no API call made."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="oid_fresh", price=0.48, shares=50,
            placed_at=time.time() - 60,  # 1 minute old
        )
        markets = {ms.cid: ms}
        ol = _make_lifecycle(markets)

        ol._check_stale_order(ms, "yes", ms.orders["yes"])

        # get_order should NOT have been called
        ol.client.get_order.assert_not_called()

    def test_stale_order_no_fills_updates_check_time(self):
        """Order alive 6 min with no fills → last_stale_check updated, order kept."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="oid_stale", price=0.48, shares=50,
            placed_at=time.time() - 360,  # 6 min old
        )
        markets = {ms.cid: ms}
        ol = _make_lifecycle(markets)

        # Exchange says: no fills
        ol.client.get_order.return_value = {
            "status": "LIVE",
            "size_matched": 0,
            "price": "0.48",
        }

        old_check = ms.orders["yes"].last_stale_check
        ol._check_stale_order(ms, "yes", ms.orders["yes"])

        # API was called
        ol.client.get_order.assert_called_once_with("oid_stale")
        # Order still alive
        self.assertEqual(ms.orders["yes"].order_id, "oid_stale")
        # last_stale_check updated
        self.assertGreater(ms.orders["yes"].last_stale_check, old_check)

    def test_stale_order_partial_fill_detected(self):
        """Order alive 6 min with 30/50 shares matched → fill recorded, order cancelled."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="oid_partial", price=0.48, shares=50,
            placed_at=time.time() - 360,  # 6 min old
        )
        markets = {ms.cid: ms}
        ol = _make_lifecycle(markets)

        # Exchange says: 30 of 50 shares matched
        ol.client.get_order.return_value = {
            "status": "LIVE",
            "size_matched": 30,
            "price": "0.48",
        }

        ol._check_stale_order(ms, "yes", ms.orders["yes"])

        # Order should be cancelled
        ol.client.cancel_order.assert_called_once()
        ol.db.delete_active_order.assert_called_with("oid_partial")

        # Slot should be cleared
        self.assertIsNone(ms.orders["yes"].order_id)

        # handle_fill should have been called (via dump manager)
        # handle_fill records the fill and triggers dump_position
        ol._dump_mgr.dump_position.assert_called()

    def test_stale_order_full_fill_detected(self):
        """Order alive 6 min with 50/50 shares matched → full fill recorded."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="oid_full", price=0.48, shares=50,
            placed_at=time.time() - 360,
        )
        markets = {ms.cid: ms}
        ol = _make_lifecycle(markets)

        # Exchange says: all 50 shares matched
        ol.client.get_order.return_value = {
            "status": "LIVE",
            "size_matched": 50,
            "price": "0.48",
        }

        ol._check_stale_order(ms, "yes", ms.orders["yes"])

        # Order cancelled and slot cleared
        ol.client.cancel_order.assert_called_once()
        self.assertIsNone(ms.orders["yes"].order_id)

    def test_stale_check_api_error_backoff(self):
        """API error during stale check → last_stale_check updated (backoff), order kept."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="oid_err", price=0.48, shares=50,
            placed_at=time.time() - 360,
        )
        markets = {ms.cid: ms}
        ol = _make_lifecycle(markets)

        # API fails
        ol.client.get_order.side_effect = Exception("API timeout")

        old_check = ms.orders["yes"].last_stale_check
        ol._check_stale_order(ms, "yes", ms.orders["yes"])

        # Order should still be alive
        self.assertEqual(ms.orders["yes"].order_id, "oid_err")
        # last_stale_check updated so we don't hammer the API
        self.assertGreater(ms.orders["yes"].last_stale_check, old_check)

    def test_recently_checked_order_not_rechecked(self):
        """Order checked 60s ago (< 300s interval) → no API call even though placed_at is old."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="oid_recent", price=0.48, shares=50,
            placed_at=time.time() - 600,  # 10 min old
            last_stale_check=time.time() - 60,  # checked 1 min ago
        )
        markets = {ms.cid: ms}
        ol = _make_lifecycle(markets)

        ol._check_stale_order(ms, "yes", ms.orders["yes"])

        # Should NOT make an API call — last check was recent
        ol.client.get_order.assert_not_called()

    def test_second_check_after_interval(self):
        """Order checked 6 min ago → force-check again."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="oid_recheck", price=0.48, shares=50,
            placed_at=time.time() - 900,  # 15 min old
            last_stale_check=time.time() - 360,  # last checked 6 min ago
        )
        markets = {ms.cid: ms}
        ol = _make_lifecycle(markets)

        ol.client.get_order.return_value = {
            "status": "LIVE",
            "size_matched": 0,
            "price": "0.48",
        }

        ol._check_stale_order(ms, "yes", ms.orders["yes"])

        # API should be called (interval elapsed since last check)
        ol.client.get_order.assert_called_once_with("oid_recheck")


class TestDetectFillsIntegration(unittest.TestCase):
    """detect_fills() calls _check_stale_order for orders in open_ids."""

    def test_order_in_open_ids_triggers_stale_check(self):
        """Order present in open_ids → _check_stale_order called."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="oid_live", price=0.48, shares=50,
            placed_at=time.time() - 360,  # 6 min old → stale
        )
        markets = {ms.cid: ms}
        ol = _make_lifecycle(markets)

        # Exchange says: order still live, partially filled
        ol.client.get_order.return_value = {
            "status": "LIVE",
            "size_matched": 20,
            "price": "0.48",
        }

        # open_ids contains our order
        open_ids = {"oid_live"}
        ol.detect_fills(open_ids)

        # get_order was called (stale check triggered)
        ol.client.get_order.assert_called_once_with("oid_live")
        # Order was cancelled (partial fill detected)
        ol.client.cancel_order.assert_called_once()
        self.assertIsNone(ms.orders["yes"].order_id)

    def test_order_not_in_open_ids_uses_normal_path(self):
        """Order NOT in open_ids → normal fill detection path (not stale check)."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="oid_gone", price=0.48, shares=50,
            placed_at=time.time() - 60,  # recent — doesn't matter, normal path
        )
        markets = {ms.cid: ms}
        ol = _make_lifecycle(markets)

        # Exchange says: fully matched
        ol.client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": 50,
            "price": "0.48",
        }

        # open_ids does NOT contain our order
        open_ids = set()
        ol.detect_fills(open_ids)

        # Normal fill detection ran
        ol.client.get_order.assert_called_once_with("oid_gone")
        self.assertIsNone(ms.orders["yes"].order_id)


if __name__ == "__main__":
    unittest.main()
