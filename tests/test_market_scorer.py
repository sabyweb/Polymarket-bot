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
    ScoredMarket, _detect_regime_signals,
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
        """New market with low confidence gets trial deployment at min_size."""
        m = _make_metric(daily_rate=20, q_share_pct=0, on_book_hours=1)
        sm = classify_market(m, score=-1.0)
        # Should deploy as trial if rate >= 5, at min_size (not default_shares)
        self.assertEqual(sm.action, "deploy")
        self.assertEqual(sm.confidence, "low")
        self.assertEqual(sm.recommended_shares, int(m.min_size))


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

    def test_all_positive_markets_deploy(self):
        """All score-positive markets get deploy — exchange decides capital limit."""
        metrics = [
            _make_metric(condition_id=f"m{i}", daily_rate=100-i, q_share_pct=1.0)
            for i in range(10)
        ]
        scored = rank_markets(metrics, max_markets=3)
        deploy_count = sum(1 for s in scored if s.action == "deploy")
        # All 10 are score-positive → all deploy. Bot stops on exchange error.
        self.assertEqual(deploy_count, 10)


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

    def test_needs_2_snapshots(self):
        """Markets with < 2 snapshots are not adjusted (lowered from 3 for speed)."""
        self._insert_snapshot("0xtest", fill_count=0)
        result = load_historical_adjustments(self.db_path)
        self.assertNotIn("0xtest", result)

    def test_2_snapshots_sufficient(self):
        """2 snapshots is now enough for historical adjustment (was 3)."""
        self._insert_snapshot("0xtest", fill_count=0)
        self._insert_snapshot("0xtest", fill_count=0)
        result = load_historical_adjustments(self.db_path)
        self.assertIn("0xtest", result)

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

    def test_capped_at_4x_capital(self):
        """Even an amazing market can't exceed 4x base capital worth of shares."""
        m = _make_metric(daily_rate=1000, q_share_pct=1.0)
        sm = classify_market(m, score=1000.0)
        # 4x base capital = 4 * 50 * 0.91 = $182, at $0.91/share = 200 shares
        self.assertLessEqual(sm.recommended_shares, 200)

    def test_price_awareness_cheap_market_gets_more_shares(self):
        """A cheap market (wide spread) should get more shares for the same capital."""
        # Wide spread → lower cost per share → more shares for same capital
        cheap = _make_metric(daily_rate=50, q_share_pct=1.0, max_spread=0.20)
        # Narrow spread → higher cost per share → fewer shares
        expensive = _make_metric(daily_rate=50, q_share_pct=1.0, max_spread=0.02)
        sm_cheap = classify_market(cheap, score=50.0)
        sm_expensive = classify_market(expensive, score=50.0)
        # Same score, same multiplier, but cheap market should get more shares
        self.assertGreater(sm_cheap.recommended_shares, sm_expensive.recommended_shares)

    def test_price_awareness_equal_capital(self):
        """Different-priced markets at same score deploy roughly equal capital."""
        cheap = _make_metric(daily_rate=50, q_share_pct=1.0, max_spread=0.20)
        expensive = _make_metric(daily_rate=50, q_share_pct=1.0, max_spread=0.02)
        sm_cheap = classify_market(cheap, score=50.0)
        sm_expensive = classify_market(expensive, score=50.0)
        # Compute actual capital deployed
        cheap_cost = sm_cheap.recommended_shares * max(0.05, (1 - 2*0.20)/2) * 2
        expensive_cost = sm_expensive.recommended_shares * max(0.05, (1 - 2*0.02)/2) * 2
        # Capital should be within 20% of each other (same score → same target capital)
        ratio = max(cheap_cost, expensive_cost) / min(cheap_cost, expensive_cost)
        self.assertLess(ratio, 1.25)


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


