"""FX-050: DumpManager applies Polymarket taker fee to recorded unwind value.

Contracts (R6):

C1: When ``RF_POLYMARKET_TAKER_FEE = 0.009`` (default), a dump matched
    at book price P with size S produces ``log_unwind(usd_value=
    S × P × (1 − 0.009))``. The bot's recorded revenue matches the cash
    actually settled to the wallet after Polymarket's taker fee.

C2: When ``RF_POLYMARKET_TAKER_FEE = 0`` (escape hatch), behaviour is
    byte-identical to pre-FX-050 — ``usd_value = S × P``.

C3: The phantom-fill defense (FX-007 SELL-side, dump_manager.py:60-87)
    is unaffected — when the exchange balance still shows the shares,
    no unwind is recorded regardless of fee value.

C4: pnl in the log line uses the post-fee revenue, not gross.

EVIDENCE (production verification from 2026-05-22 incident):
  - Trade: SELL 50 NO @ $0.78 (book)
  - Gross revenue (pre-FX-050): 50 × $0.78 = $39.00
  - Post-fee (data-api usdcSize): $38.6568
  - Fee: $0.3432 / $39.00 = 0.88%
  - With RF_POLYMARKET_TAKER_FEE=0.009: bot would record $39 × 0.991 = $38.649
    (matches actual $38.6568 within $0.01 — float rounding only)
"""

import os
import sys
import time
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Mirror the test_order_lifecycle SDK shim — see test_order_lifecycle.py
# for full rationale. Drop stale MagicMock partial mocks, try real SDK,
# fall back to passthrough.
def _install_passthrough_clob_shim() -> None:
    stale = [
        k for k in list(sys.modules)
        if (k == "py_clob_client_v2" or k.startswith("py_clob_client_v2."))
        and isinstance(sys.modules[k], MagicMock)
    ]
    for k in stale:
        del sys.modules[k]
    try:
        import py_clob_client_v2.clob_types  # noqa: F401
        return
    except ImportError:
        pass

    class _PassthroughDataclass:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class _EnumLike:
        COLLATERAL = "COLLATERAL"
        CONDITIONAL = "CONDITIONAL"

    mock_clob = MagicMock()
    clob_types = types.ModuleType("py_clob_client_v2.clob_types")
    clob_types.BalanceAllowanceParams = _PassthroughDataclass
    clob_types.OrderPayload = _PassthroughDataclass
    clob_types.OrderArgs = _PassthroughDataclass
    clob_types.AssetType = _EnumLike
    sys.modules["py_clob_client_v2"] = mock_clob
    sys.modules["py_clob_client_v2.clob_types"] = clob_types


_install_passthrough_clob_shim()

from models import MarketState  # noqa: E402
from dump_manager import DumpManager  # noqa: E402


def _make_ms_with_dump(**overrides) -> MarketState:
    """MarketState with an active dump order on the YES side."""
    defaults = dict(
        cid="cid_fx050", question="FX-050 fee test?",
        yes_tid="ytid_fee", no_tid="ntid_fee",
        daily_rate=50.0, max_spread=0.045, min_size=20, tick_size=0.01,
        yes_price=0.50, agent_shares=50, agent_approved=True,
    )
    defaults.update(overrides)
    ms = MarketState(**defaults)
    ms.dump_orders["yes"] = "dump_oid_fee_test"
    ms.dump_state["yes"] = {
        "fill_price": 0.50, "started_at": time.time() - 60,
        "shares": 50, "tid": "ytid_fee",
    }
    return ms


def _make_dm(positions: MagicMock, fee_value: float, retry_threshold: int = 2):
    """DumpManager stub. cfg() is mocked with side_effect to return:
       - RF_POLYMARKET_TAKER_FEE → fee_value (FX-050)
       - RF_UNKNOWN_RETRY_THRESHOLD → retry_threshold (unrelated)
    """
    dm = DumpManager(
        client=MagicMock(), db=MagicMock(), positions=positions,
        cancel_fn=MagicMock(), dry_run=False,
    )
    return dm


