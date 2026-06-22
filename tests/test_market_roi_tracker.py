"""Contract tests for MarketROITracker (FX-051 / Ground Rule 3 data layer).

Each test names the contract it protects. Deterministic, no network, no sleeps.

Contracts under test (R-series for ROI):
- R1: empty DB → tick is a clean no-op (no exceptions, no rows written)
- R2: snapshot_capital writes one row per deploy in alloc_result
- R3: snapshot_capital is a no-op when alloc has zero deploys
- R4: tick computes fill_loss correctly from unwinds (sum of |pnl| for pnl<0)
- R5: tick excludes positive-pnl unwinds from fill_loss
- R6: tick counts fills correctly (fill_count, fill_rate_per_hour)
- R7: tick computes time-weighted capital_committed_avg
- R8: tick honours window cutoffs — 1h fills don't leak into 1h window if older
- R9: tick is idempotent (running twice produces same row, only last_updated changes)
- R10: get_roi returns None for unseen (cid, window) pairs
- R11: get_all_for_window returns one row per market for that window only
- R12: get_global_summary aggregates correctly across markets
- R13: prune_old_snapshots deletes only old rows
- R14: roi formula: (reward_earned - fill_loss) / max(capital_committed_avg, 0.01)
- R15: tick skips reward API when skip_reward_api=True (deterministic for tests)
- R16: failed _http (network down) is fail-quiet — tick still completes
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from types import SimpleNamespace

import pytest

from market_roi_tracker import (
    MarketROITracker,
    MarketROISnapshot,
    WINDOWS,
)
from database import BotDatabase
from simple_allocator import CandidateMarket, AllocationResult
from config import BotConfig


# ── Fixtures ──

def _make_db():
    p = tempfile.mktemp(suffix=".db")
    BotDatabase(p)  # init schema (creates all tables including FX-051's)
    return p


def _make_tracker(db_path: str, now: float = 1_700_000_000.0, http=None):
    return MarketROITracker(
        db_path=db_path, funder="0xFUNDER",
        _now=lambda: now,
        _http=http or (lambda *a, **k: SimpleNamespace(status_code=500, text="", json=lambda: {})),
    )


def _insert_fill(db_path: str, cid: str, ts: float, shares: float = 50, side: str = "yes"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO fills (ts, condition_id, side, fill_type, shares, price, "
        "clob_cost, usd_value, midpoint, slippage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, cid, side, "FULL", shares, 0.5, 0.5, shares * 0.5, 0.5, 0),
    )
    conn.commit()
    conn.close()


def _insert_unwind(db_path: str, cid: str, ts: float, pnl: float,
                   shares: float = 50, side: str = "yes"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO unwinds (ts, condition_id, side, shares, sell_price, "
        "usd_value, vwap_cost, pnl) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, cid, side, shares, 0.49, shares * 0.49, shares * 0.5, pnl),
    )
    conn.commit()
    conn.close()


def _insert_capital_snapshot(db_path: str, cid: str, ts: float, capital: float):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO capital_committed_snapshots (ts, condition_id, est_capital_cost) "
        "VALUES (?, ?, ?)",
        (ts, cid, capital),
    )
    conn.commit()
    conn.close()


def _insert_market_roi(db_path: str, cid: str, window: str, reward: float,
                       capital: float, loss: float = 0.0, fills: int = 0,
                       window_end_ts: float = 0.0):
    """Insert a market_roi row directly (bypasses tick) so get_global_summary
    reads a known reward/capital pair — used by the FX-085 capital_efficiency tests."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO market_roi (condition_id, window, window_end_ts, reward_earned, "
        "fill_loss, capital_committed_avg, roi, fill_count, fill_rate_per_hour, "
        "samples, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cid, window, window_end_ts, reward, loss, capital, 0.0, fills, 0.0, fills, 0.0),
    )
    conn.commit()
    conn.close()


# ── R1: empty DB ──

