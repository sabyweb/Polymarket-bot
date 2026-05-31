# Stage-C Pull Runbook — 2026-05-31

**Goal:** land the staged fixes on Helsinki, confirm them in dry, then cut over to live.
**Companion:** `docs/runbooks/9_of_10_p5_p7_operator_runbook.md` (live-ops / P6 / P7 detail),
`docs/STATUS_2026-05-31.md` (why these fixes exist).

This pull is **not single-axis** — it lands a large batch (CRITICAL set FX-065/066/067/068,
HIGH set FX-063/069/071/072 + FX-070/074, EV-gate retune, and this session's FX-078 + FX-080).
You cannot single-axis a batched cutover, so the de-risking is: **(a)** split the *operator
actions* into single-axis steps each with one observable hypothesis (Phase 1A code, Phase 1B
config, Phase 2 mode), **(b)** re-soak in dry and verify each fix's signature before the
irreversible live flip (P4), **(c)** keep a one-command rollback ready (P2).

```
SSH   = ssh -i ~/.ssh/polymarket_bot_ed25519 polymarket@46.62.209.203
REPO  = /home/polymarket/Polymarket-bot
DB    = /home/polymarket/Polymarket-bot/bot_history.db
units = polymarket-farmer.service (reward_farmer.py --mode dry)
        polymarket-oversight.service (simple_oversight.py --loop)
```

Reusable **read-only** DB probe (never locks the live writer — `mode=ro` respects WAL):

```bash
ssh -i ~/.ssh/polymarket_bot_ed25519 polymarket@46.62.209.203 \
  'cd /home/polymarket/Polymarket-bot && python3 - ' <<'PY'
import sqlite3, datetime, os, time
def f(ts): return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if ts else None
c = sqlite3.connect('file:bot_history.db?mode=ro', uri=True)
for t in ["portfolio_snapshots","wallet_reconcile_history","capital_committed_snapshots"]:
    n,mn,mx = c.execute(f"SELECT count(*),min(ts),max(ts) FROM {t}").fetchone()
    print(f"{t:30s} n={n:<7} max={f(mx)}")
w = os.path.getsize('bot_history.db-wal') if os.path.exists('bot_history.db-wal') else 0
print(f"WAL size: {w/1e6:.1f} MB   (now: {f(time.time())})")
c.close()
PY
```

---

## Phase 0 — Pre-flight snapshot (READ-ONLY, ~2 min)

Capture the baseline so you can prove the fixes worked and can roll back cleanly.

```bash
$SSH 'cd /home/polymarket/Polymarket-bot && \
  echo "HEAD: $(git rev-parse --short HEAD)" && \
  systemctl is-active polymarket-farmer polymarket-oversight && \
  echo "--- overrides ---" && cat config_overrides.json && \
  ls -la bot_history.db-wal'
```

**Expected baseline (what this session verified):** HEAD `3bb6137`; both `active`;
overrides `{"RF_TRIAL_BUDGET_PCT": 0.75, "RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC": 0.01}`;
WAL ~95 MB. Run the read-only DB probe above — expect `portfolio_snapshots` frozen at
**2026-05-25 08:20** and `wallet_reconcile_history` frozen at **2026-05-29 04:22**
(these are the bugs you're about to fix; record them to compare against).

**Record the rollback point:** `3bb6137` (current HEAD).

---

## Phase 1A — Pull code, restart in DRY, verify persistence recovers  ·  *axis: code*

**Hypothesis:** the code fixes make the oversight persist again — `portfolio_snapshots`
and `wallet_reconcile_history` resume, the `database is locked` spam stops, and the WAL
checkpoints. (The overcommit ratio will still be ~8.1× here — the 0.01 override still
binds until Phase 1B. That separation is intentional.)

```bash
# MUTATING — back up overrides first (P2), then pull, then restart (stays --mode dry):
$SSH 'cd /home/polymarket/Polymarket-bot && \
  cp config_overrides.json config_overrides.json.bak.pre-stagec && \
  git pull origin main && \
  git rev-parse --short HEAD && \
  sudo systemctl restart polymarket-oversight polymarket-farmer'
```

Expect HEAD `4778b1d`. DB migrations (e.g. FX-067 `unwinds.unwind_event_id`) auto-apply on
first start — watch for the schema-ready line. **P4: keep `journalctl -f` open for the
first 5–10 cycles:**

```bash
$SSH 'journalctl -u polymarket-oversight -u polymarket-farmer -f'   # Ctrl-C after ~10 min
```

**Verify (after ≥2 oversight cycles, i.e. ~1 h):** re-run the read-only DB probe.
- ✅ `portfolio_snapshots` max ts is **now/current** and `n` is climbing (FX-078 working).
- ✅ `wallet_reconcile_history` max ts is **current**, rows every ~30 min (FX-080 working).
- ✅ WAL size **dropping toward a few MB** (checkpoint resumed).
- ✅ `database is locked` count ≈ 0:
  `$SSH 'journalctl -u polymarket-oversight -n 2000 --no-pager | grep -c "database is locked"'`
- ✅ No `[CRITICAL]` / kill: `... | grep -cE "CRITICAL|kill_switch.*true"` → 0.

If any ❌ → go to **Rollback**.

---

## Phase 1B — Clear the EV override, hot-reload, verify the ratio drops  ·  *axis: config*

**Hypothesis:** removing the `0.01` pin lets the committed `0.015` bind → overcommit ratio
falls from ~8.1× into the 3–8× band. No restart needed — FX-063 hot-reloads on file mtime.

**Decide on `RF_TRIAL_BUDGET_PCT: 0.75` first.** It was not previously flagged; confirm it's
intentional. If unsure, leave it and change only the EV key (true single-axis). To clear
*only* the EV pin and keep trial budget:

```bash
# MUTATING — write overrides without the EV key (edit/confirm the JSON by hand if unsure):
$SSH 'cd /home/polymarket/Polymarket-bot && \
  python3 -c "import json,io; p=\"config_overrides.json\"; d=json.load(open(p)); d.pop(\"RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC\",None); json.dump(d,open(p,\"w\"),indent=2); print(\"now:\",d)"'
```

**Verify (next oversight cycle, ≤30 min):**
- ✅ Config-reload log line appears (FX-063 `check_and_reload`).
- ✅ Latest `[SIMPLE_ALLOC]` / `[OVERCOMMIT_ALLOC]`: `capital_deployed / wallet` now **3–8×**
  (was 8.1×). `$SSH 'journalctl -u polymarket-oversight -n 200 --no-pager | grep -E "SIMPLE_ALLOC|OVERCOMMIT_ALLOC" | tail -1'`

---

## Re-soak in DRY (12–24 h)  ·  the gate before live

Let it run dry with the new code + cleared override. **"Clean" = all true:**
1. 0 `[CRITICAL]`, 0 kill-switch activations, 0 `Traceback/FATAL`.
2. `portfolio_snapshots` + `wallet_reconcile_history` keep accruing (no re-freeze).
3. WAL stays small (checkpointing steadily).
4. `database is locked` ≈ 0 across the window.
5. Overcommit ratio sits in 3–8× (not parked at the 5.0× soft block, not breaching 8.0×).
6. Cycle duration < 30 s; both services 0 restarts.

If clean → eligible for Phase 2. If anything trips → diagnose (the now-truthful WARNING
logs will name the failure) before proceeding.

---

## Phase 2 — Flip dry → live (the actual Stage-C cutover)  ·  *axis: mode*  ·  SEPARATE GO

> ⚠ Real money. Only after the dry re-soak is clean. This activates the live-fill behaviors
> (FX-065/066/067/068/071/072) that have never run in production — see the
> `9_of_10_p5_p7_operator_runbook.md` P6 fill-burst procedure.

```bash
# MUTATING — switch the farmer's ExecStart --mode dry -> --mode live, then restart:
$SSH 'sudo sed -i "s/--mode dry/--mode live/" /etc/systemd/system/polymarket-farmer.service && \
  sudo systemctl daemon-reload && \
  sudo systemctl restart polymarket-farmer && \
  systemctl show polymarket-farmer -p ExecStart --value'   # confirm shows --mode live
```

**First-hour watch (P4 + P6), `journalctl -f` open the whole time:**
- ✅ FX-077 clearance — one read-only authenticated 200 on `/rewards/user/markets` +
  `/rewards/user/percentages` (zero order risk; do this before/at first placement).
- ✅ First real fill → confirm exactly one `fills` row + truthful `[FILL_WRITE] succeeded`
  (FX-054/065), and any dump → one `unwinds` row with correct pnl (FX-066/067/071).
- ✅ Wallet reconcile `ok` (no `WALLET_DESYNC`); kill_switch false.
- ✅ Overcommit ratio in band; no hard-cancel churn.

---

## Rollback (P2 — one command)

Code rollback is clean: FX-078 only *adds* a table (IF NOT EXISTS), FX-080 is pure code; no
destructive migration to undo.

```bash
# MUTATING — revert to the pre-pull state:
$SSH 'cd /home/polymarket/Polymarket-bot && \
  git checkout 3bb6137 && \
  cp config_overrides.json.bak.pre-stagec config_overrides.json && \
  sudo sed -i "s/--mode live/--mode dry/" /etc/systemd/system/polymarket-farmer.service && \
  sudo systemctl daemon-reload && \
  sudo systemctl restart polymarket-oversight polymarket-farmer'
```

## Emergency halt (if live goes wrong)

```bash
# Stop placing immediately (existing orders remain until cancelled by the bot/exchange):
$SSH 'sudo systemctl stop polymarket-farmer'
# Full stop:
$SSH 'sudo systemctl stop polymarket-farmer polymarket-oversight'
```

The kill switch (24h realized loss > 10% wallet, or — now that FX-078 is live — 15% drawdown
from peak) will also self-trip and cancel all live orders; a manual stop is the faster lever
if you're watching.
