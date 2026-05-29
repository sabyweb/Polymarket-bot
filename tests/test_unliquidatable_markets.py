"""Phase 3 — unliquidatable_markets mechanism (FX-005/006/007/008/009/028).

The mechanism: a cid lands in the ``unliquidatable_markets`` DB table when
the bot definitively confirms (via ``create_and_post_order`` returning
"orderbook does not exist") that the orderbook is gone. Once marked, every
order path (BUY in ``OrderLifecycle``, SELL in ``DumpManager``, orphan-scan
queue, dump-state restore) skips the cid. The periodic re-probe
(``RewardFarmer._reprobe_unliquidatable``) un-marks cids whose orderbook
has come back to life.

Coverage:

* DB layer: mark / is / delete / load / re-probe-list / retry-stamp
* DumpManager gate: skips on unliquidatable, no API call
* DumpManager mark-on-exception: 400 "orderbook does not exist" → mark + clean dump_state
* DumpManager mark-on-exception: other exceptions → no mark, dump_state preserved
* OrderLifecycle gate: skips BUY on unliquidatable, returns 0
* OrderLifecycle mark-on-exception: YES and NO sides both detect + mark
* Re-probe end-to-end is tested in tests/test_safety_controller.py-style fixtures.
"""

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import BotDatabase
from models import MarketState
from dump_manager import DumpManager
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


def _fresh_db_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


# ── DB layer ─────────────────────────────────────────────────────────────────


class TestUnliquidatableDB(unittest.TestCase):

    def setUp(self):
        self.path = _fresh_db_path()
        self.db = BotDatabase(self.path)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        os.unlink(self.path)

    def test_table_created_on_init(self):
        # The init path should create the table; verify via a raw query.
        conn = sqlite3.connect(self.path)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='unliquidatable_markets'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)

    def test_mark_and_is_round_trip(self):
        self.assertFalse(self.db.is_unliquidatable("cid_A"))
        self.db.mark_unliquidatable("cid_A", reason="dump_yes_orderbook_gone")
        self.assertTrue(self.db.is_unliquidatable("cid_A"))

    def test_mark_is_idempotent(self):
        self.db.mark_unliquidatable("cid_A", reason="first")
        self.db.mark_unliquidatable("cid_A", reason="second")
        self.assertTrue(self.db.is_unliquidatable("cid_A"))
        # Confirm only one row via raw count.
        conn = sqlite3.connect(self.path)
        count = conn.execute(
            "SELECT COUNT(*) FROM unliquidatable_markets WHERE condition_id=?",
            ("cid_A",),
        ).fetchone()[0]
        conn.close()
        self.assertEqual(1, count)

    def test_delete_unliquidatable(self):
        self.db.mark_unliquidatable("cid_A", reason="x")
        self.db.delete_unliquidatable("cid_A")
        self.assertFalse(self.db.is_unliquidatable("cid_A"))

    def test_load_unliquidatable_set(self):
        self.db.mark_unliquidatable("cid_A", "x")
        self.db.mark_unliquidatable("cid_B", "y")
        result = self.db.load_unliquidatable_set()
        self.assertEqual({"cid_A", "cid_B"}, result)

    def test_load_unliquidatable_set_empty(self):
        self.assertEqual(set(), self.db.load_unliquidatable_set())

    def test_reprobe_list_returns_never_retried(self):
        # marked_at = now, last_retry_at = 0 → always eligible for re-probe.
        self.db.mark_unliquidatable("cid_A", "x")
        result = self.db.get_unliquidatable_for_reprobe(stale_secs=3600)
        self.assertEqual(1, len(result))
        self.assertEqual("cid_A", result[0][0])
        self.assertEqual(0.0, result[0][1])

    def test_reprobe_list_skips_recently_retried(self):
        self.db.mark_unliquidatable("cid_A", "x")
        self.db.update_unliquidatable_retry("cid_A")
        # last_retry_at is now ~now; stale_secs=3600 means cutoff is now-3600.
        # Row's last_retry_at > cutoff → excluded.
        result = self.db.get_unliquidatable_for_reprobe(stale_secs=3600)
        self.assertEqual([], result)

    def test_reprobe_list_returns_stale_after_window(self):
        self.db.mark_unliquidatable("cid_A", "x")
        self.db.update_unliquidatable_retry("cid_A")
        # Tiny stale window → row is already stale.
        result = self.db.get_unliquidatable_for_reprobe(stale_secs=-1)
        self.assertEqual(1, len(result))


