"""Tests for order reconciliation (_reconcile_orders).

Covers:
- Ghost buy orders (tracked but not on exchange) are cleared
- Ghost buy orders with silent fills are detected and recorded
- Ghost dump orders are cleared but dump_state preserved for retry
- Orders that ARE on exchange are left alone
- API errors during reconciliation don't crash
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock py_clob_client_v2 before importing
if "py_clob_client_v2" not in sys.modules:
    mock_clob = MagicMock()
    sys.modules["py_clob_client_v2"] = mock_clob
    sys.modules["py_clob_client_v2.clob_types"] = mock_clob.clob_types
    sys.modules["py_clob_client_v2.client"] = mock_clob.client
    sys.modules["py_clob_client_v2.order_builder"] = mock_clob.order_builder
    sys.modules["py_clob_client_v2.order_builder.constants"] = mock_clob.order_builder.constants
    mock_clob.order_builder.constants.BUY = "BUY"
    mock_clob.order_builder.constants.SELL = "SELL"

from models import MarketState, OrderSlot


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_ms(cid="cid_001", agent_approved=True):
    """Create a minimal MarketState."""
    return MarketState(
        cid=cid, question="Test market?", yes_tid="ytid", no_tid="ntid",
        daily_rate=20.0, max_spread=0.10, min_size=10, tick_size=0.01,
        yes_price=0.50, agent_shares=50, agent_approved=agent_approved,
    )


def _make_farmer_stub(markets):
    """Create a minimal object with attributes needed by _reconcile_orders."""
    from reward_farmer import RewardFarmer
    from order_lifecycle import OrderLifecycle

    class Stub:
        pass

    stub = Stub()
    stub.markets = markets
    stub.dry_run = False
    stub.client = MagicMock()
    stub.db = MagicMock()
    stub.positions = MagicMock()
    stub.positions.get_shares.return_value = 0
    stub.positions.can_quote.return_value = True

    # Real OrderLifecycle with mocked dependencies
    stub.order_lifecycle = OrderLifecycle(
        client=stub.client, db=stub.db, positions=stub.positions,
        rewards=MagicMock(), markets=markets, dry_run=False,
    )
    stub.order_lifecycle._dump_mgr = MagicMock()
    return stub


# ═══════════════════════════════════════════════════════════════════════
# Ghost buy order tests
# ═══════════════════════════════════════════════════════════════════════

class TestReconcileGhostBuyOrders(unittest.TestCase):

    def test_ghost_buy_order_cleared(self):
        """Buy order tracked but NOT on exchange → cleared, slot reset."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="ghost_buy_yes", price=0.48, shares=50,
            placed_at=time.time() - 600,
        )
        markets = {ms.cid: ms}
        stub = _make_farmer_stub(markets)

        # Exchange has no orders at all
        stub.client.get_orders.return_value = []
        # get_order says: CANCELLED, no fills
        stub.client.get_order.return_value = {
            "status": "CANCELLED", "size_matched": 0, "price": "0.48",
        }

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_orders(stub)

        # Slot should be cleared
        self.assertIsNone(ms.orders["yes"].order_id)
        stub.db.delete_active_order.assert_called_with("ghost_buy_yes")

    def test_ghost_buy_order_with_fill_recorded(self):
        """Ghost buy order that was silently filled → fill recorded before clearing."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="ghost_filled", price=0.48, shares=50,
            placed_at=time.time() - 600,
        )
        markets = {ms.cid: ms}
        stub = _make_farmer_stub(markets)

        stub.client.get_orders.return_value = []
        # get_order says: MATCHED with 50 shares
        stub.client.get_order.return_value = {
            "status": "MATCHED", "size_matched": 50, "price": "0.48",
        }

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_orders(stub)

        # Slot cleared
        self.assertIsNone(ms.orders["yes"].order_id)
        # Fill should have been handled (dump_mgr called)
        stub.order_lifecycle._dump_mgr.dump_position.assert_called()

    def test_valid_buy_order_untouched(self):
        """Buy order tracked AND on exchange → left alone."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="valid_buy", price=0.48, shares=50,
            placed_at=time.time() - 60,
        )
        markets = {ms.cid: ms}
        stub = _make_farmer_stub(markets)

        # Exchange has our order
        stub.client.get_orders.return_value = [{"id": "valid_buy"}]

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_orders(stub)

        # Slot should be untouched
        self.assertEqual(ms.orders["yes"].order_id, "valid_buy")
        stub.db.delete_active_order.assert_not_called()

    def test_ghost_buy_both_sides_cleared(self):
        """Ghost orders on BOTH sides → both cleared independently."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="ghost_yes", price=0.48, shares=50,
            placed_at=time.time() - 600,
        )
        ms.orders["no"] = OrderSlot(
            order_id="ghost_no", price=0.52, shares=50,
            placed_at=time.time() - 600,
        )
        markets = {ms.cid: ms}
        stub = _make_farmer_stub(markets)

        stub.client.get_orders.return_value = []
        stub.client.get_order.return_value = {
            "status": "CANCELLED", "size_matched": 0, "price": "0.50",
        }

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_orders(stub)

        self.assertIsNone(ms.orders["yes"].order_id)
        self.assertIsNone(ms.orders["no"].order_id)


# ═══════════════════════════════════════════════════════════════════════
# Ghost dump order tests
# ═══════════════════════════════════════════════════════════════════════

class TestReconcileGhostDumpOrders(unittest.TestCase):

    def test_ghost_dump_order_cleared_state_preserved(self):
        """Dump order not on exchange → dump_orders cleared, dump_state kept for retry."""
        ms = _make_ms()
        ms.dump_orders["yes"] = "ghost_dump_oid"
        ms.dump_state["yes"] = {
            "fill_price": 0.48, "started_at": time.time() - 120,
            "shares": 50, "tid": "ytid",
        }
        markets = {ms.cid: ms}
        stub = _make_farmer_stub(markets)

        stub.client.get_orders.return_value = []

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_orders(stub)

        # dump_orders cleared
        self.assertIsNone(ms.dump_orders["yes"])
        # dump_state PRESERVED for retry
        self.assertIsNotNone(ms.dump_state["yes"])
        stub.db.delete_active_order.assert_called_with("ghost_dump_oid")

    def test_valid_dump_order_untouched(self):
        """Dump order tracked AND on exchange → left alone."""
        ms = _make_ms()
        ms.dump_orders["yes"] = "valid_dump"
        ms.dump_state["yes"] = {
            "fill_price": 0.48, "started_at": time.time() - 60,
            "shares": 50, "tid": "ytid",
        }
        markets = {ms.cid: ms}
        stub = _make_farmer_stub(markets)

        stub.client.get_orders.return_value = [{"id": "valid_dump"}]

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_orders(stub)

        # Everything untouched
        self.assertEqual(ms.dump_orders["yes"], "valid_dump")
        self.assertIsNotNone(ms.dump_state["yes"])
        stub.db.delete_active_order.assert_not_called()

    def test_mixed_ghost_buy_and_dump(self):
        """Ghost buy on YES + ghost dump on NO → both cleared correctly."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="ghost_buy", price=0.48, shares=50,
            placed_at=time.time() - 600,
        )
        ms.dump_orders["no"] = "ghost_dump"
        ms.dump_state["no"] = {
            "fill_price": 0.52, "started_at": time.time() - 120,
            "shares": 50, "tid": "ntid",
        }
        markets = {ms.cid: ms}
        stub = _make_farmer_stub(markets)

        stub.client.get_orders.return_value = []
        stub.client.get_order.return_value = {
            "status": "CANCELLED", "size_matched": 0, "price": "0.48",
        }

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_orders(stub)

        # Buy order cleared
        self.assertIsNone(ms.orders["yes"].order_id)
        # Dump order cleared, state preserved
        self.assertIsNone(ms.dump_orders["no"])
        self.assertIsNotNone(ms.dump_state["no"])


