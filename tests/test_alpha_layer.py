"""Phase 4 Alpha Layer tests.

STEP 13 coverage:
  Bandit      — alpha increases on success, beta on failure, valid dist
  Attribution — sum preservation, proportional split
  Regime      — high fill rate → hostile, low → normal
  Integration — bandit affects allocation, regime scales capital,
                safety still enforced downstream
"""

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ───────────────────────────────────────────────────────────────
# Shared fixtures
# ───────────────────────────────────────────────────────────────


def _make_db() -> str:
    """Create a tmp DB with all tables Phase 4 touches."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = tmp.name
    tmp.close()
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, condition_id TEXT, side TEXT, fill_type TEXT,
            shares REAL, price REAL, clob_cost REAL, usd_value REAL
        );
        CREATE TABLE IF NOT EXISTS unwinds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, condition_id TEXT, side TEXT, shares REAL,
            sell_price REAL, usd_value REAL, pnl REAL DEFAULT 0,
            reward_earned_est REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS orders_placed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, condition_id TEXT, side TEXT,
            price REAL, size REAL, order_id TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS reward_daily (
            id INTEGER PRIMARY KEY, date TEXT UNIQUE,
            total_reward_usd REAL DEFAULT 0, total_rebate_usd REAL DEFAULT 0,
            total_combined_usd REAL DEFAULT 0,
            num_markets_active INTEGER DEFAULT 0,
            est_daily_total REAL DEFAULT 0, correction_factor REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS reward_daily_markets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, condition_id TEXT, scoring_seconds REAL DEFAULT 0,
            daily_rate REAL DEFAULT 0, max_spread_cfg REAL DEFAULT 0,
            fill_count INTEGER DEFAULT 0,
            avg_bid_size REAL DEFAULT 0, avg_ask_size REAL DEFAULT 0,
            avg_spread REAL DEFAULT 0, avg_midpoint REAL DEFAULT 0,
            UNIQUE(date, condition_id)
        );
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
    """)
    db.commit()
    db.close()
    return path


def _insert_fill(db_path: str, cid: str, ts: float, shares: float = 50,
                  cost: float = 0.50):
    db = sqlite3.connect(db_path)
    db.execute(
        "INSERT INTO fills (ts, condition_id, side, fill_type, shares, "
        "price, clob_cost, usd_value) VALUES (?,?,?,?,?,?,?,?)",
        (ts, cid, "yes", "FULL", shares, 0.5, cost, shares * 0.5),
    )
    db.commit()
    db.close()


def _insert_unwind(db_path: str, cid: str, ts: float, usd_value: float = 30,
                    reward: float = 0.0):
    db = sqlite3.connect(db_path)
    db.execute(
        "INSERT INTO unwinds (ts, condition_id, side, shares, sell_price, "
        "usd_value, reward_earned_est) VALUES (?,?,?,?,?,?,?)",
        (ts, cid, "yes", 50, 0.6, usd_value, reward),
    )
    db.commit()
    db.close()


# ───────────────────────────────────────────────────────────────
# Bandit
# ───────────────────────────────────────────────────────────────


