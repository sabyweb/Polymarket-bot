"""Unit tests for ab/promotion_gate.py — pure-function tests for the consecutive-reconciling-day logic.

Read-only; no DB / live state. Covers the trailing-run semantics (skip pending tail, break on
out-of-band or collector gap) and the per-day status classification.
"""
from __future__ import annotations

from ab.promotion_gate import trailing_run, day_statuses


def test_trailing_run_counts_recent_in_band():
    per = [("d1", "in"), ("d2", "in"), ("d3", "in")]
    assert trailing_run(per) == (3, None, None)


def test_trailing_run_breaks_on_out_of_band():
    per = [("d1", "in"), ("d2", "out"), ("d3", "in"), ("d4", "in")]
    assert trailing_run(per) == (2, "d2", "out")


def test_trailing_run_skips_trailing_pending_tail():
    # the unsettled +1-credit tail must NOT count as a break
    per = [("d1", "in"), ("d2", "in"), ("d3", "pending"), ("d4", "pending")]
    assert trailing_run(per) == (2, None, None)


def test_trailing_run_collector_gap_breaks():
    per = [("d1", "in"), ("d2", "gap"), ("d3", "in")]
    assert trailing_run(per) == (1, "d2", "gap")


def test_trailing_run_empty_and_all_pending():
    assert trailing_run([]) == (0, None, None)
    assert trailing_run([("d1", "pending")]) == (0, None, None)


def test_day_statuses_classifies_band_gap_pending():
    rs = {"2026-06-10": 6.0, "2026-06-11": 9.0, "2026-06-13": 3.0}      # 06-12 missing -> gap
    agg = {"2026-06-11": 6.0, "2026-06-12": 9.0}                         # no 06-14 -> 06-13 pending
    rows = day_statuses(rs, agg, 0.7, 1.3)
    by_date = {d: s for d, s, *_ in rows}
    assert by_date["2026-06-10"] == "in"        # 6.0/6.0 = 1.00
    assert by_date["2026-06-11"] == "in"        # 9.0/9.0 = 1.00
    assert by_date["2026-06-12"] == "gap"       # no RS that day
    assert by_date["2026-06-13"] == "pending"   # RS present, AGG[06-14] absent
    # span is contiguous min..max even across the gap
    assert [d for d, *_ in rows] == ["2026-06-10", "2026-06-11", "2026-06-12", "2026-06-13"]


def test_day_statuses_out_of_band():
    rs = {"2026-06-10": 6.0}
    agg = {"2026-06-11": 12.0}     # 6/12 = 0.5 < 0.7
    rows = day_statuses(rs, agg, 0.7, 1.3)
    assert rows[0][1] == "out"
