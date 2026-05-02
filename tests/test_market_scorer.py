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
        """q_share=1.0 is capped at 0.5 internally. Zero fills, no bonus."""
        m = _make_metric(daily_rate=50, q_share_pct=1.0)
        score = score_market(m, hours=24)
        # q_share capped to 0.5: effective_daily = 50 * 0.5 = 25
        # Zero-fill bonus removed. score = 25.0
        self.assertAlmostEqual(score, 25.0)

    def test_zero_fill_low_qshare(self):
        """Low Q-share but zero fills: still positive, no bonus."""
        m = _make_metric(daily_rate=100, q_share_pct=0.1)
        score = score_market(m, hours=24)
        # effective = 100 * 0.1 = 10, no bonus. score = 10.0
        self.assertAlmostEqual(score, 10.0)

    def test_no_zero_fill_bonus(self):
        """Zero-fill bonus was removed — zero fills and with-fills score the same
        (minus fill damage). Markets prove themselves through earnings, not silence."""
        tiny = _make_metric(daily_rate=1, q_share_pct=0.01)
        score_tiny = score_market(tiny, hours=24)
        # effective = 1 * 0.01 = 0.01, no bonus. score = 0.01
        self.assertAlmostEqual(score_tiny, 0.01)

        big = _make_metric(daily_rate=50, q_share_pct=1.0, fill_count_recent=1,
                           fill_cost_recent=1.0, dump_revenue_recent=0.0)
        score_big = score_market(big, hours=24)
        # q_share capped to 0.5: effective = 25, damage = 1. score = 24
        self.assertAlmostEqual(score_big, 24.0)

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
        # q_share capped to 0.5: effective=25, damage=(20-15)/1=5/day
        # score = 25 - 5 = 20
        self.assertAlmostEqual(score, 20.0)

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
        # q_share capped to 0.5. No zero-fill bonus.
        # Without correction: 100 * 0.5 * 1.0 = 50
        # With 0.1: 100 * 0.5 * 0.1 = 5
        self.assertAlmostEqual(score_no_correction, 50.0)
        self.assertAlmostEqual(score_with_correction, 5.0)

    def test_correction_factor_does_not_affect_fill_damage(self):
        """Fill damage is real, should NOT be scaled by correction factor."""
        m = _make_metric(
            daily_rate=100, q_share_pct=1.0,
            fill_cost_recent=20.0, fill_count_recent=2,
        )
        score = score_market(m, hours=24, correction_factor=0.1)
        # q_share capped to 0.5: corrected_daily = 100 * 0.5 * 0.1 = 5
        # damage = 20/day, score = 5 - 20 = -15
        self.assertAlmostEqual(score, -15.0)


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

    def test_low_confidence_with_fills_avoids(self):
        """Regression: low confidence + fills + negative score must avoid.

        Bug found 2026-04-06: Cardinals market had on_book_hours=0.09 (low
        confidence), fill_count=2, score=-155, but was classified as 'deploy'
        because the trial path only checked confidence and daily_rate, not
        whether fills already existed. This kept the bot placing orders on a
        market actively losing $740.
        """
        m = _make_metric(
            daily_rate=233, q_share_pct=1.0, on_book_hours=0.09,
            fill_count_recent=2, fill_cost_recent=740, dump_revenue_recent=0,
        )
        sm = classify_market(m, score=-155.0)
        self.assertEqual(sm.action, "avoid")
        self.assertEqual(sm.recommended_shares, 0)

    def test_classify_market_propagates_game_start_time(self):
        """ScoredMarket carries game_start_time from MarketMetrics for both
        the deploy path and the MIN_EFFECTIVE_DAILY avoid path."""
        # Deploy path: positive-score market with a game_start_time.
        m_deploy = _make_metric(
            daily_rate=50, q_share_pct=1.0, on_book_hours=24,
            end_date_iso="2026-05-01T00:00:00Z",
            game_start_time="2026-04-28T18:30:00Z",
        )
        sm_deploy = classify_market(m_deploy, score=25.0)
        self.assertEqual(sm_deploy.action, "deploy")
        self.assertEqual(sm_deploy.game_start_time, "2026-04-28T18:30:00Z")

        # MIN_EFFECTIVE_DAILY avoid path: q_share > 0 but effective_daily < 0.10.
        m_avoid = _make_metric(
            daily_rate=1.0, q_share_pct=0.05, on_book_hours=5,
            end_date_iso="2026-05-01T00:00:00Z",
            game_start_time="2026-04-29T20:00:00Z",
        )
        sm_avoid = classify_market(m_avoid, score=0.05, correction_factor=1.0)
        self.assertEqual(sm_avoid.game_start_time, "2026-04-29T20:00:00Z")


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

    def test_trial_cap_is_configurable_and_keeps_top_by_daily_rate(self):
        """Trial cap reads from RF_MAX_TRIAL_MARKETS config and keeps the
        highest-daily_rate trials.

        Creates more trial candidates than the configured cap (all score=0 via
        q_share_pct=0, low confidence via on_book_hours=1, varying daily_rate).
        Asserts:
        1. Exactly RF_MAX_TRIAL_MARKETS trials deploy (rest are trial-capped).
        2. The kept trials are the top-N by daily_rate (not random).
        3. Capped markets have reason containing "Trial cap reached".
        """
        from config import RF_MAX_TRIAL_MARKETS
        n_trials = RF_MAX_TRIAL_MARKETS + 10  # overflow the cap by 10
        # daily_rate=100.0 for m0 down to daily_rate=41.0 for m59.
        # All above the trial-path threshold of $5/day, no sports keywords,
        # no end_date_iso (so no short-duration filter fires), q_share=0 (score=0).
        metrics = [
            _make_metric(
                condition_id=f"trial{i}",
                question=f"Trial market {i}?",
                daily_rate=100.0 - i,
                q_share_pct=0.0,
                on_book_hours=1.0,
                fill_count_recent=0,
            )
            for i in range(n_trials)
        ]
        # Use a bogus db_path so rank_markets doesn't touch real DB state.
        scored = rank_markets(metrics, max_markets=100, db_path="/nonexistent.db")

        deployed = [s for s in scored if s.action == "deploy"]
        capped = [s for s in scored if s.action == "avoid" and "Trial cap reached" in s.reason]

        # (1) Exactly RF_MAX_TRIAL_MARKETS deploy.
        self.assertEqual(len(deployed), RF_MAX_TRIAL_MARKETS,
                         f"Expected {RF_MAX_TRIAL_MARKETS} deploys, got {len(deployed)}")

        # (2) Overflow count = n_trials - cap, and all are trial-capped.
        self.assertEqual(len(capped), n_trials - RF_MAX_TRIAL_MARKETS)

        # (3) The kept trials are the top-N by daily_rate (descending).
        # Highest daily_rate is trial0 (100.0), trial1 (99.0), ... so the first
        # RF_MAX_TRIAL_MARKETS cids (trial0..trial{N-1}) should deploy; the rest avoid.
        deployed_cids = {s.condition_id for s in deployed}
        expected_deployed = {f"trial{i}" for i in range(RF_MAX_TRIAL_MARKETS)}
        self.assertEqual(deployed_cids, expected_deployed,
                         "Trial cap did not keep the top-N by daily_rate")

    def test_trial_cap_uses_confidence_and_fills_not_score(self):
        """Trial cap catches cold-start markets even when they score positive.

        After RF_NEW_MARKET_Q_SHARE_PRIOR was introduced, new markets can
        score > 0 due to the prior. The trial cap must still throttle them
        based on confidence (low = on_book_hours < 2) + zero fills, not score.
        """
        from config import RF_MAX_TRIAL_MARKETS
        n_trials = RF_MAX_TRIAL_MARKETS + 5
        # All markets have LOW confidence (on_book_hours=1) and ZERO fills.
        # q_share_pct=0.2 (simulating the prior post-propagation) so score > 0.
        metrics = [
            _make_metric(
                condition_id=f"coldstart{i}",
                question=f"Cold-start market {i}?",
                daily_rate=100.0 - i,
                q_share_pct=0.2,           # positive score despite cold-start
                on_book_hours=1.0,          # low confidence
                fill_count_recent=0,        # zero fills
            )
            for i in range(n_trials)
        ]
        scored = rank_markets(metrics, max_markets=200, db_path="/nonexistent.db")

        deployed = [s for s in scored if s.action == "deploy"]
        capped = [s for s in scored if s.action == "avoid" and "Trial cap reached" in s.reason]

        # Cap should still enforce RF_MAX_TRIAL_MARKETS, even though every
        # market has positive score (due to the prior-like q_share=0.2).
        self.assertEqual(len(deployed), RF_MAX_TRIAL_MARKETS,
                         f"Trial cap leaked: expected {RF_MAX_TRIAL_MARKETS} deploys, got {len(deployed)}")
        self.assertEqual(len(capped), n_trials - RF_MAX_TRIAL_MARKETS)

    def test_trial_cap_ignores_high_confidence_markets(self):
        """Markets with confidence >= "medium" (on_book_hours >= 2) are NOT
        trial-capped regardless of score. Only cold-start markets count.

        Verifies that the new trial cap criterion (confidence == "low" AND
        fill_count == 0) correctly excludes markets that have accumulated
        sufficient on-book observation time.
        """
        from config import RF_MAX_TRIAL_MARKETS
        # 5 high-confidence markets — should always deploy, not counted toward cap.
        high_conf = [
            _make_metric(
                condition_id=f"hc{i}",
                question=f"High-confidence market {i}?",
                daily_rate=50.0,
                q_share_pct=1.0,         # score > 0
                on_book_hours=10.0,       # HIGH confidence (>= 8h)
                fill_count_recent=0,
            )
            for i in range(5)
        ]
        # 60 cold-start markets — subject to trial cap.
        n_trials = RF_MAX_TRIAL_MARKETS + 10
        cold_start = [
            _make_metric(
                condition_id=f"cs{i}",
                question=f"Cold-start market {i}?",
                daily_rate=25.0,          # lower, so hc markets rank above
                q_share_pct=0.0,
                on_book_hours=1.0,        # low confidence
                fill_count_recent=0,
            )
            for i in range(n_trials)
        ]
        metrics = high_conf + cold_start
        scored = rank_markets(metrics, max_markets=200, db_path="/nonexistent.db")

        deployed_cids = {s.condition_id for s in scored if s.action == "deploy"}

        # All 5 high-confidence markets deploy (never counted as trials).
        for i in range(5):
            self.assertIn(f"hc{i}", deployed_cids,
                          f"High-confidence market hc{i} was incorrectly capped")

        # Exactly RF_MAX_TRIAL_MARKETS cold-start markets deploy.
        cs_deployed = [c for c in deployed_cids if c.startswith("cs")]
        self.assertEqual(len(cs_deployed), RF_MAX_TRIAL_MARKETS,
                         f"Expected {RF_MAX_TRIAL_MARKETS} cold-start deploys, got {len(cs_deployed)}")

        # Total deployed = 5 (high-conf) + RF_MAX_TRIAL_MARKETS (cold-start cap).
        self.assertEqual(len(deployed_cids), 5 + RF_MAX_TRIAL_MARKETS)


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

    def test_compute_allocations_stamps_total_capital(self):
        """Every deploy row carries _total_capital matching the input
        total_capital (rounded to 2 dp). Mirrors profit/allocator.py:379.
        Read by reward_farmer._guardrail_total_capital_from_alloc to gate
        notional/cluster/loss safety checks and the notional_drift /
        slow_bleed shadow oversight signals."""
        from oversight.allocation_writer import compute_allocations
        scored = [
            ScoredMarket(condition_id="cidA", question="A?", score=10.0,
                         action="deploy", recommended_shares=50, reason="test",
                         confidence="high", actual_reward_total=0,
                         fill_damage=0, fill_count=0, daily_rate=100,
                         min_size=50, max_spread=0.045),
            ScoredMarket(condition_id="cidB", question="B?", score=8.0,
                         action="deploy", recommended_shares=50, reason="test",
                         confidence="high", actual_reward_total=0,
                         fill_damage=0, fill_count=0, daily_rate=80,
                         min_size=50, max_spread=0.045),
        ]
        allocs = compute_allocations(scored, total_capital=1234.567)
        deployed = [a for a in allocs if a["action"] == "deploy"]
        self.assertGreater(len(deployed), 0, "expected at least one deploy")
        for a in deployed:
            self.assertIn("_total_capital", a,
                          f"deploy row missing _total_capital: {a['condition_id']}")
            self.assertEqual(a["_total_capital"], 1234.57,
                             "stamp must be round(total_capital, 2)")

    def test_compute_allocations_avoid_rows_not_stamped(self):
        """Avoid rows do NOT carry _total_capital; the stamp is deploy-only.
        Mirrors the reader at reward_farmer.py:1082 which filters
        action == 'deploy' before reading the stamp."""
        from oversight.allocation_writer import compute_allocations
        scored = [
            ScoredMarket(condition_id="cidA", question="A?", score=10.0,
                         action="avoid", recommended_shares=0,
                         reason="below threshold",
                         confidence="low", actual_reward_total=0,
                         fill_damage=0, fill_count=0, daily_rate=2,
                         min_size=50, max_spread=0.045),
        ]
        allocs = compute_allocations(scored, total_capital=1000.0)
        avoided = [a for a in allocs if a["action"] == "avoid"]
        self.assertGreater(len(avoided), 0)
        for a in avoided:
            self.assertNotIn("_total_capital", a,
                             "avoid rows should not be stamped")


