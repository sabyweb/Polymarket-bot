"""Contract tests for DecisionPolicy (FX-051 / Ground Rule 3 consumer).

Each test names a contract per R6. No network, no sleeps.

Contracts under test (P-series for Policy):
- P1: empty state → no cooldowns, no exclusions
- P2: ROI < threshold AND samples ≥ min → cool_down + add to excluded_cids
- P3: ROI < threshold but samples < min → NO cooldown (sample-gate respected)
- P4: fill_loss > abs_fast_threshold → cool_down BYPASSING the sample gate
- P5: still-active cooldown → still_cooled action, cid stays in excluded_cids
- P6: expired cooldown → reactivate, cid removed from excluded_cids
- P7: get_excluded_cids returns the set of currently-cooled cids
- P8: is_cooled_down agrees with get_excluded_cids
- P9: evaluate() emits structured [LEARN] log per ground_rules.md
- P10: global loss > 50% × reward → emits global warning
- P11: cooldown is per-market (cooling 0xA doesn't cool 0xB)
- P12: re-evaluating a still-cooled market doesn't bump cooldown_until
- P13: ROI snapshot updates DON'T retroactively change cooldown_until
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from types import SimpleNamespace

import pytest

from market_roi_tracker import MarketROITracker
from decision_policy import (
    DecisionPolicy,
    ROI_COOLDOWN_THRESHOLD,
    ROI_COOLDOWN_MIN_SAMPLES,
    ABS_LOSS_FAST_COOLDOWN_USD,
    COOLDOWN_PERIOD_SEC,
)
from database import BotDatabase


# ── Fixtures ──

def _make_db():
    p = tempfile.mktemp(suffix=".db")
    BotDatabase(p)
    return p


def _make_tracker_and_policy(db, now: float = 1_700_000_000.0):
    """Build a tracker + policy pair sharing a clock function so tests can
    advance time via the returned `clock` callable."""
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


def _insert_roi_row(
    db: str, cid: str, window: str, *,
    roi: float, fill_loss: float, samples: int,
    reward_earned: float = 0.0, capital_avg: float = 50.0,
    fill_count: int | None = None, now: float = 1_700_000_000.0,
):
    """Directly insert into market_roi (bypassing tick) for deterministic tests."""
    fc = samples if fill_count is None else fill_count
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO market_roi (condition_id, window, window_end_ts, reward_earned, "
        "fill_loss, capital_committed_avg, roi, fill_count, fill_rate_per_hour, "
        "samples, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cid, window, now, reward_earned, fill_loss, capital_avg, roi, fc, fc / 24.0,
         samples, now),
    )
    conn.commit()
    conn.close()


# ── P1: empty state ──

def test_P1_empty_state_no_action():
    db = _make_db()
    _, policy, _ = _make_tracker_and_policy(db)
    ev = policy.evaluate()
    assert ev["newly_cooled"] == []
    assert ev["still_cooled"] == []
    assert ev["reactivated"] == []
    assert ev["allowed"] == []
    assert policy.get_excluded_cids() == set()
    os.unlink(db)


# ── P2: ROI threshold ──

def test_P2_bad_roi_with_enough_samples_triggers_cooldown():
    db = _make_db()
    _, policy, st = _make_tracker_and_policy(db)
    _insert_roi_row(
        db, "0xBAD", "24h",
        roi=ROI_COOLDOWN_THRESHOLD - 0.01,  # just below threshold
        fill_loss=0.5,  # below the abs-loss fast trigger ($2.00)
        samples=ROI_COOLDOWN_MIN_SAMPLES,  # exactly at the gate
        now=st["now"],
    )
    ev = policy.evaluate()
    assert ev["newly_cooled"] == ["0xBAD"]
    assert "0xBAD" in policy.get_excluded_cids()
    os.unlink(db)


# ── P3: sample-gate respect ──

def test_P3_bad_roi_with_too_few_samples_no_cooldown():
    db = _make_db()
    _, policy, st = _make_tracker_and_policy(db)
    _insert_roi_row(
        db, "0xUNLUCKY", "24h",
        roi=-0.50,  # way below threshold
        fill_loss=0.1,  # well below abs-fast threshold
        samples=ROI_COOLDOWN_MIN_SAMPLES - 1,  # not enough confidence
        now=st["now"],
    )
    ev = policy.evaluate()
    assert ev["newly_cooled"] == []
    assert ev["allowed"] == ["0xUNLUCKY"]
    assert policy.get_excluded_cids() == set()
    os.unlink(db)


# ── P4: abs-fast cooldown ──

def test_P4_abs_loss_fast_path_bypasses_sample_gate():
    """Single big-loss fill cools immediately, even with only 1 sample.

    The 2026-05-25 0x46c09232 incident was $2.13 from a single fill — we
    want to cool *that* market the next cycle without waiting for 3 fills.
    """
    db = _make_db()
    _, policy, st = _make_tracker_and_policy(db)
    _insert_roi_row(
        db, "0xBOOM", "24h",
        roi=0.0,  # not breaching ROI threshold
        fill_loss=ABS_LOSS_FAST_COOLDOWN_USD + 0.50,  # over $2
        samples=1,  # only one fill
        now=st["now"],
    )
    ev = policy.evaluate()
    assert ev["newly_cooled"] == ["0xBOOM"]
    assert "0xBOOM" in policy.get_excluded_cids()
    os.unlink(db)


# ── P5: still-cooled persistence ──

def test_P5_still_active_cooldown_persists():
    db = _make_db()
    _, policy, st = _make_tracker_and_policy(db)
    _insert_roi_row(db, "0xSTILL", "24h",
                    roi=-0.10, fill_loss=5.0, samples=5, now=st["now"])
    policy.evaluate()  # cools 0xSTILL
    # Advance by 1h (cooldown is 24h, so still active)
    st["now"] += 3600
    ev = policy.evaluate()
    assert ev["newly_cooled"] == []
    assert ev["still_cooled"] == ["0xSTILL"]
    assert "0xSTILL" in policy.get_excluded_cids()
    os.unlink(db)


# ── P6: expired → reactivate ──

def test_P6_expired_cooldown_reactivates():
    db = _make_db()
    _, policy, st = _make_tracker_and_policy(db)
    _insert_roi_row(db, "0xEXP", "24h",
                    roi=-0.10, fill_loss=5.0, samples=5, now=st["now"])
    policy.evaluate()
    assert "0xEXP" in policy.get_excluded_cids()

    # Advance past cooldown — but the underlying market_roi row still says
    # ROI is bad. Update it to a positive value to simulate "fills aged out".
    st["now"] += COOLDOWN_PERIOD_SEC + 1
    # Remove old row and insert a healthy one
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM market_roi WHERE condition_id='0xEXP'")
    conn.commit()
    conn.close()
    _insert_roi_row(db, "0xEXP", "24h",
                    roi=0.0, fill_loss=0.0, samples=0, now=st["now"])

    ev = policy.evaluate()
    assert ev["reactivated"] == ["0xEXP"]
    assert "0xEXP" not in policy.get_excluded_cids()
    os.unlink(db)


# ── P7: get_excluded_cids ──

def test_P7_get_excluded_cids_returns_active_cooldowns_only():
    db = _make_db()
    _, policy, st = _make_tracker_and_policy(db)
    # Manually insert two cooldowns: one active, one expired
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO market_cooldowns VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("0xACTIVE", st["now"], st["now"] + 1000, "test", 0, 0, 0),
    )
    conn.execute(
        "INSERT INTO market_cooldowns VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("0xEXPIRED", st["now"] - 1000, st["now"] - 1, "test", 0, 0, 0),
    )
    conn.commit()
    conn.close()
    excluded = policy.get_excluded_cids()
    assert excluded == {"0xACTIVE"}
    os.unlink(db)


# ── P8: is_cooled_down ──

def test_P8_is_cooled_down_matches_get_excluded():
    db = _make_db()
    _, policy, st = _make_tracker_and_policy(db)
    _insert_roi_row(db, "0xCD", "24h",
                    roi=-0.10, fill_loss=5.0, samples=5, now=st["now"])
    policy.evaluate()
    assert policy.is_cooled_down("0xCD")
    assert not policy.is_cooled_down("0xNOPE")
    os.unlink(db)


# ── P9: structured log ──

def test_P9_evaluate_emits_LEARN_line(caplog):
    db = _make_db()
    _, policy, st = _make_tracker_and_policy(db)
    _insert_roi_row(db, "0xLOG", "24h",
                    roi=-0.10, fill_loss=5.0, samples=5, now=st["now"])
    with caplog.at_level(logging.INFO, logger="decision_policy"):
        policy.evaluate()
    learn_lines = [r for r in caplog.records if "[LEARN]" in r.getMessage()]
    assert len(learn_lines) >= 1
    msg = learn_lines[0].getMessage()
    assert "newly_cooled=1" in msg
    assert "daily_roi=" in msg
    os.unlink(db)


# ── P10: global warning ──

def test_P10_global_loss_warning_fires():
    db = _make_db()
    _, policy, st = _make_tracker_and_policy(db)
    # Many losers, 0 reward → loss/reward ratio breached
    _insert_roi_row(db, "0xL1", "24h", roi=-0.10, fill_loss=2.0,
                    samples=1, reward_earned=0.0, now=st["now"])
    _insert_roi_row(db, "0xL2", "24h", roi=-0.10, fill_loss=2.0,
                    samples=1, reward_earned=0.0, now=st["now"])
    # And one with reward but smaller than total loss
    _insert_roi_row(db, "0xW", "24h", roi=0.10, fill_loss=0.0,
                    samples=1, reward_earned=1.0, now=st["now"])

    ev = policy.evaluate()
    # global loss ($4) > 50% × global reward ($1)
    warning_msgs = [w for w in ev["warnings"] if "global_loss_ratio" in w]
    assert len(warning_msgs) == 1
    os.unlink(db)


# ── P11: per-market scope ──

def test_P11_cooldown_is_per_market():
    db = _make_db()
    _, policy, st = _make_tracker_and_policy(db)
    _insert_roi_row(db, "0xBAD", "24h",
                    roi=-0.10, fill_loss=5.0, samples=5, now=st["now"])
    _insert_roi_row(db, "0xGOOD", "24h",
                    roi=0.05, fill_loss=0.0, samples=5, now=st["now"])
    policy.evaluate()
    excluded = policy.get_excluded_cids()
    assert excluded == {"0xBAD"}
    assert "0xGOOD" not in excluded
    os.unlink(db)


# ── P12: still-cooled doesn't bump cooldown_until ──

def test_P12_recooling_same_market_doesnt_extend():
    db = _make_db()
    _, policy, st = _make_tracker_and_policy(db)
    _insert_roi_row(db, "0xSAME", "24h",
                    roi=-0.10, fill_loss=5.0, samples=5, now=st["now"])
    policy.evaluate()

    # Read the cooldown_until set on first cool
    conn = sqlite3.connect(db)
    until_first = conn.execute(
        "SELECT cooldown_until FROM market_cooldowns WHERE condition_id='0xSAME'"
    ).fetchone()[0]
    conn.close()

    # Advance 1h, evaluate again — market is still cooled
    st["now"] += 3600
    ev = policy.evaluate()
    assert ev["still_cooled"] == ["0xSAME"]

    conn = sqlite3.connect(db)
    until_second = conn.execute(
        "SELECT cooldown_until FROM market_cooldowns WHERE condition_id='0xSAME'"
    ).fetchone()[0]
    conn.close()
    # cooldown_until is NOT bumped — the original 24h timer stands
    assert until_second == until_first
    os.unlink(db)


# ── P13: ROI changes don't retroactively affect existing cooldowns ──

def test_P13_roi_change_during_cooldown_doesnt_alter_until():
    db = _make_db()
    _, policy, st = _make_tracker_and_policy(db)
    _insert_roi_row(db, "0xR13", "24h",
                    roi=-0.10, fill_loss=5.0, samples=5, now=st["now"])
    policy.evaluate()
    conn = sqlite3.connect(db)
    until_first = conn.execute(
        "SELECT cooldown_until FROM market_cooldowns WHERE condition_id='0xR13'"
    ).fetchone()[0]
    conn.close()

    # Now the ROI row is "updated" to look much worse — but evaluate should
    # not extend the cooldown because the market is already cooled.
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE market_roi SET fill_loss=20, roi=-0.50 WHERE condition_id='0xR13'"
    )
    conn.commit()
    conn.close()

    st["now"] += 60
    policy.evaluate()
    conn = sqlite3.connect(db)
    until_second = conn.execute(
        "SELECT cooldown_until FROM market_cooldowns WHERE condition_id='0xR13'"
    ).fetchone()[0]
    conn.close()
    assert until_second == until_first
    os.unlink(db)
