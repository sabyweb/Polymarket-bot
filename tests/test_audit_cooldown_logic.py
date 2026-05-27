"""Adversarial audit — Cold-Start Trap (#6) + Cooldown Gaming (#4).

Each test asserts the DESIRED post-fix behaviour. A FAILING test in this file =
an exposed bug. Once the fix lands, the tests must pass.

Naming convention:
  test_CS<N>_… → Cold-Start trap attack (audit category #6)
  test_CG<N>_… → Cooldown Gaming attack (audit category #4)

Tests are deliberately written against the public surface (tracker.tick +
policy.evaluate) — not against internal helpers — so fixes that change the
mechanism (lower fast-path threshold, add 7d window, add per-fill ratio
trigger, etc.) all satisfy the same assertion.

Each test docstring states:
  • Scenario reproduced
  • Severity in operational terms
  • Why this matters under ground_rules.md
  • Why current code fails
  • What "fixed" means (the assertion)
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from types import SimpleNamespace

import pytest

from market_roi_tracker import MarketROITracker
from decision_policy import (
    DecisionPolicy,
    ABS_LOSS_FAST_COOLDOWN_USD,
    COOLDOWN_PERIOD_SEC,
    ROI_COOLDOWN_MIN_SAMPLES,
    ROI_COOLDOWN_THRESHOLD,
)
from database import BotDatabase


# ── Fixtures (mirrored from test_decision_policy.py for self-containment) ──

def _make_db():
    p = tempfile.mktemp(suffix=".db")
    BotDatabase(p)
    return p


def _make_tracker_and_policy(db, now: float = 1_700_000_000.0):
    state = {"now": now}
    def fake_now():
        return state["now"]
    tracker = MarketROITracker(
        db_path=db, funder="0xF",
        _now=fake_now,
        _http=lambda *a, **k: SimpleNamespace(status_code=500, json=lambda: {}),
    )
    policy = DecisionPolicy(db_path=db, tracker=tracker, _now=fake_now)
    return tracker, policy, state


def _insert_unwind(db, cid, ts, pnl, shares=50, vwap_cost=None):
    """Insert one SELL/unwind row."""
    if vwap_cost is None:
        vwap_cost = shares * 0.5
    sell_price = (vwap_cost + pnl) / shares if shares > 0 else 0
    usd_value = sell_price * shares
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO unwinds (ts, condition_id, side, shares, sell_price, "
        "usd_value, vwap_cost, pnl) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, cid, "yes", shares, sell_price, usd_value, vwap_cost, pnl),
    )
    conn.commit()
    conn.close()


def _insert_fill(db, cid, ts, shares=50):
    """Insert one BUY/fill row (drives `samples` in the tracker)."""
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO fills (ts, condition_id, side, fill_type, shares, price, "
        "clob_cost, usd_value, midpoint, slippage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, cid, "yes", "FULL", shares, 0.5, 0.5, shares * 0.5, 0.5, 0),
    )
    conn.commit()
    conn.close()


def _insert_capital_snapshot(db, cid, ts, capital):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO capital_committed_snapshots (ts, condition_id, est_capital_cost) "
        "VALUES (?, ?, ?)",
        (ts, cid, capital),
    )
    conn.commit()
    conn.close()


def _seed_full_window_capital(db, cid, now, window_secs=86400.0, capital=50.0,
                               cadence_secs=1800.0):
    """Seed capital snapshots at the allocator cadence so capital_committed_avg
    is sensible. Avoids polluting tests with the CS-3/CS-4 denominator bug."""
    t = now - window_secs
    while t <= now:
        _insert_capital_snapshot(db, cid, t, capital)
        t += cadence_secs


# ════════════════════════════════════════════════════════════════════════════
# COLD-START TRAP (#6) — attacks on the regime where samples < gate
# ════════════════════════════════════════════════════════════════════════════


def test_CS1_single_fill_slow_bleed_under_fast_path_never_cools():
    """A market with ONE fill producing a $1.50 loss never cools.

    Scenario: at ground-rules-target rate of < 1 fill/day, a market that
    produced a single $1.50 loss with $50 capital deployed sits at ROI = -3%
    after 24h. samples=1 < 3 → roi-trigger inhibited. fill_loss=$1.50 < $2.00
    → fast-path inhibited. Market stays in the deploy set, ready to bleed
    again tomorrow.

    Severity: HIGH. 50 such markets × $1.50/day × 30 days = $2,250/month loss
    that the bot cannot self-correct from. Violates Ground Rule 3
    (auto-correction must trigger on negative ROI markets).

    Post-fix: the assertion `cool_down` must hold via SOME mechanism
    (lowered fast-path, per-fill loss-ratio gate, lowered sample gate, or
    new low-confidence-cold trigger).
    """
    db = _make_db()
    tracker, policy, st = _make_tracker_and_policy(db)
    now = st["now"]
    _seed_full_window_capital(db, "0xBLEED", now, capital=50.0)
    _insert_fill(db, "0xBLEED", now - 100)
    _insert_unwind(db, "0xBLEED", now - 50, pnl=-1.50, vwap_cost=25.0)

    tracker.tick(skip_reward_api=True)
    ev = policy.evaluate()

    assert "0xBLEED" in ev["newly_cooled"], (
        f"slow-bleed market not cooled. ev={ev}"
    )
    os.unlink(db)


def test_CS2_aggregate_sub_threshold_losses_dont_trigger():
    """5 fills × $0.39 loss = $1.95 cumulative, samples=5 (gate cleared!),
    roi=-0.039 (just above -0.05 threshold) — neither trigger fires.

    Scenario: the cooldown thresholds have a gap between
    (samples_gate cleared, roi within tolerance) and (fast-path tripped).
    A market producing many small losses falls into that gap.

    Severity: HIGH. Same family as CS-1 but the sample gate IS satisfied
    here — the bug is purely that ROI -0.039 isn't considered bad. With
    50 such markets the bleed is identical to CS-1.

    Post-fix: a market with samples ≥ 3, zero reward, and consistent net
    losses must cool regardless of whether ROI cleared the -5% threshold.
    """
    db = _make_db()
    tracker, policy, st = _make_tracker_and_policy(db)
    now = st["now"]
    _seed_full_window_capital(db, "0xDRIP", now, capital=50.0)
    for i in range(5):
        _insert_fill(db, "0xDRIP", now - (3600 + i * 100))
        _insert_unwind(db, "0xDRIP", now - (3500 + i * 100),
                       pnl=-0.39, vwap_cost=20.0)

    tracker.tick(skip_reward_api=True)
    ev = policy.evaluate()

    # Tracker should report this as a clear loser (no reward, $1.95 loss in 24h)
    snap = tracker.get_roi("0xDRIP", "24h")
    assert snap is not None
    assert snap.fill_loss == pytest.approx(1.95, rel=0.01)
    assert snap.reward_earned == 0.0

    assert "0xDRIP" in ev["newly_cooled"], (
        f"drip-loss market not cooled despite 0 reward + $1.95 loss "
        f"+ samples={snap.samples}. roi={snap.roi:+.4f}. ev={ev}"
    )
    os.unlink(db)


def test_CS3_zero_capital_snapshot_produces_meaningless_roi():
    """A market with fills/unwinds but ZERO capital snapshots gets ROI
    computed against the 0.01 denominator floor — producing a wildly
    misleading number that survives in the DB and is logged.

    Scenario: snapshot_capital() was never called for this cid (e.g., the
    market appeared via on-chain orphan detection rather than alloc, or
    the very first oversight cycle hasn't run yet). tracker.tick computes
    roi = (0 - $1) / max(0, 0.01) = -100. That's a -10000% ROI logged
    every cycle for a $1 loss on a $50 position — pure noise in
    monitoring, AND blocks the operator from spotting real catastrophes.

    Severity: MEDIUM. Telemetry corruption. No false-cooling (samples<3
    blocks the trigger), but no auto-action either, and the operator's
    [LEARN] log line carries an alarming ROI that misleads triage.

    Post-fix: tracker must either (a) bound reported ROI to a sane range
    (e.g., ±10) when capital_committed_avg falls under a threshold, or
    (b) skip ROI computation entirely when capital data is absent,
    leaving the snapshot with a sentinel/NaN that the policy treats as
    "no signal".
    """
    db = _make_db()
    tracker, policy, st = _make_tracker_and_policy(db)
    now = st["now"]
    # Deliberately NO capital snapshots
    _insert_fill(db, "0xGHOST", now - 100)
    _insert_unwind(db, "0xGHOST", now - 50, pnl=-1.0, vwap_cost=20.0)

    tracker.tick(skip_reward_api=True)
    snap = tracker.get_roi("0xGHOST", "24h")
    assert snap is not None

    # ROI MUST be in a reasonable range. -100 / -10 / -1000 are all garbage.
    assert -10.0 <= snap.roi <= 10.0, (
        f"ROI={snap.roi} is wildly out-of-range due to capital_avg floor; "
        f"tracker should bound or skip ROI when capital is absent. "
        f"capital_avg={snap.capital_committed_avg}"
    )
    os.unlink(db)


def test_CS4_capital_avg_undercount_when_only_one_snapshot_in_window():
    """1h window with a single capital snapshot at minute 59 yields a
    time-weighted average of ~$0.83 (capital × dwell / window) instead of
    $50, because the time BEFORE the first snapshot has no contribution.

    Scenario: oversight runs every 30 min, so the 1h window typically has
    1-2 snapshots. If allocator-output drift puts both snapshots in the
    final few minutes, the average collapses → ROI denominator collapses
    → cooldown fires for the wrong reason on otherwise-healthy markets.

    Severity: MEDIUM. Spurious cooldowns reduce reward-farming surface
    area (violates Ground Rule 1) and the operator can't distinguish a
    real loser from a window-boundary artifact in the [LEARN] log.

    Post-fix: _capital_committed_avg must account for capital state
    BEFORE the first in-window snapshot — either by querying the last
    snapshot before window_start, or by extrapolating the first
    in-window snapshot backwards.
    """
    db = _make_db()
    tracker, policy, st = _make_tracker_and_policy(db)
    now = st["now"]
    # Capital has been $50 the whole hour, but only one snapshot at minute 59
    _insert_capital_snapshot(db, "0xEDGE", now - 60, 50.0)
    _insert_fill(db, "0xEDGE", now - 30)
    _insert_unwind(db, "0xEDGE", now - 10, pnl=-0.10, vwap_cost=25.0)

    tracker.tick(skip_reward_api=True)
    snap_1h = tracker.get_roi("0xEDGE", "1h")
    assert snap_1h is not None

    # Capital_committed_avg over the 1h window should be ~$50 (capital
    # was committed for at least the period the snapshot reflects).
    assert snap_1h.capital_committed_avg >= 25.0, (
        f"capital_avg={snap_1h.capital_committed_avg} undercounts. "
        f"Pre-first-snapshot interval was unattributed. "
        f"ROI={snap_1h.roi} is therefore inflated relative to reality."
    )
    os.unlink(db)


# ════════════════════════════════════════════════════════════════════════════
# COOLDOWN GAMING (#4) — attacks on the cooldown lifecycle
# ════════════════════════════════════════════════════════════════════════════


def test_CG1_expired_cooldown_with_still_bad_roi_does_not_re_cool():
    """After cooldown expires, if the market's ROI is STILL bad, the policy
    must re-cool — not reactivate-then-rediscover-it-is-bad next cycle.

    Scenario: cool 0xLOOP via fast-path. Advance past 24h cooldown. The
    fresh ROI row still shows the same losses (haven't aged out of 24h
    window yet because we advanced JUST past cooldown_period, the
    losses are still within window_end - 24h).
    The current code:
      1. evaluate_market sees existing cooldown row, until_ts < now
      2. _delete_cooldown(cid)
      3. roi_now = tracker.get_roi(cid, '24h') → returns the still-bad snap
      4. returns action='reactivate' (with the still-bad ROI in reason)
      5. get_excluded_cids() returns set without the cid → allocator
         re-deploys this market
      6. Some farmer cycles pass; another loss is taken
      7. Next oversight cycle re-cools the market
    The intermediate "allow" window is the bug.

    Severity: HIGH. Bot eats one extra round of fills on a confirmed
    loser every cooldown expiry. Worst case: a market resolves slowly
    and bleeds losses for weeks → cool → expire → reactivate → bleed
    again → cool → ... 1 loss/day × N days of haven't-resolved-yet.

    Post-fix: on expired cooldown + still-bad fresh ROI, immediately
    re-cool (action='cool_down' not 'reactivate'). Equivalently: the
    cooldown row should be replaced with a new one with a fresh
    cooldown_until, NOT deleted.
    """
    db = _make_db()
    tracker, policy, st = _make_tracker_and_policy(db)
    now = st["now"]
    _seed_full_window_capital(db, "0xLOOP", now, capital=50.0)

    # Initial bad state — fast-path fires
    _insert_fill(db, "0xLOOP", now - 100)
    _insert_unwind(db, "0xLOOP", now - 50, pnl=-3.0, vwap_cost=25.0)
    tracker.tick(skip_reward_api=True)
    ev1 = policy.evaluate()
    assert "0xLOOP" in ev1["newly_cooled"]
    assert "0xLOOP" in policy.get_excluded_cids()

    # Advance past cooldown. The unwind ts (now-50) is now 24h+50s ago →
    # outside the 24h window. So tick will report fill_loss = 0 (aged out).
    # Insert a FRESH bad fill within the new window to keep "still bad".
    st["now"] += COOLDOWN_PERIOD_SEC + 60
    new_now = st["now"]
    _seed_full_window_capital(db, "0xLOOP", new_now, capital=50.0)
    _insert_fill(db, "0xLOOP", new_now - 100)
    _insert_unwind(db, "0xLOOP", new_now - 50, pnl=-3.0, vwap_cost=25.0)

    tracker.tick(skip_reward_api=True)
    ev2 = policy.evaluate()

    # The market is STILL bad. It must be cooled, not reactivated.
    assert "0xLOOP" in policy.get_excluded_cids(), (
        f"market still bad post-expiry but was reactivated and left in "
        f"allow set for one cycle. ev2={ev2}"
    )
    os.unlink(db)


def test_CG2_persistent_per_fill_loss_just_under_fast_path_never_cools():
    """A market that loses exactly $1.99 per fill, 1 fill/day for 7 days,
    is never cooled by either trigger. Net 7-day loss: $13.93/market.

    Scenario: pnl=-1.99 on each unwind. fill_loss_24h is always $1.99
    (only 1 fill per 24h window) → fast-path threshold $2 never tripped.
    samples=1 → roi-trigger sample gate never tripped. Even though the
    market is a pure loser with zero reward, the bot deploys on it day
    after day.

    Severity: HIGH. This is the same family as CS-1 but with a sharper
    boundary case: an adversarial market maker (or just an unlucky
    distribution) could keep loss-per-fill an epsilon under $2 and farm
    losses out of the bot indefinitely.

    Post-fix: a multi-day pattern of pure-loss fills with zero reward
    must cool the market, by ONE of:
      (a) Lowered fast-path threshold (e.g., $1.00)
      (b) Per-fill ratio trigger (pnl / vwap_cost < -X%)
      (c) New 7d cumulative threshold
      (d) Lowered sample gate (e.g., samples ≥ 1 + roi < -2%)

    We simulate 7 ticks at 1 fill/day. After the 7th day, market must
    be cooled.
    """
    db = _make_db()
    tracker, policy, st = _make_tracker_and_policy(db)
    cid = "0xEDGE99"

    # Simulate 7 days. Each day: insert 1 fill+unwind, tick, evaluate.
    # Advance just shy of the cooldown period (86340 sec = 23h 59min) so the
    # next cycle still sees the cooldown as active rather than landing at the
    # exact expiry boundary (a test artifact that doesn't occur in production,
    # where oversight cycles run every 30 min — many checks per cooldown
    # lifetime).
    daily_cool_observations = 0
    for day in range(7):
        now = st["now"]
        _seed_full_window_capital(db, cid, now, capital=50.0)
        _insert_fill(db, cid, now - 100)
        _insert_unwind(db, cid, now - 50, pnl=-1.99, vwap_cost=25.0)
        tracker.tick(skip_reward_api=True)
        policy.evaluate()
        if cid in policy.get_excluded_cids():
            daily_cool_observations += 1
        st["now"] += 86340  # 23h 59min — keeps cooldown active across iterations

    # Across all 7 daily cycles, the market must be observed as cooled.
    # Even one "allow" cycle is a bleed window.
    assert daily_cool_observations == 7, (
        f"market with 7 days of $1.99 losses/day was cooled in only "
        f"{daily_cool_observations}/7 daily checks. Each missed day = one "
        f"farmer-cycle window of $1.99 bleed."
    )
    os.unlink(db)


def test_CG3_catastrophic_single_fill_loss_under_2usd_absolute():
    """A single fill with 100% loss (e.g., $1.90 position fully zeroed)
    doesn't cool because the absolute USD amount is under the $2 fast-path.

    Scenario: small position ($1.95 vwap_cost) sold for $0 (pnl=-1.95).
    Per-fill loss RATIO is 100%, but the per-fill loss DOLLAR is $1.95 <
    $2 fast-path threshold. samples=1 < 3 → roi-trigger inhibited.
    Outcome: bot will retry this market and likely lose again.

    Severity: MEDIUM (under current $1.2k wallet; sizes are small enough
    that this matters less than CS-1). But it's a class of attack:
    "exploit the dollar threshold by sizing positions just under the
    fast-path number, even if your fundamentals are catastrophic."

    Post-fix: per-fill ratio trigger should fire on pnl/vwap_cost < -50%
    regardless of absolute dollar amount.
    """
    db = _make_db()
    tracker, policy, st = _make_tracker_and_policy(db)
    now = st["now"]
    _seed_full_window_capital(db, "0xWIPE", now, capital=50.0)
    _insert_fill(db, "0xWIPE", now - 100, shares=20)
    # $1.95 position sold for ~$0 — 100% loss on the position
    _insert_unwind(db, "0xWIPE", now - 50, pnl=-1.95, vwap_cost=1.95, shares=20)

    tracker.tick(skip_reward_api=True)
    ev = policy.evaluate()

    assert "0xWIPE" in ev["newly_cooled"], (
        f"market with single 100%-loss fill not cooled. ev={ev}"
    )
    os.unlink(db)
