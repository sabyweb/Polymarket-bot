"""Tests for database persistence: dump_states, active_orders, market_performance."""

import sys
import os
import sqlite3
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import BotDatabase


class TestDumpStatePersistence(unittest.TestCase):
    """Test dump state save/load/delete for crash recovery."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.db = BotDatabase(self.db_path)

    def tearDown(self):
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_save_and_load(self):
        state = {
            "fill_price": 0.85,
            "started_at": time.time(),
            "shares": 50.0,
            "tid": "token123",
            "dump_order_id": "order456",
            "last_passive_reprice": 0,
        }
        self.db.save_dump_state("0xcid1", "yes", state)

        loaded = self.db.load_all_dump_states()
        self.assertIn(("0xcid1", "yes"), loaded)
        self.assertAlmostEqual(loaded[("0xcid1", "yes")]["fill_price"], 0.85)
        self.assertEqual(loaded[("0xcid1", "yes")]["tid"], "token123")

    def test_delete(self):
        state = {"fill_price": 0.5, "started_at": time.time(), "shares": 50, "tid": "t1"}
        self.db.save_dump_state("0xcid1", "yes", state)
        self.db.delete_dump_state("0xcid1", "yes")

        loaded = self.db.load_all_dump_states()
        self.assertNotIn(("0xcid1", "yes"), loaded)

    def test_upsert_overwrites(self):
        """Saving same (cid, side) twice should overwrite."""
        state1 = {"fill_price": 0.5, "started_at": time.time(), "shares": 50, "tid": "t1"}
        state2 = {"fill_price": 0.9, "started_at": time.time(), "shares": 30, "tid": "t2"}
        self.db.save_dump_state("0xcid1", "yes", state1)
        self.db.save_dump_state("0xcid1", "yes", state2)

        loaded = self.db.load_all_dump_states()
        self.assertAlmostEqual(loaded[("0xcid1", "yes")]["fill_price"], 0.9)

    def test_multiple_sides(self):
        """Can track both YES and NO dumps for same market."""
        self.db.save_dump_state("0xcid1", "yes", {
            "fill_price": 0.8, "started_at": time.time(), "shares": 50, "tid": "ty",
        })
        self.db.save_dump_state("0xcid1", "no", {
            "fill_price": 0.2, "started_at": time.time(), "shares": 50, "tid": "tn",
        })

        loaded = self.db.load_all_dump_states()
        self.assertEqual(len(loaded), 2)


class TestActiveOrderPersistence(unittest.TestCase):
    """Test active order tracking for crash recovery."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.db = BotDatabase(self.db_path)

    def tearDown(self):
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_save_and_load(self):
        self.db.save_active_order("oid1", "0xcid1", "yes", "buy", 0.45, 50)
        orders = self.db.load_active_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["order_id"], "oid1")
        self.assertEqual(orders[0]["side"], "yes")

    def test_delete(self):
        self.db.save_active_order("oid1", "0xcid1", "yes", "buy", 0.45, 50)
        self.db.delete_active_order("oid1")
        orders = self.db.load_active_orders()
        self.assertEqual(len(orders), 0)

    def test_clear_all(self):
        self.db.save_active_order("oid1", "0xcid1", "yes", "buy", 0.45, 50)
        self.db.save_active_order("oid2", "0xcid2", "no", "dump_sell", 0.80, 50)
        self.db.clear_all_active_orders()
        orders = self.db.load_active_orders()
        self.assertEqual(len(orders), 0)

    def test_dump_sell_type(self):
        self.db.save_active_order("oid1", "0xcid1", "yes", "dump_sell", 0.80, 50)
        orders = self.db.load_active_orders()
        self.assertEqual(orders[0]["order_type"], "dump_sell")


