"""tests/test_continuous_allocator.py — §13 compliance for the new
continuous allocator (profit/allocator.py).

Verifies the hard guarantees in the spec (§9 + §13):

    G1  Never return zero deployments when candidates exist.
    G2  No binary EV/RAS filtering.
    G3  Smooth allocation changes across small input perturbations.
    G4  Deterministic for fixed inputs.

Plus edge cases from the spec's additional decisions:

    A   C_i ≥ cpb_i · min_shares (min-capital floor)
    B   λ1·E_loss + λ2 ≈ 0 does not explode weights
    C   Σ p_i·raw_alloc ≈ 0 falls back to equal allocation
    D   Caps clip only (no redistribution)
"""

import math
import os
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass

from profit.allocator import (
    allocate_portfolio,
    DEFAULT_BETA, DEFAULT_ETA, CAPITAL_BUFFER,
)
from profit.learning import LearningState, MODE_ACTIVE
from oversight.market_scorer import ScoredMarket


# ═══════════════════════════════════════════════════════════════
# Fake calibrator — deterministic, no DB
# ═══════════════════════════════════════════════════════════════

@dataclass
class _FakePreds:
    p_fill_24h: float
    e_loss_given_fill: float
    e_time_on_book_hours: float = 12.0
    reward_rate_per_hour: float = 1.0
    ev_per_day: float = 0.0
    confidence: str = "model"
    model_versions: dict = None
    model_confidence: float = 1.0
    raw_ev_per_day: float = 0.0
    raw_reward_per_day: float = 0.0

    def __post_init__(self):
        if self.model_versions is None:
            self.model_versions = {}


class _FakeCalibrator:
    """Deterministic fake — pulls predictions from a dict keyed by cid."""

    def __init__(self, preds_by_cid: dict):
        self.preds = preds_by_cid

    def get_predictions(self, condition_id, **kwargs):
        return self.preds.get(condition_id)


def _sm(cid: str, spread: float = 0.045, min_size: float = 50.0,
        question_group: str = "") -> ScoredMarket:
    return ScoredMarket(
        condition_id=cid,
        question=f"Q{cid}",
        score=1.0,
        action="deploy",
        recommended_shares=50,
        reason="test",
        confidence="high",
        actual_reward_total=0.0,
        fill_damage=0.0,
        fill_count=0,
        daily_rate=10.0,
        min_size=min_size,
        max_spread=spread,
        est_capital_cost=0.0,
        locked_position_usd=0.0,
        question_group=question_group,
        q_share_pct=0.10,
        end_date_iso="",
        game_start_time="",
    )


def _fresh_db() -> str:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return f.name


# ═══════════════════════════════════════════════════════════════
# §13 case 1 — small R everywhere
# ═══════════════════════════════════════════════════════════════

class TestSmallRewardsStillDeploy(unittest.TestCase):

    def test_all_small_r_still_produces_allocations(self):
        """G1: when every R is tiny but non-zero, we still deploy."""
        preds = {
            f"M{i}": _FakePreds(
                p_fill_24h=0.1,
                e_loss_given_fill=2.0,
                raw_reward_per_day=0.001 * (i + 1),  # ∈ [0.001, 0.005]
            )
            for i in range(5)
        }
        markets = [_sm(cid) for cid in preds.keys()]
        db = _fresh_db()
        try:
            allocs = allocate_portfolio(
                markets, 1000.0, _FakeCalibrator(preds), db,
            )
            deployed = [a for a in allocs if a["action"] == "deploy"]
            self.assertEqual(len(deployed), 5, "every market should deploy")
            for a in deployed:
                self.assertGreater(
                    a["shares_per_side"], 0,
                    "no market may deploy zero shares (G1)",
                )
        finally:
            os.unlink(db)


# ═══════════════════════════════════════════════════════════════
# §13 case 2 — large E_loss everywhere
# ═══════════════════════════════════════════════════════════════

