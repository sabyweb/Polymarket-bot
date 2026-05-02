"""Tests for oversight/data_collector.py — query logic, q_share priors."""

import sys
import os
import sqlite3
import tempfile
import time
import json
import unittest

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import RF_NEW_MARKET_Q_SHARE_PRIOR, RF_POISONED_Q_SHARE_THRESHOLD
from oversight.data_collector import query_reward_stats, _fetch_reward_market_expiries


class TestQueryRewardStatsPrior(unittest.TestCase):
    """Test the cold-start q_share prior in query_reward_stats."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        db = sqlite3.connect(self.db_path)
        # Minimal schema — just the tables query_reward_stats touches.
        db.execute("""
            CREATE TABLE reward_market_stats (
                condition_id TEXT PRIMARY KEY,
                data         TEXT NOT NULL,
                updated_at   REAL NOT NULL DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE scoring_snapshots (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL NOT NULL,
                order_id     TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                side         TEXT NOT NULL,
                scoring      INTEGER NOT NULL,
                price        REAL NOT NULL DEFAULT 0,
                shares       REAL NOT NULL DEFAULT 0
            )
        """)
        db.commit()
        self.db = db

    def tearDown(self):
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _insert_market_stat(self, cid: str, total_q: float = 0.0,
                            market_q: float = 0.0, samples: int = 0,
                            time_on_book_secs: float = 0.0, daily_rate: float = 50.0,
                            add_recent_snapshot: bool = True):
        """Helper: insert a reward_market_stats row with the given q-score fields.

        By default also inserts a recent scoring_snapshots row so the staleness
        gate (last-seen > 6h → q_share=0) doesn't trip. Set add_recent_snapshot
        to False to simulate a stale/silent market.
        """
        data = {
            "condition_id": cid,
            "question": f"Question for {cid}?",
            "daily_rate": daily_rate,
            "time_on_book_secs": time_on_book_secs,
            "total_q_score": total_q,
            "total_market_q": market_q,
            "q_score_samples": samples,
            "buy_fills": 0,
            "cycles_with_orders": 0,
            "total_cycles": 0,
            "avg_bid_price": 0.0,
            "avg_ask_price": 0.0,
            "adverse_fills": 0,
            "spread_capture_usd": 0.0,
            "cycles_in_reward_window": 0,
            "cycles_both_in_window": 0,
        }
        self.db.execute(
            "INSERT INTO reward_market_stats (condition_id, data, updated_at) VALUES (?, ?, ?)",
            (cid, json.dumps(data), time.time()),
        )
        if add_recent_snapshot:
            # Recent (now) snapshot so last-seen check passes.
            self.db.execute(
                "INSERT INTO scoring_snapshots (ts, order_id, condition_id, side, scoring) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), f"oid_{cid}", cid, "yes", 0),
            )
        self.db.commit()

    def test_prior_applied_to_cold_start_markets(self):
        """Market in reward_market_stats with zero samples and on_book < 2h
        gets the prior q_share, not 0.0."""
        self._insert_market_stat(
            "0xcold_start",
            total_q=0.0, market_q=0.0, samples=0,
            time_on_book_secs=1800,  # 0.5h — below 2h threshold
        )
        result = query_reward_stats(self.db_path)
        self.assertIn("0xcold_start", result)
        self.assertAlmostEqual(result["0xcold_start"]["q_share"], RF_NEW_MARKET_Q_SHARE_PRIOR)

    def test_prior_does_not_override_cumulative_data(self):
        """Market with cumulative scoring data (total_q_score > 0, samples > 0)
        uses the cumulative ratio, NOT the prior."""
        # total_q=300, market_q=1000 → observed q_share = 0.3 (NOT the prior 0.10)
        self._insert_market_stat(
            "0xobserved",
            total_q=300.0, market_q=1000.0, samples=100,
            time_on_book_secs=7200,  # 2h
        )
        result = query_reward_stats(self.db_path)
        self.assertIn("0xobserved", result)
        self.assertAlmostEqual(result["0xobserved"]["q_share"], 0.3)
        self.assertNotEqual(result["0xobserved"]["q_share"], RF_NEW_MARKET_Q_SHARE_PRIOR)

    def test_prior_skipped_for_long_on_book_with_no_samples(self):
        """Market that has been on-book > 2h but still has zero samples falls
        through to 0.0 (not prior) — this is a diagnostic signal that
        something is wrong with scoring, not a cold-start case."""
        self._insert_market_stat(
            "0xstale_watcher",
            total_q=0.0, market_q=0.0, samples=0,
            time_on_book_secs=10800,  # 3h — above 2h threshold
        )
        result = query_reward_stats(self.db_path)
        self.assertIn("0xstale_watcher", result)
        self.assertEqual(result["0xstale_watcher"]["q_share"], 0.0)


class TestMarketExpiryCacheGameStartTime(unittest.TestCase):
    """Test that market_expiry_cache stores and returns game_start_time."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        db = sqlite3.connect(self.db_path)
        # Schema must match the post-migration shape.
        db.execute("""
            CREATE TABLE market_expiry_cache (
                condition_id    TEXT PRIMARY KEY,
                end_date_iso    TEXT NOT NULL,
                game_start_time TEXT NOT NULL DEFAULT '',
                question        TEXT NOT NULL DEFAULT '',
                fetched_at      REAL NOT NULL
            )
        """)
        db.commit()
        self.db = db

    def tearDown(self):
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_cache_round_trip_preserves_game_start_time(self):
        """A cached row with a non-empty game_start_time is returned
        via _fetch_reward_market_expiries under the new dict shape."""
        self.db.execute(
            "INSERT INTO market_expiry_cache "
            "(condition_id, end_date_iso, game_start_time, fetched_at) VALUES (?, ?, ?, ?)",
            ("0xcid_sports", "2026-05-01T00:00:00Z", "2026-04-28T18:30:00Z", time.time()),
        )
        self.db.commit()
        result = _fetch_reward_market_expiries(
            condition_ids=["0xcid_sports"], db_path=self.db_path
        )
        self.assertIn("0xcid_sports", result)
        self.assertEqual(result["0xcid_sports"]["end_date_iso"], "2026-05-01T00:00:00Z")
        self.assertEqual(result["0xcid_sports"]["game_start_time"], "2026-04-28T18:30:00Z")

    def test_cache_handles_empty_game_start_time(self):
        """A cached row with empty game_start_time (non-sports market) returns
        the empty string cleanly — not None or missing key."""
        self.db.execute(
            "INSERT INTO market_expiry_cache "
            "(condition_id, end_date_iso, game_start_time, fetched_at) VALUES (?, ?, ?, ?)",
            ("0xcid_politics", "2026-12-31T23:59:59Z", "", time.time()),
        )
        self.db.commit()
        result = _fetch_reward_market_expiries(
            condition_ids=["0xcid_politics"], db_path=self.db_path
        )
        self.assertIn("0xcid_politics", result)
        self.assertEqual(result["0xcid_politics"]["end_date_iso"], "2026-12-31T23:59:59Z")
        self.assertEqual(result["0xcid_politics"]["game_start_time"], "")

    def test_fetch_returns_dict_of_dicts_shape(self):
        """The function now returns dict[cid, dict[str, str]] with
        'end_date_iso', 'game_start_time', and 'question' keys always present."""
        self.db.execute(
            "INSERT INTO market_expiry_cache "
            "(condition_id, end_date_iso, game_start_time, fetched_at) VALUES (?, ?, ?, ?)",
            ("0xcid_a", "2026-06-01T00:00:00Z", "2026-05-30T12:00:00Z", time.time()),
        )
        self.db.commit()
        result = _fetch_reward_market_expiries(
            condition_ids=["0xcid_a"], db_path=self.db_path
        )
        self.assertIsInstance(result, dict)
        self.assertIsInstance(result["0xcid_a"], dict)
        self.assertIn("end_date_iso", result["0xcid_a"])
        self.assertIn("game_start_time", result["0xcid_a"])
        self.assertIn("question", result["0xcid_a"])

    def test_cache_round_trip_preserves_question(self):
        """A cached row with non-empty question text is returned via
        _fetch_reward_market_expiries. Gates safety controls that depend on
        m.question (sports detection, per-group cluster cap, keyword filters)."""
        self.db.execute(
            "INSERT INTO market_expiry_cache "
            "(condition_id, end_date_iso, game_start_time, question, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("0xcid_q", "2026-05-15T00:00:00Z", "",
             "Will Lakers beat Warriors on May 15?", time.time()),
        )
        self.db.commit()
        result = _fetch_reward_market_expiries(
            condition_ids=["0xcid_q"], db_path=self.db_path
        )
        self.assertIn("0xcid_q", result)
        self.assertEqual(
            result["0xcid_q"]["question"],
            "Will Lakers beat Warriors on May 15?",
        )

    def test_cache_handles_empty_question(self):
        """A cached row with empty question (legacy pre-fix-3 row) returns
        the empty string cleanly — not None or missing key."""
        self.db.execute(
            "INSERT INTO market_expiry_cache "
            "(condition_id, end_date_iso, fetched_at) VALUES (?, ?, ?)",
            ("0xcid_legacy", "2026-12-31T23:59:59Z", time.time()),
        )
        self.db.commit()
        result = _fetch_reward_market_expiries(
            condition_ids=["0xcid_legacy"], db_path=self.db_path
        )
        self.assertIn("0xcid_legacy", result)
        self.assertEqual(result["0xcid_legacy"]["question"], "")


