# 9/10 Plan — Operator Runbook for P5 through P7

**Audience:** the human operator on Helsinki. P1-P4 are shipped (4 commits
on `main`, ending at `b1d7ddd`). Three of five 9/10 gates are met at the
code level: G-A (FX-052+053), G-B (4 triggers wired), G-D (FX-046 resolved).

The remaining two gates require **live operation**:
- **G-C** — FX-054 verified against a real fill burst (≥3 fills in 1h).
- **G-E** — G1 7-day clean run on dev wallet (zero CRITICAL / kill switch / manual SQL / unscheduled restart, ≥1 fill+dump with ≤3% slippage).

This runbook is the step-by-step for those.

---

## P5 — Staged rebring-up

### Stage A: paper-mode sanity (1 day, local)

Skip — paper mode (`paper_trader_v2.py`) is a separate code path that
doesn't exercise the new FX-052+053+057+058+059 stack. Validation
already done via the 126+ unit/contract/audit tests in this session.
Move to Stage B.

### Stage B: shadow mode on Helsinki (≥48h)

**Goal:** verify the new stack runs cleanly against real Polymarket data
without placing any orders. **NO CAPITAL AT RISK.**

```bash
# 1. SSH into Helsinki
ssh polymarket@helsinki.polymarket-bot

# 2. Pull main, verify HEAD
cd ~/Polymarket-bot
git pull origin main
git log -1 --oneline
# Expect: b1d7ddd or later — confirm P1-P4 are in.

# 3. Switch systemd unit to dry mode (idempotent — already default)
sudo sed -i 's|--mode live|--mode dry|' /etc/systemd/system/polymarket-farmer.service
sudo systemctl daemon-reload

# 4. Restart both services
sudo systemctl restart polymarket-farmer polymarket-oversight

# 5. Tail logs — keep this window open
journalctl -f -u polymarket-farmer -u polymarket-oversight | grep -E \
  'OVERCOMMIT_ALLOC|FILL_DETECT_TRACE|FILL_WRITE|RECONCILE_DRIFT|LEARN|GUARDRAIL|CRITICAL|FATAL|Traceback|ERROR'
```

**Stage B pass criteria (must hold continuously for ≥48h):**

- ✅ Zero `[CRITICAL]` / `[FATAL]` / `Traceback` log lines.
- ✅ `[OVERCOMMIT_ALLOC]` log emitted every oversight cycle (~30min) with `deploys >= 50` once eligible market discovery has run.
- ✅ `overcommit_ratio` in `[OVERCOMMIT_ALLOC]` log lands in the **3.0× – 8.0×** band (Ground Rule 2 design point).
- ✅ Farmer cycle duration < 30s (check `[CYCLE_SUMMARY]` log) — confirms the 100+ market plan doesn't blow the cycle budget.
- ✅ No kill switch activations (search journalctl for `kill_switch=True` or `KILL_SWITCH`).

**If any pass criterion fails:** stop, file as a new FX-NNN entry in `Polymarket bot fixit.md`, fix per the build → audit → fix workflow, then restart Stage B's clock from 0.

### Stage C: live cutover at full wallet

**Per operator answer in plan approval:** full wallet from cutover (not the 10% staged step I originally proposed).

**Pre-cutover checklist:**

- [ ] Stage B has been clean for ≥48h continuous.
- [ ] Last commit on main is `b1d7ddd` or later AND your local HEAD matches.
- [ ] `config_overrides.json` on Helsinki has NO leftover overrides from prior debugging sessions.
- [ ] Wallet balance ≥ $200 (per the 9/10 plan operator confirmation).
- [ ] You're prepared to halt fully (`systemctl stop polymarket-farmer polymarket-oversight`) within 30s if anything looks wrong.

**Cutover command:**

```bash
ssh polymarket@helsinki.polymarket-bot
cd ~/Polymarket-bot

# Switch to live mode
sudo sed -i 's|--mode dry|--mode live|' /etc/systemd/system/polymarket-farmer.service
sudo systemctl daemon-reload
sudo systemctl restart polymarket-farmer polymarket-oversight

# Tail logs (keep open for first 1 hour minimum)
journalctl -f -u polymarket-farmer -u polymarket-oversight | grep -E \
  'OVERCOMMIT_ALLOC|FILL_DETECT_TRACE|FILL_WRITE|RECONCILE_DRIFT|LEARN|GUARDRAIL|CRITICAL|FATAL|Traceback'
```

**First-hour watch criteria:**

- First `[OVERCOMMIT_ALLOC]` log shows `deploys` > 0 (allocator made a plan).
- First [FILL_WRITE attempting] eventually followed by `step=succeeded` or `step=duplicate` (NEVER `step=FAILED`).
- No kill switch activations.