def test_R1_empty_db_tick_is_noop():
    db = _make_db()
    tracker = _make_tracker(db)
    summary = tracker.tick(skip_reward_api=True)
    assert summary["markets_updated"] == 0
    assert summary["windows_updated"] == 0
    assert summary["errors"] == []
    assert summary["active_cids"] == 0

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT COUNT(*) FROM market_roi").fetchone()[0]
    conn.close()
    assert rows == 0
    os.unlink(db)


# ── R2-R3: snapshot_capital ──

def test_R2_snapshot_capital_writes_one_row_per_deploy():
    db = _make_db()
    tracker = _make_tracker(db, now=1700000000)
    cm1 = CandidateMarket("0xA", "yA", "nA", 100, 4.5, 20)
    cm1.target_capital = 25.0
    cm2 = CandidateMarket("0xB", "yB", "nB", 200, 4.5, 20)
    cm2.target_capital = 50.0
    result = AllocationResult(
        deploys=[cm1, cm2], avoids=[], total_capital=1000,
        capital_deployed=75, expected_total_reward=0.5,
    )
    n = tracker.snapshot_capital(result)
    assert n == 2
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT condition_id, ts, est_capital_cost FROM capital_committed_snapshots ORDER BY condition_id"
    ).fetchall()
    conn.close()
    assert len(rows) == 2
    assert rows[0] == ("0xA", 1700000000.0, 25.0)
    assert rows[1] == ("0xB", 1700000000.0, 50.0)
    os.unlink(db)


def test_R3_snapshot_capital_empty_deploys_noop():
    db = _make_db()
    tracker = _make_tracker(db)
    result = AllocationResult(deploys=[], avoids=[], total_capital=1000,
                              capital_deployed=0, expected_total_reward=0)
    assert tracker.snapshot_capital(result) == 0
    os.unlink(db)


# ── R4-R5: fill_loss correctness ──

def test_R4_tick_sums_fill_loss_correctly():
    db = _make_db()
    now = 1_700_000_000.0
    # Two losing unwinds for same cid within 24h
    _insert_unwind(db, "0xLOSER", now - 100, pnl=-2.0)
    _insert_unwind(db, "0xLOSER", now - 200, pnl=-3.0)
    tracker = _make_tracker(db, now=now)
    tracker.tick(skip_reward_api=True)
    snap = tracker.get_roi("0xLOSER", "24h")
    assert snap is not None
    assert snap.fill_loss == pytest.approx(5.0)
    os.unlink(db)


def test_R5_positive_pnl_excluded_from_fill_loss():
    db = _make_db()
    now = 1_700_000_000.0
    _insert_unwind(db, "0xWIN", now - 100, pnl=+3.0)
    _insert_unwind(db, "0xWIN", now - 200, pnl=-1.0)
    tracker = _make_tracker(db, now=now)
    tracker.tick(skip_reward_api=True)
    snap = tracker.get_roi("0xWIN", "24h")
    assert snap is not None
    assert snap.fill_loss == pytest.approx(1.0)  # only the negative
    os.unlink(db)


# ── R6: fill_count + rate ──

def test_R6_tick_counts_fills_and_computes_rate():
    db = _make_db()
    now = 1_700_000_000.0
    for delta in [60, 1200, 3600, 7200]:  # 4 fills in last 2h
        _insert_fill(db, "0xACTIVE", now - delta)
    tracker = _make_tracker(db, now=now)
    tracker.tick(skip_reward_api=True)

    snap_1h = tracker.get_roi("0xACTIVE", "1h")
    snap_24h = tracker.get_roi("0xACTIVE", "24h")
    # The 7200s (2h-old) fills are outside the 1h window; only 60s + 1200s + 3600s inside
    # Actually 3600s ≡ exactly at the cutoff; depends on strict-> vs strict>=. Code uses ts > since_ts.
    # 3600s ago: since_ts = now - 3600; row ts = now - 3600 → NOT > → excluded. So 1h window has 2 (60+1200).
    assert snap_1h.fill_count == 2
    assert snap_24h.fill_count == 4
    assert snap_1h.samples == snap_1h.fill_count  # samples == fill_count alias
    assert snap_24h.fill_rate_per_hour == pytest.approx(4.0 / 24.0)
    os.unlink(db)


