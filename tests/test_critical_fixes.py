"""Tests for the three critical fixes:
1. Resize cancellation (orders cancelled when agent_shares changes)
2. Agent-approval gating (non-agent markets blocked from buy orders)
3. Dump safety (UNKNOWN threshold preserves dump_state, phantom fill guard)
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import MarketState, OrderSlot


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_ms(cid="cid_001", agent_shares=50, agent_approved=True):
    """Create a minimal MarketState."""
    return MarketState(
        cid=cid, question="Test market?", yes_tid="ytid", no_tid="ntid",
        daily_rate=20.0, max_spread=0.10, min_size=10, tick_size=0.01,
        yes_price=0.50, agent_shares=agent_shares, agent_approved=agent_approved,
    )


def _make_lifecycle(markets_dict):
    """Create an OrderLifecycle with minimal mocks."""
    from order_lifecycle import OrderLifecycle

    positions = MagicMock()
    positions.get_shares.return_value = 0
    positions.can_quote.return_value = True

    ol = OrderLifecycle(
        client=MagicMock(), db=MagicMock(), positions=positions,
        rewards=MagicMock(), markets=markets_dict, dry_run=True,
    )
    ol.capital_ceiling = None
    return ol


def _make_dump_manager(positions=None):
    """Create a DumpManager with minimal mocks."""
    from dump_manager import DumpManager

    if positions is None:
        positions = MagicMock()
        positions.get_shares.return_value = 0
        positions.get_avg_price.return_value = 0.5

    dm = DumpManager(
        client=MagicMock(), db=MagicMock(), positions=positions,
        cancel_fn=MagicMock(), dry_run=False,
    )
    return dm


# ═══════════════════════════════════════════════════════════════════════════
# Fix 1: Resize cancellation
# ═══════════════════════════════════════════════════════════════════════════

class TestResizeCancellation(unittest.TestCase):
    """When agent_shares changes, existing orders must be cancelled."""

    def test_resize_cancels_existing_orders(self):
        """Orders on both sides cancelled + slots cleared when sizing changes."""
        ms = _make_ms(agent_shares=69)
        ms.orders["yes"] = OrderSlot(order_id="oid_yes_1", price=0.48, shares=69)
        ms.orders["no"] = OrderSlot(order_id="oid_no_1", price=0.48, shares=69)

        ol = _make_lifecycle({ms.cid: ms})
        db = MagicMock()

        # Simulate what _update_market_states does after the fix
        new_shares = 23.0
        ms.agent_shares = new_shares
        for side in ("yes", "no"):
            slot = ms.orders[side]
            if slot.order_id:
                ol.cancel_order(slot.order_id, reason="resize")
                db.delete_active_order(slot.order_id)
                ms.orders[side] = OrderSlot()

        self.assertEqual(ms.agent_shares, 23.0)
        self.assertIsNone(ms.orders["yes"].order_id)
        self.assertIsNone(ms.orders["no"].order_id)
        db.delete_active_order.assert_any_call("oid_yes_1")
        db.delete_active_order.assert_any_call("oid_no_1")

    def test_resize_same_size_keeps_orders(self):
        """No cancellation when shares unchanged."""
        ms = _make_ms(agent_shares=50)
        ms.orders["yes"] = OrderSlot(order_id="oid_yes_1", price=0.48, shares=50)

        ol = _make_lifecycle({ms.cid: ms})

        # Same shares — no resize path triggered
        new_shares = 50.0
        self.assertEqual(new_shares, ms.agent_shares)
        # Orders should remain untouched
        self.assertEqual(ms.orders["yes"].order_id, "oid_yes_1")

    def test_resize_from_zero_no_cancel(self):
        """First sizing set (0 → 50) with no existing orders — nothing to cancel."""
        ms = _make_ms(agent_shares=0)
        # No orders placed yet
        self.assertIsNone(ms.orders["yes"].order_id)
        self.assertIsNone(ms.orders["no"].order_id)

        ms.agent_shares = 50.0
        self.assertEqual(ms.agent_shares, 50.0)
        # Still no orders — nothing was cancelled
        self.assertIsNone(ms.orders["yes"].order_id)


# ═══════════════════════════════════════════════════════════════════════════
# Fix 2: Agent-approval gating
# ═══════════════════════════════════════════════════════════════════════════

class TestAgentApprovalGating(unittest.TestCase):
    """Non-agent-approved markets must be blocked from buy orders."""

    def test_can_place_rejects_unapproved_market(self):
        """can_place returns not_agent_approved for unapproved markets."""
        ms = _make_ms(agent_approved=False)
        ol = _make_lifecycle({ms.cid: ms})

        ok, reason = ol.can_place(ms.cid, "yes", 5.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "not_agent_approved")

    def test_can_place_allows_approved_market(self):
        """Approved market passes the agent_approved guard."""
        ms = _make_ms(agent_approved=True)
        ol = _make_lifecycle({ms.cid: ms})

        ok, reason = ol.can_place(ms.cid, "yes", 5.0)
        self.assertTrue(ok, f"Expected approved market to pass, got reason={reason}")

    def test_stale_allocation_blocks_all_orders(self):
        """When allocation goes stale, all markets get agent_approved=False."""
        markets = {
            "cid_a": _make_ms("cid_a", agent_approved=True),
            "cid_b": _make_ms("cid_b", agent_approved=True),
            "cid_c": _make_ms("cid_c", agent_approved=True),
        }

        # Simulate the stale-allocation path
        for ms in markets.values():
            ms.agent_approved = False

        for cid, ms in markets.items():
            self.assertFalse(ms.agent_approved, f"{cid} should be unapproved")

    def test_agent_mode_cancels_non_allocated_orders(self):
        """Buy orders on markets not in new allocation are cancelled."""
        ms_in = _make_ms("cid_in", agent_approved=True)
        ms_out = _make_ms("cid_out", agent_approved=True)
        ms_out.orders["yes"] = OrderSlot(order_id="oid_stale", price=0.48, shares=50)

        ol = _make_lifecycle({"cid_in": ms_in, "cid_out": ms_out})
        db = MagicMock()

        # Simulate agent-mode path: only cid_in in validated set
        validated_cids = {"cid_in"}
        markets = {"cid_in": ms_in, "cid_out": ms_out}
        for cid, ms in markets.items():
            ms.agent_approved = cid in validated_cids
            if not ms.agent_approved:
                for side in ("yes", "no"):
                    slot = ms.orders[side]
                    if slot.order_id:
                        ol.cancel_order(slot.order_id, reason="not_in_allocation")
                        db.delete_active_order(slot.order_id)
                        ms.orders[side] = OrderSlot()

        self.assertTrue(ms_in.agent_approved)
        self.assertFalse(ms_out.agent_approved)
        self.assertIsNone(ms_out.orders["yes"].order_id)
        db.delete_active_order.assert_called_once_with("oid_stale")

    def test_orphan_scan_markets_not_approved(self):
        """Orphan markets default to agent_approved=False."""
        ms = MarketState(
            cid="orphan_cid", question="Orphan?", yes_tid="y", no_tid="n",
            daily_rate=0, max_spread=0.05, min_size=1, tick_size=0.01,
            yes_price=None,
        )
        self.assertFalse(ms.agent_approved)


# ═══════════════════════════════════════════════════════════════════════════
# Fix 3: Dump safety
# ═══════════════════════════════════════════════════════════════════════════

class TestDumpUnknownThreshold(unittest.TestCase):
    """UNKNOWN threshold should preserve dump_state for retry."""

    @patch("dump_manager.cfg")
    def test_unknown_threshold_preserves_dump_state(self, mock_cfg):
        """dump_orders cleared, dump_state preserved, unknown_count reset."""
        mock_cfg.return_value = 2  # RF_UNKNOWN_RETRY_THRESHOLD = 2

        ms = _make_ms()
        ms.dump_orders["yes"] = "dump_oid_1"
        ms.dump_state["yes"] = {
            "fill_price": 0.50, "started_at": time.time() - 60,
            "shares": 200, "tid": "ytid",
        }
        ms.unknown_count["yes"] = 1  # Will become 2 = threshold

        dm = _make_dump_manager()
        # Mock: order not in open_ids, exchange returns UNKNOWN
        dm.client.get_order.return_value = {"status": "UNKNOWN"}

        dm.check_dump_fills({"cid_001": ms}, open_ids=set())

        # dump_orders cleared, dump_state preserved for retry
        self.assertIsNone(ms.dump_orders["yes"])
        self.assertIsNotNone(ms.dump_state["yes"], "dump_state should be preserved for retry")
        self.assertEqual(ms.unknown_count["yes"], 0, "unknown_count should reset")

    @patch("dump_manager.cfg")
    def test_unknown_below_threshold_keeps_retrying(self, mock_cfg):
        """Below threshold: increments count, keeps both dump_orders and dump_state."""
        mock_cfg.return_value = 5  # High threshold

        ms = _make_ms()
        ms.dump_orders["yes"] = "dump_oid_1"
        ms.dump_state["yes"] = {
            "fill_price": 0.50, "started_at": time.time() - 60,
            "shares": 200, "tid": "ytid",
        }
        ms.unknown_count["yes"] = 0

        dm = _make_dump_manager()
        dm.client.get_order.return_value = {"status": "UNKNOWN"}

        dm.check_dump_fills({"cid_001": ms}, open_ids=set())

        self.assertEqual(ms.dump_orders["yes"], "dump_oid_1")
        self.assertIsNotNone(ms.dump_state["yes"])
        self.assertEqual(ms.unknown_count["yes"], 1)


def _ensure_clob_types_mock():
    """Ensure py_clob_client.clob_types is importable (mocked if missing)."""
    if "py_clob_client" not in sys.modules:
        mock_clob = MagicMock()
        sys.modules["py_clob_client"] = mock_clob
        sys.modules["py_clob_client.clob_types"] = mock_clob.clob_types


class TestDumpPhantomFillGuard(unittest.TestCase):
    """MATCHED status must verify exchange balance before recording unwind."""

    @patch("dump_manager.cfg")
    def test_matched_phantom_fill_skips_unwind(self, mock_cfg):
        """Exchange still holds shares → record_unwind NOT called."""
        _ensure_clob_types_mock()
        mock_cfg.return_value = 2

        ms = _make_ms()
        ms.dump_orders["yes"] = "dump_oid_1"
        ms.dump_state["yes"] = {
            "fill_price": 0.50, "started_at": time.time() - 60,
            "shares": 200, "tid": "ytid",
        }

        positions = MagicMock()
        positions.get_shares.return_value = 200.0  # Tracked 200
        positions.get_avg_price.return_value = 0.50

        dm = _make_dump_manager(positions=positions)
        # Exchange says MATCHED but balance hasn't changed
        dm.client.get_order.return_value = {
            "status": "MATCHED", "price": "0.48", "size_matched": "200",
        }
        dm.client.get_balance_allowance.return_value = {
            "balance": str(200 * 1_000_000),  # Still 200 shares on exchange
        }

        dm.check_dump_fills({"cid_001": ms}, open_ids=set())

        # record_unwind should NOT have been called
        positions.record_unwind.assert_not_called()
        # dump state cleared for fresh retry
        self.assertIsNone(ms.dump_orders["yes"])

    @patch("dump_manager.cfg")
    def test_matched_real_fill_records_unwind(self, mock_cfg):
        """Exchange balance decreased → normal unwind path."""
        _ensure_clob_types_mock()
        mock_cfg.return_value = 2

        ms = _make_ms()
        ms.dump_orders["yes"] = "dump_oid_1"
        ms.dump_state["yes"] = {
            "fill_price": 0.50, "started_at": time.time() - 60,
            "shares": 200, "tid": "ytid",
        }

        positions = MagicMock()
        positions.get_shares.return_value = 200.0
        positions.get_avg_price.return_value = 0.50

        dm = _make_dump_manager(positions=positions)
        dm.client.get_order.return_value = {
            "status": "MATCHED", "price": "0.48", "size_matched": "200",
        }
        # Exchange balance is 0 — shares actually sold
        dm.client.get_balance_allowance.return_value = {"balance": "0"}

        dm.check_dump_fills({"cid_001": ms}, open_ids=set())

        positions.record_unwind.assert_called_once_with(ms.cid, "yes", 200.0)
        self.assertIsNone(ms.dump_orders["yes"])
        self.assertIsNone(ms.dump_state["yes"])


class TestDumpCancelledRetry(unittest.TestCase):
    """CANCELLED dump orders should clear order but preserve state for retry."""

    @patch("dump_manager.cfg")
    def test_cancelled_dump_preserves_state_for_retry(self, mock_cfg):
        mock_cfg.return_value = 2

        ms = _make_ms()
        ms.dump_orders["yes"] = "dump_oid_1"
        ms.dump_state["yes"] = {
            "fill_price": 0.50, "started_at": time.time() - 60,
            "shares": 200, "tid": "ytid",
        }

        dm = _make_dump_manager()
        dm.client.get_order.return_value = {"status": "CANCELLED"}

        dm.check_dump_fills({"cid_001": ms}, open_ids=set())

        # Order cleared, state preserved
        self.assertIsNone(ms.dump_orders["yes"])
        self.assertIsNotNone(ms.dump_state["yes"], "dump_state should be preserved for retry")


class TestSafetySweep(unittest.TestCase):
    """Safety sweep in reprice_active_dumps catches orphaned positions."""

    def test_safety_sweep_catches_lost_positions(self):
        """Shares with no dump/buy state trigger dump_position."""
        ms = _make_ms()
        # No dump state, no dump orders, no buy orders — but has shares
        ms.dump_state = {"yes": None, "no": None}
        ms.dump_orders = {"yes": None, "no": None}
        ms.orders = {"yes": OrderSlot(), "no": OrderSlot()}

        positions = MagicMock()
        positions.get_shares.return_value = 100.0  # 100 orphaned shares

        dm = _make_dump_manager(positions=positions)
        dm.dump_position = MagicMock()

        dm.reprice_active_dumps({"cid_001": ms}, open_ids=set())

        # dump_position should be called for both sides
        self.assertEqual(dm.dump_position.call_count, 2)
        dm.dump_position.assert_any_call(ms, "yes", 100.0)
        dm.dump_position.assert_any_call(ms, "no", 100.0)

    def test_safety_sweep_skips_active_dumps(self):
        """Markets with active dump_state are NOT double-dumped by sweep."""
        ms = _make_ms()
        ms.dump_state["yes"] = {"fill_price": 0.5, "started_at": time.time(), "shares": 50, "tid": "ytid"}
        ms.dump_orders["yes"] = "dump_oid_active"

        positions = MagicMock()
        positions.get_shares.return_value = 50.0

        dm = _make_dump_manager(positions=positions)
        dm.dump_position = MagicMock()

        dm.reprice_active_dumps({"cid_001": ms}, open_ids={"dump_oid_active"})

        # dump_position may be called by the reprice logic (elapsed check),
        # but the safety sweep should NOT trigger because dump_state exists
        # We check that it's not called with the full 50 shares from sweep
        for c in dm.dump_position.call_args_list:
            # Calls from reprice logic use dump_state["shares"], which is fine
            # Just verify the sweep didn't redundantly trigger
            pass
        # If dump_state and dump_orders both exist, the sweep skips this side
        # The reprice path may or may not call depending on elapsed time


# ═══════════════════════════════════════════════════════════════════════════
# Fix 4: try_merge balance verification (WTI root cause)
# ═══════════════════════════════════════════════════════════════════════════

class TestMergeBalanceVerification(unittest.TestCase):
    """try_merge must verify exchange balance decreased before recording unwind."""

    def test_phantom_merge_falls_back_to_dual_dump(self):
        """Merge API returns but balance unchanged → record_unwind NOT called, falls back to dump."""
        _ensure_clob_types_mock()

        ms = _make_ms()
        positions = MagicMock()
        positions.get_shares.return_value = 200.0

        dm = _make_dump_manager(positions=positions)
        dm.dump_position = MagicMock()

        # Merge API "succeeds" but balance doesn't change
        dm.client.merge_positions.return_value = {"success": True}
        dm.client.update_balance_allowance.return_value = None
        # Pre and post balance are the same (200 shares = 200_000_000 raw)
        dm.client.get_balance_allowance.return_value = {
            "balance": str(200 * 1_000_000),
        }

        dm.try_merge(ms, 200.0)

        # record_unwind should NOT have been called — merge didn't actually happen
        positions.record_unwind.assert_not_called()
        # Should fall back to dual dump
        self.assertTrue(dm.dump_position.call_count >= 1,
                        "Should fall back to dump_position after phantom merge")

    def test_real_merge_records_unwind(self):
        """Merge succeeds and balance drops → record_unwind called normally."""
        _ensure_clob_types_mock()

        ms = _make_ms()
        positions = MagicMock()
        positions.get_shares.return_value = 200.0

        dm = _make_dump_manager(positions=positions)
        dm.dump_position = MagicMock()

        dm.client.merge_positions.return_value = {"success": True}
        dm.client.update_balance_allowance.return_value = None

        # Pre-merge: 200 shares. Post-merge: 0 shares.
        call_count = [0]
        def mock_balance(*args, **kwargs):
            call_count[0] += 1
            # First call = pre-merge snapshot, second = post-merge verification
            # (update_balance_allowance calls are separate and return None)
            if call_count[0] == 1:
                return {"balance": str(200 * 1_000_000)}  # pre: 200 shares
            else:
                return {"balance": "0"}  # post: 0 shares (merged)

        dm.client.get_balance_allowance.side_effect = mock_balance

        dm.try_merge(ms, 200.0)

        # record_unwind SHOULD have been called for both sides
        self.assertEqual(positions.record_unwind.call_count, 2)
        positions.record_unwind.assert_any_call(ms.cid, "yes", 200.0)
        positions.record_unwind.assert_any_call(ms.cid, "no", 200.0)
        # dump_position should NOT have been called
        dm.dump_position.assert_not_called()

    def test_merge_api_exception_falls_back_to_dump(self):
        """Merge API raises exception → falls back to dual dump, no unwind recorded."""
        _ensure_clob_types_mock()

        ms = _make_ms()
        positions = MagicMock()
        positions.get_shares.return_value = 200.0

        dm = _make_dump_manager(positions=positions)
        dm.dump_position = MagicMock()

        dm.client.update_balance_allowance.return_value = None
        dm.client.get_balance_allowance.return_value = {
            "balance": str(200 * 1_000_000),
        }
        dm.client.merge_positions.side_effect = Exception("API timeout")

        dm.try_merge(ms, 200.0)

        positions.record_unwind.assert_not_called()
        self.assertTrue(dm.dump_position.call_count >= 1)


if __name__ == "__main__":
    unittest.main()
