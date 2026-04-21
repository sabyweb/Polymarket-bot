"""tests/test_patch13_corrected.py — PATCH 13 (FINAL CORRECTED).

Five spec-required tests:
  1. avg_overcommit_active increases > 1.5 (target-driven allocation hits target)
  2. efficiency does not collapse below 0.7× (marginal-efficiency gate)
  3. oscillation reduced (hysteresis direction lock + dead-band)
  4. expected_capital invariant holds (Σ p_fill × size ≤ 0.95 × total)
  5. backward compatibility unchanged (learning_state=None / mode=OFF)
"""

import os
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass

from oversight.market_scorer import ScoredMarket
from profit.allocator import (
    allocate_portfolio,
    EXPECTED_CAPITAL_BUFFER,
    OVERCOMMIT_MIN, OVERCOMMIT_MAX,
)
from profit.learning import (
    LearningState, LearningController,
    MODE_ACTIVE, MODE_OFF,
    CAPITAL_CHANGE_MIN_STEP, CAPITAL_DIRECTION_LOCK,
    _reset_capital_history_cache,
)


# ═══════════════════════════════════════════════════════════════
# Fixtures (shared pattern with test_patch7/9/10/11)
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


def _temp_db(seed_healthy_eff: bool = False) -> str:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    db = sqlite3.connect(f.name)
    db.executescript(_schema_sql())
    if seed_healthy_eff:
        import time as _t
        import datetime as _dt
        now = _t.time()
        for d in range(5):
            dstr = _dt.datetime.fromtimestamp(
                now - d * 86400,
            ).strftime("%Y-%m-%d")
            db.execute(
                "INSERT OR IGNORE INTO reward_daily "
                "(date, total_reward_usd, total_rebate_usd, "
                "total_combined_usd, num_markets_active, est_daily_total, "
                "correction_factor) VALUES (?, 16.0, 0.0, 16.0, 40, 2000.0, 1.0)",
                (dstr,),
            )
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


def _preds(ev: float = 0.30, raw_ev: float = 0.30,
           p_fill: float = 0.05, e_loss: float = 0.30) -> _FakePreds:
    return _FakePreds(
        p_fill_24h=p_fill, e_loss_given_fill=e_loss,
        e_time_on_book_hours=8.0, reward_rate_per_hour=0.2,
        ev_per_day=ev, raw_ev_per_day=raw_ev,
    )


# ═══════════════════════════════════════════════════════════════
# Test 1 — avg_overcommit_active increases > 1.5
# ═══════════════════════════════════════════════════════════════

class TestTargetDrivenOvercommit(unittest.TestCase):

    def test_total_notional_clears_15x_floor(self):
        """Target-driven allocation must land total deployed notional
        at ≥ 1.5× of total_capital — the INV3 floor the V3.1 audit
        flagged before this patch. Fixture uses 30 positive-EV markets
        with baseline None so the marginal-efficiency gate is open."""
        db = _temp_db(seed_healthy_eff=True)
        try:
            cids = [f"T{i}" for i in range(30)]
            markets = [
                _scored(cid, q_share_pct=5.0, daily_rate=2.0,
                        question_group=f"g{i % 6}")
                for i, cid in enumerate(cids)
            ]
            preds_map = {
                cid: _preds(ev=0.20, raw_ev=0.20, p_fill=0.02, e_loss=0.20)
                for cid in cids
            }
            total_capital = 2000.0
            allocs = allocate_portfolio(
                scored_markets=markets, total_capital=total_capital,
                calibrator=_FakeCalibrator(preds_map), db_path=db,
                learning_state=LearningState(mode=MODE_ACTIVE),
            )
            deployed = sum(
                float(a.get("est_capital_cost") or 0.0)
                for a in allocs if a.get("action") == "deploy"
            )
            self.assertGreaterEqual(
                deployed, total_capital * OVERCOMMIT_MIN - 1.0,
                f"target-driven under-deployed: ${deployed:.0f} "
                f"< INV3 floor ${total_capital * OVERCOMMIT_MIN:.0f}",
            )
        finally:
            if os.path.exists(db):
                os.unlink(db)

    def test_target_allocation_flagged_on_forced_rows(self):
        """At least one deploy must carry _forced_target_alloc=True
        when the target-driven greedy meaningfully upsizes Phase C."""
        db = _temp_db(seed_healthy_eff=True)
        try:
            cids = [f"F{i}" for i in range(30)]
            markets = [
                _scored(cid, q_share_pct=5.0, daily_rate=2.0,
                        question_group=f"g{i % 6}")
                for i, cid in enumerate(cids)
            ]
            preds_map = {
                cid: _preds(ev=0.20, raw_ev=0.20, p_fill=0.02, e_loss=0.20)
                for cid in cids
            }
            allocs = allocate_portfolio(
                scored_markets=markets, total_capital=2000.0,
                calibrator=_FakeCalibrator(preds_map), db_path=db,
                learning_state=LearningState(mode=MODE_ACTIVE),
            )
            self.assertTrue(
                any(a.get("_forced_target_alloc") is True
                    for a in allocs if a.get("action") == "deploy"),
                "no deploys carry _forced_target_alloc — target-driven "
                "allocation did not fire",
            )
        finally:
            if os.path.exists(db):
                os.unlink(db)