# ── R7: capital_committed_avg ──

def test_R7_capital_committed_avg_time_weighted():
    """Two capital snapshots: $20 for half the window, $40 for the other half.
    Time-weighted avg should be $30 (within the window).
    """
    db = _make_db()
    window_secs = 86400.0
    now = 1_700_000_000.0
    # Snapshot 1 at start of window
    _insert_capital_snapshot(db, "0xCAP", now - window_secs, 20.0)
    # Snapshot 2 halfway through
    _insert_capital_snapshot(db, "0xCAP", now - window_secs / 2, 40.0)
    # Plus a recent fill so the cid shows as "active"
    _insert_fill(db, "0xCAP", now - 60)
    tracker = _make_tracker(db, now=now)
    tracker.tick(skip_reward_api=True)
    snap = tracker.get_roi("0xCAP", "24h")
    assert snap is not None
    # First snapshot dwells for window/2, second snapshot dwells for window/2
    # Average = (20 × 0.5 + 40 × 0.5) = 30
    assert snap.capital_committed_avg == pytest.approx(30.0, rel=0.01)
    os.unlink(db)


# ── R8: window cutoff respect ──

def test_R8_old_fills_outside_window_dont_contaminate():
    db = _make_db()
    now = 1_700_000_000.0
    _insert_fill(db, "0xOLD", now - 3600 * 2)  # 2h old — outside 1h window
    _insert_fill(db, "0xOLD", now - 60)         # inside both windows
    tracker = _make_tracker(db, now=now)
    tracker.tick(skip_reward_api=True)
    snap_1h = tracker.get_roi("0xOLD", "1h")
    snap_24h = tracker.get_roi("0xOLD", "24h")
    assert snap_1h.fill_count == 1
    assert snap_24h.fill_count == 2
    os.unlink(db)


# ── R9: tick idempotency ──

def test_R9_tick_is_idempotent():
    db = _make_db()
    now = 1_700_000_000.0
    _insert_unwind(db, "0xIDEM", now - 100, pnl=-1.0)
    tracker = _make_tracker(db, now=now)
    tracker.tick(skip_reward_api=True)
    snap1 = tracker.get_roi("0xIDEM", "24h")
    tracker.tick(skip_reward_api=True)
    snap2 = tracker.get_roi("0xIDEM", "24h")
    # All non-timestamp fields identical; the row was upserted not duplicated
    assert snap1.fill_loss == snap2.fill_loss
    assert snap1.fill_count == snap2.fill_count
    # And only one row in market_roi for (cid, window)
    conn = sqlite3.connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM market_roi WHERE condition_id='0xIDEM' AND window='24h'"
    ).fetchone()[0]
    conn.close()
    assert n == 1
    os.unlink(db)


# ── R10: get_roi missing ──

def test_R10_get_roi_returns_none_for_unseen():
    db = _make_db()
    tracker = _make_tracker(db)
    assert tracker.get_roi("0xNOPE", "24h") is None
    os.unlink(db)


# ── R11: get_all_for_window scoping ──

def test_R11_get_all_for_window_scopes_correctly():
    db = _make_db()
    now = 1_700_000_000.0
    for cid in ("0xA", "0xB", "0xC"):
        _insert_unwind(db, cid, now - 60, pnl=-1.0)
    tracker = _make_tracker(db, now=now)
    tracker.tick(skip_reward_api=True)
    snaps_24h = tracker.get_all_for_window("24h")
    snaps_1h = tracker.get_all_for_window("1h")
    assert {s.condition_id for s in snaps_24h} == {"0xA", "0xB", "0xC"}
    assert {s.condition_id for s in snaps_1h} == {"0xA", "0xB", "0xC"}
    # Window strings on the rows match the queried window
    for s in snaps_24h:
        assert s.window == "24h"
    for s in snaps_1h:
        assert s.window == "1h"
    os.unlink(db)


