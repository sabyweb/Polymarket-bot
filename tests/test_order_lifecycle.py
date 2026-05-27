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
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── py_clob_client_v2 mock (not installed in local dev env) ─────────────────
# Mirrors the pattern in test_sports_protection.py / test_critical_fixes.py.
# The mock provides MINIMALLY real ``BalanceAllowanceParams`` and
# ``OrderPayload`` shims that pass kwargs through to attributes, so
# FX-037 token_id-routing assertions can introspect what was constructed.
# Plain MagicMock would silently absorb ``token_id=...`` and return another
# MagicMock when accessed — making the contract test pass for the wrong
# reason. R6: tests encode contracts, not implementation details.

class _PassthroughDataclass:
    """A class that stores constructor kwargs as attributes — replaces SDK
    dataclasses (BalanceAllowanceParams, OrderPayload, OrderArgs) so tests
    can introspect what production code constructed via ``call_args``.

    Plain MagicMock would silently absorb ``token_id=...`` and return another
    MagicMock when accessed, making FX-037 contract tests pass for the wrong
    reason.
    """
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _EnumLike:
    """Stand-in for AssetType enum — sentinel attributes."""
    COLLATERAL = "COLLATERAL"
    CONDITIONAL = "CONDITIONAL"


def _install_passthrough_clob_shim() -> None:
    """Ensure ``py_clob_client_v2`` is importable with passthrough semantics
    for FX-037 token_id-routing tests.

    Protocol (works in both local-dev and Helsinki/Ubuntu CI):

    1. Drop any MagicMock-based partial mocks left by sibling tests
       (test_critical_fixes / test_sports_protection set
       ``sys.modules["py_clob_client_v2"]`` to a MagicMock at their own
       module load and never clean up; mirrors
       test_placement.py::_drop_stale_clob_mocks).
    2. Try to import the real SDK. If it imports cleanly, return — the
       real ``BalanceAllowanceParams`` is a dataclass and naturally
       supports ``instance.token_id`` introspection. This is the Helsinki
       CI path (Ubuntu 24.04 + Python 3.14 with the SDK in
       requirements.txt).
    3. Otherwise install passthrough stand-ins so the FX-037 tests can
       introspect what was constructed via ``call_args``. This is the
       local-dev path where the SDK isn't installed.

    Bug history: CI run 26329526380 (2026-05-23, commit 0ec898a) failed
    2/770 tests because the prior version had an early-return guard that
    didn't distinguish "real SDK installed" from "sibling left a
    MagicMock". When the sibling-set MagicMock was present,
    ``BalanceAllowanceParams(token_id=tid).token_id`` returned a
    MagicMock instead of the string, defeating the token_id-routing
    contracts. Fixed by dropping MagicMocks first and re-trying the real
    import.
    """
    # 1. Drop stale MagicMock-based partial mocks from sibling tests.
    stale = [
        k for k in list(sys.modules)
        if (k == "py_clob_client_v2" or k.startswith("py_clob_client_v2."))
        and isinstance(sys.modules[k], MagicMock)
    ]
    for k in stale:
        del sys.modules[k]

    # 2. If the real SDK is available, use it as-is (Helsinki CI path).
    try:
        import py_clob_client_v2.clob_types  # noqa: F401
        import py_clob_client_v2.order_builder.constants  # noqa: F401
        return
    except ImportError:
        pass

    # 3. Real SDK absent (local dev). Install passthrough stand-ins.
    mock_clob = MagicMock()
    clob_types = types.ModuleType("py_clob_client_v2.clob_types")
    clob_types.BalanceAllowanceParams = _PassthroughDataclass
    clob_types.OrderPayload = _PassthroughDataclass
    clob_types.OrderArgs = _PassthroughDataclass
    clob_types.AssetType = _EnumLike

    order_builder = types.ModuleType("py_clob_client_v2.order_builder")
    constants_mod = types.ModuleType("py_clob_client_v2.order_builder.constants")
    constants_mod.BUY = "BUY"
    constants_mod.SELL = "SELL"
    order_builder.constants = constants_mod

    sys.modules["py_clob_client_v2"] = mock_clob
    sys.modules["py_clob_client_v2.clob_types"] = clob_types
    sys.modules["py_clob_client_v2.order_builder"] = order_builder
    sys.modules["py_clob_client_v2.order_builder.constants"] = constants_mod