class TestPoisonedRowHeuristic(unittest.TestCase):
    """Verify the Fix 3 read-time guard that skips rows with cumulative
    q_share above RF_POISONED_Q_SHARE_THRESHOLD and falls through to the
    cold-start prior instead."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        db = sqlite3.connect(self.db_path)
        db.execute("""
            CREATE TABLE reward_market_stats (
                condition_id TEXT PRIMARY KEY,
                data         TEXT NOT NULL,
                updated_at   REAL NOT NULL DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE scoring_snapshots (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL NOT NULL,
                order_id     TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                side         TEXT NOT NULL,
                scoring      INTEGER NOT NULL,
                price        REAL NOT NULL DEFAULT 0,
                shares       REAL NOT NULL DEFAULT 0
            )
        """)
        db.commit()
        self.db = db

    def tearDown(self):
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _insert_stat_row(self, cid: str, total_q: float, market_q: float,
                         samples: int, time_on_book_secs: float = 7200):
        """Insert a reward_market_stats row and a recent scoring_snapshot
        to prevent staleness filtering."""
        data = {
            "condition_id": cid, "question": f"Q for {cid}?",
            "daily_rate": 50.0, "time_on_book_secs": time_on_book_secs,
            "total_q_score": total_q, "total_market_q": market_q,
            "q_score_samples": samples,
            "buy_fills": 0, "cycles_with_orders": 0, "total_cycles": 0,
            "avg_bid_price": 0.0, "avg_ask_price": 0.0, "adverse_fills": 0,
            "spread_capture_usd": 0.0, "cycles_in_reward_window": 0,
            "cycles_both_in_window": 0,
        }
        self.db.execute(
            "INSERT INTO reward_market_stats (condition_id, data, updated_at) VALUES (?, ?, ?)",
            (cid, json.dumps(data), time.time()),
        )
        # Recent snapshot so the >6h / >24h stale gates don't fire.
        self.db.execute(
            "INSERT INTO scoring_snapshots (ts, order_id, condition_id, side, scoring) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), f"oid_{cid}", cid, "yes", 0),
        )
        self.db.commit()

    def test_poisoned_row_falls_through_to_prior(self):
        """Row with total_q_score == total_market_q (ratio=1.0) is
        treated as poisoned; q_share becomes the cold-start prior."""
        self._insert_stat_row("0xpoisoned", total_q=1000.0, market_q=1000.0, samples=50)
        result = query_reward_stats(self.db_path)
        self.assertIn("0xpoisoned", result)
        self.assertAlmostEqual(result["0xpoisoned"]["q_share"],
                               RF_NEW_MARKET_Q_SHARE_PRIOR)

    def test_normal_row_uses_cumulative_ratio(self):
        """Row with realistic ratio (e.g. 0.02) uses Priority 2 cumulative."""
        self._insert_stat_row("0xhealthy", total_q=100.0, market_q=5000.0, samples=100)
        result = query_reward_stats(self.db_path)
        self.assertIn("0xhealthy", result)
        self.assertAlmostEqual(result["0xhealthy"]["q_share"], 0.02)
        self.assertNotAlmostEqual(result["0xhealthy"]["q_share"],
                                  RF_NEW_MARKET_Q_SHARE_PRIOR)

    def test_ratio_just_above_threshold_is_poisoned(self):
        """Ratio of 0.51 (just above the 0.5 threshold) → treated as poisoned."""
        self._insert_stat_row("0xedge_hi", total_q=51.0, market_q=100.0, samples=20)
        result = query_reward_stats(self.db_path)
        self.assertAlmostEqual(result["0xedge_hi"]["q_share"],
                               RF_NEW_MARKET_Q_SHARE_PRIOR)

    def test_ratio_at_threshold_is_not_poisoned(self):
        """Ratio of exactly 0.5 → NOT poisoned (> comparison, not >=).
        Note: on_book > 4h and q_share > 0.5 exactly triggers the
        cumulative_capped branch which caps to 0.5 — but we have exactly
        0.5 here, which fails the `q_share > 0.5` inner check, so it stays
        at 0.5 uncapped."""
        self._insert_stat_row("0xedge_eq", total_q=50.0, market_q=100.0, samples=20)
        result = query_reward_stats(self.db_path)
        self.assertAlmostEqual(result["0xedge_eq"]["q_share"], 0.5)


if __name__ == "__main__":
    unittest.main()