# ── R12: global summary ──

def test_R12_get_global_summary_aggregates():
    db = _make_db()
    now = 1_700_000_000.0
    _insert_unwind(db, "0xA", now - 60, pnl=-3.0)
    _insert_unwind(db, "0xB", now - 60, pnl=-2.0)
    _insert_fill(db, "0xC", now - 60)  # no loss, just a fill
    tracker = _make_tracker(db, now=now)
    tracker.tick(skip_reward_api=True)
    gs = tracker.get_global_summary("24h")
    assert gs["window"] == "24h"
    assert gs["total_loss"] == pytest.approx(5.0)
    assert gs["n_markets"] == 3
    assert gs["n_loss_markets"] == 2  # 0xA + 0xB
    assert gs["n_reward_markets"] == 0  # no API data
    assert gs["fill_count_total"] >= 1  # at least 0xC's fill
    assert "capital_efficiency" in gs  # FX-085: always present
    os.unlink(db)


# ── FX-085: capital_efficiency (Ground Rule 1 scorecard) ──

def test_FX085_capital_efficiency_ratio():
    db = _make_db()
    now = 1_700_000_000.0
    _insert_market_roi(db, "0xA", "24h", reward=1.5, capital=100.0)
    _insert_market_roi(db, "0xB", "24h", reward=0.5, capital=100.0)
    # FX-091: total_capital = time-averaged per-cycle committed capital from
    # capital_committed_snapshots. One oversight cycle committing $100 to each
    # of two markets → cycle total $200 → avg $200.
    _insert_capital_snapshot(db, "0xA", now - 100, 100.0)
    _insert_capital_snapshot(db, "0xB", now - 100, 100.0)
    tracker = _make_tracker(db, now=now)
    gs = tracker.get_global_summary("24h")  # no tick() → reads inserted rows
    # total_reward 2.0 / total_capital 200.0 = 0.01 reward per $ committed.
    assert gs["total_reward"] == pytest.approx(2.0)
    assert gs["total_capital"] == pytest.approx(200.0)
    assert gs["capital_efficiency"] == pytest.approx(0.01)
    os.unlink(db)


def test_FX085_capital_efficiency_zero_capital_is_safe():
    db = _make_db()
    _insert_market_roi(db, "0xA", "24h", reward=0.0, capital=0.0)
    tracker = _make_tracker(db)
    gs = tracker.get_global_summary("24h")
    # denom floored at 0.01 → no div-by-zero; efficiency resolves to 0.0.
    assert gs["capital_efficiency"] == pytest.approx(0.0)
    os.unlink(db)


def test_FX085_capital_efficiency_is_gross_not_net():
    # capital_efficiency is GROSS reward/capital; daily_roi nets out loss.
    db = _make_db()
    now = 1_700_000_000.0
    _insert_market_roi(db, "0xA", "24h", reward=2.0, capital=100.0, loss=1.0)
    # FX-091: capital from a per-cycle snapshot (one market, $100, one cycle).
    _insert_capital_snapshot(db, "0xA", now - 100, 100.0)
    tracker = _make_tracker(db, now=now)
    gs = tracker.get_global_summary("24h")
    assert gs["capital_efficiency"] == pytest.approx(0.02)        # 2.0 / 100
    assert gs["daily_roi"] == pytest.approx(0.01)                 # (2.0-1.0) / 100
    os.unlink(db)


# ── FX-091: global capital denominator (per-cycle avg, not per-market sum) ──