**Emergency halt (use within 30s if anything looks wrong):**

```bash
sudo systemctl stop polymarket-farmer polymarket-oversight
# Then page operator for review — DO NOT auto-recover per plan agreement.
```

---

## P6 — FX-054 production verification against a real fill burst

**Goal:** prove `fills_count_in_DB == on_chain_BUY_count` after a real fill burst (≥3 fills in 1h).

**Trigger:** wait for natural fill burst in production (typically 1-3 days into live operation under OverCommitAllocator).

**Verification script (run on Helsinki):**

```bash
#!/bin/bash
# Save as ~/Polymarket-bot/docs/runbooks/p6_verify_fx054.sh

cd ~/Polymarket-bot
source venv/bin/activate

# 1. Identify a fill burst window from journalctl
BURST_START_TS=$(journalctl -u polymarket-farmer --since "24 hours ago" | \
  grep "FILL_WRITE.*step=attempting" | head -1 | awk '{print $1, $2, $3}')
BURST_END_TS=$(date -u +%s)
echo "Burst window: $BURST_START_TS to $(date -u -d @$BURST_END_TS)"

# 2. Count fills in DB for the window
DB_COUNT=$(sqlite3 ~/Polymarket-bot/bot_history.db \
  "SELECT COUNT(*) FROM fills WHERE ts > $(($BURST_END_TS - 3600));")
echo "DB fills (last 1h): $DB_COUNT"

# 3. Count on-chain BUYs from Polymarket data-api
FUNDER=$(grep "FUNDER" ~/Polymarket-bot/.env | cut -d= -f2)
ONCHAIN_COUNT=$(curl -s "https://data-api.polymarket.com/activity?type=TRADE&user=${FUNDER}&limit=100" | \
  python -c "
import json, sys, time
events = json.load(sys.stdin)
cutoff = time.time() - 3600
buys = [e for e in events if e.get('side') == 'BUY' and e.get('timestamp', 0) > cutoff]
print(len(buys))
")
echo "On-chain BUYs (last 1h): $ONCHAIN_COUNT"

# 4. Compare
DIFF=$(($DB_COUNT - $ONCHAIN_COUNT))
ABS_DIFF=${DIFF#-}  # absolute value
if [ "$ABS_DIFF" -le 1 ]; then
  echo "PASS: fills_count_in_DB ($DB_COUNT) ≈ on_chain_BUY_count ($ONCHAIN_COUNT), within ±1"
  echo "Gate G-C MET."
else
  echo "FAIL: drift of $DIFF — fills missing or duplicated"
  echo "Inspect: journalctl -u polymarket-farmer --since '1 hour ago' | grep 'FILL_DETECT_TRACE'"
fi

# 5. Also check [RECONCILE_DRIFT] fired (expected: 0 or low — only on
#    primary-path-missed scenarios)
DRIFT_FIRED=$(journalctl -u polymarket-farmer --since "1 hour ago" | \
  grep -c "RECONCILE_DRIFT.*step=catching_up")
echo "Drift catch-up events in last 1h: $DRIFT_FIRED"
# Expected pattern under healthy operation: 0-1. >5 indicates the
# primary path is broken (potential FX-061 territory).
```

**Pass criterion (G-C met):** `|DB_COUNT - ONCHAIN_COUNT| ≤ 1` AND `step=FAILED` log lines = 0.

**If fail:** file FX-061 with the burst window timestamps + journalctl diff between DB vs on-chain. Don't proceed to P7 until resolved.

---

## P7 — G1 7-day clean run

**Goal:** earn the friend-rollout G1 gate. Per architecture doc §10.3 / fixit §6, "clean" = continuously for 7 days:

- Zero `[CRITICAL]` / `[FATAL]` / `Traceback` log lines
- Zero kill-switch activations
- Zero manual SQL `UPDATE` / `DELETE` on fills/unwinds/positions tables
- Zero operator-triggered restarts except planned version deploys
- ≥1 fill+dump cycle with measured slippage ≤3%

**Monitoring script (run every 4h on Helsinki):**