# ═══════════════════════════════════════════════════════════════════════
# Error handling
# ═══════════════════════════════════════════════════════════════════════

class TestReconcileErrorHandling(unittest.TestCase):

    def test_get_orders_failure_aborts_gracefully(self):
        """get_orders() failure → entire reconciliation skipped, no crash."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="should_survive", price=0.48, shares=50,
            placed_at=time.time() - 600,
        )
        markets = {ms.cid: ms}
        stub = _make_farmer_stub(markets)

        stub.client.get_orders.side_effect = Exception("API down")

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_orders(stub)

        # Order should survive — reconciliation aborted
        self.assertEqual(ms.orders["yes"].order_id, "should_survive")

    def test_get_order_failure_clears_ghost_without_fill(self):
        """get_order() fails on ghost order → still cleared (conservative)."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="ghost_api_fail", price=0.48, shares=50,
            placed_at=time.time() - 600,
        )
        markets = {ms.cid: ms}
        stub = _make_farmer_stub(markets)

        stub.client.get_orders.return_value = []
        stub.client.get_order.side_effect = Exception("API timeout")

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_orders(stub)

        # Order still cleared — it's not on exchange regardless of API error
        self.assertIsNone(ms.orders["yes"].order_id)

    def test_no_markets_no_crash(self):
        """Empty markets dict → reconciliation completes cleanly."""
        markets = {}
        stub = _make_farmer_stub(markets)
        stub.client.get_orders.return_value = []

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_orders(stub)
        # No crash


class TestReconcileDryRun(unittest.TestCase):

    def test_dry_run_skips_reconciliation(self):
        """dry_run=True → no API calls, no changes."""
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(
            order_id="should_survive", price=0.48, shares=50,
            placed_at=time.time() - 600,
        )
        markets = {ms.cid: ms}
        stub = _make_farmer_stub(markets)
        stub.dry_run = True

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_orders(stub)

        # No API calls
        stub.client.get_orders.assert_not_called()
        # Order untouched
        self.assertEqual(ms.orders["yes"].order_id, "should_survive")


if __name__ == "__main__":
    unittest.main()
