#!/usr/bin/env python3
"""ab/net_reconcile.py — deposit-aware net reconciliation (read-only).  [Phase A1]

The held-to-resolution investigation proved Δportfolio is confounded by DEPOSITS that appear in NO
queryable feed (data-api DEPOSIT type = 0 events). This module makes that confound EXPLICIT instead
of fatal, and is the forward net signal the live A/B + the Halt-Doctor diagnosis both consume.

Decomposition (all anchored to a baseline, deposits handled honestly):
    farming_pnl   = Δportfolio  -  Σ deposits            (deposits = external capital, NOT P&L)
    residual_held = farming_pnl - reward - dump_pnl       (held-to-resolution + fees + MTM)

Component trust (stated, per the no-guesswork rule):
  * reward      VERIFIED  (data-api __TOTAL__ aggregate; per-market reward_snapshots is ~85% capture)
  * dump_pnl    VERIFIED  (unwinds.pnl)
  * deposits    DETECTED  (cash jumps not explained by a same-day redeem/reward => CANDIDATE deposit,
                           flagged for operator confirmation; they are in no feed, so this is a
                           heuristic, never asserted as fact)
  * residual    DERIVED   (only as clean as the deposit detection)

FORWARD (live) mode: from a recorded resume baseline with deposits FROZEN (=0), residual_held is
clean — that is the whole point of resuming with a fixed baseline + no injections.

Read-only: opens the snapshot DB immutable=1; never writes bot state.
"""
from __future__ import annotations

import argparse
import json
import os

try:
    from ab.net_engine import open_immutable, load_net, DEFAULT_SNAP
except ImportError:
    from net_engine import open_immutable, load_net, DEFAULT_SNAP

DEPOSIT_JUMP_MIN_USD = 50.0   # cash jumps below this aren't treated as deposit candidates
REDEEM_MATCH_TOL = 0.5        # a cash jump within this $ of a same/prior-day redeem is "explained"


def _usd(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return f"${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"


def _redeem_by_day(snap_dir):
    path = os.path.join(snap_dir, "redeem_activity.json")
    if not os.path.exists(path):
        return {}
    import datetime
    out = {}
    for it in json.load(open(path)):
        ts = it.get("timestamp")
        if not ts:
            continue
        d = datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).strftime("%Y-%m-%d")
        out[d] = out.get(d, 0.0) + float(it.get("usdcSize", 0) or 0)
    return out


def detect_deposits(bot, redeem_by_day):
    """Detect CANDIDATE deposits = large positive cash jumps not explained by a redeem ±1 day.

    Returns list of dicts {date, cash_after, jump, explained_by}. Heuristic + flagged (deposits are
    in no feed). reward (~$10/day) is far below DEPOSIT_JUMP_MIN_USD so it never reads as a deposit.
    """
    import datetime
    rows = bot.execute(
        "SELECT ts, exchange_balance FROM portfolio_snapshots ORDER BY ts").fetchall()
    cands = []
    prev = None
    for ts, cash in rows:
        cash = float(cash or 0)
        if prev is not None:
            jump = cash - prev
            if jump >= DEPOSIT_JUMP_MIN_USD:
                d = datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).strftime("%Y-%m-%d")
                dprev = datetime.datetime.fromtimestamp(float(ts) - 86400, datetime.timezone.utc).strftime("%Y-%m-%d")
                redeem_near = redeem_by_day.get(d, 0.0) + redeem_by_day.get(dprev, 0.0)
                explained = abs(jump - redeem_near) <= max(REDEEM_MATCH_TOL, 0.25 * jump)
                cands.append(dict(date=d, cash_after=round(cash, 2), jump=round(jump, 2),
                                  redeem_near=round(redeem_near, 2),
                                  explained_by=("redeem" if explained else "DEPOSIT?")))
        prev = cash
    return cands