class TestDumpManagerTakerFee(unittest.TestCase):
    """FX-050: post-fee usd_value reflects cash actually settled, not book gross."""

    @patch("dump_manager.cfg")
    def test_default_fee_009_applied_to_unwind_usd_value(self, mock_cfg):
        """C1: default fee 0.009 → usd_value = matched × price × 0.991."""
        # cfg("RF_POLYMARKET_TAKER_FEE") → 0.009
        # cfg("RF_UNKNOWN_RETRY_THRESHOLD") → 2 (unrelated path)
        def cfg_side_effect(name):
            if name == "RF_POLYMARKET_TAKER_FEE":
                return 0.009
            return 2
        mock_cfg.side_effect = cfg_side_effect

        ms = _make_ms_with_dump()
        positions = MagicMock()
        positions.get_shares.return_value = 50.0
        positions.get_avg_price.return_value = 0.50
        dm = _make_dm(positions, fee_value=0.009)
        # SDK reports a 50-share match at $0.78 book price
        dm.client.get_order.return_value = {
            "status": "MATCHED", "price": "0.78", "size_matched": "50",
        }
        # Exchange balance is 0 → real fill (not phantom)
        dm.client.get_balance_allowance.return_value = {"balance": "0"}

        dm.check_dump_fills({"cid_fx050": ms}, open_ids=set())

        # Verify log_unwind was called with the POST-FEE usd_value
        dm.db.log_unwind.assert_called_once()
        kwargs = dm.db.log_unwind.call_args.kwargs
        gross = 50 * 0.78
        expected_net = gross * (1 - 0.009)
        self.assertAlmostEqual(
            kwargs["usd_value"], expected_net, places=4,
            msg=f"FX-050: usd_value must be net-of-fee. "
                f"Got {kwargs['usd_value']}, expected {expected_net} (gross {gross} × 0.991)",
        )
        # sell_price stays the book price (informational, unchanged from pre-fix)
        self.assertEqual(kwargs["sell_price"], 0.78)
        # shares unchanged
        self.assertEqual(kwargs["shares"], 50.0)

    @patch("dump_manager.cfg")
    def test_fee_zero_reverts_to_pre_fx050_behaviour(self, mock_cfg):
        """C2: RF_POLYMARKET_TAKER_FEE=0 → usd_value = matched × price (escape hatch)."""
        def cfg_side_effect(name):
            if name == "RF_POLYMARKET_TAKER_FEE":
                return 0.0
            return 2
        mock_cfg.side_effect = cfg_side_effect

        ms = _make_ms_with_dump()
        positions = MagicMock()
        positions.get_shares.return_value = 50.0
        positions.get_avg_price.return_value = 0.50
        dm = _make_dm(positions, fee_value=0.0)
        dm.client.get_order.return_value = {
            "status": "MATCHED", "price": "0.78", "size_matched": "50",
        }
        dm.client.get_balance_allowance.return_value = {"balance": "0"}

        dm.check_dump_fills({"cid_fx050": ms}, open_ids=set())

        kwargs = dm.db.log_unwind.call_args.kwargs
        # With fee=0, usd_value should equal gross (matched × price)
        self.assertAlmostEqual(
            kwargs["usd_value"], 50 * 0.78, places=4,
            msg="FX-050: fee=0 escape hatch must produce gross revenue (pre-fix behaviour)",
        )

    @patch("dump_manager.cfg")
    def test_higher_fee_scales_proportionally(self, mock_cfg):
        """Tunability: 5% fee → 50 × 0.78 × 0.95 = 37.05."""
        def cfg_side_effect(name):
            if name == "RF_POLYMARKET_TAKER_FEE":
                return 0.05
            return 2
        mock_cfg.side_effect = cfg_side_effect

        ms = _make_ms_with_dump()
        positions = MagicMock()
        positions.get_shares.return_value = 50.0
        positions.get_avg_price.return_value = 0.50
        dm = _make_dm(positions, fee_value=0.05)
        dm.client.get_order.return_value = {
            "status": "MATCHED", "price": "0.78", "size_matched": "50",
        }
        dm.client.get_balance_allowance.return_value = {"balance": "0"}

        dm.check_dump_fills({"cid_fx050": ms}, open_ids=set())

        kwargs = dm.db.log_unwind.call_args.kwargs
        self.assertAlmostEqual(kwargs["usd_value"], 50 * 0.78 * 0.95, places=4)

    @patch("dump_manager.cfg")
    def test_phantom_path_still_skips_record_regardless_of_fee(self, mock_cfg):
        """C3: phantom defense (FX-007 SELL-side) fires before fee math; no log_unwind."""
        def cfg_side_effect(name):
            if name == "RF_POLYMARKET_TAKER_FEE":
                return 0.009
            return 2
        mock_cfg.side_effect = cfg_side_effect

        ms = _make_ms_with_dump()
        positions = MagicMock()
        positions.get_shares.return_value = 50.0   # Tracked 50
        positions.get_avg_price.return_value = 0.50
        dm = _make_dm(positions, fee_value=0.009)
        dm.client.get_order.return_value = {
            "status": "MATCHED", "price": "0.78", "size_matched": "50",
        }
        # Exchange balance still shows 50 sh → phantom!
        dm.client.get_balance_allowance.return_value = {
            "balance": str(50 * 1_000_000),
        }

        dm.check_dump_fills({"cid_fx050": ms}, open_ids=set())

        # Phantom defense fired — no unwind recorded
        dm.db.log_unwind.assert_not_called()
        positions.record_unwind.assert_not_called()

    @patch("dump_manager.cfg")
    def test_pnl_uses_post_fee_revenue(self, mock_cfg):
        """C4: pnl = post_fee_revenue − vwap_cost (not gross − cost)."""
        def cfg_side_effect(name):
            if name == "RF_POLYMARKET_TAKER_FEE":
                return 0.009
            return 2
        mock_cfg.side_effect = cfg_side_effect

        ms = _make_ms_with_dump()
        positions = MagicMock()
        positions.get_shares.return_value = 50.0
        # Avg buy price 0.80 (NO-direct). For NO side, to_clob converts:
        # to_clob(YES-equiv p, "no") = 1-p. So avg_p=0.20 YES-equiv→
        # to_clob(0.20, "no") = 0.80. Cost = 50 × 0.80 = $40.
        positions.get_avg_price.return_value = 0.20  # YES-equiv
        ms_no_side = _make_ms_with_dump()
        ms_no_side.dump_orders["no"] = "dump_oid_no"
        ms_no_side.dump_state["no"] = {
            "fill_price": 0.20, "started_at": time.time() - 60,
            "shares": 50, "tid": "ntid_fee",
        }
        ms_no_side.dump_orders["yes"] = None  # only NO side has dump
        ms_no_side.dump_state["yes"] = None

        dm = _make_dm(positions, fee_value=0.009)
        dm.client.get_order.return_value = {
            "status": "MATCHED", "price": "0.78", "size_matched": "50",
        }
        dm.client.get_balance_allowance.return_value = {"balance": "0"}

        dm.check_dump_fills({"cid_fx050": ms_no_side}, open_ids=set())

        kwargs = dm.db.log_unwind.call_args.kwargs
        # log_unwind itself computes pnl internally (database.py:486), so
        # we verify the INPUTS that drive it: usd_value (post-fee) and
        # vwap_cost. The DB then computes pnl = usd_value - vwap_cost.
        expected_net = 50 * 0.78 * (1 - 0.009)
        expected_cost = 50 * 0.80  # 1 - 0.20 (NO conversion)
        self.assertAlmostEqual(kwargs["usd_value"], expected_net, places=4)
        self.assertAlmostEqual(kwargs["vwap_cost"], expected_cost, places=4)
        expected_pnl = expected_net - expected_cost
        # Sanity: matches the production-incident magnitude (~−$1.34 net)
        # rather than the pre-fix under-reporting (~−$1.00).
        self.assertAlmostEqual(expected_pnl, -1.349, places=2,
                               msg="Sanity check: post-fee pnl should match real loss magnitude")