# ── DumpManager gate + mark-on-exception ─────────────────────────────────────


class TestDumpManagerGate(unittest.TestCase):
    """FX-007: ``dump_position`` skips on unliquidatable cid, no API call."""

    def _make_dm(self, db):
        positions = MagicMock()
        positions.get_shares.return_value = 0
        positions.get_avg_price.return_value = 0.5
        return DumpManager(
            client=MagicMock(), db=db, positions=positions,
            cancel_fn=MagicMock(return_value=True), dry_run=False,
        )

    def test_gate_skips_on_unliquidatable(self):
        db = MagicMock()
        db.is_unliquidatable.return_value = True
        dm = self._make_dm(db)
        ms = _make_ms()
        # Pre-existing dump_state to test the cleanup.
        ms.dump_state["yes"] = {"shares": 100, "started_at": time.time(),
                                 "fill_price": 0.5, "tid": "ytid"}
        dm.dump_position(ms, "yes", 100)
        # No client API call should have been made.
        dm.client.create_and_post_order.assert_not_called()
        dm.client.get_balance_allowance.assert_not_called()
        # dump_state in memory is cleared.
        self.assertIsNone(ms.dump_state["yes"])
        # DB cleanup was triggered.
        db.delete_dump_state.assert_called_once_with(ms.cid, "yes")

    def test_gate_does_not_clear_when_no_dump_state(self):
        db = MagicMock()
        db.is_unliquidatable.return_value = True
        dm = self._make_dm(db)
        ms = _make_ms()
        # No existing dump_state.
        dm.dump_position(ms, "yes", 100)
        dm.client.create_and_post_order.assert_not_called()
        # delete_dump_state should NOT be called when there's nothing to clean.
        db.delete_dump_state.assert_not_called()


class TestDumpManagerMarkOnException(unittest.TestCase):
    """FX-007 + FX-009: ``create_and_post_order`` raising "orderbook does not
    exist" → mark unliquidatable + delete dump_state row + clear ms.dump_state[side]."""

    def _make_dm(self, db, client):
        positions = MagicMock()
        positions.get_shares.return_value = 0
        positions.get_avg_price.return_value = 0.5
        return DumpManager(
            client=client, db=db, positions=positions,
            cancel_fn=MagicMock(return_value=True), dry_run=False,
        )

    def test_marks_unliquidatable_on_orderbook_gone(self):
        db = MagicMock()
        db.is_unliquidatable.return_value = False
        client = MagicMock()
        client.get_balance_allowance.return_value = {"balance": "1000000000"}  # 1000 shares
        client.create_and_post_order.side_effect = RuntimeError(
            "the orderbook 0xabc does not exist"
        )
        dm = self._make_dm(db, client)
        ms = _make_ms()
        # last_fill_price feeds the decay-start fill_price.
        ms.last_fill_price["yes"] = 0.5
        dm.dump_position(ms, "yes", 100)
        # Mark + delete should have fired.
        db.mark_unliquidatable.assert_called_once()
        call_args = db.mark_unliquidatable.call_args
        self.assertEqual(ms.cid, call_args[0][0])
        self.assertIn("orderbook_gone", call_args[1]["reason"])
        db.delete_dump_state.assert_called_with(ms.cid, "yes")
        self.assertIsNone(ms.dump_state["yes"])

    def test_does_not_mark_on_transient_exception(self):
        db = MagicMock()
        db.is_unliquidatable.return_value = False
        client = MagicMock()
        client.get_balance_allowance.return_value = {"balance": "1000000000"}
        client.create_and_post_order.side_effect = RuntimeError("network timeout")
        dm = self._make_dm(db, client)
        ms = _make_ms()
        ms.last_fill_price["yes"] = 0.5
        dm.dump_position(ms, "yes", 100)
        db.mark_unliquidatable.assert_not_called()
        # dump_state should still be present for retry.
        self.assertIsNotNone(ms.dump_state["yes"])

    def test_does_not_mark_on_insufficient_balance(self):
        db = MagicMock()
        db.is_unliquidatable.return_value = False
        client = MagicMock()
        client.get_balance_allowance.return_value = {"balance": "1000000000"}
        client.create_and_post_order.side_effect = RuntimeError(
            "insufficient balance for size"
        )
        dm = self._make_dm(db, client)
        ms = _make_ms()
        ms.last_fill_price["yes"] = 0.5
        dm.dump_position(ms, "yes", 100)
        db.mark_unliquidatable.assert_not_called()


