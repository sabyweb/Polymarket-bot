# Plan — Two Agent Loops for the Polymarket Bot

**Status: DESIGN ONLY. No code written, nothing deployed.** This document is for review.
We lock it first, then implement in reversible phases.

Two loops, deliberately separate:
- **Loop A — Daily soak monitor.** Read-only. Tells you how the live canary is doing. Safe today.
- **Loop B — Offline market-selection research loop.** Sweeps the (already-built) selection knobs
  against the simulator/backtester, scores them, and hands *you* a ranked comparison. Never touches
  production or capital.

---

## 0. Non-negotiable constraints (carried from your ground rules)

These bound every design choice below.

1. **No loop deploys capital, edits live `config_overrides.json`, restarts a service, or clears a kill.** Loops produce *evidence and reports*; humans act.
2. **Read-only on anything live.** Live `bot_history.db` is opened `mode=ro`; sims/backtests run on a **copy**, never the live WAL.
3. **Single-axis (P3).** One knob changes per candidate. No loop is allowed to emit a multi-knob recommendation as "ready to ship."
4. **Reversible + adversarially tested (P2).** Every artifact is reproducible from inputs; a separate verifier checks every candidate.
5. **Verification stays human.** "The loop says it's better" is never the thing that authorizes a live rollout. It feeds your existing staged Wave-4 process.
6. **No AI/Anthropic branding** in any commit or file the loop writes.
7. **Token cost is a first-class budget**, metered and capped (see §5).

---

## 1. What already exists (so we build less than it sounds)

Confirmed in the repo today:

| Capability | Where | State |
|---|---|---|
| Volatility penalty in ranking `reward/(1+k·vol)` | `simple_allocator.py:640` via `cfg("RF_RANK_VOL_PENALTY_K")` | implemented, default `0.0` (off) |
| Per-market capital cap | `simple_allocator.py:739` via `cfg("RF_MAX_CAPITAL_PER_MARKET_USD")` | implemented, default `0.0` (off) |
| Recent-volatility exclusion filter | `simple_allocator.py:78`, `order_lifecycle.py:860` via `cfg("RF_ALLOC_MAX_RECENT_VOLATILITY")` | implemented, default `0.10` |
| Pre-emptive cooldown | `decision_policy.py:469` via `cfg("RF_PREEMPTIVE_COOLDOWN_ENABLED")` | implemented, default `False` |
| `_recent_volatility(cid)` data source | `simple_allocator.py:461` (reads `book_snapshots`) | implemented |
| Backtest with config overrides | `backtest.py` — `--days N`, `--override KEY=VALUE` (repeatable), `--compare A B` | implemented |
| Invariant simulation (safety) | `simulation/run_audit_v5.py` — `--cycles --seeds --scenarios`, emits INV3/INV5/INV7 verdicts + audit JSON | implemented |
| Adverse vs favourable fill labelling | `reward_tracker.py` `record_fill_quality()` → `reward_market_stats.data` | implemented |
| Per-market reward-vs-damage view | dashboard "Market Selection" tab | implemented (this session) |
| Test suite | `tests/` (~60+ files); fast tier `pytest tests/ --ignore=tests/test_simulation.py` | implemented |

**Implication:** Loop B is mostly *orchestration + judgement* over machinery that already exists and
is already unit-tested. The agents set knob values, run the existing backtester/simulator, and compare
outputs. They do **not** invent new trading logic. This is the single biggest risk reduction in the plan.

**Open item to verify before build (§6):** confirm whether `backtest.py` replays from the live
`bot_history.db` directly. If so, Loop B must point it at a copied snapshot, not the live file.

---

## 2. Loop A — Daily soak monitor (the heartbeat)

### Purpose
Once a day, summarise how the live canary is actually doing and surface anything that needs your eyes —
so you are not manually SSHing and running queries every morning during the soak/G-E clock.

### Trigger
Scheduled, once daily, shortly after rewards settle (~00:30 UTC, since settlement is ~00:20 UTC).

### Steps (all read-only)
1. Open a **read-only copy/handle** of `bot_history.db` (`mode=ro`).
2. Pull: yesterday's `reward_daily` (reward + rebate), `fills`/`unwinds` deltas, `safety_state`
   transitions, `wallet_reconcile_history` divergence, `correction_factor_history`, and the worst
   adverse-selection markets from `reward_market_stats` (same parse the dashboard uses).
3. Pull the authoritative number from the data-api (`/activity?type=REWARD` + `MAKER_REBATE`) — the
   trusted source per your runbook §7.
