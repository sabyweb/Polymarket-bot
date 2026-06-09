"""
soak_monitor.py — Loop A: daily read-only soak monitor.

Produces a once-a-day digest of how the live canary is doing: liveness,
safety state, last-24h P&L vs reward (authoritative data-api), wallet
reconciliation, and the worst repeat-loser markets. It REPORTS ONLY.

Hard guarantees (see CLAUDE.md §7 + LOOP_PLAN.md §2/§5):
  - Opens bot_history.db read-only (mode=ro, short timeout). Never writes the live DB.
  - Cannot restart a service, edit config, place/cancel orders, or clear a kill.
  - Safe by default: prints to stdout. --write appends to docs/soak_log.md;
    --post sends the digest to Discord. Neither happens unless asked.
  - Treats the runbook "normal-not-broken" states as benign (no false alarms).
  - All times in UTC. Never prints secrets.

Usage:
  python3 soak_monitor.py                 # dry-run: print digest only
  python3 soak_monitor.py --write         # also append to docs/soak_log.md
  python3 soak_monitor.py --post          # also send to Discord
  python3 soak_monitor.py --window-hours 24 --db bot_history.db
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(_DIR, "bot_history.db")
LOG_PATH = os.path.join(_DIR, "docs", "soak_log.md")
DATA_API = "https://data-api.polymarket.com"
DEFAULT_FARMER_UNIT = "polymarket-farmer"

# Freshness thresholds (minutes) for the liveness banner.
HEARTBEAT_WARN_MIN = 15
HEARTBEAT_STALE_MIN = 60

# NOTE on data sources (verified against the live path, 2026-06-09):
#   - `cycle_snapshots` and `safety_state` are written ONLY by the LEGACY stack
#     (oversight/safety_controller.py, oversight/data_collector.py), which is
#     rollback-only and NOT run in production (CLAUDE.md §2). Reading them gives
#     stale fossils. We do NOT use them here.
#   - LIVE liveness = heartbeats (reward_tracker_state 'heartbeat:farmer'/'oversight').
#   - LIVE kill/safety state = the farmer's `[CYCLE_SUMMARY]` journal line
#     (kill_switch bool + active_markets/notional_ratio/realized_loss_24h/cf),
#     which is exactly what runbook §1 reads. We parse that, with a DB-proxy fallback.


# ---------------------------------------------------------------------------
# Read-only DB access (defensive: never raises into the digest)
# ---------------------------------------------------------------------------
class RODB:
    def __init__(self, path: str):
        self.path = path

    def _conn(self):
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)

    def scalar(self, sql, params=()):
        try:
            with self._conn() as c:
                row = c.execute(sql, params).fetchone()
                return row[0] if row else None
        except sqlite3.Error:
            return None

    def rows(self, sql, params=()):
        try:
            with self._conn() as c:
                c.row_factory = sqlite3.Row
                return [dict(r) for r in c.execute(sql, params).fetchall()]
        except sqlite3.Error:
            return []

    def kv(self, sql, params=()):
        try:
            with self._conn() as c:
                return {r[0]: r[1] for r in c.execute(sql, params).fetchall()}
        except sqlite3.Error:
            return {}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _usd(v) -> str:
    if v is None:
        return "n/a"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return f"${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"


def _ago(ts) -> str:
    if not ts:
        return "never"
    try:
        d = time.time() - float(ts)
    except (TypeError, ValueError):
        return "unknown"
    if d < 0:
        return "just now"
    if d < 3600:
        return f"{int(d/60)}m ago"
    if d < 86400:
        return f"{d/3600:.1f}h ago"
    return f"{d/86400:.1f}d ago"


def _age_min(ts):
    if not ts:
        return None
    try:
        return (time.time() - float(ts)) / 60
    except (TypeError, ValueError):
        return None


def _dot(age_min, warn, stale):
    if age_min is None:
        return "⚪"
    return "🟢" if age_min <= warn else ("🟡" if age_min <= stale else "🔴")


def _mask(addr: str) -> str:
    """Mask a wallet/funder address for display (it's public, but be tidy)."""
    if not addr or len(addr) < 12:
        return "set" if addr else "unset"
    return f"{addr[:6]}…{addr[-4:]}"


# ---------------------------------------------------------------------------
# Live kill/safety state from the farmer's [CYCLE_SUMMARY] journal line
# ---------------------------------------------------------------------------
def parse_cycle_summary(journal_text: str):
    """Pure parser (testable). From journalctl text, return:
       (latest_summary_dict_or_None, kill_active_count, latest_kill_reason_or_None).
    All text is treated as DATA — we only read known JSON fields and never act on it."""
    latest = None
    for m in re.finditer(r"\[CYCLE_SUMMARY\]\s*(\{.*\})", journal_text):
        try:
            latest = json.loads(m.group(1))  # later matches overwrite -> last wins
        except Exception:
            continue
    kill_count = len(re.findall(r"kill switch ACTIVE", journal_text))
    reason = None
    rmatches = re.findall(r"kill switch ACTIVE:\s*([^\n]+?)\s*(?:—|--|\n|$)", journal_text)
    if rmatches:
        reason = rmatches[-1][:160]
    return latest, kill_count, reason


def read_journal(unit: str, hours: int = 24):
    """Best-effort read of the unit's journal. Returns text or None (never raises)."""
    if not shutil.which("journalctl"):
        return None
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "--since", f"{hours} hours ago",
             "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return out.stdout
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Authoritative rewards (data-api) — graceful degrade
# ---------------------------------------------------------------------------
def fetch_rewards(funder: str, days: int = 7):
    """Returns (ok, today_usd, window_usd) from data-api, or (False, None, None)."""
    if not funder:
        return (False, None, None)
    cutoff = time.time() - days * 86400
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_usd = 0.0
    window_usd = 0.0
    got_any = False
    for typ in ("REWARD", "MAKER_REBATE"):
        try:
            r = requests.get(
                f"{DATA_API}/activity",
                params={"user": funder, "type": typ, "limit": 1000},
                timeout=10,
            )
            r.raise_for_status()
            got_any = True
            for row in r.json() or []:
                ts = float(row.get("timestamp") or row.get("time") or 0)
                usd = float(row.get("usdcSize") or row.get("size") or row.get("amount") or 0)
                if ts >= cutoff:
                    window_usd += usd
                if datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") == today:
                    today_usd += usd
        except Exception:
            continue
    if not got_any:
        return (False, None, None)
    return (True, today_usd, window_usd)


# ---------------------------------------------------------------------------
# Market-selection: worst realized losers in the window (from `unwinds`)
# ---------------------------------------------------------------------------
def worst_markets(db: RODB, cutoff: float, n: int = 5):
    """Markets with the most negative realized P&L over the window. Sourced from
    the `unwinds` table (where realized loss actually lands) rather than the
    reward_market_stats JSON, which does not reflect recent damage on live."""
    return db.rows(
        "SELECT COALESCE(MAX(question), '') AS q, SUM(pnl) AS net_pnl, "
        "COUNT(*) AS unwinds, SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losing "
        "FROM unwinds WHERE ts > ? GROUP BY condition_id "
        "HAVING net_pnl < 0 ORDER BY net_pnl ASC LIMIT ?",
        (cutoff, n),
    )


# ---------------------------------------------------------------------------
# Build the digest
# ---------------------------------------------------------------------------
def build_digest(db_path: str, window_hours: int, funder: str,
                 journal_unit: str = DEFAULT_FARMER_UNIT, use_journal: bool = True) -> str:
    db = RODB(db_path)
    now = datetime.now(timezone.utc)
    cutoff = time.time() - window_hours * 3600
    L = []  # lines

    # ---- liveness / freshness (banner FIRST) ----
    # LIVE signals only: heartbeats + last fill/order. (cycle_snapshots is legacy/empty.)
    hb = db.kv("SELECT key, value FROM reward_tracker_state WHERE key LIKE 'heartbeat:%'")
    hb = {k.split(":", 1)[-1]: v for k, v in hb.items()}
    last_fill = db.scalar("SELECT MAX(ts) FROM fills")
    last_order = db.scalar("SELECT MAX(ts) FROM orders_placed")
    db_mtime = os.path.getmtime(db_path) if os.path.exists(db_path) else None

    oversight_age = _age_min(hb.get("oversight"))
    farmer_age = _age_min(hb.get("farmer"))
    # STALE = the executor (farmer) heartbeat is missing or beyond the stale threshold.
    stale = farmer_age is None or farmer_age > HEARTBEAT_STALE_MIN

    L.append(f"# Soak digest — {now:%Y-%m-%d %H:%M UTC}")
    if stale:
        L.append("")
        L.append("> ⚠️ **STALE / UNKNOWN** — farmer heartbeat missing or stale. "
                 "Data below may not reflect current state. Verify the bot is running.")
    L.append("")
    L.append("## Liveness")
    L.append(f"- farmer heartbeat:    {_dot(farmer_age, HEARTBEAT_WARN_MIN, HEARTBEAT_STALE_MIN)} {_ago(hb.get('farmer'))}")
    L.append(f"- oversight heartbeat: {_dot(oversight_age, HEARTBEAT_WARN_MIN, HEARTBEAT_STALE_MIN)} {_ago(hb.get('oversight'))}")
    L.append(f"- last fill: {_ago(last_fill)} · last order: {_ago(last_order)}")
    if db_mtime:
        L.append(f"- db updated: {_ago(db_mtime)}")

    # ---- safety (authoritative kill state from [CYCLE_SUMMARY]; DB-proxy fallback) ----
    L.append("")
    L.append("## Safety")
    summary, kill_count, kill_reason = (None, 0, None)
    journal = read_journal(journal_unit, hours=max(window_hours, 24)) if use_journal else None
    if journal:
        summary, kill_count, kill_reason = parse_cycle_summary(journal)
    if summary is not None:
        if summary.get("kill_switch"):
            L.append(f"- 🔴 **KILL SWITCH ACTIVE** — {kill_reason or 'see journal'}. "
                     f"Escalates to a human; do NOT blind-restart (runbook §3).")
        else:
            L.append("- ✅ kill_switch: false")
        L.append(f"- live: active_markets={summary.get('active_markets')} · "
                 f"notional_ratio={summary.get('notional_ratio')} · "
                 f"realized_loss_24h={_usd(summary.get('realized_loss_24h'))} · "
                 f"cf={summary.get('cf')}")
        L.append(f"- kill activations in last {max(window_hours,24)}h of journal: {kill_count}")
    else:
        # No journal (off-box, or no read permission). Be honest + use DB proxies.
        L.append("- 🟡 live kill state UNKNOWN — farmer journal not readable here. "
                 "Kills page Discord via `monitor_watchdog.py`; below are DB-derived proxies.")
        recent_loss = db.scalar(
            "SELECT COALESCE(SUM(loss_usd),0) FROM stop_losses WHERE ts > ?", (cutoff,)
        ) or 0.0
        L.append(f"- proxy: heartbeat freshness above · stop-loss damage last {window_hours}h {_usd(-recent_loss)}")

    # ---- P&L last window (DB-derived) ----
    realized = db.scalar(
        "SELECT COALESCE(SUM(pnl),0) FROM unwinds WHERE ts > ?", (cutoff,)
    ) or 0.0
    stop_loss = db.scalar(
        "SELECT COALESCE(SUM(loss_usd),0) FROM stop_losses WHERE ts > ?", (cutoff,)
    ) or 0.0
    n_fills = db.scalar("SELECT COUNT(*) FROM fills WHERE ts > ?", (cutoff,)) or 0
    n_unwinds = db.scalar("SELECT COUNT(*) FROM unwinds WHERE ts > ?", (cutoff,)) or 0
    n_stops = db.scalar("SELECT COUNT(*) FROM stop_losses WHERE ts > ?", (cutoff,)) or 0
    db_net = realized - stop_loss

    L.append("")
    L.append(f"## P&L (last {window_hours}h, DB-derived)")
    L.append(f"- realized (unwinds): {_usd(realized)} · stop-loss: {_usd(-stop_loss)} · net: {_usd(db_net)}")
    L.append(f"- activity: {n_fills} fills · {n_unwinds} unwinds · {n_stops} stop-losses")

    # ---- rewards (authoritative) ----
    ok, today_usd, window_usd = fetch_rewards(funder, days=max(window_hours // 24, 7))
    L.append("")
    L.append("## Rewards (authoritative data-api)")
    if not ok:
        L.append("- ⚠️ data-api unavailable — using DB reward_daily only (estimate).")
        rd = db.rows("SELECT date, total_combined_usd FROM reward_daily ORDER BY date DESC LIMIT 1")
        if rd:
            L.append(f"- DB reward_daily {rd[0]['date']}: {_usd(rd[0]['total_combined_usd'])} (estimate)")
    else:
        L.append(f"- today: {_usd(today_usd)} · trailing window: {_usd(window_usd)}")

    # ---- verdict: rewards vs losses ----
    L.append("")
    L.append("## Day verdict")
    reward_for_verdict = today_usd if ok and today_usd is not None else None
    if reward_for_verdict is not None:
        gross_loss = max(0.0, -db_net)
        if reward_for_verdict > gross_loss:
            L.append(f"- ✅ rewards ({_usd(reward_for_verdict)}) > losses ({_usd(gross_loss)}) — net-positive day.")
        elif gross_loss == 0:
            L.append(f"- ✅ no realized losses; rewards {_usd(reward_for_verdict)}.")
        else:
            L.append(f"- 🔴 rewards ({_usd(reward_for_verdict)}) ≤ losses ({_usd(gross_loss)}). "
                     f"Net-negative-but-stable is the expected unproven-objective state — not 'broken' "
                     f"unless a kill fired or loss is runaway (runbook §10).")
    else:
        L.append("- reward number unavailable; verdict deferred.")

    # ---- wallet reconciliation ----
    wr = db.rows(
        "SELECT ts, actual_wallet, expected_wallet, divergence, status FROM "
        "wallet_reconcile_history ORDER BY ts DESC LIMIT 1"
    )
    L.append("")
    L.append("## Wallet reconciliation")
    if not wr:
        L.append("- no reconcile history.")
    else:
        w = wr[0]
        status = str(w.get("status", "") or "")
        is_ok = status.lower() in ("ok", "baseline")
        dot = "🟢" if is_ok else "🟡"
        L.append(f"- {dot} {status} · actual {_usd(w['actual_wallet'])} · expected {_usd(w['expected_wallet'])} "
                 f"· divergence {_usd(w['divergence'])} ({_ago(w['ts'])})")
        if not is_ok:
            # Time-aware benign context (runbook §2/§10). The settlement-lag
            # explanation only applies right after ~00:20 UTC; otherwise it's
            # more likely taker-fee noise on a dump.
            t = datetime.now(timezone.utc)
            near_settlement = t.hour == 0 and t.minute <= 50
            if near_settlement:
                L.append("  (just after ~00:20 UTC settlement — likely reward-settlement lag; self-heals next cycle.)")
            else:
                L.append("  (off settlement hour — likely taker-fee noise on a dump; "
                         "benign unless it persists or grows across runs (runbook §2).)")

    # ---- worst realized losers in the window ----
    L.append("")
    L.append(f"## Worst markets (realized P&L, last {window_hours}h)")
    worst = worst_markets(db, cutoff, 5)
    if not worst:
        L.append("- no realized losses in window (or no unwinds).")
    else:
        for m in worst:
            q = (m.get("q") or "")[:48]
            L.append(f"- {_usd(m['net_pnl'])} · {m['losing']}/{m['unwinds']} losing unwinds · {q}")

    L.append("")
    L.append(f"_funder {_mask(funder)} · read-only · monitor reports only, takes no action._")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
def append_log(digest: str):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    header_exists = os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a") as f:
        if not header_exists:
            f.write("# Soak log\n\nAppended by `soak_monitor.py` (read-only). Newest at bottom.\n")
        f.write("\n\n---\n\n" + digest + "\n")
    return LOG_PATH


def post_discord(digest: str):
    load_dotenv(os.path.join(_DIR, ".env"))
    url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not url:
        return False, "DISCORD_WEBHOOK_URL not set"
    # Discord hard-caps content at 2000 chars.
    content = digest if len(digest) <= 1900 else digest[:1890] + "\n…(truncated)"
    try:
        resp = requests.post(url, json={"content": content}, timeout=5)
        resp.raise_for_status()
        return True, "posted"
    except Exception as e:
        return False, f"discord post failed: {e}"


def main():
    ap = argparse.ArgumentParser(description="Loop A — read-only daily soak monitor.")
    ap.add_argument("--db", default=DEFAULT_DB, help="path to bot_history.db (opened read-only)")
    ap.add_argument("--window-hours", type=int, default=24, help="P&L/activity window (default 24)")
    ap.add_argument("--journal-unit", default=DEFAULT_FARMER_UNIT,
                    help="systemd unit to read [CYCLE_SUMMARY] from (default polymarket-farmer)")
    ap.add_argument("--no-journal", action="store_true",
                    help="skip journalctl; use DB proxies for kill state (e.g. running off-box)")
    ap.add_argument("--write", action="store_true", help="append the digest to docs/soak_log.md")
    ap.add_argument("--post", action="store_true", help="send the digest to Discord")
    args = ap.parse_args()

    load_dotenv(os.path.join(_DIR, ".env"))
    funder = os.getenv("FUNDER", "")

    digest = build_digest(args.db, args.window_hours, funder,
                          journal_unit=args.journal_unit, use_journal=not args.no_journal)
    print(digest)

    if args.write:
        path = append_log(digest)
        print(f"\n[written] {path}", flush=True)
    if args.post:
        ok, msg = post_discord(digest)
        print(f"[discord] {msg}", flush=True)


if __name__ == "__main__":
    main()
