"""FX-040 trial-mode sizing regression tests.

The 2026-05-19 cascade chained:
  - cold-start prior (q_share=0.10) over-estimated reward on OpenAI markets
  - allocator sized full positions (143 sh) on these untested markets
  - thin-book fills generated 5-11% dump slippage per trade
  - cumulative realized loss hit kill-switch threshold

FX-040 inserts a trial-mode sizing branch in ``compute_allocations``:
  - untested markets (`q_score_samples < RF_TRIAL_SCORING_SAMPLES`) deploy at
    `max(min_size, RF_TRIAL_MIN_SHARES)` shares regardless of computed allocation
  - cumulative trial exposure is capped at `RF_TRIAL_BUDGET_PCT * total_capital`
  - graduated markets (`q_score_samples >= N`) use full sizing as before
  - redistribution pass excludes trial markets so the cap actually holds

These tests pin the contract:

- escape hatch                              → ``samples >= N`` → full sizing
- trial cap binds                            → ``samples < N`` → shares = max(min_size, RF_TRIAL_MIN_SHARES)
- trial budget exhaustion                    → over-budget trials rejected with reason
- redistribution skips trial markets         → trial cap survives the surplus pass
- score ordering matters                     → top-scored trials get first dibs on budget
- graduated + trial mixed                    → only the untested ones get the cap
- venue compliance (min_size floor)          → trial shares ≥ market min_size
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oversight.allocation_writer import (
    compute_allocations, _is_trial_market, _trial_target_shares,
)
from oversight.market_scorer import ScoredMarket


def _sm(
    cid="cid_x", question="market?", score=10.0, action="deploy",
    recommended_shares=100, min_size=50, max_spread=0.045,
    q_share_pct=0.10, q_score_samples=0, reason="default",
    fill_count=0, daily_rate=200, est_capital_cost=70,
):
    return ScoredMarket(
        condition_id=cid, question=question, score=score, action=action,
        recommended_shares=recommended_shares, reason=reason,
        confidence="low" if q_score_samples == 0 else "high",
        actual_reward_total=0.0, fill_damage=0.0, fill_count=fill_count,
        daily_rate=daily_rate, min_size=min_size, max_spread=max_spread,
        est_capital_cost=est_capital_cost, locked_position_usd=0.0,
        question_group="",
        q_share_pct=q_share_pct, q_score_samples=q_score_samples,
        end_date_iso="", game_start_time="",
    )


# ── _is_trial_market unit ────────────────────────────────────────────────────


class TestIsTrialMarket(unittest.TestCase):

    def test_zero_samples_is_trial(self):
        sm = _sm(q_score_samples=0)
        self.assertTrue(_is_trial_market(sm))

    def test_below_threshold_is_trial(self):
        sm = _sm(q_score_samples=3)  # default threshold 5
        self.assertTrue(_is_trial_market(sm))

    def test_at_threshold_is_graduated(self):
        sm = _sm(q_score_samples=5)
        self.assertFalse(_is_trial_market(sm))

    def test_above_threshold_is_graduated(self):
        sm = _sm(q_score_samples=100)
        self.assertFalse(_is_trial_market(sm))


# ── _trial_target_shares unit ────────────────────────────────────────────────


class TestTrialTargetShares(unittest.TestCase):
    """Trial shares = max(min_size, RF_TRIAL_MIN_SHARES) for venue compliance."""

    def test_min_size_below_trial_floor(self):
        sm = _sm(min_size=10)  # below default RF_TRIAL_MIN_SHARES=20
        self.assertEqual(20, _trial_target_shares(sm))

    def test_min_size_above_trial_floor(self):
        sm = _sm(min_size=50)
        self.assertEqual(50, _trial_target_shares(sm))

    def test_large_min_size(self):
        sm = _sm(min_size=200)  # like yesterday's OpenAI HIGH $1.5T
        self.assertEqual(200, _trial_target_shares(sm))


# ── compute_allocations end-to-end ───────────────────────────────────────────


class TestComputeAllocationsTrialSizing(unittest.TestCase):

    def test_trial_market_capped_at_trial_size(self):
        """Untested market with high recommended_shares is capped to trial size."""
        trial = _sm(cid="cid_trial", recommended_shares=200, min_size=50,
                    q_score_samples=0)
        out = compute_allocations([trial], total_capital=1000.0)
        self.assertEqual(1, len(out))
        a = out[0]
        self.assertEqual("deploy", a["action"])
        self.assertEqual(50, a["shares_per_side"])  # capped to min_size (the floor for venue)
        self.assertIn("[TRIAL:", a["reason"])

    def test_graduated_market_uses_full_sizing(self):
        """Market with enough scoring samples gets full recommended size."""
        grad = _sm(cid="cid_grad", recommended_shares=100, min_size=50,
                   q_score_samples=10)
        out = compute_allocations([grad], total_capital=1000.0)
        self.assertEqual(1, len(out))
        a = out[0]
        self.assertEqual("deploy", a["action"])
        # base 100 + possible redistribution; no trial tag
        self.assertGreaterEqual(a["shares_per_side"], 100)
        self.assertNotIn("[TRIAL:", a["reason"])

    def test_trial_budget_rejects_over_budget_markets(self):
        """When cumulative trial cost would exceed budget, additional trials are rejected.

        Setup tuned so per-market cap doesn't gate first AND trial budget DOES gate:
        - total_capital=$400, per_market_cap = min($200, $400*0.15) = $60 (loose vs $46 trial cost)
        - trial budget = 25% * $400 = $100
        - Each trial at min_size=50 costs ~$46 (50 * $0.455 * 2)
        - 1st + 2nd trials: $46 + $46 = $92 ≤ $100 → both fit
        - 3rd trial: $46 + $46 + $46 = $138 > $100 → REJECTED
        """
        t1 = _sm(cid="cid_t1", score=30.0, min_size=50, q_score_samples=0)
        t2 = _sm(cid="cid_t2", score=20.0, min_size=50, q_score_samples=0)
        t3 = _sm(cid="cid_t3", score=10.0, min_size=50, q_score_samples=0)
        out = compute_allocations([t1, t2, t3], total_capital=400.0)
        t1_out = next(a for a in out if a["condition_id"] == "cid_t1")
        t2_out = next(a for a in out if a["condition_id"] == "cid_t2")
        t3_out = next(a for a in out if a["condition_id"] == "cid_t3")
        self.assertEqual("deploy", t1_out["action"], f"t1 reason: {t1_out['reason']}")
        self.assertIn("[TRIAL:", t1_out["reason"])
        self.assertEqual("deploy", t2_out["action"], f"t2 reason: {t2_out['reason']}")
        self.assertIn("[TRIAL:", t2_out["reason"])
        self.assertEqual("avoid", t3_out["action"], f"t3 reason: {t3_out['reason']}")
        self.assertIn("Trial budget exhausted", t3_out["reason"])

    def test_graduated_doesnt_consume_trial_budget(self):
        """Graduated markets don't count against trial budget."""
        # Three graduated markets, all should deploy at full size
        sms = [
            _sm(cid=f"cid_grad_{i}", score=10.0, q_score_samples=10)
            for i in range(3)
        ]
        out = compute_allocations(sms, total_capital=1000.0)
        deploys = [a for a in out if a["action"] == "deploy"]
        self.assertEqual(3, len(deploys))
        for a in deploys:
            self.assertNotIn("[TRIAL:", a["reason"])

    def test_redistribution_skips_trial_markets(self):
        """Redistribution pass must not blow past the trial cap."""
        # Single trial market with high score — would otherwise attract surplus
        trial = _sm(cid="cid_trial_only", score=50.0, recommended_shares=100,
                    min_size=50, q_score_samples=0)
        out = compute_allocations([trial], total_capital=1000.0)
        a = out[0]
        self.assertEqual("deploy", a["action"])
        # Should be EXACTLY 50 (trial cap), not boosted by redistribution
        self.assertEqual(50, a["shares_per_side"])
        # And the reason should NOT contain redistrib tag
        self.assertNotIn("redistrib", a["reason"])

    def test_mixed_trial_and_graduated_each_handled_correctly(self):
        """Trial markets capped, graduated markets full-sized, in the same cycle."""
        trial = _sm(cid="cid_trial", score=10.0, recommended_shares=200,
                    min_size=50, q_score_samples=0)
        grad = _sm(cid="cid_grad", score=10.0, recommended_shares=200,
                   min_size=50, q_score_samples=10)
        out = compute_allocations([trial, grad], total_capital=2000.0)
        trial_out = next(a for a in out if a["condition_id"] == "cid_trial")
        grad_out = next(a for a in out if a["condition_id"] == "cid_grad")
        self.assertEqual(50, trial_out["shares_per_side"])
        self.assertIn("[TRIAL:", trial_out["reason"])
        self.assertGreaterEqual(grad_out["shares_per_side"], 200)
        self.assertNotIn("[TRIAL:", grad_out["reason"])

    def test_score_ordering_for_trial_budget(self):
        """Highest-scored trial gets first dibs on the trial budget.

        Setup: $200 wallet → per_market_cap $30, trial budget $50.
        Two trials at min_size=20 (cost $18.2 each). Only one fits the $50 / $46 head room.
        Actually 2 fit ($36.4), but to make it tight, use 3 markets and verify ordering.
        """
        # $400 wallet → per_market_cap $60, trial budget $100, min_size=50 costs $46.
        # Two trials fit ($92), 3rd doesn't ($138). Verify the lowest-scored is rejected.
        winner = _sm(cid="cid_winner", score=100.0, min_size=50, q_score_samples=0)
        middle = _sm(cid="cid_middle", score=50.0, min_size=50, q_score_samples=0)
        loser = _sm(cid="cid_loser", score=5.0, min_size=50, q_score_samples=0)
        out = compute_allocations([winner, middle, loser], total_capital=400.0)
        w = next(a for a in out if a["condition_id"] == "cid_winner")
        m = next(a for a in out if a["condition_id"] == "cid_middle")
        l = next(a for a in out if a["condition_id"] == "cid_loser")
        self.assertEqual("deploy", w["action"])
        self.assertEqual("deploy", m["action"])
        self.assertEqual("avoid", l["action"])
        self.assertIn("Trial budget exhausted", l["reason"])


class TestComputeAllocationsBackwardCompat(unittest.TestCase):
    """FX-040 must not regress existing behavior for graduated markets."""

    def test_no_trial_markets_no_trial_tag(self):
        """When all markets are graduated, no [TRIAL: tag appears anywhere."""
        sms = [
            _sm(cid=f"cid_{i}", q_score_samples=10)
            for i in range(3)
        ]
        out = compute_allocations(sms, total_capital=1000.0)
        for a in out:
            self.assertNotIn("[TRIAL:", a["reason"])

    def test_avoid_action_passes_through_unchanged(self):
        """Pre-marked avoid markets are emitted as-is regardless of samples."""
        sm_avoid = _sm(cid="cid_av", action="avoid", q_score_samples=0,
                       reason="already-avoided")
        out = compute_allocations([sm_avoid], total_capital=1000.0)
        self.assertEqual(1, len(out))
        a = out[0]
        self.assertEqual("avoid", a["action"])
        self.assertEqual(0, a["shares_per_side"])
        self.assertNotIn("[TRIAL:", a["reason"])
        self.assertNotIn("Trial budget", a["reason"])


if __name__ == "__main__":
    unittest.main()
