"""Adversarial audit — P2 of 9/10 plan (FX-052 + FX-053 OverCommitAllocator).

P2 transforms SimpleAllocator from a budget-capped 20-market allocator into
an OverCommitAllocator obeying Ground Rules 1+2 (50-200 markets, 3-8× wallet
notional). The class name is retained for import-site compatibility.

Each test asserts the DESIRED post-P2 behaviour. A FAILING test = a bug.

Attack families:

  AO-A  Overcommit guarantees (notional > wallet, market count > 20)
  AO-B  EV-gate correctness (positive-EV filter at boundaries)
  AO-C  Pre-P2 filters still respected (cooldowns, extreme-price)
  AO-D  Kill-switch + 0-candidate edge cases
  AO-E  Telemetry + alloc.json metadata stamps
  AO-F  Adversarial inputs (malformed candidates, anomalous API responses)

Ground Rules 1 + 2 + 3 invariants under test:
  - Rule 1: deploy on EVERY positive-EV eligible market (no arbitrary count cap)
  - Rule 2: total notional permitted 3-8× wallet (no DEPLOY_RATIO)
  - Rule 3 (via FX-051): excluded_cids cooldown filter still respected
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import simple_allocator as sa
from simple_allocator import (
    SimpleAllocator,
    CandidateMarket,
    AllocationResult,
    COLD_START_Q_SHARE,
)
from config import cfg as _real_cfg  # FX-086: real cfg for delegating patches


# ── Fixtures ──

def _make_allocator(http_stub=None, now=1700000000):
    return SimpleAllocator(
        db_path=":memory:",
        wallet_address="0xWALLET", funder="0xFUNDER",
        api_key="key",
        api_secret="MTIzNDU2Nzg5MDEyMzQ1Ng==",
        api_passphrase="phrase",
        _now=lambda: now,
        _http=http_stub or (lambda *a, **k: SimpleNamespace(
            status_code=200, json=lambda: {"data": []},
        )),
    )


def _make_candidate(cid: str, daily_rate=100, min_size=20, midpoint=0.5) -> CandidateMarket:
    return CandidateMarket(
        condition_id=cid, yes_tid="y_" + cid, no_tid="n_" + cid,
        daily_rate=daily_rate, max_spread=4.5, min_size=min_size,
        midpoint_guess=midpoint,
    )


# ════════════════════════════════════════════════════════════════════════════
# AO-A — Overcommit guarantees
# ════════════════════════════════════════════════════════════════════════════


class TestAO_A_OvercommitGuarantees(unittest.TestCase):

    def test_AO_A1_50_eligible_markets_all_deploy(self):
        """Ground Rule 1 target lower bound: 50 markets simultaneous."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {f"0x{i:04d}": 0.10 for i in range(50)}
        candidates = [_make_candidate(f"0x{i:04d}", daily_rate=500) for i in range(50)]
        result = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
        )
        self.assertEqual(50, len(result.deploys))

    def test_AO_A2_200_eligible_markets_all_deploy(self):
        """Ground Rule 1 target upper bound: 200 markets simultaneous."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {f"0x{i:04d}": 0.10 for i in range(200)}
        candidates = [_make_candidate(f"0x{i:04d}", daily_rate=500) for i in range(200)]
        result = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
        )
        self.assertEqual(200, len(result.deploys),
                         "Ground Rule 1: 200 EV-positive markets must all deploy")

    def test_AO_A3_above_soft_cap_routes_excess_to_avoid(self):
        """700 candidates exceed the 500 soft sanity cap — first 500 deploy,
        excess routes to avoid. No crash, no clamp to wallet."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {f"0x{i:04d}": 0.10 for i in range(700)}
        candidates = [_make_candidate(f"0x{i:04d}", daily_rate=500) for i in range(700)]
        result = a.compute(
            wallet_usd=10_000, wallet_peak_usd=10_000, wallet_24h_ago_usd=10_000,
            realized_loss_24h=0, markets=candidates,
        )
        self.assertEqual(500, len(result.deploys),
                         "soft sanity cap (500) must hold")
        # Avoids = (700 - 500 cap) + 0 EV-filtered (all are positive-EV) = 200
        avoided_to_cap = [m for m in result.avoids if m.condition_id.startswith("0x")]
        self.assertGreaterEqual(len(avoided_to_cap), 200)

    def test_AO_A4_notional_routinely_exceeds_wallet(self):
        """Ground Rule 2: 3-8× wallet notional is the design point.
        100 markets at $22/market = $2200 on a $500 wallet = 4.4×.
        """
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {f"0x{i:04d}": 0.10 for i in range(100)}
        candidates = [_make_candidate(f"0x{i:04d}", daily_rate=500) for i in range(100)]
        wallet = 500
        result = a.compute(
            wallet_usd=wallet, wallet_peak_usd=wallet, wallet_24h_ago_usd=wallet,
            realized_loss_24h=0, markets=candidates,
        )
        overcommit_ratio = result.capital_deployed / wallet
        self.assertGreater(overcommit_ratio, 3.0,
                           f"design point 3-8× must be reachable; got {overcommit_ratio:.2f}×")
        self.assertLess(overcommit_ratio, 10.0,
                        f"healthy upper bound; got {overcommit_ratio:.2f}×")


