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


if __name__ == "__main__":
    unittest.main()