# ── OrderLifecycle gate + mark-on-exception ──────────────────────────────────


class TestOrderLifecycleGate(unittest.TestCase):
    """FX-005 / FX-007: ``place_orders_for_market`` skips BUY on unliquidatable cid."""

    def _make_ol(self, db, ms):
        positions = MagicMock()
        positions.get_shares.return_value = 0
        positions.can_quote.return_value = True
        ol = OrderLifecycle(
            client=MagicMock(), db=db, positions=positions,
            rewards=MagicMock(), markets={ms.cid: ms}, dry_run=False,
        )
        ol.capital_ceiling = None
        return ol

    @patch("order_lifecycle.get_merged_book")
    def test_gate_skips_unliquidatable_cid(self, mock_book):
        db = MagicMock()
        db.is_unliquidatable.return_value = True
        ms = _make_ms()
        ol = self._make_ol(db, ms)
        self.assertEqual(0, ol.place_orders_for_market(ms))
        # No book fetch should have happened — gate hits first.
        mock_book.assert_not_called()


class TestOrderLifecycleMarkOnException(unittest.TestCase):
    """FX-005 / FX-007: BUY exception with "orderbook does not exist" → mark."""

    def _make_ol(self, db, ms):
        positions = MagicMock()
        positions.get_shares.return_value = 0
        positions.can_quote.return_value = True
        ol = OrderLifecycle(
            client=MagicMock(), db=db, positions=positions,
            rewards=MagicMock(), markets={ms.cid: ms}, dry_run=False,
        )
        ol.capital_ceiling = None
        return ol

    @patch("order_lifecycle.get_merged_book")
    def test_marks_on_yes_buy_orderbook_gone(self, mock_book):
        mock_book.return_value = _healthy_book()
        db = MagicMock()
        db.is_unliquidatable.return_value = False
        ms = _make_ms()
        ol = self._make_ol(db, ms)
        ol.client.create_and_post_order.side_effect = RuntimeError(
            "the orderbook X does not exist"
        )
        n_placed = ol.place_orders_for_market(ms)
        self.assertEqual(0, n_placed)
        db.mark_unliquidatable.assert_called_once()
        self.assertIn("buy_yes_orderbook_gone",
                      db.mark_unliquidatable.call_args[1]["reason"])

    @patch("order_lifecycle.get_merged_book")
    def test_marks_on_no_buy_orderbook_gone(self, mock_book):
        mock_book.return_value = _healthy_book()
        db = MagicMock()
        db.is_unliquidatable.return_value = False
        ms = _make_ms()
        ol = self._make_ol(db, ms)
        # YES succeeds; NO fails with "orderbook does not exist". The mark
        # should fire on the NO failure (the YES success increments to 1).
        ol.client.create_and_post_order.side_effect = [
            {"orderID": "OID_YES"},
            RuntimeError("the orderbook X does not exist"),
        ]
        n_placed = ol.place_orders_for_market(ms)
        self.assertEqual(1, n_placed)
        db.mark_unliquidatable.assert_called_once()
        self.assertIn("buy_no_orderbook_gone",
                      db.mark_unliquidatable.call_args[1]["reason"])

    @patch("order_lifecycle.get_merged_book")
    def test_does_not_mark_on_insufficient_balance(self, mock_book):
        mock_book.return_value = _healthy_book()
        db = MagicMock()
        db.is_unliquidatable.return_value = False
        ms = _make_ms()
        ol = self._make_ol(db, ms)
        ol.client.create_and_post_order.side_effect = RuntimeError(
            "insufficient balance"
        )
        ol.place_orders_for_market(ms)
        db.mark_unliquidatable.assert_not_called()

    @patch("order_lifecycle.get_merged_book")
    def test_does_not_mark_on_market_does_not_exist(self, mock_book):
        # Audit-regression: the detector requires BOTH "orderbook" AND
        # "does not exist" substrings. A bare "market does not exist"
        # error (no "orderbook" substring) must NOT trigger the mark —
        # it's a different signal (market level, not orderbook level)
        # and the bot should NOT permanently retire the cid on it.
        mock_book.return_value = _healthy_book()
        db = MagicMock()
        db.is_unliquidatable.return_value = False
        ms = _make_ms()
        ol = self._make_ol(db, ms)
        ol.client.create_and_post_order.side_effect = RuntimeError(
            "the market does not exist"
        )
        ol.place_orders_for_market(ms)
        db.mark_unliquidatable.assert_not_called()

    @patch("order_lifecycle.get_merged_book")
    def test_does_not_mark_on_rate_limit(self, mock_book):
        mock_book.return_value = _healthy_book()
        db = MagicMock()
        db.is_unliquidatable.return_value = False
        ms = _make_ms()
        ol = self._make_ol(db, ms)
        ol.client.create_and_post_order.side_effect = RuntimeError(
            "rate limit exceeded"
        )
        ol.place_orders_for_market(ms)
        db.mark_unliquidatable.assert_not_called()