class TestLargeLossStillDeploy(unittest.TestCase):

    def test_all_large_e_loss_still_produces_allocations(self):
        """When every p·L is huge, weights collapse toward the floor but
        the formula still produces a valid (small) allocation. Min-capital
        floor (§A) guarantees each market hits ≥ min_size shares."""
        preds = {
            f"M{i}": _FakePreds(
                p_fill_24h=0.9,
                e_loss_given_fill=1000.0,
                raw_reward_per_day=1.0,
            )
            for i in range(3)
        }
        markets = [_sm(cid) for cid in preds.keys()]
        db = _fresh_db()
        try:
            allocs = allocate_portfolio(
                markets, 1000.0, _FakeCalibrator(preds), db,
            )
            deployed = [a for a in allocs if a["action"] == "deploy"]
            self.assertEqual(len(deployed), 3)
            for a in deployed:
                self.assertGreaterEqual(
                    a["shares_per_side"], a["min_size"],
                    "min_shares floor must hold (§A)",
                )
        finally:
            os.unlink(db)


# ═══════════════════════════════════════════════════════════════
# §13 case 3 — smoothness under small perturbations
# ═══════════════════════════════════════════════════════════════

class TestSmoothness(unittest.TestCase):

    def _allocate(self, reward_multiplier: float):
        preds = {
            "M1": _FakePreds(
                p_fill_24h=0.2,
                e_loss_given_fill=3.0,
                raw_reward_per_day=5.0 * reward_multiplier,
            ),
            "M2": _FakePreds(
                p_fill_24h=0.3,
                e_loss_given_fill=2.0,
                raw_reward_per_day=4.0,
            ),
        }
        markets = [_sm("M1"), _sm("M2")]
        db = _fresh_db()
        try:
            return allocate_portfolio(
                markets, 1000.0, _FakeCalibrator(preds), db,
            )
        finally:
            os.unlink(db)

    def test_small_reward_bump_produces_smooth_change(self):
        """A 1% bump in one market's R should shift shares by at most a
        fraction of the market's baseline allocation — no step changes."""
        base = self._allocate(1.00)
        bumped = self._allocate(1.01)

        base_by_cid = {a["condition_id"]: a["shares_per_side"] for a in base}
        bumped_by_cid = {a["condition_id"]: a["shares_per_side"] for a in bumped}

        for cid in ("M1", "M2"):
            bs = base_by_cid[cid]
            us = bumped_by_cid[cid]
            if bs == 0:
                continue
            rel = abs(us - bs) / bs
            # For this configuration a 1% R change should move shares by
            # well under 10% of baseline (spec G3: no discontinuities).
            self.assertLess(
                rel, 0.10,
                f"{cid}: relative change {rel:.3f} too large for 1% R bump",
            )


# ═══════════════════════════════════════════════════════════════
# §9 — determinism
# ═══════════════════════════════════════════════════════════════

class TestDeterminism(unittest.TestCase):

    def test_same_inputs_produce_identical_output(self):
        preds = {
            f"M{i}": _FakePreds(
                p_fill_24h=0.15 + 0.02 * i,
                e_loss_given_fill=1.5 + 0.3 * i,
                raw_reward_per_day=3.0 + 0.5 * i,
            )
            for i in range(4)
        }
        markets = [_sm(f"M{i}") for i in range(4)]
        db = _fresh_db()
        try:
            a1 = allocate_portfolio(
                markets, 1000.0, _FakeCalibrator(preds), db,
            )
            a2 = allocate_portfolio(
                markets, 1000.0, _FakeCalibrator(preds), db,
            )
        finally:
            os.unlink(db)
        self.assertEqual(len(a1), len(a2))
        for x, y in zip(a1, a2):
            self.assertEqual(x["shares_per_side"], y["shares_per_side"])
            self.assertAlmostEqual(
                x["est_capital_cost"], y["est_capital_cost"], places=6,
            )


# ═══════════════════════════════════════════════════════════════
# §B — pathological denominator (λ1·E_loss + λ2 ≈ 0)
# ═══════════════════════════════════════════════════════════════

