"""B-5 — Position correction audit trail.

Every PositionStore.set_shares() and reset_side() that changes shares or cost
basis must queue a correction row and flush it atomically with the next _save().
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as _database_module
from database import BotDatabase
from state import PositionStore


class TestPositionCorrections(unittest.TestCase):
    """B-5: set_shares / reset_side emit durable correction rows."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        # PositionStore uses get_db() singleton. Save any existing instance and
        # inject our temp DB so corrections land where we can query them.
        self._saved_instance = _database_module._instance
        self.db = BotDatabase(self.db_path)
        _database_module._instance = self.db
        self.store = PositionStore()

    def tearDown(self):
        _database_module._instance = self._saved_instance
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_set_shares_emits_correction(self):
        self.store.register_market("0xcid1", "Test market")
        self.store.record_fill("0xcid1", "yes", 20.0, 0.48)
        # Now correct to a different share count and cost basis.
        self.store.set_shares("0xcid1", "yes", shares=10.0, avg_price=0.50)

        rows = self.db.get_position_corrections(condition_id="0xcid1")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["side"], "yes")
        self.assertEqual(row["old_shares"], 20.0)
        self.assertEqual(row["new_shares"], 10.0)
        self.assertEqual(row["old_avg_price"], 0.48)
        self.assertEqual(row["new_avg_price"], 0.50)
        self.assertEqual(row["reason"], "set_shares")

    def test_set_shares_no_change_does_not_emit_correction(self):
        self.store.register_market("0xcid1", "Test market")
        self.store.record_fill("0xcid1", "yes", 10.0, 0.50)
        # Call set_shares with identical values.
        self.store.set_shares("0xcid1", "yes", shares=10.0, avg_price=0.50)
        rows = self.db.get_position_corrections(condition_id="0xcid1")
        self.assertEqual(len(rows), 0)

    def test_reset_side_emits_correction(self):
        self.store.register_market("0xcid1", "Test market")
        self.store.record_fill("0xcid1", "yes", 15.0, 0.50)
        self.store.reset_side("0xcid1", "yes")

        rows = self.db.get_position_corrections(condition_id="0xcid1")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["side"], "yes")
        self.assertEqual(row["old_shares"], 15.0)
        self.assertEqual(row["new_shares"], 0.0)
        self.assertEqual(row["old_avg_price"], 0.50)
        self.assertEqual(row["new_avg_price"], 0.0)
        self.assertEqual(row["reason"], "reset_side")

    def test_reset_side_zero_position_is_silent(self):
        self.store.register_market("0xcid1", "Test market")
        # No prior position; reset should not emit a correction.
        self.store.reset_side("0xcid1", "yes")
        rows = self.db.get_position_corrections(condition_id="0xcid1")
        self.assertEqual(len(rows), 0)

    def test_corrections_clear_after_successful_save(self):
        self.store.register_market("0xcid1", "Test market")
        self.store.record_fill("0xcid1", "yes", 10.0, 0.50)
        self.store.set_shares("0xcid1", "yes", shares=5.0, avg_price=0.50)
        self.assertEqual(len(self.store._pending_corrections), 0)

    def test_multiple_corrections_persist(self):
        self.store.register_market("0xcid1", "Test market")
        self.store.record_fill("0xcid1", "yes", 20.0, 0.48)
        self.store.set_shares("0xcid1", "yes", shares=15.0, avg_price=0.49)
        self.store.set_shares("0xcid1", "yes", shares=10.0, avg_price=0.50)
        rows = self.db.get_position_corrections(condition_id="0xcid1")
        self.assertEqual(len(rows), 2)
        # Most recent first because of ORDER BY ts DESC.
        self.assertEqual(rows[0]["new_shares"], 10.0)
        self.assertEqual(rows[1]["new_shares"], 15.0)


if __name__ == "__main__":
    unittest.main()
