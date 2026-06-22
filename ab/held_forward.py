#!/usr/bin/env python3
"""ab/held_forward.py — A4 forward held-to-resolution ledger (READ-ONLY, non-behavioral).

The objective is ``Σ[exact_reward − dump_loss − held_to_resolution_loss] / $capital``. The third term
— a position bought (fills) but never exited (no dump/merge) that rides to resolution and pays $1 (won)
or $0 (lost) per share — is INVISIBLE to dump-basis net (no unwind row). ``ab/held_to_res.py`` estimates
it but is MAGNITUDE-UNRELIABLE: its three confounds (DB-rotation seam, deposits-not-in-feed, phantom
fills) all PRE-DATE the frozen baseline.

This module measures it FORWARD from the frozen baseline, where those confounds vanish:
  - Cost basis is taken ONLY from fills at/after ``baseline_ts`` → post-rotation, local, no phantom seam.
  - A position is counted ONLY once it has RESOLVED — i.e. it has net held shares but is GONE from the
    live on-chain ``/positions`` (a still-open position is excluded; a fully dumped/merged one nets ~0).
  - ``held_pnl = redeem_proceeds − cost_of_held`` (a lost side simply produces no redeem event → $0
    proceeds → full-cost loss, flagged for verification).
  - Each resolution is attributed to its A/B cohort (``ab.cohort``, byte-parity with the live allocator),
    so it can RETROACTIVELY correct each cohort's dump-basis net (the provisional-promotion caveat).

Read-only invariant: reads ``bot_history.db`` (mode=ro) for fills/unwinds + the public data-api
(/activity?type=REDEEM and /positions, browser-UA, no auth) — the same feeds the live reconciler reads.
Writes ONLY to a SEPARATE ``held_resolution_ledger.db`` (the ``reward_snapshot.py`` pattern); it NEVER
opens or writes ``bot_history.db`` and never touches allocator/kill/config state. Pure-compute
(``compute_forward_htr``) is split from network I/O so it is hermetically testable without a live API.

Usage:
  python3 -m ab.held_forward --baseline-ts <unix> [--db held_resolution_ledger.db] [--cohorts 2]
  python3 -m ab.held_forward --report [--db ...] [--cohorts 2]
  python3 -m ab.held_forward --baseline-ts <unix> --dry-run     # compute + print, write nothing
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time

try:
    from ab.cohort import cohort
except ImportError:  # allow running as a loose script
    from cohort import cohort

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BOT_DB = os.path.join(_DIR, "bot_history.db")
DEFAULT_LEDGER_DB = os.path.join(_DIR, "held_resolution_ledger.db")
FUNDER = "0xB23Bc80E6719099aeBE0c34389f05EC8C928503f"


# ───────────────────────── pure compute (no network, hermetically testable) ─────────────────────────

def held_cost_since(bot_ro, baseline_ts: float) -> dict:
    """{(cid, side): (held_shares, cost_usd)} for shares OPENED at/after baseline_ts.

    held_shares = fills_since − unwinds_since(yes/no for this side) − merge_since (merges exit BOTH
    sides, so a merge-row of N shares removes N from each of yes and no). cost_usd = held × vwap.
    Only positions whose FILLS are post-baseline are scoped, so the cost basis is clean (no rotation
    seam, no phantom pre-baseline fills)."""
    fills: dict = {}
    for cid, side, sh, usd in bot_ro.execute(
        "SELECT condition_id, side, SUM(shares), SUM(usd_value) FROM fills "
        "WHERE ts >= ? GROUP BY condition_id, side", (baseline_ts,)
    ):
        fills[(cid, side)] = (float(sh or 0), float(usd or 0))

    direct_unwind: dict = {}   # (cid, side) -> shares, for side in (yes, no)
    merge_unwind: dict = {}    # cid -> shares (a merge removes this many from BOTH sides)
    for cid, side, sh in bot_ro.execute(
        "SELECT condition_id, side, SUM(shares) FROM unwinds "
        "WHERE ts >= ? GROUP BY condition_id, side", (baseline_ts,)
    ):
        if side == "merge":
            merge_unwind[cid] = merge_unwind.get(cid, 0.0) + float(sh or 0)
        else:
            direct_unwind[(cid, side)] = direct_unwind.get((cid, side), 0.0) + float(sh or 0)

    out: dict = {}
    for (cid, side), (fsh, fusd) in fills.items():
        if fsh <= 0 or side not in ("yes", "no"):
            continue
        exited = direct_unwind.get((cid, side), 0.0) + merge_unwind.get(cid, 0.0)
        held = max(0.0, fsh - exited)
        if held > 0.5:
            out[(cid, side)] = (held, held * (fusd / fsh))
    return out


def compute_forward_htr(bot_ro, baseline_ts: float, redeem_by_cid: dict,
                        open_cids: set, n_cohorts: int, questions: dict | None = None) -> list:
    """Return ledger records for post-baseline held positions that have RESOLVED.

    Resolved = has net held shares (held_cost_since) AND is NOT in open_cids (gone from on-chain
    /positions). Still-open positions are excluded (not yet resolved). redeem_by_cid: {cid: proceeds};
    a cid absent from it resolved with $0 proceeds (lost side / unverified) → no_redeem_flag=1."""
    held = held_cost_since(bot_ro, baseline_ts)
    questions = questions or {}
    by_cid: dict = {}
    for (cid, side), (sh, cost) in held.items():
        rec = by_cid.setdefault(cid, {"held_yes": 0.0, "held_no": 0.0, "cost_usd": 0.0})
        rec[f"held_{side}"] = sh
        rec["cost_usd"] += cost

    records = []
    now = baseline_ts  # deterministic stamp source for tests; main() overrides recorded_ts
    for cid, rec in by_cid.items():
        if cid in open_cids:
            continue  # still held on-chain → not yet resolved → skip
        proceeds = float(redeem_by_cid.get(cid, 0.0) or 0.0)
        no_redeem = cid not in redeem_by_cid
        records.append({
            "condition_id": cid,
            "cohort": cohort(cid, n_cohorts),
            "baseline_ts": baseline_ts,
            "held_yes": round(rec["held_yes"], 4),
            "held_no": round(rec["held_no"], 4),
            "cost_usd": round(rec["cost_usd"], 6),
            "redeem_proceeds_usd": round(proceeds, 6),
            "held_pnl_usd": round(proceeds - rec["cost_usd"], 6),
            "resolved_ts": 0.0,
            "no_redeem_flag": 1 if no_redeem else 0,
            "question": (questions.get(cid) or "")[:120],
            "recorded_ts": now,
        })
    return records


def cohort_htr(records: list) -> dict:
    """{cohort: {'pnl': sum, 'n': count, 'cost': sum, 'proceeds': sum, 'unverified': count}}."""
    out: dict = {}
    for r in records:
        c = out.setdefault(r["cohort"], {"pnl": 0.0, "n": 0, "cost": 0.0, "proceeds": 0.0, "unverified": 0})
        c["pnl"] += r["held_pnl_usd"]
        c["cost"] += r["cost_usd"]
        c["proceeds"] += r["redeem_proceeds_usd"]
        c["n"] += 1
        c["unverified"] += r["no_redeem_flag"]
    return out


# ───────────────────────── isolated store (mirrors reward_snapshot.py; never bot_history.db) ─────────

def ensure_schema(db_path: str):
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS held_resolution ("
            "condition_id TEXT PRIMARY KEY, cohort INTEGER, baseline_ts REAL, "
            "held_yes REAL, held_no REAL, cost_usd REAL, redeem_proceeds_usd REAL, "
            "held_pnl_usd REAL, resolved_ts REAL, no_redeem_flag INTEGER, question TEXT, recorded_ts REAL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hr_cohort ON held_resolution(cohort)")
        conn.commit()
    finally:
        conn.close()


def upsert_ledger(db_path: str, records: list) -> int:
    """Idempotent upsert keyed on condition_id (INSERT OR REPLACE). Returns rows written."""
    if not records:
        return 0
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO held_resolution (condition_id, cohort, baseline_ts, held_yes, "
            "held_no, cost_usd, redeem_proceeds_usd, held_pnl_usd, resolved_ts, no_redeem_flag, "
            "question, recorded_ts) VALUES (:condition_id, :cohort, :baseline_ts, :held_yes, :held_no, "
            ":cost_usd, :redeem_proceeds_usd, :held_pnl_usd, :resolved_ts, :no_redeem_flag, :question, "
            ":recorded_ts)", records,
        )
        conn.commit()
        return len(records)
    finally:
        conn.close()


# ───────────────────────── network (read-only public data-api; isolated from compute) ───────────────

def _fetch_redeem_by_cid(funder: str) -> dict:
    from ab.fetch_redeem import fetch  # reuse the proven browser-UA pull
    by_cid: dict = {}
    for it in fetch(funder, "REDEEM"):
        c = it.get("conditionId", "")
        if c:
            by_cid[c] = by_cid.get(c, 0.0) + float(it.get("usdcSize", 0) or 0)
    return by_cid


def _fetch_open_cids(funder: str) -> set:
    import json as _json
    import urllib.request
    url = f"https://data-api.polymarket.com/positions?user={funder}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = _json.load(r)
    return {p.get("conditionId", "") for p in data if p.get("conditionId") and float(p.get("size", 0) or 0) > 0.5}


def _open_bot_ro(path: str):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


def _usd(v: float) -> str:
    return f"${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"


def report(db_path: str, n_cohorts: int) -> str:
    if not os.path.exists(db_path):
        return "no held_resolution_ledger.db yet — run the collector first."
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        rows = conn.execute(
            "SELECT cohort, COUNT(*), SUM(held_pnl_usd), SUM(cost_usd), SUM(redeem_proceeds_usd), "
            "SUM(no_redeem_flag) FROM held_resolution GROUP BY cohort ORDER BY cohort").fetchall()
        total = conn.execute("SELECT COALESCE(SUM(held_pnl_usd),0), COUNT(*) FROM held_resolution").fetchone()
    finally:
        conn.close()
    L = ["# A4 forward held-to-resolution ledger (per cohort) — corrects dump-basis net",
         f"  TOTAL: {_usd(float(total[0]))} over {total[1]} resolved held positions"]
    for c, n, pnl, cost, proc, unver in rows:
        L.append(f"  cohort {c}: held-to-res {_usd(float(pnl or 0))}  (n={n}, cost {_usd(float(cost or 0))}, "
                 f"proceeds {_usd(float(proc or 0))}, unverified-no-redeem {unver})")
    L.append("  NOTE: subtract each cohort's held-to-res from its dump-basis net/$ (ab/cohort_net) for true net.")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="A4 forward held-to-resolution ledger (read-only).")
    ap.add_argument("--bot-db", default=DEFAULT_BOT_DB, help="bot_history.db (opened mode=ro)")
    ap.add_argument("--db", default=DEFAULT_LEDGER_DB, help="isolated ledger DB (never bot_history.db)")
    ap.add_argument("--funder", default=FUNDER)
    ap.add_argument("--cohorts", type=int, default=2)
    ap.add_argument("--baseline-ts", type=float, default=0.0, help="unix ts of the frozen baseline")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="compute + print, write nothing")
    args = ap.parse_args()

    if args.report:
        print(report(args.db, args.cohorts))
        return
    if args.baseline_ts <= 0:
        raise SystemExit("--baseline-ts <unix> is required (the frozen-baseline timestamp)")

    redeem = _fetch_redeem_by_cid(args.funder)
    open_cids = _fetch_open_cids(args.funder)
    bot = _open_bot_ro(args.bot_db)
    try:
        questions = {cid: (q or "") for cid, q in bot.execute(
            "SELECT condition_id, MAX(question) FROM fills GROUP BY condition_id")}
        records = compute_forward_htr(bot, args.baseline_ts, redeem, open_cids, args.cohorts, questions)
    finally:
        bot.close()
    stamp = time.time()
    for r in records:
        r["recorded_ts"] = stamp

    by_c = cohort_htr(records)
    print(f"[held_forward] resolved post-baseline held positions: {len(records)} "
          f"(open excluded: {len(open_cids)} on-chain cids)")
    for c in sorted(by_c):
        v = by_c[c]
        print(f"  cohort {c}: {_usd(v['pnl'])}  n={v['n']}  unverified-no-redeem={v['unverified']}")
    if args.dry_run:
        print("[dry-run] nothing written.")
    else:
        print(f"[written] {upsert_ledger(args.db, records)} rows -> {args.db}")


if __name__ == "__main__":
    main()
