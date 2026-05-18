"""Phase 5 — graceful shutdown (FX-014 systemd directives + FX-015 signal handlers).

Coverage:

* SIGINT + SIGTERM handlers registered in ``reward_farmer.RewardFarmer.run``
  (source-grep regression — guards against accidental removal).
* SIGINT + SIGTERM handlers registered in ``oversight_agent.run_loop``.
* ``_shutdown_cleanup`` cancels live buy orders, dump orders, with the
  kill-switch override flag set so cancels fire regardless of mode.
* Structured ``[SHUTDOWN]`` log lines on entry + exit, with cancel counts.
* Reward-state save tolerates an exception (won't crash shutdown).
"""

import inspect
import logging
import os
import signal
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import MarketState, OrderSlot


def _make_ms(cid="cid_001", yes_oid=None, no_oid=None, dump_yes=None, dump_no=None):
    ms = MarketState(
        cid=cid, question="Test", yes_tid="y", no_tid="n",
        daily_rate=20.0, max_spread=0.10, min_size=10, tick_size=0.01,
        yes_price=0.50, agent_shares=50, agent_approved=True,
    )
    if yes_oid:
        ms.orders["yes"] = OrderSlot(order_id=yes_oid, price=0.48, shares=50, placed_at=0)
    if no_oid:
        ms.orders["no"] = OrderSlot(order_id=no_oid, price=0.52, shares=50, placed_at=0)
    if dump_yes:
        ms.dump_orders["yes"] = dump_yes
    if dump_no:
        ms.dump_orders["no"] = dump_no
    return ms


# ── FX-015: handler registration (source-grep regression guards) ────────────


class TestSignalHandlersRegistered(unittest.TestCase):
    """If a future refactor accidentally drops a signal handler, these
    tests catch it before the bot leaks orders on a real shutdown."""

    def test_reward_farmer_run_registers_sigint(self):
        import reward_farmer
        src = inspect.getsource(reward_farmer.RewardFarmer.run)
        self.assertIn("signal.signal(signal.SIGINT", src)

    def test_reward_farmer_run_registers_sigterm(self):
        import reward_farmer
        src = inspect.getsource(reward_farmer.RewardFarmer.run)
        self.assertIn("signal.signal(signal.SIGTERM", src)

    def test_oversight_agent_run_loop_registers_sigint(self):
        import oversight_agent
        src = inspect.getsource(oversight_agent.run_loop)
        self.assertIn("signal.signal(signal.SIGINT", src)

    def test_oversight_agent_run_loop_registers_sigterm(self):
        import oversight_agent
        src = inspect.getsource(oversight_agent.run_loop)
        self.assertIn("signal.signal(signal.SIGTERM", src)


# ── FX-015: _shutdown_cleanup behaviour ─────────────────────────────────────


def _make_farmer_stub(batch_succeeds: bool = True):
    """Minimal stub for testing _shutdown_cleanup directly.

    ``batch_succeeds`` controls whether ``client.cancel_orders`` raises —
    when it raises, the fallback per-order path fires via
    ``_gated_cancel_order``.
    """
    from reward_farmer import RewardFarmer

    class FarmerStub:
        pass

    stub = FarmerStub()
    stub.markets = {}
    stub.client = MagicMock()
    if not batch_succeeds:
        stub.client.cancel_orders.side_effect = RuntimeError(
            "simulated batch cancel failure"
        )
    stub._gated_cancel_order = MagicMock(return_value=True)
    stub.rewards = MagicMock()
    # Mode + kill-switch flag start at LIVE / False; the cleanup flips the
    # kill-switch flag mid-method.
    from reward_farmer import MODE_LIVE
    stub.mode = MODE_LIVE
    stub._kill_switch_active = False
    return stub