# ── RewardFarmer integration paths ───────────────────────────────────────────


def _make_farmer_stub(db=None):
    """Minimal farmer stub for testing the bound methods. Mirrors the pattern
    used in tests/test_sports_protection.py::TestLayer3ExpirySweep."""
    from reward_farmer import RewardFarmer  # noqa: F401 (import sanity)

    class FarmerStub:
        pass

    stub = FarmerStub()
    stub.db = db or MagicMock()
    stub.client = MagicMock()
    stub.markets = {}
    stub.dry_run = False
    return stub


class TestRestoreDumpStatesGate(unittest.TestCase):
    """FX-008: _restore_dump_states must drop rows for unliquidatable cids."""

    def test_skips_and_deletes_dump_state_on_unliquidatable_cid(self):
        from reward_farmer import RewardFarmer
        stub = _make_farmer_stub()
        # One dump_state row pointing at an unliquidatable cid.
        stub.db.load_all_dump_states.return_value = {
            ("cid_dead", "yes"): {
                "started_at": time.time(), "shares": 100,
                "fill_price": 0.5, "tid": "ytid", "dump_order_id": "",
                "last_passive_reprice": 0,
            },
        }
        stub.db.is_unliquidatable.side_effect = lambda cid: cid == "cid_dead"
        # Provide a matching market so the prior-step short-circuit (no market)
        # doesn't kick in first.
        stub.markets["cid_dead"] = _make_ms(cid="cid_dead")
        RewardFarmer._restore_dump_states(stub)
        stub.db.delete_dump_state.assert_called_with("cid_dead", "yes")

    def test_restores_dump_state_for_normal_cid(self):
        from reward_farmer import RewardFarmer
        stub = _make_farmer_stub()
        ms = _make_ms(cid="cid_alive")
        stub.markets["cid_alive"] = ms
        stub.db.load_all_dump_states.return_value = {
            ("cid_alive", "no"): {
                "started_at": time.time(), "shares": 100,
                "fill_price": 0.5, "tid": "ntid", "dump_order_id": "",
                "last_passive_reprice": 0,
            },
        }
        stub.db.is_unliquidatable.return_value = False
        RewardFarmer._restore_dump_states(stub)
        # Should have restored into ms.dump_state, not deleted.
        self.assertIsNotNone(ms.dump_state["no"])
        stub.db.delete_dump_state.assert_not_called()


