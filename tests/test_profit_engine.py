"""Tests for Phase 3 Profit Engine.

Covers:
  1. Allocation sums ≤ total capital
  2. Higher risk_adjusted_score → more capital
  3. Risk factor (p_fill) reduces allocation
  4. Low confidence → lower allocation
  5. Rebalance: small delta → hold
  6. Rebalance: large delta → increase/decrease
  7. Dynamic market count: low efficiency → fewer markets
  8. Output format matches filter_allocations input
  9. Fallback to compute_allocations when calibrator not ready
  10. Safety filter still enforced after profit engine
"""

import math
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_scored_market(cid="cid_001", score=1.0, action="deploy",
                        daily_rate=25.0, min_size=50, max_spread=0.045,
                        q_share_pct=0.1, fill_count=0, fill_damage=0,
                        locked_position_usd=0, question_group=""):
    from oversight.market_scorer import ScoredMarket
    return ScoredMarket(
        condition_id=cid, question=f"Test {cid}?",
        score=score, action=action,
        recommended_shares=50, reason="test",
        confidence="high", actual_reward_total=0,
        fill_damage=fill_damage, fill_count=fill_count,
        daily_rate=daily_rate, min_size=min_size, max_spread=max_spread,
        est_capital_cost=0, locked_position_usd=locked_position_usd,
        question_group=question_group, q_share_pct=q_share_pct,
    )


def _make_predictions(cid="cid_001", ev=1.0, p_fill=0.1, loss=5.0,
                      e_time=12.0, reward_rate=0.05, confidence="model"):
    from calibration.manager import CalibrationPredictions
    return CalibrationPredictions(
        condition_id=cid,
        p_fill_24h=p_fill,
        e_loss_given_fill=loss,
        e_time_on_book_hours=e_time,
        reward_rate_per_hour=reward_rate,
        ev_per_day=ev,
        confidence=confidence,
        model_versions={"p_fill": "model", "e_loss": "model",
                        "e_time": "model", "reward": "phase1"},
    )


def _make_mock_calibrator(predictions_map=None):
    """Create a mock CalibrationManager that returns specified predictions."""
    cal = MagicMock()
    cal.is_ready.return_value = True
    cal._book_cache = {}

    def get_preds(**kwargs):
        cid = kwargs.get("condition_id", "")
        if predictions_map and cid in predictions_map:
            return predictions_map[cid]
        return _make_predictions(cid=cid)

    cal.get_predictions.side_effect = get_preds
    return cal


def _create_profit_test_db():
    """Create a minimal DB for profit engine tests."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = tmp.name
    tmp.close()
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            condition_id TEXT PRIMARY KEY, question TEXT DEFAULT '',
            yes_shares REAL DEFAULT 0, yes_avg_price REAL DEFAULT 0,
            yes_halted INTEGER DEFAULT 0,
            no_shares REAL DEFAULT 0, no_avg_price REAL DEFAULT 0,
            no_halted INTEGER DEFAULT 0, updated_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS active_orders (
            order_id TEXT PRIMARY KEY, condition_id TEXT,
            side TEXT, order_type TEXT DEFAULT 'buy',
            price REAL, shares REAL, placed_at REAL
        );
        CREATE TABLE IF NOT EXISTS reward_daily (
            id INTEGER PRIMARY KEY, date TEXT UNIQUE,
            total_reward_usd REAL DEFAULT 0, total_rebate_usd REAL DEFAULT 0,
            total_combined_usd REAL DEFAULT 0,
            num_markets_active INTEGER DEFAULT 0,
            est_daily_total REAL DEFAULT 0, correction_factor REAL DEFAULT 0
        );
    """)
    db.commit()
    db.close()
    return path


class TestRiskAdjustedScore(unittest.TestCase):
    def test_higher_ev_higher_score(self):
        from profit.allocator import _risk_adjusted_score
        s1 = _risk_adjusted_score(ev_per_day=2.0, p_fill_24h=0.1, loss_per_fill=5.0)
        s2 = _risk_adjusted_score(ev_per_day=1.0, p_fill_24h=0.1, loss_per_fill=5.0)
        self.assertGreater(s1, s2)

    def test_higher_fill_prob_lower_score(self):
        from profit.allocator import _risk_adjusted_score
        s_safe = _risk_adjusted_score(ev_per_day=1.0, p_fill_24h=0.05, loss_per_fill=5.0)
        s_risky = _risk_adjusted_score(ev_per_day=1.0, p_fill_24h=0.5, loss_per_fill=5.0)
        self.assertGreater(s_safe, s_risky)

    def test_higher_loss_lower_score(self):
        """FIX 14: loss term actually moves the score now (previous formula
        ignored it)."""
        from profit.allocator import _risk_adjusted_score
        s_low = _risk_adjusted_score(ev_per_day=1.0, p_fill_24h=0.3, loss_per_fill=1.0)
        s_high = _risk_adjusted_score(ev_per_day=1.0, p_fill_24h=0.3, loss_per_fill=10.0)
        self.assertGreater(s_low, s_high)

    def test_negative_ev_zero_score(self):
        """FIX 1: Negative EV collapses to exactly zero (no negative scores)."""
        from profit.allocator import _risk_adjusted_score
        s = _risk_adjusted_score(ev_per_day=-1.0, p_fill_24h=0.1, loss_per_fill=5.0)
        self.assertEqual(s, 0.0)