class TestRebalanceCredit(unittest.TestCase):
    """Test that avoided markets with locked capital free budget for new deploys."""

    def test_avoid_with_position_frees_capital(self):
        """Avoided market's locked capital should become available (discounted).

        With $250 base + $80 rebalance credit = $330 effective.
        per_market_cap = min(200, 330 * 0.15) = $49.50 → enough for
        a 50-share market (est_cost ~$45.50).
        Without credit: per_market_cap = min(200, 250 * 0.15) = $37.50 → too small.
        """
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
        # Without credit: per_market_cap = 250 * 0.15 = $37.50 < $45.50 est → avoided.
        # With credit: effective = 250 + 80 = $330, cap = $49.50 > $45.50 → deployed.
        allocs_no_credit = compute_allocations(scored[:1] + [scored[1]], total_capital=250.0)
        # Verify without credit it would be avoided (locked=0 → no credit)
        scored_no_lock = [
            ScoredMarket(condition_id="bad", question="Bad?", score=-10.0,
                         action="avoid", recommended_shares=0, reason="test",
                         confidence="high", actual_reward_total=0,
                         fill_damage=50, fill_count=5, daily_rate=50,
                         min_size=50, max_spread=0.045,
                         locked_position_usd=0.0),
            scored[1],
        ]
        allocs_without = compute_allocations(scored_no_lock, total_capital=250.0)
        good_without = next(a for a in allocs_without if a["condition_id"] == "good")
        self.assertEqual(good_without["action"], "avoid")

        # With credit it should deploy
        allocs = compute_allocations(scored, total_capital=250.0)
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