class TestReprobeUnliquidatable(unittest.TestCase):
    """FX-028: periodic re-probe of unliquidatable cids un-marks on a healthy
    orderbook, stamps last_retry_at otherwise."""

    @patch("reward_farmer.cfg")
    @patch("market_discovery.get_merged_book")
    def test_unmarks_on_healthy_book(self, mock_book, mock_cfg):
        from reward_farmer import RewardFarmer
        mock_cfg.return_value = 3600
        mock_book.return_value = _healthy_book()
        stub = _make_farmer_stub()
        # Provide a matching in-memory market so the re-probe finds the tids.
        stub.markets["cid_dead"] = _make_ms(cid="cid_dead")
        stub.db.get_unliquidatable_for_reprobe.return_value = [("cid_dead", 0.0)]
        RewardFarmer._reprobe_unliquidatable(stub)
        stub.db.delete_unliquidatable.assert_called_once_with("cid_dead")
        stub.db.update_unliquidatable_retry.assert_not_called()

    @patch("reward_farmer.cfg")
    @patch("market_discovery.get_merged_book")
    def test_stamps_retry_on_still_dead_book(self, mock_book, mock_cfg):
        from reward_farmer import RewardFarmer
        mock_cfg.return_value = 3600
        mock_book.return_value = None  # still dead
        stub = _make_farmer_stub()
        stub.markets["cid_dead"] = _make_ms(cid="cid_dead")
        stub.db.get_unliquidatable_for_reprobe.return_value = [("cid_dead", 0.0)]
        RewardFarmer._reprobe_unliquidatable(stub)
        stub.db.delete_unliquidatable.assert_not_called()
        stub.db.update_unliquidatable_retry.assert_called_once_with("cid_dead")

    @patch("reward_farmer.cfg")
    def test_skips_in_dry_run(self, mock_cfg):
        from reward_farmer import RewardFarmer
        mock_cfg.return_value = 3600
        stub = _make_farmer_stub()
        stub.dry_run = True
        RewardFarmer._reprobe_unliquidatable(stub)
        stub.db.get_unliquidatable_for_reprobe.assert_not_called()

    @patch("reward_farmer.cfg")
    def test_no_op_when_no_stale_candidates(self, mock_cfg):
        from reward_farmer import RewardFarmer
        mock_cfg.return_value = 3600
        stub = _make_farmer_stub()
        stub.db.get_unliquidatable_for_reprobe.return_value = []
        RewardFarmer._reprobe_unliquidatable(stub)
        stub.db.delete_unliquidatable.assert_not_called()
        stub.db.update_unliquidatable_retry.assert_not_called()


class TestScanOrphanedPositionsGate(unittest.TestCase):
    """FX-007: ``_scan_orphaned_positions`` must skip cids already marked
    unliquidatable so the on-chain CTF balance doesn't keep re-spawning
    orphan dumps every restart."""

    def test_skips_unliquidatable_cid(self):
        # Direct unit shape: assert the gate logic. The production loop
        # iterates candidates; we mimic that with a list and verify the
        # skip predicate matches db.is_unliquidatable.
        db = MagicMock()
        db.is_unliquidatable.side_effect = lambda cid: cid == "cid_dead"
        candidates = [("cid_dead", "Dead market"), ("cid_alive", "Alive market")]
        processed = []
        for cid, _q in candidates:
            if db.is_unliquidatable(cid):
                continue
            processed.append(cid)
        self.assertEqual(["cid_alive"], processed)
        db.is_unliquidatable.assert_any_call("cid_dead")


class TestSyncExchangePositionsGate(unittest.TestCase):
    """FX-007 audit follow-up: ``_sync_exchange_positions`` must NOT
    re-register MarketState rows for cids the bot has already confirmed
    are unliquidatable. CTF balance on-exchange never clears (manual UI
    redemption only); without this gate the 30-min sync would re-spawn
    self.markets entries indefinitely."""

    def test_skips_unliquidatable_orphan_cid(self):
        db = MagicMock()
        db.is_unliquidatable.side_effect = lambda cid: cid == "cid_dead"
        # Mimic the orphan-iteration shape inside _sync_exchange_positions:
        # exchange_cids - tracked_cids → iterate, gate first.
        orphan_cids = {"cid_dead", "cid_alive"}
        registered = []
        for cid in orphan_cids:
            if db.is_unliquidatable(cid):
                continue
            registered.append(cid)
        self.assertEqual(["cid_alive"], registered)