_install_passthrough_clob_shim()


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
    it every call short-circuits with ``no_market`` and the count stays 0.

    The DB mock is configured with ``is_unliquidatable -> False`` so the
    FX-007 gate doesn't short-circuit every call (auto-MagicMock returns a
    truthy MagicMock instance by default). Tests covering the gate set the
    return value explicitly.
    """
    positions = MagicMock()
    positions.get_shares.return_value = 0
    positions.can_quote.return_value = True
    db = MagicMock()
    db.is_unliquidatable.return_value = False
    markets = {ms.cid: ms} if ms is not None else {}
    ol = OrderLifecycle(
        client=MagicMock(), db=db, positions=positions,
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


# ── FX-037: BUY-side phantom-fill defense ───────────────────────────────────
#
# Contracts encoded by these tests (per R6 in the META charter):
#
#   C1: When the SDK over-reports ``size_matched`` (on-chain delta is less
#       than reported), the helper returns the on-chain truth, not the
#       SDK value. Recorded fills then reflect what was actually delivered.
#
#   C2: When the SDK reports honestly (on-chain delta ≥ matched - 0.5),
#       the helper returns ``matched`` unchanged — behaviour identical to
#       pre-FX-037.
#
#   C3: When ``get_balance_allowance`` raises, the helper fails OPEN with
#       a log.warning, returning ``matched``. Losing a legitimate fill is
#       strictly worse than recording a phantom (which orphan-scan +
#       reconciliation will catch next cycle).
#
#   C4: ``matched <= 0`` is a no-op (no SDK call, no DB hit). Documented
#       precondition: the caller already checked this branch is a fill.
#
#   C5: When the helper detects a phantom, a ``log.critical`` line tagged
#       ``PHANTOM FILL:`` is emitted for operator visibility — the same
#       channel DumpManager uses for SELL-side phantoms.
#
#   C6: Token ID routing — YES side probes ``ms.yes_tid``, NO side probes
#       ``ms.no_tid``. Wrong-token probe would defeat the defense.
#
#   C7: ``actual_delta`` is clamped at ``max(0, on_chain - pre_fill_tracked)``
#       so a positions-table desync (tracker thinks we have more than
#       on-chain) doesn't produce a negative fill quantity.


class TestCheckBuyPhantomFill(unittest.TestCase):
    """FX-037: ``_check_buy_phantom_fill`` unit tests.

    Mirror of DumpManager's PHANTOM FILL check at dump_manager.py:60-87.
    """

    def setUp(self):
        self.ms = _make_ms(cid="cid_phantom", yes_tid="ytid_xyz", no_tid="ntid_abc")
        self.ol = _make_lifecycle(ms=self.ms)
        # Default: positions tracker shows 0 shares pre-fill.
        self.ol.positions.get_shares = MagicMock(return_value=0)

    def _set_on_chain_balance(self, shares: float) -> None:
        """Configure the mocked client to return the given on-chain balance.

        The SDK returns balance in 6-decimal units, so we multiply by 1e6
        on the way out — matches the production reading at
        ``order_lifecycle.py: ... float(bal.get("balance", 0)) / 1e6``.
        """
        self.ol.client.get_balance_allowance = MagicMock(
            return_value={"balance": str(int(shares * 1e6))}
        )

    # ── C1: phantom detected → return on-chain truth ────────────────────

    def test_phantom_detected_returns_on_chain_delta(self):
        """SDK reports 158 sh matched but on-chain delta is 38 → return 38."""
        self._set_on_chain_balance(38)
        out = self.ol._check_buy_phantom_fill(self.ms, "no", matched=158)
        self.assertEqual(38, out)

    def test_phantom_detected_emits_critical_log(self):
        """Phantom detection must emit log.critical with 'PHANTOM FILL:' tag (C5)."""
        self._set_on_chain_balance(38)
        with self.assertLogs("reward_farmer", level="CRITICAL") as cap:
            self.ol._check_buy_phantom_fill(self.ms, "no", matched=158)
        self.assertTrue(
            any("PHANTOM FILL" in line for line in cap.output),
            f"Expected 'PHANTOM FILL' in log output, got: {cap.output}",
        )

    # ── C2: no phantom → return matched unchanged ───────────────────────

    def test_honest_sdk_returns_matched_unchanged(self):
        """On-chain delta exactly matches SDK report → return matched."""
        self._set_on_chain_balance(50)
        out = self.ol._check_buy_phantom_fill(self.ms, "yes", matched=50)
        self.assertEqual(50, out)

    def test_on_chain_exceeds_matched_returns_matched(self):
        """On-chain delta > matched (e.g. concurrent silent fill) → still trust SDK.

        We can only attribute up to ``matched`` shares to THIS order. The
        surplus is a separate concern that ``_reconcile_after_unknown``
        handles.
        """
        self._set_on_chain_balance(100)
        out = self.ol._check_buy_phantom_fill(self.ms, "yes", matched=50)
        self.assertEqual(50, out)

    def test_within_tolerance_returns_matched_unchanged(self):
        """Tolerance window: actual_delta = matched - 0.4 should not trip."""
        # The threshold in the helper is ``actual_delta < matched - 0.5``.
        # 49.6 < 50 - 0.5 = 49.5 is False, so we trust SDK and return 50.
        self._set_on_chain_balance(49.6)
        out = self.ol._check_buy_phantom_fill(self.ms, "yes", matched=50)
        self.assertEqual(50, out)

    def test_just_below_tolerance_triggers_phantom(self):
        """Tolerance window: actual_delta = matched - 1 should trip."""
        self._set_on_chain_balance(49)
        out = self.ol._check_buy_phantom_fill(self.ms, "yes", matched=50)
        self.assertEqual(49, out)

    # ── C3: API failure → fail-open with warning ────────────────────────

    def test_api_exception_returns_matched_and_warns(self):
        """get_balance_allowance raising → fail-open, log.warning."""
        self.ol.client.get_balance_allowance = MagicMock(
            side_effect=ConnectionError("transient network blip")
        )
        with self.assertLogs("reward_farmer", level="WARNING") as cap:
            out = self.ol._check_buy_phantom_fill(self.ms, "no", matched=158)
        self.assertEqual(158, out, "Fail-open MUST preserve SDK value")
        self.assertTrue(
            any("phantom check failed" in line.lower() for line in cap.output),
            f"Expected 'phantom check failed' in warning, got: {cap.output}",
        )

    # ── C4: matched <= 0 → no-op ────────────────────────────────────────

    def test_matched_zero_short_circuits(self):
        """matched = 0 → return 0, no SDK call (defensive against UNKNOWN path)."""
        self.ol.client.get_balance_allowance = MagicMock()
        out = self.ol._check_buy_phantom_fill(self.ms, "yes", matched=0)
        self.assertEqual(0, out)
        self.ol.client.get_balance_allowance.assert_not_called()

    def test_matched_negative_short_circuits(self):
        """Negative matched (shouldn't happen but defensive) → return as-is."""
        self.ol.client.get_balance_allowance = MagicMock()
        out = self.ol._check_buy_phantom_fill(self.ms, "yes", matched=-5)
        self.assertEqual(-5, out)
        self.ol.client.get_balance_allowance.assert_not_called()

    # ── C6: token_id routing per side ───────────────────────────────────

    def test_yes_side_probes_yes_tid(self):
        """YES side fill must probe ms.yes_tid, not ms.no_tid."""
        self._set_on_chain_balance(50)
        self.ol._check_buy_phantom_fill(self.ms, "yes", matched=50)
        params = self.ol.client.get_balance_allowance.call_args[0][0]
        self.assertEqual("ytid_xyz", params.token_id)

    def test_no_side_probes_no_tid(self):
        """NO side fill must probe ms.no_tid, not ms.yes_tid."""
        self._set_on_chain_balance(50)
        self.ol._check_buy_phantom_fill(self.ms, "no", matched=50)
        params = self.ol.client.get_balance_allowance.call_args[0][0]
        self.assertEqual("ntid_abc", params.token_id)

    # ── C7: actual_delta clamped at zero ────────────────────────────────

    def test_negative_delta_clamps_to_zero(self):
        """positions tracker > on-chain (rare desync) → actual_delta = 0, full phantom."""
        # Tracker thinks we have 100 sh; on-chain says 50. Pre-fill mismatch
        # of -50 should clamp to 0 actual_delta, treating the fill as full
        # phantom rather than negative.
        self.ol.positions.get_shares.return_value = 100
        self._set_on_chain_balance(50)
        out = self.ol._check_buy_phantom_fill(self.ms, "yes", matched=10)
        self.assertEqual(0, out)


class TestDetectFillsPhantomIntegration(unittest.TestCase):
    """FX-037: end-to-end integration — detect_fills must apply the phantom
    check before invoking handle_fill, and route a zeroed result through
    the clean-slot path without recording a fill.

    FX-054 note: ``slot.placed_at`` is set 120s in the past so the new
    balance-lag tolerance window (60s default) does NOT trip and the
    phantom check fires the original FX-037 way. Separate audit tests
    in tests/test_audit_fill_detection.py cover the new lag-tolerance
    branch.

    FX-054 note 2: ``positions.get_shares`` is wired to bump after
    ``record_fill`` is called so the end-of-cycle drift sweep sees a
    consistent tracked-vs-on-chain pair and doesn't double-count.
    Without the wiring the mock returns 0 forever, drift sweep would
    see on_chain > tracked and emit a redundant catch-up call.
    """

    def setUp(self):
        self.ms = _make_ms(cid="cid_int", yes_tid="ytid", no_tid="ntid")
        # FX-054: placed_at well outside the 60s balance-lag tolerance so the
        # phantom check fires per FX-037 contract.
        self.ms.orders["no"] = OrderSlot(
            order_id="OID_PHANTOM_001", price=0.53, shares=158,
            placed_at=time.time() - 120,
        )
        self.ol = _make_lifecycle(ms=self.ms)
        self.ol.positions.get_shares = MagicMock(return_value=0)
        # SDK returns the inflated size_matched (the FX-037 bug shape)
        self.ol.client.get_order = MagicMock(
            return_value={"status": "MATCHED", "size_matched": 158, "price": 0.53}
        )
        # On-chain only delivered 38 NO shares (the actual payload)
        self.ol.client.get_balance_allowance = MagicMock(
            return_value={"balance": str(int(38 * 1e6))}
        )

        # Replace handle_fill with a recording mock that ALSO bumps the
        # positions mock — so the drift sweep sees on_chain == tracked
        # post-primary and doesn't double-count.
        def _record_and_track(*args, **kwargs):
            shares = kwargs.get("actual_shares", 0)
            current = self.ol.positions.get_shares.return_value
            self.ol.positions.get_shares.return_value = current + shares
        self.ol.handle_fill = MagicMock(side_effect=_record_and_track)

    def test_detect_fills_records_on_chain_truth_not_sdk_inflation(self):
        """The 2026-05-19 Iran NO regression: SDK says 158, chain says 38.

        Contract: handle_fill is invoked with actual_shares=38, NOT 158.
        Without FX-037 this test would fail with the SDK-value 158.
        """
        # open_ids does NOT contain our order — that triggers the get_order
        # → matched branch in detect_fills.
        self.ol.detect_fills(open_ids=set())
        self.ol.handle_fill.assert_called_once()
        kwargs = self.ol.handle_fill.call_args.kwargs
        self.assertEqual(38, kwargs["actual_shares"],
                         "FX-037: recorded fill must reflect on-chain delta, not SDK report")

    def test_detect_fills_skips_record_on_full_phantom(self):
        """If on-chain delta is 0 (and the order is older than the FX-054
        60s balance-lag tolerance), no fill is recorded. Slot still cleared.
        The drift sweep also reads on_chain=0, so no catch-up either.
        """
        # On-chain delta = 0 (full phantom)
        self.ol.client.get_balance_allowance.return_value = {"balance": "0"}
        self.ol.detect_fills(open_ids=set())
        self.ol.handle_fill.assert_not_called()
        # Slot cleared regardless — exchange has confirmed the order isn't open
        self.assertIsNone(self.ms.orders["no"].order_id)

    def test_detect_fills_unchanged_on_honest_sdk(self):
        """Backwards compat: honest SDK fill (matched == on-chain delta) → unchanged."""
        # On-chain shows full 158 sh delivered — no phantom
        self.ol.client.get_balance_allowance.return_value = {"balance": str(int(158 * 1e6))}
        self.ol.detect_fills(open_ids=set())
        self.ol.handle_fill.assert_called_once()
        kwargs = self.ol.handle_fill.call_args.kwargs
        self.assertEqual(158, kwargs["actual_shares"])


if __name__ == "__main__":
    unittest.main()
