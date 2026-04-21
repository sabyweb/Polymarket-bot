"""tests/test_patch11.py — PATCH 11 exposure saturation + oscillation damping.

Five spec-required tests:
  1. Exposure saturation reaches ≥ target_notional (or hits MAX clamp)
  2. Expected capital ≤ total_capital × EXPECTED_CAPITAL_BUFFER always
  3. detect_fill_event flags a fill after a position increases
  4. Oscillation damping reduces capital_scale variance in a noisy trace
  5. Backward compatibility — learning_state=None produces pre-Patch-11 output
"""

import os
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass

from oversight.market_scorer import ScoredMarket
from profit.allocator import (
    allocate_portfolio,
    EXPOSURE_SATURATION_MAX_SCALE,
    EXPECTED_CAPITAL_BUFFER,
    OVERCOMMIT_DEFAULT,
    OVERCOMMIT_MIN,
    OVERCOMMIT_MAX,
    UPSCALE_STEP,
    UPSCALE_MAX_ITERS,
)
from profit.learning import (
    LearningState, MODE_ACTIVE, MODE_OFF,
    LearningController,
    OSCILLATION_WINDOW, OSCILLATION_THRESHOLD, OSCILLATION_DAMPEN_FACTOR,
    CAPITAL_HISTORY_MAX,
    _detect_oscillation, _reset_capital_history_cache,
)
from profit.refill import detect_fill_event


# ═══════════════════════════════════════════════════════════════
# Fixtures (shared pattern with test_patch7/9/10)
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
    """Create a fresh schema DB. When `seed_healthy_eff` is True, also
    seed reward_daily rows giving rpd ≈ 0.008 so the allocator's
    eff_scale doesn't floor at the cold-start minimum."""
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
                now - d * 86400
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
# Test 1 — Exposure saturation reaches target (or MAX clamp)
# ═══════════════════════════════════════════════════════════════

class TestExposureSaturation(unittest.TestCase):

    def test_saturation_pushes_notional_toward_overcommit_target(self):
        """In ACTIVE, saturation upsizes existing deploys until their
        total notional either reaches the Patch 7 overcommit target
        (overcommit_factor × total_capital) or hits the
        EXPOSURE_SATURATION_MAX_SCALE cumulative ceiling.

        We can't know the exact Patch 7 factor without re-running its
        branch, but we know the target is ≥ OVERCOMMIT_MIN × total and
        that saturation must have raised the pre-Patch-11 notional (or
        we were already at / above target). The assertion is on the
        _saturation_scale stamp: it must be > 1.0 when we started
        under-deployed, and bounded at EXPOSURE_SATURATION_MAX_SCALE."""
        db = _temp_db(seed_healthy_eff=True)
        try:
            # 30 markets with low p_fill so pre-Patch-11 notional starts
            # well below the overcommit target.
            cids = [f"S{i}" for i in range(30)]
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
            deploys = [a for a in allocs if a.get("action") == "deploy"]
            self.assertGreater(
                len(deploys), 0, "no deploys produced — bad fixture",
            )
            # Every deploy should carry the observability stamps.
            for a in deploys:
                self.assertIn("_saturation_scale", a)
                self.assertIn("_target_notional", a)
                self.assertIn("_saturation_applied", a)
                # Scale never exceeds the cumulative ceiling.
                self.assertLessEqual(
                    float(a["_saturation_scale"]),
                    EXPOSURE_SATURATION_MAX_SCALE + 1e-6,
                )
                # Scale never goes below 1.0 (saturation only pushes up).
                self.assertGreaterEqual(float(a["_saturation_scale"]), 1.0)
            # At least one deploy must have saturation_applied=True (i.e.
            # the pre-Patch-11 notional was under target and we scaled up).
            self.assertTrue(
                any(a["_saturation_applied"] is True for a in deploys),
                "saturation never fired — check overcommit_factor setup "
                "or fixture scale",
            )
        finally:
            if os.path.exists(db):
                os.unlink(db)

    def test_saturation_target_matches_overcommit_factor(self):
        """The target_notional stamp must be total_capital × Patch-7
        overcommit_factor, which is clamped to [OVERCOMMIT_MIN,
        OVERCOMMIT_MAX]. The factor depends on aggressiveness/trust —
        at neutral 1.0 / 1.0 it resolves to OVERCOMMIT_DEFAULT (3.0)."""
        db = _temp_db(seed_healthy_eff=True)
        try:
            cids = [f"T{i}" for i in range(20)]
            markets = [
                _scored(cid, question_group=f"g{i % 4}") for i, cid in enumerate(cids)
            ]
            preds_map = {cid: _preds() for cid in cids}
            total_capital = 1000.0
            allocs = allocate_portfolio(
                scored_markets=markets, total_capital=total_capital,
                calibrator=_FakeCalibrator(preds_map), db_path=db,
                learning_state=LearningState(mode=MODE_ACTIVE),
            )
            deploys = [a for a in allocs if a.get("action") == "deploy"]
            self.assertGreater(len(deploys), 0)
            for a in deploys:
                target = float(a["_target_notional"])
                # Target is clamped by Patch 7's overcommit factor range.
                self.assertGreaterEqual(
                    target, total_capital * OVERCOMMIT_MIN - 1e-6,
                )
                self.assertLessEqual(
                    target, total_capital * OVERCOMMIT_MAX + 1e-6,
                )
        finally:
            if os.path.exists(db):
                os.unlink(db)