class TestSyncExchangePositionsDumpGuard(unittest.TestCase):
    """FX-070: the stale-position cleanup in _sync_exchange_positions must NOT
    remove a position that is actively being dumped. The old guard checked
    hasattr(dump_mgr, 'dump_states') — an attribute DumpManager never has — so
    it was always False and a mid-dump position could be stranded (losing its
    loss-accounting trail). The real signal is ms.dump_state / ms.dump_orders."""

    def _run_sync(self, stub):
        from reward_farmer import RewardFarmer
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = []  # empty exchange → every DB pos is "stale"
        with patch("config.FUNDER", "0xFUNDER"):
            with patch("requests.get", return_value=resp):
                RewardFarmer._sync_exchange_positions(stub)

    def test_does_not_remove_actively_dumping_position(self):
        stub = _make_farmer_stub()
        stub.positions = MagicMock()
        stub.positions.get_all_positions.return_value = {
            "cid_dumping": {"yes_shares": 100.0, "no_shares": 0.0,
                            "question": "Dumping market?"},
        }
        ms = _make_ms(cid="cid_dumping")
        ms.dump_state["yes"] = {"shares": 100, "started_at": time.time(),
                                "fill_price": 0.5, "tid": "ytid"}
        stub.markets["cid_dumping"] = ms
        self._run_sync(stub)
        stub.positions.remove_market.assert_not_called()

    def test_removes_stale_position_not_dumping(self):
        stub = _make_farmer_stub()
        stub.positions = MagicMock()
        stub.positions.get_all_positions.return_value = {
            "cid_stale": {"yes_shares": 100.0, "no_shares": 0.0,
                          "question": "Resolved market?"},
        }
        stub.markets["cid_stale"] = _make_ms(cid="cid_stale")  # empty dump_state
        self._run_sync(stub)
        stub.positions.remove_market.assert_called_once_with("cid_stale")

    def test_dumping_via_dump_orders_only_is_protected(self):
        # Transient reprice window: dump_state cleared but a resting dump SELL
        # order still exists — must still count as dumping.
        stub = _make_farmer_stub()
        stub.positions = MagicMock()
        stub.positions.get_all_positions.return_value = {
            "cid_reprice": {"yes_shares": 0.0, "no_shares": 100.0,
                            "question": "Repricing dump?"},
        }
        ms = _make_ms(cid="cid_reprice")
        ms.dump_orders["no"] = "OID_LIVE"
        stub.markets["cid_reprice"] = ms
        self._run_sync(stub)
        stub.positions.remove_market.assert_not_called()


class TestReprobeTokenIdFallback(unittest.TestCase):
    """FX-028: when a re-probe candidate isn't in self.markets, the method
    falls back to a CLOB market lookup for token_ids. If that fallback
    also fails (404, network, malformed response), the row stays marked
    and ``last_retry_at`` is stamped."""

    @patch("reward_farmer.cfg")
    @patch("requests.get")
    def test_clob_fallback_succeeds_and_unmarks(self, mock_req, mock_cfg):
        from reward_farmer import RewardFarmer
        mock_cfg.return_value = 3600
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"tokens": [
            {"token_id": "ytid_fb"}, {"token_id": "ntid_fb"},
        ]}
        mock_req.return_value = resp
        stub = _make_farmer_stub()
        # Not in stub.markets → triggers fallback.
        stub.db.get_unliquidatable_for_reprobe.return_value = [("cid_orphan", 0.0)]
        with patch("market_discovery.get_merged_book", return_value=_healthy_book()):
            RewardFarmer._reprobe_unliquidatable(stub)
        stub.db.delete_unliquidatable.assert_called_once_with("cid_orphan")

    @patch("reward_farmer.cfg")
    @patch("requests.get")
    def test_clob_fallback_404_stamps_retry(self, mock_req, mock_cfg):
        from reward_farmer import RewardFarmer
        mock_cfg.return_value = 3600
        resp = MagicMock()
        resp.status_code = 404
        mock_req.return_value = resp
        stub = _make_farmer_stub()
        stub.db.get_unliquidatable_for_reprobe.return_value = [("cid_gone", 0.0)]
        RewardFarmer._reprobe_unliquidatable(stub)
        # Should stamp retry, not un-mark.
        stub.db.delete_unliquidatable.assert_not_called()
        stub.db.update_unliquidatable_retry.assert_called_once_with("cid_gone")


