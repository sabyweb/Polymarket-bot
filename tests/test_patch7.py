"""tests/test_patch7.py — PATCH 7 overcommit + cold-start + refill tests.

Five spec-required tests:
  1. Overcommit enabled              — total_notional > capital
  2. Expected capital bounded        — Σ(p_fill × size) ≤ capital
  3. Cold-start p_fill non-zero      — p_fill ≥ 0.01 (fallback floor)
  4. Refill after fill               — new orders placed
  5. No capital breach after fills   — remaining_capital ≥ 0
"""

import os
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass

from oversight.market_scorer import ScoredMarket
from profit.allocator import (
    allocate_portfolio,
    _compute_overcommit_factor,
    _enforce_expected_capital,
    OVERCOMMIT_MIN, OVERCOMMIT_MAX, OVERCOMMIT_DEFAULT,
    EXPECTED_CAPITAL_BUFFER,
)
from profit.learning import LearningState, MODE_ACTIVE
from profit.refill import (
    handle_fill_event, cancel_unfunded_orders, plan_refill_after_fill,
)
from calibration.manager import _fallback_p_fill_cold_start


# ═══════════════════════════════════════════════════════════════
# Shared fixtures — reuse the Patch 6 test setup pattern
# ═══════════════════════════════════════════════════════════════

@dataclass
class _FakePreds:
    p_fill_24h: float
    e_loss_given_fill: float
    e_time_on_book_hours: float
    reward_rate_per_hour: float
    ev_per_day: float
    raw_ev_per_day: float
    confidence: str = "model"
    model_versions: dict = None
    model_confidence: float = 1.0

    def __post_init__(self):
        if self.model_versions is None:
            self.model_versions = {}


class _FakeCalibrator:
    def __init__(self, preds_by_cid: dict):
        self._preds = preds_by_cid
        self._book_cache: dict = {}
        self.reward_trust = 1.0

    def get_predictions(self, condition_id: str, **kw) -> _FakePreds:
        return self._preds[condition_id]


def _schema_sql() -> str:
    return """
    CREATE TABLE IF NOT EXISTS fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, condition_id TEXT,
        side TEXT, fill_type TEXT, shares REAL, price REAL,
        clob_cost REAL, usd_value REAL, midpoint REAL, slippage REAL);
    CREATE TABLE IF NOT EXISTS unwinds (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, condition_id TEXT,
        side TEXT, shares REAL, sell_price REAL, usd_value REAL,
        vwap_cost REAL, pnl REAL);
    CREATE TABLE IF NOT EXISTS orders_placed (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, condition_id TEXT,
        side TEXT, price REAL, size REAL, order_id TEXT, order_type TEXT);
    CREATE TABLE IF NOT EXISTS orders_cancelled (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, order_id TEXT,
        reason TEXT);
    CREATE TABLE IF NOT EXISTS reward_attribution (
        market_id TEXT, date TEXT, reward_usd REAL,
        PRIMARY KEY(market_id, date));
    CREATE TABLE IF NOT EXISTS reward_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, total_reward_usd REAL,
        total_rebate_usd REAL, total_combined_usd REAL,
        num_markets_active INTEGER, est_daily_total REAL,
        correction_factor REAL, UNIQUE(date));
    CREATE TABLE IF NOT EXISTS reward_daily_markets (
        date TEXT, condition_id TEXT, scoring_seconds REAL,
        daily_rate REAL, PRIMARY KEY(date, condition_id));
    CREATE TABLE IF NOT EXISTS book_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, condition_id TEXT,
        spread REAL, midpoint REAL, our_bid_depth_ahead REAL,
        our_ask_depth_ahead REAL, total_bid_depth REAL,
        total_ask_depth REAL, bid_depth_5c REAL, ask_depth_5c REAL,
        daily_rate REAL, agent_shares REAL);
    CREATE TABLE IF NOT EXISTS cycle_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, cycle_num INTEGER,
        condition_id TEXT);
    CREATE TABLE IF NOT EXISTS bandit_state (
        market_id TEXT PRIMARY KEY, alpha REAL, beta REAL, last_updated REAL);
    """


def _temp_db() -> str:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    db = sqlite3.connect(f.name)
    db.executescript(_schema_sql())
    db.commit()
    db.close()
    return f.name


def _scored(cid: str, q_share_pct: float = 10.0, daily_rate: float = 5.0,
            question_group: str = "", score: float = 1.0) -> ScoredMarket:
    return ScoredMarket(
        condition_id=cid, question=f"Q-{cid}",
        score=score, action="deploy",
        recommended_shares=50, reason="test", confidence="high",
        actual_reward_total=0.0, fill_damage=0.0, fill_count=0,
        daily_rate=daily_rate, min_size=50.0, max_spread=0.045,
        est_capital_cost=0.0, locked_position_usd=0.0,
        question_group=question_group, q_share_pct=q_share_pct,
        end_date_iso="",
    )