class TestPathologicalDenominator(unittest.TestCase):

    def test_zero_loss_does_not_explode(self):
        """With E_loss = 0 the new allocator's denominator is
        `1 + p·L = 1`, which can't diverge regardless of η. Allocation
        still lands within the expected_capital bound."""
        preds = {
            "M1": _FakePreds(
                p_fill_24h=0.1,
                e_loss_given_fill=0.0,           # zero loss
                raw_reward_per_day=5.0,
            ),
        }
        ls = LearningState(beta=0.5, eta=4.0, mode=MODE_ACTIVE)
        markets = [_sm("M1")]
        db = _fresh_db()
        try:
            allocs = allocate_portfolio(
                markets, 1000.0, _FakeCalibrator(preds), db,
                learning_state=ls,
            )
        finally:
            os.unlink(db)
        self.assertEqual(len(allocs), 1)
        a = allocs[0]
        self.assertEqual(a["action"], "deploy")
        # shares must be finite + positive
        self.assertTrue(math.isfinite(a["est_capital_cost"]))
        self.assertGreater(a["shares_per_side"], 0)


# ═══════════════════════════════════════════════════════════════
# §C — equal-allocation fallback when Σ p·raw ≈ 0
# ═══════════════════════════════════════════════════════════════

class TestEqualFallback(unittest.TestCase):

    def test_zero_rewards_everywhere_falls_back_to_equal(self):
        """If every R = 0, weights hit WEIGHT_FLOOR (1e-6), raw_alloc =
        1e-12, Σ p·raw ≈ a few e-12 < SCALE_EPSILON (1e-9 requires enough
        smallness). The fallback must produce equal allocations so the
        system still deploys."""
        preds = {
            f"M{i}": _FakePreds(
                p_fill_24h=0.1,
                e_loss_given_fill=5.0,
                raw_reward_per_day=0.0,         # forces weight floor
            )
            for i in range(4)
        }
        markets = [_sm(f"M{i}") for i in range(4)]
        db = _fresh_db()
        try:
            allocs = allocate_portfolio(
                markets, 1000.0, _FakeCalibrator(preds), db,
            )
        finally:
            os.unlink(db)
        deployed = [a for a in allocs if a["action"] == "deploy"]
        self.assertEqual(len(deployed), 4)
        # Under the equal-fallback path every market gets the same
        # est_capital_cost (modulo integer rounding on shares).
        costs = [a["est_capital_cost"] for a in deployed]
        for c in costs[1:]:
            self.assertAlmostEqual(c, costs[0], delta=1.0)


# ═══════════════════════════════════════════════════════════════
# §D — caps clip extremes, never distort ranking
# ═══════════════════════════════════════════════════════════════

class TestCapsClipOnly(unittest.TestCase):

    def test_per_market_cap_clips_dominant_market(self):
        """A single market with a massively higher R would otherwise
        absorb most of the capital. The per-market cap (= max_per_market)
        must clip it; lower-R markets must not be upsized to redistribute
        the freed capital."""
        preds = {
            "BIG":  _FakePreds(
                p_fill_24h=0.2, e_loss_given_fill=1.0,
                raw_reward_per_day=100.0,     # dominant
            ),
            "SMALL1": _FakePreds(
                p_fill_24h=0.2, e_loss_given_fill=1.0,
                raw_reward_per_day=1.0,
            ),
            "SMALL2": _FakePreds(
                p_fill_24h=0.2, e_loss_given_fill=1.0,
                raw_reward_per_day=1.0,
            ),
        }
        markets = [_sm("BIG"), _sm("SMALL1"), _sm("SMALL2")]
        db = _fresh_db()
        try:
            # per_market_cap default = min(200, 1000·0.15) = 150
            allocs = allocate_portfolio(
                markets, 1000.0, _FakeCalibrator(preds), db,
                max_per_market=150.0, max_capital_pct=0.15,
            )
        finally:
            os.unlink(db)
        by_cid = {a["condition_id"]: a for a in allocs}
        self.assertLessEqual(
            by_cid["BIG"]["est_capital_cost"], 150.0 + 1e-6,
            "per-market cap must clip BIG",
        )
        # SMALL1 / SMALL2 each got their raw formula-share; they should
        # NOT have been upsized to absorb the freed capital from BIG.
        # Their est_capital_cost should sit well under the cap, since
        # with the dominant market clipped there's no redistribution.
        for cid in ("SMALL1", "SMALL2"):
            self.assertLess(by_cid[cid]["est_capital_cost"], 150.0)

    def test_expected_capital_never_exceeds_95_pct(self):
        """§8 of the spec — after the final rescale, Σ p·cost ≤ 0.95·total."""
        preds = {
            f"M{i}": _FakePreds(
                p_fill_24h=0.4,
                e_loss_given_fill=2.0,
                raw_reward_per_day=5.0 + 0.5 * i,
            )
            for i in range(6)
        }
        markets = [_sm(f"M{i}") for i in range(6)]
        db = _fresh_db()
        try:
            allocs = allocate_portfolio(
                markets, 1000.0, _FakeCalibrator(preds), db,
            )
        finally:
            os.unlink(db)
        expected = sum(
            float(a.get("_expected_capital") or 0.0)
            for a in allocs if a["action"] == "deploy"
        )
        self.assertLessEqual(
            expected, 1000.0 * CAPITAL_BUFFER + 1e-6,
            f"expected_capital {expected:.2f} exceeds ceiling",
        )