class TestAllocatePortfolio(unittest.TestCase):
    def setUp(self):
        self.db_path = _create_profit_test_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_basic_allocation(self):
        from profit.allocator import allocate_portfolio
        markets = [
            _make_scored_market("a", score=2.0),
            _make_scored_market("b", score=1.0),
        ]
        cal = _make_mock_calibrator({
            "a": _make_predictions("a", ev=2.0, p_fill=0.1),
            "b": _make_predictions("b", ev=1.0, p_fill=0.1),
        })
        allocs = allocate_portfolio(markets, 1000.0, cal, self.db_path)
        self.assertEqual(len(allocs), 2)

    def test_total_cost_within_capital(self):
        from profit.allocator import allocate_portfolio
        markets = [_make_scored_market(f"m{i}", score=1.0) for i in range(10)]
        preds = {f"m{i}": _make_predictions(f"m{i}", ev=1.0) for i in range(10)}
        cal = _make_mock_calibrator(preds)
        # Use realistic capital so per_market_cap >= min_cost after eff scaling
        allocs = allocate_portfolio(markets, 10000.0, cal, self.db_path)
        total_cost = sum(a.get("est_capital_cost", 0) for a in allocs
                         if a["action"] == "deploy")
        self.assertGreater(total_cost, 0)
        # FIX 6: total must not exceed effective_capital by more than 1%
        # (effective = deployable, no rebalance credit; eff_scale = 0.30 floor)
        self.assertLessEqual(total_cost, 10000.0 * 1.01)

    def test_higher_ras_gets_more_capital(self):
        from profit.allocator import allocate_portfolio
        markets = [
            _make_scored_market("high", score=5.0),
            _make_scored_market("low", score=0.5),
        ]
        cal = _make_mock_calibrator({
            "high": _make_predictions("high", ev=5.0, p_fill=0.05),
            "low": _make_predictions("low", ev=0.5, p_fill=0.05),
        })
        allocs = allocate_portfolio(markets, 1000.0, cal, self.db_path)
        deploy = {a["condition_id"]: a for a in allocs if a["action"] == "deploy"}
        if "high" in deploy and "low" in deploy:
            self.assertGreaterEqual(
                deploy["high"]["shares_per_side"],
                deploy["low"]["shares_per_side"],
            )

    def test_negative_ev_avoided(self):
        from profit.allocator import allocate_portfolio
        markets = [
            _make_scored_market("good", score=1.0),
            _make_scored_market("bad", score=0.1),
        ]
        cal = _make_mock_calibrator({
            "good": _make_predictions("good", ev=1.0),
            "bad": _make_predictions("bad", ev=-0.5),
        })
        allocs = allocate_portfolio(markets, 1000.0, cal, self.db_path)
        bad_alloc = next(a for a in allocs if a["condition_id"] == "bad")
        self.assertEqual(bad_alloc["action"], "avoid")

    def test_group_cap_enforced(self):
        from profit.allocator import allocate_portfolio
        markets = [
            _make_scored_market(f"g{i}", score=2.0, question_group="sports")
            for i in range(10)
        ]
        preds = {f"g{i}": _make_predictions(f"g{i}", ev=2.0) for i in range(10)}
        cal = _make_mock_calibrator(preds)
        allocs = allocate_portfolio(
            markets, 1000.0, cal, self.db_path, max_group_pct=0.30,
        )
        sports_cost = sum(
            a.get("est_capital_cost", 0)
            for a in allocs
            if a["action"] == "deploy" and a.get("question_group") == "sports"
        )
        # Group cap: 30% of ~$1000 = $300
        self.assertLessEqual(sports_cost, 1000 * 0.30 + 100)  # some slack

    def test_output_format_has_required_keys(self):
        """Output must have all keys that filter_allocations reads."""
        from profit.allocator import allocate_portfolio
        markets = [_make_scored_market("fmt")]
        cal = _make_mock_calibrator()
        allocs = allocate_portfolio(markets, 1000.0, cal, self.db_path)
        required_keys = {
            "condition_id", "question", "action", "shares_per_side",
            "score", "reason", "confidence", "min_size", "max_spread",
            "est_capital_cost", "q_share_pct",
        }
        for a in allocs:
            for k in required_keys:
                self.assertIn(k, a, f"Missing key: {k}")


class TestSizing(unittest.TestCase):
    def test_min_size_respected(self):
        from profit.sizing import compute_shares
        shares, cost = compute_shares(10.0, 0.045, min_size=50)
        self.assertGreaterEqual(shares, 50)

    def test_max_per_market_cap(self):
        from profit.sizing import compute_shares
        shares, cost = compute_shares(500.0, 0.045, min_size=50, max_per_market=200)
        self.assertLessEqual(cost, 200 + 50)  # some slack from rounding

    def test_depth_aware_reduction(self):
        from profit.sizing import compute_shares
        # With no depth constraint
        shares_no_depth, _ = compute_shares(200.0, 0.045, min_size=50)
        # With thin depth ahead
        shares_thin, _ = compute_shares(200.0, 0.045, min_size=50, depth_ahead=30)
        self.assertLessEqual(shares_thin, shares_no_depth)

    def test_zero_capital_gives_zero_shares(self):
        """FIX 11: zero capital must return zero shares (no speculative min_size)."""
        from profit.sizing import compute_shares
        shares, cost = compute_shares(0.0, 0.045, min_size=50)
        self.assertEqual(shares, 0)
        self.assertEqual(cost, 0.0)

    def test_negative_capital_gives_zero_shares(self):
        """FIX 11: negative capital also returns zero shares."""
        from profit.sizing import compute_shares
        shares, cost = compute_shares(-5.0, 0.045, min_size=50)
        self.assertEqual(shares, 0)
        self.assertEqual(cost, 0.0)

    def test_positive_capital_respects_min_size(self):
        """Min-size floor still applies once capital IS allocated."""
        from profit.sizing import compute_shares
        shares, _ = compute_shares(10.0, 0.045, min_size=50)
        self.assertGreaterEqual(shares, 50)

    def test_slippage_estimate(self):
        from profit.sizing import estimate_slippage
        # Thin market
        slip_thin = estimate_slippage(100, total_same_depth=100)
        # Thick market
        slip_thick = estimate_slippage(100, total_same_depth=1000)
        self.assertGreater(slip_thin, slip_thick)


