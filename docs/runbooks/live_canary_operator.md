# Live-Canary Operator Runbook

**For:** running + monitoring the bot while it is `--mode live` on Helsinki.
**As of:** 2026-06-02 (HEAD `92ec34c`). Supersedes the dry-shadow-era assumptions in
`9_of_10_p5_p7_operator_runbook.md` and `stage_c_pull_2026-05-31.md` for day-to-day live ops.

> **Mental model:** two processes, file-coupled. `simple_oversight.py` (~30 min) plans and
> writes `market_allocations.json`; `reward_farmer.py` (~30 s) executes it and owns all
> real-time kill switches. The bot self-protects (kills + Discord pages); your job is
> supervisory — confirm health, read the daily settlement, and tune.

---

## 0. Access

```
ssh -i ~/.ssh/polymarket_bot_ed25519 polymarket@46.62.209.203
# repo: /home/polymarket/Polymarket-bot   (Hetzner Helsinki hel1 — only region clearing the geoblock)
```
Read-only DB probe (never lock the live WAL): `sqlite3 'file:bot_history.db?mode=ro' uri=True`.

## 1. 30-second health check

```bash
RB=/home/polymarket/Polymarket-bot; DB=$RB/bot_history.db
systemctl is-active polymarket-farmer polymarket-oversight        # both: active
systemctl show polymarket-farmer -p MainPID -p NRestarts          # NRestarts should stay 0
journalctl -u polymarket-farmer --since "3 min ago" | grep -E "on-book|CYCLE_SUMMARY" | tail -2
```
**Healthy looks like:** `kill_switch: false`, `active_markets` > 0, `N/M on-book` with N>0,
`total_live_notional` > 0, `realized_loss_24h` small. **No `kill switch ACTIVE` lines.**

## 2. Discord alerts — what each means + what to do

| Alert | Source | Meaning | Action |
|---|---|---|---|
| **KILL SWITCH ACTIVATED** | FX-092 | A kill tripped; trading halted; process alive-but-idle | See §3 — do **not** reflexively restart |
| **HEARTBEAT STALE** (peer) | FX-083 | A process hasn't written a heartbeat (hung/dead) | SSH in; check `systemctl status`; restart the dead unit |
| **WALLET_DESYNC** | FX-074/049 | Cash delta ≠ expected (fills−unwinds+rewards) by >$0.50 | Usually **benign**: reward-settlement lag (`rewards_delta=0` right after ~00:20 UTC) or taker-fee noise on a dump. Observational — no halt. Investigate only if it persists/grows. |

## 3. Kill response (READ THIS BEFORE RESTARTING)

Kills are **sticky** — they require a process restart to clear (deliberate: each kill condition
benefits from human eyes-on first). The restart cancels then re-places orders.

1. **Identify why:** `journalctl -u polymarket-farmer --since "1 hour ago" | grep -E "KILL SWITCH ACTIVATED|kill switch ACTIVE" | head -3`
2. **Decide before restarting:**
   - **fill-rate kill** (`fill_rate_ratio > 3.0×`) → the bot is *adversely filling too fast*.
     **Restarting without reducing the fill rate just re-enters a kill loop.** First deepen
     the queue / tighten selection (§5), *then* restart.
   - **realized-loss kill** (24h loss > 10% wallet) or **drawdown kill** (>15% from peak) →
     a real loss event. Understand the cause before resuming.
   - **unrealized-loss kill** (FX-084, held inventory marked down >20%) → check held positions.
   - **oversight-silence backstop** (FX-082, alloc stale >2h + drawdown) → oversight is
     dead/wedged; fix oversight first.
3. **Check held inventory** (unmanaged while killed): `https://data-api.polymarket.com/positions?user=<funder>&sizeThreshold=0.1`. FX-071's bounded-loss floor protects it; it rides to resolution.
4. **Restart to clear** (only after the cause is addressed): `sudo systemctl restart polymarket-farmer`.

## 4. Halt / restart / mode