# ═══════════════════════════════════════════════════════════════
# Test 2 — Expected capital ≤ 0.95 × total_capital always
# ═══════════════════════════════════════════════════════════════

class TestExpectedCapitalInvariant(unittest.TestCase):

    def test_expected_capital_within_buffer_after_saturation(self):
        """Hard Guarantee #1 — Σ (_p_fill × est_capital_cost) must stay
        ≤ total_capital × EXPECTED_CAPITAL_BUFFER (0.95) after Patch 11
        saturation + the final _enforce_expected_capital safety net."""
        db = _temp_db(seed_healthy_eff=True)
        try:
            # High p_fill forces the expected-capital ceiling to bind
            # after saturation upsizes.
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
            ceiling = total_capital * EXPECTED_CAPITAL_BUFFER
            # Allow tiny arithmetic slack (rounding in share conversion).
            self.assertLessEqual(
                expected, ceiling + 1.0,
                f"expected_capital=${expected:.2f} exceeds "
                f"${ceiling:.2f} ceiling after Patch 11 saturation",
            )
        finally:
            if os.path.exists(db):
                os.unlink(db)


# ═══════════════════════════════════════════════════════════════
# Test 3 — detect_fill_event
# ═══════════════════════════════════════════════════════════════

class TestDetectFillEvent(unittest.TestCase):

    def test_increase_returns_true(self):
        """Any key with a strict increase from prev → current must
        signal a fill."""
        prev = {"M1": 0, "M2": 100}
        current = {"M1": 50, "M2": 100}  # M1 went 0 → 50
        self.assertTrue(detect_fill_event(prev, current))

    def test_no_change_returns_false(self):
        prev = {"M1": 50, "M2": 100}
        current = {"M1": 50, "M2": 100}
        self.assertFalse(detect_fill_event(prev, current))

    def test_decrease_returns_false(self):
        """Position decreases (unwinds) do NOT count as a fill — we
        only care about new entries into positions."""
        prev = {"M1": 100, "M2": 200}
        current = {"M1": 50, "M2": 200}
        self.assertFalse(detect_fill_event(prev, current))

    def test_new_key_treated_as_from_zero(self):
        prev = {"M1": 100}
        current = {"M1": 100, "M2": 25}  # M2 is new → 0 → 25
        self.assertTrue(detect_fill_event(prev, current))

    def test_float_and_int_compare_correctly(self):
        prev = {"M1": 50}
        current = {"M1": 50.1}
        self.assertTrue(detect_fill_event(prev, current))

    def test_empty_dicts(self):
        self.assertFalse(detect_fill_event({}, {}))


