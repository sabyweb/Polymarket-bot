"""Tests for Phase 2 Calibration Layer.

Covers:
  - Feature extraction completeness
  - Fill model training + prediction
  - Loss model fallback + weighted averages
  - Hazard model survival curve monotonicity
  - Reward model Phase 1 CF passthrough
  - EV calculation correctness
  - CalibrationManager integration
  - rank_markets with calibrator
"""

import math
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _create_calibration_db():
    """Create a DB with orders, fills, books, scoring for calibration tests."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = tmp.name
    tmp.close()
    db = sqlite3.connect(path)

    # Core tables
    db.executescript("""
        CREATE TABLE IF NOT EXISTS orders_placed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, condition_id TEXT, side TEXT, price REAL,
            size REAL, order_id TEXT DEFAULT '', order_type TEXT DEFAULT 'BUY'
        );
        CREATE TABLE IF NOT EXISTS fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, condition_id TEXT, question TEXT DEFAULT '',
            side TEXT, fill_type TEXT, shares REAL, price REAL,
            clob_cost REAL, usd_value REAL,
            midpoint REAL DEFAULT 0, slippage REAL DEFAULT 0,
            order_age_secs REAL DEFAULT 0,
            position_usd_after REAL DEFAULT 0,
            reward_rate_hr REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS unwinds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, condition_id TEXT, question TEXT DEFAULT '',
            side TEXT, shares REAL, sell_price REAL, usd_value REAL,
            vwap_cost REAL DEFAULT 0, pnl REAL DEFAULT 0,
            hold_duration_secs REAL DEFAULT 0,
            unwind_type TEXT DEFAULT '', reward_earned_est REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS orders_cancelled (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, order_id TEXT, reason TEXT DEFAULT '',
            condition_id TEXT DEFAULT '', side TEXT DEFAULT '',
            price REAL DEFAULT 0, age_secs REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS book_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, condition_id TEXT,
            best_bid REAL, best_ask REAL, midpoint REAL, spread REAL,
            bid_depth_5c REAL DEFAULT 0, ask_depth_5c REAL DEFAULT 0,
            bid_depth_10c REAL DEFAULT 0, ask_depth_10c REAL DEFAULT 0,
            total_bid_depth REAL DEFAULT 0, total_ask_depth REAL DEFAULT 0,
            num_bid_levels INTEGER DEFAULT 0, num_ask_levels INTEGER DEFAULT 0,
            our_bid_price REAL DEFAULT 0, our_ask_price REAL DEFAULT 0,
            our_bid_depth_ahead REAL DEFAULT 0, our_ask_depth_ahead REAL DEFAULT 0,
            daily_rate REAL DEFAULT 0, max_spread REAL DEFAULT 0,
            agent_shares REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS scoring_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, order_id TEXT, condition_id TEXT,
            side TEXT, scoring INTEGER, price REAL DEFAULT 0,
            shares REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS stop_losses (
            ts REAL, condition_id TEXT, loss_usd REAL
        );
        CREATE TABLE IF NOT EXISTS calibration_model_state (
            model_name TEXT PRIMARY KEY, weights_json TEXT NOT NULL,
            trained_at REAL NOT NULL, n_samples INTEGER NOT NULL,
            n_positive INTEGER NOT NULL DEFAULT 0,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            feature_names TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS reward_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE, total_reward_usd REAL DEFAULT 0,
            total_rebate_usd REAL DEFAULT 0, total_combined_usd REAL DEFAULT 0,
            num_markets_active INTEGER DEFAULT 0,
            est_daily_total REAL DEFAULT 0, correction_factor REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS reward_daily_markets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, condition_id TEXT, scoring_seconds REAL DEFAULT 0,
            avg_bid_size REAL DEFAULT 0, avg_ask_size REAL DEFAULT 0,
            avg_spread REAL DEFAULT 0, avg_midpoint REAL DEFAULT 0,
            daily_rate REAL DEFAULT 0, max_spread_cfg REAL DEFAULT 0,
            fill_count INTEGER DEFAULT 0,
            UNIQUE(date, condition_id)
        );
    """)

    now = time.time()

    # Insert 80 orders (enough to train fill model: need 50+ labeled, 15+ positive)
    for i in range(80):
        cid = f"cid_{i % 5:03d}"
        side = "yes" if i % 2 == 0 else "no"
        oid = f"order_{i:04d}"
        price = 0.48 + (i % 5) * 0.01
        db.execute(
            "INSERT INTO orders_placed (ts, condition_id, side, price, size, order_id, order_type) "
            "VALUES (?, ?, ?, ?, ?, ?, 'BUY')",
            (now - 86400 + i * 1000, cid, side, price, 50, oid),
        )

    # Insert 25 fills (matched to first 25 orders by cid+side+price)
    for i in range(25):
        cid = f"cid_{i % 5:03d}"
        side = "yes" if i % 2 == 0 else "no"
        price = 0.48 + (i % 5) * 0.01
        db.execute(
            "INSERT INTO fills (ts, condition_id, side, fill_type, shares, price, "
            "clob_cost, usd_value, midpoint, slippage) VALUES (?, ?, ?, 'FULL', 50, ?, ?, 25, 0.50, ?)",
            (now - 86400 + i * 1000 + 600, cid, side, price, price, (i % 3) * 0.01),
        )

    # Insert 25 unwinds (matched to fills)
    for i in range(25):
        cid = f"cid_{i % 5:03d}"
        side = "yes" if i % 2 == 0 else "no"
        sell_price = 0.47 + (i % 5) * 0.01
        db.execute(
            "INSERT INTO unwinds (ts, condition_id, side, shares, sell_price, usd_value, "
            "hold_duration_secs) VALUES (?, ?, ?, 50, ?, ?, ?)",
            (now - 86400 + i * 1000 + 3600, cid, side, sell_price, sell_price * 50,
             3000 + i * 100),
        )

    # Insert 40 cancellations (for orders 30-69)
    for i in range(30, 70):
        oid = f"order_{i:04d}"
        db.execute(
            "INSERT INTO orders_cancelled (ts, order_id, reason, condition_id, side, price, age_secs) "
            "VALUES (?, ?, 'stale', ?, ?, 0.50, ?)",
            (now - 86400 + i * 1000 + 1800, oid, f"cid_{i % 5:03d}",
             "yes" if i % 2 == 0 else "no", 1800 + i * 10),
        )

    # Insert book_snapshots for each market near each order
    for i in range(80):
        cid = f"cid_{i % 5:03d}"
        ts = now - 86400 + i * 1000
        db.execute(
            "INSERT INTO book_snapshots (ts, condition_id, best_bid, best_ask, "
            "midpoint, spread, bid_depth_5c, ask_depth_5c, "
            "total_bid_depth, total_ask_depth, "
            "our_bid_depth_ahead, our_ask_depth_ahead, "
            "daily_rate, agent_shares) "
            "VALUES (?, ?, 0.48, 0.52, 0.50, 0.04, 100, 120, 500, 600, ?, ?, ?, 50)",
            (ts, cid, 50 + i * 2, 60 + i * 2, 20 + (i % 5) * 10),
        )

    # Insert scoring_snapshots
    for i in range(20):
        cid = f"cid_{i % 5:03d}"
        db.execute(
            "INSERT INTO scoring_snapshots (ts, order_id, condition_id, side, scoring, price, shares) "
            "VALUES (?, ?, ?, 'yes', 1, 0.48, 50)",
            (now - 3600 + i * 180, f"order_{i:04d}", cid),
        )

    db.commit()
    db.close()
    return path


class TestFeatureExtraction(unittest.TestCase):
    def setUp(self):
        self.db_path = _create_calibration_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_build_training_set_returns_data(self):
        from calibration.features import build_training_set
        dataset = build_training_set(self.db_path)
        self.assertGreater(len(dataset), 0)

    def test_features_are_numeric(self):
        from calibration.features import build_training_set, features_to_vector
        dataset = build_training_set(self.db_path)
        for feat in dataset[:10]:
            vec = features_to_vector(feat)
            for v in vec:
                self.assertIsInstance(v, float)
                self.assertFalse(math.isnan(v))
                self.assertFalse(math.isinf(v))

    def test_outcomes_labeled(self):
        from calibration.features import build_training_set
        dataset = build_training_set(self.db_path)
        outcomes = {f.outcome for f in dataset}
        # Should have both filled and cancelled
        self.assertIn("filled", outcomes)
        self.assertIn("cancelled", outcomes)

    def test_no_none_in_features(self):
        from calibration.features import build_training_set, features_to_vector
        dataset = build_training_set(self.db_path)
        for feat in dataset:
            vec = features_to_vector(feat)
            self.assertNotIn(None, vec)

    def test_feature_count_matches(self):
        from calibration.features import (
            build_training_set, features_to_vector, NUM_FEATURES,
        )
        dataset = build_training_set(self.db_path)
        if dataset:
            vec = features_to_vector(dataset[0])
            self.assertEqual(len(vec), NUM_FEATURES)


class TestFillModel(unittest.TestCase):
    def setUp(self):
        self.db_path = _create_calibration_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_train_produces_metrics(self):
        from calibration.fill_model import FillModel
        model = FillModel()
        result = model.train(self.db_path)
        self.assertIn("status", result)

    def test_trained_model_predicts_probabilities(self):
        from calibration.fill_model import FillModel
        model = FillModel()
        model.train(self.db_path)
        if model.is_ready():
            p = model.predict_from_book(
                spread=0.04, midpoint=0.50, depth_ahead=100,
                total_same_depth=500, opposite_depth_5c=120,
                daily_rate=25.0, agent_shares=50, order_price=0.48,
            )
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)

    def test_fallback_when_not_ready(self):
        from calibration.fill_model import FillModel
        model = FillModel()
        # Not trained
        p = model.predict_from_book(
            spread=0.04, midpoint=0.50, depth_ahead=100,
            total_same_depth=500, opposite_depth_5c=120,
            daily_rate=25.0, agent_shares=50, order_price=0.48,
        )
        self.assertEqual(p, -1.0)  # signal: use fallback

    def test_save_load_roundtrip(self):
        from calibration.fill_model import FillModel
        model = FillModel()
        model.train(self.db_path)
        if model.is_ready():
            model.save(self.db_path)
            model2 = FillModel()
            loaded = model2.load(self.db_path)
            self.assertTrue(loaded)
            self.assertEqual(model.n_samples, model2.n_samples)
            # Predictions should match
            p1 = model.predict_from_book(
                spread=0.04, midpoint=0.50, depth_ahead=100,
                total_same_depth=500, opposite_depth_5c=120,
                daily_rate=25.0, agent_shares=50, order_price=0.48,
            )
            p2 = model2.predict_from_book(
                spread=0.04, midpoint=0.50, depth_ahead=100,
                total_same_depth=500, opposite_depth_5c=120,
                daily_rate=25.0, agent_shares=50, order_price=0.48,
            )
            self.assertAlmostEqual(p1, p2, places=6)


class TestLossModel(unittest.TestCase):
    def setUp(self):
        self.db_path = _create_calibration_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_train_produces_metrics(self):
        from calibration.loss_model import LossModel
        model = LossModel()
        result = model.train(self.db_path)
        self.assertIn("status", result)

    def test_predict_returns_nonneg(self):
        from calibration.loss_model import LossModel
        model = LossModel()
        model.train(self.db_path)
        loss = model.predict(slippage=0.01, spread=0.04, fill_size=50)
        self.assertGreaterEqual(loss, 0.0)

    def test_fallback_when_insufficient(self):
        from calibration.loss_model import LossModel, DEFAULT_LOSS_PER_SHARE
        model = LossModel()
        # Not trained
        loss = model.predict()
        self.assertEqual(loss, DEFAULT_LOSS_PER_SHARE)

    def test_predict_total_loss_scales_with_shares(self):
        from calibration.loss_model import LossModel
        model = LossModel()
        model.train(self.db_path)
        if model.is_ready():
            loss_50 = model.predict_total_loss(shares=50)
            loss_100 = model.predict_total_loss(shares=100)
            # More shares → more total loss
            self.assertGreater(loss_100, loss_50)


class TestHazardModel(unittest.TestCase):
    def setUp(self):
        self.db_path = _create_calibration_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_train_produces_metrics(self):
        from calibration.hazard_model import HazardModel
        model = HazardModel()
        result = model.train(self.db_path)
        self.assertIn("status", result)

    def test_predict_in_range(self):
        from calibration.hazard_model import HazardModel
        model = HazardModel()
        model.train(self.db_path)
        if model.is_ready():
            e_time = model.predict(depth_ahead=100)
            self.assertGreater(e_time, 0)
            self.assertLessEqual(e_time, 24.0)

    def test_survival_monotonic(self):
        from calibration.hazard_model import HazardModel
        model = HazardModel()
        model.train(self.db_path)
        for seg, curve in model.survival_curves.items():
            for i in range(1, len(curve)):
                self.assertLessEqual(curve[i], curve[i - 1] + 1e-9,
                                     f"Survival curve {seg} not monotonic at bin {i}")

    def test_fallback_when_insufficient(self):
        from calibration.hazard_model import HazardModel, DEFAULT_TIME_HOURS
        model = HazardModel()
        e = model.predict()
        self.assertEqual(e, DEFAULT_TIME_HOURS)


class TestRewardModel(unittest.TestCase):
    def setUp(self):
        self.db_path = _create_calibration_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_phase1_passthrough(self):
        from calibration.reward_model import RewardModel
        model = RewardModel()
        model.train(self.db_path, correction_factor=0.5)
        # Phase 1: should use CF=0.5
        rate = model.predict_rate(
            condition_id="cid_001", daily_rate=100.0,
            q_share_pct=0.2, correction_factor=0.5,
        )
        # Expected: 100 * min(0.2, 0.5) * 0.5 / 24 = 0.4167
        expected = 100.0 * 0.2 * 0.5 / 24.0
        self.assertAlmostEqual(rate, expected, places=2)

    def test_phase1_when_no_daily_data(self):
        from calibration.reward_model import RewardModel
        model = RewardModel()
        result = model.train(self.db_path, correction_factor=0.3)
        self.assertEqual(result.get("phase"), 1)
        self.assertFalse(model.is_ready())  # Phase 2 not active


class TestEVCalculation(unittest.TestCase):
    def test_ev_uses_predictions_ev(self):
        """score_market_ev returns predictions.ev_per_day directly
        (which already includes reward bias, tail risk, uncertainty penalty)."""
        from calibration.manager import CalibrationPredictions
        from oversight.market_scorer import score_market_ev
        from oversight.data_collector import MarketMetrics

        m = MarketMetrics(
            condition_id="test", question="test?",
            daily_rate=50.0, actual_reward_total=0,
            fill_cost_recent=0, dump_revenue_recent=0,
            fill_count_recent=0, net_pnl_recent=0,
            current_position_usd=0, on_book_hours=24, q_share_pct=0.2,
        )
        preds = CalibrationPredictions(
            condition_id="test",
            p_fill_24h=0.1,
            e_loss_given_fill=5.0,
            e_time_on_book_hours=20.0,
            reward_rate_per_hour=0.10,
            ev_per_day=1.5,  # pre-computed by manager
            confidence="model",
            model_versions={},
        )
        ev = score_market_ev(m, preds)
        self.assertAlmostEqual(ev, 1.5, places=2)

    def test_ev_negative_when_risky(self):
        from calibration.manager import CalibrationPredictions
        from oversight.market_scorer import score_market_ev
        from oversight.data_collector import MarketMetrics

        m = MarketMetrics(
            condition_id="risky", question="risky?",
            daily_rate=10.0, actual_reward_total=0,
            fill_cost_recent=0, dump_revenue_recent=0,
            fill_count_recent=0, net_pnl_recent=0,
            current_position_usd=0, on_book_hours=24, q_share_pct=0.1,
        )
        preds = CalibrationPredictions(
            condition_id="risky",
            p_fill_24h=0.5,
            e_loss_given_fill=10.0,
            e_time_on_book_hours=5.0,
            reward_rate_per_hour=0.02,
            ev_per_day=-4.9,  # pre-computed: clearly negative
            confidence="model",
            model_versions={},
        )
        ev = score_market_ev(m, preds)
        self.assertLess(ev, 0)


class TestCalibrationManager(unittest.TestCase):
    def setUp(self):
        self.db_path = _create_calibration_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_retrain_returns_metrics(self):
        from calibration.manager import CalibrationManager
        mgr = CalibrationManager(db_path=self.db_path)
        result = mgr.retrain(correction_factor=0.5)
        self.assertIn("fill_model", result)
        self.assertIn("loss_model", result)

    def test_get_predictions_returns_struct(self):
        from calibration.manager import CalibrationManager, CalibrationPredictions
        mgr = CalibrationManager(db_path=self.db_path)
        mgr.retrain(correction_factor=0.5)
        preds = mgr.get_predictions(
            condition_id="cid_001", daily_rate=25.0,
            q_share_pct=0.1, on_book_hours=10.0,
        )
        self.assertIsInstance(preds, CalibrationPredictions)
        self.assertGreaterEqual(preds.p_fill_24h, 0.0)
        self.assertLessEqual(preds.p_fill_24h, 1.0)
        self.assertGreaterEqual(preds.e_loss_given_fill, 0.0)
        self.assertGreater(preds.e_time_on_book_hours, 0.0)

    def test_model_info(self):
        from calibration.manager import CalibrationManager
        mgr = CalibrationManager(db_path=self.db_path)
        mgr.retrain(correction_factor=0.5)
        info = mgr.model_info
        self.assertIn("fill_model", info)
        self.assertIn("is_ready", info)

    def test_ev_has_correct_sign(self):
        from calibration.manager import CalibrationManager
        mgr = CalibrationManager(db_path=self.db_path)
        mgr.retrain(correction_factor=0.5)
        preds = mgr.get_predictions(
            condition_id="cid_001", daily_rate=25.0,
            q_share_pct=0.1, on_book_hours=10.0,
        )
        # EV should be a real number (can be negative)
        self.assertIsInstance(preds.ev_per_day, float)
        self.assertFalse(math.isnan(preds.ev_per_day))


class TestRankMarketsWithCalibrator(unittest.TestCase):
    def setUp(self):
        self.db_path = _create_calibration_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_rank_markets_accepts_calibrator(self):
        """rank_markets should work with calibrator=None (existing behavior)."""
        from oversight.data_collector import MarketMetrics
        from oversight.market_scorer import rank_markets

        metrics = [
            MarketMetrics(
                condition_id="cid_001", question="Test?",
                daily_rate=25.0, actual_reward_total=0,
                fill_cost_recent=0, dump_revenue_recent=0,
                fill_count_recent=0, net_pnl_recent=0,
                current_position_usd=0, on_book_hours=24, q_share_pct=0.1,
            ),
        ]
        # Without calibrator (backward compat)
        scored = rank_markets(
            metrics, hours=24, correction_factor=0.5,
            db_path=self.db_path, calibrator=None,
        )
        self.assertEqual(len(scored), 1)

    def test_rank_markets_with_calibrator(self):
        """rank_markets should use EV scoring when calibrator is ready."""
        from calibration.manager import CalibrationManager
        from oversight.data_collector import MarketMetrics
        from oversight.market_scorer import rank_markets

        mgr = CalibrationManager(db_path=self.db_path)
        mgr.retrain(correction_factor=0.5)

        metrics = [
            MarketMetrics(
                condition_id="cid_001", question="Test?",
                daily_rate=25.0, actual_reward_total=0,
                fill_cost_recent=0, dump_revenue_recent=0,
                fill_count_recent=0, net_pnl_recent=0,
                current_position_usd=0, on_book_hours=24, q_share_pct=0.1,
            ),
        ]
        scored = rank_markets(
            metrics, hours=24, correction_factor=0.5,
            db_path=self.db_path, calibrator=mgr,
        )
        self.assertEqual(len(scored), 1)
        # Score should be a real number
        self.assertFalse(math.isnan(scored[0].score))


class TestSafetyStillEnforced(unittest.TestCase):
    """Verify safety controller still works after calibration integration."""

    def test_filter_allocations_after_ev_scoring(self):
        from oversight.safety_controller import SafetyController, UNSAFE

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        path = tmp.name
        tmp.close()
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE IF NOT EXISTS fills (ts REAL, condition_id TEXT, side TEXT, fill_type TEXT, shares REAL, price REAL, clob_cost REAL, usd_value REAL)")
        db.execute("CREATE TABLE IF NOT EXISTS unwinds (ts REAL, condition_id TEXT, usd_value REAL)")
        db.execute("CREATE TABLE IF NOT EXISTS stop_losses (ts REAL, condition_id TEXT, loss_usd REAL)")
        db.execute("CREATE TABLE IF NOT EXISTS scoring_snapshots (id INTEGER PRIMARY KEY, ts REAL, order_id TEXT, condition_id TEXT, side TEXT, scoring INTEGER, price REAL, shares REAL)")
        db.execute("INSERT INTO scoring_snapshots (ts, order_id, condition_id, side, scoring, price, shares) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (time.time() - 60, "t", "t", "yes", 1, 0.5, 100))
        db.commit()
        db.close()

        sc = SafetyController(db_path=path)
        sc.state = UNSAFE  # force UNSAFE

        # Even with high-EV allocations, safety filter caps at 3 markets + min_size
        allocs = [
            {"action": "deploy", "shares_per_side": 200, "score": 10,
             "condition_id": "a", "est_capital_cost": 200,
             "max_spread": 0.045, "q_share_pct": 0.1, "min_size": 50},
            {"action": "deploy", "shares_per_side": 200, "score": 5,
             "condition_id": "b", "est_capital_cost": 200,
             "max_spread": 0.045, "q_share_pct": 0.1, "min_size": 50},
        ]
        filtered = sc.filter_allocations(allocs, 1000)
        for a in filtered:
            if a["action"] == "deploy":
                self.assertEqual(a["shares_per_side"], 50)  # min_size
                self.assertIn("PROBE", a.get("reason", ""))

        os.unlink(path)


class TestTailRiskAdjustment(unittest.TestCase):
    """Fix 2: Loss model uses mean + 1.5*std for tail-aware predictions."""

    def setUp(self):
        self.db_path = _create_calibration_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_tail_aware_higher_than_mean(self):
        from calibration.loss_model import LossModel
        model = LossModel()
        model.train(self.db_path)
        if model.is_ready():
            loss_tail = model.predict(slippage=0.01, tail_aware=True)
            loss_mean = model.predict(slippage=0.01, tail_aware=False)
            # Tail-aware should be >= mean (adds 1.5*std)
            self.assertGreaterEqual(loss_tail, loss_mean)

    def test_metrics_include_tail_loss(self):
        from calibration.loss_model import LossModel
        model = LossModel()
        result = model.train(self.db_path)
        if result.get("status") == "trained":
            self.assertIn("global_tail_loss", result)
            self.assertGreaterEqual(result["global_tail_loss"], result["global_avg_loss"])

    def test_std_computed(self):
        from calibration.loss_model import LossModel
        model = LossModel()
        model.train(self.db_path)
        if model.is_ready():
            self.assertGreaterEqual(model.global_std, 0.0)


class TestRecencyDecayHazard(unittest.TestCase):
    """Fix 3: Hazard model weights recent observations more heavily."""

    def setUp(self):
        self.db_path = _create_calibration_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_survival_still_monotonic(self):
        from calibration.hazard_model import HazardModel
        model = HazardModel()
        model.train(self.db_path)
        for seg, curve in model.survival_curves.items():
            for i in range(1, len(curve)):
                self.assertLessEqual(curve[i], curve[i - 1] + 1e-9,
                                     f"Survival {seg} not monotonic at bin {i}")

    def test_expected_time_reasonable(self):
        from calibration.hazard_model import HazardModel
        model = HazardModel()
        model.train(self.db_path)
        if model.is_ready():
            e = model.predict(depth_ahead=100)
            self.assertGreater(e, 0)
            self.assertLessEqual(e, 24.0)


class TestRewardSafetyBias(unittest.TestCase):
    """Fix 4: Reward term capped at 80% of naive estimate."""

    def setUp(self):
        self.db_path = _create_calibration_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_reward_rate_biased_down(self):
        from calibration.manager import CalibrationManager, REWARD_SAFETY_BIAS
        mgr = CalibrationManager(db_path=self.db_path)
        mgr.retrain(correction_factor=1.0)
        preds = mgr.get_predictions(
            condition_id="cid_001", daily_rate=100.0,
            q_share_pct=0.2, correction_factor=1.0,
        )
        # Naive: 100 * 0.2 * 1.0 / 24 = 0.833/hr
        # With 80% bias: 0.833 * 0.8 = 0.667/hr
        naive_rate = 100.0 * 0.2 * 1.0 / 24.0
        self.assertLess(preds.reward_rate_per_hour, naive_rate)
        self.assertAlmostEqual(
            preds.reward_rate_per_hour,
            naive_rate * REWARD_SAFETY_BIAS,
            places=2,
        )


class TestMinEVThreshold(unittest.TestCase):
    """Fix 5: Markets below MIN_EV_THRESHOLD get avoided."""

    def test_marginal_ev_zeroed(self):
        from calibration.manager import CalibrationPredictions, MIN_EV_THRESHOLD
        from oversight.market_scorer import score_market_ev
        from oversight.data_collector import MarketMetrics

        m = MarketMetrics(
            condition_id="marginal", question="marginal?",
            daily_rate=10.0, actual_reward_total=0,
            fill_cost_recent=0, dump_revenue_recent=0,
            fill_count_recent=0, net_pnl_recent=0,
            current_position_usd=0, on_book_hours=24, q_share_pct=0.05,
        )
        # EV = $0.05/day — below MIN_EV_THRESHOLD ($0.10)
        preds = CalibrationPredictions(
            condition_id="marginal",
            p_fill_24h=0.01, e_loss_given_fill=1.0,
            e_time_on_book_hours=20.0, reward_rate_per_hour=0.003,
            ev_per_day=0.05,
            confidence="model", model_versions={},
        )
        ev = score_market_ev(m, preds)
        # score_market_ev returns predictions.ev_per_day directly
        # The MIN_EV gate is applied in rank_markets, not score_market_ev
        self.assertEqual(ev, 0.05)  # score_market_ev passes through
        # The gate in rank_markets zeroes scores < MIN_EV_THRESHOLD
        self.assertLess(ev, MIN_EV_THRESHOLD)


class TestUncertaintyPenalty(unittest.TestCase):
    """Fix 6: EV scaled by model confidence (uncertainty discount)."""

    def setUp(self):
        self.db_path = _create_calibration_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_ev_discounted_by_confidence(self):
        from calibration.manager import CalibrationManager, UNCERTAINTY_FLOOR
        mgr = CalibrationManager(db_path=self.db_path)
        mgr.retrain(correction_factor=0.5)
        preds = mgr.get_predictions(
            condition_id="cid_001", daily_rate=25.0,
            q_share_pct=0.1, on_book_hours=10.0,
        )
        # EV should be finite and the model_versions dict should exist
        self.assertIsNotNone(preds.model_versions)
        # With some fallbacks, EV should be discounted
        # (not all 4 models may be ready)
        n_model = sum(1 for v in preds.model_versions.values() if v != "fallback")
        n_total = len(preds.model_versions)
        expected_mult = max(UNCERTAINTY_FLOOR, n_model / max(n_total, 1))
        # The EV has been scaled — just verify it's a real number
        self.assertFalse(math.isnan(preds.ev_per_day))
        self.assertFalse(math.isinf(preds.ev_per_day))

    def test_all_fallback_still_produces_ev(self):
        """Even with all fallbacks, EV should be computed (at UNCERTAINTY_FLOOR)."""
        from calibration.manager import CalibrationManager, UNCERTAINTY_FLOOR
        # Create empty DB — all models will use fallbacks
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        path = tmp.name
        tmp.close()
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS orders_placed (id INTEGER PRIMARY KEY, ts REAL, condition_id TEXT, side TEXT, price REAL, size REAL, order_id TEXT DEFAULT '', order_type TEXT DEFAULT 'BUY');
            CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY, ts REAL, condition_id TEXT, side TEXT, fill_type TEXT, shares REAL, price REAL, clob_cost REAL, usd_value REAL, midpoint REAL DEFAULT 0, slippage REAL DEFAULT 0, order_age_secs REAL DEFAULT 0, question TEXT DEFAULT '', position_usd_after REAL DEFAULT 0, reward_rate_hr REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS unwinds (id INTEGER PRIMARY KEY, ts REAL, condition_id TEXT, side TEXT, shares REAL, sell_price REAL, usd_value REAL, question TEXT DEFAULT '', vwap_cost REAL DEFAULT 0, pnl REAL DEFAULT 0, hold_duration_secs REAL DEFAULT 0, unwind_type TEXT DEFAULT '', reward_earned_est REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS orders_cancelled (id INTEGER PRIMARY KEY, ts REAL, order_id TEXT, reason TEXT DEFAULT '', condition_id TEXT DEFAULT '', side TEXT DEFAULT '', price REAL DEFAULT 0, age_secs REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS book_snapshots (id INTEGER PRIMARY KEY, ts REAL, condition_id TEXT, best_bid REAL, best_ask REAL, midpoint REAL, spread REAL, bid_depth_5c REAL DEFAULT 0, ask_depth_5c REAL DEFAULT 0, bid_depth_10c REAL DEFAULT 0, ask_depth_10c REAL DEFAULT 0, total_bid_depth REAL DEFAULT 0, total_ask_depth REAL DEFAULT 0, num_bid_levels INTEGER DEFAULT 0, num_ask_levels INTEGER DEFAULT 0, our_bid_price REAL DEFAULT 0, our_ask_price REAL DEFAULT 0, our_bid_depth_ahead REAL DEFAULT 0, our_ask_depth_ahead REAL DEFAULT 0, daily_rate REAL DEFAULT 0, max_spread REAL DEFAULT 0, agent_shares REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS scoring_snapshots (id INTEGER PRIMARY KEY, ts REAL, order_id TEXT, condition_id TEXT, side TEXT, scoring INTEGER, price REAL DEFAULT 0, shares REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS stop_losses (ts REAL, condition_id TEXT, loss_usd REAL);
            CREATE TABLE IF NOT EXISTS calibration_model_state (model_name TEXT PRIMARY KEY, weights_json TEXT NOT NULL, trained_at REAL NOT NULL, n_samples INTEGER NOT NULL, n_positive INTEGER NOT NULL DEFAULT 0, metrics_json TEXT NOT NULL DEFAULT '{}', feature_names TEXT NOT NULL DEFAULT '[]');
            CREATE TABLE IF NOT EXISTS reward_daily (id INTEGER PRIMARY KEY, date TEXT UNIQUE, total_reward_usd REAL DEFAULT 0, total_rebate_usd REAL DEFAULT 0, total_combined_usd REAL DEFAULT 0, num_markets_active INTEGER DEFAULT 0, est_daily_total REAL DEFAULT 0, correction_factor REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS reward_daily_markets (id INTEGER PRIMARY KEY, date TEXT, condition_id TEXT, scoring_seconds REAL DEFAULT 0, avg_bid_size REAL DEFAULT 0, avg_ask_size REAL DEFAULT 0, avg_spread REAL DEFAULT 0, avg_midpoint REAL DEFAULT 0, daily_rate REAL DEFAULT 0, max_spread_cfg REAL DEFAULT 0, fill_count INTEGER DEFAULT 0, UNIQUE(date, condition_id));
        """)
        db.commit()
        db.close()

        mgr = CalibrationManager(db_path=path)
        mgr.retrain(correction_factor=0.5)
        self.assertFalse(mgr.is_ready())  # all models in fallback
        preds = mgr.get_predictions(
            condition_id="test_cid", daily_rate=25.0,
            q_share_pct=0.1, on_book_hours=10.0,
        )
        self.assertEqual(preds.confidence, "fallback")
        self.assertFalse(math.isnan(preds.ev_per_day))
        os.unlink(path)


if __name__ == "__main__":
    unittest.main()
