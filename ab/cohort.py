"""ab/cohort.py — A/B cohort assignment, byte-identical to simple_allocator._ab_cohort.

    cohort(cid, n) = int(sha1(cid.utf-8).hexdigest(), 16) % n      (0 when n <= 1)

Cohort is a PURE function of condition_id (no stored column), so the offline analyzer recomputes the
exact same partition the live allocator used. The parity test (tests/test_ab_cohort_parity.py) asserts
this matches simple_allocator._ab_cohort for many cids and counts — if the live function ever changes,
the test fails loudly (a silent drift would make every cohort comparison wrong).
"""
from __future__ import annotations

import hashlib


def cohort(cid: str, n: int) -> int:
    """Stable pseudo-random cohort for a market. Mirrors simple_allocator._ab_cohort exactly."""
    try:
        n = int(n or 1)
    except (TypeError, ValueError):
        n = 1
    if n <= 1:
        return 0
    return int(hashlib.sha1(cid.encode("utf-8")).hexdigest(), 16) % n


def aggregate(rows: list[dict], n: int, net_key: str = "net") -> dict[int, dict]:
    """Group net-engine rows by cohort -> per-cohort totals (net/reward/dump/breadth).

    NOTE: on data generated with the A/B experiment OFF, this is a RANDOM partition of one policy —
    use it for pipeline validation + a variance/noise floor, NOT a treatment effect (see lever_replay
    for the offline signal; the true cohort treatment effect only exists live).
    """
    out: dict[int, dict] = {}
    for r in rows:
        k = cohort(r["cid"], n)
        b = out.setdefault(k, dict(cohort=k, n=0, reward=0.0, dump=0.0, net=0.0))
        b["n"] += 1
        b["reward"] += float(r.get("reward", 0.0) or 0.0)
        b["dump"] += float(r.get("dump", 0.0) or 0.0)
        b["net"] += float(r.get(net_key, 0.0) or 0.0)
    return out