class TestBandit(unittest.TestCase):
    def setUp(self):
        self.db_path = _make_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_table_created_on_init(self):
        from profit.bandit import Bandit
        Bandit(self.db_path)
        db = sqlite3.connect(self.db_path)
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='bandit_state'"
        ).fetchall()
        db.close()
        self.assertEqual(len(rows), 1)

    def test_alpha_increases_on_success(self):
        """PnL > 0 must bump alpha but not beta."""
        from profit.bandit import Bandit
        now = time.time()
        # reward (est=10) > fill_damage (cost=1×50=50 - unwind 30 = 20)... no
        # Simpler: fill with tiny cost + unwind carrying reward + enough
        # usd_value to wipe damage.
        _insert_fill(self.db_path, "win", now - 3600, shares=50, cost=0.40)
        _insert_unwind(self.db_path, "win", now - 1800, usd_value=25.0,
                       reward=5.0)
        # reward 5 - max(0, 50*0.40 - 25) = 5 - max(0, -5) = 5 > 0 → success

        b = Bandit(self.db_path)
        b.update()
        state = b.load_state()
        self.assertIn("win", state)
        a, beta = state["win"]
        self.assertGreater(a, 1.0)   # alpha incremented
        self.assertEqual(beta, 1.0)  # beta untouched

    def test_beta_increases_on_failure(self):
        """PnL <= 0 must bump beta but not alpha."""
        from profit.bandit import Bandit
        now = time.time()
        # High fill cost, no unwind → net_damage large → pnl = 0 - damage < 0
        _insert_fill(self.db_path, "lose", now - 3600, shares=100, cost=0.80)
        # No unwind row at all

        b = Bandit(self.db_path)
        b.update()
        state = b.load_state()
        self.assertIn("lose", state)
        a, beta = state["lose"]
        self.assertEqual(a, 1.0)      # alpha untouched
        self.assertGreater(beta, 1.0)  # beta incremented

    def test_sample_clamped_to_min_score(self):
        """Invariant 1: sampled score >= 0.3 always."""
        from profit.bandit import Bandit, MIN_SCORE
        # Write a very pessimistic posterior that would routinely draw <0.3
        db = sqlite3.connect(self.db_path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS bandit_state ("
            "market_id TEXT PRIMARY KEY, alpha REAL NOT NULL, "
            "beta REAL NOT NULL, last_updated_ts INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT OR REPLACE INTO bandit_state VALUES (?, ?, ?, ?)",
            ("tiny", 0.1, 50.0, int(time.time())),  # E[Beta] ≈ 0.002
        )
        db.commit()
        db.close()

        b = Bandit(self.db_path)
        for _ in range(50):
            scores = b.sample()
            self.assertIn("tiny", scores)
            self.assertGreaterEqual(scores["tiny"], MIN_SCORE)

    def test_sample_returns_valid_distribution(self):
        """Draws should span a reasonable range for Beta(5, 5).

        PART 9: Bandit seeds numpy from hash(int(time.time())) per call, so
        repeated calls within one second are deterministic. To exercise the
        underlying Beta(5,5) distribution we vary the simulated cycle id.
        """
        from unittest.mock import patch
        from profit.bandit import Bandit
        db = sqlite3.connect(self.db_path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS bandit_state ("
            "market_id TEXT PRIMARY KEY, alpha REAL NOT NULL, "
            "beta REAL NOT NULL, last_updated_ts INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT OR REPLACE INTO bandit_state VALUES (?, ?, ?, ?)",
            ("mid", 5.0, 5.0, int(time.time())),  # E[Beta] = 0.5
        )
        db.commit()
        db.close()

        b = Bandit(self.db_path)
        # Vary cycle timestamps across draws so each call gets a fresh seed
        base_ts = int(time.time())
        draws = []
        for i in range(200):
            with patch("profit.bandit.time.time", return_value=base_ts + i):
                draws.append(b.sample()["mid"])
        # All draws valid
        self.assertTrue(all(0.3 <= d <= 1.0 for d in draws))
        # Mean near 0.5 (generous band for 200 draws across distinct cycles)
        mean = sum(draws) / len(draws)
        self.assertGreater(mean, 0.35)
        self.assertLess(mean, 0.75)

    def test_sample_deterministic_within_cycle(self):
        """PART 9: same cycle_id → same draw (reproducibility invariant)."""
        from unittest.mock import patch
        from profit.bandit import Bandit
        db = sqlite3.connect(self.db_path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS bandit_state ("
            "market_id TEXT PRIMARY KEY, alpha REAL NOT NULL, "
            "beta REAL NOT NULL, last_updated_ts INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT OR REPLACE INTO bandit_state VALUES (?, ?, ?, ?)",
            ("rep", 3.0, 7.0, int(time.time())),
        )
        db.commit()
        db.close()

        b = Bandit(self.db_path)
        with patch("profit.bandit.time.time", return_value=1700000000):
            d1 = b.sample()["rep"]
            d2 = b.sample()["rep"]
            d3 = b.sample()["rep"]
        self.assertEqual(d1, d2)
        self.assertEqual(d2, d3)

    def test_update_with_no_data_is_noop(self):
        """Invariant 5: empty DB → no crash, returns no_data status."""
        from profit.bandit import Bandit
        b = Bandit(self.db_path)
        result = b.update()
        self.assertEqual(result["status"], "no_data")

    def test_uses_real_pnl_only(self):
        """Invariant 2: update must read from fills/unwinds — a row that
        only appears in reward_daily (synthetic signal) must NOT create
        a bandit entry."""
        from profit.bandit import Bandit
        db = sqlite3.connect(self.db_path)
        db.execute(
            "INSERT INTO reward_daily (date, total_combined_usd, est_daily_total) "
            "VALUES (?, ?, ?)",
            ("2026-04-14", 100.0, 50.0),
        )
        db.execute(
            "INSERT INTO reward_daily_markets (date, condition_id, "
            "scoring_seconds, daily_rate) VALUES (?, ?, ?, ?)",
            ("2026-04-14", "phantom", 3600, 5.0),
        )
        db.commit()
        db.close()
        b = Bandit(self.db_path)
        b.update()
        state = b.load_state()
        self.assertNotIn("phantom", state)


# ───────────────────────────────────────────────────────────────
# Attribution
# ───────────────────────────────────────────────────────────────


class TestAttribution(unittest.TestCase):
    def setUp(self):
        self.db_path = _make_db()
        self.date = "2026-04-14"

    def tearDown(self):
        os.unlink(self.db_path)

    def _seed(self, total_reward: float, markets: list[tuple[str, float, float]]):
        """markets = [(cid, scoring_seconds, daily_rate), ...]"""
        db = sqlite3.connect(self.db_path)
        db.execute(
            "INSERT INTO reward_daily (date, total_combined_usd) VALUES (?, ?)",
            (self.date, total_reward),
        )
        for cid, secs, rate in markets:
            db.execute(
                "INSERT INTO reward_daily_markets "
                "(date, condition_id, scoring_seconds, daily_rate) "
                "VALUES (?, ?, ?, ?)",
                (self.date, cid, secs, rate),
            )
        db.commit()
        db.close()

    def test_sum_equals_total_rewards(self):
        """Invariant 3: sum of attributed rewards == total payout exactly."""
        from calibration.attribution import compute_attribution
        self._seed(
            total_reward=100.0,
            markets=[("a", 3600, 10.0), ("b", 7200, 5.0), ("c", 1800, 20.0)],
        )
        attr = compute_attribution(self.db_path, date_str=self.date)
        self.assertEqual(len(attr), 3)
        # Sum must equal total to float precision
        self.assertAlmostEqual(sum(attr.values()), 100.0, places=6)

    def test_proportional_split(self):
        """Market with 2× contribution gets ~2× reward."""
        from calibration.attribution import compute_attribution
        # Equal daily_rate, a has 2× the scoring_seconds of b
        self._seed(
            total_reward=300.0,
            markets=[("a", 7200, 5.0), ("b", 3600, 5.0)],
        )
        attr = compute_attribution(self.db_path, date_str=self.date)
        self.assertAlmostEqual(attr["a"], 200.0, places=2)
        self.assertAlmostEqual(attr["b"], 100.0, places=2)

    def test_missing_total_returns_empty(self):
        """Invariant 5: no payout row → empty dict, no crash."""
        from calibration.attribution import compute_attribution
        attr = compute_attribution(self.db_path, date_str="1999-01-01")
        self.assertEqual(attr, {})

    def test_persists_to_table(self):
        """Output must be written to reward_attribution table."""
        from calibration.attribution import compute_attribution
        self._seed(
            total_reward=50.0,
            markets=[("x", 3600, 5.0), ("y", 3600, 5.0)],
        )
        compute_attribution(self.db_path, date_str=self.date)
        db = sqlite3.connect(self.db_path)
        rows = db.execute(
            "SELECT market_id, reward_usd FROM reward_attribution "
            "WHERE date = ?",
            (self.date,),
        ).fetchall()
        db.close()
        self.assertEqual(len(rows), 2)
        cid_to_rew = dict(rows)
        self.assertAlmostEqual(cid_to_rew["x"], 25.0, places=2)
        self.assertAlmostEqual(cid_to_rew["y"], 25.0, places=2)

    def test_idempotent_recompute(self):
        """Re-running on the same date should replace, not duplicate rows."""
        from calibration.attribution import compute_attribution
        self._seed(
            total_reward=100.0,
            markets=[("a", 3600, 10.0), ("b", 3600, 10.0)],
        )
        compute_attribution(self.db_path, date_str=self.date)
        compute_attribution(self.db_path, date_str=self.date)
        db = sqlite3.connect(self.db_path)
        n = db.execute(
            "SELECT COUNT(*) FROM reward_attribution WHERE date = ?",
            (self.date,),
        ).fetchone()[0]
        db.close()
        self.assertEqual(n, 2)  # not 4


# ───────────────────────────────────────────────────────────────
# Regime
# ───────────────────────────────────────────────────────────────


class TestRegime(unittest.TestCase):
    def setUp(self):
        self.db_path = _make_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def _seed_orders(self, cids: list[str]):
        now = time.time()
        db = sqlite3.connect(self.db_path)
        for i, cid in enumerate(cids):
            db.execute(
                "INSERT INTO orders_placed "
                "(ts, condition_id, side, price, size) VALUES (?,?,?,?,?)",
                (now - 3600, cid, "yes", 0.5, 50),
            )
        db.commit()
        db.close()

    def _seed_fills(self, cid: str, count: int, within_secs: float = 1800):
        now = time.time()
        db = sqlite3.connect(self.db_path)
        for i in range(count):
            db.execute(
                "INSERT INTO fills (ts, condition_id, side, fill_type, "
                "shares, price, clob_cost, usd_value) VALUES (?,?,?,?,?,?,?,?)",
                (now - within_secs + i, cid, "yes", "FULL", 50, 0.5, 0.5, 25),
            )
        db.commit()
        db.close()

    def test_low_fill_rate_is_normal(self):
        """1 active market + 0 fills/hr → normal."""
        from profit.regime import detect_regime
        self._seed_orders(["m1"])
        regime = detect_regime(self.db_path)
        self.assertEqual(regime, "normal")

    def test_high_fill_rate_is_hostile(self):
        """1 active market + 5 fills in last 1h → fill_rate=5 > 1.5 → hostile."""
        from profit.regime import detect_regime
        self._seed_orders(["m1"])
        self._seed_fills("m1", count=5, within_secs=1500)
        regime = detect_regime(self.db_path)
        self.assertEqual(regime, "hostile")

    def test_no_active_markets_is_normal(self):
        """Invariant 5: empty DB should default to normal, never crash."""
        from profit.regime import detect_regime
        regime = detect_regime(self.db_path)
        self.assertEqual(regime, "normal")

    def test_missing_table_does_not_crash(self):
        """Nonexistent DB path → graceful normal fallback."""
        from profit.regime import detect_regime
        regime = detect_regime("/nonexistent/path/to.db")
        self.assertEqual(regime, "normal")

    def test_hostile_threshold_boundary(self):
        """fill_rate just below threshold → normal; just above → hostile.

        HOSTILE_THRESHOLD = 1.5. With 2 active markets:
          3 fills  → rate = 1.5 → NOT hostile (uses '>' not '>=')
          4 fills  → rate = 2.0 → hostile
        """
        from profit.regime import detect_regime
        self._seed_orders(["m1", "m2"])
        self._seed_fills("m1", count=3, within_secs=1500)
        self.assertEqual(detect_regime(self.db_path), "normal")
        self._seed_fills("m2", count=1, within_secs=1000)
        self.assertEqual(detect_regime(self.db_path), "hostile")


# ───────────────────────────────────────────────────────────────
# Integration: bandit + regime + safety
# ───────────────────────────────────────────────────────────────


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
        p_fill_24h=p_fill, e_loss_given_fill=loss,
        e_time_on_book_hours=e_time, reward_rate_per_hour=reward_rate,
        ev_per_day=ev, confidence=confidence,
        model_versions={"p_fill": "model", "e_loss": "model",
                        "e_time": "model", "reward": "phase1"},
    )


def _make_mock_calibrator(predictions_map=None):
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


class TestAllocatorIntegration(unittest.TestCase):
    def setUp(self):
        self.db_path = _make_db()
        # Reset the clustering-failure counter so capital doesn't vary
        # across tests based on ordering.
        import profit.allocator as pa
        pa._cycles_without_clustering = 0

    def tearDown(self):
        os.unlink(self.db_path)

    def test_bandit_multiplier_affects_final_score(self):
        """STEP 5: final_score = RAS * bandit appears in output metadata."""
        from profit.allocator import allocate_portfolio
        markets = [
            _make_scored_market("a", score=1.0),
            _make_scored_market("b", score=1.0),
        ]
        cal = _make_mock_calibrator({
            "a": _make_predictions("a", ev=1.0, p_fill=0.1, loss=5.0),
            "b": _make_predictions("b", ev=1.0, p_fill=0.1, loss=5.0),
        })
        # Seed bandit_state: "a" strongly preferred (Beta(100,1) → draw ≈ 1.0)
        # "b" strongly disfavored (Beta(1,100) → draw ≈ 0.0 → clamped 0.3)
        db = sqlite3.connect(self.db_path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS bandit_state ("
            "market_id TEXT PRIMARY KEY, alpha REAL NOT NULL, "
            "beta REAL NOT NULL, last_updated_ts INTEGER NOT NULL)"
        )
        now_ts = int(time.time())
        db.execute("INSERT INTO bandit_state VALUES (?,?,?,?)",
                   ("a", 100.0, 1.0, now_ts))
        db.execute("INSERT INTO bandit_state VALUES (?,?,?,?)",
                   ("b", 1.0, 100.0, now_ts))
        db.commit()
        db.close()

        allocs = allocate_portfolio(markets, 10000.0, cal, self.db_path)
        by_cid = {a["condition_id"]: a for a in allocs
                   if a["action"] == "deploy"}
        self.assertIn("a", by_cid)
        self.assertIn("b", by_cid)
        # a's bandit draw ≫ b's (floor 0.3 vs ~1.0)
        self.assertGreater(by_cid["a"]["_bandit"], by_cid["b"]["_bandit"])
        # And the final_score inherits that ordering
        self.assertGreater(by_cid["a"]["_final_score"],
                           by_cid["b"]["_final_score"])
        # "a" gets >= as many shares as "b"
        self.assertGreaterEqual(
            by_cid["a"]["shares_per_side"], by_cid["b"]["shares_per_side"],
        )

    def test_unknown_market_uses_neutral_bandit(self):
        """Markets with no bandit row fall back to 1.0 neutral multiplier."""
        from profit.allocator import allocate_portfolio
        markets = [_make_scored_market("new")]
        cal = _make_mock_calibrator()
        allocs = allocate_portfolio(markets, 10000.0, cal, self.db_path)
        deploy = [a for a in allocs if a["action"] == "deploy"]
        self.assertEqual(len(deploy), 1)
        self.assertEqual(deploy[0]["_bandit"], 1.0)

    def test_hostile_regime_reduces_capital(self):
        """STEP 9: in hostile regime, deployed capital must not exceed
        HOSTILE_CAPITAL_SCALE × effective_capital + 1%.

        Test at a scale where baseline would exhaust the deployable budget,
        so regime scaling actually binds (at small portfolio sizes the
        per-market cap is the binding constraint, not regime)."""
        from profit.allocator import allocate_portfolio
        from profit.regime import HOSTILE_CAPITAL_SCALE

        # Enough markets to saturate any per-market cap. With 30 markets at
        # $200 per-market cap = $6000 worst-case > 30% × $10k = $3000 budget.
        markets = [_make_scored_market(f"m{i}", score=1.0)
                    for i in range(30)]
        preds = {f"m{i}": _make_predictions(f"m{i}", ev=1.0)
                 for i in range(30)}
        cal = _make_mock_calibrator(preds)

        # Baseline — no fills → regime = normal
        baseline = allocate_portfolio(markets, 10000.0, cal, self.db_path)
        baseline_total = sum(
            a.get("est_capital_cost", 0) for a in baseline
            if a["action"] == "deploy"
        )

        # Seed hostile regime (fills/hour/market > threshold).
        now = time.time()
        db = sqlite3.connect(self.db_path)
        for cid in [f"m{i}" for i in range(30)]:
            db.execute(
                "INSERT INTO orders_placed (ts, condition_id, side, price, size) "
                "VALUES (?,?,?,?,?)",
                (now - 600, cid, "yes", 0.5, 50),
            )
        # Flood: 100 fills in last hour across 30 active markets → rate=3.3
        for j in range(100):
            db.execute(
                "INSERT INTO fills (ts, condition_id, side, fill_type, "
                "shares, price, clob_cost, usd_value) VALUES (?,?,?,?,?,?,?,?)",
                (now - 3000 + j * 20, f"m{j % 30}", "yes", "FULL",
                 50, 0.5, 0.5, 25),
            )
        db.commit()
        db.close()

        hostile = allocate_portfolio(markets, 10000.0, cal, self.db_path)
        hostile_total = sum(
            a.get("est_capital_cost", 0) for a in hostile
            if a["action"] == "deploy"
        )

        # Primary invariant: hostile must not exceed the regime-scaled budget.
        # effective_capital ≈ 10000 * eff_scale (0.30 floor, empty DB) = 3000
        # hostile budget = 3000 * 0.70 = 2100 (conservation allows +1%)
        hostile_budget = 10000.0 * 0.30 * HOSTILE_CAPITAL_SCALE
        self.assertLessEqual(hostile_total, hostile_budget * 1.01)
        # Strict monotonicity: hostile < baseline when baseline saturates
        self.assertLess(hostile_total, baseline_total)

    def test_safety_filter_still_runs_downstream(self):
        """Safety layer is NOT bypassed by Phase 4. The allocator returns a
        list of dicts; the safety filter is a separate layer that processes
        that list. We verify the list is a valid input to the safety layer
        (integration test of the contract, not a re-test of safety itself)."""
        from profit.allocator import allocate_portfolio
        from oversight.safety_controller import SafetyController

        markets = [_make_scored_market("s1", score=1.0)]
        cal = _make_mock_calibrator()
        allocs = allocate_portfolio(markets, 10000.0, cal, self.db_path)

        # Contract: every allocation dict has the keys safety_filter reads
        required_keys = {
            "condition_id", "action", "shares_per_side",
            "min_size", "max_spread",
        }
        for a in allocs:
            for k in required_keys:
                self.assertIn(k, a)

        # And safety must be callable on the output without raising.
        safety = SafetyController(db_path=self.db_path)
        filtered = safety.filter_allocations(allocs, 10000.0)
        self.assertIsInstance(filtered, list)

    def test_regime_normal_does_not_scale_capital(self):
        """STEP 11 invariant: regime scaling only activates in hostile mode."""
        from profit.allocator import allocate_portfolio
        markets = [_make_scored_market(f"n{i}", score=1.0) for i in range(3)]
        preds = {f"n{i}": _make_predictions(f"n{i}", ev=1.0) for i in range(3)}
        cal = _make_mock_calibrator(preds)

        # No fills at all → regime = normal (no active markets)
        allocs = allocate_portfolio(markets, 10000.0, cal, self.db_path)
        total = sum(a.get("est_capital_cost", 0) for a in allocs
                    if a["action"] == "deploy")
        # Should use full deployable; at 10k capital and eff_scale=0.30 (empty
        # DB) + min_size floors, total should be comfortably positive.
        self.assertGreater(total, 0)


# ───────────────────────────────────────────────────────────────
# Confidence hook (STEP 10)
# ───────────────────────────────────────────────────────────────


class TestAttributionConfidenceHook(unittest.TestCase):
    """STEP 10: attribution error > 30% → confidence *= 0.8."""

    def setUp(self):
        self.db_path = _make_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_attribution_error_zero_when_consistent(self):
        from calibration.attribution import (
            compute_attribution, get_attribution_error,
        )
        db = sqlite3.connect(self.db_path)
        db.execute(
            "INSERT INTO reward_daily (date, total_combined_usd) VALUES (?, ?)",
            ("2026-04-14", 100.0),
        )
        for cid, secs in [("a", 3600), ("b", 3600)]:
            db.execute(
                "INSERT INTO reward_daily_markets "
                "(date, condition_id, scoring_seconds, daily_rate) "
                "VALUES (?, ?, ?, ?)",
                ("2026-04-14", cid, secs, 5.0),
            )
        db.commit()
        db.close()
        compute_attribution(self.db_path, date_str="2026-04-14")
        err = get_attribution_error(self.db_path, date_str="2026-04-14")
        self.assertLess(err, 1e-6)

    def test_attribution_error_no_data_returns_zero(self):
        """Invariant 5: missing data → 0 error (penalty off)."""
        from calibration.attribution import get_attribution_error
        err = get_attribution_error(self.db_path, date_str="1999-01-01")
        self.assertEqual(err, 0.0)


if __name__ == "__main__":
    unittest.main()
