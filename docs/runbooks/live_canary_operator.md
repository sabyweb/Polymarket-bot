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

### 8.1 FX-094–097 staged rollout (2026-06-05)

**Scope:** merge fix (FX-094), portfolio drawdown (FX-095), unrealized marks (FX-096),
escalating cooldowns (FX-097), farmer vol guard (phase 5b). Deploy in waves; do not enable
all knobs at once.

**Pre-flight (before first pull):**

```bash
RB=/home/polymarket/Polymarket-bot; cd $RB
git rev-parse HEAD
cp bot_history.db bot_history.db.bak.pre-fx094-$(date +%Y%m%d)
cp config_overrides.json config_overrides.json.bak.pre-fx094-$(date +%Y%m%d)
grep -E '^BUILDER_' .env | sed 's/=.*/=***redacted***/'   # FX-094 needs all three set
```

**Wave 0 — safety overrides** (apply before or with first pull; restart oversight):

```json
{
  "RF_TRIAL_BUDGET_PCT": 0.75,
  "RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC": 0.01,
  "RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS": 5,
  "RF_TARGET_QUEUE_AHEAD_USD": 4000,
  "RF_FILL_BREAKER_WINDOW": 900,
  "RF_COOLDOWN_ESCALATION_ENABLED": false,
  "RF_ALLOC_MAX_RECENT_VOLATILITY": 0
}
```

`RF_COOLDOWN_ESCALATION_ENABLED: false` keeps legacy 24h cooldowns until Wave 2 soak passes.
`RF_ALLOC_MAX_RECENT_VOLATILITY: 0` disables the 30s farmer vol guard until Wave 3.

**Wave 1 — safety fixes** (FX-094 + FX-095 + FX-096):

```bash
cd $RB && git pull --ff-only origin main
venv/bin/pip install -r requirements.txt    # poly-web3, py-builder-relayer-client
sudo systemctl restart polymarket-oversight polymarket-farmer
```

Verify (first 2h): `portfolio_snapshots.total_value` populated; no false drawdown kill on
cash→inventory conversion; merge logs show `[MERGE]` not `merge_positions` AttributeError.
**Soak gate:** 48h clean before Wave 2.

**Wave 2 — FX-097** (after 48h soak): set `"RF_COOLDOWN_ESCALATION_ENABLED": true`;
`sudo systemctl restart polymarket-oversight`. **Soak gate:** 7d on breadth/rewards.

**Wave 3 — farmer vol guard** (after Wave 2 stable): set
`"RF_ALLOC_MAX_RECENT_VOLATILITY": 0.10` (or `0.15` conservative); restart farmer.
**Soak gate:** 3–7d on adverse-fill $/day.

**Wave 4 — selection knobs** (one at a time, 3–7d soak each): `RF_MAX_CAPITAL_PER_MARKET_USD`,
`RF_RANK_VOL_PENALTY_K`, `RF_PREEMPTIVE_COOLDOWN_ENABLED`.

**Builder env (FX-094):** `.env` must include `BUILDER_API_KEY`, `BUILDER_SECRET`,
`BUILDER_PASSPHRASE` for Safe-wallet merge via Builder Relayer. Without them merge is
disabled — hedged pairs are held and `alert_merge_needed` pages Discord (no auto dual-dump).

**New Discord alert:**

| Alert | Meaning | Action |
|---|---|---|
| **MERGE NEEDED** | Both sides filled; merge failed or creds missing | Check builder creds; hold pair (~$1/pair) or manual merge; do not expect auto dual-dump |

**Restart matrix:**

| Change | Restart |
|---|---|
| FX-094 merge / FX-096 unrealized | farmer |
| FX-095 drawdown / FX-097 cooldowns | oversight (+ farmer for FX-095 backstop) |
| Phase 5b farmer vol | farmer (hot-reloads, restart once to clear state) |
| Phase 5a/5c allocator | oversight |

**Rollback:** `git checkout <pre-wave-sha> && venv/bin/pip install -r requirements.txt`;
restore `config_overrides.json.bak.pre-fx094-*`; restart both units.

## 9. Current objective + gates (so you know what "done" means)

Objective: **max-farm rewards, capital-efficiently, NET-positive.** Gross is there
(~1.4%/day); the open work is killing the adverse-fill leak on news markets (deeper queue =
round 1; volatility/news filter = round 2). **Gate G-E = 7 days clean, rewards > losses.**
Until then this is an unproven canary — keep the cap small and watch the daily settlement.

## 10. Normal behaviors that look alarming (NOT bugs)

- **One-sided placement** (a market with only a YES *or* only a NO order): expected. Placement
  is per-side (`order_lifecycle.py:976` / `:1037`); each side needs (a) exit-liquidity (≥ our
  size of book depth to unwind it) and (b) `can_place()` (no fill-breaker / post-fill cooldown /
  dump-failure block). The exact per-side reason is recorded — read it, don't guess:
  `sqlite3 bot_history.db "SELECT * FROM placement_feedback WHERE condition_id LIKE '0x...%';"`.
  Common reasons: `exit_liquidity` (book too thin to exit that side), `dump_failures`
  (`RF_DUMP_MAX_FAILURES` hit on that market), `resolution_proximity`, `wide_spread`,
  `capital_exhausted`. One-sided = the bot declining a side it can't safely exit; it still
  earns reward on the placed side.
