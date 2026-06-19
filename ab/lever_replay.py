#!/usr/bin/env python3
"""ab/lever_replay.py — counterfactual replay of EXCLUSION levers on the snapshot (read-only, FILTER not proof).

The question: would the calm-book filter (sweep / volatility) have improved net on the markets we entered?

CRITICAL honesty axis — COVERAGE + LOOKAHEAD (the reason volatility was refuted):
  * The LIVE allocator can only see a market's book history BEFORE it deploys. We mirror that with a
    strictly pre-first-fill window [first_fill - W, first_fill) (W=6h, like feature_separability.py).
  * net_analysis.py computed sweep over the FIRST 24h of book — which for fast-filling markets includes
    movement AFTER the adverse fill (LOOKAHEAD). We report BOTH:
        - sweep_pre  : pre-fill window -> honest, live-faithful, but often LOW coverage
        - sweep_24h  : first-24h-of-book -> optimistic/contaminated (what net_analysis used)
    The gap between them is the "coverage tax" the live filter actually pays.

Replay math (per lever cap c), faithful to the live fail-open filter:
    excluded = { markets with a MEASURED pre-fill feature > c }   (unmeasured markets are NEVER excluded)
    net_after = total_net - sum(net of excluded)      (removes their reward too — no free lunch)
    + breadth_after and reward_forgone reported beside net (Goodhart guard), outlier-robust.

DUMP-BASIS net (held-to-resolution excluded) -> excluding event/news losers is UNDER-credited here,
so any benefit shown is a conservative LOWER bound. Offline is a FILTER; the live soak is the proof.
"""
from __future__ import annotations

import argparse
import os
import sqlite3

try:
    from ab.net_engine import load_net, open_immutable, SWEEP_JUMP_USD, DEFAULT_SNAP
except ImportError:  # allow direct run from inside ab/
    from net_engine import load_net, open_immutable, SWEEP_JUMP_USD, DEFAULT_SNAP

PREFILL_WINDOW_H = 6.0       # mirror config RF_ALLOC_VOLATILITY_WINDOW_HOURS / feature_separability
MIN_SAMPLES = 3              # mirror feature_separability DEFAULT_MIN_SAMPLES
SWEEP_CAPS = [0.02, 0.03, 0.05]
VOL_CAPS = [0.05, 0.10, 0.15]