```bash
sudo systemctl stop polymarket-farmer        # graceful halt — cancels resting orders
sudo systemctl restart polymarket-farmer     # clears a sticky kill; reloads code + config
# dry  ⇄ live:
sudo sed -i 's/--mode live/--mode dry/' /etc/systemd/system/polymarket-farmer.service \
  && sudo systemctl daemon-reload && sudo systemctl restart polymarket-farmer
```

## 5. Tuning knobs (hot-reload — edit `config_overrides.json`, picked up next cycle, NO restart)

Edit safely (preserve keys): `python3 -c "import json;p='config_overrides.json';d=json.load(open(p));d['KNOB']=VALUE;json.dump(d,open(p,'w'),indent=2)"`

| Knob | Now | Effect / when to change |
|---|---|---|
| `RF_TARGET_QUEUE_AHEAD_USD` | 4000 | **Adverse-fill lever.** Higher = quote only when shielded by deeper queue (fewer fills, less reward). Raise if filling too much; lower if earning too little. |
| `RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC` | 0.01 | EV-gate fill-cost assumption. Higher = stricter (fewer, higher-reward deploys). Raise toward 0.03–0.05 if marginal markets keep losing. |
| `RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS` | 5 | The canary cap. **Only raise (5→25→50…) once net-positive holds** — it scales losses with rewards. |
| `RF_COLD_START_Q_SHARE` | 0.005 (default) | Prior for unseen markets; binds the EV gate. FX-046: heuristics under-predict 24–94×. Tune once real reward data lands. |
| `RF_ALLOC_MIN_HOURS_TO_RESOLUTION` | 48 (default) | FX-090: exclude markets resolving within N h. |
| `RF_ALLOC_MIN_HOURS_TO_GAME_START` | 12 (default) | FX-090: exclude markets within N h of game start. |

## 6. Key log lines (`journalctl -u polymarket-farmer -u polymarket-oversight -f`)

- `[CYCLE_SUMMARY]` — farmer per-cycle: `active_markets`, `kill_switch`, `orders_placed`, `total_live_notional`, `realized_loss_24h`.
- `[OVERCOMMIT_ALLOC]` — allocator: `eligible / positive_ev / deploys / timing_excluded / timing_fetches / overcommit_ratio / p4_global_tighten`.
- `[LEARN_CAPEFF]` — `capital_efficiency` (gross reward/$), `daily_roi` (net), `total_reward`, `total_capital`.
- `[LEARN]` — cooldowns: `newly_cooled / still_cooled / total_reward / total_loss`.
- `EXPIRY SWEEP`, `SKIP resolution proximity`, `DUMP …` — placement/dump decisions.

## 7. Authoritative reward / P&L (on-chain, public, no auth)

```
https://data-api.polymarket.com/activity?user=<funder>&type=REWARD       # liquidity rewards
https://data-api.polymarket.com/activity?user=<funder>&type=MAKER_REBATE # rebates
https://data-api.polymarket.com/positions?user=<funder>&sizeThreshold=0.1
```
Funder proxy: `0xB23Bc80E6719099aeBE0c34389f05EC8C928503f`. **Rewards settle as a daily
aggregate at ~00:20 UTC** ($1/day/user threshold). This is the truth source for P&L — trust
it over SDK-derived numbers (see FX-088/089). Local mirror: `daily_reward_cache.__TOTAL__`.

## 8. Deploy a code change

```bash
cd /home/polymarket/Polymarket-bot && git pull --ff-only origin main
sudo systemctl restart polymarket-oversight   # if the change is allocator/oversight-side
sudo systemctl restart polymarket-farmer      # if farmer-side (also clears a sticky kill)
```
Always: change committed on `main` (tests green) first; `--ff-only` (Helsinki tracked files
stay clean; `config_overrides.json`/DB are untracked and survive). Rollback = `git checkout
<prev> && restart`, or revert the config edit.

## 9. Current objective + gates (so you know what "done" means)

Objective: **max-farm rewards, capital-efficiently, NET-positive.** Gross is there
(~1.4%/day); the open work is killing the adverse-fill leak on news markets (deeper queue =
round 1; volatility/news filter = round 2). **Gate G-E = 7 days clean, rewards > losses.**
Until then this is an unproven canary — keep the cap small and watch the daily settlement.