def reconcile(snap_dir=DEFAULT_SNAP, days=7, baseline_cash=None, known_deposits=0.0, baseline_ts=None):
    rows, meta = load_net(snap_dir, days)
    reward = sum(r["reward"] for r in rows)        # per-market (under-captures)
    dump = sum(r["dump"] for r in rows)
    bot = open_immutable(os.path.join(snap_dir, "bot_history.db"))
    try:
        # full-period portfolio endpoints + reward aggregate, for the deposit-confound picture
        p0 = bot.execute("SELECT total_value FROM portfolio_snapshots ORDER BY ts ASC LIMIT 1").fetchone()
        p1 = bot.execute("SELECT total_value FROM portfolio_snapshots ORDER BY ts DESC LIMIT 1").fetchone()
        v0 = float(p0[0]) if p0 else None
        v1 = float(p1[0]) if p1 else None
        agg_reward = float(bot.execute(
            "SELECT COALESCE(SUM(reward_earned),0) FROM daily_reward_cache WHERE condition_id='__TOTAL__'"
        ).fetchone()[0])
        dump_all = float(bot.execute("SELECT COALESCE(SUM(pnl),0) FROM unwinds").fetchone()[0])
        cands = detect_deposits(bot, _redeem_by_day(snap_dir))
        # FORWARD components: reward + dump SINCE the baseline ts (default = snapshot's latest ts =>
        # degenerate ~0 on a static snapshot; real + accumulating when run live from a resume baseline).
        import datetime as _dt
        bts = baseline_ts
        if bts is None:
            row = bot.execute("SELECT MAX(ts) FROM portfolio_snapshots").fetchone()
            bts = float(row[0]) if row and row[0] else None
        reward_since = dump_since = 0.0
        if bts is not None:
            bdate = _dt.datetime.fromtimestamp(bts, _dt.timezone.utc).strftime("%Y-%m-%d")
            reward_since = float(bot.execute(
                "SELECT COALESCE(SUM(reward_earned),0) FROM daily_reward_cache "
                "WHERE condition_id='__TOTAL__' AND date > ?", (bdate,)).fetchone()[0])
            dump_since = float(bot.execute(
                "SELECT COALESCE(SUM(pnl),0) FROM unwinds WHERE ts > ?", (bts,)).fetchone()[0])
    finally:
        bot.close()
    detected_dep = sum(c["jump"] for c in cands if c["explained_by"] == "DEPOSIT?")
    return dict(meta=meta, window_reward=reward, window_dump=dump,
                v0=v0, v1=v1, d_portfolio=(v1 - v0 if v0 is not None and v1 is not None else None),
                agg_reward_all=agg_reward, dump_all=dump_all,
                reward_since=reward_since, dump_since=dump_since, baseline_ts=bts,
                deposit_candidates=cands, detected_deposits=detected_dep,
                baseline_cash=baseline_cash, known_deposits=known_deposits)


def main():
    ap = argparse.ArgumentParser(description="Deposit-aware net reconciliation (read-only).")
    ap.add_argument("--snap", default=DEFAULT_SNAP)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--baseline-cash", type=float, default=None,
                    help="forward mode: known resume cash baseline (deposits frozen => clean residual)")
    ap.add_argument("--deposits", type=float, default=0.0,
                    help="forward mode: operator-confirmed deposits since baseline (default 0 = frozen)")
    args = ap.parse_args()
    R = reconcile(args.snap, args.days, args.baseline_cash)
    m = R["meta"]

    print("# ab/net_reconcile — deposit-aware net (read-only)")
    print(f"  reward window {m['window_start']}..{m['window_end']} (per-market): {_usd(R['window_reward'])}  "
          f"dump: {_usd(R['window_dump'])}  -> dump-basis window net: {_usd(R['window_reward'] + R['window_dump'])}")

    print("\n  -- full-period portfolio decomposition (shows WHY held-to-resolution is confounded) --")
    print(f"  portfolio: {_usd(R['v0'])} -> {_usd(R['v1'])}   Δ = {_usd(R['d_portfolio'])}")
    print(f"  data-api aggregate reward (all): {_usd(R['agg_reward_all'])}   dump (all): {_usd(R['dump_all'])}")
    big = [c for c in R["deposit_candidates"] if c["explained_by"] == "DEPOSIT?" and c["jump"] >= 150]
    print(f"  LIKELY-deposit jumps (>= $150, UNCONFIRMED): {_usd(sum(c['jump'] for c in big))} across {len(big)}")

    if args.baseline_cash is not None:
        # FORWARD / explicit mode: operator supplies baseline + confirmed deposits, components are
        # SINCE-baseline -> CLEAN residual. On a static snapshot the since-baseline window is ~empty
        # (degenerate ~0); live, it accumulates from the resume point.
        farming = (R["v1"] - args.baseline_cash) - args.deposits
        residual = farming - R["reward_since"] - R["dump_since"]
        degenerate = abs(R["reward_since"]) < 1e-6 and abs(R["dump_since"]) < 1e-6
        print(f"\n  [FORWARD] farming = (portfolio {_usd(R['v1'])} - baseline {_usd(args.baseline_cash)}) "
              f"- deposits {_usd(args.deposits)} = {_usd(farming)}")
        print(f"  [FORWARD] reward_since={_usd(R['reward_since'])}  dump_since={_usd(R['dump_since'])}  "
              f"-> residual (held+fees) = {_usd(residual)}" + ("  (DEGENERATE on static snapshot — mechanism only)" if degenerate else "  (CLEAN)"))
    else:
        print("\n  HISTORICAL residual is NOT computable here: deposits are in no feed, and cash-jump")
        print("  detection conflates deposits with collateral churn / dump proceeds / reward (the small")
        print("  $79-102 jumps are churn, not deposits). 4th independent confirmation that historical net")
        print("  is unrecoverable. Clean net requires the FORWARD path: fixed baseline + frozen deposits.")

    print("\n  cash jumps >= ${:.0f} (label is heuristic, NOT authoritative):".format(DEPOSIT_JUMP_MIN_USD))
    for c in R["deposit_candidates"]:
        tag = c["explained_by"]
        if tag == "DEPOSIT?":
            tag = "LIKELY DEPOSIT (confirm)" if c["jump"] >= 150 else "churn/ambiguous"
        print(f"    {c['date']}  +{_usd(c['jump'])}  redeem_near={_usd(c['redeem_near'])}  -> {tag}")

    print("\n  FORWARD USE: this same detector becomes the DEPOSIT-FREEZE ALARM during the live experiment — "
          "any large unexplained cash jump => flag (someone deposited, or an accounting anomaly).")


if __name__ == "__main__":
    main()