def test_FX091_total_capital_is_per_cycle_average_not_sum():
    """FX-091: total_capital is the time-averaged TOTAL committed per oversight
    cycle (SUM est_capital_cost GROUP BY ts, then AVG across cycles) — NOT the
    sum of per-market capital_committed_avg, which forward-fills dropped markets
    to `now` and ballooned the Helsinki denominator to $78k on a $1.2k wallet."""
    db = _make_db()
    now = 1_700_000_000.0
    # Cycle A (1h ago): two markets, $100 each → cycle total $200
    _insert_capital_snapshot(db, "0xX", now - 3600, 100.0)
    _insert_capital_snapshot(db, "0xY", now - 3600, 100.0)
    # Cycle B (30m ago): three markets, $100 each → cycle total $300
    _insert_capital_snapshot(db, "0xX", now - 1800, 100.0)
    _insert_capital_snapshot(db, "0xY", now - 1800, 100.0)
    _insert_capital_snapshot(db, "0xZ", now - 1800, 100.0)
    tracker = _make_tracker(db, now=now)
    gs = tracker.get_global_summary("24h")
    # avg per-cycle total = (200 + 300) / 2 = 250 (NOT a held-to-now sum)
    assert gs["total_capital"] == pytest.approx(250.0, rel=0.01)
    os.unlink(db)


def test_FX091_dropped_market_not_forward_filled_to_now():
    """FX-091: a market deployed in one early cycle then DROPPED contributes
    only to that cycle's total — it is not forward-filled at full notional
    across the rest of the window (the root cause of the $78k denominator)."""
    db = _make_db()
    now = 1_700_000_000.0
    # Cycle 1 (20h ago): a $500 market, then dropped (no later snapshots)
    _insert_capital_snapshot(db, "0xDROPPED", now - 72000, 500.0)
    # Cycle 2 (30m ago): a $100 market currently deployed
    _insert_capital_snapshot(db, "0xCURRENT", now - 1800, 100.0)
    tracker = _make_tracker(db, now=now)
    gs = tracker.get_global_summary("24h")
    # Two cycles: totals 500 and 100 → avg 300. The dropped $500 is NOT held to
    # now (the old sum-of-per-market-avg counted such markets ~full-window).
    assert gs["total_capital"] == pytest.approx(300.0, rel=0.01)
    os.unlink(db)


# ── R13: prune_old_snapshots ──

def test_FX091R_global_summary_excludes_stale_rows():
    """Reward-side twin of FX-091: get_global_summary must aggregate only the LATEST
    tick's rows, not every stale market_roi row. tick() conserves Σ reward_earned = the
    window's data-api total per cycle, but it upserts a row per (cid, window) for every
    cid ever active and never deletes them, so SUMming all rows over-counts reward/loss
    by the accumulated stale cids (live 2026-06-22: 1040 rows vs 179 latest-tick →
    $47.50 vs the authoritative $8.46). The inflated reward feeds + suppresses the
    global_tighten / global_reward_low triggers. OFF = byte-identical (sums all)."""
    db = _make_db()
    tracker = _make_tracker(db)
    # Latest tick (window_end_ts=2000): two active cids, conserved totals.
    _insert_market_roi(db, "0xLATE_A", "24h", reward=5.0, capital=100.0, loss=2.0, window_end_ts=2000.0)
    _insert_market_roi(db, "0xLATE_B", "24h", reward=3.0, capital=100.0, loss=1.0, window_end_ts=2000.0)
    # Stale rows from older ticks (cids since inactive) — must NOT be summed.
    _insert_market_roi(db, "0xSTALE_1", "24h", reward=20.0, capital=100.0, loss=4.0, window_end_ts=1000.0)
    _insert_market_roi(db, "0xSTALE_2", "24h", reward=19.5, capital=100.0, loss=3.0, window_end_ts=500.0)

    bc = BotConfig.instance()

    # Flag OFF (default) → byte-identical legacy behaviour: sums ALL rows (inflated).
    bc._overrides.pop("RF_GLOBAL_SUMMARY_LATEST_TICK_ENABLED", None)
    gs_off = tracker.get_global_summary("24h")
    assert gs_off["total_reward"] == pytest.approx(47.5)   # 5+3+20+19.5 (stale included)
    assert gs_off["total_loss"] == pytest.approx(10.0)     # 2+1+4+3
    assert gs_off["n_markets"] == 4

    # Flag ON → only the latest tick's conserved aggregate (stale excluded).
    bc._overrides["RF_GLOBAL_SUMMARY_LATEST_TICK_ENABLED"] = True
    gs_on = tracker.get_global_summary("24h")
    assert gs_on["total_reward"] == pytest.approx(8.0)     # 5+3 only
    assert gs_on["total_loss"] == pytest.approx(3.0)       # 2+1 only
    assert gs_on["n_markets"] == 2

    os.unlink(db)