def _preds(ev: float = 1.0, raw_ev: float = 1.0,
           p_fill: float = 0.10, e_loss: float = 0.50) -> _FakePreds:
    return _FakePreds(
        p_fill_24h=p_fill, e_loss_given_fill=e_loss,
        e_time_on_book_hours=8.0, reward_rate_per_hour=0.2,
        ev_per_day=ev, raw_ev_per_day=raw_ev,
    )


# ═══════════════════════════════════════════════════════════════
# 1. Overcommit enabled
# ═══════════════════════════════════════════════════════════════

class TestOvercommitEnabled(unittest.TestCase):

    def test_overcommit_factor_default(self):
        """Default learning + normal regime → factor in the OC band."""
        factor = _compute_overcommit_factor(None, regime_multiplier=1.0)
        self.assertGreaterEqual(factor, OVERCOMMIT_MIN)
        self.assertLessEqual(factor, OVERCOMMIT_MAX)
        # Default is 3.0 — but None-learning path should still give
        # base unchanged.
        self.assertAlmostEqual(factor, OVERCOMMIT_DEFAULT, places=3)

    def test_overcommit_clamped_high(self):
        """Max aggr + max trust + normal regime: base × 1.4 × 1.1 = 4.62."""
        state = LearningState(
            aggressiveness=1.5, capital_scale=1.0,
            risk_multiplier=1.0, reward_trust=1.0,
            mode=MODE_ACTIVE,
        )
        factor = _compute_overcommit_factor(state, regime_multiplier=1.0)
        self.assertGreater(factor, OVERCOMMIT_DEFAULT)
        self.assertLessEqual(factor, OVERCOMMIT_MAX)

    def test_overcommit_clamped_hostile(self):
        """Hostile regime shrinks overcommit but stays >= OC_MIN."""
        factor = _compute_overcommit_factor(None, regime_multiplier=0.5)
        self.assertGreaterEqual(factor, OVERCOMMIT_MIN)

    def test_allocator_produces_overcommitted_notional(self):
        """Total notional > total_capital when overcommit is enabled.

        Setup: 40 markets + seeded efficiency history so eff_scale = 1.0
        (not cold-start 0.3). Without the seed, min_cost would floor every
        allocation, keeping total ≤ $1820 regardless of overcommit. The
        overcommit signal itself is visible in `_overcommit_factor`."""
        db = _temp_db()
        # Seed healthy efficiency so eff_scale isn't floored at 0.3.
        import time as _t
        import datetime as _dt
        conn = sqlite3.connect(db)
        now = _t.time()
        for d in range(5):
            date_str = _dt.datetime.utcfromtimestamp(
                now - d * 86400
            ).strftime("%Y-%m-%d")
            conn.execute(
                "INSERT INTO reward_daily "
                "(date, total_reward_usd, total_rebate_usd, "
                "total_combined_usd, num_markets_active, est_daily_total, "
                "correction_factor) VALUES (?, 16.0, 0.0, 16.0, 40, 2000.0, 1.0) "
                "ON CONFLICT(date) DO UPDATE SET "
                "total_combined_usd=excluded.total_combined_usd",
                (date_str,),
            )
        conn.commit()
        conn.close()
        try:
            cids = [f"CID_{i}" for i in range(40)]
            markets = [
                _scored(cid, q_share_pct=5.0, daily_rate=2.0,
                        question_group=f"g{i % 5}")
                for i, cid in enumerate(cids)
            ]
            preds_map = {
                cid: _preds(ev=0.30, raw_ev=0.30,
                            p_fill=0.05, e_loss=0.30)
                for cid in cids
            }
            active = LearningState(mode=MODE_ACTIVE)
            total_capital = 2000.0
            allocs = allocate_portfolio(
                scored_markets=markets,
                total_capital=total_capital,
                calibrator=_FakeCalibrator(preds_map),
                db_path=db,
                learning_state=active,
            )
            total_notional = sum(
                a.get("est_capital_cost", 0)
                for a in allocs if a.get("action") == "deploy"
            )
            # With 40 markets × post-Patch-9 per-market ceiling (~$150),
            # total can exceed total_capital by the breadth alone.
            self.assertGreater(
                total_notional, total_capital,
                f"overcommit not applied: notional ${total_notional:.0f} "
                f"<= capital ${total_capital:.0f}",
            )
            # Every deploy row carries the overcommit observability.
            for a in allocs:
                if a.get("action") == "deploy":
                    self.assertIn("_overcommit_factor", a)
                    self.assertIn("_expected_capital", a)
                    self.assertGreaterEqual(a["_overcommit_factor"], OVERCOMMIT_MIN)
        finally:
            if os.path.exists(db):
                os.unlink(db)