# ═══════════════════════════════════════════════════════════════
# Observability stamps (§11 + decision #6)
# ═══════════════════════════════════════════════════════════════

class TestStamps(unittest.TestCase):

    def test_stamps_present_on_deploy_rows(self):
        preds = {
            "M1": _FakePreds(
                p_fill_24h=0.2, e_loss_given_fill=1.5,
                raw_reward_per_day=3.0,
            ),
        }
        ls = LearningState(beta=0.6, eta=2.0, mode=MODE_ACTIVE)
        markets = [_sm("M1")]
        db = _fresh_db()
        try:
            allocs = allocate_portfolio(
                markets, 1000.0, _FakeCalibrator(preds), db,
                learning_state=ls,
            )
        finally:
            os.unlink(db)
        a = allocs[0]
        for key in (
            "_p_fill", "_reward", "_expected_loss", "_weight",
            "_raw_alloc", "_beta", "_eta",
            "_expected_capital", "_expected_capital_contribution",
        ):
            self.assertIn(key, a, f"missing stamp {key}")
        self.assertAlmostEqual(a["_beta"], 0.6, places=5)
        self.assertAlmostEqual(a["_eta"],  2.0, places=5)
        self.assertAlmostEqual(a["_reward"], 3.0, places=5)


# ═══════════════════════════════════════════════════════════════
# Pass-through of scorer's avoid decisions
# ═══════════════════════════════════════════════════════════════

class TestScorerAvoidPassThrough(unittest.TestCase):

    def test_avoid_rows_pass_through_at_zero_shares(self):
        preds = {
            "OK": _FakePreds(
                p_fill_24h=0.2, e_loss_given_fill=1.0,
                raw_reward_per_day=2.0,
            ),
        }
        ok = _sm("OK")
        skipped = _sm("SKIP")
        skipped.action = "avoid"  # simulate sports / trial-cap avoid
        db = _fresh_db()
        try:
            allocs = allocate_portfolio(
                [ok, skipped], 1000.0, _FakeCalibrator(preds), db,
            )
        finally:
            os.unlink(db)
        by_cid = {a["condition_id"]: a for a in allocs}
        self.assertEqual(by_cid["SKIP"]["action"], "avoid")
        self.assertEqual(by_cid["SKIP"]["shares_per_side"], 0)
        self.assertEqual(by_cid["OK"]["action"], "deploy")
        self.assertGreater(by_cid["OK"]["shares_per_side"], 0)


# ═══════════════════════════════════════════════════════════════
# Defaults match when learning_state is None
# ═══════════════════════════════════════════════════════════════

class TestDefaultControls(unittest.TestCase):

    def test_none_learning_state_uses_default_controls(self):
        preds = {
            "M1": _FakePreds(
                p_fill_24h=0.2, e_loss_given_fill=1.5,
                raw_reward_per_day=3.0,
            ),
        }
        markets = [_sm("M1")]
        db = _fresh_db()
        try:
            allocs = allocate_portfolio(
                markets, 1000.0, _FakeCalibrator(preds), db,
                learning_state=None,
            )
        finally:
            os.unlink(db)
        a = allocs[0]
        self.assertAlmostEqual(a["_beta"], DEFAULT_BETA, places=6)
        self.assertAlmostEqual(a["_eta"],  DEFAULT_ETA,  places=6)


if __name__ == "__main__":
    unittest.main()
