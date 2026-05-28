"""Adversarial audit — P10 + P11 of 9/10 plan (FX-060 + FX-061).

Closes the final two ground-rules §3 self-correction triggers so the bot's
self-learning loop is 6/6 wired to behavior change (not 4/6).

P10 (trigger #4): global reward < target → widen filters
  - decision_policy.evaluate() sets `global_reward_low=True` when
    total_reward_24h < RF_GLOBAL_REWARD_TARGET_24H_USD AND NOT global_tighten
  - Allocator halves MIN_DAILY_RATE_USD + MIN_EXPECTED_PER_MARKET this cycle
  - Mutually exclusive with global_tighten (loss recovery wins)

P11 (trigger #6): API q_share diverges > 2× from cumulative → distrust
  - simple_oversight passes api_q + cumul_q per cid to policy.record_qshare_divergence
  - On breach: row inserted in q_share_recalibration_events DB table + [LEARN_DIVERGENCE] log
  - Next cycle: policy.evaluate() loads recent events → q_share_distrust_cids set
  - Allocator applies extra 0.5× factor to NON-API q_share for distrust cids

Attack families:
  PT-A  P10 trigger detection (decision_policy boolean output)
  PT-B  P10 allocator behavior (filter widening when trigger fires)
  PT-C  P10 mutual exclusion with global_tighten
  PT-D  P11 divergence event recording
  PT-E  P11 distrust persistence + allocator behavior
  PT-F  Integration: full self-learning loop (6/6 triggers exercised)
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from simple_allocator import SimpleAllocator, CandidateMarket
from decision_policy import DecisionPolicy
from market_roi_tracker import MarketROITracker
from database import BotDatabase


# ── Fixtures ──

def _make_db():
    p = tempfile.mktemp(suffix=".db")
    BotDatabase(p)
    return p


def _make_allocator(now=1700000000):
    return SimpleAllocator(
        db_path=":memory:", wallet_address="0xW", funder="0xF",
        api_key="k", api_secret="MTIzNDU2Nzg5MDEyMzQ1Ng==",
        api_passphrase="p",
        _now=lambda: now,
        _http=lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: {"data": []}),
    )


def _make_candidate(cid, daily_rate=500, min_size=20, midpoint=0.5):
    return CandidateMarket(
        condition_id=cid, yes_tid="y", no_tid="n",
        daily_rate=daily_rate, max_spread=4.5, min_size=min_size,
        midpoint_guess=midpoint,
    )


def _seed_roi_row(
    db, cid, *, samples, fill_loss=0, reward_earned=0, roi=0,
    capital_avg=50, now=1_700_000_000.0,
):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO market_roi (condition_id, window, window_end_ts, reward_earned, "
        "fill_loss, capital_committed_avg, roi, fill_count, fill_rate_per_hour, "
        "samples, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cid, "24h", now, reward_earned, fill_loss, capital_avg, roi,
         samples, samples / 24.0, samples, now),
    )
    conn.commit()
    conn.close()


def _make_policy(db, now=1_700_000_000.0):
    tracker = MarketROITracker(
        db_path=db, funder="0xF",
        _now=lambda: now,
        _http=lambda *a, **k: SimpleNamespace(status_code=500, json=lambda: {}),
    )
    return DecisionPolicy(db_path=db, tracker=tracker, _now=lambda: now)


# ════════════════════════════════════════════════════════════════════════════
# PT-A — P10 trigger detection
# ════════════════════════════════════════════════════════════════════════════


class TestPT_A_P10_TriggerDetection(unittest.TestCase):

    def test_PT_A1_low_reward_no_loss_sets_global_reward_low(self):
        """total_reward_24h=$2 < $4 target AND no loss → global_reward_low=True."""
        db = _make_db()
        policy = _make_policy(db)
        _seed_roi_row(db, "0xLOW", samples=1, reward_earned=2.0, fill_loss=0.0)
        ev = policy.evaluate()
        self.assertTrue(ev["global_reward_low"])
        self.assertFalse(ev["global_tighten"])
        os.unlink(db)

    def test_PT_A2_reward_above_target_does_NOT_trigger(self):
        """total_reward_24h=$10 > $4 target → global_reward_low=False."""
        db = _make_db()
        policy = _make_policy(db)
        _seed_roi_row(db, "0xHIGH", samples=1, reward_earned=10.0, fill_loss=0.0)
        ev = policy.evaluate()
        self.assertFalse(ev["global_reward_low"])
        os.unlink(db)

    def test_PT_A3_loss_dominates_disables_reward_trigger(self):
        """global_tighten=True overrides global_reward_low. Even if reward<$4,
        if losses > rewards, we should TIGHTEN not WIDEN."""
        db = _make_db()
        policy = _make_policy(db)
        # reward $0.50, loss $2 → losses dominate
        _seed_roi_row(db, "0xMIXED", samples=1, reward_earned=0.50, fill_loss=2.0)
        ev = policy.evaluate()
        self.assertTrue(ev["global_tighten"], "loss > reward must set global_tighten")
        self.assertFalse(ev["global_reward_low"],
                         "global_tighten must suppress global_reward_low")
        os.unlink(db)


# ════════════════════════════════════════════════════════════════════════════
# PT-B — P10 allocator behavior
# ════════════════════════════════════════════════════════════════════════════


class TestPT_B_P10_AllocatorWidens(unittest.TestCase):

    def test_PT_B1_global_reward_low_halves_min_daily_rate(self):
        """When global_reward_low=True, MIN_DAILY_RATE_USD halved → lower-yield
        markets pass the eligibility filter."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {"0xLOW_RATE": 0.10}
        # daily_rate=$6 fails default $10 floor but passes halved $5 floor
        candidates = [_make_candidate("0xLOW_RATE", daily_rate=6)]
        result_normal = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
            global_reward_low=False,
        )
        result_widened = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
            global_reward_low=True,
        )
        self.assertEqual(0, len(result_normal.deploys),
                         "$6 daily_rate fails default $10 floor")
        self.assertEqual(1, len(result_widened.deploys),
                         "$6 daily_rate passes halved $5 floor under global_reward_low")

    def test_PT_B2_global_tighten_AND_global_reward_low_tighten_wins(self):
        """If allocator receives BOTH flags True (shouldn't happen but
        defensive), global_tighten should win — safer of the two."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {"0xMED": 0.10}
        # daily_rate=$15: passes default $10, fails 2× tightened $20
        candidates = [_make_candidate("0xMED", daily_rate=15)]
        result = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
            global_tighten=True,
            global_reward_low=True,  # both True — tighten wins per elif branch
        )
        self.assertEqual(0, len(result.deploys),
                         "global_tighten wins when both flags True ($15 < tightened $20 floor)")


# ════════════════════════════════════════════════════════════════════════════
# PT-D — P11 divergence event recording
# ════════════════════════════════════════════════════════════════════════════


class TestPT_D_P11_DivergenceRecording(unittest.TestCase):

    def test_PT_D1_divergence_above_2x_records_event(self):
        """api_q=0.10, cumul_q=0.02 → ratio=5× > 2× threshold → event recorded."""
        db = _make_db()
        policy = _make_policy(db)
        breached = policy.record_qshare_divergence("0xDIV", api_q=0.10, cumulative_q=0.02)
        self.assertTrue(breached)
        # Confirm event in DB
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT api_q_share, cumulative_q_share, divergence_ratio "
            "FROM q_share_recalibration_events WHERE condition_id=?",
            ("0xDIV",),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(0.10, row[0])
        self.assertAlmostEqual(0.02, row[1])
        self.assertAlmostEqual(5.0, row[2])
        os.unlink(db)

    def test_PT_D2_divergence_below_threshold_no_event(self):
        """api_q=0.10, cumul_q=0.07 → ratio≈1.43× < 2× → no event."""
        db = _make_db()
        policy = _make_policy(db)
        breached = policy.record_qshare_divergence("0xCLOSE", api_q=0.10, cumulative_q=0.07)
        self.assertFalse(breached)
        conn = sqlite3.connect(db)
        n = conn.execute(
            "SELECT COUNT(*) FROM q_share_recalibration_events WHERE condition_id=?",
            ("0xCLOSE",),
        ).fetchone()[0]
        conn.close()
        self.assertEqual(0, n)
        os.unlink(db)

    def test_PT_D3_zero_values_no_event_no_div_zero(self):
        """If api_q or cumul_q is 0, we can't compute a meaningful ratio.
        Must not crash, must not record."""
        db = _make_db()
        policy = _make_policy(db)
        self.assertFalse(policy.record_qshare_divergence("0xZERO_API", api_q=0, cumulative_q=0.10))
        self.assertFalse(policy.record_qshare_divergence("0xZERO_CUMUL", api_q=0.10, cumulative_q=0))
        os.unlink(db)


# ════════════════════════════════════════════════════════════════════════════
# PT-E — P11 distrust persistence + allocator behavior
# ════════════════════════════════════════════════════════════════════════════


class TestPT_E_P11_DistrustPropagation(unittest.TestCase):

    def test_PT_E1_recent_event_appears_in_distrust_set(self):
        """After record_qshare_divergence fires, evaluate() should pick up
        the cid in q_share_distrust_cids next cycle."""
        db = _make_db()
        policy = _make_policy(db)
        policy.record_qshare_divergence("0xPERSIST", api_q=0.10, cumulative_q=0.02)
        ev = policy.evaluate()
        self.assertIn("0xPERSIST", ev["q_share_distrust_cids"])
        os.unlink(db)

    def test_PT_E2_old_event_outside_window_NOT_in_distrust(self):
        """Events older than 24h should NOT persist in distrust set."""
        db = _make_db()
        # Manually insert an old event
        conn = sqlite3.connect(db)
        old_ts = 1_700_000_000.0 - 86400 * 2  # 48h old
        conn.execute(
            "INSERT INTO q_share_recalibration_events "
            "(ts, condition_id, api_q_share, cumulative_q_share, divergence_ratio) "
            "VALUES (?, ?, ?, ?, ?)",
            (old_ts, "0xOLD", 0.10, 0.02, 5.0),
        )
        conn.commit()
        conn.close()
        policy = _make_policy(db, now=1_700_000_000.0)
        ev = policy.evaluate()
        self.assertNotIn("0xOLD", ev["q_share_distrust_cids"])
        os.unlink(db)

    def test_PT_E3_distrust_factor_applied_to_cumulative_qshare(self):
        """When a cid is in q_share_distrust_cids AND allocator uses cumulative
        (not API) for it, expected_q_share = cumulative × conservative × 0.5.
        """
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}  # No API
        a.load_cumulative_ratios = lambda: {"0xDIST": 0.10}
        candidates = [_make_candidate("0xDIST", daily_rate=500)]
        result = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
            q_share_distrust_cids={"0xDIST"},
        )
        # cumul=0.10, conservative=1.0 (default), distrust=0.5 → effective q=0.05
        self.assertEqual(1, len(result.deploys))
        self.assertAlmostEqual(0.05, result.deploys[0].expected_q_share, places=4)

    def test_PT_E4_distrust_does_NOT_apply_to_API_qshare(self):
        """API q_share is ground truth. Even if cid in distrust set,
        when API value is available we use it unmodified."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {"0xDIST": 0.20}
        a.load_cumulative_ratios = lambda: {"0xDIST": 0.02}
        candidates = [_make_candidate("0xDIST", daily_rate=500)]
        result = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
            q_share_distrust_cids={"0xDIST"},
        )
        # API=0.20 used directly (not modified by distrust)
        self.assertAlmostEqual(0.20, result.deploys[0].expected_q_share, places=4)
        self.assertEqual("api", result.deploys[0].q_share_source)