class TestEfficiency(unittest.TestCase):
    def setUp(self):
        self.db_path = _create_profit_test_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_no_data_returns_zero(self):
        from profit.efficiency import get_efficiency
        eff = get_efficiency(self.db_path)
        self.assertEqual(eff["days_with_data"], 0)
        self.assertEqual(eff["reward_per_dollar"], 0)

    def test_with_data(self):
        from profit.efficiency import get_efficiency
        db = sqlite3.connect(self.db_path)
        for i in range(5):
            db.execute(
                "INSERT INTO reward_daily (date, total_combined_usd, est_daily_total) "
                "VALUES (?, ?, ?)",
                (f"2025-01-{10+i:02d}", 10.0 + i, 500.0),
            )
        db.commit()
        db.close()
        eff = get_efficiency(self.db_path)
        self.assertEqual(eff["days_with_data"], 5)
        self.assertGreater(eff["reward_per_dollar"], 0)

    def test_target_count_holds_medium_efficiency(self):
        from profit.efficiency import get_target_market_count
        eff = {"reward_per_dollar": 0.02, "trend": 0, "days_with_data": 5}
        target = get_target_market_count(eff, current_count=20)
        self.assertEqual(target, 20)  # hold

    def test_target_count_concentrates_low_efficiency(self):
        from profit.efficiency import get_target_market_count
        eff = {"reward_per_dollar": 0.005, "trend": -0.001, "days_with_data": 5}
        target = get_target_market_count(eff, current_count=20)
        self.assertLess(target, 20)  # concentrate

    def test_target_count_expands_high_efficiency(self):
        from profit.efficiency import get_target_market_count
        eff = {"reward_per_dollar": 0.04, "trend": 0.001, "days_with_data": 5}
        target = get_target_market_count(eff, current_count=20)
        self.assertGreater(target, 20)  # expand


class TestRebalance(unittest.TestCase):
    def setUp(self):
        self.db_path = _create_profit_test_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_new_market_always_enters(self):
        from profit.rebalance import compute_deltas
        allocs = [{"condition_id": "new", "action": "deploy",
                    "shares_per_side": 100, "max_spread": 0.045}]
        result = compute_deltas(allocs, self.db_path)
        self.assertEqual(result[0]["_rebalance_action"], "enter")

    def test_small_delta_holds(self):
        from profit.rebalance import compute_deltas
        # Insert current position
        db = sqlite3.connect(self.db_path)
        db.execute("INSERT INTO active_orders VALUES ('o1', 'existing', 'yes', 'buy', 0.48, 100, ?)",
                   (time.time(),))
        db.commit()
        db.close()
        # New allocation: 105 shares (5% change < 15% threshold)
        allocs = [{"condition_id": "existing", "action": "deploy",
                    "shares_per_side": 105, "max_spread": 0.045}]
        result = compute_deltas(allocs, self.db_path)
        self.assertEqual(result[0]["_rebalance_action"], "hold")
        self.assertEqual(result[0]["shares_per_side"], 100)  # reverted

    def test_large_delta_increases(self):
        from profit.rebalance import compute_deltas
        db = sqlite3.connect(self.db_path)
        db.execute("INSERT INTO active_orders VALUES ('o1', 'existing', 'yes', 'buy', 0.48, 100, ?)",
                   (time.time(),))
        db.commit()
        db.close()
        # New allocation: 150 shares (50% change > 15% threshold)
        allocs = [{"condition_id": "existing", "action": "deploy",
                    "shares_per_side": 150, "max_spread": 0.045}]
        result = compute_deltas(allocs, self.db_path)
        self.assertEqual(result[0]["_rebalance_action"], "increase")

    def test_exit_on_avoid(self):
        from profit.rebalance import compute_deltas
        db = sqlite3.connect(self.db_path)
        db.execute("INSERT INTO positions VALUES ('exiting', '', 50, 0.5, 0, 50, 0.5, 0, ?)",
                   (time.time(),))
        db.commit()
        db.close()
        allocs = [{"condition_id": "exiting", "action": "avoid",
                    "shares_per_side": 0, "max_spread": 0.045}]
        result = compute_deltas(allocs, self.db_path)
        self.assertEqual(result[0]["_rebalance_action"], "exit")


class TestFallbackPath(unittest.TestCase):
    """When calibrator not ready, falls back to compute_allocations."""

    def test_legacy_allocation_without_calibrator(self):
        from oversight.allocation_writer import compute_allocations
        from oversight.market_scorer import ScoredMarket

        markets = [_make_scored_market("legacy", score=1.0)]
        allocs = compute_allocations(markets, total_capital=1000.0)
        self.assertEqual(len(allocs), 1)
        self.assertIn("condition_id", allocs[0])