def _usd(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return f"${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"


def prefill_features(bot, cid, first_fill, w_sec, min_samples):
    """Pre-fill (lookahead-safe) sweep + volatility for one market. None if not measurable."""
    rows = bot.execute(
        "SELECT midpoint FROM book_snapshots WHERE condition_id=? AND ts>=? AND ts<? ORDER BY ts",
        (cid, first_fill - w_sec, first_fill)).fetchall()
    mids = [float(r[0]) for r in rows if r[0] is not None]
    n = len(mids)
    if n < min_samples:
        return dict(n_pre=n, measured=False, sweep_pre=None, vol_pre=None)
    sweep = sum(1 for i in range(1, len(mids)) if abs(mids[i] - mids[i - 1]) > SWEEP_JUMP_USD) / max(1, len(mids) - 1)
    return dict(n_pre=n, measured=True, sweep_pre=sweep, vol_pre=max(mids) - min(mids))


def replay(rows, feat_key, cap, measured_only=True):
    """Exclude markets whose feature > cap (fail-open on unmeasured). Returns counterfactual deltas."""
    total_net = sum(r["net"] for r in rows)
    total_reward = sum(r["reward"] for r in rows)
    excluded = [r for r in rows if r.get(feat_key) is not None and r[feat_key] > cap]
    net_removed = sum(r["net"] for r in excluded)
    reward_forgone = sum(r["reward"] for r in excluded)
    return dict(
        cap=cap, n_excl=len(excluded), net_removed=net_removed, reward_forgone=reward_forgone,
        net_after=total_net - net_removed, breadth_after=len(rows) - len(excluded),
        excl_cids={r["cid"] for r in excluded},
    )


def main():
    ap = argparse.ArgumentParser(description="Counterfactual lever replay on a snapshot (read-only).")
    ap.add_argument("--snap", default=DEFAULT_SNAP)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--window-hours", type=float, default=PREFILL_WINDOW_H)
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    args = ap.parse_args()

    rows, meta = load_net(args.snap, args.days)
    bot = open_immutable(os.path.join(args.snap, "bot_history.db"))
    try:
        # first fill per cid within the window (the decision anchor)
        # TRUE first fill (decision anchor); <= window_end avoids lookahead. NOT lower-bounded by
        # window_start: a market filled BEFORE the window but dumped IN it was entered earlier;
        # restricting to in-window fills misclassified those and broke the baseline.
        ff = {cid: float(ts) for cid, ts in bot.execute(
            "SELECT condition_id, MIN(ts) FROM fills WHERE ts <= ? GROUP BY condition_id",
            (meta["window_end_ts"],)) if ts}
        w_sec = args.window_hours * 3600.0
        for r in rows:
            r["sweep_24h"] = r.get("sweep")  # first-24h (lookahead-y), from net_engine
            f = ff.get(r["cid"])
            if f is None:
                r.update(n_pre=0, measured_pre=False, sweep_pre=None, vol_pre=None)
            else:
                pf = prefill_features(bot, r["cid"], f, w_sec, args.min_samples)
                r.update(measured_pre=pf["measured"], n_pre=pf["n_pre"],
                         sweep_pre=pf["sweep_pre"], vol_pre=pf["vol_pre"])
    finally:
        bot.close()

    n = len(rows)
    total_net = sum(r["net"] for r in rows)
    entered = [r for r in rows if r["cid"] in ff]      # filled >=1 time: loss-bearing + filter-gateable offline
    pure = [r for r in rows if r["cid"] not in ff]     # never filled = pure-reward bucket
    filled_net = sum(r["net"] for r in entered)
    pure_net = sum(r["net"] for r in pure)

    print(f"# ab/lever_replay — DUMP-BASIS, FILTER not proof  | window {meta['window_start']}..{meta['window_end']}")
    print(f"  universe: {n} markets  net={_usd(total_net)}")
    print(f"    filled (loss-bearing):       {len(entered):3}  net={_usd(filled_net)}   <- replay operates HERE")
    print(f"    never-filled (PURE REWARD):  {len(pure):3}  net={_usd(pure_net)}   <- 'edge is reward without fills'")
    print(f"  LIMIT: offline replay avoids filled-market losses but CANNOT net out reward forgone on the")
    print(f"  never-filled candidates a live filter would also drop -> net_after is partial/directional.\n")

    # ── [1] COVERAGE — can the live (pre-fill) filter even see these markets? ──
    cov_pre = sum(1 for r in entered if r["measured_pre"])
    cov_24h = sum(1 for r in entered if r["sweep_24h"] is not None)
    print("[1] DECISION-TIME COVERAGE (the test that refuted volatility)")
    print(f"  entered markets with MEASURABLE pre-fill book window [-{args.window_hours}h): "
          f"{cov_pre}/{len(entered)}   <- the live filter is BLIND on the rest (fail-open)")
    print(f"  entered markets with first-24h book (net_analysis basis, lookahead-y): {cov_24h}/{len(entered)}")
    npre = sorted(r["n_pre"] for r in entered)
    if npre:
        import statistics as st
        print(f"  pre-fill book rows/market: median={st.median(npre):.0f}  (0 = filled before we booked it)")

    # ── [2] SWEEP LEVER — pre-fill (honest) vs 24h (optimistic) ──
    print("\n[2] SWEEP-CAP replay  (exclude entered markets with sweep > cap; fail-open on unmeasured)")
    print(f"  baseline filled-net={_usd(filled_net)}")
    for basis, key in [("pre-fill (LIVE-faithful)", "sweep_pre"), ("first-24h (lookahead-y)", "sweep_24h")]:
        print(f"  -- basis: {basis} --")
        for cap in SWEEP_CAPS:
            d = replay(entered, key, cap)
            print(f"    cap={cap:.2f}: excl={d['n_excl']:2}  net_removed={_usd(d['net_removed']):>9}  "
                  f"reward_forgone={_usd(d['reward_forgone']):>7}  -> net_after={_usd(d['net_after']):>9}  "
                  f"breadth {len(entered)}->{d['breadth_after']}")

    # ── [3] VOL LEVER — pre-fill only (the live filter is pre-decision) ──
    print("\n[3] VOL-CAP replay (pre-fill midpoint range; exclude > cap)")
    print(f"  baseline filled-net={_usd(filled_net)}")
    for cap in VOL_CAPS:
        d = replay(entered, "vol_pre", cap)
        print(f"    cap={cap:.2f}: excl={d['n_excl']:2}  net_removed={_usd(d['net_removed']):>9}  "
              f"reward_forgone={_usd(d['reward_forgone']):>7}  -> net_after={_usd(d['net_after']):>9}  "
              f"breadth {len(entered)}->{d['breadth_after']}")

    # ── [4] OUTLIER ROBUSTNESS — one market can flip a 7-day result ──
    print("\n[4] OUTLIER ROBUSTNESS (one fat-tail market dominates at this sample size)")
    by_net = sorted(entered, key=lambda r: r["net"])
    for k in (1, 3):
        net_excl_top = sum(r["net"] for r in by_net[:k])
        print(f"  drop worst {k}: removes {_usd(net_excl_top)} of loss -> "
              f"filled-net would be {_usd(filled_net - net_excl_top)}  "
              f"(worst: {by_net[0]['q'][:34]})")

    print("\nVERDICT GUIDE: if pre-fill COVERAGE is low, the sweep/vol filter is mostly BLIND at decision "
          "time (like vol was) — the first-24h J result is then optimistic/lookahead, NOT what the live "
          "filter delivers. Held-to-resolution excluded -> exclusion benefit is a LOWER bound.")


if __name__ == "__main__":
    main()