# ════════════════════════════════════════════════════════════════════════════
# PT-F — Integration: full 6/6 self-learning loop exercised
# ════════════════════════════════════════════════════════════════════════════


class TestPT_F_Full6Of6Integration(unittest.TestCase):

    def test_PT_F1_evaluate_returns_all_6_trigger_outputs(self):
        """evaluate() must return all 6 trigger-related fields in the dict:
            #1 newly_cooled (ROI threshold)
            #2 newly_cooled (fast-path loss — same field as #1 by design)
            #3 size_reduction_cids (fill_rate)
            #4 global_reward_low (low rewards)
            #5 global_tighten (loss > rewards)
            #6 q_share_distrust_cids (API/cumul divergence)
        """
        db = _make_db()
        policy = _make_policy(db)
        ev = policy.evaluate()
        required_keys = {
            "newly_cooled",           # #1 + #2 (cool-down family)
            "size_reduction_cids",    # #3
            "global_reward_low",      # #4 (P10)
            "global_tighten",         # #5
            "q_share_distrust_cids",  # #6 (P11)
        }
        missing = required_keys - set(ev.keys())
        self.assertEqual(set(), missing,
                         f"evaluate() missing trigger outputs: {missing}")
        # Confirm types
        self.assertIsInstance(ev["newly_cooled"], list)
        self.assertIsInstance(ev["size_reduction_cids"], set)
        self.assertIsInstance(ev["global_reward_low"], bool)
        self.assertIsInstance(ev["global_tighten"], bool)
        self.assertIsInstance(ev["q_share_distrust_cids"], set)
        os.unlink(db)

    def test_PT_F2_all_6_triggers_can_fire_simultaneously_no_crash(self):
        """Stress: construct a scenario where all 6 trigger conditions are
        true and verify the bot reaches a coherent decision (not 0 deploys
        from over-stacking, not unhandled exception)."""
        db = _make_db()
        policy = _make_policy(db)
        # #1 + #2: market with bad ROI and big loss
        _seed_roi_row(db, "0xBAD", samples=5, roi=-0.10, fill_loss=5.0)
        # #3: market with high fill_rate
        _seed_roi_row(db, "0xHOT", samples=30, roi=0.05, fill_loss=0)
        # #5: global loss > reward (and indirectly #4 prevented by tighten)
        # The seeded rows have total_loss=$5, total_reward=$0 → tighten fires
        # Pre-seed a divergence event (#6)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO q_share_recalibration_events "
            "(ts, condition_id, api_q_share, cumulative_q_share, divergence_ratio) "
            "VALUES (?, ?, ?, ?, ?)",
            (1_700_000_000.0, "0xDIV", 0.10, 0.02, 5.0),
        )
        conn.commit()
        conn.close()
        try:
            ev = policy.evaluate()
        except Exception as e:
            self.fail(f"all-6-triggers scenario crashed: {type(e).__name__}: {e}")
        # Verify each fired
        self.assertIn("0xBAD", ev["newly_cooled"])
        self.assertIn("0xHOT", ev["size_reduction_cids"])
        self.assertTrue(ev["global_tighten"])
        self.assertFalse(ev["global_reward_low"], "tighten suppresses reward_low")
        self.assertIn("0xDIV", ev["q_share_distrust_cids"])
        os.unlink(db)


# ════════════════════════════════════════════════════════════════════════════
# PT-C — Backward compat (P10/P11 defaults are no-op)
# ════════════════════════════════════════════════════════════════════════════


class TestPT_C_BackwardCompat(unittest.TestCase):

    def test_PT_C1_default_values_preserve_pre_p10_behavior(self):
        """compute() called without P10/P11 args must behave identically
        to pre-P10/P11 (P4 backward compat preserved)."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {"0xA": 0.10}
        candidates = [_make_candidate("0xA", daily_rate=500)]
        result_omitted = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
        )
        result_explicit_defaults = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
            global_reward_low=False,
            q_share_distrust_cids=None,
        )
        self.assertEqual(
            result_omitted.deploys[0].target_capital,
            result_explicit_defaults.deploys[0].target_capital,
        )
        self.assertEqual(
            result_omitted.deploys[0].expected_q_share,
            result_explicit_defaults.deploys[0].expected_q_share,
        )


if __name__ == "__main__":
    unittest.main()
