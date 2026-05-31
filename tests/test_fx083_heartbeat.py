"""FX-083 — production heartbeat / stall alert.

Before FX-083 a hung or dead production process paged nobody: the only
heartbeat sender (alerts.alert_heartbeat_failure) was wired into the legacy
bot.py path, never the production farmer/oversight. FX-083 has each process
write a liveness heartbeat to reward_tracker_state every cycle (mode-independent)
and page its PEER (via alerts.maybe_alert_stale_heartbeat) when stale, deduped.

These tests cover the DB round-trip and the full staleness/dedup/fail-open
contract of the checker. The cross-process wiring (farmer checks oversight,
oversight checks farmer) is thin glue over these two verified primitives.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alerts  # noqa: E402
from database import BotDatabase  # noqa: E402


class TestHeartbeatDB(unittest.TestCase):
    """record_heartbeat / get_heartbeat round-trip on the real schema."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.db = BotDatabase(self.db_path)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_roundtrip(self):
        t = 1_700_000_000.0
        self.assertTrue(self.db.record_heartbeat("farmer", t))
        self.assertEqual(self.db.get_heartbeat("farmer"), t)

    def test_missing_returns_none(self):
        # Never written → None so the caller fails OPEN (no false stale-page).
        self.assertIsNone(self.db.get_heartbeat("oversight"))

    def test_overwrite_advances(self):
        self.db.record_heartbeat("oversight", 100.0)
        self.db.record_heartbeat("oversight", 200.0)
        self.assertEqual(self.db.get_heartbeat("oversight"), 200.0)

    def test_default_ts_is_now(self):
        before = time.time()
        self.db.record_heartbeat("farmer")
        got = self.db.get_heartbeat("farmer")
        self.assertIsNotNone(got)
        self.assertGreaterEqual(got, before)

    def test_processes_are_independent(self):
        self.db.record_heartbeat("farmer", 111.0)
        self.db.record_heartbeat("oversight", 222.0)
        self.assertEqual(self.db.get_heartbeat("farmer"), 111.0)
        self.assertEqual(self.db.get_heartbeat("oversight"), 222.0)


class TestMaybeAlertStaleHeartbeat(unittest.TestCase):
    """alerts.maybe_alert_stale_heartbeat — staleness, dedup, fail-open."""

    def setUp(self):
        # Isolate the module-level dedup state between tests.
        alerts._HB_LAST_ALERT.clear()
        self._patcher = patch.object(alerts, "alert_heartbeat_failure")
        self.mock_alert = self._patcher.start()
        self.now = 1_000_000.0
        self.stale = 3600.0  # 1h

    def tearDown(self):
        self._patcher.stop()
        alerts._HB_LAST_ALERT.clear()

    def test_none_ts_is_failopen(self):
        # Unknown peer state (fresh deploy / read error) must NOT page.
        self.assertFalse(
            alerts.maybe_alert_stale_heartbeat("oversight", None, self.now, self.stale)
        )
        self.mock_alert.assert_not_called()

    def test_nonpositive_ts_is_failopen(self):
        self.assertFalse(
            alerts.maybe_alert_stale_heartbeat("oversight", 0.0, self.now, self.stale)
        )
        self.mock_alert.assert_not_called()

    def test_fresh_does_not_page(self):
        fresh = self.now - 60.0  # 1 min ago, well under 1h
        self.assertFalse(
            alerts.maybe_alert_stale_heartbeat("oversight", fresh, self.now, self.stale)
        )
        self.mock_alert.assert_not_called()

    def test_stale_pages_once(self):
        stale_ts = self.now - 7200.0  # 2h ago > 1h threshold
        self.assertTrue(
            alerts.maybe_alert_stale_heartbeat("oversight", stale_ts, self.now, self.stale)
        )
        self.mock_alert.assert_called_once()
        # The age handed to the pager is ~2h, labelled with the peer name.
        args, kwargs = self.mock_alert.call_args
        self.assertAlmostEqual(args[0], 7200.0, delta=1.0)
        self.assertEqual(kwargs.get("process"), "oversight")

    def test_repage_suppressed_within_window(self):
        stale_ts = self.now - 7200.0
        self.assertTrue(
            alerts.maybe_alert_stale_heartbeat("oversight", stale_ts, self.now, self.stale, repage_secs=1800.0)
        )
        # 10 min later, still stale → suppressed (< 30min repage window).
        self.assertFalse(
            alerts.maybe_alert_stale_heartbeat("oversight", stale_ts, self.now + 600.0, self.stale, repage_secs=1800.0)
        )
        self.assertEqual(self.mock_alert.call_count, 1)

    def test_repage_after_window(self):
        stale_ts = self.now - 7200.0
        alerts.maybe_alert_stale_heartbeat("oversight", stale_ts, self.now, self.stale, repage_secs=1800.0)
        # 31 min later, still stale → re-pages.
        self.assertTrue(
            alerts.maybe_alert_stale_heartbeat("oversight", stale_ts, self.now + 1860.0, self.stale, repage_secs=1800.0)
        )
        self.assertEqual(self.mock_alert.call_count, 2)

    def test_recovery_resets_dedup(self):
        stale_ts = self.now - 7200.0
        alerts.maybe_alert_stale_heartbeat("oversight", stale_ts, self.now, self.stale)
        self.assertEqual(self.mock_alert.call_count, 1)
        # Peer recovers (fresh heartbeat) → dedup state cleared.
        self.assertFalse(
            alerts.maybe_alert_stale_heartbeat("oversight", self.now + 10.0, self.now + 20.0, self.stale)
        )
        # New stall episode pages immediately despite being soon after.
        new_stale = (self.now + 20.0) - 7200.0
        self.assertTrue(
            alerts.maybe_alert_stale_heartbeat("oversight", new_stale, self.now + 30.0, self.stale)
        )
        self.assertEqual(self.mock_alert.call_count, 2)

    def test_disabled_when_threshold_zero(self):
        stale_ts = self.now - 99999.0
        self.assertFalse(
            alerts.maybe_alert_stale_heartbeat("oversight", stale_ts, self.now, 0.0)
        )
        self.mock_alert.assert_not_called()

    def test_peers_tracked_independently(self):
        stale_ts = self.now - 7200.0
        self.assertTrue(
            alerts.maybe_alert_stale_heartbeat("oversight", stale_ts, self.now, self.stale)
        )
        self.assertTrue(
            alerts.maybe_alert_stale_heartbeat("farmer", stale_ts, self.now, self.stale)
        )
        self.assertEqual(self.mock_alert.call_count, 2)


if __name__ == "__main__":
    unittest.main()
