"""
reward_snapshot.py — per-market reward collector (data-foundation fix).

Why this exists: Polymarket pays liquidity rewards as a single daily lump, and
the bot only ever persisted that aggregate (`daily_reward_cache.__TOTAL__`). So
per-market reward — the half you need to know which markets are net-good vs
net-bad — was being thrown away. The authenticated `/rewards/user/markets`
endpoint DOES expose a per-market `earnings` figure (verified 2026-06-10:
current-day accrual that resets ~00:00 UTC). This script snapshots it so we
build the per-market reward history going forward.

Design (operator-approved):
  - Reads the CLOB rewards endpoint (signed GET — same L2 auth the bot uses).
  - Writes ONLY to a SEPARATE reward_snapshots.db. It never opens or writes
    bot_history.db, so it cannot affect live trading state in any way.
  - Stores raw timestamped snapshots; per-market DAILY reward is later derived
    as the pre-reset max per (date, condition_id) — robust whether `earnings`
    is daily-accrual or cumulative (we keep ts either way).
  - Idempotent to re-runs (each run just appends a snapshot row).
  - Reversible: delete reward_snapshots.db and the timer; no bot code touched.

Usage:
  python3 reward_snapshot.py --dry-run        # fetch + print, write nothing
  python3 reward_snapshot.py                  # fetch + append to reward_snapshots.db
  python3 reward_snapshot.py --report         # summarize per-market daily reward so far
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import sqlite3
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(_DIR, "reward_snapshots.db")
ENV_PATH = os.path.join(_DIR, ".env")
CLOB_HOST = "https://clob.polymarket.com"
REWARDS_PATH = "/rewards/user/markets"


# ---------------------------------------------------------------------------
# Auth (mirrors simple_allocator._auth_headers — L2 HMAC signing)
# ---------------------------------------------------------------------------
def auth_headers(method: str, path: str, secret: str, key: str, addr: str, passphrase: str) -> dict:
    ts = str(int(time.time()))
    msg = ts + method + path
    sig = base64.urlsafe_b64encode(
        hmac.new(base64.urlsafe_b64decode(secret), msg.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "POLY_API_KEY": key, "POLY_ADDRESS": addr, "POLY_SIGNATURE": sig,
        "POLY_PASSPHRASE": passphrase, "POLY_TIMESTAMP": ts,
    }


# ---------------------------------------------------------------------------
# Fetch (network) — paginated
# ---------------------------------------------------------------------------
def fetch_reward_markets(creds: dict, max_pages: int = 80, http=requests.get) -> list:
    """Return the raw per-market objects across all pages. Network errors raise."""
    items, cursor, pages = [], "", 0
    while pages < max_pages:
        params = {"signature_type": 2}
        if cursor:
            params["next_cursor"] = cursor
        # Retry transient network errors (the service died 2026-06-18 on a single CLOB ReadTimeout
        # with no retry). 3 attempts, exponential backoff, longer timeout.
        r = None
        for attempt in range(3):
            try:
                r = http(
                    CLOB_HOST + REWARDS_PATH, params=params,
                    headers=auth_headers("GET", REWARDS_PATH, **creds), timeout=30,
                )
                r.raise_for_status()
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)  # 1s, 2s
        j = r.json()
        items.extend(j.get("data", []) or [])
        cursor = j.get("next_cursor") or ""
        pages += 1
        if not cursor or cursor == "LTE=":  # LTE= is the API's end-of-list sentinel
            break
        time.sleep(0.1)  # be polite to the API
    return items


# ---------------------------------------------------------------------------
# Parse (pure — testable without network)
# ---------------------------------------------------------------------------
def market_earned_usd(item: dict) -> float:
    total = 0.0
    for e in item.get("earnings") or []:
        total += float(e.get("earnings") or 0) * float(e.get("asset_rate") or 1.0)
    return total


def _daily_rate(item: dict) -> float:
    return sum(float(c.get("rate_per_day") or 0) for c in (item.get("rewards_config") or []))


def extract(items: list, ts: float, min_earnings: float = 0.0) -> list:
    """Turn raw API items into snapshot records for markets with earnings > min."""
    date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    out = []
    for it in items:
        earned = market_earned_usd(it)
        if earned <= min_earnings:
            continue
        out.append({
            "ts": ts, "date": date,
            "condition_id": it.get("condition_id", ""),
            "question": (it.get("question") or "")[:120],
            "earnings_usd": round(earned, 6),
            "daily_rate": _daily_rate(it),
            "earning_percentage": float(it.get("earning_percentage") or 0),
        })
    return out


# ---------------------------------------------------------------------------
# Write (separate DB — never bot_history.db)
# ---------------------------------------------------------------------------
def ensure_schema(db_path: str):
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS reward_snapshots ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, date TEXT NOT NULL, "
            "condition_id TEXT NOT NULL, question TEXT, earnings_usd REAL NOT NULL, "
            "daily_rate REAL, earning_percentage REAL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rs_date_cid ON reward_snapshots(date, condition_id)")
        conn.commit()
    finally:
        conn.close()


def write_snapshots(db_path: str, records: list) -> int:
    if not records:
        return 0
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.executemany(
            "INSERT INTO reward_snapshots (ts, date, condition_id, question, "
            "earnings_usd, daily_rate, earning_percentage) VALUES "
            "(:ts, :date, :condition_id, :question, :earnings_usd, :daily_rate, :earning_percentage)",
            records,
        )
        conn.commit()
        return len(records)
    finally:
        conn.close()


def _post_discord(text: str):
    load_dotenv(ENV_PATH)
    url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not url:
        return False, "DISCORD_WEBHOOK_URL not set"
    content = text if len(text) <= 1900 else text[:1890] + "\n…(truncated)"
    try:
        r = requests.post(url, json={"content": content}, timeout=5)
        r.raise_for_status()
        return True, "posted"
    except Exception as e:
        return False, f"discord post failed: {e}"


def report(db_path: str, days: int = 14) -> str:
    """Per-market daily reward derived as the pre-reset max per (date, condition_id)."""
    if not os.path.exists(db_path):
        return "no reward_snapshots.db yet — run the collector first."
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    L = ["# Per-market daily reward (max snapshot per day)"]
    try:
        rows = conn.execute(
            "SELECT date, condition_id, MAX(earnings_usd) e, MAX(question) q "
            "FROM reward_snapshots GROUP BY date, condition_id"
        ).fetchall()
    finally:
        conn.close()
    by_market: dict = {}
    by_day: dict = {}
    for date, cid, e, q in rows:
        by_market.setdefault(cid, {"q": q, "total": 0.0})
        by_market[cid]["total"] += e
        by_day[date] = by_day.get(date, 0.0) + e
    L.append(f"days observed: {len(by_day)} · markets seen: {len(by_market)}")
    for d in sorted(by_day)[-days:]:
        L.append(f"  {d}: ${by_day[d]:.2f}")
    L.append("\ntop earners (sum of daily maxes):")
    for cid, v in sorted(by_market.items(), key=lambda kv: kv[1]["total"], reverse=True)[:12]:
        L.append(f"  ${v['total']:.2f}  {cid[:12]}  {(v['q'] or '')[:44]}")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Per-market reward collector (writes a separate DB).")
    ap.add_argument("--db", default=DEFAULT_DB, help="separate snapshot DB (never bot_history.db)")
    ap.add_argument("--dry-run", action="store_true", help="fetch + print, write nothing")
    ap.add_argument("--report", action="store_true", help="summarize collected per-market daily reward")
    ap.add_argument("--post", action="store_true", help="with --report, also send the summary to Discord")
    ap.add_argument("--max-pages", type=int, default=80)
    args = ap.parse_args()

    if args.report:
        rep = report(args.db)
        print(rep)
        if args.post:
            ok, msg = _post_discord(rep)
            print(f"[discord] {msg}")
        return

    load_dotenv(ENV_PATH)
    creds = {
        "secret": os.getenv("CLOB_SECRET"), "key": os.getenv("CLOB_API_KEY"),
        "addr": os.getenv("WALLET_ADDRESS"), "passphrase": os.getenv("CLOB_PASS_PHRASE"),
    }
    if not all(creds.values()):
        raise SystemExit("missing CLOB creds in .env (CLOB_SECRET/CLOB_API_KEY/WALLET_ADDRESS/CLOB_PASS_PHRASE)")

    ts = time.time()
    try:
        items = fetch_reward_markets(creds, max_pages=args.max_pages)
    except Exception as e:
        # Persistent failure after retries: exit CLEANLY so systemd doesn't mark the unit failed;
        # the next scheduled run retries. (A crash here is what left the service in 'failed' state.)
        print(f"[reward_snapshot] fetch failed after retries: {type(e).__name__}: {e} — "
              f"exiting cleanly, will retry next run")
        return
    records = extract(items, ts, min_earnings=0.0)
    total = sum(r["earnings_usd"] for r in records)
    when = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"{when} · scanned {len(items)} markets · {len(records)} with earnings>0 · total ${total:.4f}")
    for r in sorted(records, key=lambda r: r["earnings_usd"], reverse=True)[:12]:
        print(f"  ${r['earnings_usd']:.4f}  {r['condition_id'][:12]}  {r['question'][:44]}")

    if args.dry_run:
        print("[dry-run] nothing written.")
    else:
        n = write_snapshots(args.db, records)
        print(f"[written] {n} rows -> {args.db}")


if __name__ == "__main__":
    main()