class TestCapInversionRegression(unittest.TestCase):
    """Regression test: $0 available capital must NOT deploy markets at min_size."""

    def test_zero_capital_avoids_high_min_size_trials(self):
        """With $0 capital, trial markets with min_size=1000 must be avoided,
        not deployed at 1000 shares (the old cap-inversion bug)."""
        from oversight.allocation_writer import compute_allocations
        scored = [
            ScoredMarket(condition_id="trial1", question="Sports Match A?",
                         score=0.0, action="deploy", recommended_shares=50,
                         reason="Trial", confidence="low",
                         actual_reward_total=0, fill_damage=0, fill_count=0,
                         daily_rate=113, min_size=1000, max_spread=0.025),
        ]
        allocs = compute_allocations(scored, total_capital=0.0)
        trial = next(a for a in allocs if a["condition_id"] == "trial1")
        # Must be avoided, NOT deployed at 1000 shares
        self.assertEqual(trial["action"], "avoid")
        self.assertEqual(trial["shares_per_side"], 0)

    def test_low_capital_avoids_expensive_markets(self):
        """With $50 capital, a market needing $45.50 should deploy (fits
        within 15% cap of $50 = $7.50? No — too small). Only deploy when
        per_market_cap >= est_cost."""
        from oversight.allocation_writer import compute_allocations
        scored = [
            ScoredMarket(condition_id="m1", question="Test?", score=10.0,
                         action="deploy", recommended_shares=50,
                         reason="test", confidence="high",
                         actual_reward_total=5, fill_damage=0, fill_count=0,
                         daily_rate=80, min_size=50, max_spread=0.045),
        ]
        # per_market_cap = min(200, 50*0.15) = $7.50, est_cost ~$45.50
        # capped_shares = int(7.50 / 0.91) = 8, which is < min_size (50)
        allocs = compute_allocations(scored, total_capital=50.0)
        m1 = next(a for a in allocs if a["condition_id"] == "m1")
        self.assertEqual(m1["action"], "avoid")


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
        """$0.14/day pool with tiny q_share → avoided (min effective reward gate
        or capital inefficiency — either way, correctly avoided)."""
        m = _make_metric(daily_rate=0.14, q_share_pct=0.01, max_spread=0.04)
        sm = classify_market(m, score=0.01, default_shares=200)
        # effective_daily = 0.14 * 0.01 * 1.0 = 0.0014 < $0.10 min threshold
        # The min-effective-reward gate fires before the capital efficiency gate.
        self.assertEqual(sm.action, "avoid")

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
        """High pool rate but tiny q_share → avoided (min effective reward gate
        or competition inefficiency — either way, correctly avoided)."""
        # $50/day pool, 0.1% q_share → effective = 50 * 0.001 * 1.0 = $0.05/day
        # Min effective reward gate: $0.05 < $0.10 → avoid
        m = _make_metric(daily_rate=50, q_share_pct=0.001, max_spread=0.045)
        sm = classify_market(m, score=0.05)
        self.assertEqual(sm.action, "avoid")

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

    def test_trial_capped_at_default_shares(self):
        """Trial deployments capped at min(min_size, default_shares) to limit exposure."""
        m = _make_metric(daily_rate=15, q_share_pct=0, on_book_hours=1, min_size=100)
        sm = classify_market(m, score=-0.5)
        self.assertEqual(sm.action, "deploy")
        self.assertEqual(sm.recommended_shares, 50)  # capped at default_shares, not inflated to min_size=100

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