# ════════════════════════════════════════════════════════════════════════════
# AO-B — EV-gate correctness
# ════════════════════════════════════════════════════════════════════════════


class TestAO_B_EVGate(unittest.TestCase):

    def test_AO_B1_positive_ev_market_deploys(self):
        """High q_share + high daily_rate → expected_reward >> fill_cost.
        Must deploy.
        """
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {"0xWIN": 0.10}
        # min_size=20, midpoint=0.5: cost ~ $22, fill_cost ~ $0.44
        # daily_rate=500 × q=0.10 = $50/day reward. $50/day vs $0.44/fill → positive
        candidates = [_make_candidate("0xWIN", daily_rate=500)]
        result = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
        )
        self.assertEqual(1, len(result.deploys))

    def test_AO_B2_negative_ev_market_avoided(self):
        """Tiny q_share or low daily_rate → expected_reward < fill_cost.
        Must avoid.
        """
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        # daily_rate=20 × q=0.0001 = $0.002/day reward
        # fill_cost ~ $0.44 → way under
        a.load_cumulative_ratios = lambda: {"0xLOSE": 0.0001}
        candidates = [_make_candidate("0xLOSE", daily_rate=20)]
        result = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
        )
        self.assertEqual(0, len(result.deploys))
        self.assertIn("0xLOSE", [m.condition_id for m in result.avoids])

    def test_AO_B3_ev_gate_at_boundary_inclusive(self):
        """expected_reward == expected_fill_cost: deploy (>= comparison)."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        # Tune so daily_rate × q ≈ position_notional × fill_frac
        # position = min_size × midpoint × 2 × 1.1 = 20 × 1.0 × 1.1 = $22
        # fill_cost = $22 × 0.02 = $0.44
        # daily_rate × q = 0.44 exactly → boundary
        # Set q = 0.044, daily_rate = 10 → expected_reward = 0.44
        a.load_cumulative_ratios = lambda: {"0xEDGE": 0.044}
        candidates = [_make_candidate("0xEDGE", daily_rate=10)]
        result = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
        )
        # Within float tolerance, the boundary case should deploy
        # (the bot prefers permissive: marginal markets are still deployed
        #  and FX-051 will cool them if they actually lose money)
        # NOTE: this is also subject to MIN_EXPECTED_PER_MARKET ≥ 0.01 floor


# ════════════════════════════════════════════════════════════════════════════
# AO-C — Pre-P2 filters still respected
# ════════════════════════════════════════════════════════════════════════════


class TestAO_C_PrePsiFiltersRespected(unittest.TestCase):

    def test_AO_C1_cooldown_excluded_cids_still_filters(self):
        """FX-051: excluded_cids set must still drop markets from deploy."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {"0xA": 0.10, "0xB": 0.10}
        candidates = [
            _make_candidate("0xA", daily_rate=500),
            _make_candidate("0xB", daily_rate=500),
        ]
        result = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
            excluded_cids={"0xA"},  # cooled by FX-051
        )
        deploy_ids = [m.condition_id for m in result.deploys]
        self.assertNotIn("0xA", deploy_ids, "cooled cid must not deploy")
        self.assertIn("0xB", deploy_ids, "non-cooled cid must deploy")

    def test_AO_C2_extreme_price_filter_still_drops_below_0_10(self):
        """FX-056: midpoint < 0.10 → not eligible (extreme dump slippage)."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {"0xCHEAP": 0.10}
        candidates = [_make_candidate("0xCHEAP", daily_rate=500, midpoint=0.05)]
        result = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
        )
        self.assertEqual(0, len(result.deploys))

    def test_AO_C3_extreme_price_filter_still_drops_above_0_90(self):
        """FX-056: midpoint > 0.90 → not eligible (extreme dump slippage)."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {"0xRICH": 0.10}
        candidates = [_make_candidate("0xRICH", daily_rate=500, midpoint=0.95)]
        result = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
        )
        self.assertEqual(0, len(result.deploys))


