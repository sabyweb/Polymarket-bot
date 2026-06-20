#!/usr/bin/env python3
"""ab/promotion_gate.py — is the A/B promotion gate OPEN? (read-only)

The promotion precondition (B1 / capture-ratio gate): per-market reward (reward_snapshots) must
reconcile to the data-api aggregate (daily_reward_cache.__TOTAL__) within [lo, hi] = [0.7, 1.3] for
>= N (default 7) CONSECUTIVE days, using the verified +1-day earned-vs-credited shift (reward EARNED
on day D is CREDITED on day D+1). A missing day (collector GAP) or an OUT-of-band day BREAKS the run.
A trailing day whose +1 credit hasn't settled yet is PENDING (the unsettled tail) and is skipped, not
counted as a break.

Until this gate is OPEN, per-cohort net/$ rests on un-reconciled reward and NO promotion is allowed
(rule #1: never promote on a number we cannot trust). This turns the ">=7-day" precondition into a
one-command answer instead of an eyeballed judgement, and surfaces collector gaps explicitly.

Read-only, non-behavioral. Snapshot (immutable=1) or live reward_snapshots.db+bot_history.db.

Usage: python3 -m ab.promotion_gate --snap snapshots/2026-06-19 [--need 7] [--lo 0.7 --hi 1.3]
"""
from __future__ import annotations

import argparse
import os
from datetime import date, timedelta

from ab.net_engine import DEFAULT_SNAP, open_immutable, open_ro

LO_DEFAULT, HI_DEFAULT, NEED_DEFAULT = 0.7, 1.3, 7


def rs_reward_by_date(rew) -> dict:
    """{date: per-market reward sum (latest snapshot per (date,cid))} from reward_snapshots."""
    q = ("WITH latest AS (SELECT date, condition_id, earnings_usd, "
         "ROW_NUMBER() OVER (PARTITION BY date, condition_id ORDER BY ts DESC) rn "
         "FROM reward_snapshots) "
         "SELECT date, SUM(earnings_usd) FROM latest WHERE rn = 1 GROUP BY date")
    return {d: float(v or 0) for d, v in rew.execute(q)}


def agg_reward_by_date(bot) -> dict:
    """{date: data-api aggregate reward} from daily_reward_cache __TOTAL__ sentinel."""
    return {d: float(v or 0) for d, v in bot.execute(
        "SELECT date, reward_earned FROM daily_reward_cache WHERE condition_id = '__TOTAL__'")}


def day_statuses(rs: dict, agg: dict, lo: float, hi: float) -> list[tuple]:
    """For each calendar day in [min(rs)..max(rs)], classify reconciliation status (ascending).

    status in {'in','out','gap','pending'}:
      gap     = no reward_snapshots data that day (collector gap)
      pending = RS present but the +1-day aggregate credit hasn't settled yet (unsettled tail)
      in/out  = ratio = RS[D] / AGG[D+1] is / isn't within [lo,hi]
    Returns list of (date_str, status, ratio_or_None, rs_val, agg_next_or_None).
    """
    if not rs:
        return []
    d0, d1 = min(rs), max(rs)
    cur, end = date.fromisoformat(d0), date.fromisoformat(d1)
    out = []
    while cur <= end:
        ds = cur.isoformat()
        nxt = (cur + timedelta(days=1)).isoformat()
        rs_val = rs.get(ds)
        agg_next = agg.get(nxt)
        if rs_val is None:
            out.append((ds, "gap", None, None, None))
        elif agg_next is None or agg_next == 0:
            out.append((ds, "pending", None, rs_val, agg_next))
        else:
            ratio = rs_val / agg_next
            out.append((ds, "in" if lo <= ratio <= hi else "out", ratio, rs_val, agg_next))
        cur += timedelta(days=1)
    return out


def trailing_run(per_day: list[tuple]) -> tuple:
    """Trailing consecutive 'in' run (most recent). Skip a trailing 'pending' tail (unsettled, not a
    break); stop at the first 'out'/'gap'. Returns (run_len, break_date_or_None, break_status_or_None).
    """
    items = list(per_day)
    while items and items[-1][1] == "pending":
        items.pop()
    run = 0
    for row in reversed(items):
        if row[1] == "in":
            run += 1
        else:
            return run, row[0], row[1]
    return run, None, None


def main():
    ap = argparse.ArgumentParser(description="A/B promotion gate: consecutive reconciling days (read-only).")
    ap.add_argument("--snap", default=DEFAULT_SNAP, help="snapshot dir with bot_history.db + reward_snapshots.db")
    ap.add_argument("--need", type=int, default=NEED_DEFAULT, help="consecutive reconciling days required")
    ap.add_argument("--lo", type=float, default=LO_DEFAULT)
    ap.add_argument("--hi", type=float, default=HI_DEFAULT)
    args = ap.parse_args()

    rew = open_ro(os.path.join(args.snap, "reward_snapshots.db"))
    bot = open_immutable(os.path.join(args.snap, "bot_history.db"))
    try:
        rs = rs_reward_by_date(rew)
        agg = agg_reward_by_date(bot)
    finally:
        rew.close()
        bot.close()

    per_day = day_statuses(rs, agg, args.lo, args.hi)
    run, break_at, break_status = trailing_run(per_day)

    print(f"# ab/promotion_gate — capture-ratio reconciliation (read-only)")
    print(f"  snapshot: {args.snap}")
    print(f"  rule: RS[D]/AGG[D+1] in [{args.lo}, {args.hi}] for >= {args.need} consecutive days "
          f"(+1day earned-vs-credited shift)\n")
    print(f"  date         RS_reward   AGG[D+1]   ratio   status")
    for ds, status, ratio, rs_val, agg_next in per_day:
        rstr = f"{ratio:.2f}" if ratio is not None else "  -"
        rsv = f"${rs_val:,.2f}" if rs_val is not None else "    -"
        agv = f"${agg_next:,.2f}" if agg_next is not None else "    -"
        flag = {"in": "ok", "out": "OUT-OF-BAND", "gap": "GAP (no collector data)",
                "pending": "pending (+1 credit unsettled)"}[status]
        print(f"  {ds}   {rsv:>9}  {agv:>9}   {rstr:>5}   {flag}")

    gate_open = run >= args.need
    print(f"\n  trailing consecutive reconciling days: {run}  (need >= {args.need})")
    if break_at is not None:
        print(f"  run breaks at {break_at}: {break_status}")
    print(f"\n  PROMOTION GATE: {'OPEN — reconciliation precondition met' if gate_open else 'CLOSED — do NOT promote'}")
    if not gate_open:
        print(f"  (per-cohort net/$ still rests on un-reconciled reward; fix the break / wait for more days)")


if __name__ == "__main__":
    main()
