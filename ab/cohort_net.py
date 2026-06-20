#!/usr/bin/env python3
"""ab/cohort_net.py — per-cohort NET/$ on a snapshot (read-only).

THE KEYSTONE A/B MEASUREMENT. Makes per-cohort net/$ REAL by sourcing per-market reward from
reward_snapshots.db via the verified ab/net_engine spine (canonical latest-per-(date,cid) reward +
capture band + data-anchored window + immutable=1), instead of the empty daily_reward_cache
per-market path that left ab_cohort_metrics.py printing net/$ = "n/a".

Honesty rules baked in (the offline-measurement contract):
  * DUMP-BASIS net = reward + unwinds.pnl. Held-to-resolution is NOT included -> true net is WORSE
    (a held position has no unwind row). Stated on every run.
  * dump_loss/$ is EXACT (estimate-free) -> the PRIMARY comparator. net/$ is a BAND because per-market
    reward under-captures the data-api aggregate (capture ratio carried from net_engine.meta); shown
    raw (pessimistic) AND capture-adjusted (assumes capture is uniform across cohorts — stated).
  * On data generated with the A/B experiment OFF (window entirely before go-live), cohorts are a
    RANDOM partition of ONE policy -> a NOISE FLOOR, NOT a treatment effect. Banner shown loudly.
  * Fat-tail robustness: one Hormuz-class market can flip a 7-day result, so the cohort ordering is
    re-checked dropping the worst-1 and worst-3 markets by net (per ab/lever_replay).

Read-only, non-behavioral. Reuses ab/net_engine.load_net + ab/cohort.cohort (no live state touched).

Usage: python3 -m ab.cohort_net --snap snapshots/2026-06-19 --days 7 --cohorts 2
                                 [--experiment-start 2026-06-19] [--min-markets 8]
"""
from __future__ import annotations

import argparse

from ab.net_engine import load_net, DEFAULT_SNAP
from ab.cohort import cohort

COHORT_NAMES = {0: "C0 baseline", 1: "C1 calmer-pond", 2: "C2 real-time-reaction"}
GO_LIVE_DEFAULT = "2026-06-19"   # recorded A/B go-live (ground_rules.md change-log 2026-06-19)
MIN_MARKETS_DEFAULT = 8          # below this a cohort is too thin to compare (RT-2)
GOODHART_FLOOR = 0.5             # challenger breadth/reward must stay >= this x baseline (anti-do-nothing)


