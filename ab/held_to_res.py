#!/usr/bin/env python3
"""ab/held_to_res.py — per-market held-to-resolution P&L (read-only).

A position bought (fills) but never dumped (no unwind) rides to resolution: each held share pays $1
(won) or $0 (lost). That P&L is invisible to dump-basis net (no unwind row). Here:

    held_to_resolution_pnl(cid) = REDEEM_proceeds(cid)  -  cost_of_held(cid)
    cost_of_held(cid)           = Σ_side max(0, fills_shares - unwinds_shares) × (fills_usd / fills_shares)

REDEEM_proceeds from the cached data-api pull (ab/fetch_redeem.py). cost from the snapshot fills/unwinds.
SCOPE: only cids that have fills IN THE SNAPSHOT — a redeem for a pre-rotation cid (DB only has fills
from 2026-05-19) has no local cost and would look like a phantom gain, so it is excluded. Per-side
(one side pays $1, the other $0). Merges are not a factor here (merges table = 0 rows, verified).
"""
from __future__ import annotations

import argparse
import json
import os

try:
    from ab.net_engine import open_immutable, DEFAULT_SNAP
except ImportError:
    from net_engine import open_immutable, DEFAULT_SNAP


def load_redeem(snap_dir: str) -> dict[str, float]:
    """{cid: total redemption proceeds usdcSize} from the cached pull."""
    path = os.path.join(snap_dir, "redeem_activity.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"run `python3 -m ab.fetch_redeem` first — missing {path}")
    with open(path) as f:
        data = json.load(f)
    by_cid: dict[str, float] = {}
    for it in data:
        c = it.get("conditionId", "")
        if c:
            by_cid[c] = by_cid.get(c, 0.0) + float(it.get("usdcSize", 0) or 0)
    return by_cid


def cost_of_held(bot) -> dict[str, float]:
    """{cid: cost basis of shares held to resolution} from snapshot fills/unwinds, per-side."""
    fills: dict[tuple, tuple] = {}
    for cid, side, sh, usd in bot.execute(
            "SELECT condition_id, side, SUM(shares), SUM(usd_value) FROM fills GROUP BY condition_id, side"):
        fills[(cid, side)] = (float(sh or 0), float(usd or 0))
    unw: dict[tuple, float] = {}
    for cid, side, sh in bot.execute(
            "SELECT condition_id, side, SUM(shares) FROM unwinds GROUP BY condition_id, side"):
        unw[(cid, side)] = float(sh or 0)
    cost: dict[str, float] = {}
    for (cid, side), (fsh, fusd) in fills.items():
        if fsh <= 0:
            continue
        held = max(0.0, fsh - unw.get((cid, side), 0.0))
        if held > 0.5:
            cost[cid] = cost.get(cid, 0.0) + held * (fusd / fsh)
    return cost


def load_held_to_resolution(snap_dir: str = DEFAULT_SNAP):
    """Return (htr {cid: pnl}, cost {cid: cost_held}, redeem {cid: proceeds}, missing set).

    htr defined only for cids with held shares in the snapshot. `missing` = held cids with NO redeem
    event (proceeds default 0 -> treated as total loss; flagged, since the all-cash bot should have
    redeemed everything)."""
    redeem = load_redeem(snap_dir)
    bot = open_immutable(os.path.join(snap_dir, "bot_history.db"))
    try:
        cost = cost_of_held(bot)
    finally:
        bot.close()
    htr, missing = {}, set()
    for cid, c in cost.items():
        if cid not in redeem:
            missing.add(cid)
        htr[cid] = redeem.get(cid, 0.0) - c
    return htr, cost, redeem, missing


def _usd(v):
    return f"${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"


def main():
    ap = argparse.ArgumentParser(description="Per-market held-to-resolution P&L (read-only).")
    ap.add_argument("--snap", default=DEFAULT_SNAP)
    args = ap.parse_args()

    htr, cost, redeem, missing = load_held_to_resolution(args.snap)
    total = sum(htr.values())
    losers = {c: v for c, v in htr.items() if v < 0}
    winners = {c: v for c, v in htr.items() if v > 0}

    # question text for readability
    bot = open_immutable(os.path.join(args.snap, "bot_history.db"))
    try:
        q = {cid: (que or "")[:42] for cid, que in bot.execute(
            "SELECT condition_id, MAX(question) FROM fills GROUP BY condition_id")}
    finally:
        bot.close()

    print("# ab/held_to_res — per-market held-to-resolution P&L (REDEEM proceeds - cost of held shares)")
    print(f"  markets with held shares (cost basis in snapshot): {len(cost)}")
    print(f"  TOTAL held-to-resolution P&L: {_usd(total)}   (losers {len(losers)} = {_usd(sum(losers.values()))}, "
          f"winners {len(winners)} = {_usd(sum(winners.values()))})")
    print(f"  cost basis of all held shares: {_usd(sum(cost.values()))}   redeem proceeds on held cids: "
          f"{_usd(sum(redeem.get(c, 0.0) for c in cost))}")
    if missing:
        print(f"  ** {len(missing)} held cids had NO redeem event (treated as $0 proceeds = total loss) — "
              f"verify these resolved **")
    print(f"\n  *** UNRELIABLE MAGNITUDE — DO NOT USE THIS NUMBER AS held-to-resolution P&L. ***")
    print(f"  Computed gross = {_usd(total)}, but this is confounded (verified 2026-06-19):")
    print(f"    1) DB-rotation seam: fills start 2026-05-18; redeems/positions run back to March.")
    print(f"    2) deposits are NOT in the data-api feed (DEPOSIT type=0), so winning-side proceeds")
    print(f"       can't be matched and external capital can't be subtracted.")
    print(f"    3) likely phantom-inflated fills (net-held {_usd(sum(cost.values()))} >> the real ~-$215 drawdown).")
    print(f"  USE THIS ONLY for the IDENTITY of held positions (directional), not the dollar magnitude.")
    print("\n  largest held-to-resolution LOSERS:")
    for cid, v in sorted(losers.items(), key=lambda kv: kv[1])[:12]:
        print(f"    {_usd(v):>9}  cost={_usd(cost.get(cid, 0)):>8} redeem={_usd(redeem.get(cid, 0)):>8}  {q.get(cid, cid[:20])}")


if __name__ == "__main__":
    main()