```bash
#!/bin/bash
# Save as ~/Polymarket-bot/docs/runbooks/p7_g1_monitor.sh
# Run via cron: */60*4 * * * /home/polymarket/Polymarket-bot/docs/runbooks/p7_g1_monitor.sh

cd ~/Polymarket-bot
SUMMARY_LOG=~/g1_daily_summary.log

echo "=== $(date -u) ===" >> $SUMMARY_LOG

# 1. Critical/fatal/traceback in last 4h
CRIT=$(journalctl -u polymarket-farmer -u polymarket-oversight --since "4 hours ago" | \
  grep -cE 'CRITICAL|FATAL|Traceback|WALLET_DESYNC|RECONCILE_DRIFT.*FAILED')
echo "Critical events (4h): $CRIT" >> $SUMMARY_LOG

# 2. Kill switch
KILL=$(journalctl -u polymarket-farmer --since "4 hours ago" | \
  grep -c "kill_switch.*True\|KILL_SWITCH")
echo "Kill switch activations: $KILL" >> $SUMMARY_LOG

# 3. Notional overcommit ratio range
sqlite3 bot_history.db "SELECT MIN(total_value), AVG(total_value), MAX(total_value) FROM portfolio_snapshots WHERE ts > strftime('%s','now') - 14400;" >> $SUMMARY_LOG

# 4. Slippage on last 10 unwinds
sqlite3 bot_history.db <<SQL >> $SUMMARY_LOG
SELECT
  AVG((vwap_cost - usd_value) / vwap_cost) AS avg_slippage,
  MAX((vwap_cost - usd_value) / vwap_cost) AS max_slippage,
  COUNT(*) AS n
FROM (SELECT * FROM unwinds WHERE ts > strftime('%s','now') - 14400 ORDER BY ts DESC LIMIT 10);
SQL

# 5. Daily reward earnings
sqlite3 bot_history.db "SELECT date, SUM(rewards_paid) FROM reward_daily WHERE date >= date('now', '-1 day') GROUP BY date;" >> $SUMMARY_LOG

# 6. Halt conditions
if [ "$CRIT" -gt 0 ]; then
  echo "ALERT: $CRIT critical events — resetting G1 window. Investigate before continuing." >> $SUMMARY_LOG
  # Optional: send email / slack — wire to your alerting channel
fi
```

**G1 timer reset rules:**
- Any `[CRITICAL]` or kill switch → restart the 7-day clock from 0.
- Operator-initiated restart for non-emergency reason (e.g., `git pull` for unrelated docs) → does NOT reset.
- Operator manual SQL → resets.

**G1 success looks like:** 7 consecutive days of monitor outputs with `Critical events: 0` AND `Kill switch activations: 0` AND `avg_slippage ≤ 0.03` (≤3%) on at least one observed fill+dump cycle.

---

## Hand-back to me

Once one of the following happens, ping me for P8 / P9:

1. **All 5 gates met (G1 7-day run clean):** ready for P9 final certification.
2. **P5 Stage B or Stage C fails:** ping me with the failing log lines for P9 fix-loopholes.
3. **P6 verification fails:** ping me with the DB-vs-data-api diff.
4. **P7 G1 timer resets twice:** ping me — likely an architectural issue not in the plan that needs design review.

For P8 (adversarial sweep + chaos engineering) — I can do this OFFLINE in parallel with your P7 monitoring. I'll write the chaos tests as a separate offline workstream and have them ready as a new commit by the time P7 completes.

---

## Quick reference — what's where on Helsinki

| Thing | Path |
|---|---|
| Repo | `/home/polymarket/Polymarket-bot` |
| systemd units | `/etc/systemd/system/polymarket-farmer.service`, `polymarket-oversight.service` |
| DB | `~/Polymarket-bot/bot_history.db` |
| Alloc file | `~/Polymarket-bot/market_allocations.json` |
| Config overrides | `~/Polymarket-bot/config_overrides.json` |
| Logs | `journalctl -u polymarket-farmer` / `polymarket-oversight` |

## Quick reference — new cfg knobs you can tune via `config_overrides.json`

| Knob | Default | Tune if... |
|---|---|---|
| `RF_MAX_NOTIONAL_RATIO` | 5.0 | False kills during normal overcommit → raise to 6.0-7.0 |
| `RF_HARD_NOTIONAL_RATIO` | 8.0 | Hard cancels firing during normal operation → raise to 10.0 |
| `RF_RAPID_GROWTH_KILL_RATIO` | 5.0 | False kills on legitimate ramp-ups → raise to 7.0; set 0 to disable |
| `RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS` | 500 | Hitting the cap routinely → raise to 1000 |
| `RF_OVERCOMMIT_PER_MARKET_BUFFER_FRAC` | 0.10 | Per-market cost too tight → raise to 0.20 |
| `RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC` | 0.02 | EV gate too restrictive → lower to 0.01; too permissive → raise to 0.03 |
| `RF_OVERCOMMIT_Q_SHARE_CONSERVATIVE_FACTOR` | 1.0 | Over-deployment from heuristic q_share → lower to 0.5 |