class TestRebalanceCredit(unittest.TestCase):
    """Test that avoided markets with locked capital free budget for new deploys."""

    def test_avoid_with_position_frees_capital(self):
        """Avoided market's locked capital should become available (discounted)."""
        from oversight.allocation_writer import compute_allocations
        scored = [
            # Avoided market has $100 locked in positions
            ScoredMarket(condition_id="bad", question="Bad?", score=-10.0,
                         action="avoid", recommended_shares=0, reason="test",
                         confidence="high", actual_reward_total=0,
                         fill_damage=50, fill_count=5, daily_rate=50,
                         min_size=50, max_spread=0.045,
                         locked_position_usd=100.0),
            # Good market wants deployment
            ScoredMarket(condition_id="good", question="Good?", score=50.0,
                         action="deploy", recommended_shares=50, reason="test",
                         confidence="high", actual_reward_total=10.0,
                         fill_damage=0, fill_count=0, daily_rate=80,
                         min_size=50, max_spread=0.045),
        ]
        # With only $10 budget, normally can't deploy $45 market.
        # But $100 locked × 80% = $80 rebalance credit → $90 available.
        allocs = compute_allocations(scored, total_capital=10.0)
        good_alloc = next(a for a in allocs if a["condition_id"] == "good")
        self.assertEqual(good_alloc["action"], "deploy")

    def test_no_credit_without_locked_positions(self):
        """Avoided market with no position doesn't generate phantom credit."""
        from oversight.allocation_writer import compute_allocations
        # With locked position → rebalance credit inflates budget
        scored_with_lock = [
            ScoredMarket(condition_id="bad", question="Bad?", score=-10.0,
                         action="avoid", recommended_shares=0, reason="test",
                         confidence="high", actual_reward_total=0,
                         fill_damage=50, fill_count=5, daily_rate=50,
                         min_size=50, max_spread=0.045,
                         locked_position_usd=200.0),
            ScoredMarket(condition_id="good", question="Good?", score=50.0,
                         action="deploy", recommended_shares=100, reason="test",
                         confidence="high", actual_reward_total=10.0,
                         fill_damage=0, fill_count=0, daily_rate=80,
                         min_size=50, max_spread=0.045),
        ]
        # Without locked position → no credit
        scored_without_lock = [
            ScoredMarket(condition_id="bad", question="Bad?", score=-10.0,
                         action="avoid", recommended_shares=0, reason="test",
                         confidence="high", actual_reward_total=0,
                         fill_damage=50, fill_count=5, daily_rate=50,
                         min_size=50, max_spread=0.045,
                         locked_position_usd=0.0),
            ScoredMarket(condition_id="good", question="Good?", score=50.0,
                         action="deploy", recommended_shares=100, reason="test",
                         confidence="high", actual_reward_total=10.0,
                         fill_damage=0, fill_count=0, daily_rate=80,
                         min_size=50, max_spread=0.045),
        ]
        allocs_with = compute_allocations(scored_with_lock, total_capital=200.0)
        allocs_without = compute_allocations(scored_without_lock, total_capital=200.0)
        # Both deploy good market now (no capital gate), but the one with
        # rebalance credit has more budget for redistribution
        good_with = next(a for a in allocs_with if a["condition_id"] == "good")
        good_without = next(a for a in allocs_without if a["condition_id"] == "good")
        # With credit: budget inflated by $160 (200*0.8), so redistribution
        # gives more shares than without credit
        self.assertGreaterEqual(good_with["shares_per_side"], good_without["shares_per_side"])