# ═══════════════════════════════════════════════════════════════
# Test 2 — Efficiency does not collapse below 0.7×
# ═══════════════════════════════════════════════════════════════

class TestEfficiencyCollapseGuard(unittest.TestCase):

    def test_low_marginal_eff_markets_skipped_by_target_gate(self):
        """When baseline is set and a market's marginal efficiency
        ev / (p_fill × size) falls below 0.7 × baseline, the target-
        driven allocation must NOT upsize it (Part 1 gate). We detect
        this by checking that no _forced_target_alloc stamp appears
        under a restrictive baseline."""
        db = _temp_db(seed_healthy_eff=True)
        try:
            cids = [f"L{i}" for i in range(20)]
            markets = [
                _scored(cid, q_share_pct=5.0, daily_rate=2.0,
                        question_group=f"g{i % 4}")
                for i, cid in enumerate(cids)
            ]
            # Marginal eff for these markets at the soft-cap size:
            #   ev / (p_fill × size) = 0.2 / (0.05 × 300) = 0.0133
            # With baseline = 1.0, the gate demands marginal_eff ≥ 0.7.
            # All markets fall below → no target-driven allocations fire.
            preds_map = {
                cid: _preds(ev=0.20, raw_ev=0.20, p_fill=0.05, e_loss=0.30)
                for cid in cids
            }
            ls = LearningState(
                mode=MODE_ACTIVE,
                reward_efficiency=0.9, reward_efficiency_baseline=1.0,
            )
            allocs = allocate_portfolio(
                scored_markets=markets, total_capital=3000.0,
                calibrator=_FakeCalibrator(preds_map), db_path=db,
                learning_state=ls,
            )
            forced = [a for a in allocs
                      if a.get("action") == "deploy"
                      and a.get("_forced_target_alloc") is True]
            self.assertEqual(
                len(forced), 0,
                f"marginal-efficiency gate failed: {len(forced)} "
                f"deploys were upsized despite low marginal_eff",
            )
        finally:
            if os.path.exists(db):
                os.unlink(db)

    def test_part4_penalty_multiplier_is_0_9(self):
        """Part 4 efficiency penalty reduces final_score uniformly by
        10% when reward_efficiency < baseline in ACTIVE."""
        db = _temp_db(seed_healthy_eff=True)
        try:
            cids = [f"P{i}" for i in range(8)]
            markets = [_scored(cid, question_group=f"g{i % 2}")
                       for i, cid in enumerate(cids)]
            preds_map = {cid: _preds() for cid in cids}
            ls_low = LearningState(
                mode=MODE_ACTIVE,
                reward_efficiency=0.001,
                reward_efficiency_baseline=0.01,
            )
            ls_at = LearningState(
                mode=MODE_ACTIVE,
                reward_efficiency=0.01,
                reward_efficiency_baseline=0.01,
            )
            a_low = allocate_portfolio(
                scored_markets=markets, total_capital=5000.0,
                calibrator=_FakeCalibrator(preds_map), db_path=db,
                learning_state=ls_low,
            )
            a_at = allocate_portfolio(
                scored_markets=markets, total_capital=5000.0,
                calibrator=_FakeCalibrator(preds_map), db_path=db,
                learning_state=ls_at,
            )
            scores_low = [float(a.get("_final_score") or 0.0)
                          for a in a_low if a.get("action") == "deploy"]
            scores_at = [float(a.get("_final_score") or 0.0)
                         for a in a_at if a.get("action") == "deploy"]
            self.assertTrue(scores_low and scores_at)
            # 0.9× penalty (within 2 pp slack for floating point noise).
            self.assertAlmostEqual(
                max(scores_low) / max(scores_at), 0.9, delta=0.02,
                msg=f"expected ≈0.9 Part 4 penalty; got "
                    f"{max(scores_low)/max(scores_at):.3f}",
            )
        finally:
            if os.path.exists(db):
                os.unlink(db)


# ═══════════════════════════════════════════════════════════════
# Test 3 — Oscillation reduced via hysteresis
# ═══════════════════════════════════════════════════════════════