def test_R13_prune_old_snapshots_deletes_only_old():
    db = _make_db()
    now = 1_700_000_000.0
    _insert_capital_snapshot(db, "0xX", now - 86400 * 20, 10.0)  # 20 days old
    _insert_capital_snapshot(db, "0xX", now - 86400 * 5, 20.0)   # 5 days old
    tracker = _make_tracker(db, now=now)
    deleted = tracker.prune_old_snapshots(retain_secs=86400 * 14)  # keep 14d
    assert deleted == 1
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT COUNT(*) FROM capital_committed_snapshots").fetchone()[0]
    conn.close()
    assert rows == 1  # only the 5-day-old row remains
    os.unlink(db)


# ── R14: roi formula ──

def test_R14_roi_formula_correct():
    """ROI = (reward - loss) / max(capital_avg, 0.01).

    Scenario: $0 reward (no API data), $2 loss, $50 capital. ROI should be
    (-2 - 0) / 50 = -0.04.
    """
    db = _make_db()
    now = 1_700_000_000.0
    _insert_unwind(db, "0xR14", now - 100, pnl=-2.0)
    _insert_capital_snapshot(db, "0xR14", now - 100, 50.0)
    # Also add a snapshot at the start of the window so dwell-time math sees it as $50 throughout
    _insert_capital_snapshot(db, "0xR14", now - 86400, 50.0)
    tracker = _make_tracker(db, now=now)
    tracker.tick(skip_reward_api=True)
    snap = tracker.get_roi("0xR14", "24h")
    assert snap is not None
    # capital_avg should be approximately $50 throughout the window
    assert snap.capital_committed_avg == pytest.approx(50.0, rel=0.05)
    # roi = (0 - 2) / 50 = -0.04
    assert snap.roi == pytest.approx(-0.04, rel=0.05)
    os.unlink(db)


# ── R15: skip_reward_api ──

def test_R15_skip_reward_api_makes_zero_http():
    db = _make_db()
    now = 1_700_000_000.0
    _insert_unwind(db, "0xSK", now - 60, pnl=-1.0)
    call_count = [0]

    def counting_http(*a, **k):
        call_count[0] += 1
        return SimpleNamespace(status_code=500, json=lambda: {})

    tracker = _make_tracker(db, now=now, http=counting_http)
    tracker.tick(skip_reward_api=True)
    assert call_count[0] == 0
    os.unlink(db)


# ── R16: HTTP failure is fail-quiet ──

def test_R16_http_failure_is_fail_quiet():
    db = _make_db()
    now = 1_700_000_000.0
    _insert_unwind(db, "0xQ", now - 60, pnl=-1.0)

    def raising_http(*a, **k):
        raise ConnectionError("network down")

    tracker = MarketROITracker(
        db_path=db, funder="0xF",
        api_key="k", api_secret="MTIzNDU2Nzg5MDEyMzQ1Ng==",
        api_passphrase="p", wallet_address="0xW",
        _now=lambda: now, _http=raising_http,
    )
    # Should not raise even though every API call would
    summary = tracker.tick()
    snap = tracker.get_roi("0xQ", "24h")
    assert snap is not None
    assert snap.fill_loss == pytest.approx(1.0)
    # reward_earned stays 0 since API failed — no false positive
    assert snap.reward_earned == 0.0
    os.unlink(db)
