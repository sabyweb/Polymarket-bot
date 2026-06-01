"""FX-088 — reward sourcing from the data-api (un-blinds the bot to its rewards).

The old CLOB /rewards/user/markets parser looked for a "markets" key (the API
returns "data") and a flat earnings field (nested per-asset), so reward_earned
was ALWAYS 0 — zeroing capital_efficiency and biasing every market toward "pure
loss". Rewards are credited as a daily AGGREGATE (empty per-market conditionId),
so we source the authoritative total from the public data-api /activity feed and
attribute it across markets proportional to committed capital.
"""

from __future__ import annotations

import calendar
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_roi_tracker import MarketROITracker  # noqa: E402
from database import BotDatabase  # noqa: E402

NOW = 1_700_000_000.0
TODAY = time.strftime("%Y-%m-%d", time.gmtime(NOW))
DAY_START = float(calendar.timegm(time.strptime(TODAY, "%Y-%m-%d")))


def _resp(data, status=200):
    return SimpleNamespace(status_code=status, json=lambda: data)


def _reward_http(reward_items, rebate_items=None):
    rebate_items = rebate_items or []

    def http(url, params=None, timeout=None, **kw):
        params = params or {}
        if params.get("offset", 0):
            return _resp([])  # single page
        t = params.get("type")
        if t == "REWARD":
            return _resp(reward_items)
        if t == "MAKER_REBATE":
            return _resp(rebate_items)
        return _resp([])

    return http


def _make_db():
    p = tempfile.mktemp(suffix=".db")
    BotDatabase(p)
    return p


def _cap(db, cid, ts, cap):
    c = sqlite3.connect(db)
    c.execute("INSERT INTO capital_committed_snapshots (ts, condition_id, est_capital_cost) "
              "VALUES (?,?,?)", (ts, cid, cap))
    c.commit(); c.close()


def _unwind(db, cid, ts, pnl):
    c = sqlite3.connect(db)
    c.execute("INSERT INTO unwinds (ts, condition_id, side, shares, sell_price, usd_value, "
              "vwap_cost, pnl) VALUES (?,?,?,?,?,?,?,?)",
              (ts, cid, "yes", 50, 0.49, 24.5, 25.0, pnl))
    c.commit(); c.close()


class TestFX088Fetch(unittest.TestCase):
    def _t(self, http):
        return MarketROITracker(db_path=":memory:", funder="0xFUND", _http=http, _now=lambda: NOW)

    def test_aggregate_total_in_window(self):
        reward = [{"conditionId": "", "usdcSize": 6.41, "timestamp": DAY_START + 3600},
                  {"conditionId": "", "usdcSize": 99.0, "timestamp": DAY_START - 50}]  # pre-window
        rebate = [{"conditionId": "", "usdcSize": 3.71, "timestamp": DAY_START + 5000}]
        out = self._t(_reward_http(reward, rebate))._fetch_rewards_for_date(TODAY)
        # REWARD(in-window) + MAKER_REBATE(in-window); the 99.0 pre-window is excluded.
        self.assertAlmostEqual(out["__TOTAL__"], 6.41 + 3.71, places=4)

    def test_after_window_excluded(self):
        reward = [{"conditionId": "", "usdcSize": 5.0, "timestamp": DAY_START + 86400 + 10}]
        out = self._t(_reward_http(reward))._fetch_rewards_for_date(TODAY)
        self.assertEqual(out, {"__TOTAL__": 0.0})

    def test_empty_but_fetched_records_zero(self):
        self.assertEqual(self._t(_reward_http([], []))._fetch_rewards_for_date(TODAY),
                         {"__TOTAL__": 0.0})

    def test_non200_failopen(self):
        self.assertEqual(self._t(lambda *a, **k: _resp([], 500))._fetch_rewards_for_date(TODAY), {})

    def test_no_funder_failopen(self):
        t = MarketROITracker(db_path=":memory:", funder="",
                             _http=_reward_http([{"usdcSize": 9, "timestamp": DAY_START}]),
                             _now=lambda: NOW)
        self.assertEqual(t._fetch_rewards_for_date(TODAY), {})

    def test_exception_failopen(self):
        def boom(*a, **k):
            raise RuntimeError("net down")
        self.assertEqual(self._t(boom)._fetch_rewards_for_date(TODAY), {})

    def test_bad_date_failopen(self):
        self.assertEqual(self._t(_reward_http([]))._fetch_rewards_for_date("not-a-date"), {})


class TestFX088Attribution(unittest.TestCase):
    """tick() splits the aggregate reward proportional to committed capital."""

    def test_reward_split_proportional_and_capital_efficiency_nonzero(self):
        db = _make_db()
        # cidA capital ~100, cidB capital ~300 (two snapshots each → dwell-avg holds).
        _cap(db, "0xA", NOW - 100, 100.0); _cap(db, "0xA", NOW - 86400, 100.0)
        _cap(db, "0xB", NOW - 100, 300.0); _cap(db, "0xB", NOW - 86400, 300.0)
        _unwind(db, "0xA", NOW - 100, -5.0)  # cidA realized a $5 loss
        reward = [{"conditionId": "", "usdcSize": 40.0, "timestamp": NOW}]
        t = MarketROITracker(db_path=db, funder="0xFUND", _http=_reward_http(reward), _now=lambda: NOW)
        t.tick()
        a = t.get_roi("0xA", "24h"); b = t.get_roi("0xB", "24h")
        self.assertIsNotNone(a); self.assertIsNotNone(b)
        # Full attribution: shares sum to the aggregate total.
        self.assertAlmostEqual(a.reward_earned + b.reward_earned, 40.0, delta=0.5)
        # Proportional: B has 3x A's capital → ~3x the reward.
        self.assertAlmostEqual(b.reward_earned, 3.0 * a.reward_earned, delta=1.0)
        self.assertAlmostEqual(a.reward_earned, 10.0, delta=1.5)
        self.assertAlmostEqual(b.reward_earned, 30.0, delta=2.0)
        # The headline fix: capital_efficiency is no longer 0.
        gs = t.get_global_summary("24h")
        self.assertGreater(gs["capital_efficiency"], 0.0)
        self.assertAlmostEqual(gs["total_reward"], 40.0, delta=0.5)
        # ROI now reflects reward: A = (10 - 5)/100 = +0.05 (was negative when reward≡0).
        self.assertGreater(a.roi, 0.0)
        os.unlink(db)


if __name__ == "__main__":
    unittest.main()