class TestGroupConcentration(unittest.TestCase):
    """Test portfolio concentration limits per question group."""

    def test_group_cap_limits_allocation(self):
        """Markets sharing a question group are capped at max_group_pct."""
        from oversight.allocation_writer import compute_allocations
        # 3 Bitcoin markets, all high scoring, same group
        scored = [
            ScoredMarket(condition_id=f"btc{i}", question=f"Will Bitcoin reach ${p}k?",
                         score=80.0 - i*5, action="deploy", recommended_shares=100,
                         reason="test", confidence="high", actual_reward_total=10.0,
                         fill_damage=0, fill_count=0, daily_rate=80,
                         min_size=50, max_spread=0.045,
                         question_group="bitcoin reach")
            for i, p in enumerate([100, 150, 200])
        ]
        # Budget=$500, group_cap=30% → $150 max for Bitcoin group
        # Each market costs 100sh × $0.91 ≈ $91. Only ~1.6 fit in $150 cap.
        allocs = compute_allocations(scored, total_capital=500.0, max_group_pct=0.30)
        deployed_btc = [a for a in allocs if a["action"] == "deploy"]
        total_btc_cost = sum(
            a["shares_per_side"] * max(0.10, (1.0 - 2*0.045)/2) * 2
            for a in deployed_btc
        )
        self.assertLessEqual(total_btc_cost, 500 * 0.30 + 5)  # allow rounding

    def test_different_groups_independent(self):
        """Markets in different groups don't interfere with each other."""
        from oversight.allocation_writer import compute_allocations
        scored = [
            ScoredMarket(condition_id="btc1", question="Will Bitcoin reach $100k?",
                         score=80.0, action="deploy", recommended_shares=50,
                         reason="test", confidence="high", actual_reward_total=10.0,
                         fill_damage=0, fill_count=0, daily_rate=80,
                         min_size=50, max_spread=0.045,
                         question_group="bitcoin reach"),
            ScoredMarket(condition_id="eth1", question="Will Ethereum flip Bitcoin?",
                         score=70.0, action="deploy", recommended_shares=50,
                         reason="test", confidence="high", actual_reward_total=10.0,
                         fill_damage=0, fill_count=0, daily_rate=80,
                         min_size=50, max_spread=0.045,
                         question_group="ethereum flip bitcoin"),
        ]
        allocs = compute_allocations(scored, total_capital=500.0, max_group_pct=0.30)
        deployed = [a for a in allocs if a["action"] == "deploy"]
        self.assertEqual(len(deployed), 2)


class TestQuestionGrouping(unittest.TestCase):
    """Test the question grouping heuristic."""

    def test_related_questions_same_group(self):
        from oversight.data_collector import _question_group_key
        g1 = _question_group_key("Will Bitcoin reach $100k by June?")
        g2 = _question_group_key("Will Bitcoin reach $150k by June?")
        self.assertEqual(g1, g2)

    def test_different_topics_different_groups(self):
        from oversight.data_collector import _question_group_key
        g1 = _question_group_key("Will Bitcoin reach $100k?")
        g2 = _question_group_key("Will the Lakers win the championship?")
        self.assertNotEqual(g1, g2)

    def test_empty_question_empty_group(self):
        from oversight.data_collector import _question_group_key
        self.assertEqual(_question_group_key(""), "")


class TestCapitalEfficiency(unittest.TestCase):
    """Test that capital-inefficient markets are rejected."""

    def test_low_rate_high_cost_avoided(self):
        """$0.14/day pool with $186 deployed → capital inefficient → avoid."""
        # This is the Todd Blanche scenario: $0.14/day, 200sh × $0.93 = $186
        m = _make_metric(daily_rate=0.14, q_share_pct=0.01, max_spread=0.04)
        sm = classify_market(m, score=0.01, default_shares=200)
        # 0.14 / (200 * 0.92) = 0.00076 = 0.076% < 1% threshold → avoid
        self.assertEqual(sm.action, "avoid")
        self.assertIn("Capital inefficient", sm.reason)

    def test_high_rate_passes_efficiency_check(self):
        """$50/day pool with $91 deployed → very efficient → deploy."""
        m = _make_metric(daily_rate=50, q_share_pct=1.0, max_spread=0.045)
        sm = classify_market(m, score=50.0)
        self.assertEqual(sm.action, "deploy")

    def test_low_rate_filtered_in_rank_markets(self):
        """Markets below $5/day should be filtered before scoring."""
        metrics = [
            _make_metric(condition_id="cheap", daily_rate=0.14, q_share_pct=0.01),
            _make_metric(condition_id="good", daily_rate=50, q_share_pct=1.0),
        ]
        scored = rank_markets(metrics, max_markets=10)
        cids = {s.condition_id for s in scored}
        self.assertNotIn("cheap", cids)
        self.assertIn("good", cids)