4. Compute: net P&L vs reward for the day, soak-gate progress (Wave-1 48h, G-E 7-day clean clock),
   any kill events, any heartbeat staleness.
5. Write a dated entry to a **memory file** (`docs/soak_log.md` or similar) — append, never overwrite.
6. Post a short digest to Discord (reusing `alerts.py` patterns) and/or surface in the dashboard.

### Output
A dated markdown digest + Discord message. Example shape: "06-09: net −$X, reward +$Y (data-api),
3 new repeat-loser markets, no kills, Wave-1 soak 31/48h, G-E clock not started."

### What it must NEVER do
Restart anything, edit config, clear a kill, place/cancel orders, or write to the live DB. If it
detects a kill or a real desync, it **reports** — it does not act. (Your runbook: a protective kill
escalates to a human.)

### Where it runs (decision needed — §6)
- **Option A1 (simplest):** a scheduled task on your laptop that SSHes in read-only. Only runs when the laptop is on.
- **Option A2 (robust):** a `systemd` timer or cron on Helsinki (sibling of `monitor_watchdog.py`), output to Discord + a repo file pulled on demand.
- **Option A3:** GitHub Actions on a schedule (needs read access to data-api + a way to reach the DB; more plumbing).

Recommendation: **A2** — it lives next to the bot, survives your laptop being closed, and matches the existing watchdog pattern.

---

## 3. Loop B — Offline market-selection research loop

### Purpose
Each run, trial the candidate selection knobs against historical/simulated data, score them on
reward-vs-adverse-fill-damage, gate them on safety invariants + tests, and produce a ranked comparison
for you to choose from. The agents do the legwork; you make the call.

### The knob grid (single-axis — each candidate varies ONE knob)
| Knob | Off (baseline) | Sweep values to trial |
|---|---|---|
| `RF_RANK_VOL_PENALTY_K` | 0.0 | 0.25, 0.5, 1.0, 2.0 |
| `RF_MAX_CAPITAL_PER_MARKET_USD` | 0.0 | 40, 80, 120 |
| `RF_ALLOC_MAX_RECENT_VOLATILITY` | 0.10 | 0.05, 0.08, 0.15 |
| `RF_PREEMPTIVE_COOLDOWN_ENABLED` | false | true |

(Exact ranges are an §6 open item — these are placeholders for review.)

### Execution
1. **Snapshot** the live DB to a read-only copy (or use an existing paper/historical DB).
2. **Baseline run:** `python3 backtest.py --days N` with all knobs at default → reference metrics.
3. **Candidate runs (parallel fan-out):** for each (knob, value), run
   `python3 backtest.py --days N --override KEY=VALUE` (or `--compare baseline.json candidate.json`),
   each writing to a **distinct output path** so they don't collide.
   - Worktrees are only needed if a candidate requires a small code tweak; pure value sweeps don't need
     them (the `--override` flag is enough). We'll use worktrees only for the rare wiring change.
4. **Verifier (maker/checker split):** a separate agent (different instructions, ideally stronger model)
   for each promising candidate:
   - runs `python3 -m simulation.run_audit_v5 --seeds 1 42 1337` to confirm INV3/INV5/INV7 still pass,
   - runs the fast test tier `pytest tests/ --ignore=tests/test_simulation.py`,
   - confirms the candidate is a single-axis change and is reversible,
   - flags overfitting (see §5) by re-scoring on a **held-out** date window.
5. **Score & rank** by net = reward + spread_capture − fill_damage, with secondary columns: adverse-fill
   ratio, drawdown, capital efficiency, deploy count. Compare each candidate to baseline.
6. **Memory:** append results to `docs/selection_experiments.md` — what was tried, on which window, the
   scores, pass/fail on invariants. So experiments compound instead of re-running from zero.

### Output
A ranked comparison table + the verifier verdicts, written to the memory file and surfaced to you. Each
row: knob, value, Δnet vs baseline, Δadverse-ratio, invariants pass/fail, tests pass/fail, overfit flag.

### Human handoff
You read the comparison and decide whether a knob enters your existing **staged Wave-4 rollout**
(`config_overrides.json` edit + restart, soak gate). The loop never makes that edit.

### Where it runs
Offline, on a copy — your laptop or a non-prod box. Never on the live trading process.

---

## 4. End-to-end flow (text diagram)

