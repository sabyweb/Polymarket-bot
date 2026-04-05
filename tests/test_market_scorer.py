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
        # effective_daily = 50 * 1.0 = 50, + min(50*0.5, 2.0) = +2.0 = 52
        self.assertAlmostEqual(score, 52.0)

    def test_zero_fill_low_qshare(self):
        """Low Q-share but zero fills: still positive."""
        m = _make_metric(daily_rate=100, q_share_pct=0.1)
        score = score_market(m, hours=24)
        # effective = 10, + min(10*0.5, 2.0) = +2.0 = 12
        self.assertAlmostEqual(score, 12.0)

    def test_zero_fill_bonus_capped(self):
        """Zero-fill bonus is capped at $2/day so dust markets can't dominate."""
        tiny = _make_metric(daily_rate=1, q_share_pct=0.01)  # effective = $0.01/day
        score_tiny = score_market(tiny, hours=24)
        # effective = 0.01, bonus = min(0.005, 2.0) = 0.005, total = 0.015
        self.assertAlmostEqual(score_tiny, 0.015)

        big = _make_metric(daily_rate=50, q_share_pct=1.0, fill_count_recent=1,
                           fill_cost_recent=1.0, dump_revenue_recent=0.0)
        score_big = score_market(big, hours=24)
        # effective = 50, damage = 1, no bonus (has fills), total = 49
        self.assertAlmostEqual(score_big, 49.0)

        # Big market with 1 fill MUST outscore dust market with zero fills
        self.assertGreater(score_big, score_tiny)

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
        # Without: 100 + min(50, 2) = 102
        # With 0.1: 10 + min(5, 2) = 12
        self.assertAlmostEqual(score_no_correction, 102.0)
        self.assertAlmostEqual(score_with_correction, 12.0)

    def test_correction_factor_does_not_affect_fill_damage(self):
        """Fill damage is real, should NOT be scaled by correction factor."""
        m = _make_metric(
            daily_rate=100, q_share_pct=1.0,
            fill_cost_recent=20.0, fill_count_recent=2,
        )
        score = score_market(m, hours=24, correction_factor=0.1)
        # corrected_daily = 10, damage = 20/day, score = 10 - 20 = -10 (no bonus, has fills)
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


class TestContinuousSizing(unittest.TestCase):
    """Test that sizing scales continuously with score, not discrete tiers."""

    def test_higher_score_gets_more_shares(self):
        """Markets with higher scores get strictly more shares."""
        low = _make_metric(daily_rate=20, q_share_pct=1.0)
        high = _make_metric(daily_rate=100, q_share_pct=1.0)
        sm_low = classify_market(low, score=20.0)
        sm_high = classify_market(high, score=100.0)
        self.assertGreater(sm_high.recommended_shares, sm_low.recommended_shares)

    def test_no_cliff_at_boundaries(self):
        """Markets at $49/d and $51/d should NOT jump 2x in shares."""
        m49 = _make_metric(daily_rate=49, q_share_pct=1.0)
        m51 = _make_metric(daily_rate=51, q_share_pct=1.0)
        sm49 = classify_market(m49, score=49.0)
        sm51 = classify_market(m51, score=51.0)
        # Ratio should be close, not 2x jump
        ratio = sm51.recommended_shares / sm49.recommended_shares
        self.assertLess(ratio, 1.2)  # no more than 20% jump

    def test_fills_reduce_sizing_multiplier(self):
        """Markets with fills get more conservative sizing than zero-fill."""
        m_nofill = _make_metric(daily_rate=50, q_share_pct=1.0)
        m_fills = _make_metric(daily_rate=50, q_share_pct=1.0,
                               fill_count_recent=2, fill_cost_recent=5.0)
        sm_nofill = classify_market(m_nofill, score=50.0)
        sm_fills = classify_market(m_fills, score=45.0)
        self.assertGreaterEqual(sm_nofill.recommended_shares, sm_fills.recommended_shares)

    def test_negative_score_gets_default_or_zero(self):
        """Negative score: should not scale up."""
        m = _make_metric(daily_rate=50, q_share_pct=0.5,
                         fill_count_recent=5, fill_cost_recent=100.0)
        sm = classify_market(m, score=-50.0)
        self.assertEqual(sm.recommended_shares, 0)

    def test_capped_at_4x(self):
        """Even an amazing market can't exceed 4x default shares."""
        m = _make_metric(daily_rate=1000, q_share_pct=1.0)
        sm = classify_market(m, score=1000.0)
        self.assertLessEqual(sm.recommended_shares, 50 * 4)


