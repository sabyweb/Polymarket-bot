# Go-Live Resume Package — 2026-06-19 (operator-executed)

Operator-authorized (recorded in `ground_rules.md` change-log 2026-06-19). Bounded A/B soak from a
clean baseline, deposits frozen, full kill stack armed. **You** apply the config + press restart; I do
not restart services. Verified state: portfolio ~$985 all-cash, peak $1,220.52 (stale), drawdown 19.29%
(oversight already cleared; farmer sticky-killed awaiting this restart).

## Decisions (locked)
| Knob | Value | Why |
|---|---|---|
| Resume mode | **A/B from start** (C0 baseline vs C1 calmer-pond) | C0 is the concurrent control |
| Drawdown floor | **~$880** (`*_DRAWDOWN_FRAC=0.28` vs $1,220.52 peak) | ~$105 runway from $985 |
| Breadth | **20 markets** (`RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS=20`) | enough for A/B signal |
| Per-market cap | **$60** (`RF_MAX_CAPITAL_PER_MARKET_USD=60`) | unlocks min_size≤50 (70% of universe); $25 would deploy only min_size-20 (26%) |
| Deposits | **FROZEN** | only way forward net is measurable |
| Halt self-recovery | **gated auto-execute** (FALSE_POSITIVE only) | supervisor is a fast-follow build, operator-reviewed before deploy |

## Step 1 — config_overrides.json (merge to this exact set)
```json
{
  "RF_TRIAL_BUDGET_PCT": 0.75,
  "RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC": 0.01,
  "RF_TARGET_QUEUE_AHEAD_USD": 4000.0,
  "RF_FILL_BREAKER_WINDOW": 900,
  "RF_COOLDOWN_ESCALATION_ENABLED": false,
  "RF_ALLOC_MAX_RECENT_VOLATILITY": 0.15,
  "RF_KILL_PORTFOLIO_SOURCE": "onchain",
  "RF_PREEMPTIVE_COOLDOWN_ENABLED": true,

  "RF_KILL_DRAWDOWN_FRAC": 0.28,
  "RF_FARMER_DRAWDOWN_KILL_FRAC": 0.28,
  "RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS": 20,
  "RF_MAX_CAPITAL_PER_MARKET_USD": 60,

  "RF_AB_EXPERIMENT_ENABLED": true,
  "RF_AB_COHORT_COUNT": 2,
  "RF_AB_C1_MAX_RECENT_VOLATILITY": 0.03,
  "RF_AB_TOTAL_CAPITAL_USD": 400
}
```
The A/B's only differential is C1's tighter vol gate (0.03) vs C0 (0.15) — bounds/floor apply to both
cohorts so they cancel in the C0-vs-C1 comparison (single-axis treatment).

## Step 2 — gate (on the box, after `git pull`, BEFORE restart)
```
python3 -m simulation.run_audit_v5 --seeds 1 42 1337            # INV3/5/7 PASS
python3 -m pytest tests/ --ignore=tests/test_simulation.py --continue-on-collection-errors -q
```
(Never run the full suite on prod.) Confirm the new tests pass:
`tests/test_ab_cohort_parity.py`, `tests/test_halt_diagnose.py`.

## Step 3 — restart (clears the farmer's sticky flag; oversight already healthy)
```
sudo systemctl restart polymarket-oversight && sudo systemctl restart polymarket-farmer
```

## Step 4 — verify (read-only)
```
venv/bin/python3 soak_monitor.py --window-hours 24
journalctl -u polymarket-oversight --since "10 min ago" | grep OVERCOMMIT_ALLOC | tail -2
```
Expect: `kill_switch:false`; deploy count **NOT collapsed** (if ~0 → per-market cap too tight → raise to $30).
Confirm both cohorts deploy (the C1 vol gate excludes more than C0).

## Step 5 — measure (forward, clean)
- `python3 -m ab.net_reconcile --snap <fresh snapshot> --baseline-cash 985 --deposits 0` → clean forward net.
- `python3 -m ab.fetch_redeem` daily → forward held-to-resolution.
- The cohort comparison (C0 vs C1) on dump-loss-per-$ + fill-rate (estimate-free) + net.

## Deposit freeze
No deposits/withdrawals during the soak. The net-reconciler's deposit-freeze alarm flags any unexplained
large cash jump.

## Halt self-recovery (fast-follow, NOT needed for this manual resume)
The Halt-Doctor diagnosis engine (`ab/halt_diagnose.py`) is built + tested. The **auto-execute supervisor**
(gated, whitelist FALSE_POSITIVE, max 2/24h, re-kill hard-stop, paged, override file) is built next and
operator-reviewed before deploy. It handles FUTURE false-positive kills — the FIRST resume is this manual
restart. The current halt diagnoses as REAL_RESOLVED (human resume), not auto-recoverable.

## Revert (instant, hot-reload)
A/B + caps off; `*_DRAWDOWN_FRAC` → 0.15 once net-positive + recovered; disable auto-recovery via override.