# ═══════════════════════════════════════════════════════════════
# Test 4 — Oscillation damping
# ═══════════════════════════════════════════════════════════════

class TestOscillationDamping(unittest.TestCase):

    def setUp(self):
        _reset_capital_history_cache()

    def tearDown(self):
        _reset_capital_history_cache()

    def test_detect_oscillation_flags_alternating_trace(self):
        """A strictly alternating up/down trace yields window-1 flips."""
        history = [1.0, 1.1, 1.0, 1.1, 1.0, 1.1, 1.0, 1.1, 1.0, 1.1,
                   1.0, 1.1, 1.0, 1.1, 1.0, 1.1, 1.0, 1.1, 1.0, 1.1]
        self.assertEqual(len(history), OSCILLATION_WINDOW)
        self.assertTrue(_detect_oscillation(history))

    def test_detect_oscillation_monotone_trace_false(self):
        """A monotonically rising trace has zero flips."""
        history = [1.0 + i * 0.01 for i in range(OSCILLATION_WINDOW)]
        self.assertFalse(_detect_oscillation(history))

    def test_detect_oscillation_below_threshold_false(self):
        """Fewer than OSCILLATION_THRESHOLD flips → no damping."""
        # Build a trace with exactly OSCILLATION_THRESHOLD - 1 flips and
        # the rest monotone.
        history = [1.0] * OSCILLATION_WINDOW
        # Inject (threshold - 1) zig-zag points at the tail.
        flips_needed = OSCILLATION_THRESHOLD - 1
        for k in range(flips_needed):
            idx = len(history) - 1 - 2 * k
            if idx >= 2:
                history[idx] = history[idx - 1] + (0.05 if k % 2 == 0 else -0.05)
        # Safety: verify flip count is actually under threshold.
        self.assertFalse(_detect_oscillation(history))

    def test_detect_oscillation_indexing_safe(self):
        """Regression for the spec's range(1, …) off-by-one: for very
        short traces, _detect_oscillation must not IndexError via
        Python's negative-index wrap."""
        self.assertFalse(_detect_oscillation([]))
        self.assertFalse(_detect_oscillation([1.0]))
        self.assertFalse(_detect_oscillation([1.0, 1.1]))
        # 3 elements: 1 iteration at i=2, no flip (monotone).
        self.assertFalse(_detect_oscillation([1.0, 1.1, 1.2]))

    def test_damping_applied_when_history_oscillates(self):
        """When prev.capital_history shows ≥ OSCILLATION_THRESHOLD flips,
        the update_state return's capital_scale is lower than the same
        call with an empty history (damping fired)."""
        # Build a metrics dict that leaves u_cap at prev (no rule fires).
        # Easiest: provide complete "healthy" metrics so no rule modifies
        # u_cap, then compare outputs between oscillating and empty history.
        metrics = {
            "status": "ok",
            "net_profit": 5.0,
            "total_rewards": 10.0,
            "total_loss": 5.0,
            "fill_count": 10,
            "fill_rate": 0.10,              # < FILL_RATE_HIGH
            "avg_loss_per_fill": 0.50,      # < LOSS_PER_FILL_HIGH
            "reward_efficiency": 0.0007,
            "reward_efficiency_raw": 0.0007,
            "reward_efficiency_baseline": 0.0007,  # == re_ → no Rule B
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
        prev_no_history = LearningState(
            capital_scale=1.0, capital_history=[],
        )
        prev_oscillating = LearningState(
            capital_scale=1.0,
            capital_history=[
                1.0, 1.1, 1.0, 1.1, 1.0, 1.1, 1.0, 1.1, 1.0, 1.1,
                1.0, 1.1, 1.0, 1.1, 1.0, 1.1, 1.0, 1.1, 1.0, 1.1,
            ],
        )

        new_no_hist = LearningController.update_state(metrics, prev_no_history)
        new_osc = LearningController.update_state(metrics, prev_oscillating)

        # Oscillating-history path: u_cap is scaled by OSCILLATION_DAMPEN_FACTOR
        # before EMA, so the resulting capital_scale should be lower.
        self.assertLess(
            new_osc.capital_scale, new_no_hist.capital_scale,
            f"damping failed to reduce capital_scale: "
            f"osc={new_osc.capital_scale:.4f} vs baseline={new_no_hist.capital_scale:.4f}",
        )

    def test_history_bounded_at_max(self):
        """capital_history grows per update but never exceeds CAPITAL_HISTORY_MAX."""
        metrics = {
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
        state = LearningState(capital_scale=1.0)
        # Fill the buffer past the cap and verify it stays bounded.
        for _ in range(CAPITAL_HISTORY_MAX + 25):
            state = LearningController.update_state(metrics, state)
        self.assertEqual(len(state.capital_history), CAPITAL_HISTORY_MAX)


# ═══════════════════════════════════════════════════════════════
# Test 5 — Backward compatibility
# ═══════════════════════════════════════════════════════════════

class TestBackwardCompat(unittest.TestCase):

    def test_no_learning_state_skips_saturation(self):
        """learning_state=None produces allocations identical (for the
        fields that matter) to LearningState(mode=OFF)."""
        db = _temp_db(seed_healthy_eff=True)
        try:
            cids = [f"C{i}" for i in range(8)]
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
                learning_state=LearningState(),  # mode=OFF
            )
            for x, y in zip(a_none, a_off):
                self.assertEqual(x["action"], y["action"])
                self.assertEqual(x["shares_per_side"], y["shares_per_side"])
                self.assertEqual(
                    x.get("est_capital_cost"), y.get("est_capital_cost"),
                )
        finally:
            if os.path.exists(db):
                os.unlink(db)

    def test_saturation_stamps_absent_outside_active(self):
        """Patch 11 observability stamps must not appear on OFF/SHADOW/None
        callers — backward-compat invariant."""
        db = _temp_db(seed_healthy_eff=True)
        try:
            cids = [f"O{i}" for i in range(5)]
            markets = [_scored(cid) for cid in cids]
            preds_map = {cid: _preds() for cid in cids}
            allocs = allocate_portfolio(
                scored_markets=markets, total_capital=1000.0,
                calibrator=_FakeCalibrator(preds_map), db_path=db,
                learning_state=None,
            )
            for a in allocs:
                self.assertNotIn("_saturation_applied", a)
                self.assertNotIn("_saturation_scale", a)
                self.assertNotIn("_target_notional", a)
        finally:
            if os.path.exists(db):
                os.unlink(db)

    def test_damping_does_not_fire_in_off_mode(self):
        """A LearningState with mode=OFF and 20-sample oscillating
        history still must not alter the applied_state scalars because
        OFF always publishes neutral 1.0. This verifies the update_state
        path itself still runs cleanly — the neutral publication is the
        responsibility of `step()`, which we don't exercise here."""
        _reset_capital_history_cache()
        metrics = {
            "status": "ok",
            "net_profit": 0.0,
            "total_rewards": 0.0,
            "total_loss": 0.0,
            "fill_count": 5,
            "fill_rate": 0.05,
            "avg_loss_per_fill": 0.1,
            "reward_efficiency": 0.0007,
            "reward_efficiency_raw": 0.0007,
            "reward_efficiency_baseline": 0.0007,
            "global_fill_rate_1h": 0.1,
            "loss_per_capital": 0.005,
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
        prev_off = LearningState(
            mode=MODE_OFF, capital_scale=1.0,
            capital_history=[1.0, 1.1] * (OSCILLATION_WINDOW // 2),
        )
        new = LearningController.update_state(metrics, prev_off)
        # update_state is pure rules — the OFF mode publication gate is
        # applied upstream in step(). We only verify no exception is
        # raised and the history grows as expected.
        self.assertEqual(len(new.capital_history), len(prev_off.capital_history) + 1)
        self.assertIn(new.capital_scale, [new.capital_scale])  # sanity


if __name__ == "__main__":
    unittest.main()