```
LOOP A (daily, read-only)
  cron/timer ──> read-only DB copy + data-api ──> compute soak metrics
            ──> append docs/soak_log.md ──> Discord digest ──> (human reads)
            [never writes live state]

LOOP B (on demand / scheduled, offline)
  snapshot live DB ──> baseline backtest
                   ──> fan-out: backtest --override per (knob,value)   [parallel, distinct outputs]
                        └─ maker subagent runs each
                   ──> verifier subagent per candidate:
                        run_audit_v5 (INV3/5/7) + fast pytest + held-out re-score
                   ──> score & rank vs baseline
                   ──> append docs/selection_experiments.md
                   ──> ranked report ──> (human picks ──> existing Wave-4 staged rollout)
                        [never edits live config / never deploys]
```

---

## 5. Failure-mode analysis + designed fixes

This is the part you asked to harden. Each row: how it breaks → impact → designed mitigation.

### Loop B (research) — correctness/economics failures
| # | Failure mode | Impact | Designed fix |
|---|---|---|---|
| B1 | **Overfitting to the replay window** — a knob looks great on the N days we backtested, fails live. | Ship a fix that loses money. | Train/test split: tune on window 1, **re-score on a held-out window 2**; require improvement on *both*. Report both numbers. Never recommend on a single window. |
| B2 | **Simulator ≠ live** (sim fidelity gap; FX-046 notes heuristic q_share is 24–94× off). | Backtest ranks a knob that won't reproduce live. | Treat backtest as a *filter, not proof*. Anything it likes still goes through the real staged Wave-4 soak on the canary before any cap increase. Cross-check sim adverse-fill rates against the live `reward_market_stats` distribution; flag large divergence. |
| B3 | **Lookahead / survivorship bias** in the replay (using data the bot wouldn't have had). | Inflated backtest results. | Verifier audits the backtest window for lookahead; restrict to markets/data available at decision time. Document the replay's data-availability assumptions in the memory file. |
| B4 | **Non-determinism across seeds** — result flips by seed. | False ranking. | Always run `run_audit_v5` across the 3 standard seeds (1, 42, 1337); require the *sign* of the improvement to hold across all seeds. Report variance. |
| B5 | **Multi-axis leakage** — agent recommends two knobs together. | Violates P3; un-attributable. | Hard rule in the scorer: one candidate = one knob delta vs baseline. Reject/relabel anything else as "needs separate trials." |

### Loop B — safety/process failures
| # | Failure mode | Impact | Designed fix |
|---|---|---|---|
| B6 | **Production contamination** — a sim writes to the live DB or live `config_overrides.json`. | Could disturb the live bot. | Loop B operates only on a **copied** DB and **temp** config files in an output dir; it has no path to live config. Run as a user without write access to the live repo if on the same box. Verifier asserts no live-path writes occurred. |
| B7 | **Reading the live WAL while the bot writes it.** | Lock contention / partial reads. | Snapshot via `sqlite3 .backup` (or file copy of a quiesced read-only handle) before backtesting; never attach to the live WAL. |
| B8 | **Verifier rubber-stamps the maker** (same model, too agreeable). | "Verified" means nothing. | Maker and checker are *separate* agents with different instructions and, ideally, different models/effort. Checker's job is adversarial: reproduce the metric independently, not re-run the maker's command. |
| B9 | **A candidate breaks invariants but scores high on P&L.** | Tempting to ship something unsafe. | Invariant gate is **blocking**: any INV3/INV5/INV7 FAIL or any failing fast-test disqualifies a candidate regardless of P&L. P&L is only compared among invariant-passing candidates. |

### Loop B — cost/operational failures
| # | Failure mode | Impact | Designed fix |
|---|---|---|---|
| B10 | **Token blowup** — fan-out × subagents × daily runs balloons cost. | Surprise bill. | Per-run token ceiling + max parallel subagents (e.g. ≤4). Backtests are plain Python (cheap); spend agent tokens only on the verifier judgement, not on running loops. Cap grid size per run. Log token usage to the memory file. Start **on-demand, not scheduled**, until cost is understood. |
| B11 | **Worktree/disk sprawl** — leftover checkouts and `audit_*` outputs fill disk. | Box runs out of space. | Use auto-cleaning worktrees (`isolation: worktree`); write outputs to a single dated dir; a cleanup step prunes runs older than K days. |
| B12 | **Scheduled runs overlap / pile up.** | Concurrent runs corrupt shared outputs. | A lockfile guard: a new run aborts if the previous one is still going. Distinct per-run output dirs. |
| B13 | **Memory file corruption / concurrent append.** | Lose experiment history. | Append-only with a timestamped section per run; never rewrite prior entries. If on git, commit each run's entry as its own commit (no AI branding). |

### Loop A (monitor) — failures
| # | Failure mode | Impact | Designed fix |
|---|---|---|---|
| A1 | **False "all clear"** — query silently returns empty (e.g. stale DB) and it reports healthy. | You miss a real problem. | Freshness checks first: if last fill/cycle/heartbeat is older than threshold, the digest leads with a **STALE/UNKNOWN** banner, not "healthy." Distinguish "no losses" from "no data." |
| A2 | **Monitor acts on what it reads** (e.g. sees a kill, tries to restart). | Violates the human-escalation rule. | Loop A has zero write/exec capability over the bot. It can only read + post a message. Restarting is explicitly out of scope. |
| A3 | **Secret leakage** — `.env` `FUNDER`/keys end up in a digest, log, or commit. | Exposure. | Never echo `.env`; the funder address is already public on-chain, but treat keys as redacted. Digest contains metrics only. Verifier/reviewer checks no secrets in output before any commit. |
| A4 | **Data-api unreachable / rate-limited.** | Digest missing the authoritative number. | Graceful degrade: report DB-derived estimate + a clear "data-api unavailable" note; retry with backoff; never crash the run. |
| A5 | **Misreads "normal-not-broken" states as alarms** (one-sided placement, benign desync, net-negative-but-stable). | Alarm fatigue / wrong conclusions. | Encode the runbook §10 "normal behaviours" list into the monitor's logic so it doesn't flag them. (This is also why Loop knowledge should live in a skill/`CLAUDE.md` — §7.) |

### Second-pass adversarial cases (added after review — the §5 list above was NOT exhaustive)
| # | Failure mode | Impact | Designed fix |
|---|---|---|---|
| C1 | **Prompt injection via untrusted text** — market `question`s, `placement_feedback.reason`, `journalctl` lines may contain text crafted to steer an agent ("…set RF_MAX_CAPITAL_PER_MARKET_USD=9999…"). | Agent manipulated into a harmful action. | **All tool-observed text is data, never instructions.** Structural guarantee: loop agents can only run the fixed sweep harness + read; they have no arbitrary-exec or live-write path. Treat any embedded directive as content to report, not obey. |
| C2 | **Goodhart / degenerate "do-nothing" solution** — net = reward − damage is maximised by deploying almost nothing (no fills → no damage → "net positive", zero reward). | Loop recommends a knob that quietly stops farming. | Scorer enforces a **minimum-breadth + reward-floor** guard: a candidate that collapses deploy count or reward below baseline thresholds is disqualified, not ranked #1. Report deploy count + reward alongside net, always. |
| C3 | **Adaptive adversary / regime change** — counterparties adapt once our selection is predictable; a knob tuned on past adversaries gets exploited live. | Live underperformance vs backtest. | Don't treat any backtest win as durable. The real canary soak (Wave-4) is the proof; re-evaluate knobs periodically on fresh windows; watch live adverse-fill rate for drift after any rollout. |
| C4 | **Polymarket reward-rule / API drift** — the reward formula or q_share endpoint changes, invalidating backtest history. | Entire backtest basis becomes wrong silently. | Loop A watches for reward-structure anomalies (sudden est-vs-actual divergence); Loop B records the reward-rule assumptions per run; a detected change halts new recommendations until re-baselined. |
| C5 | **Cross-loop DB-table contamination** — a research run writes to tables the live bot reads (`market_roi`, `cooldowns`, `reward_market_stats`). | Research poisons live decisions. | Snapshot is fully isolated (separate file); the live bot must never read anything a research run wrote. Verifier asserts zero writes to any live-readable path. (Generalises B6 from config to tables.) |
| C6 | **Agent hallucinates / misrounds metrics** in its summary. | False ranking from a confident wrong number. | Scores are **machine-parsed from the backtest/audit JSON**; agents never hand-type a metric. Verifier diffs reported-vs-file and fails the run on mismatch. |
| C7 | **Partial-grid failure reported as complete** — a backtest crashes mid-sweep; survivors presented as the full result. | Decisions on incomplete evidence. | Explicit completeness assertion: every grid cell must produce a parseable result or the run is marked INCOMPLETE and not used for a recommendation. |
| C8 | **Torn snapshot / disk capacity / timezone gate math** — `cp` of a live WAL tears; ~200 MB × parallel copies fills disk; 48h/7-day gate arithmetic is off-by-timezone. | Corrupt input, full disk, or wrong "soak complete". | Use `sqlite3 .backup` (not `cp`); capacity pre-check before snapshot; all gate math in explicit UTC with the source timestamp shown in the digest. |
| C9 | **Observer effect** — Loop A's queries load/lock the DB and slow the farmer's 30s cycle. | Monitor degrades the thing it measures. | Read from a snapshot or with `mode=ro` + short timeout; run off the farmer's busy window; never hold a long transaction. |
| C10 | **Governance / scope creep** — someone later wires Loop B to auto-edit config "because it's reliable." | The human-approves-deploys invariant erodes by drift. | That transition is explicitly out of scope and requires a deliberate, documented human decision — never an incremental change. Stated as a standing invariant in the knowledge skill (Phase 0). |
| C11 | **Trusted source is wrong** — the data-api returns a stale/incorrect value and is taken as ground truth. | Wrong P&L conclusion. | Cross-check data-api against DB-derived estimates; flag large divergence rather than silently trusting either; never auto-act on a single source. |

**Completeness statement:** this list is a strong two-pass effort, not a proof of exhaustiveness — no adversarial list ever is. The standing control is that the **verifier subagent performs its own adversarial pass each run** (per §3 step 4) and this table is a *living* document appended to as new cases surface in operation.

---

## 6. Locked decisions (operator chose "use your recommendations" — 2026-06-09)

1. **Where each loop runs:** Loop A → **Helsinki `systemd` timer** (sibling of `monitor_watchdog.py`, survives laptop-off). Loop B → **offline on a copied DB** (laptop or any non-prod box); never on the live process.
2. **Backtest DB source:** treat as live regardless — Loop B **always runs against a `sqlite3 .backup` snapshot**, never the live file. (Still confirm the exact read path in Phase 2, but the snapshot rule stands either way.)
3. **Token budget:** Loop B starts **on-demand, not scheduled**; **≤4 parallel subagents**; grid **≤12 cells/run**; agent tokens spent only on the verifier's judgement (backtests are cheap Python); usage logged to the memory file. We set a hard ceiling number after the first few runs show real cost.
4. **Knob grid:** accept the §3 placeholder ranges for the first pass; refine from results.
5. **Output sinks:** Loop A → Discord digest + `docs/soak_log.md`. Loop B → `docs/selection_experiments.md` now, optional dashboard "Experiments" tab later.
6. **Replay window:** tune on a **7-day** window, re-score on a **second non-overlapping 7-day** held-out window (B1); confirm ≥14 days of usable history exists in Phase 2, else shrink windows.
7. **Cadence:** Loop A **daily** from the start (safe); Loop B **on-demand first**, scheduled only once token cost is understood.

---

## 6b. Open questions to resolve before we lock (superseded by §6)

1. **Where does each loop run?** (Loop A: laptop task vs Helsinki timer vs GH Actions — I recommend Helsinki timer. Loop B: laptop vs non-prod box.)
2. **Does `backtest.py` read the live `bot_history.db`?** If yes, we must snapshot first. (Quick to confirm.)
3. **Token budget ceiling** per Loop B run, and max parallel subagents?
4. **Knob grid + value ranges** — accept §3 placeholders or adjust?
5. **Output sinks** — Loop A: Discord + repo file + dashboard tab? Loop B: repo memory file only, or also a dashboard "Experiments" tab?
6. **Backtest replay window N** (days) and whether we have ≥2 non-overlapping windows for the train/test split (B1).
7. **Start cadence** — I recommend Loop A daily from the start (safe), Loop B **on-demand first**, scheduled only once token cost is known.

---

## 7. Phased implementation (ONLY after the plan is locked)

Each phase is independently reversible and adds capability without risking the live bot.

- **Phase 0 — Knowledge skill / `CLAUDE.md`.** Consolidate ground rules, the "normal-not-broken" list, safety invariants, and the no-AI-branding rule so every agent run starts warm. (Prevents A5/B-class mistakes.)
- **Phase 1 — Loop A monitor (read-only).** Script the daily digest; dry-run it by hand; then schedule. Lowest risk, immediate value.
- **Phase 2 — Loop B manual sweep.** A plain script that runs the baseline + grid via `backtest.py --override` and prints a ranked table. No agents yet — just verify the methodology and the metrics.
- **Phase 3 — Wrap Loop B in maker/checker subagents** with the invariant + test + held-out gates.
- **Phase 4 — Memory + (optional) scheduling** with token caps and lockfile.

We stop after each phase, confirm it behaves, and only then proceed.

---

## 8. What I need from you to lock this

Answer the §6 open questions (or say "use your recommendations"), and tell me if the failure list in §5
is missing anything you're worried about. Once locked, I start at Phase 0 and we go phase by phase — no
code lands without your sign-off on the plan.
