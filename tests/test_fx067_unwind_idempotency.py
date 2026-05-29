"""FX-067 — log_unwind must be truthful + idempotent.

The realized loss/profit written by log_unwind is the SOLE input to the
24h-realized-loss kill switch (both the farmer's and the oversight's).
Pre-FX-067, log_unwind swallowed write exceptions at log.debug and returned
None — a dropped row silently removed a loss from the kill math (the
2026-05-25 "8 SELLs on-chain, 1 unwinds row" failure mode). It was also
non-idempotent: a restart between the unwind write and the dump-state clear
could double-log the same loss.

FX-067 mirrors FX-054's log_fill hardening: optional unwind_event_id +
partial unique index + INSERT OR IGNORE + truthful bool return + WARNING on
DB error.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import BotDatabase  # noqa: E402


class TestFX067UnwindIdempotency(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "t.db")
        self.db = BotDatabase(self.db_path)

    def tearDown(self):
        for f in os.listdir(self.tmpdir):
            os.unlink(os.path.join(self.tmpdir, f))

    def _count(self, **where) -> int:
        sql = "SELECT COUNT(*) FROM unwinds"
        params = ()
        if where:
            clause = " AND ".join(f"{k} = ?" for k in where)
            sql += " WHERE " + clause
            params = tuple(where.values())
        return self.db._get_conn().execute(sql, params).fetchone()[0]

    def test_new_unwind_returns_true_and_inserts(self):
        ok = self.db.log_unwind(
            condition_id="0xabc", question="q", side="yes",
            shares=50, sell_price=0.78, usd_value=38.66, vwap_cost=40.0,
            unwind_event_id="unwind:0xabc:yes:oid1",
        )
        self.assertTrue(ok)
        self.assertEqual(1, self._count(condition_id="0xabc"))

    def test_duplicate_event_id_ignored(self):
        """Same unwind_event_id twice → second returns False, only 1 row.
        This is the restart-double-log guard."""
        kw = dict(condition_id="0xabc", question="q", side="yes",
                  shares=50, sell_price=0.78, usd_value=38.66, vwap_cost=40.0,
                  unwind_event_id="unwind:0xabc:yes:oid1")
        first = self.db.log_unwind(**kw)
        second = self.db.log_unwind(**kw)
        self.assertTrue(first)
        self.assertFalse(second, "duplicate event_id must collide → False")
        self.assertEqual(1, self._count(condition_id="0xabc"))

    def test_distinct_event_ids_both_insert(self):
        self.assertTrue(self.db.log_unwind(
            condition_id="0xabc", question="q", side="yes", shares=10,
            sell_price=0.5, usd_value=5.0, unwind_event_id="u1"))
        self.assertTrue(self.db.log_unwind(
            condition_id="0xabc", question="q", side="yes", shares=10,
            sell_price=0.5, usd_value=5.0, unwind_event_id="u2"))
        self.assertEqual(2, self._count(condition_id="0xabc"))

    def test_empty_event_id_is_append_only(self):
        """Legacy/merge callers pass no event_id → both insert (partial index
        only constrains non-empty values)."""
        self.assertTrue(self.db.log_unwind(
            condition_id="0xmerge", question="q", side="merge", shares=5,
            sell_price=1.0, usd_value=5.0))
        self.assertTrue(self.db.log_unwind(
            condition_id="0xmerge", question="q", side="merge", shares=5,
            sell_price=1.0, usd_value=5.0))
        self.assertEqual(2, self._count(condition_id="0xmerge"))

    def test_pnl_computed_and_negative_loss_visible(self):
        """pnl = usd_value - vwap_cost, and a real loss stays negative (so the
        kill's WHERE pnl<0 sees it)."""
        self.db.log_unwind(
            condition_id="0xloss", question="q", side="yes",
            shares=50, sell_price=0.78, usd_value=38.66, vwap_cost=40.0,
            unwind_event_id="loss1")
        row = self.db._get_conn().execute(
            "SELECT pnl FROM unwinds WHERE condition_id = ?", ("0xloss",)
        ).fetchone()
        self.assertAlmostEqual(38.66 - 40.0, row[0], places=4)
        self.assertLess(row[0], 0, "a real loss must record negative pnl")

    def test_returns_bool_not_none(self):
        """Return type is bool (was None pre-FX-067) so callers can react."""
        r = self.db.log_unwind(
            condition_id="0xabc", question="q", side="yes", shares=1,
            sell_price=0.5, usd_value=0.5, unwind_event_id="b1")
        self.assertIsInstance(r, bool)


if __name__ == "__main__":
    unittest.main()
