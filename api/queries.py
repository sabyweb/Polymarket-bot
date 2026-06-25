"""Read-only queries for the dashboard backend.

All connections use SQLite URI mode=ro.  Queries are defensive: missing tables
or rows return empty results rather than raising.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

_DIR = Path(__file__).parent.parent.resolve()
DB_PATH = _DIR / "bot_history.db"
CF_PATH = _DIR / "candidate_features.db"
ALLOC_PATH = _DIR / "market_allocations.json"
ENV_PATH = _DIR / ".env"
CONFIG_PATH = _DIR / "config.py"
OVERRIDES_PATH = _DIR / "config_overrides.json"

FUNDER = ""
if ENV_PATH.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
        FUNDER = os.getenv("FUNDER", "")
    except Exception:
        pass

DATA_API = "https://data-api.polymarket.com"


def _conn(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _ts_ago(unix_ts: float | None) -> str | None:
    if unix_ts is None:
        return None
    try:
        delta = time.time() - float(unix_ts)
    except (TypeError, ValueError):
        return None
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{delta / 3600:.1f}h ago"
    return f"{delta / 86400:.1f}d ago"


def _ts_str(unix_ts: float | None) -> str | None:
    if unix_ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(unix_ts), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (TypeError, ValueError):
        return None


def cohort_for(cid: str, n: int = 3) -> int:
    if n <= 1:
        return 0
    return int(hashlib.sha1(cid.encode("utf-8")).hexdigest(), 16) % n


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
def get_health() -> dict:
    kv = {}
    heartbeats = {}
    try:
        conn = _conn(DB_PATH)
        cur = conn.cursor()
        for k, v in cur.execute(
            "SELECT key, value FROM reward_tracker_state WHERE key LIKE 'heartbeat:%'"
        ).fetchall():
            name = k.split(":", 1)[1] if ":" in k else k
            try:
                heartbeats[name] = float(v)
            except Exception:
                heartbeats[name] = None
        kv = {
            r[0]: r[1]
            for r in cur.execute(
                "SELECT key, value FROM reward_tracker_state WHERE key IN "
                "('usdc_balance', 'usdc_balance_at', 'bot_start')"
            ).fetchall()
        }
        conn.close()
    except Exception:
        pass

    def scalar(q: str):
        try:
            conn = _conn(DB_PATH)
            row = conn.execute(q).fetchone()
            conn.close()
            return row[0] if row else None
        except Exception:
            return None

    safety = {}
    try:
        conn = _conn(DB_PATH)
        row = conn.execute(
            "SELECT ts, state, reason, consecutive_good FROM safety_state ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            safety = {"ts": row[0], "state": row[1], "reason": row[2]}
    except Exception:
        pass

    db_size = DB_PATH.stat().st_size / (1024 * 1024) if DB_PATH.exists() else 0.0
    db_mtime = DB_PATH.stat().st_mtime if DB_PATH.exists() else None

    return {
        "safety_state": safety.get("state"),
        "safety_reason": safety.get("reason"),
        "safety_since": _ts_ago(safety.get("ts")),
        "last_fill": _ts_ago(scalar("SELECT MAX(ts) FROM fills")),
        "last_order": _ts_ago(scalar("SELECT MAX(ts) FROM orders_placed")),
        "last_cycle": _ts_ago(scalar("SELECT MAX(ts) FROM cycle_snapshots")),
        "last_agent": _ts_ago(scalar("SELECT MAX(ts) FROM market_performance")),
        "active_orders": scalar("SELECT COUNT(*) FROM active_orders") or 0,
        "active_dumps": scalar("SELECT COUNT(*) FROM dump_states") or 0,
        "usdc_balance": kv.get("usdc_balance"),
        "bot_start": _ts_str(kv.get("bot_start")),
        "heartbeats": {k: _ts_ago(v) for k, v in heartbeats.items()},
        "db_size_mb": round(db_size, 2),
        "db_updated": _ts_ago(db_mtime),
    }


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------
def get_portfolio() -> dict:
    cash = 0.0
    try:
        conn = _conn(DB_PATH)
        row = conn.execute(
            "SELECT value FROM reward_tracker_state WHERE key='usdc_balance'"
        ).fetchone()
        conn.close()
        if row:
            cash = float(row[0] or 0)
    except Exception:
        pass

    inventory_value = 0.0
    unrealized = 0.0
    num_positions = 0
    if FUNDER:
        try:
            resp = requests.get(
                f"{DATA_API}/positions",
                params={"user": FUNDER, "sizeThreshold": "0.1"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json() or []
            num_positions = len(data)
            inventory_value = sum(float(p.get("currentValue") or 0) for p in data)
            unrealized = sum(float(p.get("cashPnl") or 0) for p in data)
        except Exception:
            pass

    total = cash + inventory_value
    drawdown = None
    try:
        conn = _conn(DB_PATH)
        rows = conn.execute(
            "SELECT total_position_usd, realized_pnl FROM hourly_snapshots ORDER BY ts DESC LIMIT 336"
        ).fetchall()
        conn.close()
        if rows:
            peaks = [(pos + pnl) for pos, pnl in rows]
            peak = max(peaks)
            if peak > 0:
                drawdown = (peak - total) / peak
    except Exception:
        pass

    return {
        "cash": cash,
        "inventory_value": inventory_value,
        "unrealized": unrealized,
        "total": total,
        "num_positions": num_positions,
        "drawdown_pct": drawdown,
    }


# ---------------------------------------------------------------------------
# P&L
# ---------------------------------------------------------------------------
def get_pnl_summary() -> dict:
    try:
        conn = _conn(DB_PATH)
        cur = conn.cursor()
        realized = cur.execute("SELECT COALESCE(SUM(pnl), 0) FROM unwinds").fetchone()[0] or 0
        stop_loss = cur.execute("SELECT COALESCE(SUM(loss_usd), 0) FROM stop_losses").fetchone()[0] or 0
        num_fills = cur.execute("SELECT COUNT(*) FROM fills").fetchone()[0] or 0
        num_unwinds = cur.execute("SELECT COUNT(*) FROM unwinds").fetchone()[0] or 0
        num_stops = cur.execute("SELECT COUNT(*) FROM stop_losses").fetchone()[0] or 0
        conn.close()
    except Exception:
        return {"realized_pnl": 0, "stop_loss_total": 0, "net": 0, "num_fills": 0, "num_unwinds": 0, "num_stops": 0}
    return {
        "realized_pnl": float(realized),
        "stop_loss_total": float(stop_loss),
        "net": float(realized) - float(stop_loss),
        "num_fills": int(num_fills),
        "num_unwinds": int(num_unwinds),
        "num_stops": int(num_stops),
    }


def get_daily_pnl(days: int = 14) -> list[dict]:
    try:
        conn = _conn(DB_PATH)
        cur = conn.cursor()
        rows = cur.execute(
            """SELECT date(ts, 'unixepoch') as day,
                      SUM(CASE WHEN pnl >= 0 THEN pnl ELSE 0 END) as gains,
                      SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) as losses,
                      SUM(pnl) as net_pnl,
                      COUNT(*) as unwind_count
               FROM unwinds GROUP BY day ORDER BY day DESC LIMIT ?""",
            (days,),
        ).fetchall()
        conn.close()
        return [
            {"day": r[0], "gains": r[1], "losses": r[2], "net_pnl": r[3], "unwind_count": r[4]}
            for r in rows
        ]
    except Exception:
        return []


def get_daily_fills(days: int = 14) -> list[dict]:
    try:
        conn = _conn(DB_PATH)
        cur = conn.cursor()
        rows = cur.execute(
            """SELECT date(ts, 'unixepoch') as day,
                      COUNT(*) as fill_count,
                      SUM(usd_value) as total_usd,
                      AVG(slippage) as avg_slippage
               FROM fills GROUP BY day ORDER BY day DESC LIMIT ?""",
            (days,),
        ).fetchall()
        conn.close()
        return [
            {"day": r[0], "fill_count": r[1], "total_usd": r[2], "avg_slippage": r[3]}
            for r in rows
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Fills & Unwinds
# ---------------------------------------------------------------------------
def get_recent_fills(limit: int = 50, since: float | None = None) -> list[dict]:
    try:
        conn = _conn(DB_PATH)
        cur = conn.cursor()
        if since:
            rows = cur.execute(
                """SELECT ts, condition_id, question, side, shares, price, usd_value, slippage, fill_type
                   FROM fills WHERE ts >= ? ORDER BY ts DESC LIMIT ?""",
                (since, limit),
            ).fetchall()
        else:
            rows = cur.execute(
                """SELECT ts, condition_id, question, side, shares, price, usd_value, slippage, fill_type
                   FROM fills ORDER BY ts DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        conn.close()
        return [
            {
                "ts": r[0],
                "time": _ts_str(r[0]),
                "condition_id": r[1],
                "question": r[2],
                "side": r[3],
                "shares": r[4],
                "price": r[5],
                "usd_value": r[6],
                "slippage": r[7],
                "fill_type": r[8],
                "cohort": cohort_for(r[1]),
            }
            for r in rows
        ]
    except Exception:
        return []