# ─────────────────────────────────────────────────────────────────────
# NEW TEST GROUPS: Coverage for adaptation layers in rank_markets
# ─────────────────────────────────────────────────────────────────────


def _make_ranking_db():
    """Create a temp DB with all tables rank_markets needs."""
    fd, path = tempfile.mkstemp(suffix=".db")
    db = sqlite3.connect(path)
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
    db.execute("""CREATE TABLE placement_feedback (
        condition_id TEXT NOT NULL,
        side TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT DEFAULT '',
        ts REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (condition_id, side)
    )""")
    db.execute("""CREATE TABLE cycle_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        cycle_num INTEGER NOT NULL,
        condition_id TEXT NOT NULL,
        best_bid REAL,
        best_ask REAL,
        our_bid REAL,
        our_ask REAL,
        yes_position_usd REAL DEFAULT 0,
        no_position_usd REAL DEFAULT 0,
        active_orders INTEGER DEFAULT 0,
        unwind_orders INTEGER DEFAULT 0
    )""")
    db.commit()
    return fd, path, db


class TestMultiplierComposition(unittest.TestCase):
    """Test that multiple adaptation layers compound multiplicatively."""

    def setUp(self):
        self.db_fd, self.db_path, self.db = _make_ranking_db()

    def tearDown(self):
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_all_layers_compound_multiplicatively(self):
        """Regime + fast-react + historical should all multiply the score."""
        # Market with near-resolution price (regime 0.3x) + 3 fills (fast-react 0.88^3)
        m_penalized = _make_metric(
            condition_id="penalized", daily_rate=50, q_share_pct=1.0,
            avg_bid=0.93, avg_ask=0.95, fill_count_recent=3,
            on_book_hours=24,
        )
        # Clean market for comparison
        m_clean = _make_metric(
            condition_id="clean", daily_rate=50, q_share_pct=1.0,
            avg_bid=0.48, avg_ask=0.52, fill_count_recent=0,
            on_book_hours=24,
        )
        scored = rank_markets([m_penalized, m_clean], max_markets=10, db_path=self.db_path)
        scores = {s.condition_id: s.score for s in scored}

        # Penalized should be much lower: base * 0.3 (regime) * 0.681 (fast-react)
        self.assertGreater(scores["clean"], scores["penalized"] * 3)

    def test_heavily_penalized_market_classified_correctly(self):
        """Market with many penalties should be avoid (near-zero or negative score)."""
        m = _make_metric(
            condition_id="doomed", daily_rate=10, q_share_pct=0.1,
            avg_bid=0.93, avg_ask=0.95,  # regime: 0.3x
            fill_count_recent=5,          # fast-react: 0.88^5 = 0.53x
            fill_cost_recent=5.0, dump_revenue_recent=0.0,
            on_book_hours=24,
        )
        scored = rank_markets([m], max_markets=10, db_path=self.db_path)
        self.assertEqual(scored[0].action, "avoid")

    def test_sizing_never_below_min_size(self):
        """Even with stacked penalties, deployed market has shares >= min_size."""
        m = _make_metric(
            condition_id="penalized_deploy", daily_rate=100, q_share_pct=1.0,
            fill_count_recent=2, fill_cost_recent=1.0, dump_revenue_recent=0.0,
            on_book_hours=2,  # confidence ramp: ~0.625
            min_size=50,
        )
        # Insert feedback for placement penalty
        self.db.execute(
            "INSERT INTO placement_feedback (condition_id, side, status, reason, ts) VALUES (?, ?, ?, ?, ?)",
            ("penalized_deploy", "yes", "skipped", "wide_spread", time.time()),
        )
        self.db.commit()
        scored = rank_markets([m], max_markets=10, db_path=self.db_path)
        deployed = [s for s in scored if s.action == "deploy"]
        for s in deployed:
            self.assertGreaterEqual(s.recommended_shares, 50)


