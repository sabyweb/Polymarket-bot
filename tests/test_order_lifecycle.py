"""OrderLifecycle unit tests covering FX-004 (counter / DB consistency).

``place_orders_for_market`` returns the count of API-confirmed placements
written to the ``orders_placed`` DB table (0, 1, or 2). The farmer's
``_gated_place_orders_for_market`` accumulates this into
``_cycle_orders_placed`` so the telemetry counter matches the DB.

These tests stub the CLOB client and DB, drive the function through every
return path (early returns, dry-run, partial success, API failure,
full success), and assert the returned count.
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import MarketState, OrderSlot
from order_lifecycle import OrderLifecycle


# ── fixtures ─────────────────────────────────────────────────────────────────

def _make_ms(**overrides) -> MarketState:
    defaults = dict(
        cid="cid_001", question="Test market?", yes_tid="ytid", no_tid="ntid",
        daily_rate=20.0, max_spread=0.10, min_size=10, tick_size=0.01,
        yes_price=0.50, agent_shares=50, agent_approved=True,
    )
    defaults.update(overrides)
    return MarketState(**defaults)


def _healthy_book() -> dict:
    return {
        "bids": [{"price": "0.48", "size": "500"}],
        "asks": [{"price": "0.52", "size": "500"}],
    }


def _make_lifecycle(dry_run: bool = False, ms: MarketState | None = None) -> OrderLifecycle:
    """Build an OrderLifecycle with a single market registered when ``ms`` is
    given. ``can_place`` requires the market to be in ``self.markets``; without
    it every call short-circuits with ``no_market`` and the count stays 0."""
    positions = MagicMock()
    positions.get_shares.return_value = 0
    positions.can_quote.return_value = True
    markets = {ms.cid: ms} if ms is not None else {}
    ol = OrderLifecycle(
        client=MagicMock(), db=MagicMock(), positions=positions,
        rewards=MagicMock(), markets=markets, dry_run=dry_run,
    )
    ol.capital_ceiling = None
    return ol


def _ok_response_yes(*_args, **_kwargs):
    """Successful YES placement response from the V2 SDK."""
    return {"orderID": "OID_YES_001"}


def _ok_response_no(*_args, **_kwargs):
    return {"orderID": "OID_NO_001"}


def _ok_response_either(*_args, **_kwargs):
    # Side determined by call order; tests can alternate via side_effect.
    return {"orderID": "OID_OK"}


# ── FX-004: returned count semantics ─────────────────────────────────────────


class TestPlaceOrdersForMarketReturnsCount(unittest.TestCase):

    @patch("order_lifecycle.get_merged_book")
    def test_returns_2_when_both_sides_placed(self, mock_book):
        mock_book.return_value = _healthy_book()
        ms = _make_ms()
        ol = _make_lifecycle(dry_run=False, ms=ms)
        ol.client.create_and_post_order.side_effect = [
            {"orderID": "OID_YES"}, {"orderID": "OID_NO"}
        ]
        self.assertEqual(2, ol.place_orders_for_market(ms))

    @patch("order_lifecycle.get_merged_book")
    def test_returns_1_when_yes_succeeds_and_no_raises(self, mock_book):
        mock_book.return_value = _healthy_book()
        ms = _make_ms()
        ol = _make_lifecycle(dry_run=False, ms=ms)
        ol.client.create_and_post_order.side_effect = [
            {"orderID": "OID_YES"},
            RuntimeError("V2 SDK 400 — simulated NO failure"),
        ]
        self.assertEqual(1, ol.place_orders_for_market(ms))

    @patch("order_lifecycle.get_merged_book")
    def test_returns_1_when_yes_raises_and_no_succeeds(self, mock_book):
        mock_book.return_value = _healthy_book()
        ms = _make_ms()
        ol = _make_lifecycle(dry_run=False, ms=ms)
        ol.client.create_and_post_order.side_effect = [
            RuntimeError("V2 SDK 400 — simulated YES failure"),
            {"orderID": "OID_NO"},
        ]
        self.assertEqual(1, ol.place_orders_for_market(ms))

    @patch("order_lifecycle.get_merged_book")
    def test_returns_0_when_both_api_calls_raise(self, mock_book):
        mock_book.return_value = _healthy_book()
        ms = _make_ms()
        ol = _make_lifecycle(dry_run=False, ms=ms)
        ol.client.create_and_post_order.side_effect = [
            RuntimeError("simulated YES failure"),
            RuntimeError("simulated NO failure"),
        ]
        self.assertEqual(0, ol.place_orders_for_market(ms))

    @patch("order_lifecycle.get_merged_book")
    def test_returns_0_when_response_missing_order_id(self, mock_book):
        mock_book.return_value = _healthy_book()
        ms = _make_ms()
        ol = _make_lifecycle(dry_run=False, ms=ms)
        # API returns 200 but no orderID — counts as failure, no DB write.
        ol.client.create_and_post_order.return_value = {"status": "rejected"}
        self.assertEqual(0, ol.place_orders_for_market(ms))


class TestPlaceOrdersForMarketEarlyReturns(unittest.TestCase):

    @patch("order_lifecycle.get_merged_book")
    def test_returns_0_when_book_unavailable(self, mock_book):
        mock_book.return_value = None
        ms = _make_ms()
        ol = _make_lifecycle(dry_run=False, ms=ms)
        self.assertEqual(0, ol.place_orders_for_market(ms))
        self.assertEqual(1, ms.book_failures)

    @patch("order_lifecycle.get_merged_book")
    def test_returns_0_when_book_empty(self, mock_book):
        mock_book.return_value = {"bids": [], "asks": []}
        ms = _make_ms()
        ol = _make_lifecycle(dry_run=False, ms=ms)
        self.assertEqual(0, ol.place_orders_for_market(ms))

    @patch("order_lifecycle.get_merged_book")
    def test_returns_0_when_book_spread_too_wide(self, mock_book):
        mock_book.return_value = {
            "bids": [{"price": "0.10", "size": "500"}],
            "asks": [{"price": "0.90", "size": "500"}],
        }
        ms = _make_ms()
        ol = _make_lifecycle(dry_run=False, ms=ms)
        self.assertEqual(0, ol.place_orders_for_market(ms))

    @patch("order_lifecycle.get_merged_book")
    def test_returns_0_when_both_sides_already_have_orders_and_book_fresh(
        self, mock_book
    ):
        ms = _make_ms()
        ms.orders["yes"] = OrderSlot(order_id="existing_yes", price=0.48,
                                     shares=50, placed_at=time.time())
        ms.orders["no"] = OrderSlot(order_id="existing_no", price=0.52,
                                    shares=50, placed_at=time.time())
        ms.last_book_fetch = time.time()  # fresh
        ol = _make_lifecycle(dry_run=False, ms=ms)
        self.assertEqual(0, ol.place_orders_for_market(ms))
        # Confirms we hit the early-return before fetching the book.
        mock_book.assert_not_called()  # noqa: F841 (mock used by decorator)

    @patch("order_lifecycle.get_merged_book")
    def test_returns_0_when_market_in_resolution_proximity(self, mock_book):
        # Midpoint > 0.90 → resolution proximity → block.
        mock_book.return_value = {
            "bids": [{"price": "0.93", "size": "500"}],
            "asks": [{"price": "0.95", "size": "500"}],
        }
        ms = _make_ms()
        ol = _make_lifecycle(dry_run=False, ms=ms)
        self.assertEqual(0, ol.place_orders_for_market(ms))


class TestPlaceOrdersForMarketDryRunReturnsZero(unittest.TestCase):
    """dry_run path writes fake order IDs into ms.orders but does NOT touch
    the orders_placed DB table, so it must not contribute to the counter."""

    @patch("order_lifecycle.get_merged_book")
    def test_dry_run_returns_0(self, mock_book):
        mock_book.return_value = _healthy_book()
        ms = _make_ms()
        ol = _make_lifecycle(dry_run=True)
        self.assertEqual(0, ol.place_orders_for_market(ms))
        # log_order_placed must NOT have been called.
        ol.db.log_order_placed.assert_not_called()


# ── FX-004: gated wrapper accumulates correctly ──────────────────────────────


class TestGatedWrapperAccumulation(unittest.TestCase):
    """RewardFarmer._gated_place_orders_for_market should add the returned
    count into _cycle_orders_placed — not unconditionally +=1."""

    def setUp(self):
        from reward_farmer import RewardFarmer, MODE_LIVE
        self.MODE_LIVE = MODE_LIVE

        # Build a minimal farmer stub with only the attributes
        # _gated_place_orders_for_market touches.
        farmer = MagicMock(spec=RewardFarmer)
        farmer.mode = MODE_LIVE
        farmer._cycle_orders_placed = 0
        farmer.order_lifecycle = MagicMock()
        # Bind the real method to the stub so we exercise the real wrapper logic.
        farmer._gated_place_orders_for_market = (
            RewardFarmer._gated_place_orders_for_market.__get__(farmer, RewardFarmer)
        )
        self.farmer = farmer

    def _call_wrapper_returning(self, n: int):
        self.farmer.order_lifecycle.place_orders_for_market.return_value = n
        ms = MagicMock()
        self.farmer._gated_place_orders_for_market(ms)

    def test_counter_unchanged_when_zero_placed(self):
        self._call_wrapper_returning(0)
        self.assertEqual(0, self.farmer._cycle_orders_placed)

    def test_counter_increments_by_one_on_partial_success(self):
        self._call_wrapper_returning(1)
        self.assertEqual(1, self.farmer._cycle_orders_placed)

    def test_counter_increments_by_two_on_full_success(self):
        self._call_wrapper_returning(2)
        self.assertEqual(2, self.farmer._cycle_orders_placed)

    def test_counter_accumulates_across_calls(self):
        self.farmer.order_lifecycle.place_orders_for_market.side_effect = [
            2, 0, 1, 0, 2
        ]
        for _ in range(5):
            self.farmer._gated_place_orders_for_market(MagicMock())
        # 2 + 0 + 1 + 0 + 2 = 5
        self.assertEqual(5, self.farmer._cycle_orders_placed)

    def test_counter_tolerates_pre_fx004_none_return(self):
        # Defence: a stale stub (or a future regression that drops the
        # return) returns None. Counter must not advance; must not raise.
        self.farmer.order_lifecycle.place_orders_for_market.return_value = None
        self.farmer._gated_place_orders_for_market(MagicMock())
        self.assertEqual(0, self.farmer._cycle_orders_placed)

    def test_non_live_mode_does_not_call_or_increment(self):
        from reward_farmer import MODE_DRY_RUN
        self.farmer.mode = MODE_DRY_RUN
        self.farmer._log_dry_run_intent = MagicMock()
        ms = MagicMock()
        ms.cid = "cid"
        ms.question = "Q"
        self.farmer._gated_place_orders_for_market(ms)
        self.assertEqual(0, self.farmer._cycle_orders_placed)
        self.farmer.order_lifecycle.place_orders_for_market.assert_not_called()
        self.farmer._log_dry_run_intent.assert_called_once()


if __name__ == "__main__":
    unittest.main()
