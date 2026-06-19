#!/usr/bin/env python3
"""ab/net_engine.py — read-only per-market NET on a DB snapshot.

Reuses the verified net definition from net_analysis.py:

    net_dump_basis = SUM(reward_snapshots.earnings_usd, latest-per-date)  +  SUM(unwinds.pnl)

Held-to-resolution P&L is NOT in this (a position held to expiry has no unwind row), so
dump-basis net OVERSTATES for held markets. Full-net requires a separate, read-only
data-api pull (load_held_to_resolution — TODO, flagged in output, never faked here).

Why this exists instead of pointing net_analysis.py at the snapshot (both verified this session):
  * bot_history.db from `sqlite3 .backup` is WAL-mode with no -wal sidecar -> `mode=ro` throws
    SQLITE_CANTOPEN(14). We open it with `immutable=1` (reads the fully-checkpointed main file).
  * net_analysis.py anchors its window to wall-clock now(); on a stale snapshot (data ends
    Jun 16, analysed Jun 19) `--days 7` would silently capture only the last ~4 days. We anchor
    the window to the DATA's max ts and assert it, so the window is always exactly the data.

This module is a library (load_net) + a CLI summary. It NEVER writes, NEVER touches live state.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics as st
from datetime import datetime, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SNAP = os.path.join(os.path.dirname(_DIR), "snapshots", "2026-06-19")

BOOK_FEATURE_WINDOW_SEC = 24 * 3600.0   # book features over first 24h per cid (matches net_analysis.py)
SWEEP_JUMP_USD = 0.02                    # |Δmid| > 2c between consecutive snapshots = a "sweep"
MIN_BOOK_ROWS = 5                        # need >= this many snapshots in the window for a feature


def open_immutable(path: str) -> sqlite3.Connection:
    """Open a (possibly WAL-mode, sidecar-less) snapshot DB read-only via immutable=1."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"snapshot DB not found: {path}")
    return sqlite3.connect(f"file:{path}?immutable=1", uri=True, timeout=10)


