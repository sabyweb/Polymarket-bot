"""FX-089 — dump unwinds booked at the marketable EXECUTION price, not the limit.

check_dump_fills used `status["price"]` (the order's LIMIT) to compute proceeds.
A marketable dump SELL (the aggressive/passive dump sets a low limit to force a
fill) executes at the BID via price improvement, not its limit — so the limit
massively over-states the loss. Verified on-chain: a dump booked at limit $0.01
actually executed at ~$0.24 → recorded −$54 vs real −$8 (the source of the
WALLET_DESYNC alarms + inflated realized-loss that poisoned the earlier
analysis). The fix re-prices to the best bid for the sold side when it beats the
limit, fail-open to the limit; the FX-049/055 reconciler backstops residual drift.

Mirrors tests/test_dump_manager_fee.py.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dump_manager import DumpManager  # noqa: E402
from models import MarketState  # noqa: E402


def _ms():
    ms = MarketState(
        cid="cid89", question="FX-089 exec-price test?",
        yes_tid="ytid89", no_tid="ntid89",
        daily_rate=50.0, max_spread=0.045, min_size=20, tick_size=0.01,
        yes_price=0.50, agent_shares=200, agent_approved=True,
    )
    ms.dump_orders["yes"] = "dump_oid_89"
    ms.dump_state["yes"] = {"fill_price": 0.28, "started_at": time.time() - 60,
                            "shares": 200, "tid": "ytid89"}
    return ms


def _dm(positions):
    return DumpManager(client=MagicMock(), db=MagicMock(), positions=positions,
                       cancel_fn=MagicMock(), dry_run=False)


def _cfg(name):
    # check_dump_fills only reads RF_POLYMARKET_TAKER_FEE; default the rest.
    return 0.009 if name == "RF_POLYMARKET_TAKER_FEE" else 2


class TestFX089DumpExecPrice(unittest.TestCase):

    @patch("dump_manager.get_merged_book")
    @patch("dump_manager.cfg", side_effect=_cfg)
    def test_marketable_dump_recorded_at_bid_not_limit(self, _c, mock_book):
        """Aggressive dump: limit $0.01, bid $0.24, cost $0.28 → record at $0.24,
        loss ~−$8 (NOT −$54 as the limit would imply)."""
        mock_book.return_value = {"bids": [{"price": "0.24", "size": "500"}],
                                  "asks": [{"price": "0.80", "size": "500"}]}
        positions = MagicMock()
        positions.get_shares.return_value = 200.0
        positions.get_avg_price.return_value = 0.28
        dm = _dm(positions)
        dm.client.get_order.return_value = {"status": "MATCHED", "price": "0.01", "size_matched": "200"}
        dm.client.get_balance_allowance.return_value = {"balance": "0"}  # real fill

        dm.check_dump_fills({"cid89": _ms()}, open_ids=set())

        dm.db.log_unwind.assert_called_once()
        kw = dm.db.log_unwind.call_args.kwargs
        self.assertEqual(kw["sell_price"], 0.24)  # re-priced to bid, not the 0.01 limit
        self.assertAlmostEqual(kw["usd_value"], 200 * 0.24 * 0.991, places=2)
        # pnl = usd_value - vwap_cost ≈ 47.57 − 56 = −8.4 — NOT the −54 the limit
        # (200 × 0.01 × 0.991 − 56) would have produced.
        pnl = kw["usd_value"] - kw["vwap_cost"]
        self.assertAlmostEqual(kw["vwap_cost"], 56.0, places=1)
        self.assertGreater(pnl, -12.0)
        self.assertLess(pnl, 0.0)

    @patch("dump_manager.get_merged_book")
    @patch("dump_manager.cfg", side_effect=_cfg)
    def test_maker_fill_keeps_limit_when_bid_below(self, _c, mock_book):
        """If the bid is BELOW the order's price (a maker fill at its own limit),
        keep the limit — never under-state proceeds."""
        mock_book.return_value = {"bids": [{"price": "0.45", "size": "500"}],
                                  "asks": [{"price": "0.80", "size": "500"}]}
        positions = MagicMock()
        positions.get_shares.return_value = 200.0
        positions.get_avg_price.return_value = 0.40
        dm = _dm(positions)
        dm.client.get_order.return_value = {"status": "MATCHED", "price": "0.50", "size_matched": "200"}
        dm.client.get_balance_allowance.return_value = {"balance": "0"}

        dm.check_dump_fills({"cid89": _ms()}, open_ids=set())

        kw = dm.db.log_unwind.call_args.kwargs
        self.assertEqual(kw["sell_price"], 0.50)  # limit kept (bid 0.45 < 0.50)

    @patch("dump_manager.get_merged_book")
    @patch("dump_manager.cfg", side_effect=_cfg)
    def test_book_unavailable_falls_back_to_limit(self, _c, mock_book):
        """Fail-open: no book → use the limit (prior, loss-over-stating behavior)."""
        mock_book.return_value = None
        positions = MagicMock()
        positions.get_shares.return_value = 200.0
        positions.get_avg_price.return_value = 0.28
        dm = _dm(positions)
        dm.client.get_order.return_value = {"status": "MATCHED", "price": "0.05", "size_matched": "200"}
        dm.client.get_balance_allowance.return_value = {"balance": "0"}

        dm.check_dump_fills({"cid89": _ms()}, open_ids=set())

        kw = dm.db.log_unwind.call_args.kwargs
        self.assertEqual(kw["sell_price"], 0.05)  # fell back to limit

    @patch("dump_manager.get_merged_book", side_effect=RuntimeError("book api down"))
    @patch("dump_manager.cfg", side_effect=_cfg)
    def test_book_exception_falls_back_to_limit(self, _c, _b):
        positions = MagicMock()
        positions.get_shares.return_value = 200.0
        positions.get_avg_price.return_value = 0.28
        dm = _dm(positions)
        dm.client.get_order.return_value = {"status": "MATCHED", "price": "0.06", "size_matched": "200"}
        dm.client.get_balance_allowance.return_value = {"balance": "0"}

        dm.check_dump_fills({"cid89": _ms()}, open_ids=set())

        kw = dm.db.log_unwind.call_args.kwargs
        self.assertEqual(kw["sell_price"], 0.06)  # exception → limit


if __name__ == "__main__":
    unittest.main()
