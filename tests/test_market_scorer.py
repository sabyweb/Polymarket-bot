"""Tests for oversight/market_scorer.py — the adaptive scoring engine."""

import sys
import os
import sqlite3
import tempfile
import time
import unittest

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oversight.data_collector import MarketMetrics
from oversight.market_scorer import (
    score_market, classify_market, rank_markets, load_historical_adjustments,
    ScoredMarket,
)


def _make_metric(**overrides) -> MarketMetrics:
    """Helper to create a MarketMetrics with sane defaults."""
    defaults = dict(
        condition_id="0xtest123",
        question="Test market?",
        daily_rate=50.0,
        actual_reward_total=0.0,
        fill_cost_recent=0.0,
        dump_revenue_recent=0.0,
        fill_count_recent=0,
        net_pnl_recent=0.0,
        current_position_usd=0.0,
        on_book_hours=24.0,
        q_share_pct=1.0,
    )
    defaults.update(overrides)
    return MarketMetrics(**defaults)


class TestScoreMarket(unittest.TestCase):
    """Test the scoring formula."""

    def test_zero_fill_100pct_qshare(self):
        """Perfect market: 100% Q-share, zero fills, should score highest."""
        m = _make_metric(daily_rate=50, q_share_pct=1.0)
        score = score_market(m, hours=24)
        # effective_daily = 50 * 1.0 = 50, + 50% bonus = 75
        self.assertAlmostEqual(score, 75.0)

    def test_zero_fill_low_qshare(self):
        """Low Q-share but zero fills: still positive."""
        m = _make_metric(daily_rate=100, q_share_pct=0.1)
        score = score_market(m, hours=24)
        # effective = 10, + 50% bonus = 15
        self.assertAlmostEqual(score, 15.0)

    def test_fills_reduce_score(self):
        """Fills should reduce score by fill_damage/day."""
        m = _make_metric(
            daily_rate=50, q_share_pct=1.0,
            fill_cost_recent=20.0, dump_revenue_recent=15.0,
            fill_count_recent=3,
        )
        score = score_market(m, hours=24)
        # effective=50, damage=(20-15)/1 = 5/day, no bonus (has fills)
        # score = 50 - 5 = 45
        self.assertAlmostEqual(score, 45.0)

    def test_negative_score_when_damage_exceeds_reward(self):
        """Fill damage > reward = negative score."""
        m = _make_metric(
            daily_rate=10, q_share_pct=0.5,
            fill_cost_recent=50.0, dump_revenue_recent=0.0,
            fill_count_recent=5,
        )
        score = score_market(m, hours=24)
        # effective = 5, damage = 50/day, score = 5 - 50 = -45
        self.assertAlmostEqual(score, -45.0)

    def test_correction_factor_scales_reward(self):
        """Correction factor < 1 should reduce the effective daily reward."""
        m = _make_metric(daily_rate=100, q_share_pct=1.0)
        score_no_correction = score_market(m, hours=24, correction_factor=1.0)
        score_with_correction = score_market(m, hours=24, correction_factor=0.1)
        # Without: 100 + 50% = 150
        # With 0.1: 10 + 50% = 15
        self.assertAlmostEqual(score_no_correction, 150.0)
        self.assertAlmostEqual(score_with_correction, 15.0)

    def test_correction_factor_does_not_affect_fill_damage(self):
        """Fill damage is real, should NOT be scaled by correction factor."""
        m = _make_metric(
            daily_rate=100, q_share_pct=1.0,
            fill_cost_recent=20.0, fill_count_recent=2,
        )
        score = score_market(m, hours=24, correction_factor=0.1)
        # corrected_daily = 10, damage = 20/day, score = 10 - 20 = -10
        self.assertAlmostEqual(score, -10.0)


class TestClassifyMarket(unittest.TestCase):
    """Test classification logic (deploy/avoid/trial)."""

    def test_zero_fills_always_deploy(self):
        """Zero fills with positive rate = always deploy."""
        m = _make_metric(daily_rate=50, q_share_pct=1.0)
        sm = classify_market(m, score=75.0)
        self.assertEqual(sm.action, "deploy")

    def test_positive_score_deploys(self):
        """Positive score with fills = deploy."""
        m = _make_metric(fill_count_recent=2, daily_rate=50, q_share_pct=1.0)
        sm = classify_market(m, score=10.0)
        self.assertEqual(sm.action, "deploy")

    def test_high_fills_negative_avoids(self):
        """3+ fills with damage > reward = avoid."""
        m = _make_metric(
            fill_count_recent=3,
            fill_cost_recent=100, dump_revenue_recent=0,
            actual_reward_total=50,
            daily_rate=50, q_share_pct=1.0,
        )
        sm = classify_market(m, score=-50.0)
        self.assertEqual(sm.action, "avoid")

    def test_new_market_trial(self):
        """New market with low confidence gets trial deployment."""
        m = _make_metric(daily_rate=20, q_share_pct=0, on_book_hours=1)
        sm = classify_market(m, score=-1.0)
        # Should deploy as trial if rate >= 5
        self.assertEqual(sm.action, "deploy")
        self.assertEqual(sm.confidence, "low")


