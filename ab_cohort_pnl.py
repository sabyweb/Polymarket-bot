"""A/B cohort-level P&L aggregator.

Reads three independent data sources and joins them by condition_id:
  - candidate_features.db  -> which cohort a market belonged to when deployed
  - reward_snapshots.db    -> actual per-market liquidity earnings
  - bot_history.db         -> fills (cost/slippage/age) and unwinds (realized pnl)

Outputs one row per cohort per window into `bot_history.db.cohort_pnl`.
This is the canonical source for "which A/B cohort is making money".
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

from config import cfg

log = logging.getLogger("ab_cohort_pnl")


def _default_reward_db(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "reward_snapshots.db")


def _default_candidate_db(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "candidate_features.db")


def compute(
    window_hours: float = 24.0,
    db_path: str = "bot_history.db",
    reward_db_path: str | None = None,
    candidate_db_path: str | None = None,
    _now: Callable[[], float] = time.time,
    _cohort_count: int | None = None,
) -> list[dict]:
    """Compute cohort P&L for the trailing `window_hours` and return row dicts.

    The rows are also written to `bot_history.db.cohort_pnl` if possible.
    Fail-open: any exception logs a warning and returns an empty list.
    """
    if reward_db_path is None:
        reward_db_path = _default_reward_db(db_path)
    if candidate_db_path is None:
        candidate_db_path = _default_candidate_db(db_path)
    if _cohort_count is None:
        try:
            _cohort_count = max(1, int(cfg("RF_AB_COHORT_COUNT") or 2))
        except Exception:
            _cohort_count = 2

    try:
        return _compute_unsafe(
            window_hours, db_path, reward_db_path, candidate_db_path, _now, _cohort_count
        )
    except Exception as e:
        log.warning(f"[AB_COHORT_PNL] compute failed: {e}")
        return []


def _compute_unsafe(
    window_hours: float,
    db_path: str,
    reward_db_path: str,
    candidate_db_path: str,
    _now: Callable[[], float],
    cohort_count: int,
) -> list[dict]:
    now = _now()
    window_start = now - window_hours * 3600.0
    window_end = now

    # 1. Cohort map: cid -> cohort for markets that were deployed in the window.
    cohort_map: dict[str, int] = {}
    deploy_rows: list[tuple[int, str, float]] = []
    try:
        conn = sqlite3.connect(candidate_db_path)
        conn.row_factory = sqlite3.Row
        # Latest cohort per cid in the window (stable hash, but we trust the log).
        rows = conn.execute(
            """SELECT condition_id, cohort, MAX(ts) AS ts
               FROM candidate_features
               WHERE action='deploy' AND ts >= ? AND ts <= ?
               GROUP BY condition_id""",
            (window_start, window_end),
        ).fetchall()
        for r in rows:
            cohort_map[r["condition_id"]] = int(r["cohort"])
        # Deploy rows for target-capital aggregation.
        deploy_rows = conn.execute(
            """SELECT cohort, condition_id, target_capital
               FROM candidate_features
               WHERE action='deploy' AND ts >= ? AND ts <= ?""",
            (window_start, window_end),
        ).fetchall()
        conn.close()
    except Exception as e:
        log.warning(f"[AB_COHORT_PNL] candidate_features read failed: {e}")
        return []

    if not cohort_map:
        log.debug("[AB_COHORT_PNL] no deployed markets in window")
        return []

    cids = list(cohort_map.keys())
    cid_set = set(cids)
    placeholders = ",".join("?" * len(cids))

    # 2. Per-cid reward from reward_snapshots (latest snapshot per date/cid).
    reward_by_cid: dict[str, float] = defaultdict(float)
    try:
        if os.path.exists(reward_db_path):
            conn = sqlite3.connect(reward_db_path)
            conn.row_factory = sqlite3.Row
            # SQLite pre-3.25 lacks window functions in some environments, so use
            # a correlated subquery fallback. This is safe because the table is
            # small (thousands of rows).
            rows = conn.execute(
                f"""SELECT condition_id, SUM(earnings_usd) AS reward
                    FROM reward_snapshots rs1
                    WHERE condition_id IN ({placeholders})
                      AND ts >= ? AND ts <= ?
                      AND ts = (
                          SELECT MAX(ts) FROM reward_snapshots rs2
                          WHERE rs2.condition_id = rs1.condition_id
                            AND rs2.date = rs1.date
                      )
                    GROUP BY condition_id""",
                cids + [window_start, window_end],
            ).fetchall()
            for r in rows:
                reward_by_cid[r["condition_id"]] = float(r["reward"] or 0.0)
            conn.close()
    except Exception as e:
        log.warning(f"[AB_COHORT_PNL] reward_snapshots read failed: {e}")

    # 3. Per-cid unwind pnl from bot_history.db.
    unwind_pnl_by_cid: dict[str, float] = defaultdict(float)
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT condition_id, SUM(pnl) AS pnl
                FROM unwinds
                WHERE condition_id IN ({placeholders})
                  AND ts >= ? AND ts <= ?
                GROUP BY condition_id""",
            cids + [window_start, window_end],
        ).fetchall()
        for r in rows:
            unwind_pnl_by_cid[r["condition_id"]] = float(r["pnl"] or 0.0)
        conn.close()
    except Exception as e:
        log.warning(f"[AB_COHORT_PNL] unwinds read failed: {e}")

    # 4. Per-cid fill stats from bot_history.db.
    fill_stats_by_cid: dict[str, dict] = defaultdict(
        lambda: {
            "count": 0,
            "shares": 0.0,
            "gross_cost": 0.0,
            "total_slippage": 0.0,
            "age_sum": 0.0,
            "slippage_sum": 0.0,
        }
    )
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT condition_id,
                       COUNT(*) AS cnt,
                       SUM(shares) AS shares,
                       SUM(shares * clob_cost) AS gross_cost,
                       SUM(shares * slippage) AS total_slippage,
                       SUM(order_age_secs) AS age_sum,
                       SUM(slippage) AS slippage_sum
                FROM fills
                WHERE condition_id IN ({placeholders})
                  AND ts >= ? AND ts <= ?
                GROUP BY condition_id""",
            cids + [window_start, window_end],
        ).fetchall()
        for r in rows:
            cid = r["condition_id"]
            fill_stats_by_cid[cid] = {
                "count": int(r["cnt"] or 0),
                "shares": float(r["shares"] or 0.0),
                "gross_cost": float(r["gross_cost"] or 0.0),
                "total_slippage": float(r["total_slippage"] or 0.0),
                "age_sum": float(r["age_sum"] or 0.0),
                "slippage_sum": float(r["slippage_sum"] or 0.0),
            }
        conn.close()
    except Exception as e:
        log.warning(f"[AB_COHORT_PNL] fills read failed: {e}")

    # 5. Aggregate by cohort. Include every possible cohort id so rows with
    # zero activity are still emitted (makes the comparison table complete).
    cohorts = set(range(cohort_count))
    agg: dict[int, dict] = {
        c: {
            "reward_earned": 0.0,
            "unwind_pnl": 0.0,
            "fill_count": 0,
            "filled_markets": set(),
            "shares_filled": 0.0,
            "gross_fill_cost": 0.0,
            "total_slippage": 0.0,
            "age_sum": 0.0,
            "slippage_sum": 0.0,
            "deployed_markets": set(),
            "target_capital": 0.0,
        }
        for c in cohorts
    }

    for r in deploy_rows:
        cohort, cid, cap = int(r[0]), r[1], float(r[2] or 0.0)
        if cohort not in agg:
            continue
        agg[cohort]["deployed_markets"].add(cid)
        agg[cohort]["target_capital"] += cap

    for cid, cohort in cohort_map.items():
        agg[cohort]["reward_earned"] += reward_by_cid.get(cid, 0.0)
        agg[cohort]["unwind_pnl"] += unwind_pnl_by_cid.get(cid, 0.0)
        fs = fill_stats_by_cid.get(cid)
        if fs and fs["count"]:
            agg[cohort]["fill_count"] += fs["count"]
            agg[cohort]["filled_markets"].add(cid)
            agg[cohort]["shares_filled"] += fs["shares"]
            agg[cohort]["gross_fill_cost"] += fs["gross_cost"]
            agg[cohort]["total_slippage"] += fs["total_slippage"]
            agg[cohort]["age_sum"] += fs["age_sum"]
            agg[cohort]["slippage_sum"] += fs["slippage_sum"]

    rows = []
    for cohort in sorted(agg):
        a = agg[cohort]
        fill_count = a["fill_count"]
        filled_markets = len(a["filled_markets"])
        deployed_markets = len(a["deployed_markets"])
        avg_fill_age = a["age_sum"] / fill_count if fill_count else 0.0
        avg_slippage = a["slippage_sum"] / fill_count if fill_count else 0.0
        row = {
            "ts": now,
            "window_start_ts": window_start,
            "window_end_ts": window_end,
            "cohort": cohort,
            "cohort_count": cohort_count,
            "reward_earned": round(a["reward_earned"], 6),
            "unwind_pnl": round(a["unwind_pnl"], 6),
            "net_pnl": round(a["reward_earned"] + a["unwind_pnl"], 6),
            "fill_count": fill_count,
            "filled_markets": filled_markets,
            "shares_filled": round(a["shares_filled"], 6),
            "gross_fill_cost": round(a["gross_fill_cost"], 6),
            "total_slippage": round(a["total_slippage"], 6),
            "avg_fill_age_secs": round(avg_fill_age, 2),
            "avg_slippage": round(avg_slippage, 6),
            "deployed_markets": deployed_markets,
            "target_capital": round(a["target_capital"], 2),
        }
        rows.append(row)

    # 6. Persist to bot_history.db.
    try:
        conn = sqlite3.connect(db_path)
        conn.executemany(
            """INSERT INTO cohort_pnl (
                ts, window_start_ts, window_end_ts, cohort, cohort_count, reward_earned,
                unwind_pnl, net_pnl, fill_count, filled_markets, shares_filled,
                gross_fill_cost, total_slippage, avg_fill_age_secs, avg_slippage,
                deployed_markets, target_capital
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(window_end_ts, cohort, cohort_count) DO UPDATE SET
                ts=excluded.ts,
                window_start_ts=excluded.window_start_ts,
                reward_earned=excluded.reward_earned,
                unwind_pnl=excluded.unwind_pnl,
                net_pnl=excluded.net_pnl,
                fill_count=excluded.fill_count,
                filled_markets=excluded.filled_markets,
                shares_filled=excluded.shares_filled,
                gross_fill_cost=excluded.gross_fill_cost,
                total_slippage=excluded.total_slippage,
                avg_fill_age_secs=excluded.avg_fill_age_secs,
                avg_slippage=excluded.avg_slippage,
                deployed_markets=excluded.deployed_markets,
                target_capital=excluded.target_capital""",
            [
                (
                    r["ts"], r["window_start_ts"], r["window_end_ts"], r["cohort"],
                    r["cohort_count"], r["reward_earned"], r["unwind_pnl"], r["net_pnl"],
                    r["fill_count"], r["filled_markets"], r["shares_filled"],
                    r["gross_fill_cost"], r["total_slippage"], r["avg_fill_age_secs"],
                    r["avg_slippage"], r["deployed_markets"], r["target_capital"],
                )
                for r in rows
            ],
        )
        conn.commit()
        conn.close()
        net_parts = " ".join(
            f"C{r['cohort']}={r['net_pnl']:.4f}" for r in rows
        )
        log.info(
            f"[AB_COHORT_PNL] window={window_hours}h count={cohort_count} {net_parts}"
        )
    except Exception as e:
        log.warning(f"[AB_COHORT_PNL] persist failed: {e}")

    return rows


def report(
    window_hours: float = 24.0,
    db_path: str = "bot_history.db",
    reward_db_path: str | None = None,
    candidate_db_path: str | None = None,
    _now: Callable[[], float] = time.time,
    _cohort_count: int | None = None,
) -> None:
    """Compute and print a human-readable cohort P&L report."""
    rows = compute(window_hours, db_path, reward_db_path, candidate_db_path, _now, _cohort_count)
    if not rows:
        print("No cohort P&L data available for the requested window.")
        return

    start_dt = datetime.fromtimestamp(rows[0]["window_start_ts"], tz=timezone.utc)
    end_dt = datetime.fromtimestamp(rows[0]["window_end_ts"], tz=timezone.utc)
    cohort_count = rows[0]["cohort_count"]
    print(f"Cohort P&L — {window_hours}h window ending {end_dt.isoformat()} UTC (cohort_count={cohort_count})")
    print(f"  window start: {start_dt.isoformat()} UTC")
    print()
    for r in rows:
        print(f"Cohort {r['cohort']}:")
        print(f"  deployed markets: {r['deployed_markets']}")
        print(f"  target capital:   ${r['target_capital']:.2f}")
        print(f"  fills:            {r['fill_count']} ({r['filled_markets']} markets)")
        print(f"  reward earned:    ${r['reward_earned']:.4f}")
        print(f"  unwind pnl:       ${r['unwind_pnl']:.4f}")
        print(f"  net pnl:          ${r['net_pnl']:.4f}")
        print(f"  avg fill age:     {r['avg_fill_age_secs']:.0f}s")
        print(f"  avg slippage:     ${r['avg_slippage']:.6f}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="A/B cohort P&L report")
    ap.add_argument("--window-hours", type=float, default=24.0)
    ap.add_argument("--db", default="bot_history.db")
    ap.add_argument("--reward-db", default=None)
    ap.add_argument("--candidate-db", default=None)
    args = ap.parse_args()
    report(
        window_hours=args.window_hours,
        db_path=args.db,
        reward_db_path=args.reward_db,
        candidate_db_path=args.candidate_db,
    )