def get_recent_unwinds(limit: int = 50) -> list[dict]:
    try:
        conn = _conn(DB_PATH)
        cur = conn.cursor()
        rows = cur.execute(
            """SELECT ts, condition_id, question, side, shares, sell_price, usd_value, pnl,
                      hold_duration_secs, unwind_type
               FROM unwinds ORDER BY ts DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        conn.close()
        return [
            {
                "ts": r[0],
                "time": _ts_str(r[0]),
                "condition_id": r[1],
                "question": r[2],
                "side": r[3],
                "shares": r[4],
                "sell_price": r[5],
                "usd_value": r[6],
                "pnl": r[7],
                "hold_hours": round((r[8] or 0) / 3600, 1),
                "unwind_type": r[9],
            }
            for r in rows
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
def get_active_orders() -> list[dict]:
    try:
        conn = _conn(DB_PATH)
        cur = conn.cursor()
        rows = cur.execute(
            """SELECT condition_id, side, order_type, price, shares, placed_at
               FROM active_orders ORDER BY placed_at DESC"""
        ).fetchall()
        conn.close()
        return [
            {
                "condition_id": r[0],
                "side": r[1],
                "order_type": r[2],
                "price": r[3],
                "shares": r[4],
                "notional": round(r[3] * r[4], 2),
                "placed_at": _ts_str(r[5]),
            }
            for r in rows
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------
def get_positions() -> list[dict]:
    if not FUNDER:
        return []
    try:
        resp = requests.get(
            f"{DATA_API}/positions",
            params={"user": FUNDER, "sizeThreshold": "0.1"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json() or []
        return [
            {
                "market": p.get("title", "")[:55],
                "side": p.get("outcome", ""),
                "shares": round(float(p.get("size") or 0), 2),
                "avg": round(float(p.get("avgPrice") or 0), 4),
                "now": round(float(p.get("curPrice") or 0), 4),
                "value": round(float(p.get("currentValue") or 0), 2),
                "pnl": round(float(p.get("cashPnl") or 0), 2),
                "pnl_pct": round(float(p.get("percentPnl") or 0), 1),
                "expires": p.get("endDate"),
            }
            for p in data
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Cohorts
# ---------------------------------------------------------------------------
def get_cohort_history(days: int = 2) -> list[dict]:
    try:
        conn = _conn(DB_PATH)
        cur = conn.cursor()
        cutoff = time.time() - days * 86400
        rows = cur.execute(
            """SELECT window_end_ts, cohort, reward_earned, unwind_pnl, net_pnl,
                      fill_count, deployed_markets, target_capital
               FROM cohort_pnl WHERE window_end_ts >= ? ORDER BY window_end_ts DESC""",
            (cutoff,),
        ).fetchall()
        conn.close()
        return [
            {
                "ts": r[0],
                "time": _ts_str(r[0]),
                "cohort": r[1],
                "reward_earned": r[2],
                "unwind_pnl": r[3],
                "net_pnl": r[4],
                "fill_count": r[5],
                "deployed_markets": r[6],
                "target_capital": r[7],
            }
            for r in rows
        ]
    except Exception:
        return []


def get_cohort_latest() -> list[dict]:
    history = get_cohort_history(days=2)
    if not history:
        return []
    latest_ts = max(r["ts"] for r in history)
    out = []
    for r in history:
        if r["ts"] == latest_ts:
            tc = r["target_capital"] or 1
            out.append(
                {
                    **r,
                    "return_pct": round(r["net_pnl"] / tc * 100, 3),
                }
            )
    return sorted(out, key=lambda x: x["cohort"])


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------
def get_allocation() -> dict:
    try:
        with open(ALLOC_PATH) as f:
            data = json.load(f)
    except Exception:
        return {"num_deploy": 0, "num_avoid": 0, "total_capital_deployed": 0, "generated_at": "", "deploys": []}
    deploys = [m for m in data.get("markets", []) if m.get("action") == "deploy"]
    return {
        "num_deploy": data.get("num_deploy", len(deploys)),
        "num_avoid": data.get("num_avoid", 0),
        "total_capital_deployed": data.get("total_capital_deployed", 0),
        "generated_at": data.get("generated_at", ""),
        "deploys": deploys,
    }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def get_config() -> list[dict]:
    """Return effective config for all RF_* keys by reusing config.py logic."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("config", CONFIG_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        overrides = {}
        if OVERRIDES_PATH.exists():
            with open(OVERRIDES_PATH) as f:
                overrides = json.load(f) or {}
        rows = []
        for key in dir(mod):
            if not key.startswith("RF_"):
                continue
            default = getattr(mod, key)
            override = overrides.get(key)
            effective = override if override is not None else default
            rows.append(
                {
                    "key": key,
                    "default_value": default,
                    "override_value": override,
                    "effective_value": effective,
                    "overridden": override is not None,
                }
            )
        return sorted(rows, key=lambda x: x["key"])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
def get_logs(service: str = "polymarket-farmer", lines: int = 100) -> list[dict]:
    try:
        out = subprocess.run(
            ["journalctl", "-u", f"{service}.service", "-n", str(lines), "--no-pager", "-q"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        entries = []
        for line in out.stdout.splitlines():
            if not line.strip():
                continue
            # Best-effort parse: "Jun 25 11:00:00 host process[level]: message"
            parts = line.split(None, 4)
            if len(parts) >= 5:
                ts = " ".join(parts[:3])
                rest = parts[4]
                level = "INFO"
                msg = rest
                if " - " in rest:
                    proc, msg = rest.split(" - ", 1)
                    for lvl in ["ERROR", "WARNING", "WARN", "INFO", "DEBUG"]:
                        if f"{lvl.lower()} -" in line.lower() or f"{lvl} -" in line:
                            level = lvl
                            break
                else:
                    proc = ""
                entries.append({"service": service, "ts": ts, "level": level, "message": msg})
            else:
                entries.append({"service": service, "ts": "", "level": "INFO", "message": line})
        return entries
    except Exception:
        return []