class TestShutdownCleanup(unittest.TestCase):
    """``_shutdown_cleanup`` is the production-critical path that runs
    after the main loop sets ``_shutdown = True``. Every live order must
    be cancelled; the call must not raise even on degraded state."""

    def test_cancels_live_buy_orders_via_batch(self):
        from reward_farmer import RewardFarmer
        stub = _make_farmer_stub()
        ms1 = _make_ms(cid="cid_A", yes_oid="oid_A_yes", no_oid="oid_A_no")
        ms2 = _make_ms(cid="cid_B", yes_oid="oid_B_yes")
        stub.markets = {"cid_A": ms1, "cid_B": ms2}
        RewardFarmer._shutdown_cleanup(stub)
        # Phase 5 audit fix 3: cancellation is now a single batch API call.
        stub.client.cancel_orders.assert_called_once()
        batch_oids = stub.client.cancel_orders.call_args.args[0]
        self.assertEqual({"oid_A_yes", "oid_A_no", "oid_B_yes"}, set(batch_oids))
        # Per-order fallback must NOT fire when the batch succeeds.
        stub._gated_cancel_order.assert_not_called()

    def test_cancels_dump_orders_via_batch(self):
        from reward_farmer import RewardFarmer
        stub = _make_farmer_stub()
        ms = _make_ms(cid="cid_A", dump_yes="dump_oid_yes", dump_no="dump_oid_no")
        stub.markets = {"cid_A": ms}
        RewardFarmer._shutdown_cleanup(stub)
        stub.client.cancel_orders.assert_called_once()
        batch_oids = stub.client.cancel_orders.call_args.args[0]
        self.assertEqual({"dump_oid_yes", "dump_oid_no"}, set(batch_oids))

    def test_skips_dry_run_placeholders(self):
        # Placeholder order_ids "dry_yes" / "dry_no" come from OL's internal
        # dry_run branches. They have no real Polymarket counterpart and
        # must NOT enter the batch payload — the API would reject it.
        from reward_farmer import RewardFarmer
        stub = _make_farmer_stub()
        ms = _make_ms(cid="cid_A", yes_oid="dry_yes", no_oid="dry_no")
        stub.markets = {"cid_A": ms}
        RewardFarmer._shutdown_cleanup(stub)
        # all_oids empty → batch call should be skipped entirely
        stub.client.cancel_orders.assert_not_called()
        stub._gated_cancel_order.assert_not_called()

    def test_falls_back_to_per_order_on_batch_failure(self):
        # Phase 5 audit fix 3: when client.cancel_orders raises (rate
        # limit, network, malformed payload), the cleanup must NOT leak
        # orders — it falls back to the per-order _gated_cancel_order loop.
        from reward_farmer import RewardFarmer
        stub = _make_farmer_stub(batch_succeeds=False)
        ms = _make_ms(cid="cid_A", yes_oid="o1", no_oid="o2", dump_yes="d1")
        stub.markets = {"cid_A": ms}
        RewardFarmer._shutdown_cleanup(stub)
        # Batch was attempted...
        stub.client.cancel_orders.assert_called_once()
        # ...then per-order fallback fired for each oid.
        fallback_oids = {c.args[0] for c in stub._gated_cancel_order.call_args_list}
        self.assertEqual({"o1", "o2", "d1"}, fallback_oids)

    def test_sets_kill_switch_active_for_fallback_force_execute(self):
        # The per-order fallback path calls _gated_cancel_order which
        # propagates force_execute = self._kill_switch_active into OL.
        # cleanup must set the flag BEFORE the fallback loop so OL's
        # dry_run shortcut is bypassed.
        from reward_farmer import RewardFarmer
        stub = _make_farmer_stub(batch_succeeds=False)
        flag_at_cancel = {}

        def _track(oid, reason=""):
            flag_at_cancel["v"] = stub._kill_switch_active
            return True

        stub._gated_cancel_order = MagicMock(side_effect=_track)
        ms = _make_ms(cid="cid_A", yes_oid="oid_yes")
        stub.markets = {"cid_A": ms}
        RewardFarmer._shutdown_cleanup(stub)
        self.assertTrue(flag_at_cancel.get("v"),
                        "_kill_switch_active must be True during fallback cancels")

    def test_tolerates_rewards_save_failure(self):
        # If rewards._save() raises, _shutdown_cleanup must still log the
        # complete line and not propagate the exception (shutdown is
        # already past the point of no return).
        from reward_farmer import RewardFarmer
        stub = _make_farmer_stub()
        stub.rewards._save.side_effect = RuntimeError("disk full")
        ms = _make_ms(cid="cid_A", yes_oid="oid_yes")
        stub.markets = {"cid_A": ms}
        # Must not raise.
        RewardFarmer._shutdown_cleanup(stub)
        # Batch cancel still ran.
        stub.client.cancel_orders.assert_called_once()


