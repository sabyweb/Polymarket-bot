"""FX-066 Tier 1 — an unknown-cost dump must never record positive pnl.

Orphan / startup-recovered positions are registered from on-chain balance via
set_shares, which sets the share count but NO price → get_avg_price returns 0.
Pre-fix, check_dump_fills computed vwap_cost=0 when avg_price=0, so
pnl = usd_value − 0 = +sell_revenue: a REAL loss recorded as a PROFIT, which
the 24h-loss kill (SUM(pnl WHERE pnl<0)) then ignores → kill goes blind.

Tier 1 floors vwap_cost to the gross (pre-fee) proceeds when the cost basis
is unknown, so pnl = sell_revenue − gross = −fee ≤ 0 — visible to the kill as
a (small) loss, never a phantom profit. The TRUE loss magnitude is Tier 2
(reconstruct avg_price at orphan registration) + FX-074 (wallet reconciler).
"""

import os
import sys
import time
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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

    clob_types = types.ModuleType("py_clob_client_v2.clob_types")
    clob_types.BalanceAllowanceParams = _PassthroughDataclass
    clob_types.OrderPayload = _PassthroughDataclass
    clob_types.OrderArgs = _PassthroughDataclass
    clob_types.AssetType = _EnumLike
    sys.modules["py_clob_client_v2"] = MagicMock()
    sys.modules["py_clob_client_v2.clob_types"] = clob_types


_install_passthrough_clob_shim()

from models import MarketState  # noqa: E402
from dump_manager import DumpManager  # noqa: E402


def _make_ms_with_dump(**overrides) -> MarketState:
    defaults = dict(
        cid="cid_fx066", question="FX-066 orphan dump?",
        yes_tid="ytid", no_tid="ntid",
        daily_rate=50.0, max_spread=0.045, min_size=20, tick_size=0.01,
        yes_price=0.50, agent_shares=50, agent_approved=True,
    )
    defaults.update(overrides)
    ms = MarketState(**defaults)
    ms.dump_orders["yes"] = "dump_oid"
    ms.dump_state["yes"] = {
        "fill_price": 0.50, "started_at": time.time() - 60,
        "shares": 50, "tid": "ytid",
    }
    return ms


def _run_dump(avg_price: float, fee: float):
    """Run one check_dump_fills with get_avg_price=avg_price; return the
    kwargs passed to db.log_unwind (or None if not called)."""
    def cfg_side_effect(name):
        if name == "RF_POLYMARKET_TAKER_FEE":
            return fee
        return 2
    positions = MagicMock()
    positions.get_shares.return_value = 50.0
    positions.get_avg_price.return_value = avg_price
    dm = DumpManager(client=MagicMock(), db=MagicMock(), positions=positions,
                     cancel_fn=MagicMock(), dry_run=False)
    dm.client.get_order.return_value = {
        "status": "MATCHED", "price": "0.78", "size_matched": "50",
    }
    dm.client.get_balance_allowance.return_value = {"balance": "0"}  # real fill
    ms = _make_ms_with_dump()
    with patch("dump_manager.cfg", side_effect=cfg_side_effect):
        dm.check_dump_fills({"cid_fx066": ms}, open_ids=set())
    if dm.db.log_unwind.called:
        return dm.db.log_unwind.call_args.kwargs
    return None


class TestFX066Tier1LossFloor(unittest.TestCase):

    def test_unknown_cost_pnl_not_positive_with_fee(self):
        """avg_price=0, fee=0.009 → vwap_cost floored to gross (39.0);
        pnl = net − gross = −fee < 0 (never a profit)."""
        kw = _run_dump(avg_price=0.0, fee=0.009)
        self.assertIsNotNone(kw)
        gross = 50 * 0.78
        self.assertAlmostEqual(kw["vwap_cost"], gross, places=4,
            msg="unknown cost must floor vwap_cost to gross proceeds")
        pnl = kw["usd_value"] - kw["vwap_cost"]
        self.assertLessEqual(pnl, 0.0, f"unknown-cost pnl must be <=0, got {pnl}")
        self.assertAlmostEqual(pnl, -gross * 0.009, places=4)

    def test_unknown_cost_pnl_not_positive_fee_zero(self):
        """Even with fee=0 the invariant holds: pnl = gross − gross = 0 (≤0),
        never positive."""
        kw = _run_dump(avg_price=0.0, fee=0.0)
        pnl = kw["usd_value"] - kw["vwap_cost"]
        self.assertLessEqual(pnl, 0.0, f"pnl must be <=0 at fee=0, got {pnl}")

    def test_known_cost_unchanged(self):
        """avg_price>0 → vwap_cost = matched × to_clob(avg_p); Tier 1 must NOT
        alter the known-cost path (regression guard)."""
        from price import to_clob
        kw = _run_dump(avg_price=0.50, fee=0.009)
        expected = 50 * to_clob(0.50, "yes")
        self.assertAlmostEqual(kw["vwap_cost"], expected, places=4,
            msg="known-cost path must be unchanged by Tier 1")

    def test_pre_fix_would_have_been_positive(self):
        """Sanity: with the floored cost, a $0.78 sell that pre-fix would have
        recorded pnl=+$38.66 (vwap_cost=0) now records pnl<=0."""
        kw = _run_dump(avg_price=0.0, fee=0.009)
        pre_fix_pnl = kw["usd_value"] - 0.0  # what pre-fix vwap_cost=0 yielded
        self.assertGreater(pre_fix_pnl, 0, "pre-fix this WAS a phantom profit")
        post_fix_pnl = kw["usd_value"] - kw["vwap_cost"]
        self.assertLessEqual(post_fix_pnl, 0, "Tier 1 converts it to a non-profit")


if __name__ == "__main__":
    unittest.main()
