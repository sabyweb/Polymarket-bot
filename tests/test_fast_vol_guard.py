"""FX-098: fast-volatility timeout guard unit tests.

Covers trigger logic, cohort gating, fail-open behaviour, and timeout
persistence. Uses mocked DB rows so no real SQLite operations are needed.
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import BotConfig, cfg
from models import MarketState
from fast_vol_guard import check_fast_vol_timeout


def _make_ms(cid: str = "cid_001", question: str = "Test?") -> MarketState:
    return MarketState(
        cid=cid, question=question, yes_tid="ytid", no_tid="ntid",
        daily_rate=10.0, max_spread=0.10, min_size=10, tick_size=0.01,
        yes_price=0.50,
    )


def _make_db(rows: list[tuple]) -> MagicMock:
    """Build a DB mock whose _get_conn().execute().fetchone() returns rows."""
    db = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = rows[0] if rows else None
    conn = MagicMock()
    conn.execute.return_value = cursor
    db._get_conn.return_value = conn
    return db


class TestFastVolTrigger(unittest.TestCase):
    """Midpoint-range trigger thresholds."""

    def setUp(self):
        self.now = 1000.0
        self.ms = _make_ms()
        # Defaults: 4c/30s, 6c/60s, 120s timeout.
        BotConfig.instance()._overrides.clear()

    def test_no_movement_no_timeout(self):
        row = (5, 0.50, 0.50, 3, 0.50, 0.50)  # cnt60, max60, min60, cnt30, max30, min30
        db = _make_db([row])
        self.assertFalse(check_fast_vol_timeout(self.ms, db, now=self.now))
        self.assertEqual(0.0, self.ms.fast_vol_timeout_until)

    def test_4c_in_30s_triggers(self):
        # range 0.041 (> 0.04) to avoid floating-point edge at exactly 0.04.
        row = (5, 0.50, 0.459, 3, 0.50, 0.459)
        db = _make_db([row])
        self.assertTrue(check_fast_vol_timeout(self.ms, db, now=self.now))
        self.assertEqual(self.now + 120.0, self.ms.fast_vol_timeout_until)

    def test_6c_in_60s_triggers(self):
        row = (5, 0.53, 0.47, 1, None, None)  # 30s window has only 1 sample
        db = _make_db([row])
        self.assertTrue(check_fast_vol_timeout(self.ms, db, now=self.now))

    def test_active_timeout_short_circuits_without_query(self):
        self.ms.fast_vol_timeout_until = self.now + 10.0
        db = MagicMock()
        self.assertTrue(check_fast_vol_timeout(self.ms, db, now=self.now))
        db._get_conn.assert_not_called()

    def test_timeout_expires_and_does_not_retrigger(self):
        # Old timeout has passed.
        self.ms.fast_vol_timeout_until = self.now - 1.0
        row = (5, 0.50, 0.50, 3, 0.50, 0.50)
        db = _make_db([row])
        self.assertFalse(check_fast_vol_timeout(self.ms, db, now=self.now))

    def test_sparse_30s_window_fail_open(self):
        # 30s window has only 1 sample; 60s window is calm (< 6c) so no trigger.
        row = (5, 0.51, 0.50, 1, None, None)
        db = _make_db([row])
        self.assertFalse(check_fast_vol_timeout(self.ms, db, now=self.now))

    def test_sparse_60s_window_fail_open(self):
        # Only 1 snapshot in 60s window — cannot compute range.
        row = (1, 0.54, 0.46, 1, None, None)
        db = _make_db([row])
        self.assertFalse(check_fast_vol_timeout(self.ms, db, now=self.now))

    def test_db_error_fail_open(self):
        db = MagicMock()
        db._get_conn.side_effect = RuntimeError("locked")
        self.assertFalse(check_fast_vol_timeout(self.ms, db, now=self.now))

    def test_zero_thresholds_disable_guard(self):
        BotConfig.instance()._overrides["RF_FAST_VOL_30S_CENTS"] = 0.0
        BotConfig.instance()._overrides["RF_FAST_VOL_60S_CENTS"] = 0.0
        row = (5, 0.60, 0.40, 3, 0.60, 0.40)
        db = _make_db([row])
        self.assertFalse(check_fast_vol_timeout(self.ms, db, now=self.now))


class TestFastVolCohortGating(unittest.TestCase):
    """A/B cohort gating: default C1 only."""

    def setUp(self):
        BotConfig.instance()._overrides.clear()
        BotConfig.instance()._overrides["RF_AB_EXPERIMENT_ENABLED"] = True
        BotConfig.instance()._overrides["RF_AB_COHORT_COUNT"] = 2
        BotConfig.instance()._overrides["RF_FAST_VOL_COHORT_ONLY"] = 1
        self.now = 1000.0

    def _row_with_move(self) -> tuple:
        return (5, 0.54, 0.46, 3, 0.54, 0.46)

    def test_c1_market_triggers(self):
        # cohort("cid_001", 2) == 1 is empirically true for this cid.
        ms = _make_ms(cid="cid_001")
        db = _make_db([self._row_with_move()])
        self.assertTrue(check_fast_vol_timeout(ms, db, now=self.now))

    def test_c0_market_does_not_trigger(self):
        # cohort("cid_000", 2) == 0 is empirically true for this cid.
        ms = _make_ms(cid="cid_000")
        db = _make_db([self._row_with_move()])
        self.assertFalse(check_fast_vol_timeout(ms, db, now=self.now))

    def test_negative_cohort_applies_to_all(self):
        BotConfig.instance()._overrides["RF_FAST_VOL_COHORT_ONLY"] = -1
        ms = _make_ms(cid="cid_000")
        db = _make_db([self._row_with_move()])
        self.assertTrue(check_fast_vol_timeout(ms, db, now=self.now))

    def test_ab_disabled_applies_to_all(self):
        BotConfig.instance()._overrides["RF_AB_EXPERIMENT_ENABLED"] = False
        ms = _make_ms(cid="cid_000")
        db = _make_db([self._row_with_move()])
        self.assertTrue(check_fast_vol_timeout(ms, db, now=self.now))


if __name__ == "__main__":
    unittest.main()