class TestShutdownLogLines(unittest.TestCase):
    """The structured `[SHUTDOWN]` channel is the operator's signal that
    cleanup ran. Tests assert the entry / exit lines fire with the
    expected counts."""

    def setUp(self):
        self.records: list[logging.LogRecord] = []
        h = logging.Handler()
        h.emit = lambda r: self.records.append(r)
        h.setLevel(logging.DEBUG)
        self.logger = logging.getLogger("reward_farmer")
        self.prev_level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(h)
        self._handler = h

    def tearDown(self):
        self.logger.removeHandler(self._handler)
        self.logger.setLevel(self.prev_level)

    def _shutdown_lines(self):
        return [r.getMessage() for r in self.records if "[SHUTDOWN]" in r.getMessage()]

    def test_logs_beginning_with_counts(self):
        from reward_farmer import RewardFarmer
        stub = _make_farmer_stub()
        ms1 = _make_ms(cid="cid_A", yes_oid="o1", no_oid="o2", dump_yes="d1")
        ms2 = _make_ms(cid="cid_B", yes_oid="o3")
        stub.markets = {"cid_A": ms1, "cid_B": ms2}
        RewardFarmer._shutdown_cleanup(stub)
        lines = self._shutdown_lines()
        self.assertTrue(
            any("cleanup beginning" in line and "3 buy orders" in line
                and "1 dump orders" in line for line in lines),
            f"Expected 'cleanup beginning: 3 buy orders + 1 dump orders' in {lines}",
        )

    def test_logs_completion_with_cancel_counts(self):
        from reward_farmer import RewardFarmer
        stub = _make_farmer_stub()
        ms = _make_ms(cid="cid_A", yes_oid="o1", no_oid="o2")
        stub.markets = {"cid_A": ms}
        RewardFarmer._shutdown_cleanup(stub)
        lines = self._shutdown_lines()
        self.assertTrue(
            any("cleanup complete" in line and "cancelled 2/2" in line
                for line in lines),
            f"Expected 'cleanup complete: cancelled 2/2' in {lines}",
        )

    def test_logs_failed_cancels_count_on_fallback(self):
        # When the batch path fails AND some per-order fallbacks fail too,
        # the failed count is surfaced in the [SHUTDOWN] complete line.
        from reward_farmer import RewardFarmer
        stub = _make_farmer_stub(batch_succeeds=False)
        # First fallback cancel succeeds, second fails.
        stub._gated_cancel_order = MagicMock(side_effect=[True, False])
        ms = _make_ms(cid="cid_A", yes_oid="o1", no_oid="o2")
        stub.markets = {"cid_A": ms}
        RewardFarmer._shutdown_cleanup(stub)
        lines = self._shutdown_lines()
        self.assertTrue(
            any("cancelled 1/2" in line and "(1 failed)" in line for line in lines),
            f"Expected 'cancelled 1/2 ... (1 failed)' in {lines}",
        )

    def test_logs_batch_success_line(self):
        # On the happy path, [SHUTDOWN] batch cancel succeeded line fires.
        from reward_farmer import RewardFarmer
        stub = _make_farmer_stub()
        ms = _make_ms(cid="cid_A", yes_oid="o1", no_oid="o2")
        stub.markets = {"cid_A": ms}
        RewardFarmer._shutdown_cleanup(stub)
        lines = self._shutdown_lines()
        self.assertTrue(
            any("batch cancel succeeded" in line and "2 orders" in line
                for line in lines),
            f"Expected 'batch cancel succeeded: 2 orders' in {lines}",
        )


# ── Phase 5 audit fixes ─────────────────────────────────────────────────────


class TestCancelOrderForceBypassesDryRun(unittest.TestCase):
    """Audit fix 1: OL.cancel_order(..., force=True) must fire a real API
    call even when self.dry_run is True. Without this, the kill-switch
    override path in _gated_cancel_order silently no-ops in SHADOW."""

    def _make_ol(self, dry_run: bool):
        from order_lifecycle import OrderLifecycle
        positions = MagicMock()
        positions.get_shares.return_value = 0
        positions.can_quote.return_value = True
        db = MagicMock()
        db.is_unliquidatable.return_value = False
        ol = OrderLifecycle(
            client=MagicMock(), db=db, positions=positions,
            rewards=MagicMock(), markets={}, dry_run=dry_run,
        )
        ol.capital_ceiling = None
        return ol

    def test_dry_run_default_returns_true_without_api_call(self):
        ol = self._make_ol(dry_run=True)
        result = ol.cancel_order("oid_123", reason="test")
        self.assertTrue(result)
        ol.client.cancel_order.assert_not_called()

    def test_force_true_fires_api_call_even_in_dry_run(self):
        ol = self._make_ol(dry_run=True)
        result = ol.cancel_order("oid_123", reason="test", force=True)
        self.assertTrue(result)
        ol.client.cancel_order.assert_called_once()

    def test_force_true_returns_false_on_api_exception(self):
        ol = self._make_ol(dry_run=True)
        ol.client.cancel_order.side_effect = RuntimeError("API down")
        result = ol.cancel_order("oid_123", reason="test", force=True)
        self.assertFalse(result)
        ol.client.cancel_order.assert_called_once()

    def test_live_mode_force_false_still_fires_api(self):
        ol = self._make_ol(dry_run=False)
        ol.cancel_order("oid_123", reason="test")  # default force=False
        ol.client.cancel_order.assert_called_once()


class TestRateLimiterV2MethodCoverage(unittest.TestCase):
    """Audit fix 2: rate_limiter._RATE_LIMITED_METHODS must cover every
    V2 SDK method production code calls — gaps cause silent 429 leaks."""

    def test_cancel_order_is_rate_limited(self):
        from rate_limiter import RateLimitedClient
        self.assertIn("cancel_order", RateLimitedClient._RATE_LIMITED_METHODS)

    def test_cancel_orders_batch_is_rate_limited(self):
        # Used by _shutdown_cleanup
        from rate_limiter import RateLimitedClient
        self.assertIn("cancel_orders", RateLimitedClient._RATE_LIMITED_METHODS)

    def test_get_open_orders_is_rate_limited(self):
        # V2 SDK rename from get_orders, used at 4 production call sites
        from rate_limiter import RateLimitedClient
        self.assertIn("get_open_orders", RateLimitedClient._RATE_LIMITED_METHODS)

    def test_v1_cancel_name_still_present(self):
        # V1 name kept for back-compat with mixed-SDK test fixtures
        from rate_limiter import RateLimitedClient
        self.assertIn("cancel", RateLimitedClient._RATE_LIMITED_METHODS)


if __name__ == "__main__":
    unittest.main()
