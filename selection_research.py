"""
selection_research.py — Loop B, Phase 2: offline market-selection counterfactual.

NOT a backtester and NOT a live actor. It is a read-only analysis over REAL
logged outcomes (per ground_rules: "signals are live-derived; sim is hygiene
only"). It answers one question on real data:

    "Among the markets we actually traded, do the ones that lost money have
     high recent volatility — and if we had excluded high-volatility markets
     (the RF_ALLOC_MAX_RECENT_VOLATILITY knob), what would the net effect have
     been (losses avoided vs reward forgone)?"

Why this and not backtest.py: backtest.py replays `cycle_snapshots` (empty on
live) and its engine ignores the selection knobs entirely — verified 2026-06-10.
So we use the tables that ARE populated live: `unwinds` (realized P&L per
market), `fills` (capital + entry time), `book_snapshots` (the midpoint series
the volatility knob reads).

Faithfulness: volatility is computed exactly as simple_allocator._recent_volatility
does — MAX(midpoint)-MIN(midpoint) over RF_ALLOC_VOLATILITY_WINDOW_HOURS, needing
>= RF_ALLOC_VOLATILITY_MIN_SAMPLES samples or it FAILS OPEN (market NOT excluded)
— measured in the window BEFORE each market's first fill (decision time).

Honest limits (carried from LOOP_PLAN.md §5):
  - Survivorship: only speaks to markets we entered; silent on markets we avoided.
  - Reward is an ESTIMATE (unwinds.reward_earned_est); realized loss (unwinds.pnl)
    is the hard signal. Weight the loss side.
  - Tuning a threshold on one window risks overfit (B1) — a held-out check is shown,
    and the real proof remains the staged Wave-4 canary soak, not this analysis.
  - This is a FILTER that proposes candidates to a human; it deploys nothing.

Read-only: opens the DB mode=ro. Safe by default (prints). --write appends to
docs/selection_experiments.md. On a live box, point --db at a `sqlite3 .backup`
snapshot rather than the live WAL.

Usage:
  python3 selection_research.py --db bot_history.db --days 14
  python3 selection_research.py --days 14 --thresholds 0.05 0.08 0.10 0.15 0.20 0.30
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
from datetime import datetime, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(_DIR, "bot_history.db")
EXPERIMENTS_LOG = os.path.join(_DIR, "docs", "selection_experiments.md")

# Defaults mirror config.py (RF_ALLOC_VOLATILITY_*). Override via CLI.
DEFAULT_VOL_WINDOW_HOURS = 6.0
DEFAULT_VOL_MIN_SAMPLES = 5
DEFAULT_THRESHOLDS = [0.05, 0.08, 0.10, 0.15, 0.20, 0.30]


def _ro(db_path: str):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)


def _usd(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return f"${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"


# ---------------------------------------------------------------------------
# Per-market aggregation over the window
# ---------------------------------------------------------------------------
def gather_markets(conn, cutoff: float) -> dict:
    """Return {cid: {question, net_pnl, reward_est, n_unwinds, losing,
    first_fill_ts, peak_capital}} for markets traded since cutoff."""
    m: dict = {}

    # Realized P&L + reward estimate from unwinds (the hard loss signal).
    for r in conn.execute(
        "SELECT condition_id, COALESCE(MAX(question),'') q, "
        "SUM(pnl) net, COUNT(*) n, "
        "SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) losing, "
        "SUM(COALESCE(reward_earned_est,0)) reward_est "
        "FROM unwinds WHERE ts > ? GROUP BY condition_id",
        (cutoff,),
    ):
        m[r[0]] = {
            "question": r[1], "net_pnl": float(r[2] or 0), "n_unwinds": int(r[3] or 0),
            "losing": int(r[4] or 0), "reward_est": float(r[5] or 0),
            "first_fill_ts": None, "peak_capital": 0.0, "n_fills": 0,
        }

    # Entry time + peak capital + fill count per market from fills.
    for r in conn.execute(
        "SELECT condition_id, MIN(ts) first_ts, MAX(COALESCE(position_usd_after,0)) peak, "
        "COUNT(*) nf FROM fills WHERE ts > ? GROUP BY condition_id",
        (cutoff,),
    ):
        if r[0] in m:
            m[r[0]]["first_fill_ts"] = float(r[1]) if r[1] else None
            m[r[0]]["peak_capital"] = float(r[2] or 0)
            m[r[0]]["n_fills"] = int(r[3] or 0)
    return m


def volatility_at_entry(conn, cid: str, entry_ts, window_hours: float, min_samples: int):
    """Replicate simple_allocator._recent_volatility at decision time:
    midpoint MAX-MIN over [entry_ts - window, entry_ts], >= min_samples or None."""
    if not entry_ts:
        return None
    since = entry_ts - window_hours * 3600.0
    try:
        row = conn.execute(
            "SELECT MAX(midpoint), MIN(midpoint), COUNT(*) FROM book_snapshots "
            "WHERE condition_id = ? AND ts >= ? AND ts <= ?",
            (cid, since, entry_ts),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row or row[0] is None or row[2] is None or int(row[2]) < min_samples:
        return None
    return float(row[0]) - float(row[1])


# ---------------------------------------------------------------------------
# Counterfactual sweep over the volatility-exclusion threshold
# ---------------------------------------------------------------------------
def sweep(markets: dict, thresholds: list) -> tuple:
    """LOSS-SIDE ONLY. For each threshold X, exclude markets whose volatility is
    known and > X, and report how much realized LOSS that would have avoided and
    how much breadth it costs (markets + fills dropped). We deliberately do NOT
    claim a net P&L, because per-market reward is unmeasurable historically — the
    forgone reward is the unknown the live soak must measure."""
    total_loss = -sum(d["net_pnl"] for d in markets.values() if d["net_pnl"] < 0)
    total_fills = sum(d.get("n_fills", 0) for d in markets.values())
    n_mkts = len(markets) or 1
    rows = []
    for x in thresholds:
        excl = [d for d in markets.values() if d["vol"] is not None and d["vol"] > x]
        loss_avoided = -sum(d["net_pnl"] for d in excl if d["net_pnl"] < 0)
        fills_excl = sum(d.get("n_fills", 0) for d in excl)
        rows.append({
            "x": x, "n": len(excl),
            "loss_avoided": loss_avoided,
            "pct_mkts": len(excl) / n_mkts * 100,
            "fills_excl": fills_excl,
            "pct_fills": (fills_excl / total_fills * 100) if total_fills else 0.0,
        })
    return total_loss, total_fills, rows


def loss_avoided_halves(markets: dict, x: float, mid_ts: float):
    """Overfit sanity (B1): realized loss the threshold would avoid, split by
    first-fill time into first vs second half of the window."""
    def half(first_half: bool):
        s = 0.0
        for d in markets.values():
            ft = d.get("first_fill_ts")
            if ft is None or (ft < mid_ts) != first_half:
                continue
            if d["vol"] is not None and d["vol"] > x and d["net_pnl"] < 0:
                s += -d["net_pnl"]
        return s
    return half(True), half(False)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def build_report(db_path: str, days: int, window_hours: float, min_samples: int,
                 thresholds: list, top: int = 20) -> str:
    now = time.time()
    cutoff = now - days * 86400
    L = [f"# Selection counterfactual — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
         f"window={days}d · vol_window={window_hours}h · min_samples={min_samples} · "
         f"thresholds={thresholds}"]

    conn = _ro(db_path)
    try:
        markets = gather_markets(conn, cutoff)
        if not markets:
            L.append("\nNo markets traded in window (no unwinds). Nothing to analyze.")
            return "\n".join(L)
        # attach volatility-at-entry
        vol_known = 0
        for cid, d in markets.items():
            d["vol"] = volatility_at_entry(conn, cid, d["first_fill_ts"], window_hours, min_samples)
            if d["vol"] is not None:
                vol_known += 1
    finally:
        conn.close()

    n = len(markets)
    total_net = sum(d["net_pnl"] for d in markets.values())
    total_loss = -sum(d["net_pnl"] for d in markets.values() if d["net_pnl"] < 0)
    L.append("\n> LOSS-SIDE ONLY. Per-market reward is not recoverable historically "
             "(Polymarket pays a daily lump; only __TOTAL__ is stored). This ranks "
             "candidates by realized LOSS and breadth cost; it does NOT prove net P&L. "
             "Net effect (reward included) must be measured on the live Wave-4 soak.")
    L.append(f"\nMarkets traded: {n} · volatility known for {vol_known}/{n} "
             f"(rest fail-open → never excluded)")
    L.append(f"Realized P&L (unwinds): {_usd(total_net)} · gross realized loss: {_usd(total_loss)}")

    # Per-market table (worst realized P&L first)
    L.append(f"\n## Markets ranked by realized loss (worst {top})")
    L.append("net_pnl | vol@entry | losing/unwinds | peak_cap | fills | market")
    ordered = sorted(markets.values(), key=lambda d: d["net_pnl"])
    for d in ordered[:top]:
        vol = "n/a" if d["vol"] is None else f"{d['vol']:.3f}"
        L.append(f"{_usd(d['net_pnl'])} | {vol} | {d['losing']}/{d['n_unwinds']} | "
                 f"{_usd(d['peak_capital'])} | {d.get('n_fills',0)} | {(d['question'] or '')[:44]}")

    # Volatility-exclusion sweep (loss-side + breadth cost)
    total_loss_sw, total_fills, rows = sweep(markets, thresholds)
    L.append(f"\n## Volatility-exclusion sweep (RF_ALLOC_MAX_RECENT_VOLATILITY)")
    L.append(f"total realized loss in window: {_usd(total_loss_sw)} · total fills: {total_fills}")
    L.append("threshold | #excl | loss_avoided | %markets_excl | %fills_excl (breadth cost)")
    for r in rows:
        L.append(f"{r['x']:.2f} | {r['n']} | {_usd(r['loss_avoided'])} | "
                 f"{r['pct_mkts']:.0f}% | {r['pct_fills']:.0f}%")
    L.append("Read as a tradeoff: loss_avoided is the upside; %fills_excl is the breadth "
             "(and thus reward) you'd give up — the unknown the soak resolves.")
    # overfit sanity on the loss side, at a mid threshold
    mid_x = thresholds[len(thresholds)//2]
    mid_ts = cutoff + (now - cutoff) / 2
    h1, h2 = loss_avoided_halves(markets, mid_x, mid_ts)
    L.append(f"Overfit check @ {mid_x:.2f}: loss avoided first half {_usd(h1)} · second half {_usd(h2)} "
             f"(both > 0 = the effect isn't one outlier).")

    # Candidate lists for the OTHER knobs (cap, cooldown) — also loss-side signals
    repeat = sorted((d for d in markets.values() if d["losing"] >= 2 and d["net_pnl"] < 0),
                    key=lambda d: d["net_pnl"])[:8]
    L.append("\n## Repeat-loser candidates (→ RF_PREEMPTIVE_COOLDOWN_ENABLED)")
    if not repeat:
        L.append("- none.")
    for d in repeat:
        L.append(f"- {_usd(d['net_pnl'])} · {d['losing']}/{d['n_unwinds']} losing · {(d['question'] or '')[:44]}")

    bigcap = sorted((d for d in markets.values() if d["net_pnl"] < 0 and d["peak_capital"] >= 80),
                    key=lambda d: d["net_pnl"])[:8]
    L.append("\n## High-capital losers (→ RF_MAX_CAPITAL_PER_MARKET_USD)")
    if not bigcap:
        L.append("- none above $80 peak capital.")
    for d in bigcap:
        L.append(f"- {_usd(d['net_pnl'])} · peak_cap {_usd(d['peak_capital'])} · {(d['question'] or '')[:44]}")

    L.append("\n_read-only · markets we traded only (survivorship) · loss-side only · "
             "candidate generator, NOT proof · deploys nothing._")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Loop B Phase 2 — selection counterfactual (read-only).")
    ap.add_argument("--db", default=DEFAULT_DB, help="path to bot_history.db (opened read-only; use a snapshot on live)")
    ap.add_argument("--days", type=int, default=14, help="analysis window in days (default 14)")
    ap.add_argument("--vol-window-hours", type=float, default=DEFAULT_VOL_WINDOW_HOURS)
    ap.add_argument("--vol-min-samples", type=int, default=DEFAULT_VOL_MIN_SAMPLES)
    ap.add_argument("--thresholds", nargs="+", type=float, default=DEFAULT_THRESHOLDS)
    ap.add_argument("--top", type=int, default=20, help="rows in the per-market table")
    ap.add_argument("--write", action="store_true", help="append the report to docs/selection_experiments.md")
    args = ap.parse_args()

    report = build_report(args.db, args.days, args.vol_window_hours,
                          args.vol_min_samples, args.thresholds, args.top)
    print(report)

    if args.write:
        os.makedirs(os.path.dirname(EXPERIMENTS_LOG), exist_ok=True)
        new = not os.path.exists(EXPERIMENTS_LOG)
        with open(EXPERIMENTS_LOG, "a") as f:
            if new:
                f.write("# Selection experiments\n\nAppended by `selection_research.py` (read-only).\n")
            f.write("\n\n---\n\n" + report + "\n")
        print(f"\n[written] {EXPERIMENTS_LOG}")


if __name__ == "__main__":
    main()