class TestRankMarkets(unittest.TestCase):
    """Test ranking and cap logic."""

    def test_sorts_by_score_descending(self):
        metrics = [
            _make_metric(condition_id="low", daily_rate=10, q_share_pct=1.0),
            _make_metric(condition_id="high", daily_rate=100, q_share_pct=1.0),
            _make_metric(condition_id="mid", daily_rate=50, q_share_pct=1.0),
        ]
        scored = rank_markets(metrics, max_markets=10)
        self.assertEqual(scored[0].condition_id, "high")
        self.assertEqual(scored[1].condition_id, "mid")
        self.assertEqual(scored[2].condition_id, "low")

    def test_caps_at_max_markets(self):
        metrics = [
            _make_metric(condition_id=f"m{i}", daily_rate=100-i, q_share_pct=1.0)
            for i in range(10)
        ]
        scored = rank_markets(metrics, max_markets=3)
        deploy_count = sum(1 for s in scored if s.action == "deploy")
        self.assertEqual(deploy_count, 3)


class TestHistoricalAdjustments(unittest.TestCase):
    """Test the adaptive historical adjustment system."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        db = sqlite3.connect(self.db_path)
        db.execute("""CREATE TABLE market_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, condition_id TEXT, question TEXT,
            window_hours REAL, estimated_daily REAL,
            correction_factor REAL, corrected_daily REAL,
            fill_cost REAL, dump_revenue REAL,
            net_score REAL, action TEXT,
            q_share_pct REAL, on_book_hours REAL,
            fill_count INTEGER, shares_recommended INTEGER
        )""")
        db.commit()
        self.db = db

    def tearDown(self):
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _insert_snapshot(self, cid, fill_count=0, net_score=10.0, ts_offset=0):
        self.db.execute(
            """INSERT INTO market_performance
               (ts, condition_id, question, window_hours, estimated_daily,
                correction_factor, corrected_daily, fill_cost, dump_revenue,
                net_score, action, q_share_pct, on_book_hours, fill_count,
                shares_recommended)
               VALUES (?, ?, '', 24, 50, 0.1, 5, 0, 0, ?, 'deploy', 1.0, 24, ?, 50)""",
            (time.time() - ts_offset, cid, net_score, fill_count),
        )
        self.db.commit()

    def test_no_data_returns_empty(self):
        result = load_historical_adjustments(self.db_path)
        self.assertEqual(result, {})

    def test_needs_3_snapshots(self):
        """Markets with < 3 snapshots are not adjusted."""
        self._insert_snapshot("0xtest", fill_count=0)
        self._insert_snapshot("0xtest", fill_count=0)
        result = load_historical_adjustments(self.db_path)
        self.assertNotIn("0xtest", result)

    def test_zero_fill_market_gets_bonus(self):
        """Markets with 0% fill rate get trend_mult > 1.0."""
        for _ in range(5):
            self._insert_snapshot("0xgood", fill_count=0)
        result = load_historical_adjustments(self.db_path)
        self.assertIn("0xgood", result)
        self.assertGreater(result["0xgood"]["trend_mult"], 1.0)
        self.assertAlmostEqual(result["0xgood"]["fill_rate"], 0.0)

    def test_high_fill_market_gets_penalty(self):
        """Markets with high fill rate get trend_mult < 1.0."""
        for _ in range(5):
            self._insert_snapshot("0xbad", fill_count=3)
        result = load_historical_adjustments(self.db_path)
        self.assertIn("0xbad", result)
        self.assertLess(result["0xbad"]["trend_mult"], 1.0)
        self.assertAlmostEqual(result["0xbad"]["fill_rate"], 1.0)

    def test_very_negative_score_extra_penalty(self):
        """Markets with worst_score < -5 get extra penalty."""
        for _ in range(3):
            self._insert_snapshot("0xrisky", fill_count=1, net_score=-10.0)
        result = load_historical_adjustments(self.db_path)
        # fill_rate = 100% → base mult = 0.5
        # worst_score = -10 < -5 → ×0.8 = 0.4
        self.assertIn("0xrisky", result)
        self.assertLessEqual(result["0xrisky"]["trend_mult"], 0.5)

    def test_old_data_excluded(self):
        """Snapshots older than 7 days are not used."""
        for _ in range(5):
            self._insert_snapshot("0xold", fill_count=0, ts_offset=8*86400)
        result = load_historical_adjustments(self.db_path)
        self.assertNotIn("0xold", result)


if __name__ == "__main__":
    unittest.main()