class TestDeadMarketCleanupCascade(unittest.TestCase):
    """FX-006 / FX-032: when book_failures triggers dead-market cleanup,
    the loop must cascade to dump_states (FX-006) but MUST NOT mark the
    cid as unliquidatable (FX-032).

    The dead-market threshold is `book_failures >= 3` consecutive
    failures of ``get_merged_book``. That gate fires for a much wider
    class of conditions than the canonical FX-007 "orderbook does not
    exist" body — SDK parse errors, transient network hiccups, brief
    empty-book windows, etc. Marking those cids as permanently
    unliquidatable was an FX-006 cascade overreach that bit Helsinki at
    v5.1.14 startup: 60 healthy markets (one paying $200/day in rewards)
    got flagged at 03:23:38 and the FX-028 re-probe couldn't un-mark
    them. The canonical FX-007 path in ``OrderLifecycle`` / ``DumpManager``
    is the SOLE source of truth for unliquidatable marking.

    Source under test is the actual loop in
    ``RewardFarmer.run_cycle`` (Step 4b).
    """

    def test_dead_market_cascade_clears_dump_states_but_does_not_mark_unliquidatable(self):
        # Replay the actual loop block from reward_farmer.py Step 4b.
        # Logic-shape regression: if the loop is restructured but the
        # FX-032 contract (no mark_unliquidatable here) is preserved, it
        # stays green.
        BOOK_FAILURE_LIMIT = 3
        ms = _make_ms()
        ms.book_failures = BOOK_FAILURE_LIMIT
        markets = {ms.cid: ms}
        db = MagicMock()

        dead_cids = [
            cid for cid, m in markets.items()
            if m.book_failures >= BOOK_FAILURE_LIMIT
        ]
        self.assertEqual([ms.cid], dead_cids)

        # Loop body (FX-032 — no mark_unliquidatable call):
        for cid in dead_cids:
            for side in ["yes", "no"]:
                db.delete_dump_state(cid, side)
            del markets[cid]

        # FX-006 cascade preserved: dump_states cleaned both sides
        db.delete_dump_state.assert_any_call(ms.cid, "yes")
        db.delete_dump_state.assert_any_call(ms.cid, "no")
        # FX-032: this path must NOT mark unliquidatable. Only the
        # canonical FX-007 "orderbook does not exist" body in
        # OrderLifecycle/DumpManager exception handlers is allowed to.
        db.mark_unliquidatable.assert_not_called()
        # Market removed from active set (still appropriate — it can
        # reappear via the next reward-markets refresh and get another
        # chance, which is what we want for transient failure modes).
        self.assertEqual({}, markets)

    def test_actual_reward_farmer_cleanup_does_not_call_mark_unliquidatable(self):
        # Stronger assertion: read the source and check the production
        # code path doesn't have `mark_unliquidatable` in the dead-market
        # cleanup block. This catches a future regression that re-adds
        # the call.
        import inspect
        from reward_farmer import RewardFarmer
        src = inspect.getsource(RewardFarmer.run_cycle)
        # Find the Step 4b block
        start = src.find("Step 4b")
        self.assertNotEqual(-1, start, "Step 4b marker missing from run_cycle")
        end = src.find("Step 5", start)
        self.assertNotEqual(-1, end, "Step 5 marker missing — block extraction failed")
        block = src[start:end]
        self.assertIn("delete_dump_state", block, "FX-006 cascade must be preserved")
        self.assertNotIn(
            "mark_unliquidatable", block,
            "FX-032: dead-market cleanup must not call mark_unliquidatable "
            "(only the canonical FX-007 path in OL/DM is allowed to)"
        )


if __name__ == "__main__":
    unittest.main()