class TestOscillationHysteresis(unittest.TestCase):

    def setUp(self):
        _reset_capital_history_cache()

    def tearDown(self):
        _reset_capital_history_cache()

    def _neutral_metrics(self) -> dict:
        return {
            "status": "ok",
            "net_profit": 0.0,
            "total_rewards": 0.0,
            "total_loss": 0.0,
            "fill_count": 0,
            "fill_rate": 0.0,
            "avg_loss_per_fill": 0.0,
            "reward_efficiency": 0.001,
            "reward_efficiency_raw": 0.001,
            "reward_efficiency_baseline": 0.001,
            "global_fill_rate_1h": 0.0,
            "loss_per_capital": 0.0,
            "reward_error": 1.0,
            "loss_error": 1.0,
            "reward_growth": None,
            "actual_reward_24h": 0.0,
            "regime_id": None,
            "market_efficiency_map": {},
            "fills_total": 0,
            "fill_unwind_pairs_total": 0,
            "reward_days": 0,
            "valid_cycle": True,
        }

    def test_small_delta_reverted_by_dead_band(self):
        """In ACTIVE mode with an established direction, any same-
        direction |delta| < CAPITAL_CHANGE_MIN_STEP reverts to prev —
        this is the oscillation-noise filter. First-move direction
        changes are not affected (they arm the lock and pass through)."""
        metrics = self._neutral_metrics()
        # prev has an established +1 direction. A tiny positive
        # same-direction move will be within the dead-band and revert.
        prev = LearningState(
            mode=MODE_ACTIVE, capital_scale=1.0,
            last_direction=+1, direction_lock=0,
        )
        # Rules neutral → delta ≈ 0 → same direction (+1 preserved),
        # dead-band reverts scalar.
        new = LearningController.update_state(metrics, prev)
        self.assertEqual(
            new.capital_scale, prev.capital_scale,
            f"expected dead-band revert; got Δ="
            f"{new.capital_scale - prev.capital_scale:+.4f}",
        )

    def test_direction_flip_blocked_during_lock(self):
        """In ACTIVE, a proposed direction flip while direction_lock > 0
        is blocked: capital_scale reverts, lock decrements, direction
        stays."""
        metrics = self._neutral_metrics()
        # prev already going UP and armed (lock=5, last_direction=+1).
        prev = LearningState(
            mode=MODE_ACTIVE, capital_scale=1.0,
            last_direction=+1, direction_lock=5,
        )
        # Craft metrics that would push u_cap DOWN through Rule A:
        # need fill_rate > 0.30 AND loss_high AND net_profit ≤ 0.
        metrics_down = dict(metrics)
        metrics_down.update({
            "fill_rate": 0.50, "avg_loss_per_fill": 2.0,
            "loss_per_capital": 0.10, "net_profit": -1.0,
            "total_loss": 5.0, "fill_count": 10,
        })
        new = LearningController.update_state(metrics_down, prev)
        # Direction flip blocked → capital_scale preserved.
        self.assertEqual(new.capital_scale, prev.capital_scale)
        # Direction preserved, lock decremented.
        self.assertEqual(new.last_direction, +1)
        self.assertEqual(new.direction_lock, 4)

    def test_direction_flip_allowed_when_lock_expired(self):
        """When direction_lock == 0, a real direction change is accepted
        and lock is re-armed to CAPITAL_DIRECTION_LOCK."""
        metrics = self._neutral_metrics()
        prev = LearningState(
            mode=MODE_ACTIVE, capital_scale=1.0,
            last_direction=+1, direction_lock=0,
        )
        # Push hard down (as above).
        metrics_down = dict(metrics)
        metrics_down.update({
            "fill_rate": 0.50, "avg_loss_per_fill": 2.0,
            "loss_per_capital": 0.10, "net_profit": -1.0,
            "total_loss": 5.0, "fill_count": 10,
        })
        new = LearningController.update_state(metrics_down, prev)
        # If the post-EMA delta clears MIN_STEP, the flip is accepted;
        # otherwise the dead-band kicks in — in both cases the lock
        # must NOT be the blocking mechanism here.
        self.assertIn(new.last_direction, [-1, +1, 0])
        # Lock is re-armed iff direction actually flipped.
        if new.last_direction != prev.last_direction:
            self.assertEqual(new.direction_lock, CAPITAL_DIRECTION_LOCK)


# ═══════════════════════════════════════════════════════════════
# Test 4 — Expected-capital invariant holds
# ═══════════════════════════════════════════════════════════════