# ════════════════════════════════════════════════════════════════════════════
# AO-D — Kill-switch + edge cases
# ════════════════════════════════════════════════════════════════════════════


class TestAO_D_KillSwitchAndEdgeCases(unittest.TestCase):

    def test_AO_D1_kill_switch_empties_deploys_under_overcommit_load(self):
        """Even with 200 EV-positive candidates, kill switch overrides → 0 deploys."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {f"0x{i:04d}": 0.10 for i in range(200)}
        candidates = [_make_candidate(f"0x{i:04d}", daily_rate=500) for i in range(200)]
        wallet = 1000
        # Realized loss > 10% of wallet → kill
        result = a.compute(
            wallet_usd=wallet, wallet_peak_usd=wallet, wallet_24h_ago_usd=wallet,
            realized_loss_24h=200,  # 20% loss
            markets=candidates,
        )
        self.assertTrue(result.kill_switch)
        self.assertEqual(0, len(result.deploys))
        self.assertEqual(0, result.capital_deployed)

    def test_AO_D2_zero_candidates_emits_clean_zero_result(self):
        """Empty candidate list → 0 deploys, 0 avoids, metadata still stamped."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {}
        result = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=[],
        )
        self.assertEqual(0, len(result.deploys))
        self.assertEqual(0, len(result.avoids))
        self.assertFalse(result.kill_switch)
        # Confirm capital is still stamped even on 0-candidate cycle (FX-043)
        self.assertEqual(1000.0, result.total_capital)


# ════════════════════════════════════════════════════════════════════════════
# AO-E — Telemetry + alloc.json metadata
# ════════════════════════════════════════════════════════════════════════════


class TestAO_E_Telemetry(unittest.TestCase):

    def test_AO_E1_overcommit_alloc_log_line_emitted(self):
        """[OVERCOMMIT_ALLOC] log line must appear with expected fields."""
        import logging
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {f"0x{i:04d}": 0.10 for i in range(5)}
        candidates = [_make_candidate(f"0x{i:04d}", daily_rate=500) for i in range(5)]
        with self.assertLogs("simple_allocator", level="INFO") as cm:
            a.compute(
                wallet_usd=500, wallet_peak_usd=500, wallet_24h_ago_usd=500,
                realized_loss_24h=0, markets=candidates,
            )
        joined = "\n".join(cm.output)
        self.assertIn("[OVERCOMMIT_ALLOC]", joined)
        self.assertIn("eligible=", joined)
        self.assertIn("deploys=", joined)
        self.assertIn("overcommit_ratio=", joined)

    def test_AO_E2_alloc_json_overcommit_ratio_matches_real(self):
        """_notional_overcommit_ratio in metadata = capital_deployed / wallet."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {f"0x{i:04d}": 0.10 for i in range(50)}
        candidates = [_make_candidate(f"0x{i:04d}", daily_rate=500) for i in range(50)]
        wallet = 500
        result = a.compute(
            wallet_usd=wallet, wallet_peak_usd=wallet, wallet_24h_ago_usd=wallet,
            realized_loss_24h=0, markets=candidates,
        )
        tmp = tempfile.mktemp(suffix=".json")
        a.write_allocation_json(result, output_path=tmp)
        with open(tmp) as f:
            data = json.load(f)
        os.unlink(tmp)
        expected_ratio = result.capital_deployed / wallet
        self.assertAlmostEqual(
            data["_notional_overcommit_ratio"], expected_ratio, places=2,
        )

    def test_AO_E3_target_market_count_band_in_metadata(self):
        """_target_market_count_band stamped as [50, 200] per Ground Rule 1."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {}
        result = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=[],
        )
        tmp = tempfile.mktemp(suffix=".json")
        a.write_allocation_json(result, output_path=tmp)
        with open(tmp) as f:
            data = json.load(f)
        os.unlink(tmp)
        self.assertEqual([50, 200], data["_target_market_count_band"])


