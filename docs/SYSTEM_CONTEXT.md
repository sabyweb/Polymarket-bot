# Polymarket Reward-Farming Bot — Complete System Context

> **Purpose of this file:** a single, self-contained brief that gives any human or AI agent
> complete context on what this system is, its objective, how it's built, where it stands,
> and every known open issue. Share it as-is. Last verified against live + repo ground truth
> **2026-06-03 ~07:40 UTC**. When in doubt, trust *verified data* (logs, DB rows, on-chain/API
> responses) over prose — that is the project's first operating principle.

---

## 0. The objective (immutable — `ground_rules.md`)

**Maximize Polymarket CLOB liquidity-reward earnings, capital-efficiently, while staying net-profitable.**
The bot rests `min_size` limit orders inside many markets' reward zones to earn "scoring" rewards,
and avoids/manages adverse fills. Three ground rules:
1. **Max-farm breadth** at `min_size` across many markets.
2. **Exploit capital over-commit** — place 3–8× wallet notional; if one order fills, the rest auto-cancel.
3. **A self-learning loop** with 6 auto-correction triggers (ROI cooldowns, fill-rate, global loss/reward, q_share divergence).

It is a **reward-capture allocator with layered safety** — *not* a price predictor, directional bettor, or global optimiser.

**Current honest status: the objective is UNPROVEN and net-negative.** The bot farms safely and the
foundation is correct, but losses (adverse fills) currently roughly match or exceed rewards. The
single unsolved core problem is **market selection** (see §4).

---

## 1. Architecture — two processes, one file + one DB

```
 simple_oversight.py  --loop   (~30 min)            reward_farmer.py            (~30 s)
 ── PLANS ──────────────────              ── EXECUTES ──────────────────
 wallet probe                            discover reward markets
 MarketROITracker.tick                   consume market_allocations.json
 DecisionPolicy.evaluate (6 triggers)    place / cancel / replace orders
 SimpleAllocator.compute                 detect fills → dump (unwind) inventory
 write market_allocations.json           ALL real-time guardrails + kill switches
        │                                        │
        └──► market_allocations.json ◄───────────┘   (2 h TTL)
        └──► bot_history.db (SQLite, WAL) ◄──────┘   (~31 tables; source of truth)
```

**⚠ Critical reading caveat:** the architecture doc's **§4 prose still describes the *legacy* stack**
(`oversight_agent.py` + `SafetyController` 7-state/14-invariant + β/η continuous allocator + bandit +
4-scalar learning loop). **That entire stack is rollback-only — present in the repo, NOT run in
production.** The authoritative live map is the architecture doc's **"Current Production State" table (v6.7)**,
not §4. **Live path = `simple_oversight.py` → `SimpleAllocator` (OverCommit) → `DecisionPolicy` (FX-051)
→ farmer runtime guardrails.**

**Two foundational ideas:**
- **Reward-global / loss-local asymmetry** — reward is one global scalar applied everywhere; loss is
  per-market. Reward-side errors propagate systemically; loss-side errors stay contained. Debug priority
  is always CF / scoring integrity first.
- **The one truly irreversible loop:** CF collapse → 0 deploys → q_share can't update → CF frozen →
  permanent shutdown. Only manual SQL recovery exits it.

---

## 2. Current live state (verified 2026-06-03 ~07:40 UTC)

- **Repo + Helsinki HEAD `b903c74` on `main`** (1:1, in sync). Local dev: `/Users/sabyasaachikarmakar/code/polymarket_bot`. Production: Hetzner Helsinki `/home/polymarket/Polymarket-bot`.
- **Mode:** `--mode live`, **cap-5 bounded canary** (`RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS=5`).
- **Wallet:** $1,118.31 cash, peak $1,201.76 → **drawdown ~6.9%**. **Net since the 17:30 UTC 06-02 restart ≈ −$27** over ~14 h (gross losses ~−$45, reward +$18). Objective net-positive **UNPROVEN**.
- **Rewards (on-chain, data-api):** 06-02 farming day settled **~$18** ($17.88 + $0.24) — ~3× the recent $5–6 baseline (the higher resting notional earns more scoring). Earlier days: 06-01 $6.41, 05-26 $4.78.
- **Tests:** 1118 pass / 2 skip (`pytest tests/ --ignore=tests/test_simulation.py`).
- **`config_overrides.json` (Helsinki, hot-reload):** `{RF_TRIAL_BUDGET_PCT:0.75, RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC:0.01, RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS:5, RF_TARGET_QUEUE_AHEAD_USD:4000, RF_FILL_BREAKER_WINDOW:900}`.

