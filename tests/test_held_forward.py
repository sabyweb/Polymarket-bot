"""A4 forward held-to-resolution ledger (ab/held_forward.py) — read-only, non-behavioral.

Hermetic: pure-compute (held_cost_since / compute_forward_htr / cohort_htr) is exercised against a
temp bot_history-shaped DB + synthetic redeem/open sets; the isolated ledger store is exercised on a
temp DB. No network, no live state.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ab.cohort import cohort  # noqa: E402
from ab.held_forward import (  # noqa: E402
    held_cost_since, compute_forward_htr, cohort_htr, ensure_schema, upsert_ledger,
)

BASE = 1000.0
POST = 2000.0   # at/after baseline
PRE = 500.0     # before baseline (must be excluded)


def _make_bot_db(path, fills, unwinds):
    """fills rows: (cid, side, shares, usd_value, ts, question); unwinds: (cid, side, shares, ts)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE fills (condition_id TEXT, side TEXT, shares REAL, usd_value REAL, ts REAL, question TEXT)")
    conn.execute("CREATE TABLE unwinds (condition_id TEXT, side TEXT, shares REAL, ts REAL)")
    conn.executemany("INSERT INTO fills VALUES (?,?,?,?,?,?)", fills)
    conn.executemany("INSERT INTO unwinds VALUES (?,?,?,?)", unwinds)
    conn.commit()
    conn.close()


def _ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _bot(tmp_path, fills, unwinds):
    p = os.path.join(tmp_path, "bot.db")
    _make_bot_db(p, fills, unwinds)
    return _ro(p)


# ── held_cost_since: forward scoping + merge-awareness ────────────────────────

def test_held_cost_since_excludes_pre_baseline(tmp_path):
    fills = [
        ("cidA", "yes", 100, 60.0, POST, "Q A"),   # post-baseline → counted
        ("cidB", "yes", 50, 20.0, PRE, "Q B"),      # pre-baseline → excluded
    ]
    bot = _bot(str(tmp_path), fills, [])
    try:
        held = held_cost_since(bot, BASE)
    finally:
        bot.close()
    assert held == {("cidA", "yes"): (100.0, 60.0)}


def test_held_cost_since_subtracts_direct_unwinds(tmp_path):
    fills = [("cidA", "yes", 100, 50.0, POST, "Q")]
    unwinds = [("cidA", "yes", 40, POST)]  # dumped 40 of the 100
    bot = _bot(str(tmp_path), fills, unwinds)
    try:
        held = held_cost_since(bot, BASE)
    finally:
        bot.close()
    # held 60 @ vwap 0.50 = $30
    assert held == {("cidA", "yes"): (60.0, 30.0)}


def test_held_cost_since_is_merge_aware(tmp_path):
    """A merge-row of N shares exits N from BOTH yes and no (held_to_res ignores this; we must not)."""
    fills = [
        ("cidA", "yes", 100, 60.0, POST, "Q"),
        ("cidA", "no", 100, 40.0, POST, "Q"),
    ]
    unwinds = [("cidA", "merge", 60, POST)]  # merged 60 pairs
    bot = _bot(str(tmp_path), fills, unwinds)
    try:
        held = held_cost_since(bot, BASE)
    finally:
        bot.close()
    assert held[("cidA", "yes")] == (40.0, 24.0)   # 40 @ 0.60
    assert held[("cidA", "no")] == (40.0, 16.0)    # 40 @ 0.40


def test_held_cost_since_drops_dust(tmp_path):
    fills = [("cidA", "yes", 100, 50.0, POST, "Q")]
    unwinds = [("cidA", "yes", 99.6, POST)]  # 0.4 left → dust, dropped (<=0.5)
    bot = _bot(str(tmp_path), fills, unwinds)
    try:
        held = held_cost_since(bot, BASE)
    finally:
        bot.close()
    assert held == {}


# ── compute_forward_htr: resolution detection + pnl + flags ───────────────────