- **`global_tighten=True`** in `[OVERCOMMIT_ALLOC]`: the learning loop staying defensive
  (24h loss > 0.5×reward) — fewer/smaller deploys until reward recovers. Normal, not stuck.
- **A lingering `dump_sell @ $0.01`** in `active_orders` after a position closed/merged (on-chain
  `/positions` shows it gone): an orphan dump order. Harmless (≤$1, can't fill naked on
  Polymarket). Cancel if you want; the reconciler should sweep it.
- **WALLET_DESYNC right after ~00:20 UTC:** reward-settlement lag (`rewards_delta=0` until the
  data-api indexes the credit) → a transient positive divergence that self-heals next cycle.
  Benign (observational, no halt).
- **`orders_placed: 0` in a steady-state cycle:** orders already resting; nothing to (re)place
  that cycle. Normal.
- **Net-negative-but-stable is NOT "broken"** — it's the unproven-objective state the soak
  exists to resolve. "Broken" = a kill fires, a process crashes / heartbeat stale, a *real*
  (persistent or growing) wallet desync, 0-farming for an extended window, or runaway loss
  (realized_loss_24h approaching 10% of wallet / drawdown approaching 15%).

## 11. Monitoring dashboard (read-only, localhost-only)

`dashboard.py` is a read-only Streamlit view of the live `bot_history.db` +
`market_allocations.json` + the public data-api (tabs: Overview, Market
Selection, Market Perf, P&L, Positions, History, System Health). It opens the
DB with `mode=ro` and never writes. **It reads `.env` (FUNDER) — keep it bound
to loopback and reach it over an SSH tunnel; do not expose port 8501 publicly.**

**One-time install (on Helsinki):**

```bash
RB=/home/polymarket/Polymarket-bot; cd $RB
venv/bin/pip install streamlit pandas   # not in requirements.txt (bot runtime stays lean); jinja2 comes with streamlit
sudo cp docs/runbooks/polymarket-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-dashboard
systemctl is-active polymarket-dashboard   # -> active
```

The unit (`docs/runbooks/polymarket-dashboard.service`) runs
`streamlit run dashboard.py --server.port 8501 --server.address 127.0.0.1
--server.headless true`, so it only listens on `127.0.0.1:8501`.

**Quick one-off (no unit):**
`venv/bin/streamlit run dashboard.py --server.port 8501 --server.address 127.0.0.1`

**View it from your laptop (SSH tunnel — local 8501 → server 8501):**

```bash
ssh -i ~/.ssh/polymarket_bot_ed25519 -N -L 8501:127.0.0.1:8501 polymarket@46.62.209.203
# then open http://localhost:8501  (leave `ssh -N` running; Ctrl-C closes the tunnel)
```

Restart after a code pull: `sudo systemctl restart polymarket-dashboard`.
Logs: `journalctl -u polymarket-dashboard -f`. Auto-refreshes every 60s;
supervisory only — it touches nothing the bot relies on.

## 12. Soak monitor (Loop A — read-only daily digest)

`soak_monitor.py` produces a once-a-day digest: liveness (heartbeats), live
`kill_switch` (parsed from the farmer's `[CYCLE_SUMMARY]` journal line), last-24h
P&L vs **authoritative data-api** reward, a rewards-vs-losses verdict, wallet
reconciliation, and the worst realized-loss markets (from `unwinds`). It is
**read-only and reports only** — it cannot restart, edit config, trade, or clear
a kill (CLAUDE.md §7). Safe by default: prints to stdout; `--write` appends
`docs/soak_log.md`; `--post` sends the digest to Discord.

Run it by hand anytime (pure read):

```bash
cd /home/polymarket/Polymarket-bot && venv/bin/python3 soak_monitor.py
```

Schedule it (daily 00:30 UTC, ~10 min after reward settlement):

```bash
RB=/home/polymarket/Polymarket-bot; cd $RB
sudo cp docs/runbooks/polymarket-soak-monitor.service /etc/systemd/system/
sudo cp docs/runbooks/polymarket-soak-monitor.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-soak-monitor.timer
systemctl list-timers polymarket-soak-monitor.timer    # confirm next run
```

Trigger a one-off run of the scheduled unit: `sudo systemctl start polymarket-soak-monitor.service`.
Logs: `journalctl -u polymarket-soak-monitor -n 50 --no-pager`. Digest history: `docs/soak_log.md`.
Notes: it reads the farmer journal for the live kill flag — if that's ever unreadable, the
Safety line degrades to "UNKNOWN" and falls back to DB proxies (kills still page Discord via
`monitor_watchdog.py`). `cycle_snapshots`/`safety_state` are legacy tables and are deliberately NOT read.