class TestDumpSlippageFloor(unittest.TestCase):
    """FX-071: dump_position floors the SELL price so a single dump never
    crystallizes more than RF_DUMP_MAX_SLIPPAGE_FRAC below the cost basis.
    The aggressive-decay branch previously walked the price down with no floor
    (the 2026-05-25 13.3% class: $0.08->$0.07)."""

    def _cfg(self, max_slip):
        def cfg_side_effect(name):
            return {
                "RF_DUMP_ABANDON_MINS": 30.0,
                "RF_DUMP_AGGRESSIVE_MINS": 5.0,
                "RF_DUMP_PASSIVE_REPRICE_MINS": 5.0,
                "RF_DUMP_MAX_SLIPPAGE_FRAC": max_slip,
                "RF_UNKNOWN_RETRY_THRESHOLD": 2,
            }.get(name, 0)
        return cfg_side_effect

    def _dm_and_ms(self, started_min_ago, cost=0.50):
        positions = MagicMock()
        dm = DumpManager(client=MagicMock(), db=MagicMock(), positions=positions,
                         cancel_fn=MagicMock(), dry_run=False)
        dm.db.is_unliquidatable.return_value = False
        dm.client.get_balance_allowance.return_value = {"balance": 50_000_000}  # 50 sh
        dm.client.create_and_post_order.return_value = {"orderID": "oid_new"}
        ms = _make_ms_with_dump(tick_size=0.01)
        ms.dump_state["yes"] = {
            "fill_price": cost, "started_at": time.time() - started_min_ago * 60,
            "shares": 50, "tid": "ytid_fee",
        }
        ms.dump_orders["yes"] = None  # no existing order to cancel
        return dm, ms

    def _posted_price(self, dm):
        args = dm.client.create_and_post_order.call_args
        self.assertIsNotNone(args, "create_and_post_order was not called")
        return args.args[0].price

    @patch("dump_manager.cfg")
    def test_aggressive_decay_floored_at_cap(self, mock_cfg):
        """3.5m in → decay_ticks=4 → 0.50-0.04=0.46 < floor 0.475 → floored."""
        mock_cfg.side_effect = self._cfg(0.05)
        dm, ms = self._dm_and_ms(started_min_ago=3.5, cost=0.50)
        dm.dump_position(ms, "yes", 50)
        self.assertAlmostEqual(self._posted_price(dm), 0.475, places=4)

    @patch("dump_manager.cfg")
    def test_within_cap_not_floored(self, mock_cfg):
        """1.5m in → decay_ticks=2 → 0.50-0.02=0.48 >= floor 0.475 → unchanged."""
        mock_cfg.side_effect = self._cfg(0.05)
        dm, ms = self._dm_and_ms(started_min_ago=1.5, cost=0.50)
        dm.dump_position(ms, "yes", 50)
        self.assertAlmostEqual(self._posted_price(dm), 0.48, places=4)

    @patch("dump_manager.cfg")
    def test_floor_disabled_reverts_to_pre_fx071(self, mock_cfg):
        """frac=0 → no floor → decayed 0.46 posted (byte-equivalent to pre-FX-071)."""
        mock_cfg.side_effect = self._cfg(0.0)
        dm, ms = self._dm_and_ms(started_min_ago=3.5, cost=0.50)
        dm.dump_position(ms, "yes", 50)
        self.assertAlmostEqual(self._posted_price(dm), 0.46, places=4)

    @patch("dump_manager.cfg")
    def test_unknown_cost_basis_not_floored(self, mock_cfg):
        """fill_price=0 (orphan/startup, cost unknown) → no floor; falls through
        to FX-066 Tier 1 + FX-074 paging. Decay from 0 clamps to 0.01."""
        mock_cfg.side_effect = self._cfg(0.05)
        dm, ms = self._dm_and_ms(started_min_ago=3.5, cost=0.0)
        dm.dump_position(ms, "yes", 50)
        self.assertAlmostEqual(self._posted_price(dm), 0.01, places=4)


if __name__ == "__main__":
    unittest.main()
