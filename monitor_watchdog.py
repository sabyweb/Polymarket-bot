#!/usr/bin/env python3
"""30-minute health watchdog for the Polymarket reward-farmer (Helsinki).

Read-only. Pages the bot's Discord webhook on anomalies and re-pages every
run while a problem persists (the bot's own FX-092 kill page fires only once,
so a sticky-killed-and-idle farmer would otherwise go unnoticed for hours).

Design choices (deliberate):
  * ALERT-ONLY — it does NOT restart the bot. A kill switch is a *protective*
    state; auto-restarting into an adverse regime risks a capital-drain loop,
    so a kill escalates to a human instead of being masked.
  * FAIL-SAFE — if a signal can't be read, that is itself reported, never
    silently passed. The script never raises out of main().

Signals:
  - farmer / oversight systemd unit active
  - latest [CYCLE_SUMMARY] kill_switch flag + cycle freshness (stall/dead)
  - book_snapshots heartbeat freshness
  - wallet drawdown vs peak (portfolio_snapshots)
  - latest wallet-reconcile status (desync)

Usage:
  monitor_watchdog.py            # cron mode: post to Discord only on anomaly
  monitor_watchdog.py --ping     # also post a healthy status (used to arm it)
  monitor_watchdog.py --dry      # print, never post (for testing)

Install (Helsinki crontab): */30 * * * * cd /home/polymarket/Polymarket-bot && \
  venv/bin/python3 monitor_watchdog.py >> logs/watchdog.log 2>&1
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request

REPO = os.environ.get("PMBOT_REPO", "/home/polymarket/Polymarket-bot")
DB_URI = f"file:{REPO}/bot_history.db?mode=ro"
ENV_PATH = f"{REPO}/.env"

STALL_SECS = 360          # no cycle / no book snapshot in 6 min => stalled/dead
DRAWDOWN_FRAC = 0.12      # > 12% off peak => alert (kill fires at 10%/24h realized)


def _env(key: str) -> str:
    try:
        with open(ENV_PATH) as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _webhook() -> str:
    return _env("DISCORD_WEBHOOK_URL")


def _telegram(text: str) -> bool:
    """Best-effort Telegram push for critical pages (reliable mobile notification).
    No-op without TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID. Never raises."""
    token, chat = _env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    try:
        data = json.dumps({"chat_id": chat, "text": text[:4000],
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data,
            headers={"Content-Type": "application/json",
                     "User-Agent": "polymarket-watchdog/1.0"},
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:  # pragma: no cover - network
        print("telegram post FAILED:", e)
        return False


def _healthcheck_ping(dry: bool) -> None:
    """Dead-man's-switch: ping an external Healthchecks.io URL on EVERY completed
    run. This is the one failure an on-box monitor cannot self-report — if the VPS
    halts (power / kernel panic / network loss) or cron stops, the watchdog can't
    run, the ping stops arriving, and Healthchecks alerts the operator that the
    whole system went silent. Set HEALTHCHECK_URL in .env (e.g. https://hc-ping.com/<uuid>).
    No-op without it; never raises."""
    url = _env("HEALTHCHECK_URL")
    if not url:
        return
    if dry:
        print("DRY healthcheck ping ->", url)
        return
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "polymarket-watchdog/1.0"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # pragma: no cover - network
        print("healthcheck ping FAILED:", e)


def _post(msg: str, dry: bool, critical: bool = False) -> None:
    """Post a watchdog message. Critical anomalies push to Telegram (reliable
    mobile) AND the Discord critical channel with an @mention; healthy/ping posts
    go to the normal Discord channel only."""
    text = f"[WATCHDOG] {msg}"[:1900]
    if dry:
        tag = "DRY(critical) ->" if critical else "DRY ->"
        print(tag, text)
        return
    if critical:
        _telegram(text)
    crit_url = _env("DISCORD_CRITICAL_WEBHOOK_URL")
    url = (crit_url or _webhook()) if critical else _webhook()
    if not url:
        if not critical:
            print("no webhook configured; not posting:", text)
        return
    payload: dict = {"content": text}
    if critical:
        mention = _env("DISCORD_CRITICAL_MENTION") or "@here"
        payload["content"] = f"{mention} {text}"[:1900]
        # Webhooks suppress pings unless explicitly allowed.
        payload["allowed_mentions"] = {"parse": ["everyone", "roles", "users"]}
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={
                "Content-Type": "application/json",
                # Discord/Cloudflare 403s the default Python-urllib UA.
                "User-Agent": "polymarket-watchdog/1.0",
            },
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # pragma: no cover - network
        print("discord post FAILED:", e)


def _one(sql: str):
    try:
        conn = sqlite3.connect(DB_URI, uri=True)
        try:
            return conn.execute(sql).fetchone()
        finally:
            conn.close()
    except Exception as e:
        return ("__ERR__", str(e))


def _active(unit: str) -> str:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=10,
        )
        return (r.stdout or r.stderr or "?").strip()
    except Exception as e:
        return f"err:{e}"


def _latest_cycle_summary():
    """Return (dict, age_secs) for the most recent [CYCLE_SUMMARY], or (None, reason)."""
    try:
        r = subprocess.run(
            ["journalctl", "-u", "polymarket-farmer",
             "--since", "8 min ago", "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=20,
        )
        lines = [ln for ln in r.stdout.splitlines() if "CYCLE_SUMMARY" in ln]
        if not lines:
            return None, "no_cycle_summary_in_8min"
        # Log line is: "... | [CYCLE_SUMMARY] {json}". Slice the JSON object
        # itself ({ .. }) rather than split on the tag (which leaves a "]").
        ln = lines[-1]
        start, end = ln.find("{"), ln.rfind("}")
        if start < 0 or end < 0:
            return None, "cycle_summary_no_json"
        d = json.loads(ln[start:end + 1])
        age = time.time() - float(d.get("ts", time.time()))
        return d, age
    except Exception as e:
        return None, f"journal_err:{e}"


def main() -> int:
    dry = "--dry" in sys.argv
    ping = "--ping" in sys.argv
    now = time.time()
    problems = []
    info = []

    f_act = _active("polymarket-farmer")
    o_act = _active("polymarket-oversight")
    info.append(f"farmer={f_act} oversight={o_act}")
    if f_act != "active":
        problems.append(f"farmer systemd not active ({f_act})")

    d, age = _latest_cycle_summary()
    kill = None
    if d is None:
        problems.append(f"no recent CYCLE_SUMMARY ({age}) — farmer dead/hung?")
    else:
        kill = d.get("kill_switch")
        info.append(
            f"cycle={d.get('cycle')} kill={kill} mkts={d.get('active_markets')} "
            f"notional={d.get('total_live_notional')} loss24h={d.get('realized_loss_24h')} "
            f"cyc_age={age:.0f}s"
        )
        if kill is True:
            problems.append("KILL SWITCH ACTIVE — farmer idle, needs review")
        if age > STALL_SECS:
            problems.append(f"farmer stalled (last cycle {age:.0f}s ago)")

    row = _one("SELECT strftime('%s','now') - MAX(ts) FROM book_snapshots")
    if row and row[0] is not None and row[0] != "__ERR__":
        bage = float(row[0])
        info.append(f"book_age={bage:.0f}s")
        if bage > STALL_SECS and kill is not True:
            problems.append(f"book_snapshots stale ({bage:.0f}s)")
    elif row and row[0] == "__ERR__":
        problems.append(f"db read failed: {row[1]}")

    w = _one("SELECT actual_wallet, status FROM wallet_reconcile_history ORDER BY id DESC LIMIT 1")
    pk = _one("SELECT MAX(total_value) FROM portfolio_snapshots")
    if w and isinstance(w[0], (int, float)):
        wallet = float(w[0])
        status = w[1]
        peak = float(pk[0]) if pk and isinstance(pk[0], (int, float)) else wallet
        dd = (peak - wallet) / peak if peak > 0 else 0.0
        info.append(f"wallet=${wallet:.2f} peak=${peak:.2f} dd={dd * 100:.1f}% reconcile={status}")
        if dd > DRAWDOWN_FRAC:
            problems.append(f"drawdown {dd * 100:.1f}% > {DRAWDOWN_FRAC * 100:.0f}%")
        if status == "desync":
            problems.append("wallet reconcile = DESYNC")

    stamp = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(now))
    status_line = " | ".join(info)
    if problems:
        _post(f"⚠️ {stamp} ANOMALY: " + "; ".join(problems) + "  ||  " + status_line, dry, critical=True)
        print(stamp, "ALERT:", "; ".join(problems), "||", status_line)
        # The watchdog itself ran, so prove liveness to the external dead-man's-switch
        # even on an anomaly (bot problems are handled by Telegram; Healthchecks only
        # alarms when the box/watchdog goes fully silent).
        _healthcheck_ping(dry)
        return 1
    if ping:
        _post(f"✅ {stamp} watchdog armed / healthy  ||  " + status_line, dry)
    _healthcheck_ping(dry)
    print(stamp, "OK:", status_line)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never let cron see a traceback as the only signal
        try:
            _post(f"⚠️ watchdog itself crashed: {e}", "--dry" in sys.argv, critical=True)
        except Exception:
            pass
        print("watchdog crashed:", e)
        sys.exit(2)
