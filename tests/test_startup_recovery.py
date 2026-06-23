"""Tests for startup fill recovery (_reconcile_on_startup).

Covers:
- Partial buy fills from offline period are recorded in PositionStore
- Dump fills from offline period are recorded as unwinds
- Orders with no fills are cancelled cleanly
- Orders not in DB (unknown) are cancelled without crashing
- API errors during recovery don't block cancellation
- Dry run skips everything
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch, call

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


# ── Helpers ──────────────────────────────────────────────────────────────────

class _MockPositionStore:
    """Lightweight position store for testing. Tracks shares per (cid, side)."""

    def __init__(self):
        self._shares = {}  # {(cid, side): float}

    def register_market(self, cid, question=""):
        pass  # No-op for testing

    def record_fill(self, cid, side, shares, price, question=""):
        key = (cid, side)
        self._shares[key] = self._shares.get(key, 0) + shares

    def record_unwind(self, cid, side, shares, price=0.0):
        key = (cid, side)
        self._shares[key] = max(0, self._shares.get(key, 0) - shares)

    def get_shares(self, cid, side):
        return self._shares.get((cid, side), 0)


def _make_farmer_stub(dry_run=False):
    """Create a minimal object with attributes needed by _reconcile_on_startup."""

    class Stub:
        pass

    stub = Stub()
    stub.dry_run = dry_run
    stub.client = MagicMock()
    stub.db = MagicMock()
    stub.positions = _MockPositionStore()
    return stub


def _make_db_order(order_id, condition_id="cid_001", side="yes",
                   order_type="buy", price=0.48, shares=50):
    """Create a dict matching the active_orders DB schema."""
    return {
        "order_id": order_id,
        "condition_id": condition_id,
        "side": side,
        "order_type": order_type,
        "price": price,
        "shares": shares,
        "placed_at": time.time() - 600,
    }


# ═══════════════════════════════════════════════════════════════════════
# Buy fill recovery
# ═══════════════════════════════════════════════════════════════════════

class TestStartupBuyFillRecovery(unittest.TestCase):

    def test_partial_buy_fill_recovered(self):
        """Buy order partially filled while offline → shares recorded in PositionStore."""
        stub = _make_farmer_stub()

        # DB has one tracked buy order
        stub.db.load_active_orders.return_value = [
            _make_db_order("oid_buy", side="yes", price=0.48, shares=50),
        ]

        # Exchange still has the order (partially filled)
        stub.client.get_open_orders.return_value = [{"id": "oid_buy"}]

        # get_order reveals 30/50 shares matched
        stub.client.get_order.return_value = {
            "status": "LIVE", "size_matched": 30, "price": "0.48",
        }

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_on_startup(stub)

        # PositionStore should have 30 shares
        shares = stub.positions.get_shares("cid_001", "yes")
        self.assertAlmostEqual(shares, 30.0, delta=0.5)

        # Order should still be cancelled
        stub.client.cancel_order.assert_called_once()

        # Fill should be logged
        stub.db.log_fill.assert_called_once()

    def test_full_buy_fill_recovered(self):
        """Buy order fully filled while offline (gone from exchange) → recorded."""
        stub = _make_farmer_stub()

        stub.db.load_active_orders.return_value = [
            _make_db_order("oid_filled", side="no", price=0.52, shares=100),
        ]

        # Exchange does NOT have the order (fully filled, removed)
        stub.client.get_open_orders.return_value = []

        # get_order reveals fully matched
        stub.client.get_order.return_value = {
            "status": "MATCHED", "size_matched": 100, "price": "0.52",
        }

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_on_startup(stub)

        # PositionStore should have 100 NO shares
        shares = stub.positions.get_shares("cid_001", "no")
        self.assertAlmostEqual(shares, 100.0, delta=0.5)

    def test_buy_order_no_fills_no_recording(self):
        """Buy order with zero fills → cancelled, nothing recorded."""
        stub = _make_farmer_stub()

        stub.db.load_active_orders.return_value = [
            _make_db_order("oid_clean", side="yes", price=0.48, shares=50),
        ]
        stub.client.get_open_orders.return_value = [{"id": "oid_clean"}]
        stub.client.get_order.return_value = {
            "status": "LIVE", "size_matched": 0, "price": "0.48",
        }

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_on_startup(stub)

        # No shares recorded
        shares = stub.positions.get_shares("cid_001", "yes")
        self.assertAlmostEqual(shares, 0.0, delta=0.5)

        # Still cancelled
        stub.client.cancel_order.assert_called_once()

        # No fill logged
        stub.db.log_fill.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# Dump/unwind recovery
# ═══════════════════════════════════════════════════════════════════════

class TestStartupDumpRecovery(unittest.TestCase):

    def test_dump_fill_recovered_as_unwind(self):
        """Dump sell order filled while offline → recorded as unwind."""
        stub = _make_farmer_stub()

        # Pre-seed position so unwind has something to reduce
        stub.positions.register_market("cid_001", "test")
        stub.positions.record_fill("cid_001", "yes", 50, 0.48)

        stub.db.load_active_orders.return_value = [
            _make_db_order("oid_dump", side="yes", order_type="dump_sell",
                           price=0.47, shares=50),
        ]
        stub.client.get_open_orders.return_value = []
        stub.client.get_order.return_value = {
            "status": "MATCHED", "size_matched": 50, "price": "0.47",
        }

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_on_startup(stub)

        # Position should be reduced (50 - 50 = 0)
        shares = stub.positions.get_shares("cid_001", "yes")
        self.assertAlmostEqual(shares, 0.0, delta=0.5)

        # Unwind logged
        stub.db.log_unwind.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# Mixed scenarios
# ═══════════════════════════════════════════════════════════════════════

class TestStartupMixedScenarios(unittest.TestCase):

    def test_multiple_orders_recovered(self):
        """Multiple orders: one filled buy, one filled dump, one clean → all handled."""
        stub = _make_farmer_stub()

        # Pre-seed position for dump unwind
        stub.positions.register_market("cid_002", "dump market")
        stub.positions.record_fill("cid_002", "no", 80, 0.52)

        stub.db.load_active_orders.return_value = [
            _make_db_order("oid_buy", condition_id="cid_001", side="yes", price=0.48, shares=50),
            _make_db_order("oid_dump", condition_id="cid_002", side="no",
                           order_type="dump_sell", price=0.48, shares=80),
            _make_db_order("oid_clean", condition_id="cid_003", side="yes", price=0.50, shares=30),
        ]
        stub.client.get_open_orders.return_value = [{"id": "oid_clean"}]

        def _get_order(oid):
            if oid == "oid_buy":
                return {"status": "MATCHED", "size_matched": 50, "price": "0.48"}
            elif oid == "oid_dump":
                return {"status": "MATCHED", "size_matched": 80, "price": "0.48"}
            else:
                return {"status": "LIVE", "size_matched": 0, "price": "0.50"}
        stub.client.get_order.side_effect = _get_order

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_on_startup(stub)

        # Buy fill recorded
        self.assertAlmostEqual(
            stub.positions.get_shares("cid_001", "yes"), 50.0, delta=0.5
        )
        # Dump unwind recorded (80 - 80 = 0)
        self.assertAlmostEqual(
            stub.positions.get_shares("cid_002", "no"), 0.0, delta=0.5
        )
        # Clean order: nothing recorded
        self.assertAlmostEqual(
            stub.positions.get_shares("cid_003", "yes"), 0.0, delta=0.5
        )

    def test_unknown_exchange_order_cancelled(self):
        """Order on exchange but NOT in our DB → cancelled without crash."""
        stub = _make_farmer_stub()

        stub.db.load_active_orders.return_value = []
        stub.client.get_open_orders.return_value = [
            {"id": "unknown_oid_1"},
            {"id": "unknown_oid_2"},
        ]

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_on_startup(stub)

        # Both cancelled
        self.assertEqual(stub.client.cancel_order.call_count, 2)


# ═══════════════════════════════════════════════════════════════════════
# Error handling
# ═══════════════════════════════════════════════════════════════════════

class TestStartupErrorHandling(unittest.TestCase):

    def test_get_order_failure_continues(self):
        """API error on get_order → skips that order, continues with rest."""
        stub = _make_farmer_stub()

        stub.db.load_active_orders.return_value = [
            _make_db_order("oid_err", side="yes", price=0.48, shares=50),
            _make_db_order("oid_ok", condition_id="cid_002", side="no", price=0.52, shares=30),
        ]
        stub.client.get_open_orders.return_value = [{"id": "oid_err"}, {"id": "oid_ok"}]

        def _get_order(oid):
            if oid == "oid_err":
                raise Exception("API timeout")
            return {"status": "MATCHED", "size_matched": 30, "price": "0.52"}
        stub.client.get_order.side_effect = _get_order

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_on_startup(stub)

        # Second order's fill should still be recovered
        self.assertAlmostEqual(
            stub.positions.get_shares("cid_002", "no"), 30.0, delta=0.5
        )

        # Both orders still cancelled
        self.assertEqual(stub.client.cancel_order.call_count, 2)

    def test_get_orders_failure_still_purges_db(self):
        """get_orders() fails → can't cancel, but DB still purged."""
        stub = _make_farmer_stub()

        stub.db.load_active_orders.return_value = []
        stub.client.get_open_orders.side_effect = Exception("Network error")

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_on_startup(stub)

        # DB should still be purged
        stub.db.purge_all_active_orders.assert_called_once()


