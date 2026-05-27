"""Adversarial audit — Q-share priority resolution (FX-045).

Each test asserts the DESIRED post-fix behaviour for
``data_collector.query_reward_stats``. A FAILING test = an exposed bug.

FX-045 root cause (verified via 2026-05-23 Helsinki probe):
  Pre-fix Priority 1 returned ``min(windowed_scoring_ratio × 0.5, 0.5)``.
  For a well-positioned bot scoring 100% of the time → q_share = 0.5
  regardless of actual queue share. Live probe found 1500× over-estimate
  vs the cumulative measurement (0.000249–0.000405). I6 invariant fed
  est_d = $40/day vs actual $1–5/day → I6 perpetually fires SEVERELY →
  CALIBRATED state structurally unreachable → friend-rollout G3 blocked.

Fix (Approach E, presence-gate semantics):
  Windowed signal demoted from magnitude estimator to safety override.
  When windowed has ≥ 3 samples AND scoring_ratio < 0.10 → force
  q_share = 0 (we're confidently NOT earning, override stale cumulative).
  Otherwise fall through to Priority 2 (cumulative, a real measurement).

Attack families:

  QS-A  Priority resolution under specific data shapes
  QS-B  Invariants (post-fix bounds)
  QS-C  Regression of the FX-045 verified incident shape
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import RF_NEW_MARKET_Q_SHARE_PRIOR, RF_POISONED_Q_SHARE_THRESHOLD
from oversight.data_collector import (
    query_reward_stats,
    RF_WINDOWED_PRESENCE_GATE,
    RF_WINDOWED_PRESENCE_MIN_SAMPLES,
)


# ── Fixtures ──

class _TestDB:
    """Helper: minimal schema for query_reward_stats unit testing."""

    def __init__(self):
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        db = sqlite3.connect(self.path)
        db.execute("""
            CREATE TABLE reward_market_stats (
                condition_id TEXT PRIMARY KEY,
                data         TEXT NOT NULL,
                updated_at   REAL NOT NULL DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE scoring_snapshots (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL NOT NULL,
                order_id     TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                side         TEXT NOT NULL,
                scoring      INTEGER NOT NULL,
                price        REAL NOT NULL DEFAULT 0,
                shares       REAL NOT NULL DEFAULT 0
            )
        """)
        db.commit()
        self.db = db

    def close(self):
        self.db.close()
        os.close(self.fd)
        os.unlink(self.path)

    def insert_market(
        self, cid: str, *, total_q: float = 0.0, market_q: float = 0.0,
        samples: int = 0, time_on_book_secs: float = 0.0,
        daily_rate: float = 50.0,
    ):
        data = {
            "condition_id": cid,
            "question": f"Question for {cid}?",
            "daily_rate": daily_rate,
            "time_on_book_secs": time_on_book_secs,
            "total_q_score": total_q,
            "total_market_q": market_q,
            "q_score_samples": samples,
            "buy_fills": 0, "cycles_with_orders": 0, "total_cycles": 0,
            "avg_bid_price": 0.0, "avg_ask_price": 0.0, "adverse_fills": 0,
            "spread_capture_usd": 0.0, "cycles_in_reward_window": 0,
            "cycles_both_in_window": 0,
        }
        self.db.execute(
            "INSERT INTO reward_market_stats (condition_id, data, updated_at) "
            "VALUES (?, ?, ?)",
            (cid, json.dumps(data), time.time()),
        )
        self.db.commit()

    def insert_scoring_snapshots(
        self, cid: str, *, total: int, scoring_count: int,
        ts_offset_secs: float = -60.0,
    ):
        """Insert `total` snapshot rows where `scoring_count` have scoring=1.
        All snapshots are recent (within last 4h windowed cutoff)."""
        base_ts = time.time() + ts_offset_secs
        for i in range(total):
            self.db.execute(
                "INSERT INTO scoring_snapshots "
                "(ts, order_id, condition_id, side, scoring) "
                "VALUES (?, ?, ?, ?, ?)",
                (base_ts - i, f"oid_{cid}_{i}", cid, "yes",
                 1 if i < scoring_count else 0),
            )
        self.db.commit()


# ════════════════════════════════════════════════════════════════════════════
# QS-A — Priority resolution under specific data shapes
# ════════════════════════════════════════════════════════════════════════════


class TestQS_A_PriorityResolution(unittest.TestCase):

    def setUp(self):
        self.t = _TestDB()

    def tearDown(self):
        self.t.close()

    def test_QS_A1_well_positioned_bot_uses_cumulative_not_inflated_max(self):
        """Bot scoring 100% (windowed) + small cumulative ratio (0.0003) →
        q_share == cumulative ratio. NOT inflated to 0.5 by Priority 1.

        Pre-FX-045: q_share = min(1.0 × 0.5, 0.5) = 0.5 (1500× over-estimate).
        Post-FX-045: q_share = 0.0003 (matches cumulative measurement).
        """
        self.t.insert_market(
            cid="0xWELLPOS",
            total_q=300.0, market_q=1_000_000.0, samples=50,  # ratio 0.0003
            time_on_book_secs=3600.0,
        )
        self.t.insert_scoring_snapshots(
            "0xWELLPOS", total=20, scoring_count=20,  # 100% in-zone
        )
        out = query_reward_stats(self.t.path)
        q = out["0xWELLPOS"]["q_share"]
        self.assertAlmostEqual(q, 0.0003, places=5,
            msg=f"Priority 1 magnitude inflation NOT removed. q={q}")

    def test_QS_A2_bot_not_scoring_presence_gate_fires(self):
        """Bot scoring 0% (windowed has samples but scoring_count=0)
        with non-trivial cumulative → presence gate fires → q_share = 0.
        We're confidently NOT earning, regardless of cumulative history.
        """
        self.t.insert_market(
            cid="0xSILENT",
            total_q=10_000.0, market_q=1_000_000.0, samples=50,  # ratio 0.01
            time_on_book_secs=3600.0,
        )
        self.t.insert_scoring_snapshots(
            "0xSILENT", total=10, scoring_count=0,  # 0% in-zone
        )
        out = query_reward_stats(self.t.path)
        q = out["0xSILENT"]["q_share"]
        self.assertEqual(0.0, q,
            msg=f"presence gate did not fire on 0% scoring_ratio. q={q}")

    def test_QS_A3_bot_rarely_scoring_presence_gate_fires(self):
        """Bot scoring 5% (windowed) < 10% gate threshold → q_share = 0
        regardless of cumulative."""
        self.t.insert_market(
            cid="0xRARE",
            total_q=10_000.0, market_q=1_000_000.0, samples=50,
            time_on_book_secs=3600.0,
        )
        # 20 snapshots, 1 scoring = 5% ratio (under 10% gate)
        self.t.insert_scoring_snapshots(
            "0xRARE", total=20, scoring_count=1,
        )
        out = query_reward_stats(self.t.path)
        q = out["0xRARE"]["q_share"]
        self.assertEqual(0.0, q,
            msg=f"presence gate did not fire on 5% scoring_ratio. q={q}")

    def test_QS_A4_above_gate_uses_cumulative_not_windowed(self):
        """Scoring 50% (above 10% gate) + cumulative 0.001 → q_share = 0.001.
        Even with mid-range scoring_ratio, magnitude comes from cumulative.
        """
        self.t.insert_market(
            cid="0xMIDPRES",
            total_q=1000.0, market_q=1_000_000.0, samples=50,  # ratio 0.001
            time_on_book_secs=3600.0,
        )
        self.t.insert_scoring_snapshots(
            "0xMIDPRES", total=20, scoring_count=10,  # 50% in-zone
        )
        out = query_reward_stats(self.t.path)
        q = out["0xMIDPRES"]["q_share"]
        self.assertAlmostEqual(q, 0.001, places=5,
            msg=f"50% scoring_ratio path did not use cumulative. q={q}")

    def test_QS_A5_below_sample_gate_windowed_ignored(self):
        """Windowed has only 2 samples (< MIN_SAMPLES=3) → presence gate
        does NOT fire (too noisy). Cumulative used as magnitude."""
        self.t.insert_market(
            cid="0xFEWSAMP",
            total_q=5000.0, market_q=1_000_000.0, samples=50,  # ratio 0.005
            time_on_book_secs=3600.0,
        )
        # 2 snapshots, 0 scoring → would fail gate IF samples >= 3
        self.t.insert_scoring_snapshots(
            "0xFEWSAMP", total=2, scoring_count=0,
        )
        out = query_reward_stats(self.t.path)
        q = out["0xFEWSAMP"]["q_share"]
        # _query_windowed_scoring requires total >= 2 to include the market
        # at all; with 2 samples it returns ratio=0 but our gate needs
        # samples >= 3 so falls through to cumulative.
        self.assertAlmostEqual(q, 0.005, places=5,
            msg=f"low-sample noise tripped presence gate. q={q}")

    def test_QS_A6_no_windowed_data_uses_cumulative(self):
        """No scoring_snapshots at all → cumulative path used unchanged."""
        self.t.insert_market(
            cid="0xNOWIN",
            total_q=2000.0, market_q=1_000_000.0, samples=50,  # ratio 0.002
            time_on_book_secs=3600.0,
        )
        # NO scoring_snapshots inserted
        out = query_reward_stats(self.t.path)
        q = out["0xNOWIN"]["q_share"]
        self.assertAlmostEqual(q, 0.002, places=5)

    def test_QS_A7_presence_gate_wins_over_cold_start_prior(self):
        """Cold-start market (on_book < 2h, samples=0) BUT windowed shows
        rarely scoring → presence gate forces q_share=0. Prior never fires.

        This protects against re-deploying on markets where we already
        showed we can't score (e.g., huge competitive queue).
        """
        self.t.insert_market(
            cid="0xCOLDSILENT",
            total_q=0.0, market_q=0.0, samples=0,  # no cumulative
            time_on_book_secs=1800.0,  # 30 min on book (< 2h cold-start)
        )
        self.t.insert_scoring_snapshots(
            "0xCOLDSILENT", total=10, scoring_count=0,  # 0% in-zone
        )
        out = query_reward_stats(self.t.path)
        q = out["0xCOLDSILENT"]["q_share"]
        self.assertEqual(0.0, q,
            msg=f"presence gate didn't override cold-start prior. q={q}")

    def test_QS_A8_poisoned_cumulative_still_falls_to_prior(self):
        """Cumulative ratio > RF_POISONED_Q_SHARE_THRESHOLD AND windowed
        healthy → cold-start prior (FX-005-era poison guard preserved)."""
        self.t.insert_market(
            cid="0xPOISON",
            # ratio 0.8 — above RF_POISONED_Q_SHARE_THRESHOLD (0.5)
            total_q=800_000.0, market_q=1_000_000.0, samples=50,
            time_on_book_secs=3600.0,
        )
        self.t.insert_scoring_snapshots(
            "0xPOISON", total=20, scoring_count=20,  # healthy presence
        )
        out = query_reward_stats(self.t.path)
        q = out["0xPOISON"]["q_share"]
        self.assertAlmostEqual(q, RF_NEW_MARKET_Q_SHARE_PRIOR, places=5,
            msg=f"poisoned-row guard broken. q={q}")


# ════════════════════════════════════════════════════════════════════════════
# QS-B — Invariants
# ════════════════════════════════════════════════════════════════════════════


class TestQS_B_Invariants(unittest.TestCase):

    def setUp(self):
        self.t = _TestDB()

    def tearDown(self):
        self.t.close()

    def test_QS_B1_q_share_never_exceeds_real_cumulative_ratio(self):
        """The post-FX-045 path never inflates q_share above what the
        cumulative measurement reports. This is the headline invariant
        that 2026-05-23 violated by 1500×.

        Probes 5 different markets with the well-positioned shape and
        asserts the bound holds across all of them.
        """
        cases = [
            ("0xINV_1", 250.0, 1_000_000.0),    # 0.00025
            ("0xINV_2", 400.0, 1_000_000.0),    # 0.0004
            ("0xINV_3", 1000.0, 1_000_000.0),   # 0.001
            ("0xINV_4", 5000.0, 1_000_000.0),   # 0.005
            ("0xINV_5", 50000.0, 1_000_000.0),  # 0.05
        ]
        for cid, q, mq in cases:
            self.t.insert_market(
                cid=cid, total_q=q, market_q=mq, samples=50,
                time_on_book_secs=3600.0,
            )
            self.t.insert_scoring_snapshots(
                cid, total=20, scoring_count=20,  # all scoring
            )
        out = query_reward_stats(self.t.path)
        for cid, q, mq in cases:
            cum_ratio = q / mq
            self.assertLessEqual(
                out[cid]["q_share"], cum_ratio + 1e-9,
                f"{cid}: q_share={out[cid]['q_share']} > "
                f"cumulative ratio {cum_ratio}",
            )

    def test_QS_B2_q_share_zero_for_stale_market(self):
        """6h+ since last scoring snapshot → q_share=0 regardless of
        cumulative or windowed. Existing staleness gate unchanged.

        time_on_book_secs=7200 (2h) so the `on_book > 1` strict-greater
        condition in data_collector trips. With time_on_book exactly
        equal to 1h the staleness gate would NOT fire (intentional
        cold-start carve-out for very-new markets).
        """
        self.t.insert_market(
            cid="0xSTALE",
            total_q=10_000.0, market_q=1_000_000.0, samples=50,
            time_on_book_secs=7200.0,  # 2h on book — strictly > 1
        )
        # Snapshot 7h ago — outside both 4h windowed window and 6h staleness
        self.t.insert_scoring_snapshots(
            "0xSTALE", total=20, scoring_count=20,
            ts_offset_secs=-7 * 3600,
        )
        out = query_reward_stats(self.t.path)
        self.assertEqual(0.0, out["0xSTALE"]["q_share"])

    def test_QS_B3_well_positioned_q_share_drops_dramatically_post_fix(self):
        """Headline invariant: the FX-045 verified incident — bot 100%
        scoring with cumulative ratio 0.0003 — used to return 0.5
        (Priority 1) and now returns 0.0003 (Priority 2 cumulative).
        Ratio: 1666× reduction. This is THE bug that blocked G3.
        """
        self.t.insert_market(
            cid="0xINCIDENT",
            total_q=300.0, market_q=1_000_000.0, samples=50,  # ratio 0.0003
            time_on_book_secs=3600.0,
        )
        self.t.insert_scoring_snapshots(
            "0xINCIDENT", total=20, scoring_count=20,  # 100% scoring
        )
        out = query_reward_stats(self.t.path)
        q = out["0xINCIDENT"]["q_share"]
        # Pre-FX-045: q would be 0.5. Post-fix: q ≈ 0.0003. Sanity-check
        # by asserting q is < 1/100 of the pre-fix value.
        self.assertLess(q, 0.005,
            msg=f"q_share={q} too close to pre-FX-045 inflated 0.5 "
                f"(was 1666× over-estimate)")


# ════════════════════════════════════════════════════════════════════════════
# QS-C — Regression of the FX-045 verified incident shape
# ════════════════════════════════════════════════════════════════════════════


class TestQS_C_IncidentRegression(unittest.TestCase):
    """The 2026-05-23 08:42 UTC Helsinki probe captured exact numbers for
    two deployed markets. Reproduce both shapes; verify post-fix q_share
    matches the cumulative ratio (no 1235–2000× over-estimate)."""

    def setUp(self):
        self.t = _TestDB()

    def tearDown(self):
        self.t.close()

    def test_QS_C1_market_0x475c9930_probe(self):
        """OpenAI valuation $2.5T HIGH. Cumulative ratio 0.000249,
        pre-fix returned 0.5 (Priority 1 max), 2000× over-estimate.
        Post-fix: returns 0.000249."""
        # 120968 / 487M = 0.000248
        self.t.insert_market(
            cid="0x475c9930", total_q=120968.0, market_q=487_000_000.0,
            samples=50, time_on_book_secs=3600.0, daily_rate=30.0,
        )
        self.t.insert_scoring_snapshots(
            "0x475c9930", total=20, scoring_count=20,
        )
        out = query_reward_stats(self.t.path)
        q = out["0x475c9930"]["q_share"]
        expected = 120968.0 / 487_000_000.0
        self.assertAlmostEqual(q, expected, places=8,
            msg=f"FX-045 incident-1 regression: q={q}, expected {expected}")
        # Spot-check the contribution to est_d
        est_d_contribution = 30.0 * q
        self.assertLess(est_d_contribution, 0.05,
            msg=f"est_d contribution {est_d_contribution} too high")

    def test_QS_C2_market_0x0ed3f07970_probe(self):
        """OpenAI valuation $2.0T HIGH. Cumulative ratio 0.000405,
        pre-fix returned 0.5, 1235× over-estimate. Post-fix: 0.000405."""
        # 91057 / 225M = 0.000405
        self.t.insert_market(
            cid="0x0ed3f07970", total_q=91057.0, market_q=225_000_000.0,
            samples=50, time_on_book_secs=3600.0, daily_rate=50.0,
        )
        self.t.insert_scoring_snapshots(
            "0x0ed3f07970", total=20, scoring_count=20,
        )
        out = query_reward_stats(self.t.path)
        q = out["0x0ed3f07970"]["q_share"]
        expected = 91057.0 / 225_000_000.0
        self.assertAlmostEqual(q, expected, places=8,
            msg=f"FX-045 incident-2 regression: q={q}, expected {expected}")


if __name__ == "__main__":
    unittest.main()