class TestMarketPerformance(unittest.TestCase):
    """Test performance snapshot persistence."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.db = BotDatabase(self.db_path)

    def tearDown(self):
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_save_single(self):
        self.db.save_performance_snapshot({
            "ts": time.time(),
            "condition_id": "0xtest",
            "question": "Test?",
            "net_score": 10.0,
            "action": "deploy",
            "correction_factor": 0.1,
        })
        history = self.db.get_market_performance_history("0xtest")
        self.assertEqual(len(history), 1)

    def test_save_batch(self):
        snapshots = [
            {"ts": time.time(), "condition_id": f"0x{i}", "net_score": float(i)}
            for i in range(10)
        ]
        self.db.save_performance_batch(snapshots)
        for i in range(10):
            history = self.db.get_market_performance_history(f"0x{i}")
            self.assertEqual(len(history), 1)

    def test_performance_summary(self):
        for i in range(5):
            self.db.save_performance_snapshot({
                "ts": time.time(),
                "condition_id": f"0x{i}",
                "action": "deploy" if i < 3 else "avoid",
                "net_score": 10.0 - i,
                "correction_factor": 0.1,
                "fill_cost": float(i),
                "dump_revenue": float(i) * 0.8,
            })
        summary = self.db.get_performance_summary()
        self.assertEqual(summary["unique_markets"], 5)
        self.assertEqual(summary["deploy_decisions"], 3)
        self.assertEqual(summary["avoid_decisions"], 2)

    def test_history_respects_days_filter(self):
        """Old snapshots should be excluded."""
        self.db.save_performance_snapshot({
            "ts": time.time() - 10 * 86400,  # 10 days ago
            "condition_id": "0xold",
            "net_score": 5.0,
        })
        history = self.db.get_market_performance_history("0xold", days=7)
        self.assertEqual(len(history), 0)


class TestMarketExpiryCacheMigration(unittest.TestCase):
    """Test that the game_start_time column migration runs on old schemas."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_migration_adds_game_start_time_column(self):
        """Simulate an older DB missing the game_start_time column, then
        run BotDatabase init (which invokes _migrate_enrichment_columns)
        and verify the column has been added."""
        # Create the old schema manually (without game_start_time).
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE market_expiry_cache (
                condition_id TEXT PRIMARY KEY,
                end_date_iso TEXT NOT NULL,
                fetched_at   REAL NOT NULL
            )
        """)
        conn.commit()
        # Confirm the column is NOT present pre-migration.
        cols_before = {row[1] for row in conn.execute("PRAGMA table_info(market_expiry_cache)")}
        conn.close()
        self.assertNotIn("game_start_time", cols_before)

        # Instantiate BotDatabase — this triggers _migrate_enrichment_columns.
        db = BotDatabase(self.db_path)
        try:
            # Verify the column is now present.
            conn = sqlite3.connect(self.db_path)
            cols_after = {row[1] for row in conn.execute("PRAGMA table_info(market_expiry_cache)")}
            conn.close()
            self.assertIn("game_start_time", cols_after)
        finally:
            db.close()

    def test_migration_adds_question_column(self):
        """Simulate an older DB missing the question column, then run
        BotDatabase init and verify the column has been added with the
        correct default ('')."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE market_expiry_cache (
                condition_id    TEXT PRIMARY KEY,
                end_date_iso    TEXT NOT NULL,
                game_start_time TEXT NOT NULL DEFAULT '',
                fetched_at      REAL NOT NULL
            )
        """)
        conn.commit()
        cols_before = {row[1] for row in conn.execute("PRAGMA table_info(market_expiry_cache)")}
        conn.close()
        self.assertNotIn("question", cols_before)

        db = BotDatabase(self.db_path)
        try:
            conn = sqlite3.connect(self.db_path)
            cols_after = {row[1] for row in conn.execute("PRAGMA table_info(market_expiry_cache)")}
            conn.close()
            self.assertIn("question", cols_after)
        finally:
            db.close()


class TestRollbackQuietFX080(unittest.TestCase):
    """FX-080: a failed write must roll back so it never leaves an OPEN
    transaction wedging the shared thread-local connection. An un-rolled-back
    transaction holds the WAL write lock -> 'database is locked' for every other
    writer + stalled checkpoint (the oversight wedge that froze
    wallet_reconcile_history and made the ROI cache upsert fail every cycle)."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.db = BotDatabase(self.db_path)

    def tearDown(self):
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_rollback_quiet_clears_wedge_and_connection_recovers(self):
        conn = self.db._get_conn()
        # Simulate a write whose commit failed without rollback: an open
        # transaction left behind (the wedge that held the WAL write lock).
        conn.execute(
            "INSERT INTO reward_tracker_state (key, value) VALUES ('wedge', '1')"
        )
        self.assertTrue(conn.in_transaction)

        self.db._rollback_quiet()

        # Wedge cleared: no open transaction, and the speculative row is gone.
        self.assertFalse(conn.in_transaction)
        n = conn.execute(
            "SELECT COUNT(*) FROM reward_tracker_state WHERE key='wedge'"
        ).fetchone()[0]
        self.assertEqual(n, 0)

        # Connection usable again — a normal write commits cleanly.
        self.db.save_usdc_balance(123.45)
        bal, _ = self.db.load_usdc_balance()
        self.assertEqual(bal, 123.45)

    def test_rollback_quiet_is_noop_when_clean(self):
        # No open transaction -> must not raise, must not change state.
        self.assertFalse(self.db._get_conn().in_transaction)
        self.db._rollback_quiet()  # no-op
        self.assertFalse(self.db._get_conn().in_transaction)


class TestFillStormMarkerPersist(unittest.TestCase):
    """The cross-market fill-storm breaker writes a __FILL_STORM__ audit row to
    the fills table (reward_farmer.run_cycle). Pre-fix the call site used a
    nonexistent self.db.execute_sql() swallowed by a bare except, so the storm
    HALT still worked but the row was NEVER persisted — no audit trail for the
    oversight/agent. The typed log_fill_storm_marker writer must actually land
    the row."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.db = BotDatabase(self.db_path)

    def tearDown(self):
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_marker_row_persists_with_expected_sentinel_values(self):
        ts = time.time()
        self.assertTrue(self.db.log_fill_storm_marker(ts))
        row = self.db._get_conn().execute(
            "SELECT ts, condition_id, side, fill_type, shares, price, clob_cost, usd_value "
            "FROM fills WHERE condition_id = '__FILL_STORM__' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row, "storm marker must be persisted to fills")
        self.assertAlmostEqual(row["ts"], ts, places=3)
        self.assertEqual(row["condition_id"], "__FILL_STORM__")
        self.assertEqual(row["side"], "both")
        self.assertEqual(row["fill_type"], "STORM_ALERT")
        # zeros keep the marker out of pnl / share / count aggregations
        self.assertEqual((row["shares"], row["price"], row["clob_cost"], row["usd_value"]),
                         (0, 0, 0, 0))

    def test_marker_defaults_ts_when_omitted(self):
        before = time.time()
        self.assertTrue(self.db.log_fill_storm_marker())
        after = time.time()
        ts = self.db._get_conn().execute(
            "SELECT ts FROM fills WHERE condition_id='__FILL_STORM__' ORDER BY id DESC LIMIT 1"
        ).fetchone()["ts"]
        self.assertGreaterEqual(ts, before)
        self.assertLessEqual(ts, after + 1)

    def test_marker_does_not_pollute_per_market_fill_count(self):
        # A real market's per-cid fill count must not see the sentinel row.
        self.db.log_fill_storm_marker(time.time())
        n = self.db._get_conn().execute(
            "SELECT COUNT(*) FROM fills WHERE condition_id = ?", ("0xRealMarket",)
        ).fetchone()[0]
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