def test_resolved_with_redeem_books_pnl(tmp_path):
    fills = [("cidA", "yes", 100, 60.0, POST, "Q A")]
    bot = _bot(str(tmp_path), fills, [])
    try:
        recs = compute_forward_htr(bot, BASE, redeem_by_cid={"cidA": 80.0}, open_cids=set(), n_cohorts=2)
    finally:
        bot.close()
    assert len(recs) == 1
    r = recs[0]
    assert r["condition_id"] == "cidA"
    assert r["cost_usd"] == 60.0
    assert r["redeem_proceeds_usd"] == 80.0
    assert r["held_pnl_usd"] == 20.0          # 80 - 60
    assert r["no_redeem_flag"] == 0
    assert r["cohort"] == cohort("cidA", 2)


def test_resolved_without_redeem_is_full_loss_and_flagged(tmp_path):
    fills = [("cidA", "yes", 100, 60.0, POST, "Q")]
    bot = _bot(str(tmp_path), fills, [])
    try:
        recs = compute_forward_htr(bot, BASE, redeem_by_cid={}, open_cids=set(), n_cohorts=2)
    finally:
        bot.close()
    assert len(recs) == 1
    assert recs[0]["held_pnl_usd"] == -60.0    # lost side / unverified → full cost loss
    assert recs[0]["no_redeem_flag"] == 1


def test_still_open_position_is_excluded(tmp_path):
    fills = [("cidA", "yes", 100, 60.0, POST, "Q")]
    bot = _bot(str(tmp_path), fills, [])
    try:
        recs = compute_forward_htr(bot, BASE, redeem_by_cid={"cidA": 80.0},
                                   open_cids={"cidA"}, n_cohorts=2)  # still on-chain
    finally:
        bot.close()
    assert recs == []   # not yet resolved


def test_fully_exited_position_not_counted(tmp_path):
    """A position dumped to ~0 has no net held shares → never enters the ledger (not held-to-res)."""
    fills = [("cidA", "yes", 100, 60.0, POST, "Q")]
    unwinds = [("cidA", "yes", 100, POST)]
    bot = _bot(str(tmp_path), fills, unwinds)
    try:
        recs = compute_forward_htr(bot, BASE, redeem_by_cid={}, open_cids=set(), n_cohorts=2)
    finally:
        bot.close()
    assert recs == []


def test_cohort_attribution_and_aggregation(tmp_path):
    fills = [
        ("0xaaa", "yes", 100, 60.0, POST, "Q1"),
        ("0xbbb", "yes", 100, 30.0, POST, "Q2"),
    ]
    bot = _bot(str(tmp_path), fills, [])
    try:
        recs = compute_forward_htr(bot, BASE, redeem_by_cid={"0xaaa": 100.0, "0xbbb": 0.0},
                                   open_cids=set(), n_cohorts=2)
    finally:
        bot.close()
    for r in recs:
        assert r["cohort"] == cohort(r["condition_id"], 2)
    agg = cohort_htr(recs)
    # total pnl = (100-60) + (0-30) = +10
    assert round(sum(c["pnl"] for c in agg.values()), 6) == 10.0
    assert sum(c["n"] for c in agg.values()) == 2


# ── isolated store: idempotent, separate DB ───────────────────────────────────

def test_ledger_store_is_idempotent(tmp_path):
    db = os.path.join(str(tmp_path), "held_resolution_ledger.db")
    ensure_schema(db)
    ensure_schema(db)  # idempotent
    rec = {
        "condition_id": "cidA", "cohort": 1, "baseline_ts": BASE, "held_yes": 100.0, "held_no": 0.0,
        "cost_usd": 60.0, "redeem_proceeds_usd": 80.0, "held_pnl_usd": 20.0, "resolved_ts": 0.0,
        "no_redeem_flag": 0, "question": "Q", "recorded_ts": BASE,
    }
    assert upsert_ledger(db, [rec]) == 1
    # re-upsert same cid with a corrected pnl → REPLACE, row count stays 1
    rec2 = dict(rec, held_pnl_usd=15.0, redeem_proceeds_usd=75.0)
    upsert_ledger(db, [rec2])
    conn = _ro(db)
    try:
        n, pnl = conn.execute("SELECT COUNT(*), SUM(held_pnl_usd) FROM held_resolution").fetchone()
    finally:
        conn.close()
    assert n == 1
    assert pnl == 15.0  # the corrected value, not duplicated
