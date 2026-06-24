#!/usr/bin/env python3
"""candidate_features_log.py — A3 isolated survivorship log (non-behavioral instrumentation).

Writes per-cycle candidate feature vectors (deployed AND avoided) to an ISOLATED
`candidate_features.db` — NEVER `bot_history.db` — so live trading state is untouched and the log
is removable without affecting the bot. Populated only when RF_CANDIDATE_FEATURE_LOG_ENABLED makes
SimpleAllocator.compute() build `AllocationResult.candidate_features`; the oversight loop calls
`append()` AFTER writing the alloc file, fail-quiet. Closes survivorship (we otherwise only have
outcomes for ENTERED markets).

Each row is the decision-time feature vector for one eligible candidate that cycle. recent_volatility
and recent_sweep are intentionally NOT captured here (they're computed lazily inside the deploy loop
for only some candidates); they are backfillable offline from `book_snapshots` using condition_id +
cycle_ts, so the live path adds no per-candidate DB query.

Read/append-only on its own DB. No live state touched.
"""
from __future__ import annotations

import os
import sqlite3
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(_DIR, "candidate_features.db")

_INSERT_COLS = (
    "ts", "cycle_ts", "condition_id", "cohort", "action", "reason", "daily_rate", "max_spread",
    "min_size", "midpoint_guess", "volume_24h", "expected_q_share", "q_share_source",
    "expected_daily_reward", "target_shares", "target_capital", "target_queue_usd",
    "hours_to_resolution", "end_date_iso", "game_start_time", "question",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent — safe to call on every append (CREATE TABLE/INDEX IF NOT EXISTS)."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS candidate_features (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, cycle_ts REAL,
            condition_id TEXT, cohort INTEGER, action TEXT, reason TEXT,
            daily_rate REAL, max_spread REAL, min_size INTEGER, midpoint_guess REAL,
            volume_24h REAL, expected_q_share REAL, q_share_source TEXT,
            expected_daily_reward REAL, target_shares INTEGER, target_capital REAL,
            target_queue_usd REAL, hours_to_resolution REAL,
            end_date_iso TEXT, game_start_time TEXT, question TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cf_cycle ON candidate_features(cycle_ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cf_cid ON candidate_features(condition_id)")
    # Forward-only migration for existing DBs: add columns introduced after A3 launch.
    for col, dtype in (
        ("volume_24h", "REAL"),
        ("target_queue_usd", "REAL"),
        ("hours_to_resolution", "REAL"),
    ):
        try:
            conn.execute(f"ALTER TABLE candidate_features ADD COLUMN {col} {dtype}")
        except sqlite3.OperationalError:
            pass  # column already exists


def append(records: list[dict], db_path: str = DEFAULT_DB, cycle_ts: float | None = None) -> int:
    """Append candidate feature records to the isolated DB. Returns rows written.

    Caller wraps this in try/except too (fail-quiet); this also closes the connection cleanly on
    any error. `question` is stored as DATA (never executed; offline analysis treats it as a feature).
    """
    if not records:
        return 0
    now = time.time()
    cts = cycle_ts if cycle_ts is not None else now
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        ensure_schema(conn)
        rows = [
            (now, cts, r.get("condition_id"), r.get("cohort"), r.get("action"), r.get("reason"),
             r.get("daily_rate"), r.get("max_spread"), r.get("min_size"), r.get("midpoint_guess"),
             r.get("volume_24h"), r.get("expected_q_share"), r.get("q_share_source"),
             r.get("expected_daily_reward"), r.get("target_shares"), r.get("target_capital"),
             r.get("target_queue_usd"), r.get("hours_to_resolution"), r.get("end_date_iso"),
             r.get("game_start_time"), r.get("question"))
            for r in records
        ]
        conn.executemany(
            f"INSERT INTO candidate_features ({','.join(_INSERT_COLS)}) "
            f"VALUES ({','.join('?' * len(_INSERT_COLS))})",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def log_result(result, db_path: str = DEFAULT_DB) -> int:
    """Persist an AllocationResult's candidate_features (no-op if empty/absent). Returns rows written.

    The single entry point the oversight loop calls — keeps the wiring testable and the call site a
    one-liner. Never raises on an empty/missing field; the caller still wraps in try/except (fail-quiet).
    """
    recs = getattr(result, "candidate_features", None)
    if not recs:
        return 0
    return append(recs, db_path=db_path)