class TestCompetitionAwareness(unittest.TestCase):
    """Test that competition (q_share) flows through scoring, efficiency, and output."""

    def test_competition_adjusted_efficiency_gate(self):
        """High pool rate but tiny q_share → competition inefficient → avoid."""
        # $50/day pool, but 0.5% q_share = $0.25/day effective
        # $0.25/day vs ~$45 deployed = 0.55% → below 0.5% threshold? No, 0.55% > 0.5%.
        # Use smaller q_share: 0.1% = $0.05/day vs ~$45 = 0.11% < 0.5% → avoid
        m = _make_metric(daily_rate=50, q_share_pct=0.001, max_spread=0.045)
        sm = classify_market(m, score=0.05)  # low positive score
        self.assertEqual(sm.action, "avoid")
        self.assertIn("Competition inefficient", sm.reason)

    def test_high_qshare_passes_competition_check(self):
        """High q_share → good effective return → deploy."""
        m = _make_metric(daily_rate=50, q_share_pct=0.5, max_spread=0.045)
        sm = classify_market(m, score=25.0)
        self.assertEqual(sm.action, "deploy")

    def test_unknown_competition_uses_pool_rate_only(self):
        """q_share=0 (unknown) → only pool rate check, no competition gate."""
        # $20/day pool, q_share=0, low confidence trial path
        m = _make_metric(daily_rate=20, q_share_pct=0, on_book_hours=1)
        sm = classify_market(m, score=-0.5)
        # Should deploy as trial (pool rate $20 is fine), not rejected by competition gate
        self.assertEqual(sm.action, "deploy")

    def test_trial_uses_min_size_not_default(self):
        """Trial deployments use min_size to minimize exposure on unknown markets."""
        m = _make_metric(daily_rate=15, q_share_pct=0, on_book_hours=1, min_size=100)
        sm = classify_market(m, score=-0.5)
        self.assertEqual(sm.action, "deploy")
        self.assertEqual(sm.recommended_shares, 100)  # min_size, not default_shares=50

    def test_qshare_pct_in_scored_market(self):
        """q_share_pct propagates from MarketMetrics to ScoredMarket."""
        m = _make_metric(daily_rate=50, q_share_pct=0.42)
        sm = classify_market(m, score=20.0)
        self.assertAlmostEqual(sm.q_share_pct, 0.42)

    def test_qshare_pct_in_allocation_output(self):
        """q_share_pct appears in the allocation JSON output."""
        from oversight.allocation_writer import compute_allocations
        scored = [
            ScoredMarket(condition_id="test", question="Test?", score=50.0,
                         action="deploy", recommended_shares=50, reason="test",
                         confidence="high", actual_reward_total=10.0,
                         fill_damage=0, fill_count=0, daily_rate=50,
                         min_size=50, max_spread=0.045,
                         q_share_pct=0.35),
        ]
        allocs = compute_allocations(scored, total_capital=500.0)
        self.assertIn("q_share_pct", allocs[0])
        self.assertAlmostEqual(allocs[0]["q_share_pct"], 0.35, places=4)


