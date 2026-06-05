# FX-094–097 Staged Rollout Status (2026-06-05)

Tracks Helsinki deploy waves per `docs/runbooks/live_canary_operator.md` §8.1.

## Wave status

| Wave | Description | Status | Gate |
|------|-------------|--------|------|
| 0 | Safety overrides (escalation off, farmer vol 0) | **Done** 2026-06-05 ~06:04 UTC | — |
| 1 | FX-094 merge + FX-095 drawdown + FX-096 marks | **Done** 2026-06-05 ~06:08 UTC | **48h soak ends ~2026-06-07 06:08 UTC** |
| 2 | `RF_COOLDOWN_ESCALATION_ENABLED: true` | **Pending** Wave 1 soak | 7d soak before Wave 3 |
| 3 | `RF_ALLOC_MAX_RECENT_VOLATILITY: 0.10` | **Pending** Wave 2 soak | 3–7d before Wave 4 |
| 4 | 5a cap / 5c ranker / 5d preemptive (single-axis) | **Pending** Wave 3 soak | G-E clock after stable |

## Helsinki deploy log (2026-06-05)

```
# Preflight baseline (pre-deploy)
HEAD=b903c7449079c2622dd0eb8e851f5622d5a53d07
farmer_active=active
oversight_active=active
wallet=$1097.99 (exchange_balance)
kill_switch=false
active_markets=4
realized_loss_24h=$41.31

# Builder creds
BUILDER_* configured: NO (merge disabled until creds added to .env)

# Backups
bot_history.db.bak.pre-fx094-20260605
config_overrides.json.bak.pre-fx094-20260605

# Wave 0 applied: YES
# config_overrides adds RF_COOLDOWN_ESCALATION_ENABLED=false, RF_ALLOC_MAX_RECENT_VOLATILITY=0

# Wave 1 deployed: YES (git bundle fast-forward b903c74→4306913)
HEAD=43069133ee71c995f3305c550d1e551f7e6efe13
poly-web3 import: OK
post-restart: farmer=active, oversight=active, kill_switch=false
portfolio_snapshots.total_value: populated ($1097.99)
oversight cycle: deploys=5, kill_switch=False, peak=1201.76
```

## Wave 2 enable (after 48h soak — earliest 2026-06-07 ~06:08 UTC)

```bash
cd /home/polymarket/Polymarket-bot
python3 -c "import json;p='config_overrides.json';d=json.load(open(p));d['RF_COOLDOWN_ESCALATION_ENABLED']=True;json.dump(d,open(p,'w'),indent=2)"
sudo systemctl restart polymarket-oversight
```

## Wave 3 enable (after Wave 2 stable 7d)

```bash
python3 -c "import json;p='config_overrides.json';d=json.load(open(p));d['RF_ALLOC_MAX_RECENT_VOLATILITY']=0.10;json.dump(d,open(p,'w'),indent=2)"
sudo systemctl restart polymarket-farmer
```

## Wave 4 enable (single-axis, one knob at a time)

Examples (do not enable all at once):

- `RF_MAX_CAPITAL_PER_MARKET_USD`: 80
- `RF_RANK_VOL_PENALTY_K`: 0.5
- `RF_PREEMPTIVE_COOLDOWN_ENABLED`: true

Restart oversight after allocator knobs; restart farmer after preemptive cooldown.

## G-E clock

Gate G-E (7 days clean, rewards > losses) **not started** — waiting for Waves 1–3 to soak stable.

## Local / origin note

Commit `4306913` is on local `main`. Push to `origin/main` when ready so future deploys can use `git pull --ff-only` instead of git bundle.