def _usd(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return f"${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"


def cohort_breakdown(rows: list[dict], n: int) -> dict[int, dict]:
    """Group net-engine rows into per-cohort aggregates (read-only, pure)."""
    out: dict[int, dict] = {}
    for r in rows:
        k = cohort(r["cid"], n)
        b = out.setdefault(k, dict(cohort=k, rows=[], mkts=0, fills=0,
                                   cap=0.0, dump=0.0, reward=0.0, net=0.0))
        b["rows"].append(r)
        b["mkts"] += 1
        b["fills"] += int(r.get("n_fill") or 0)
        b["cap"] += float(r.get("peak") or 0.0)
        b["dump"] += float(r.get("dump") or 0.0)
        b["reward"] += float(r.get("reward") or 0.0)
        b["net"] += float(r.get("net") or 0.0)
    return out


def dump_loss_per_dollar(subset: list[dict]) -> float:
    """EXACT dump P&L per $ capital over a subset of markets (estimate-free)."""
    cap = sum(float(r.get("peak") or 0.0) for r in subset)
    dump = sum(float(r.get("dump") or 0.0) for r in subset)
    return (dump / cap) if cap else 0.0


def drop_worst(subset: list[dict], k: int) -> list[dict]:
    """Drop the k most-negative markets by net (fat-tail robustness, per ab/lever_replay)."""
    if k <= 0:
        return list(subset)
    return sorted(subset, key=lambda r: float(r.get("net") or 0.0))[k:]


def _mark(ok) -> str:
    return "PASS" if ok is True else ("FAIL" if ok is False else "n/a")


def main():
    ap = argparse.ArgumentParser(description="Per-cohort net/$ on a snapshot (read-only).")
    ap.add_argument("--snap", default=DEFAULT_SNAP, help="snapshot dir (bot_history.db + reward_snapshots.db)")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--cohorts", type=int, default=2)
    ap.add_argument("--experiment-start", default=GO_LIVE_DEFAULT,
                    help="A/B go-live date; a window entirely before it is a NOISE FLOOR, not a treatment")
    ap.add_argument("--min-markets", type=int, default=MIN_MARKETS_DEFAULT)
    args = ap.parse_args()

    rows, meta = load_net(args.snap, args.days)
    bd = cohort_breakdown(rows, args.cohorts)
    cap_shift = meta.get("capture_shift")   # per-market / aggregate(+1day); None if no aggregate

    print(f"# ab/cohort_net — per-cohort NET/$ (read-only, DUMP-BASIS + reward band)")
    print(f"  snapshot: {args.snap}")
    print(f"  window:   {meta['window_start']} -> {meta['window_end']}  ({meta['days']}d, anchored to DATA max ts)")
    print(f"  cohorts:  {args.cohorts}  (sha1(cid)%{args.cohorts}, matches the live allocator)")
    print(f"  reward capture (per-market vs data-api aggregate): same={meta.get('capture_same')}  "
          f"+1shift={cap_shift}  (net/$ banded on this)")

    # Pre-experiment guard: window entirely before go-live => random partition of one policy.
    pre_experiment = meta["window_end"] < args.experiment_start
    if pre_experiment:
        print(f"\n  *** PRE-EXPERIMENT WINDOW (ends {meta['window_end']} < go-live {args.experiment_start}):")
        print(f"      cohorts are a RANDOM partition of ONE policy -> NOISE FLOOR ONLY, NOT a treatment")
        print(f"      effect. Use this run to validate the pipeline, never to declare a winner. ***")

    print(f"\n  cohort               mkts  fills   capital    dump_pnl   dump_loss/$   reward(RS)"
          f"   net/$ raw   net/$ capadj")
    for co in sorted(bd):
        a = bd[co]
        name = COHORT_NAMES.get(co, f"cohort {co}")
        dlpd = (a["dump"] / a["cap"]) if a["cap"] else 0.0
        net_raw = (a["net"] / a["cap"]) if a["cap"] else 0.0
        if cap_shift and a["cap"]:
            net_adj = (a["reward"] / cap_shift + a["dump"]) / a["cap"]
            adj_str = f"{net_adj:+.4f}"
        else:
            adj_str = "   n/a"
        print(f"  {name:18} {a['mkts']:5} {a['fills']:6}  {_usd(a['cap']):>9}  {_usd(a['dump']):>9}  "
              f"{dlpd:+.4f}     {_usd(a['reward']):>8}   {net_raw:+.4f}    {adj_str}")

    # ── Fat-tail robustness: re-check the C0-vs-challenger ordering dropping worst-1 / worst-3.
    print(f"\n  -- robustness: dump_loss/$ at full / drop-worst1 / drop-worst3 (fat-tail check) --")
    dl = {}
    for co in sorted(bd):
        subset = bd[co]["rows"]
        full, d1, d3 = (dump_loss_per_dollar(subset),
                        dump_loss_per_dollar(drop_worst(subset, 1)),
                        dump_loss_per_dollar(drop_worst(subset, 3)))
        dl[co] = (full, d1, d3)
        print(f"    {COHORT_NAMES.get(co, 'cohort %d' % co):18} {full:+.4f} / {d1:+.4f} / {d3:+.4f}")

    # ── Promotion checklist (B1; a HUMAN applies it, recorded in ground_rules — never automated).
    base = bd.get(0)
    print(f"\n  -- promotion checklist (B1; human applies + records; Stage-1 autonomy) --")
    if pre_experiment:
        print(f"    SKIPPED — pre-experiment noise floor (see banner). Re-run on a post-go-live snapshot.")
    elif base is None:
        print(f"    no C0 baseline cohort in window — cannot compare.")
    else:
        for co in sorted(bd):
            if co == 0:
                continue
            ch = bd[co]
            name = COHORT_NAMES.get(co, f"cohort {co}")
            base_dl = base["dump"] / base["cap"] if base["cap"] else 0.0
            ch_dl = ch["dump"] / ch["cap"] if ch["cap"] else 0.0
            base_net = base["net"] / base["cap"] if base["cap"] else 0.0
            ch_net = ch["net"] / ch["cap"] if ch["cap"] else 0.0
            primary = ch_dl >= base_dl                                   # exact: less dump loss / $
            secondary = (ch_net >= base_net) and (ch_net > 0)            # banded net/$ >= baseline & >0
            robust = all(dl[co][i] >= dl[0][i] for i in range(3))        # ordering holds dropping fat-tail
            min_sample = (ch["mkts"] >= args.min_markets) and (base["mkts"] >= args.min_markets)
            anti_good = (ch["mkts"] >= GOODHART_FLOOR * base["mkts"]) and \
                        (ch["reward"] >= GOODHART_FLOOR * base["reward"])
            print(f"    {name} vs C0:")
            print(f"      [{_mark(primary)}]  primary  (exact)  dump_loss/$ {ch_dl:+.4f} >= {base_dl:+.4f}")
            print(f"      [{_mark(secondary)}]  secondary (band) net/$ {ch_net:+.4f} >= {base_net:+.4f} and > 0")
            print(f"      [{_mark(robust)}]  outlier-robust   ordering holds at drop-1 and drop-3")
            print(f"      [{_mark(min_sample)}]  min-sample       >= {args.min_markets} markets/cohort "
                  f"(C0={base['mkts']}, {name.split()[0]}={ch['mkts']})")
            print(f"      [{_mark(anti_good)}]  anti-Goodhart    breadth & reward not collapsed vs C0")
            print(f"      [ MANUAL ]  capture-gate     >= 7 consecutive reconciling days "
                  f"(run: python3 -m ab.promotion_gate)")
            print(f"      [ MANUAL ]  provisional      held-to-resolution tail (A4) can RETROACT-invalidate")

    print(f"\n  NOTE: net/$ is DUMP-BASIS (held-to-resolution excluded -> true net is worse). "
          f"dump_loss/$ is the exact, estimate-free primary comparator; net/$ raw uses per-market "
          f"reward (captures ~{cap_shift} of credited), net/$ capadj assumes uniform capture across "
          f"cohorts. A challenger is promotable only with ALL boxes PASS + the two MANUAL gates met.")


if __name__ == "__main__":
    main()