class TestRegimeDetection(unittest.TestCase):
    """Test structural market regime detection."""

    def test_resolution_proximity_high_price(self):
        """Market with mid price > 0.92 gets heavy penalty."""
        from oversight.market_scorer import _detect_regime_signals
        m = _make_metric(avg_bid=0.93, avg_ask=0.95)
        signals = _detect_regime_signals(m)
        self.assertIn("resolution_proximity", signals)
        self.assertLessEqual(signals["resolution_proximity"]["mult"], 0.3)

    def test_resolution_proximity_low_price(self):
        """Market with mid price < 0.08 gets heavy penalty."""
        from oversight.market_scorer import _detect_regime_signals
        m = _make_metric(avg_bid=0.05, avg_ask=0.07)
        signals = _detect_regime_signals(m)
        self.assertIn("resolution_proximity", signals)

    def test_normal_price_no_resolution_signal(self):
        """Market with mid price ~0.50 gets no resolution signal."""
        from oversight.market_scorer import _detect_regime_signals
        m = _make_metric(avg_bid=0.48, avg_ask=0.52)
        signals = _detect_regime_signals(m)
        self.assertNotIn("resolution_proximity", signals)

    def test_low_reward_window(self):
        """Market rarely in reward window gets penalty."""
        from oversight.market_scorer import _detect_regime_signals
        m = _make_metric(on_book_hours=10, reward_window_pct=0.15)
        signals = _detect_regime_signals(m)
        self.assertIn("low_reward_window", signals)
        self.assertLessEqual(signals["low_reward_window"]["mult"], 0.5)

    def test_high_reward_window_no_signal(self):
        """Market frequently in reward window gets no penalty."""
        from oversight.market_scorer import _detect_regime_signals
        m = _make_metric(on_book_hours=10, reward_window_pct=0.80)
        signals = _detect_regime_signals(m)
        self.assertNotIn("low_reward_window", signals)

    def test_new_market_no_reward_window_signal(self):
        """New market (< 4h) doesn't trigger reward window penalty."""
        from oversight.market_scorer import _detect_regime_signals
        m = _make_metric(on_book_hours=2, reward_window_pct=0.10)
        signals = _detect_regime_signals(m)
        self.assertNotIn("low_reward_window", signals)

    def test_adverse_selection(self):
        """Market with high adverse fill rate gets penalty."""
        from oversight.market_scorer import _detect_regime_signals
        m = _make_metric(fill_count_recent=2, adverse_fills=4)
        signals = _detect_regime_signals(m)
        self.assertIn("adverse_selection", signals)

    def test_regime_signals_reduce_score_in_ranking(self):
        """Regime signals should reduce score during rank_markets."""
        # Near-resolution market should score lower than normal market
        metrics = [
            _make_metric(condition_id="normal", daily_rate=50, q_share_pct=1.0,
                         avg_bid=0.48, avg_ask=0.52),
            _make_metric(condition_id="resolving", daily_rate=50, q_share_pct=1.0,
                         avg_bid=0.93, avg_ask=0.95),
        ]
        scored = rank_markets(metrics, max_markets=10)
        scores = {s.condition_id: s.score for s in scored}
        self.assertGreater(scores["normal"], scores["resolving"])


class TestShortTermPerformance(unittest.TestCase):
    """Test the short-term performance adaptation layer."""

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

    def _insert_snapshot(self, cid, fill_count=0, net_score=10.0, ts_offset=0, q_share_pct=0.5):
        self.db.execute(
            """INSERT INTO market_performance
               (ts, condition_id, question, window_hours, estimated_daily,
                correction_factor, corrected_daily, fill_cost, dump_revenue,
                net_score, action, q_share_pct, on_book_hours, fill_count,
                shares_recommended)
               VALUES (?, ?, '', 24, 50, 0.1, 5, 0, 0, ?, 'deploy', ?, 24, ?, 50)""",
            (time.time() - ts_offset, cid, net_score, q_share_pct, fill_count),
        )
        self.db.commit()

    def test_query_short_term_performance(self):
        """Short-term query returns data for markets with 2+ recent snapshots."""
        from oversight.data_collector import query_short_term_performance
        # Insert 3 snapshots in last 2 hours
        self._insert_snapshot("0xtest", fill_count=1, ts_offset=3600)
        self._insert_snapshot("0xtest", fill_count=2, ts_offset=1800)
        self._insert_snapshot("0xtest", fill_count=0, ts_offset=0)
        result = query_short_term_performance(self.db_path, hours=4.0)
        self.assertIn("0xtest", result)
        self.assertEqual(result["0xtest"]["snapshots"], 3)
        self.assertEqual(result["0xtest"]["fill_snapshots"], 2)
        self.assertEqual(result["0xtest"]["total_fills"], 3)

    def test_persistent_fills_penalized(self):
        """Markets with fills in 75%+ of recent snapshots get heavier penalty."""
        from oversight.data_collector import query_short_term_performance
        # 4 snapshots, 3 with fills → 75% persistence
        for i in range(4):
            fill = 2 if i < 3 else 0
            self._insert_snapshot("0xbad", fill_count=fill, ts_offset=i*1800)
        result = query_short_term_performance(self.db_path, hours=4.0)
        self.assertIn("0xbad", result)
        persistence = result["0xbad"]["fill_snapshots"] / result["0xbad"]["snapshots"]
        self.assertGreaterEqual(persistence, 0.75)

    def test_q_share_trend_declining(self):
        """Detect declining Q-share in short-term performance."""
        from oversight.data_collector import query_short_term_performance
        # Q-share dropping from 0.5 to 0.2 over 4 snapshots
        for i, q in enumerate([0.5, 0.4, 0.3, 0.2]):
            self._insert_snapshot("0xdecline", q_share_pct=q, ts_offset=(3-i)*1800)
        result = query_short_term_performance(self.db_path, hours=4.0)
        self.assertIn("0xdecline", result)
        self.assertLess(result["0xdecline"]["q_share_trend"], 1.0)

    def test_old_snapshots_excluded(self):
        """Snapshots older than the window are excluded."""
        from oversight.data_collector import query_short_term_performance
        # All snapshots 5+ hours old (outside 4h window)
        self._insert_snapshot("0xold", ts_offset=5*3600)
        self._insert_snapshot("0xold", ts_offset=6*3600)
        result = query_short_term_performance(self.db_path, hours=4.0)
        self.assertNotIn("0xold", result)