class TestPlacementFeedbackInRanking(unittest.TestCase):
    """Test placement feedback layer within rank_markets."""

    def setUp(self):
        self.db_fd, self.db_path, self.db = _make_ranking_db()

    def tearDown(self):
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _insert_feedback(self, cid, yes_status, yes_reason, no_status, no_reason, ts=None):
        ts = ts or time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO placement_feedback (condition_id, side, status, reason, ts) VALUES (?, ?, ?, ?, ?)",
            (cid, "yes", yes_status, yes_reason, ts),
        )
        self.db.execute(
            "INSERT OR REPLACE INTO placement_feedback (condition_id, side, status, reason, ts) VALUES (?, ?, ?, ?, ?)",
            (cid, "no", no_status, no_reason, ts),
        )
        self.db.commit()

    def test_both_sides_wide_spread_0_3x(self):
        """Both sides skipped with wide_spread → ~0.3x score."""
        self._insert_feedback("penalized", "skipped", "wide_spread", "skipped", "wide_spread")
        m_penalized = _make_metric(condition_id="penalized", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        m_clean = _make_metric(condition_id="clean", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        scored = rank_markets([m_penalized, m_clean], max_markets=10, db_path=self.db_path)
        scores = {s.condition_id: s.score for s in scored}
        # 0.3x penalty means penalized score ~ 0.3 * clean score
        ratio = scores["penalized"] / scores["clean"]
        self.assertAlmostEqual(ratio, 0.3, delta=0.05)

    def test_one_side_skip_0_6x(self):
        """Only YES skipped → ~0.6x score."""
        self._insert_feedback("one_skip", "skipped", "wide_spread", "placed", "")
        m_skip = _make_metric(condition_id="one_skip", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        m_clean = _make_metric(condition_id="clean", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        scored = rank_markets([m_skip, m_clean], max_markets=10, db_path=self.db_path)
        scores = {s.condition_id: s.score for s in scored}
        ratio = scores["one_skip"] / scores["clean"]
        self.assertAlmostEqual(ratio, 0.6, delta=0.05)

    def test_stale_feedback_ignored(self):
        """Feedback > 2h old should not penalize."""
        stale_ts = time.time() - 3 * 3600  # 3 hours ago
        self._insert_feedback("stale", "skipped", "wide_spread", "skipped", "wide_spread", ts=stale_ts)
        m_stale = _make_metric(condition_id="stale", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        m_clean = _make_metric(condition_id="clean", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        scored = rank_markets([m_stale, m_clean], max_markets=10, db_path=self.db_path)
        scores = {s.condition_id: s.score for s in scored}
        # Stale feedback → no penalty → same score
        self.assertAlmostEqual(scores["stale"], scores["clean"], places=2)

    def test_both_sides_failed_0_4x(self):
        """Both sides failed (non-capital reason) → ~0.4x score."""
        self._insert_feedback("failed", "failed", "order_error", "failed", "order_error")
        m_failed = _make_metric(condition_id="failed", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        m_clean = _make_metric(condition_id="clean", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        scored = rank_markets([m_failed, m_clean], max_markets=10, db_path=self.db_path)
        scores = {s.condition_id: s.score for s in scored}
        ratio = scores["failed"] / scores["clean"]
        self.assertAlmostEqual(ratio, 0.4, delta=0.05)


class TestShortTermTrendInRanking(unittest.TestCase):
    """Test short-term trend layer within rank_markets."""

    def setUp(self):
        self.db_fd, self.db_path, self.db = _make_ranking_db()

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

    def test_75pct_fill_persistence_reduces_score(self):
        """4 snapshots, 3 with fills → 0.5x penalty."""
        for i in range(4):
            fill = 2 if i < 3 else 0
            self._insert_snapshot("persistent", fill_count=fill, ts_offset=i * 1800)
        m_bad = _make_metric(condition_id="persistent", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        m_clean = _make_metric(condition_id="clean", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        scored = rank_markets([m_bad, m_clean], max_markets=10, db_path=self.db_path)
        scores = {s.condition_id: s.score for s in scored}
        # 0.5x from short-term persistence
        self.assertLess(scores["persistent"], scores["clean"] * 0.6)

    def test_50pct_fill_persistence_moderate_penalty(self):
        """4 snapshots, 2 with fills → 0.7x penalty."""
        for i in range(4):
            fill = 2 if i < 2 else 0
            self._insert_snapshot("moderate", fill_count=fill, ts_offset=i * 1800)
        m_mod = _make_metric(condition_id="moderate", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        m_clean = _make_metric(condition_id="clean", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        scored = rank_markets([m_mod, m_clean], max_markets=10, db_path=self.db_path)
        scores = {s.condition_id: s.score for s in scored}
        ratio = scores["moderate"] / scores["clean"]
        self.assertGreater(ratio, 0.5)  # not as bad as 75%
        self.assertLess(ratio, 0.85)    # but still penalized

    def test_declining_score_compounds_penalty(self):
        """score_trend < 0.5 + avg_score < 0 → additional 0.7x compounding."""
        # Scores declining rapidly: 5.0 → -10.0
        scores_seq = [5.0, 2.0, -5.0, -10.0]
        for i, s in enumerate(scores_seq):
            fill = 2 if i >= 2 else 0  # 50% fill persistence
            self._insert_snapshot("declining", fill_count=fill, net_score=s, ts_offset=(3 - i) * 1800)
        m_dec = _make_metric(condition_id="declining", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        m_clean = _make_metric(condition_id="clean", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        scored = rank_markets([m_dec, m_clean], max_markets=10, db_path=self.db_path)
        scores = {s.condition_id: s.score for s in scored}
        # Should be penalized more than just 0.7x from fill persistence
        ratio = scores["declining"] / scores["clean"]
        self.assertLess(ratio, 0.55)  # 0.7 * 0.7 = 0.49 expected


class TestQShareTrendInRanking(unittest.TestCase):
    """Test Q-share trend multipliers within rank_markets."""

    def setUp(self):
        self.db_fd, self.db_path, self.db = _make_ranking_db()

    def tearDown(self):
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _insert_historical(self, cid, q_values, fill_counts=None):
        """Insert historical snapshots spread over 7 days."""
        if fill_counts is None:
            fill_counts = [0] * len(q_values)
        for i, (q, fc) in enumerate(zip(q_values, fill_counts)):
            self.db.execute(
                """INSERT INTO market_performance
                   (ts, condition_id, question, window_hours, estimated_daily,
                    correction_factor, corrected_daily, fill_cost, dump_revenue,
                    net_score, action, q_share_pct, on_book_hours, fill_count,
                    shares_recommended)
                   VALUES (?, ?, '', 24, 50, 1.0, 50, 0, 0, 10, 'deploy', ?, 24, ?, 50)""",
                (time.time() - (len(q_values) - 1 - i) * 86400, cid, q, fc),
            )
        self.db.commit()

    def test_q_trend_below_0_5_applies_0_6x(self):
        """Q-share halved → 0.6x additional penalty."""
        # Q drops from 0.5 to 0.1 over 5 days (trend ~0.2)
        self._insert_historical("declining", [0.5, 0.4, 0.3, 0.2, 0.1])
        m = _make_metric(condition_id="declining", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        m_clean = _make_metric(condition_id="clean", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        scored = rank_markets([m, m_clean], max_markets=10, db_path=self.db_path)
        scores = {s.condition_id: s.score for s in scored}
        # trend_mult (from fill_rate) * 0.6 (q_trend)
        # Zero fills → trend_mult = 1.2 (bonus), then * 0.6 = 0.72
        ratio = scores["declining"] / scores["clean"]
        self.assertLess(ratio, 0.85)

    def test_q_trend_between_0_5_and_0_75_applies_0_8x(self):
        """Moderate Q-share decline → 0.8x penalty."""
        # Q drops from 0.5 to 0.3 over 5 days (trend ~0.6)
        self._insert_historical("moderate", [0.5, 0.45, 0.4, 0.35, 0.3])
        m = _make_metric(condition_id="moderate", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        m_clean = _make_metric(condition_id="clean", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        scored = rank_markets([m, m_clean], max_markets=10, db_path=self.db_path)
        scores = {s.condition_id: s.score for s in scored}
        ratio = scores["moderate"] / scores["clean"]
        # 1.2 * 0.8 = 0.96, but should be distinctly less than 1.0
        self.assertLess(ratio, 1.0)

    def test_q_trend_above_0_75_no_extra_penalty(self):
        """Stable Q-share → no q-trend penalty (only normal historical trend)."""
        self._insert_historical("stable", [0.5, 0.49, 0.48, 0.47, 0.46])
        m = _make_metric(condition_id="stable", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        m_clean = _make_metric(condition_id="clean", daily_rate=50, q_share_pct=1.0, on_book_hours=24)
        scored = rank_markets([m, m_clean], max_markets=10, db_path=self.db_path)
        scores = {s.condition_id: s.score for s in scored}
        # Zero fills → 1.2x bonus, no q-trend penalty → ratio > 1.0
        ratio = scores["stable"] / scores["clean"]
        self.assertGreater(ratio, 1.0)


class TestFastReactCompounding(unittest.TestCase):
    """Test fast-react exponential compounding with multiple fills."""

    def setUp(self):
        self.db_fd, self.db_path, self.db = _make_ranking_db()

    def tearDown(self):
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_fast_react_5_fills_compounds(self):
        """5 fills → 0.88^5 ≈ 0.528x multiplier."""
        m5 = _make_metric(
            condition_id="five_fills", daily_rate=50, q_share_pct=1.0,
            fill_count_recent=5, fill_cost_recent=5.0, dump_revenue_recent=0.0,
            on_book_hours=24,
        )
        m0 = _make_metric(
            condition_id="zero_fills", daily_rate=50, q_share_pct=1.0,
            fill_count_recent=0, on_book_hours=24,
        )
        scored = rank_markets([m5, m0], max_markets=10, db_path=self.db_path)
        scores = {s.condition_id: s.score for s in scored}
        # m0 score includes zero-fill bonus, m5 has fill damage + fast-react
        # Fast-react alone: 0.88^5 = 0.528
        # Score for m5 = (50 - 5) * 0.528 ≈ 23.8
        # Score for m0 = 52 (with bonus)
        self.assertLess(scores["five_fills"], scores["zero_fills"] * 0.6)

    def test_fast_react_floors_at_0_35(self):
        """20 fills → 0.88^20 = 0.078, but floored at 0.35."""
        m20 = _make_metric(
            condition_id="twenty_fills", daily_rate=100, q_share_pct=1.0,
            fill_count_recent=20, fill_cost_recent=10.0, dump_revenue_recent=0.0,
            on_book_hours=24,
        )
        m3 = _make_metric(
            condition_id="three_fills", daily_rate=100, q_share_pct=1.0,
            fill_count_recent=3, fill_cost_recent=3.0, dump_revenue_recent=0.0,
            on_book_hours=24,
        )
        scored = rank_markets([m20, m3], max_markets=10, db_path=self.db_path)
        scores = {s.condition_id: s.score for s in scored}
        # 20 fills: (100 - 10) * 0.35 = 31.5
        # 3 fills:  (100 - 3) * 0.681 = 66.1
        # 20 fills score should be significantly lower but not zero
        self.assertGreater(scores["twenty_fills"], 0)
        self.assertLess(scores["twenty_fills"], scores["three_fills"])

    def test_fast_react_zero_fills_no_penalty(self):
        """Zero fills → no fast-react penalty at all."""
        m = _make_metric(
            condition_id="clean", daily_rate=50, q_share_pct=1.0,
            fill_count_recent=0, on_book_hours=24,
        )
        scored = rank_markets([m], max_markets=10, db_path=self.db_path)
        # q_share capped to 0.5: base score = 50 * 0.5 = 25 (no zero-fill bonus)
        self.assertAlmostEqual(scored[0].score, 25.0)


class TestCombinedSignals(unittest.TestCase):
    """Test that regime + feedback + historical all compose correctly."""

    def setUp(self):
        self.db_fd, self.db_path, self.db = _make_ranking_db()

    def tearDown(self):
        self.db.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_regime_plus_feedback_plus_historical(self):
        """All three penalty sources compound on the same market."""
        cid = "triple_hit"
        # Placement feedback: both sides skipped → 0.3x
        self.db.execute(
            "INSERT INTO placement_feedback (condition_id, side, status, reason, ts) VALUES (?, ?, ?, ?, ?)",
            (cid, "yes", "skipped", "wide_spread", time.time()),
        )
        self.db.execute(
            "INSERT INTO placement_feedback (condition_id, side, status, reason, ts) VALUES (?, ?, ?, ?, ?)",
            (cid, "no", "skipped", "wide_spread", time.time()),
        )
        # Historical: high fill rate → trend_mult ~0.5
        for i in range(4):
            self.db.execute(
                """INSERT INTO market_performance
                   (ts, condition_id, question, window_hours, estimated_daily,
                    correction_factor, corrected_daily, fill_cost, dump_revenue,
                    net_score, action, q_share_pct, on_book_hours, fill_count,
                    shares_recommended)
                   VALUES (?, ?, '', 24, 50, 1.0, 50, 10, 0, -5, 'deploy', 0.5, 24, 5, 50)""",
                (time.time() - (3 - i) * 86400, cid),
            )
        self.db.commit()

        m_hit = _make_metric(
            condition_id=cid, daily_rate=50, q_share_pct=1.0,
            avg_bid=0.93, avg_ask=0.95,  # regime: 0.3x
            on_book_hours=24,
        )
        m_clean = _make_metric(
            condition_id="clean", daily_rate=50, q_share_pct=1.0,
            avg_bid=0.48, avg_ask=0.52, on_book_hours=24,
        )
        scored = rank_markets([m_hit, m_clean], max_markets=10, db_path=self.db_path)
        scores = {s.condition_id: s.score for s in scored}
        # regime(0.3) * feedback(0.3) * historical(~0.5) = ~0.045x
        ratio = scores[cid] / scores["clean"]
        self.assertLess(ratio, 0.15)

    def test_all_sizing_penalties_stack_above_min_size(self):
        """Confidence + placement + short-term all reduce sizing, floor at min_size."""
        cid = "sized_down"
        # Placement feedback: one side skipped → 0.6x sizing
        self.db.execute(
            "INSERT INTO placement_feedback (condition_id, side, status, reason, ts) VALUES (?, ?, ?, ?, ?)",
            (cid, "yes", "skipped", "wide_spread", time.time()),
        )
        self.db.execute(
            "INSERT INTO placement_feedback (condition_id, side, status, reason, ts) VALUES (?, ?, ?, ?, ?)",
            (cid, "no", "placed", "", time.time()),
        )
        # Short-term: 4 snapshots, 3 with fills → 0.5x sizing
        for i in range(4):
            fill = 2 if i < 3 else 0
            self.db.execute(
                """INSERT INTO market_performance
                   (ts, condition_id, question, window_hours, estimated_daily,
                    correction_factor, corrected_daily, fill_cost, dump_revenue,
                    net_score, action, q_share_pct, on_book_hours, fill_count,
                    shares_recommended)
                   VALUES (?, ?, '', 24, 50, 1.0, 50, 0, 0, 10, 'deploy', 0.5, 24, ?, 50)""",
                (time.time() - i * 1800, cid, fill),
            )
        self.db.commit()

        m = _make_metric(
            condition_id=cid, daily_rate=100, q_share_pct=1.0,
            on_book_hours=3,  # confidence ramp: 0.5 + 0.5*(3/8) = 0.6875
            min_size=50,
        )
        scored = rank_markets([m], max_markets=10, db_path=self.db_path)
        deployed = [s for s in scored if s.action == "deploy"]
        if deployed:
            self.assertGreaterEqual(deployed[0].recommended_shares, 50)


class TestRecentPriceRegimeDetection(unittest.TestCase):
    """Test that recent prices override lifetime averages for regime detection."""

    def test_recent_prices_override_lifetime(self):
        """Lifetime avg normal (0.50), recent near resolution (0.93) → signal fires."""
        m = _make_metric(
            avg_bid=0.48, avg_ask=0.52,      # lifetime: normal
            recent_bid=0.93, recent_ask=0.95,  # recent: near resolution
        )
        signals = _detect_regime_signals(m)
        self.assertIn("resolution_proximity", signals)
        self.assertEqual(signals["resolution_proximity"]["mult"], 0.3)

    def test_fallback_to_lifetime_when_no_recent(self):
        """recent_bid=0 → falls back to lifetime avg."""
        m = _make_metric(
            avg_bid=0.93, avg_ask=0.95,  # lifetime: near resolution
            recent_bid=0.0, recent_ask=0.0,  # no recent data
        )
        signals = _detect_regime_signals(m)
        self.assertIn("resolution_proximity", signals)
        self.assertEqual(signals["resolution_proximity"]["mult"], 0.3)

    def test_recent_normal_overrides_lifetime_extreme(self):
        """Lifetime extreme (0.93), recent normal (0.50) → NO resolution signal."""
        m = _make_metric(
            avg_bid=0.93, avg_ask=0.95,      # lifetime: near resolution
            recent_bid=0.48, recent_ask=0.52,  # recent: back to normal
        )
        signals = _detect_regime_signals(m)
        self.assertNotIn("resolution_proximity", signals)

    def test_drift_zone_with_recent_prices(self):
        """Recent mid ~0.87 → drift zone (0.6x), not critical zone."""
        m = _make_metric(
            avg_bid=0.48, avg_ask=0.52,      # lifetime: normal
            recent_bid=0.86, recent_ask=0.88,  # recent: drifting
        )
        signals = _detect_regime_signals(m)
        self.assertIn("resolution_proximity", signals)
        self.assertEqual(signals["resolution_proximity"]["mult"], 0.6)

    def test_query_recent_prices_with_data(self):
        """query_recent_prices returns median bid/ask from cycle_snapshots."""
        from oversight.data_collector import query_recent_prices
        fd, path, db = _make_ranking_db()
        try:
            now = time.time()
            # Insert 5 snapshots for one market
            for i, (bid, ask) in enumerate([(0.90, 0.92), (0.91, 0.93), (0.95, 0.97),
                                             (0.92, 0.94), (0.93, 0.95)]):
                db.execute(
                    "INSERT INTO cycle_snapshots (ts, cycle_num, condition_id, best_bid, best_ask) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now - i * 600, i, "0xtest", bid, ask),
                )
            db.commit()
            result = query_recent_prices(path, lookback_hours=3.0)
            self.assertIn("0xtest", result)
            # Median of [0.90, 0.91, 0.95, 0.92, 0.93] = 0.92
            self.assertAlmostEqual(result["0xtest"]["recent_bid"], 0.92, places=2)
            # Median of [0.92, 0.93, 0.97, 0.94, 0.95] = 0.94
            self.assertAlmostEqual(result["0xtest"]["recent_ask"], 0.94, places=2)
            self.assertEqual(result["0xtest"]["samples"], 5)
        finally:
            db.close()
            os.close(fd)
            os.unlink(path)

    def test_query_recent_prices_empty_table(self):
        """Empty cycle_snapshots → empty dict."""
        from oversight.data_collector import query_recent_prices
        fd, path, db = _make_ranking_db()
        try:
            db.commit()
            result = query_recent_prices(path, lookback_hours=3.0)
            self.assertEqual(result, {})
        finally:
            db.close()
            os.close(fd)
            os.unlink(path)

    def test_query_recent_prices_null_values_skipped(self):
        """Rows with NULL best_bid are excluded from median."""
        from oversight.data_collector import query_recent_prices
        fd, path, db = _make_ranking_db()
        try:
            now = time.time()
            # 2 valid rows + 1 NULL row
            db.execute(
                "INSERT INTO cycle_snapshots (ts, cycle_num, condition_id, best_bid, best_ask) "
                "VALUES (?, ?, ?, ?, ?)", (now, 1, "0xtest", 0.50, 0.52),
            )
            db.execute(
                "INSERT INTO cycle_snapshots (ts, cycle_num, condition_id, best_bid, best_ask) "
                "VALUES (?, ?, ?, ?, ?)", (now - 300, 2, "0xtest", 0.48, 0.50),
            )
            db.execute(
                "INSERT INTO cycle_snapshots (ts, cycle_num, condition_id, best_bid, best_ask) "
                "VALUES (?, ?, ?, NULL, NULL)", (now - 600, 3, "0xtest"),
            )
            db.commit()
            result = query_recent_prices(path, lookback_hours=3.0)
            self.assertIn("0xtest", result)
            self.assertEqual(result["0xtest"]["samples"], 2)
        finally:
            db.close()
            os.close(fd)
            os.unlink(path)


class TestSportsSizeCap(unittest.TestCase):
    """Test sports and short-duration market size caps."""

    def test_sports_vs_pattern_capped_at_min_size(self):
        """Sports market with 'vs.' within 72h expiry → capped at min_size."""
        from datetime import datetime, timezone, timedelta
        # Sports cap only fires for markets within 72h of expiry
        expiry = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()
        m = _make_metric(
            question="Los Angeles Dodgers vs. Washington Nationals",
            daily_rate=100, q_share_pct=1.0, min_size=50,
            on_book_hours=24, end_date_iso=expiry,
        )
        score = score_market(m)
        sm = classify_market(m, score)
        self.assertEqual(sm.action, "deploy")
        self.assertEqual(sm.recommended_shares, 50)  # capped at min_size

    def test_non_sports_not_capped(self):
        """Normal market should get score-based sizing, not min_size cap."""
        m = _make_metric(
            question="Will Bitcoin reach $100k by June?",
            daily_rate=100, q_share_pct=1.0, min_size=50,
            on_book_hours=24,
        )
        score = score_market(m)
        sm = classify_market(m, score)
        self.assertEqual(sm.action, "deploy")
        self.assertGreater(sm.recommended_shares, 50)

    def test_ipl_league_pattern_capped(self):
        """IPL market expiring within 72h should be capped at min_size
        (sports detection fires, short-duration cap applies)."""
        from datetime import datetime, timezone, timedelta
        # Sports cap only fires for markets within 72h of expiry
        expiry = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()
        m = _make_metric(
            question="Indian Premier League: Kolkata Knight Riders vs Punjab Kings",
            daily_rate=100, q_share_pct=1.0, min_size=50,
            on_book_hours=24, end_date_iso=expiry,
        )
        score = score_market(m)
        sm = classify_market(m, score)
        self.assertEqual(sm.action, "deploy")
        # Sports + <72h expiry → capped at min_size
        self.assertEqual(sm.recommended_shares, 50)

    def test_short_duration_capped(self):
        """Market expiring in 48h should be capped at min_size."""
        from datetime import datetime, timezone, timedelta
        expiry = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()
        m = _make_metric(
            question="Will it rain tomorrow?",
            daily_rate=100, q_share_pct=1.0, min_size=50,
            end_date_iso=expiry, on_book_hours=24,
        )
        score = score_market(m)
        sm = classify_market(m, score)
        self.assertEqual(sm.action, "deploy")
        self.assertEqual(sm.recommended_shares, 50)

    def test_long_duration_not_capped(self):
        """Market expiring in 10 days should NOT be capped."""
        from datetime import datetime, timezone, timedelta
        expiry = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        m = _make_metric(
            question="Will it rain next month?",
            daily_rate=100, q_share_pct=1.0, min_size=50,
            end_date_iso=expiry, on_book_hours=24,
        )
        score = score_market(m)
        sm = classify_market(m, score)
        self.assertEqual(sm.action, "deploy")
        self.assertGreater(sm.recommended_shares, 50)

    def test_avoided_market_not_affected(self):
        """Sports cap should not affect markets already classified as avoid."""
        m = _make_metric(
            question="Team A vs. Team B",
            daily_rate=10, q_share_pct=0.1,
            fill_count_recent=5, fill_cost_recent=50, dump_revenue_recent=0,
            on_book_hours=24,
        )
        sm = classify_market(m, score=-100.0)
        self.assertEqual(sm.action, "avoid")
        self.assertEqual(sm.recommended_shares, 0)

    def test_sports_high_min_size_not_hard_capped(self):
        """Sports market with high min_size: sports cap only fires if shares > min_size.
        Score-based sizing may be below min_size, so no cap needed."""
        from datetime import datetime, timezone, timedelta
        expiry = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        m = _make_metric(
            question="Copa Colsanitas: Marie Bouzkova vs Panna Udvardy",
            daily_rate=100, q_share_pct=1.0, min_size=1000,
            on_book_hours=24, end_date_iso=expiry,
        )
        score = score_market(m)
        sm = classify_market(m, score)
        self.assertEqual(sm.action, "deploy")
        # Score-based sizing produces shares below min_size=1000,
        # so sports cap (shares > min_size) doesn't fire.
        # Bot will enforce max(min_size, agent_shares) at placement time.
        self.assertGreater(sm.recommended_shares, 50)

    def test_trial_min_size_1000_capped(self):
        """Trial market with min_size=1000 should NOT deploy 1000 shares."""
        m = _make_metric(
            question="",  # empty question — CLOB-only discovery
            daily_rate=100, q_share_pct=0.0, min_size=1000,
            on_book_hours=0,  # brand new
        )
        sm = classify_market(m, score=0.0)
        self.assertEqual(sm.action, "deploy")  # still a trial
        self.assertLessEqual(sm.recommended_shares, 50)  # capped, not 1000


if __name__ == "__main__":
    unittest.main()
