"""Adversarial audit — P3 of 9/10 plan (FX-046 formal resolution).

FX-046 is the "Polymarket reward formula uncertainty" entry. Research
agent investigation showed all 3 candidate formulas (squared, linear,
size-share) under-predict actual payouts by 24-94×. No clean code fix
disambiguates the cause (formula error vs market_q over-counting vs
snapshot staleness).

P3 resolution:
  1. Formally accept FX-046 as Won't Fix / Accepted Risk (fixit §5)
  2. Add `RF_OVERCOMMIT_Q_SHARE_CONSERVATIVE_FACTOR` cfg knob (default 1.0
     = no-op) that lets operators bias NON-API q_share estimates down at
     runtime if production data shows over-deployment.
  3. API-sourced q_share is the ground truth (Polymarket's own measure)
     — no margin applied to it.

Tests:
  P3-A  Conservative factor application
  P3-B  Default no-op behaviour preservation
  P3-C  Tunability via cfg
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import simple_allocator as sa
from simple_allocator import (
    SimpleAllocator,
    CandidateMarket,
    Q_SHARE_CONSERVATIVE_FACTOR,
)


def _make_allocator(now=1700000000):
    return SimpleAllocator(
        db_path=":memory:", wallet_address="0xW", funder="0xF",
        api_key="k", api_secret="MTIzNDU2Nzg5MDEyMzQ1Ng==",
        api_passphrase="p",
        _now=lambda: now,
        _http=lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: {"data": []}),
    )


def _make_candidate(cid: str, daily_rate=500, min_size=20, midpoint=0.5):
    return CandidateMarket(
        condition_id=cid, yes_tid="y", no_tid="n",
        daily_rate=daily_rate, max_spread=4.5, min_size=min_size,
        midpoint_guess=midpoint,
    )


class TestP3_A_ConservativeFactor(unittest.TestCase):

    def test_P3_A1_default_factor_is_1_no_op(self):
        """Default RF_OVERCOMMIT_Q_SHARE_CONSERVATIVE_FACTOR=1.0 must be a no-op."""
        self.assertEqual(1.0, Q_SHARE_CONSERVATIVE_FACTOR())

    def test_P3_A2_factor_halves_cumulative_q_share(self):
        """Setting factor=0.5 should halve cumulative-sourced q_share."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {"0xCUMUL": 0.10}
        candidates = [_make_candidate("0xCUMUL", daily_rate=500)]
        with patch("simple_allocator.cfg") as mock_cfg:
            mock_cfg.side_effect = lambda k: (
                0.5 if k == "RF_OVERCOMMIT_Q_SHARE_CONSERVATIVE_FACTOR"
                else 10.0 if k == "RF_OVERCOMMIT_MIN_DAILY_RATE_USD"
                else 0.01 if k == "RF_OVERCOMMIT_MIN_EXPECTED_PER_MARKET"
                else 500 if k == "RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS"
                else 0.10 if k == "RF_OVERCOMMIT_PER_MARKET_BUFFER_FRAC"
                else 0.02 if k == "RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC"
                else 1.0
            )
            result = a.compute(
                wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
                realized_loss_24h=0, markets=candidates,
            )
        # Cumulative q_share=0.10, factor=0.5 → effective q=0.05
        self.assertEqual(1, len(result.deploys))
        self.assertAlmostEqual(0.05, result.deploys[0].expected_q_share, places=4)

    def test_P3_A3_factor_does_NOT_apply_to_api_qshare(self):
        """API q_share is ground truth — conservative factor must NOT apply."""
        a = _make_allocator()
        # API returns this market with q_share=0.10
        a.fetch_current_q_shares = lambda: {"0xAPI": 0.10}
        a.load_cumulative_ratios = lambda: {}
        candidates = [_make_candidate("0xAPI", daily_rate=500)]
        with patch("simple_allocator.cfg") as mock_cfg:
            mock_cfg.side_effect = lambda k: (
                0.5 if k == "RF_OVERCOMMIT_Q_SHARE_CONSERVATIVE_FACTOR"
                else 10.0 if k == "RF_OVERCOMMIT_MIN_DAILY_RATE_USD"
                else 0.01 if k == "RF_OVERCOMMIT_MIN_EXPECTED_PER_MARKET"
                else 500 if k == "RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS"
                else 0.10 if k == "RF_OVERCOMMIT_PER_MARKET_BUFFER_FRAC"
                else 0.02 if k == "RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC"
                else 1.0
            )
            result = a.compute(
                wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
                realized_loss_24h=0, markets=candidates,
            )
        # API q_share preserved at 0.10 (no margin applied)
        self.assertEqual(1, len(result.deploys))
        self.assertAlmostEqual(0.10, result.deploys[0].expected_q_share, places=4)
        self.assertEqual("api", result.deploys[0].q_share_source)

    def test_P3_A4_factor_applies_to_cold_start_prior(self):
        """Cold-start markets also use the conservative factor."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {}
        # No API + no cumulative → cold_start prior = 0.005
        candidates = [_make_candidate("0xCOLD", daily_rate=500)]
        with patch("simple_allocator.cfg") as mock_cfg:
            mock_cfg.side_effect = lambda k: (
                0.5 if k == "RF_OVERCOMMIT_Q_SHARE_CONSERVATIVE_FACTOR"
                else 10.0 if k == "RF_OVERCOMMIT_MIN_DAILY_RATE_USD"
                else 0.01 if k == "RF_OVERCOMMIT_MIN_EXPECTED_PER_MARKET"
                else 500 if k == "RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS"
                else 0.10 if k == "RF_OVERCOMMIT_PER_MARKET_BUFFER_FRAC"
                else 0.02 if k == "RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC"
                else 1.0
            )
            result = a.compute(
                wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
                realized_loss_24h=0, markets=candidates,
            )
        # COLD_START_Q_SHARE=0.005 × factor=0.5 = 0.0025
        # expected_reward = 500 × 0.0025 = $1.25/day, above MIN_EXPECTED_PER_MARKET
        # cost-to-score = $22, fill_cost = $0.44 → 1.25 > 0.44 → deploys
        self.assertEqual(1, len(result.deploys))
        self.assertAlmostEqual(0.0025, result.deploys[0].expected_q_share, places=5)
        self.assertEqual("cold_start", result.deploys[0].q_share_source)


class TestP3_B_DefaultNoOp(unittest.TestCase):

    def test_P3_B1_default_factor_preserves_cumulative_unchanged(self):
        """With factor=1.0 default, cumulative q_share passes through unchanged."""
        a = _make_allocator()
        a.fetch_current_q_shares = lambda: {}
        a.load_cumulative_ratios = lambda: {"0xC": 0.10}
        candidates = [_make_candidate("0xC", daily_rate=500)]
        result = a.compute(
            wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
            realized_loss_24h=0, markets=candidates,
        )
        self.assertAlmostEqual(0.10, result.deploys[0].expected_q_share, places=4)


if __name__ == "__main__":
    unittest.main()