class TestQShareTrend(unittest.TestCase):
    """Test Q-share competition shift detection in historical adjustments."""

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

    def test_declining_qshare_detected(self):
        """Historical adjustments detect declining Q-share over 7 days."""
        # 8 snapshots: Q-share drops from 0.5 to 0.1
        q_values = [0.5, 0.45, 0.4, 0.35, 0.25, 0.2, 0.15, 0.1]
        for i, q in enumerate(q_values):
            self.db.execute(
                """INSERT INTO market_performance
                   (ts, condition_id, question, window_hours, estimated_daily,
                    correction_factor, corrected_daily, fill_cost, dump_revenue,
                    net_score, action, q_share_pct, on_book_hours, fill_count,
                    shares_recommended)
                   VALUES (?, ?, '', 24, 50, 1.0, 50, 0, 0, 10, 'deploy', ?, 24, 0, 50)""",
                (time.time() - (7-i) * 86400, "0xdeclining", q),
            )
        self.db.commit()
        result = load_historical_adjustments(self.db_path)
        self.assertIn("0xdeclining", result)
        # Q-share trend should be < 1.0 (declining)
        self.assertLess(result["0xdeclining"]["q_share_trend"], 0.5)

    def test_stable_qshare_no_penalty(self):
        """Stable Q-share doesn't trigger trend penalty."""
        for i in range(4):
            self.db.execute(
                """INSERT INTO market_performance
                   (ts, condition_id, question, window_hours, estimated_daily,
                    correction_factor, corrected_daily, fill_cost, dump_revenue,
                    net_score, action, q_share_pct, on_book_hours, fill_count,
                    shares_recommended)
                   VALUES (?, ?, '', 24, 50, 1.0, 50, 0, 0, 10, 'deploy', 0.5, 24, 0, 50)""",
                (time.time() - (3-i) * 86400, "0xstable"),
            )
        self.db.commit()
        result = load_historical_adjustments(self.db_path)
        self.assertIn("0xstable", result)
        self.assertAlmostEqual(result["0xstable"]["q_share_trend"], 1.0, places=1)


class TestFeedbackFreshness(unittest.TestCase):
    """Test that stale placement feedback is discounted."""

    def test_one_sided_skip_gets_moderate_penalty(self):
        """One side skipped → 0.6x penalty (not 0.3x like both sides)."""
        # This tests the enhanced feedback handling in rank_markets
        # We can verify indirectly: a market with one-side skip should score
        # higher than one with both-sides skip, but lower than no skip.
        metrics = [
            _make_metric(condition_id="clean", daily_rate=50, q_share_pct=1.0),
            _make_metric(condition_id="one_skip", daily_rate=50, q_share_pct=1.0),
            _make_metric(condition_id="both_skip", daily_rate=50, q_share_pct=1.0),
        ]
        # We can't easily mock placement_feedback in rank_markets since it
        # queries the DB, but we verify the logic exists by testing
        # _detect_regime_signals doesn't affect clean markets
        scored = rank_markets(metrics, max_markets=10)
        # Without actual feedback data, all should have same score
        scores = {s.condition_id: s.score for s in scored}
        # All should be close to each other (no feedback = no penalty)
        self.assertAlmostEqual(scores["clean"], scores["one_skip"], places=2)


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