### What happened in the last session (2026-06-02 → 06-03)
1. Found the farmer **kill-switched + idle** since 15:55:40 UTC (06-02) — a **fill-rate spike** from a news-market adverse-fill burst ("Gemini Pro" market, 4 fills both sides in 28 min). This was the **2nd** such kill that day (same pattern), so it's a confirmed recurring failure.
2. **Verified the root mechanism** (measured, not guessed): the per-market fill breaker used a **180 s** window, so a market filling every ~10 min was always pruned before it tripped → it kept re-quoting → fills accumulated into the global fill-rate kill. The 30-min planner-side filters (FX-090 time-to-event, FX-093 volatility) are structurally too slow to catch a market that turns volatile *within one planner gap*.
3. **Fix (reversible, single-axis):** widened **`RF_FILL_BREAKER_WINDOW` 180 → 900** (config override, verified isolated — only `can_place` reads it; the fill-storm detector re-filters its own 300 s window). Restarted the farmer 17:30 UTC; it ran clean.
4. **Shipped `monitor_watchdog.py`** — a read-only 30-min cron watchdog on Helsinki that pages Discord on kill / stall / drawdown>12% / desync (alert-only by design: a kill is protective, so it escalates to a human rather than blind-restarting into a capital-drain loop). Adversarially tested; commits `4b94e7b`→`b903c74`.
5. Overnight: stable for ~6 h, then a **second incident at ~03:30 UTC 06-03** — see FX-094/095 below (a both-sides fill on a high-`min_size` market converted $106 cash into a ~$100 redeemable YES+NO pair → a *cash-only* drawdown metric false-tripped the 15% drawdown kill). The bot **auto-recovered** when the pair cleared (cash back to $1,118, drawdown <15%, oversight kill auto-cleared) and is farming again.

---

## 3. Honest capability assessment

- **Self-learning loop: 6/6 wired, but only ~1.5/6 *effective*.** Triggers fire on signals, but few have ever changed a real outcome productively; #4 (`global_reward_low`) is inert (FX-076); ROI cooldowns *do* fire (9+ active overnight) but reactively, after losses.
- **Capital efficiency: measured** (~1.4–2.4%/day gross) but **net-negative** after adverse fills.
- **Mission ~6/10, handoff-readiness ~6/10.** Foundation correct, farming safely, net-positive unproven.

---

## 4. OPEN ISSUES & BUGS (the important part)

### 4a. The unsolved CORE — market selection (deferred, needs design + owner sign-off)
The allocator ranks candidates by raw **`daily_rate × q_share`**, which structurally **over-weights
volatile / news / converging / extreme-priced markets** — exactly the ones that adversely fill us. The
30-min planner filters (FX-090 clock, FX-093 volatility) and the reactive FX-051 cooldown all act *too
slowly* to prevent fills on a market that turns within one planner gap. **This is the lever for
net-positive.** It is NOT to be re-architected blindly; it needs a designed, tested change (candidate
directions: a fast 30 s farmer-side adverse-selection/volatility guard; a stability-weighted ranker; a
pre-emptive cooldown that cools on the *first* adverse fill, not after $1/3 fills).

