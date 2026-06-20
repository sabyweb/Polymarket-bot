"""Unit tests for ab/cohort_net.py — pure-function tests (no snapshot needed) + a snapshot smoke test.

Read-only measurement tool; these tests assert the per-cohort grouping, the exact dump_loss/$ math,
and the fat-tail drop-worst helper behave correctly. They do NOT touch live state.
"""
from __future__ import annotations

import os

import pytest

from ab.cohort import cohort
from ab.cohort_net import cohort_breakdown, dump_loss_per_dollar, drop_worst


def _row(cid, reward, dump, peak, n_fill):
    return dict(cid=cid, reward=reward, dump=dump, net=reward + dump, peak=peak, n_fill=n_fill)


def test_cohort_breakdown_partitions_and_sums():
    rows = [_row(f"0x{i:040x}", reward=1.0, dump=-0.5, peak=10.0, n_fill=2) for i in range(20)]
    bd = cohort_breakdown(rows, 2)
    # every row is accounted for exactly once, totals conserved
    assert sum(b["mkts"] for b in bd.values()) == 20
    assert sum(b["fills"] for b in bd.values()) == 40
    assert abs(sum(b["cap"] for b in bd.values()) - 200.0) < 1e-9
    assert abs(sum(b["net"] for b in bd.values()) - 20 * 0.5) < 1e-9
    # each row landed in exactly the cohort the pure assignment fn assigns (parity with allocator)
    for b in bd.values():
        for r in b["rows"]:
            assert cohort(r["cid"], 2) == b["cohort"]


def test_cohort_breakdown_empty():
    assert cohort_breakdown([], 2) == {}


def test_dump_loss_per_dollar_exact_and_safe():
    rows = [_row("a", 0, -2.0, 10.0, 1), _row("b", 0, -3.0, 10.0, 1)]
    assert abs(dump_loss_per_dollar(rows) - (-5.0 / 20.0)) < 1e-9
    # no capital -> 0.0, never divide-by-zero
    assert dump_loss_per_dollar([]) == 0.0
    assert dump_loss_per_dollar([_row("z", 0, -1.0, 0.0, 0)]) == 0.0


def test_drop_worst_removes_most_negative_by_net():
    rows = [_row("a", 0, -10.0, 1, 1), _row("b", 0, -1.0, 1, 1), _row("c", 0, 5.0, 1, 1)]
    assert {r["cid"] for r in drop_worst(rows, 1)} == {"b", "c"}
    assert {r["cid"] for r in drop_worst(rows, 2)} == {"c"}
    assert drop_worst(rows, 0) == rows           # k=0 is a no-op
    assert drop_worst(rows, 99) == []            # over-drop empties, never raises


_SNAP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "snapshots", "2026-06-19")


@pytest.mark.skipif(not os.path.exists(os.path.join(_SNAP, "reward_snapshots.db")),
                    reason="no local snapshot to smoke-test against")
def test_snapshot_smoke():
    from ab.net_engine import load_net
    rows, meta = load_net(_SNAP, 7)
    bd = cohort_breakdown(rows, 2)
    assert sum(b["mkts"] for b in bd.values()) == len(rows)
    # net is dump-basis: reward + dump per row, conserved through the breakdown
    assert abs(sum(b["net"] for b in bd.values()) - sum(r["net"] for r in rows)) < 1e-6