class TestStartupDryRun(unittest.TestCase):

    def test_dry_run_skips_everything(self):
        """dry_run=True → no API calls at all."""
        stub = _make_farmer_stub(dry_run=True)

        from reward_farmer import RewardFarmer
        RewardFarmer._reconcile_on_startup(stub)

        stub.client.get_open_orders.assert_not_called()
        stub.client.get_order.assert_not_called()
        stub.client.cancel_order.assert_not_called()
        stub.db.purge_all_active_orders.assert_not_called()


class TestPersistentKillBoot(unittest.TestCase):
    """B-3 regression: a restart with an active persistent kill sentinel must
    still run startup reconciliation (recover fills, cancel outstanding orders,
    purge stale DB rows) and remain halted afterwards."""

    def test_persistent_kill_boot_still_reconciles(self):
        from reward_farmer import RewardFarmer, MODE_LIVE
        from config import BotConfig

        bc = BotConfig.instance()
        saved = dict(bc._overrides)
        bc._overrides["RF_KILL_PERSISTENT_ENABLED"] = True
        try:
            stub = _make_farmer_stub()
            stub.mode = MODE_LIVE
            stub._kill_switch_active = False
            stub._kill_switch_reason = ""
            stub._kill_switch_triggered_at = 0.0
            stub.db.get_kill_switch.return_value = {
                "active": True,
                "reason": "fill_rate_kill",
                "triggered_at": 12345.0,
            }

            RewardFarmer._load_persistent_kill_switch(stub)
            self.assertTrue(stub._kill_switch_active)
            self.assertEqual(stub._kill_switch_reason, "fill_rate_kill")

            # Reconciliation should still run and cancel the tracked order.
            stub.db.load_active_orders.return_value = [
                _make_db_order("oid_clean", side="yes", price=0.50, shares=30),
            ]
            stub.client.get_open_orders.return_value = [{"id": "oid_clean"}]
            stub.client.get_order.return_value = {
                "status": "LIVE", "size_matched": 0, "price": "0.50",
            }
            RewardFarmer._reconcile_on_startup(stub)
            stub.client.cancel_order.assert_called_once()
            stub.db.purge_all_active_orders.assert_called_once()
        finally:
            bc._overrides.clear()
            bc._overrides.update(saved)


if __name__ == "__main__":
    unittest.main()