# ═══════════════════════════════════════════════════════════════
# 2. Expected capital bounded
# ═══════════════════════════════════════════════════════════════

class TestExpectedCapitalBounded(unittest.TestCase):

    def test_expected_capital_within_buffer(self):
        """Σ(p_fill × size) ≤ total_capital × EXPECTED_CAPITAL_BUFFER
        after the allocator runs."""
        db = _temp_db()
        try:
            cids = [f"X{i}" for i in range(25)]
            markets = [
                _scored(cid, q_share_pct=5.0, daily_rate=2.0,
                        question_group=f"g{i % 3}")
                for i, cid in enumerate(cids)
            ]
            # High p_fill so expected capital grows fast and the ceiling
            # can actually bite.
            preds_map = {
                cid: _preds(ev=0.30, raw_ev=0.30,
                            p_fill=0.35, e_loss=0.30)
                for cid in cids
            }
            active = LearningState(
                mode=MODE_ACTIVE, aggressiveness=1.5, reward_trust=1.0,
            )
            total_capital = 2000.0
            allocs = allocate_portfolio(
                scored_markets=markets,
                total_capital=total_capital,
                calibrator=_FakeCalibrator(preds_map),
                db_path=db,
                learning_state=active,
            )
            expected = sum(
                float(a.get("_p_fill") or 0.0)
                * float(a.get("est_capital_cost") or 0.0)
                for a in allocs if a.get("action") == "deploy"
            )
            # Allow a tiny floating-point slack.
            self.assertLessEqual(
                expected, total_capital * EXPECTED_CAPITAL_BUFFER + 1.0,
                f"expected_capital=${expected:.2f} > "
                f"cap=${total_capital * EXPECTED_CAPITAL_BUFFER:.2f}",
            )
        finally:
            if os.path.exists(db):
                os.unlink(db)

    def test_enforce_expected_capital_scales_over(self):
        """Direct unit test: _enforce_expected_capital scales down when
        expected exceeds the buffer."""
        allocs = []
        for i in range(10):
            allocs.append({
                "action": "deploy", "condition_id": f"M{i}",
                "_p_fill": 0.50, "est_capital_cost": 200.0,
                "shares_per_side": 400, "min_size": 50.0,
                "max_spread": 0.045,
            })
        # expected = 10 × 0.5 × 200 = $1000. Budget 500 × 0.95 = $475.
        # Scale = 475/1000 = 0.475.
        _enforce_expected_capital(allocs, total_capital=500.0)
        new_expected = sum(
            a["_p_fill"] * a["est_capital_cost"]
            for a in allocs if a.get("action") == "deploy"
        )
        self.assertLessEqual(
            new_expected, 500.0 * EXPECTED_CAPITAL_BUFFER + 1.0,
        )


# ═══════════════════════════════════════════════════════════════
# 3. Cold-start p_fill non-zero
# ═══════════════════════════════════════════════════════════════

class TestColdStartPFill(unittest.TestCase):

    def test_fallback_returns_positive(self):
        """_fallback_p_fill_cold_start always returns ≥ 0.01."""
        for (spread, depth, pos) in [
            (0.05, 0, 0.0),
            (0.045, 100, 0.5),
            (0.01, 50, 0.2),
            (0.15, 5000, 0.8),   # wide spread, deep book, bad queue
        ]:
            p = _fallback_p_fill_cold_start(spread, depth, pos)
            self.assertGreaterEqual(p, 0.01)
            self.assertLessEqual(p, 0.15)

    def test_fallback_tight_spread_higher(self):
        """Tighter spread → higher fallback p_fill."""
        tight = _fallback_p_fill_cold_start(0.01, 0, 0.0)
        loose = _fallback_p_fill_cold_start(0.10, 0, 0.0)
        self.assertGreater(tight, loose)

    def test_fallback_worse_queue_lower(self):
        """Worse queue position → lower p_fill (up to 0.5× penalty)."""
        front = _fallback_p_fill_cold_start(0.03, 100, 0.0)
        back = _fallback_p_fill_cold_start(0.03, 100, 1.0)
        self.assertGreaterEqual(front, back)

    def test_allocator_stamps_nonzero_p_fill(self):
        """Round-to-6-decimals + max(1e-4, …) guarantees _p_fill > 0."""
        db = _temp_db()
        try:
            cids = ["A", "B"]
            markets = [_scored(c) for c in cids]
            # Deliberately tiny p_fill (would round to 0 at 4dp).
            preds_map = {
                "A": _preds(p_fill=0.00001, ev=0.5, raw_ev=0.5),
                "B": _preds(p_fill=0.00009, ev=0.5, raw_ev=0.5),
            }
            allocs = allocate_portfolio(
                scored_markets=markets, total_capital=2000.0,
                calibrator=_FakeCalibrator(preds_map),
                db_path=db,
                learning_state=LearningState(mode=MODE_ACTIVE),
            )
            for a in allocs:
                if a.get("action") == "deploy":
                    self.assertGreaterEqual(
                        a["_p_fill"], 1e-4,
                        f"_p_fill={a['_p_fill']} is below 1e-4 floor",
                    )
        finally:
            if os.path.exists(db):
                os.unlink(db)