class TestFastReactAndConfidence(unittest.TestCase):
    """Test fast-react fill penalty and confidence ramp-up."""

    def test_fills_reduce_score_via_fast_react(self):
        """rank_markets should penalize markets with recent fills immediately."""
        # Two identical markets: one with fills, one without
        metrics = [
            _make_metric(condition_id="clean", daily_rate=50, q_share_pct=1.0),
            _make_metric(condition_id="dirty", daily_rate=50, q_share_pct=1.0,
                         fill_count_recent=3, fill_cost_recent=5.0),
        ]
        scored = rank_markets(metrics, max_markets=10)
        scores = {s.condition_id: s.score for s in scored}
        # Clean should have higher score than dirty after fast-react
        self.assertGreater(scores["clean"], scores["dirty"])

    def test_new_market_gets_reduced_shares(self):
        """Markets with <8h on-book should get reduced shares (confidence ramp)."""
        m_new = _make_metric(condition_id="new", daily_rate=50, q_share_pct=1.0,
                             on_book_hours=2)
        m_old = _make_metric(condition_id="old", daily_rate=50, q_share_pct=1.0,
                             on_book_hours=24)
        scored = rank_markets([m_new, m_old], max_markets=10)
        shares = {s.condition_id: s.recommended_shares for s in scored}
        # New market should have fewer shares due to confidence ramp
        self.assertLess(shares["new"], shares["old"])


class TestCapitalRedistribution(unittest.TestCase):
    """Test the two-pass allocation with redistribution."""

    def test_surplus_gets_redistributed(self):
        """When budget > base allocation, surplus goes to top markets."""
        from oversight.allocation_writer import compute_allocations
        scored = [
            ScoredMarket(condition_id="top", question="Top?", score=100.0,
                         action="deploy", recommended_shares=50, reason="test",
                         confidence="high", actual_reward_total=10.0,
                         fill_damage=0, fill_count=0, daily_rate=100,
                         min_size=50, max_spread=0.045),
            ScoredMarket(condition_id="mid", question="Mid?", score=50.0,
                         action="deploy", recommended_shares=50, reason="test",
                         confidence="high", actual_reward_total=5.0,
                         fill_damage=0, fill_count=0, daily_rate=50,
                         min_size=50, max_spread=0.045),
        ]
        # Base cost: 2 markets × 50sh × $0.455 × 2 = ~$91. With $500 budget,
        # there's surplus to redistribute.
        allocs = compute_allocations(scored, total_capital=500.0)
        deployed = [a for a in allocs if a["action"] == "deploy"]

        # Top market should get more than base 50 from redistribution
        top_alloc = next(a for a in deployed if a["condition_id"] == "top")
        self.assertGreater(top_alloc["shares_per_side"], 50)

    def test_per_market_cap_respected(self):
        """No single market exceeds max_capital_pct of total budget."""
        from oversight.allocation_writer import compute_allocations, _est_market_cost
        scored = [
            ScoredMarket(condition_id="only", question="Only?", score=100.0,
                         action="deploy", recommended_shares=50, reason="test",
                         confidence="high", actual_reward_total=10.0,
                         fill_damage=0, fill_count=0, daily_rate=100,
                         min_size=50, max_spread=0.045),
        ]
        allocs = compute_allocations(scored, total_capital=2000.0, max_capital_pct=0.15)
        deployed = [a for a in allocs if a["action"] == "deploy"]
        for a in deployed:
            cost = _est_market_cost(a["shares_per_side"], a.get("max_spread", 0.045))
            self.assertLessEqual(cost, 2000.0 * 0.15 + 1.0)  # allow $1 rounding


class TestCorrectionFactorSmoothing(unittest.TestCase):
    """Test EMA smoothing of correction factor."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_first_observation_used_directly(self):
        """First observation: EMA = alpha * raw + (1-alpha) * 1.0."""
        from oversight.data_collector import _smooth_correction_factor
        result = _smooth_correction_factor(0.5, self.db_path, alpha=0.3,
                                           has_new_observation=True)
        # 0.3 * 0.5 + 0.7 * 1.0 = 0.85
        self.assertAlmostEqual(result, 0.85, places=2)

    def test_smoothing_damps_spike(self):
        """A spike from 1.0 to 3.0 should be damped by EMA."""
        from oversight.data_collector import _smooth_correction_factor
        # First: factor=1.0 → smoothed = 0.3*1.0 + 0.7*1.0 = 1.0
        _smooth_correction_factor(1.0, self.db_path, alpha=0.3,
                                  has_new_observation=True)
        # Second: spike to 3.0 → smoothed = 0.3*3.0 + 0.7*1.0 = 1.6
        result = _smooth_correction_factor(3.0, self.db_path, alpha=0.3,
                                           has_new_observation=True)
        self.assertAlmostEqual(result, 1.6, places=1)
        # Without smoothing would be 3.0 — EMA brings it to ~1.6

    def test_no_observation_returns_last(self):
        """When no new data, return last stored smoothed value."""
        from oversight.data_collector import _smooth_correction_factor
        _smooth_correction_factor(0.6, self.db_path, alpha=0.3,
                                  has_new_observation=True)
        result = _smooth_correction_factor(999.0, self.db_path, alpha=0.3,
                                           has_new_observation=False)
        # Should return prev smoothed, not 999
        self.assertLess(result, 2.0)


if __name__ == "__main__":
    unittest.main()