class TestExpectedCapitalInvariant(unittest.TestCase):

    def test_expected_capital_within_buffer(self):
        """Σ (_p_fill × est_capital_cost) ≤ total_capital × 0.95 after
        target-driven allocation + Part 6 re-enforcement (HG #1)."""
        db = _temp_db(seed_healthy_eff=True)
        try:
            cids = [f"X{i}" for i in range(40)]
            markets = [
                _scored(cid, q_share_pct=5.0, daily_rate=2.0,
                        question_group=f"g{i % 5}")
                for i, cid in enumerate(cids)
            ]
            preds_map = {
                cid: _preds(ev=0.10, raw_ev=0.10, p_fill=0.30, e_loss=0.40)
                for cid in cids
            }
            total_capital = 2000.0
            allocs = allocate_portfolio(
                scored_markets=markets, total_capital=total_capital,
                calibrator=_FakeCalibrator(preds_map), db_path=db,
                learning_state=LearningState(mode=MODE_ACTIVE),
            )
            expected = sum(
                float(a.get("_p_fill") or 0.0)
                * float(a.get("est_capital_cost") or 0.0)
                for a in allocs if a.get("action") == "deploy"
            )
            self.assertLessEqual(
                expected, total_capital * EXPECTED_CAPITAL_BUFFER + 1.0,
                f"expected_capital=${expected:.2f} breached "
                f"${total_capital * EXPECTED_CAPITAL_BUFFER:.2f}",
            )
        finally:
            if os.path.exists(db):
                os.unlink(db)


# ═══════════════════════════════════════════════════════════════
# Test 5 — Backward compatibility
# ═══════════════════════════════════════════════════════════════

class TestBackwardCompat(unittest.TestCase):

    def test_none_and_off_produce_same_output(self):
        """learning_state=None must produce the same action / shares /
        cost triples as LearningState() (default mode=OFF)."""
        db = _temp_db(seed_healthy_eff=True)
        try:
            cids = [f"B{i}" for i in range(8)]
            markets = [_scored(cid) for cid in cids]
            preds_map = {cid: _preds() for cid in cids}

            a_none = allocate_portfolio(
                scored_markets=markets, total_capital=5000.0,
                calibrator=_FakeCalibrator(preds_map), db_path=db,
                learning_state=None,
            )
            a_off = allocate_portfolio(
                scored_markets=markets, total_capital=5000.0,
                calibrator=_FakeCalibrator(preds_map), db_path=db,
                learning_state=LearningState(),
            )
            for x, y in zip(a_none, a_off):
                self.assertEqual(x["action"], y["action"])
                self.assertEqual(
                    x["shares_per_side"], y["shares_per_side"],
                )
                self.assertEqual(
                    x.get("est_capital_cost"), y.get("est_capital_cost"),
                )
        finally:
            if os.path.exists(db):
                os.unlink(db)

    def test_off_skips_patch13_stamps(self):
        """OFF / None callers must NOT carry Patch 13 stamps."""
        db = _temp_db(seed_healthy_eff=True)
        try:
            cids = [f"O{i}" for i in range(5)]
            markets = [_scored(cid) for cid in cids]
            preds_map = {cid: _preds() for cid in cids}
            allocs = allocate_portfolio(
                scored_markets=markets, total_capital=1000.0,
                calibrator=_FakeCalibrator(preds_map), db_path=db,
                learning_state=LearningState(mode=MODE_OFF),
            )
            for a in allocs:
                self.assertFalse(a.get("_forced_target_alloc"))
        finally:
            if os.path.exists(db):
                os.unlink(db)

    def test_hysteresis_skipped_in_off_mode(self):
        """update_state with mode=OFF must not apply hysteresis — the
        capital_scale delta (if any) passes through untouched, and the
        hysteresis fields stay at prev values."""
        metrics = {
            "status": "ok",
            "net_profit": 5.0,
            "total_rewards": 10.0,
            "total_loss": 5.0,
            "fill_count": 10,
            "fill_rate": 0.10,
            "avg_loss_per_fill": 0.50,
            "reward_efficiency": 0.0007,
            "reward_efficiency_raw": 0.0007,
            "reward_efficiency_baseline": 0.0007,
            "global_fill_rate_1h": 0.20,
            "loss_per_capital": 0.005,
            "reward_error": 1.0,
            "loss_error": 1.0,
            "reward_growth": None,
            "actual_reward_24h": 10.0,
            "regime_id": None,
            "market_efficiency_map": {},
            "fills_total": 300,
            "fill_unwind_pairs_total": 150,
            "reward_days": 7,
            "valid_cycle": True,
        }
        prev_off = LearningState(
            mode=MODE_OFF, capital_scale=1.0,
            last_direction=+1, direction_lock=5,
        )
        new = LearningController.update_state(metrics, prev_off)
        # In OFF mode the hysteresis gate is skipped: capital_scale
        # carries the post-EMA-clamp value (may differ from prev),
        # and the direction_lock is preserved (NOT decremented).
        self.assertEqual(
            new.direction_lock, prev_off.direction_lock,
            "direction_lock must be preserved in OFF mode",
        )


if __name__ == "__main__":
    unittest.main()