def open_ro(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise FileNotFoundError(f"snapshot DB not found: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


def _cv(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return (st.pstdev(xs) / abs(m)) if m else None


def _data_max_ts(bot: sqlite3.Connection, rew: sqlite3.Connection) -> float:
    """Max ts across the snapshot — the window anchor (NOT wall-clock now)."""
    candidates = [0.0]
    for q in ("SELECT MAX(ts) FROM fills",
              "SELECT MAX(ts) FROM unwinds",
              "SELECT MAX(ts) FROM book_snapshots"):
        r = bot.execute(q).fetchone()[0]
        if r:
            candidates.append(float(r))
    r = rew.execute("SELECT MAX(ts) FROM reward_snapshots").fetchone()[0]
    if r:
        candidates.append(float(r))
    return max(candidates)


def load_net(snap_dir: str = DEFAULT_SNAP, days: int = 7) -> tuple[list[dict], dict]:
    """Return (rows, meta).

    rows: one dict per market in the window with keys:
        cid, reward, dump, net (=reward+dump, DUMP-BASIS), peak, n_fill, chronic,
        q, rate, epct, levels, depth_cv, sweep
    meta: window_start/end (UTC dates), days, n_markets, anchored_to_data (bool),
          snapshot_age_days (data max vs wall clock — staleness, informational)
    """
    bot_path = os.path.join(snap_dir, "bot_history.db")
    rew_path = os.path.join(snap_dir, "reward_snapshots.db")
    bot = open_immutable(bot_path)
    rew = open_ro(rew_path)
    try:
        wend = _data_max_ts(bot, rew)
        if wend <= 0:
            raise RuntimeError("snapshot has no timestamped rows — cannot anchor window")
        cut = wend - days * 86400.0

        # reward: exact per-market, latest snapshot per (date, cid), summed over window
        reward, feats = {}, {}
        for cid, r, q, rate, epct in rew.execute(
            "WITH latest AS ("
            "  SELECT date, condition_id, earnings_usd, question, daily_rate, earning_percentage,"
            "         ROW_NUMBER() OVER (PARTITION BY date, condition_id ORDER BY ts DESC) rn"
            "  FROM reward_snapshots WHERE ts > ? AND ts <= ?) "
            "SELECT condition_id, SUM(earnings_usd), MAX(question), AVG(daily_rate), AVG(earning_percentage) "
            "FROM latest WHERE rn = 1 GROUP BY condition_id",
            (cut, wend),
        ):
            reward[cid] = float(r or 0)
            feats[cid] = dict(q=(q or "")[:40], rate=float(rate or 0), epct=float(epct or 0))

        # dump P&L (realized SELL pnl) + peak capital + fill count, anchored to the window
        dump = {cid: float(p or 0) for cid, p in bot.execute(
            "SELECT condition_id, SUM(pnl) FROM unwinds WHERE ts > ? AND ts <= ? GROUP BY condition_id",
            (cut, wend))}
        fills = {cid: (float(pk or 0), int(nf or 0)) for cid, pk, nf in bot.execute(
            "SELECT condition_id, MAX(COALESCE(position_usd_after, 0)), COUNT(*) "
            "FROM fills WHERE ts > ? AND ts <= ? GROUP BY condition_id",
            (cut, wend))}
        chronic = {x for (x,) in bot.execute(
            "SELECT condition_id FROM market_cooldowns WHERE chronic_blocked = 1")}

        universe = set(reward) | set(dump) | set(fills)

        # book features over first 24h per cid (same definition that produced the J≈0.49 sweep result)
        book = {}
        for cid in universe:
            first = bot.execute(
                "SELECT MIN(ts) FROM book_snapshots WHERE condition_id = ?", (cid,)).fetchone()[0]
            if not first:
                continue
            brows = bot.execute(
                "SELECT total_bid_depth, total_ask_depth, num_bid_levels, num_ask_levels, midpoint "
                "FROM book_snapshots WHERE condition_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
                (cid, first, first + BOOK_FEATURE_WINDOW_SEC)).fetchall()
            if len(brows) >= MIN_BOOK_ROWS:
                deps = [float(r[0] or 0) + float(r[1] or 0) for r in brows]
                lv = [float(r[2] or 0) + float(r[3] or 0) for r in brows]
                mids = [float(r[4] or 0) for r in brows]
                book[cid] = dict(
                    levels=sum(lv) / len(lv),
                    depth_cv=_cv(deps),
                    sweep=sum(1 for i in range(1, len(mids))
                              if abs(mids[i] - mids[i - 1]) > SWEEP_JUMP_USD) / max(1, len(mids) - 1),
                )

        rows = []
        for cid in universe:
            rw = reward.get(cid, 0.0)
            dm = dump.get(cid, 0.0)
            pk, nf = fills.get(cid, (0.0, 0))
            f = feats.get(cid, {})
            b = book.get(cid, {})
            rows.append(dict(
                cid=cid, reward=rw, dump=dm, net=rw + dm, peak=pk, n_fill=nf,
                chronic=cid in chronic, q=f.get("q", cid[:18]),
                rate=f.get("rate"), epct=f.get("epct"),
                levels=b.get("levels"), depth_cv=b.get("depth_cv"), sweep=b.get("sweep"),
            ))

        # ── Capture-ratio context: per-market reward UNDER-captures the data-api aggregate, so
        # per-market net OVERSTATES loss. Compare against the bot's own cached data-api __TOTAL__
        # (daily_reward_cache) over the exact dates reward_snapshots covers, both same-day and
        # +1-day-shifted (the verified earned-vs-credited shift). Reported as a band so no one
        # over-reads the pessimistic per-market figure. (rule #1 — don't present a biased number.)
        from datetime import timedelta
        per_market_reward = sum(reward.values())
        rs_min, rs_max = rew.execute(
            "SELECT MIN(date), MAX(date) FROM reward_snapshots WHERE ts > ? AND ts <= ?",
            (cut, wend)).fetchone() or (None, None)
        agg_same = agg_shift = None
        if rs_min and rs_max:
            agg_same = float(bot.execute(
                "SELECT COALESCE(SUM(reward_earned), 0) FROM daily_reward_cache "
                "WHERE condition_id = '__TOTAL__' AND date BETWEEN ? AND ?",
                (rs_min, rs_max)).fetchone()[0])
            d0 = (datetime.strptime(rs_min, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            d1 = (datetime.strptime(rs_max, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            agg_shift = float(bot.execute(
                "SELECT COALESCE(SUM(reward_earned), 0) FROM daily_reward_cache "
                "WHERE condition_id = '__TOTAL__' AND date BETWEEN ? AND ?",
                (d0, d1)).fetchone()[0])

        import time as _t
        meta = dict(
            window_start=datetime.fromtimestamp(cut, timezone.utc).strftime("%Y-%m-%d"),
            window_end=datetime.fromtimestamp(wend, timezone.utc).strftime("%Y-%m-%d"),
            window_end_ts=wend, window_start_ts=cut, days=days, n_markets=len(rows),
            anchored_to_data=True,
            snapshot_age_days=round((_t.time() - wend) / 86400.0, 2),
            reward_dates=(rs_min, rs_max),
            per_market_reward=round(per_market_reward, 2),
            agg_reward_same=round(agg_same, 2) if agg_same is not None else None,
            agg_reward_shift=round(agg_shift, 2) if agg_shift is not None else None,
            capture_same=round(per_market_reward / agg_same, 3) if agg_same else None,
            capture_shift=round(per_market_reward / agg_shift, 3) if agg_shift else None,
        )
        return rows, meta
    finally:
        bot.close()
        rew.close()


def _usd(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return f"${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"


def main():
    ap = argparse.ArgumentParser(description="Per-market DUMP-BASIS net on a snapshot (read-only).")
    ap.add_argument("--snap", default=DEFAULT_SNAP, help="snapshot dir with bot_history.db + reward_snapshots.db")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    rows, meta = load_net(args.snap, args.days)
    pos = [r for r in rows if r["net"] > 0]
    neg = [r for r in rows if r["net"] < 0]
    tot_rew = sum(r["reward"] for r in rows)
    tot_dump = sum(r["dump"] for r in rows)
    cap = sum(r["peak"] for r in rows)

    print(f"# ab/net_engine — DUMP-BASIS net (held-to-resolution NOT included -> overstates held markets)")
    print(f"  snapshot: {args.snap}")
    print(f"  window:   {meta['window_start']} -> {meta['window_end']}  ({meta['days']}d, anchored to DATA max ts)")
    print(f"  snapshot age: {meta['snapshot_age_days']}d behind wall-clock  |  markets: {meta['n_markets']}")
    print(f"  net-POSITIVE: {len(pos)}   net-negative: {len(neg)}")
    print(f"\n  -- reward capture (per-market UNDER-captures the data-api aggregate) --")
    print(f"  per-market reward (reward_snapshots): {_usd(meta['per_market_reward'])}  over {meta['reward_dates'][0]}..{meta['reward_dates'][1]}")
    if meta.get("agg_reward_same") is not None:
        print(f"  data-api aggregate same-window:  {_usd(meta['agg_reward_same'])}   capture={meta['capture_same']}")
        print(f"  data-api aggregate +1day-shift:  {_usd(meta['agg_reward_shift'])}   capture={meta['capture_shift']}  (earned-vs-credited shift)")
    print(f"\n  -- NET band (DUMP-BASIS; held-to-resolution excluded -> true net is WORSE) --")
    print(f"  dump P&L: {_usd(tot_dump)}")
    print(f"  net (per-market reward, PESSIMISTIC): {_usd(tot_rew + tot_dump)}")
    if meta.get("agg_reward_same") is not None:
        print(f"  net (aggregate-adjusted reward):      {_usd(meta['agg_reward_same'] + tot_dump)}   <- reward not per-market-attributable")
    if cap:
        print(f"  capital (sum peak): {_usd(cap)}   net/$ (per-market basis): {(tot_rew + tot_dump) / cap:+.4f}")
    print("\n  worst 6 by net:")
    for r in sorted(rows, key=lambda x: x["net"])[:6]:
        print(f"    net={_usd(r['net']):>9} rew={_usd(r['reward']):>7} dump={_usd(r['dump']):>9} "
              f"sweep={('%.2f' % r['sweep']) if r['sweep'] is not None else '  n/a':>5} "
              f"chr={'Y' if r['chronic'] else ' '}  {r['q']}")
    print("\n  NOTE: full-net (incl. held-to-resolution) needs a read-only data-api pull — not yet added.")


if __name__ == "__main__":
    main()