### 4b. NEW bugs found 2026-06-03 (this session) — not yet ticketed in fixit until now
| ID | Sev | Bug | Evidence | Fix direction |
|---|---|---|---|---|
| **FX-094** | **High** | **`merge_positions` is broken.** When the bot's both-sides reward quotes BOTH fill, it holds a YES+NO pair (worth $1/pair) and *should* merge it back to USDC. The call fails: `'ClobClient' object has no attribute 'merge_positions'` → it falls back to a lossy "dual dump." Same class as FX-035 (V1→V2 SDK method-name miss). | Helsinki log 03:27 UTC 06-03; the Becerra market filled 100 NO + 100 YES ($106) and couldn't merge. | Ground-truth the correct py-clob-client-v2 / on-chain CTF merge call; wire it; test. |
| **FX-095** | **High** | **Drawdown kill marks cash-only, not cash+inventory.** Buying inventory (a YES+NO pair, or any position) drops *cash*, which the 15% drawdown limb reads as drawdown and **false-trips the kill** even though portfolio value is intact. On 06-03 cash fell $106 into ~$100 of tokens → computed 15.3% drawdown (true ~7%) → killed. | Helsinki 03:30 UTC: kill at dd 15.3%, true portfolio (cash+tokens) ~$1,118 → ~7%. | Mark drawdown to cash + inventory value (the FX-084 unrealized infra exists). Safety-limb change → needs owner sign-off. |
| **FX-096** | Med | **FX-084 `unrealized_loss` over-counts.** Reported $19.1 on a position whose maximum possible loss was $14.40 (a long can't lose more than cost). Fail-safe direction (kill fires conservatively) but imprecise. | Helsinki GUARDRAIL log 02:29 UTC; flaps null↔$19.1. | Fix the mark-to-cost-basis computation. |
| **FX-097** | Med | **24 h cooldown expiry re-deploys known losers.** A market FX-051 cooled (e.g. Becerra) is re-deployed when the 24 h cooldown lapses, even though it's a repeat adverse-filler. | Becerra (`0xa5d79e71`) cooled ~00:41, re-deployed + both-sides-filled ~03:08–03:27. | Escalating/longer cooldowns for repeat losers; or exclude chronic losers. |

### 4c. Pre-existing open issues (from `Polymarket bot fixit.md`)
- **FX-076** (Med) — trigger #4 `global_reward_low` pulls a **non-binding** lever (EV gate binds, not the rate floor) → inert live.
- **FX-077** (Med) — reward-API HMAC query-string question; changelog says "confirmed not a bug" but the §2 row still reads Open — **reconcile this**.
- **FX-073** (Med, partial) — notional-guardrail headroom at the 5× operating point.
- **FX-042** (Med) — `orders_cancelled` table never written by the production path → will corrupt fill-model training labels once the calibrator activates.
- **FX-038** (Med) — `_reconcile_positions` doesn't compensate `fills`/`unwinds` → phantom rows bias the hourly-loss metric.
- **FX-047 / FX-033 / FX-034** (Low/contingent) — legacy-path threshold + unliquidatable-reprobe items.
- **FX-046** (Accepted Risk) — q_share formula under-predicts payouts 24–94×; API q_share is ground truth, conservative-margin knob mitigates.

### 4d. Behaviors that look alarming but are NORMAL (don't "fix" these)
- **One-sided placement** (per-side gating; reason in `placement_feedback`).
- **Single-cycle wallet desyncs** that net to ~zero across two cycles = benign fill-recording lag (the bot's fills table lags the on-chain wallet by seconds across a reconcile boundary). Only a **persistent (≥2-3 consecutive same-direction-growing)** desync means real missing money.
- **Wallet cash dipping when resting notional is high** = collateral reservation; recovers when orders clear (`reconcile=ok` confirms).
- **Net-negative-but-stable** is the current expected state — only kill/crash/stall/real-desync/runaway-loss is "broken."

---

## 5. Safety stack (kills & guardrails)

| Limb | Trigger | Notes |
|---|---|---|
| Realized-loss kill | 24 h realized loss > 10% wallet | farmer + oversight |
| Drawdown kill | drawdown > 15% (oversight) / FX-082 farmer backstop on oversight-silence | **⚠ FX-095: cash-only → false-trips** |
| Unrealized-loss kill (FX-084) | held-inventory mark-down > 20% | **⚠ FX-096: over-counts** |
| Fill-rate spike kill | 1h/6h fill ratio > 3× (baseline ≥5) | hair-trigger on a 5-market canary; fired 2× on 06-02 |
| Per-market fill breaker | ≥2 same-side / ≥3 total fills in `RF_FILL_BREAKER_WINDOW` (now **900 s**) | blocks new placement on that market |
| CF-collapse kill | CF < 0.01 | |
| Oversight-silence backstop (FX-082) | oversight silent > 2 h + exposure + drawdown | |
| Dump slippage floor (FX-071) | floors dump SELL at cost×0.95 | holds inventory rather than crystallizing >5% loss |
| Alerts → Discord | kill (FX-092), wallet-desync (FX-074), heartbeat stall (FX-083) | + `monitor_watchdog.py` cron (this session) |

**Two kill paths differ:** the **farmer fill-rate kill** (`_activate_kill_switch`) is **sticky** — needs a process restart. The **oversight drawdown/loss kill** (writes `kill_switch` to the alloc, FX-068) **auto-clears** when the condition recovers.

---

## 6. File manifest (repo-relative = Helsinki path, 1:1)

### 6a. LIVE core
`reward_farmer.py` (farmer loop, placement, fills, dump, guardrails/kills) · `simple_oversight.py` (planner) ·
`simple_allocator.py` (`SimpleAllocator`/OverCommit: scoring, EV gate, FX-090 time filter, FX-093 vol filter, kill switch) ·
`market_roi_tracker.py` (per-market ROI; reward from data-api) · `decision_policy.py` (5 behavior-change outputs) ·
`order_lifecycle.py` (place/replace/TTL, fill detection, `can_place` breaker) · `dump_manager.py` (unwind, FX-071 floor) ·
`database.py` (`BotDatabase`, ~31 tables, WAL) · `config.py` (`RF_*` knobs + hot-reload)

### 6b. LIVE support
`models.py` · `alerts.py` (Discord) · `market_discovery.py` · `price.py` · `state.py` (PositionStore) ·
`rate_limiter.py` · `reward_tracker.py` (telemetry only) · `oversight/wallet_reconciliation.py` (FX-049/055) ·
**`monitor_watchdog.py`** (NEW — 30-min health watchdog, cron `*/30`, Discord, alert-only)

### 6c. ⚠ Live-used helpers inside otherwise-legacy modules (do NOT delete)
`oversight/data_collector.py` (`_connect_db`) · `profit/correlation.py` (`build_fill_clusters`) ·
`oversight_agent.py` (legacy planner, but farmer imports a no-op `evaluate()` shadow hook)

### 6d. Docs / share set
`README.md` · `ground_rules.md` (immutable contract) · `docs/HANDOFF.md` ·
`Polymarket bot architecture v5.1.md` (content = **v6.7**) · `Polymarket bot fixit.md` (**v1.38**) ·
**`docs/SYSTEM_CONTEXT.md` (this file)** · `docs/STATUS_2026-06-03.md` (latest session) ·
`docs/STATUS_2026-06-02.md` / `_05-31` / `_05-29` · `docs/runbooks/live_canary_operator.md` (live ops) ·
`docs/runbooks/{deployment_from_scratch,9_of_10_p5_p7_operator_runbook,stage_c_pull_2026-05-31}.md` · `CHANGELOG.md`

### 6e. Tests + config
`tests/` (59 `test_*.py` + conftest; **1118 pass / 2 skip**) · `requirements.txt` (Python 3.14; `py-clob-client-v2==1.0.0`) ·
`.env.example` · `.github/workflows/test.yml`

### 6f. Server-only (NOT in repo — Helsinki)
`/etc/systemd/system/polymarket-{farmer,oversight}.service` · `config_overrides.json` (see §2) ·
`bot_history.db` (WAL) · `market_allocations.json` · `.env` (**secrets — never share**) · `logs/` (+ `logs/watchdog.log`) ·
crontab `*/30 * * * * … monitor_watchdog.py`

### 6g. Legacy / ignore for understanding the live system
`bot.py`, `main.py`, `oversight_agent.py`, `oversight/{safety_controller,market_scorer,allocation_writer}.py`,
`profit/*` (allocator/learning/bandit/regime/sizing/…), `calibration/*`, `simulation/*`, `paper_*`, `simulate.py`,
`backtest.py`, `arbitrage.py`, the 7 root-level `test_*.py` (real suite is `tests/`), `humanpending.md` (ad-hoc, not load-bearing).

---

## 7. Operations

- **SSH:** `ssh -i ~/.ssh/polymarket_bot_ed25519 polymarket@46.62.209.203` (Hetzner `hel1`).
- **Halt (graceful, cancels orders):** `sudo systemctl stop polymarket-farmer`
- **Restart (clears a sticky farmer kill):** `sudo systemctl restart polymarket-farmer` (passwordless sudo works).
- **Authoritative P&L (on-chain, no auth):** `https://data-api.polymarket.com/activity?user=0xB23Bc80E6719099aeBE0c34389f05EC8C928503f&type=REWARD` (+ `MAKER_REBATE`, `/positions`). Rewards settle daily ~00:00–00:20 UTC.
- **Wallet truth:** `wallet_reconcile_history.actual_wallet` (on-chain-derived) + portfolio peak; or query CTF balances directly.
- **Read-only DB probe:** `sqlite3 'file:bot_history.db?mode=ro' "<SQL>"`.
- **Monitor:** `monitor_watchdog.py` runs every 30 min via cron and pages Discord on anomalies (durable, survives laptop sleep). The bot's own FX-092/074/083 alerts also page Discord.

---

## 8. Operating principles (every change, every session) — `feedback_ground_truth_only` + framework P1–P5

- **Ground truth, not guesswork.** State only what's verified in data; say "I'm unsure" explicitly; dig deeper every time.
- **P1 Verified > assumed · P2 Reversibility first · P3 Single-axis changes · P4 Production cycles > tests · P5 Friend rollout = ≥7 days clean.**
- For any change: grounded + reversible + adversarially tested. No branding in commits/repo (no Claude/Anthropic). Solo contributor, `main` only.
- A protective kill escalates to a human — do **not** blind-restart it (capital-drain-loop risk).

---

## 9. Reading order for a newcomer
`README.md` → `ground_rules.md` → **this file (`docs/SYSTEM_CONTEXT.md`)** → architecture doc *(Current-Production-State table only)* → `docs/STATUS_2026-06-03.md` → `docs/runbooks/live_canary_operator.md` → `Polymarket bot fixit.md`.
