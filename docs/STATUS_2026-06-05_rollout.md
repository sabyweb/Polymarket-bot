# FX-094–097 Staged Rollout Status (2026-06-05)

Tracks Helsinki deploy waves per `docs/runbooks/live_canary_operator.md` §8.1.

## Wave status

| Wave | Description | Status | Gate |
|------|-------------|--------|------|
| 0 | Safety overrides (escalation off, farmer vol 0) | See Helsinki log below | — |
| 1 | FX-094 merge + FX-095 drawdown + FX-096 marks | See Helsinki log below | 48h soak before Wave 2 |
| 2 | `RF_COOLDOWN_ESCALATION_ENABLED: true` | **Pending** Wave 1 soak | 7d soak before Wave 3 |
| 3 | `RF_ALLOC_MAX_RECENT_VOLATILITY: 0.10` | **Pending** Wave 2 soak | 3–7d before Wave 4 |
| 4 | 5a cap / 5c ranker / 5d preemptive (single-axis) | **Pending** Wave 3 soak | G-E clock after stable |

## Helsinki deploy log

_(Filled by operator / agent at deploy time.)_

```
# Preflight baseline
HEAD=
farmer_active=
oversight_active=
wallet=
kill_switch=

# Builder creds
BUILDER_* configured: yes/no

# Wave 0 applied: yes/no
# Wave 1 pulled + restarted: yes/no
# Post-deploy verification
total_value snapshots OK: yes/no
```

## G-E clock

Gate G-E (7 days clean, rewards > losses) **not started** until Waves 1–3 are stable.