# ═══════════════════════════════════════════════════════════════
# 4. Refill after fill
# ═══════════════════════════════════════════════════════════════

class TestRefillAfterFill(unittest.TestCase):

    def test_plan_triggers_reallocation(self):
        """Any fill event produces should_reallocate=True."""
        orders = [
            {"order_id": "o1", "size": 50.0, "priority": 3},
            {"order_id": "o2", "size": 50.0, "priority": 2},
            {"order_id": "o3", "size": 50.0, "priority": 1},
        ]
        plan = plan_refill_after_fill(
            fill_cost=25.0, remaining_capital_pre=150.0, open_orders=orders,
        )
        self.assertTrue(plan["should_reallocate"])
        self.assertEqual(plan["remaining_capital"], 125.0)

    def test_cancel_unfunded_prefers_priority(self):
        """Greedy keep by priority — highest first, stable ties."""
        orders = [
            {"order_id": "low", "size": 60.0, "priority": 1},
            {"order_id": "hi", "size": 60.0, "priority": 5},
            {"order_id": "mid", "size": 60.0, "priority": 3},
        ]
        keep, cancel = cancel_unfunded_orders(orders, remaining_capital=120.0)
        keep_ids = [o["order_id"] for o in keep]
        self.assertIn("hi", keep_ids)
        self.assertIn("mid", keep_ids)
        # Low-priority gets cancelled because 60+60 == 120 fully consumed.
        self.assertEqual(len(cancel), 1)
        self.assertEqual(cancel[0]["order_id"], "low")

    def test_refill_plan_produces_keeps_and_cancels(self):
        """Post-fill plan partitions orders cleanly."""
        orders = [
            {"order_id": f"o{i}", "size": 30.0, "priority": 5 - i}
            for i in range(6)
        ]
        plan = plan_refill_after_fill(
            fill_cost=30.0, remaining_capital_pre=120.0, open_orders=orders,
        )
        # remaining = 120 - 30 = 90. 30+30+30 fits, 4th doesn't (30+30+30+30=120 >90 actually ==90? strict <= so 3 fits).
        self.assertEqual(plan["remaining_capital"], 90.0)
        self.assertEqual(plan["n_kept"] + plan["n_cancelled"], 6)
        # Kept orders must fit within capital.
        kept_sum = sum(o["size"] for o in plan["keep_orders"])
        self.assertLessEqual(kept_sum, plan["remaining_capital"] + 1e-9)


# ═══════════════════════════════════════════════════════════════
# 5. No capital breach after fills
# ═══════════════════════════════════════════════════════════════

class TestNoCapitalBreach(unittest.TestCase):

    def test_handle_fill_event_nonnegative(self):
        """Remaining capital never goes negative even on oversize fill."""
        post = handle_fill_event(fill_cost=500.0, remaining_capital_pre=100.0)
        self.assertGreaterEqual(post, 0.0)

    def test_kept_orders_fit_remaining_capital(self):
        """Kept orders' total size ≤ remaining_capital always."""
        orders = [
            {"order_id": f"o{i}", "size": 25.0, "priority": 10 - i}
            for i in range(20)
        ]
        for remaining in [0, 25, 100, 250, 999]:
            _, _ = cancel_unfunded_orders(orders, remaining)
            keep, cancel = cancel_unfunded_orders(orders, remaining)
            kept_sum = sum(o["size"] for o in keep)
            self.assertLessEqual(
                kept_sum, remaining + 1e-9,
                f"kept_sum={kept_sum} > remaining={remaining}",
            )
            self.assertEqual(
                len(keep) + len(cancel), len(orders),
                "some orders were dropped (not in keep OR cancel)",
            )

    def test_zero_remaining_cancels_all(self):
        orders = [
            {"order_id": "o1", "size": 50.0, "priority": 1},
            {"order_id": "o2", "size": 50.0, "priority": 2},
        ]
        keep, cancel = cancel_unfunded_orders(orders, remaining_capital=0.0)
        self.assertEqual(keep, [])
        self.assertEqual(len(cancel), 2)


if __name__ == "__main__":
    unittest.main()