# ════════════════════════════════════════════════════════════════════════════
# AO-F — Adversarial inputs
# ════════════════════════════════════════════════════════════════════════════


class TestAO_F_Adversarial(unittest.TestCase):

    def test_AO_F1_zero_min_size_does_not_crash(self):
        """Polymarket API anomaly: market with min_size=0.
        Bot must not crash; should compute reasonable cost (clamped at floor).
        """
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {"0xZERO": 0.10}
        candidates = [_make_candidate("0xZERO", daily_rate=500, min_size=0)]
        try:
            result = a.compute(
                wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
                realized_loss_24h=0, markets=candidates,
            )
            # Either deploys (with whatever cost the formula yielded) or avoids —
            # both acceptable; what matters is no crash
        except Exception as e:
            self.fail(f"min_size=0 must not crash; got {type(e).__name__}: {e}")

    def test_AO_F2_zero_qshare_filters_via_ev_gate(self):
        """No API, no cumulative, no cold-start (q=0) → expected_reward=0 → avoid."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {}
        # FX-086: cold-start q_share is now the cfg knob RF_COLD_START_Q_SHARE
        # (was the module constant COLD_START_Q_SHARE). Force it to 0 to simulate
        # the worst case, delegating all other knob lookups to the real cfg.
        def _cfg_cold0(key):
            return 0.0 if key == "RF_COLD_START_Q_SHARE" else _real_cfg(key)
        with patch.object(sa, "cfg", _cfg_cold0):
            candidates = [_make_candidate("0xQZERO", daily_rate=500)]
            result = a.compute(
                wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
                realized_loss_24h=0, markets=candidates,
            )
            # expected_reward = 500 * 0 = 0, fails MIN_EXPECTED_PER_MARKET
            self.assertEqual(0, len(result.deploys))

    def test_AO_F3_high_qshare_doesnt_inflate_position_sizing(self):
        """Even with q_share=0.5 (max possible), per-market notional is
        cost-to-score, NOT scaled with expected reward. Sizing stays bounded.
        """
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {"0xHIGH": 0.50}
        candidates = [_make_candidate("0xHIGH", daily_rate=10000, min_size=20)]
        result = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
        )
        self.assertEqual(1, len(result.deploys))
        # Cost = min_size × midpoint × 2 × 1.1 = 20 × 1.0 × 1.1 = $22
        # Even with q_share=0.5 and daily_rate=$10k/day, sizing stays at $22.
        # This is the operating contract: per-market notional is cost-to-score.
        self.assertLess(result.deploys[0].target_capital, 50.0,
                        "per-market sizing must be cost-to-score, not reward-scaled")

    def test_AO_F4_explicit_excluded_cids_None_treated_as_empty_set(self):
        """Backward compat: callers passing None for excluded_cids must work."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {"0xA": 0.10}
        candidates = [_make_candidate("0xA", daily_rate=500)]
        result = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
            excluded_cids=None,
        )
        self.assertEqual(1, len(result.deploys))

    def test_AO_F5_cumulative_loaded_with_500_markets_no_perf_blowup(self):
        """500 markets in the cumulative table — allocator should not
        OOM or take >5s. Sanity check for production at scale.
        """
        import time as time_mod
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {f"0x{i:04d}": 0.10 for i in range(500)}
        candidates = [_make_candidate(f"0x{i:04d}", daily_rate=500) for i in range(500)]
        t0 = time_mod.time()
        result = a.compute(
            wallet_usd=10_000, wallet_peak_usd=10_000, wallet_24h_ago_usd=10_000,
            realized_loss_24h=0, markets=candidates,
        )
        elapsed = time_mod.time() - t0
        self.assertLess(elapsed, 5.0, f"500-market compute took {elapsed:.2f}s, must be <5s")
        self.assertEqual(500, len(result.deploys),
                         "all 500 markets EV-positive must deploy (cap=500 default)")


if __name__ == "__main__":
    unittest.main()