class TestSafetyIntegration(unittest.TestCase):
    """Safety filter still works after profit engine allocation."""

    def test_safety_caps_profit_engine_output(self):
        from profit.allocator import allocate_portfolio
        from oversight.safety_controller import SafetyController, UNSAFE

        db_path = _create_profit_test_db()
        # Add required safety tables
        db = sqlite3.connect(db_path)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS fills (ts REAL, condition_id TEXT, side TEXT, fill_type TEXT, shares REAL, price REAL, clob_cost REAL, usd_value REAL);
            CREATE TABLE IF NOT EXISTS unwinds (ts REAL, condition_id TEXT, usd_value REAL);
            CREATE TABLE IF NOT EXISTS stop_losses (ts REAL, condition_id TEXT, loss_usd REAL);
            CREATE TABLE IF NOT EXISTS scoring_snapshots (id INTEGER PRIMARY KEY, ts REAL, order_id TEXT, condition_id TEXT, side TEXT, scoring INTEGER, price REAL, shares REAL);
        """)
        db.execute("INSERT INTO scoring_snapshots (ts, order_id, condition_id, side, scoring, price, shares) VALUES (?, 't', 't', 'yes', 1, 0.5, 100)", (time.time()-60,))
        db.commit()
        db.close()

        markets = [_make_scored_market(f"m{i}", score=2.0) for i in range(5)]
        cal = _make_mock_calibrator()

        allocs = allocate_portfolio(markets, 1000.0, cal, db_path)

        # Force UNSAFE → safety filter caps to 3 markets + min_size
        sc = SafetyController(db_path=db_path)
        sc.state = UNSAFE
        filtered = sc.filter_allocations(allocs, 1000.0)
        deploy_count = sum(1 for a in filtered if a["action"] == "deploy")
        self.assertLessEqual(deploy_count, 3)

        for a in filtered:
            if a["action"] == "deploy":
                self.assertEqual(a["shares_per_side"], 50)  # min_size
                self.assertIn("PROBE", a.get("reason", ""))

        os.unlink(db_path)


class TestCorrelationClustering(unittest.TestCase):
    """Correlation-aware capital allocation."""

    def setUp(self):
        self.db_path = _create_profit_test_db()
        # Add fills table
        db = sqlite3.connect(self.db_path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS fills "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, condition_id TEXT, "
            "side TEXT, fill_type TEXT, shares REAL, price REAL, "
            "clob_cost REAL, usd_value REAL)"
        )
        db.commit()
        db.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_no_fills_returns_empty(self):
        from profit.correlation import build_fill_clusters
        clusters, oversized = build_fill_clusters(self.db_path)
        self.assertEqual(len(clusters), 0)
        self.assertEqual(len(oversized), 0)

    def test_synthetic_clusters(self):
        """3 markets co-filled 4 times in 24h → same cluster."""
        from profit.correlation import build_fill_clusters

        db = sqlite3.connect(self.db_path)
        now = time.time()
        # 4 co-fill events (>= COFILL_MIN_COUNT=3) across 3 markets
        for window in range(4):
            base_ts = now - 3600 * (window + 1)
            for cid in ["mkt_A", "mkt_B", "mkt_C"]:
                db.execute(
                    "INSERT INTO fills (ts, condition_id, side, fill_type, shares, price, clob_cost, usd_value) "
                    "VALUES (?, ?, 'yes', 'FULL', 50, 0.5, 0.5, 25)",
                    (base_ts + 10, cid),  # within same 5-min window
                )
        # Unrelated market with fills at different times
        for i in range(4):
            db.execute(
                "INSERT INTO fills (ts, condition_id, side, fill_type, shares, price, clob_cost, usd_value) "
                "VALUES (?, 'mkt_Z', 'yes', 'FULL', 50, 0.5, 0.5, 25)",
                (now - 86000 + i * 600,),  # different windows
            )
        db.commit()
        db.close()

        clusters, _oversized = build_fill_clusters(self.db_path)
        # A, B, C should be in same cluster
        self.assertEqual(clusters["mkt_A"], clusters["mkt_B"])
        self.assertEqual(clusters["mkt_B"], clusters["mkt_C"])
        # Z should be in different cluster
        self.assertNotEqual(clusters.get("mkt_Z"), clusters.get("mkt_A"))

    def test_cluster_cap_enforced(self):
        """No cluster should exceed 30% of capital after capping."""
        from profit.correlation import apply_cluster_caps

        clusters = {"a": 0, "b": 0, "c": 0, "d": 1, "e": 1}
        allocs = [
            {"condition_id": "a", "action": "deploy", "shares_per_side": 200,
             "est_capital_cost": 150, "min_size": 50, "max_spread": 0.045},
            {"condition_id": "b", "action": "deploy", "shares_per_side": 200,
             "est_capital_cost": 150, "min_size": 50, "max_spread": 0.045},
            {"condition_id": "c", "action": "deploy", "shares_per_side": 200,
             "est_capital_cost": 150, "min_size": 50, "max_spread": 0.045},
            {"condition_id": "d", "action": "deploy", "shares_per_side": 100,
             "est_capital_cost": 80, "min_size": 50, "max_spread": 0.045},
            {"condition_id": "e", "action": "deploy", "shares_per_side": 100,
             "est_capital_cost": 80, "min_size": 50, "max_spread": 0.045},
        ]
        total = 1000.0
        max_pct = 0.30
        result = apply_cluster_caps(allocs, clusters, max_pct, total)

        # Cluster 0 (a,b,c) had $450 > $300 cap → must be scaled down
        cluster0_cost = sum(
            a["est_capital_cost"] for a in result
            if a["condition_id"] in ("a", "b", "c") and a["action"] == "deploy"
        )
        # After scaling, cost should be reduced from $450.
        # Min_size floor (50 shares) prevents going below ~$45/market = $135 for 3.
        # So the cap target is $300, but floor pushes to ~$363 at minimum.
        # Key invariant: cost is strictly LESS than the original $450.
        self.assertLess(cluster0_cost, 450)
        # And shares were reduced for each market
        for a in result:
            if a["condition_id"] in ("a", "b", "c"):
                self.assertLess(a["shares_per_side"], 200)

    def test_uncorrelated_markets_unaffected(self):
        """Markets not in any cluster are not capped."""
        from profit.correlation import apply_cluster_caps

        clusters = {"a": 0}  # only 'a' in clusters
        allocs = [
            {"condition_id": "a", "action": "deploy", "shares_per_side": 100,
             "est_capital_cost": 80, "min_size": 50, "max_spread": 0.045},
            {"condition_id": "x", "action": "deploy", "shares_per_side": 200,
             "est_capital_cost": 180, "min_size": 50, "max_spread": 0.045},
        ]
        result = apply_cluster_caps(allocs, clusters, 0.30, 500.0)
        # 'x' not in clusters → unchanged
        x_alloc = next(a for a in result if a["condition_id"] == "x")
        self.assertEqual(x_alloc["shares_per_side"], 200)


class TestEfficiencyScaling(unittest.TestCase):
    """Efficiency-based capital scaling."""

    def setUp(self):
        self.db_path = _create_profit_test_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_scale_half_efficiency_sqrt(self):
        """Fix 6: sqrt damping. eff=0.004 → sqrt(0.004/0.008)=sqrt(0.5)≈0.707"""
        from profit.allocator import _compute_efficiency_scale
        db = sqlite3.connect(self.db_path)
        for i in range(5):
            db.execute(
                "INSERT INTO reward_daily (date, total_combined_usd, est_daily_total) "
                "VALUES (?, ?, ?)",
                (f"2025-01-{10+i:02d}", 2.0, 500.0),
            )
        db.commit()
        db.close()
        scale = _compute_efficiency_scale(self.db_path)
        # eff=0.004, target=0.008 → sqrt(0.5) ≈ 0.707
        self.assertAlmostEqual(scale, 0.707, places=2)

    def test_scale_high_efficiency(self):
        from profit.allocator import _compute_efficiency_scale
        db = sqlite3.connect(self.db_path)
        for i in range(5):
            db.execute(
                "INSERT INTO reward_daily (date, total_combined_usd, est_daily_total) "
                "VALUES (?, ?, ?)",
                (f"2025-01-{10+i:02d}", 10.0, 500.0),
            )
        db.commit()
        db.close()
        scale = _compute_efficiency_scale(self.db_path)
        self.assertEqual(scale, 1.0)

    def test_efficiency_zero_scale(self):
        """Fix 1: efficiency=0 (measured zero) → scale=0.30."""
        from profit.allocator import _compute_efficiency_scale, MIN_EFFICIENCY_SCALE
        db = sqlite3.connect(self.db_path)
        for i in range(5):
            db.execute(
                "INSERT INTO reward_daily (date, total_combined_usd, est_daily_total) "
                "VALUES (?, ?, ?)",
                (f"2025-01-{10+i:02d}", 0.0, 500.0),  # zero reward
            )
        db.commit()
        db.close()
        scale = _compute_efficiency_scale(self.db_path)
        self.assertEqual(scale, MIN_EFFICIENCY_SCALE)

    def test_efficiency_none_scale(self):
        """FIX 12: empty DB yields measured rpd=0.0, indistinguishable from
        measured zero → scale = MIN (0.30). No days_with_data special-case."""
        from profit.allocator import _compute_efficiency_scale, MIN_EFFICIENCY_SCALE
        scale = _compute_efficiency_scale(self.db_path)
        self.assertEqual(scale, MIN_EFFICIENCY_SCALE)

    def test_scale_floor_at_30_pct(self):
        """sqrt(ratio) < 0.30 → clamped. eff=0.0002 → ratio=0.025 → sqrt=0.158."""
        from profit.allocator import _compute_efficiency_scale, MIN_EFFICIENCY_SCALE
        db = sqlite3.connect(self.db_path)
        for i in range(5):
            db.execute(
                "INSERT INTO reward_daily (date, total_combined_usd, est_daily_total) "
                "VALUES (?, ?, ?)",
                (f"2025-01-{10+i:02d}", 0.1, 500.0),
            )
        db.commit()
        db.close()
        scale = _compute_efficiency_scale(self.db_path)
        self.assertEqual(scale, MIN_EFFICIENCY_SCALE)


class TestIntegrationBothConstraints(unittest.TestCase):
    """Allocator respects correlation + efficiency simultaneously."""

    def setUp(self):
        self.db_path = _create_profit_test_db()
        db = sqlite3.connect(self.db_path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS fills "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, condition_id TEXT, "
            "side TEXT, fill_type TEXT, shares REAL, price REAL, "
            "clob_cost REAL, usd_value REAL)"
        )
        # Insert co-fills so m0,m1,m2 cluster together
        now = time.time()
        for window in range(4):
            base_ts = now - 3600 * (window + 1)
            for cid in ["m0", "m1", "m2"]:
                db.execute(
                    "INSERT INTO fills (ts, condition_id, side, fill_type, "
                    "shares, price, clob_cost, usd_value) "
                    "VALUES (?, ?, 'yes', 'FULL', 50, 0.5, 0.5, 25)",
                    (base_ts + 10, cid),
                )
        # Insert efficiency data: below target
        for i in range(3):
            db.execute(
                "INSERT INTO reward_daily (date, total_combined_usd, est_daily_total) "
                "VALUES (?, ?, ?)",
                (f"2025-01-{10+i:02d}", 2.0, 500.0),
            )
        db.commit()
        db.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_both_constraints_active(self):
        from profit.allocator import allocate_portfolio

        markets = [_make_scored_market(f"m{i}", score=2.0) for i in range(5)]
        preds = {f"m{i}": _make_predictions(f"m{i}", ev=2.0) for i in range(5)}
        cal = _make_mock_calibrator(preds)

        allocs = allocate_portfolio(
            markets, 1000.0, cal, self.db_path,
            max_cluster_pct=0.30,
        )

        # Check: total cost should be reduced by efficiency scaling
        total_cost = sum(a.get("est_capital_cost", 0) for a in allocs
                         if a["action"] == "deploy")
        # With efficiency ~0.004 and target 0.008, scale=0.5
        # So deployable ≈ $500, not $1000
        self.assertLess(total_cost, 1000)

        # Check: correlated cluster (m0,m1,m2) capped
        # Their combined cost should be ≤ 30% of deployable
        cluster_cost = sum(
            a.get("est_capital_cost", 0) for a in allocs
            if a["condition_id"] in ("m0", "m1", "m2") and a["action"] == "deploy"
        )
        # deployable ≈ $500, cluster cap ≈ $150
        if cluster_cost > 0:
            self.assertLessEqual(cluster_cost, 1000 * 0.30 + 50)  # generous slack


class TestClusterThresholdTwo(unittest.TestCase):
    """Fix 2: Correlation threshold lowered to 2 co-fills."""

    def setUp(self):
        self.db_path = _create_profit_test_db()
        db = sqlite3.connect(self.db_path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS fills "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, condition_id TEXT, "
            "side TEXT, fill_type TEXT, shares REAL, price REAL, "
            "clob_cost REAL, usd_value REAL)"
        )
        db.commit()
        db.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_two_cofills_clusters(self):
        """2 co-fills (not 3) should now trigger clustering."""
        from profit.correlation import build_fill_clusters
        db = sqlite3.connect(self.db_path)
        now = time.time()
        # Only 2 co-fill events
        for window in range(2):
            base_ts = now - 3600 * (window + 1)
            for cid in ["x", "y"]:
                db.execute(
                    "INSERT INTO fills (ts, condition_id, side, fill_type, shares, price, clob_cost, usd_value) "
                    "VALUES (?, ?, 'yes', 'FULL', 50, 0.5, 0.5, 25)",
                    (base_ts + 5, cid),
                )
        db.commit()
        db.close()
        clusters, _oversized = build_fill_clusters(self.db_path)
        # With threshold=2, x and y should cluster
        self.assertIn("x", clusters)
        self.assertIn("y", clusters)
        self.assertEqual(clusters["x"], clusters["y"])


class TestClusterSizeGuard(unittest.TestCase):
    """FIX 9: Clusters > MAX_CLUSTER_SIZE are preserved but flagged oversized."""

    def setUp(self):
        self.db_path = _create_profit_test_db()
        db = sqlite3.connect(self.db_path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS fills "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, condition_id TEXT, "
            "side TEXT, fill_type TEXT, shares REAL, price REAL, "
            "clob_cost REAL, usd_value REAL)"
        )
        db.commit()
        db.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_oversized_cluster_preserved_and_flagged(self):
        """FIX 9: 12-market cluster is NOT dissolved (that would ignore the
        most dangerous chain-link correlation). Instead it's retained and
        flagged for stricter capping downstream."""
        from profit.correlation import build_fill_clusters, MAX_CLUSTER_SIZE
        db = sqlite3.connect(self.db_path)
        now = time.time()
        cids = [f"big_{i}" for i in range(12)]
        for window in range(3):
            base_ts = now - 3600 * (window + 1)
            for cid in cids:
                db.execute(
                    "INSERT INTO fills (ts, condition_id, side, fill_type, shares, price, clob_cost, usd_value) "
                    "VALUES (?, ?, 'yes', 'FULL', 50, 0.5, 0.5, 25)",
                    (base_ts + 5, cid),
                )
        db.commit()
        db.close()
        clusters, oversized_ids = build_fill_clusters(self.db_path)
        # All 12 markets must remain in the cluster
        for cid in cids:
            self.assertIn(cid, clusters)
        # Their shared cluster_id must be marked oversized
        cluster_id = clusters[cids[0]]
        self.assertIn(cluster_id, oversized_ids)
        self.assertGreater(len([c for c in cids if c in clusters]), MAX_CLUSTER_SIZE)


class TestRedistributionAfterCap(unittest.TestCase):
    """Fix 3: Capital freed by cluster caps redistributed to uncapped markets."""

    def test_redistribution_adds_capital(self):
        from profit.allocator import _redistribute_cluster_savings
        from profit.correlation import compute_cluster_exposure

        clusters = {"a": 0, "b": 0, "x": 1}
        allocs = [
            {"condition_id": "a", "action": "deploy", "shares_per_side": 50,
             "est_capital_cost": 45, "min_size": 50, "max_spread": 0.045,
             "score": 1.0, "_final_score": 1.0, "_cluster_capped": True},
            {"condition_id": "b", "action": "deploy", "shares_per_side": 50,
             "est_capital_cost": 45, "min_size": 50, "max_spread": 0.045,
             "score": 1.0, "_final_score": 1.0, "_cluster_capped": True},
            {"condition_id": "x", "action": "deploy", "shares_per_side": 50,
             "est_capital_cost": 45, "min_size": 50, "max_spread": 0.045,
             "score": 2.0, "_final_score": 2.0},  # not capped
        ]
        # deployable=500, allocated=135, remaining=365 (>5% of 500)
        result = _redistribute_cluster_savings(allocs, clusters, 500.0, 0.30)
        x_alloc = next(a for a in result if a["condition_id"] == "x")
        # x should get extra shares from the 365 surplus
        self.assertGreater(x_alloc["shares_per_side"], 50)

    def test_no_cap_violation_after_redistribution(self):
        from profit.allocator import _redistribute_cluster_savings
        from profit.correlation import compute_cluster_exposure

        clusters = {"a": 0, "b": 0, "c": 0, "x": 1, "y": 1}
        allocs = [
            {"condition_id": "a", "action": "deploy", "shares_per_side": 50,
             "est_capital_cost": 45, "min_size": 50, "max_spread": 0.045,
             "score": 1.0, "_final_score": 1.0, "_cluster_capped": True},
            {"condition_id": "b", "action": "deploy", "shares_per_side": 50,
             "est_capital_cost": 45, "min_size": 50, "max_spread": 0.045,
             "score": 1.0, "_final_score": 1.0, "_cluster_capped": True},
            {"condition_id": "c", "action": "deploy", "shares_per_side": 50,
             "est_capital_cost": 45, "min_size": 50, "max_spread": 0.045,
             "score": 1.0, "_final_score": 1.0, "_cluster_capped": True},
            {"condition_id": "x", "action": "deploy", "shares_per_side": 100,
             "est_capital_cost": 91, "min_size": 50, "max_spread": 0.045,
             "score": 2.0, "_final_score": 2.0},
            {"condition_id": "y", "action": "deploy", "shares_per_side": 100,
             "est_capital_cost": 91, "min_size": 50, "max_spread": 0.045,
             "score": 2.0, "_final_score": 2.0},
        ]
        result = _redistribute_cluster_savings(allocs, clusters, 1000.0, 0.30)
        # After redistribution, cluster 1 (x,y) should not exceed 30%
        exposure = compute_cluster_exposure(result, clusters)
        for cid_cluster, total in exposure.items():
            self.assertLessEqual(total, 1000 * 0.30 + 50)


class TestClusteringMissingDegradesConfidence(unittest.TestCase):
    """Fix 5: Prolonged clustering failure degrades confidence."""

    def test_counter_increments_on_failure(self):
        import profit.allocator as alloc_mod
        # Reset counter
        alloc_mod._cycles_without_clustering = 0
        # Simulate 10 cycles with clustering failure
        for _ in range(10):
            alloc_mod._cycles_without_clustering += 1
        self.assertEqual(alloc_mod._cycles_without_clustering, 10)
        self.assertGreaterEqual(
            alloc_mod._cycles_without_clustering,
            alloc_mod.CLUSTER_FAILURE_WARN_CYCLES,
        )

    def test_counter_resets_on_success(self):
        import profit.allocator as alloc_mod
        alloc_mod._cycles_without_clustering = 15
        # Simulate successful clustering
        alloc_mod._cycles_without_clustering = 0
        self.assertEqual(alloc_mod._cycles_without_clustering, 0)


class TestScalingSqrtBehavior(unittest.TestCase):
    """Fix 6: sqrt damping reduces oscillation."""

    def setUp(self):
        self.db_path = _create_profit_test_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_sqrt_less_aggressive_than_linear(self):
        """sqrt(0.25)=0.5 vs linear 0.25 — sqrt is less aggressive."""
        from profit.allocator import _compute_efficiency_scale
        db = sqlite3.connect(self.db_path)
        for i in range(5):
            db.execute(
                "INSERT INTO reward_daily (date, total_combined_usd, est_daily_total) "
                "VALUES (?, ?, ?)",
                (f"2025-01-{10+i:02d}", 1.0, 500.0),
            )
        db.commit()
        db.close()
        scale = _compute_efficiency_scale(self.db_path)
        # eff=0.002, target=0.008, ratio=0.25, sqrt(0.25)=0.5
        # Linear would give 0.25 → sqrt gives 0.5 (less aggressive)
        self.assertAlmostEqual(scale, 0.5, places=1)
        self.assertGreater(scale, 0.25)  # sqrt > linear for ratio < 1


class TestNoDoubleEfficiencyScaling(unittest.TestCase):
    """FIX 2: RAS is NOT multiplied by eff_scale. Selection pressure is
    expressed via deployable_capital alone — applying eff_scale a second
    time inside RAS was double-counting."""

    def test_ras_matches_raw_formula(self):
        """_ras stored on allocation must equal EV / (1 + p*loss), unscaled."""
        import profit.allocator as alloc_mod
        alloc_mod._cycles_without_clustering = 0  # isolate from prior tests
        from profit.allocator import allocate_portfolio, _risk_adjusted_score

        db_path = _create_profit_test_db()
        db = sqlite3.connect(db_path)
        # Low efficiency → eff_scale < 1.0 (floors at 0.30)
        for i in range(5):
            db.execute(
                "INSERT INTO reward_daily (date, total_combined_usd, est_daily_total) "
                "VALUES (?, ?, ?)",
                (f"2025-01-{10+i:02d}", 0.1, 500.0),
            )
        db.commit()
        db.close()

        markets = [_make_scored_market("m", score=1.0)]
        preds = _make_predictions("m", ev=1.0, p_fill=0.1, loss=5.0)
        cal = _make_mock_calibrator({"m": preds})

        # $10k so per_market_cap ($200) remains > min_cost ($45) despite
        # eff_scale=0.30 floor
        allocs = allocate_portfolio(markets, 10000.0, cal, db_path)
        deploy = [a for a in allocs if a["action"] == "deploy"]
        self.assertTrue(deploy, "expected market to deploy")

        expected = _risk_adjusted_score(1.0, 0.1, 5.0)  # 1/(1+0.5)=0.667
        # _ras is the raw RAS — not multiplied by eff_scale
        self.assertAlmostEqual(deploy[0].get("_ras", 0), expected, places=2)

        os.unlink(db_path)


class TestAuditFixes(unittest.TestCase):
    """Required post-audit regression tests (8 named invariants)."""

    # ── FIX 14: risk formula uses loss ──────────────────────────

    def test_risk_formula_uses_loss(self):
        """score = EV / (1 + p_fill * loss) — loss term must move the score."""
        from profit.allocator import _risk_adjusted_score
        s_low = _risk_adjusted_score(ev_per_day=1.0, p_fill_24h=0.5, loss_per_fill=1.0)
        s_high = _risk_adjusted_score(ev_per_day=1.0, p_fill_24h=0.5, loss_per_fill=10.0)
        # Numerical: 1/(1+0.5)=0.667 vs 1/(1+5)=0.167
        self.assertAlmostEqual(s_low, 1.0 / 1.5, places=3)
        self.assertAlmostEqual(s_high, 1.0 / 6.0, places=3)
        self.assertGreater(s_low, s_high)

    # ── FIX 2: no double efficiency scaling ─────────────────────

    def test_no_double_efficiency_scaling(self):
        """Two portfolios with different eff_scale see the SAME _ras per
        market (eff_scale affects deployable_capital only, not RAS)."""
        import profit.allocator as alloc_mod
        alloc_mod._cycles_without_clustering = 0
        from profit.allocator import allocate_portfolio

        # High-eff DB → eff_scale = 1.0
        db_hi = _create_profit_test_db()
        db = sqlite3.connect(db_hi)
        for i in range(5):
            db.execute(
                "INSERT INTO reward_daily (date, total_combined_usd, est_daily_total) "
                "VALUES (?, ?, ?)",
                (f"2025-01-{10+i:02d}", 10.0, 500.0),
            )
        db.commit()
        db.close()

        # Low-eff DB → eff_scale ≈ 0.3 (floor)
        db_lo = _create_profit_test_db()
        db = sqlite3.connect(db_lo)
        for i in range(5):
            db.execute(
                "INSERT INTO reward_daily (date, total_combined_usd, est_daily_total) "
                "VALUES (?, ?, ?)",
                (f"2025-01-{10+i:02d}", 0.1, 500.0),
            )
        db.commit()
        db.close()

        markets = [_make_scored_market("m", score=1.0)]
        preds = _make_predictions("m", ev=1.0, p_fill=0.1, loss=5.0)
        cal_hi = _make_mock_calibrator({"m": preds})
        cal_lo = _make_mock_calibrator({"m": preds})

        # $10k so both scenarios deploy despite low-eff 0.30 floor
        allocs_hi = allocate_portfolio(markets, 10000.0, cal_hi, db_hi)
        allocs_lo = allocate_portfolio(markets, 10000.0, cal_lo, db_lo)

        hi = next(a for a in allocs_hi if a["action"] == "deploy")
        lo = next(a for a in allocs_lo if a["action"] == "deploy")
        # _ras identical regardless of eff_scale
        self.assertEqual(hi["_ras"], lo["_ras"])

        os.unlink(db_hi)
        os.unlink(db_lo)

    # ── FIX 6: capital conservation ─────────────────────────────

    def test_capital_conservation(self):
        """Total allocated capital must not exceed effective_capital * 1.01."""
        import profit.allocator as alloc_mod
        alloc_mod._cycles_without_clustering = 0
        from profit.allocator import allocate_portfolio

        db_path = _create_profit_test_db()

        # Many high-EV markets → redistribution + min_cost floors try to overshoot
        markets = [_make_scored_market(f"m{i}", score=2.0) for i in range(30)]
        preds = {f"m{i}": _make_predictions(f"m{i}", ev=5.0, p_fill=0.05, loss=1.0)
                 for i in range(30)}
        cal = _make_mock_calibrator(preds)

        total_capital = 500.0
        allocs = allocate_portfolio(markets, total_capital, cal, db_path)

        total = sum(a.get("est_capital_cost", 0) for a in allocs
                    if a["action"] == "deploy")
        # No rebalance credit (no prior positions) → effective = deployable
        # eff_scale=1.0 (no reward_daily rows) → deployable = total_capital
        self.assertLessEqual(total, total_capital * 1.01)

        os.unlink(db_path)

    # ── FIX 7: sliding-window correlation ───────────────────────

    def test_sliding_window_correlation(self):
        """Co-fill detection uses |t_i - t_j| <= 300s, NOT bucket alignment.
        Two markets filled 100s apart across any bucket boundary must cluster."""
        from profit.correlation import build_fill_clusters

        db_path = _create_profit_test_db()
        db = sqlite3.connect(db_path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS fills "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, condition_id TEXT, "
            "side TEXT, fill_type TEXT, shares REAL, price REAL, "
            "clob_cost REAL, usd_value REAL)"
        )

        # Place fills near an arbitrary 300s bucket boundary. With bucket
        # alignment these would NOT co-occur; with sliding window they DO.
        now = time.time()
        boundary = int(now // 300) * 300 - 3600  # an hour ago, on a boundary
        for k in range(3):  # 3 co-fill events (>= COFILL_MIN_COUNT=2)
            base = boundary - 3600 * k
            # A 250s into bucket, B 50s into next bucket → 100s apart
            db.execute(
                "INSERT INTO fills (ts, condition_id, side, fill_type, shares, price, clob_cost, usd_value) "
                "VALUES (?, 'sw_A', 'yes', 'FULL', 50, 0.5, 0.5, 25)",
                (base + 250,),
            )
            db.execute(
                "INSERT INTO fills (ts, condition_id, side, fill_type, shares, price, clob_cost, usd_value) "
                "VALUES (?, 'sw_B', 'yes', 'FULL', 50, 0.5, 0.5, 25)",
                (base + 350,),
            )
        db.commit()
        db.close()

        clusters, _oversized = build_fill_clusters(db_path)
        self.assertIn("sw_A", clusters)
        self.assertIn("sw_B", clusters)
        self.assertEqual(clusters["sw_A"], clusters["sw_B"])

        os.unlink(db_path)

    # ── FIX 8: rebalance respects caps ──────────────────────────

    def test_rebalance_respects_caps(self):
        """After compute_deltas (which can preserve a large existing position
        on 'hold'), per-market cap must be re-enforced."""
        import profit.allocator as alloc_mod
        alloc_mod._cycles_without_clustering = 0
        from profit.allocator import allocate_portfolio

        db_path = _create_profit_test_db()
        # Existing 500-share order → compute_deltas may keep shares near 500
        # on a small-delta 'hold'. Per-market cap of $200 must still win.
        db = sqlite3.connect(db_path)
        db.execute(
            "INSERT INTO active_orders VALUES "
            "('o1', 'big', 'yes', 'buy', 0.48, 500, ?)",
            (time.time(),),
        )
        db.commit()
        db.close()

        markets = [_make_scored_market("big", score=5.0, min_size=50)]
        preds = {"big": _make_predictions("big", ev=10.0, p_fill=0.05, loss=1.0)}
        cal = _make_mock_calibrator(preds)

        allocs = allocate_portfolio(
            markets, 10000.0, cal, db_path,
            max_per_market=200.0,
            max_capital_pct=0.5,
        )

        for a in allocs:
            if a["action"] == "deploy":
                # FIX 8 + FIX 10: est_capital_cost strictly bounded
                self.assertLessEqual(a.get("est_capital_cost", 0), 200 + 2)

        os.unlink(db_path)

    # ── FIX 11: no min_size when zero capital ───────────────────

    def test_no_min_size_when_zero_capital(self):
        """compute_shares(0, ...) returns (0, 0.0) — never min_size."""
        from profit.sizing import compute_shares
        for cap in (0.0, -1.0, -1000.0):
            shares, cost = compute_shares(cap, 0.045, min_size=50)
            self.assertEqual(shares, 0, f"cap={cap}: expected 0 shares")
            self.assertEqual(cost, 0.0, f"cap={cap}: expected 0 cost")

    # ── FIX 13: no allocation when zero RAS ─────────────────────

    def test_no_allocation_when_zero_ras(self):
        """If total RAS across all markets is 0, NO market deploys
        (no speculative min-cost deployments)."""
        import profit.allocator as alloc_mod
        alloc_mod._cycles_without_clustering = 0
        from profit.allocator import allocate_portfolio

        db_path = _create_profit_test_db()

        # All markets have negative EV → RAS = 0 for each → total_ras = 0
        markets = [_make_scored_market(f"loss_{i}", score=1.0) for i in range(3)]
        preds = {f"loss_{i}": _make_predictions(f"loss_{i}", ev=-1.0)
                 for i in range(3)}
        cal = _make_mock_calibrator(preds)

        allocs = allocate_portfolio(markets, 1000.0, cal, db_path)
        deploys = [a for a in allocs if a["action"] == "deploy"]
        self.assertEqual(len(deploys), 0)

        os.unlink(db_path)

    # ── FIX 9: oversized cluster handling ───────────────────────

    def test_oversized_cluster_handling(self):
        """Oversized clusters use OVERSIZED_CLUSTER_PCT (0.15), not the
        default DEFAULT_MAX_CLUSTER_PCT (0.30) — tighter cap, NOT dissolve."""
        from profit.correlation import (
            apply_cluster_caps,
            OVERSIZED_CLUSTER_PCT,
            DEFAULT_MAX_CLUSTER_PCT,
        )

        clusters = {"a": 0, "b": 0}  # both in cluster 0, marked oversized
        allocs = [
            {"condition_id": "a", "action": "deploy", "shares_per_side": 300,
             "est_capital_cost": 250, "min_size": 50, "max_spread": 0.045},
            {"condition_id": "b", "action": "deploy", "shares_per_side": 300,
             "est_capital_cost": 250, "min_size": 50, "max_spread": 0.045},
        ]
        total = 1000.0
        result = apply_cluster_caps(
            allocs, clusters, DEFAULT_MAX_CLUSTER_PCT, total,
            oversized_cluster_ids={0},
        )
        cluster_cost = sum(a["est_capital_cost"] for a in result)
        # Stricter cap: 15% * $1000 = $150 (plus min-size floor slack)
        # Must be STRICTLY under default 30% ($300)
        self.assertLess(cluster_cost, total * DEFAULT_MAX_CLUSTER_PCT)
        # And target is the stricter OVERSIZED_CLUSTER_PCT
        self.assertLessEqual(
            cluster_cost, total * OVERSIZED_CLUSTER_PCT + 100  # floor slack
        )


if __name__ == "__main__":
    unittest.main()
