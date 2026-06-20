# A3 — candidate_features survivorship log: build plan (2026-06-20)

> **Status: DESIGN — fixed + hardened, ready to implement.** Extends `RECURSIVE_LOOP_BUILD_PLAN_2026-06-20.md` §3 (A3)
> and `RECURSIVE_SELECTION_LOOP.md` §3/§8 Phase 0. This is the **first change that touches the live planner code**,
> so the bar is: behavior-neutral (0 behavioral axes), gated, reversible, fail-open, proven byte-identical.

## 0. Objective + why
Close **survivorship**. Today the bot only has outcomes for markets it ENTERED; the avoided set is unlogged on the
live path (`market_selection_log` is legacy `bot.py` only — verified, FINDINGS §10). Log every **eligible** candidate's
decision-time feature vector + the deploy/avoid decision + reason, so offline analysis sees the **counterfactual
surface** (deployed AND avoided), not just survivors. This is the prerequisite for any honest feature→outcome study —
without it, every separation analysis is survivorship-biased.

## 1. The invariant (non-negotiable for this step)
A3 only **records**; it NEVER gates selection or sizing. **0 behavioral axes** (the master invariant). Proof
obligation: an **output-byte-identical test** — `compute()`'s `deploys` list must be identical with the flag ON vs OFF.
If A3 ever changed a deploy decision it would become a behavioral axis and must be sequenced, not parallelized.

## 2. Design (grounded in code)
1. **Config knob** `RF_CANDIDATE_FEATURE_LOG_ENABLED` (default **False**) in `config.py`. `cfg()` missing → None → off.
2. **`AllocationResult`** (`simple_allocator.py:175`) gains `candidate_features: list[dict] = field(default_factory=list)`
   — backward-compatible; the existing constructors at `:741` and `:1036` don't pass it → default `[]`.
3. **Capture** — a SINGLE block in `compute()` **after** the deploy loop + avoids are finalized (`simple_allocator.py:~1010`),
   **behind the flag**, wrapped in `try/except` → on ANY error set `candidate_features=[]` and continue (fail-open;
   compute's decision path is untouched). Pure reads of each `m`'s attributes into NEW dicts (no mutation of `m`).
   - **Scope: the ELIGIBLE set only** (`deploys` + in-loop `avoids`) — the decision boundary. The large non-eligible
     tail (failed `min_rate`/`extreme_price`/`cooldown` before the loop) is logged as an **aggregate count**, not
     per-row (volume + low information — see A3-RT5).
   - **Feature vector** (all ALREADY computed in the normal flow → zero added work): `condition_id`, `cohort`
     (`_ab_cohort(cid)`), `action` (deploy|avoid), `reason` (`timing_excluded_reason`/`event_guard_reason` or ""),
     `daily_rate`, `max_spread`, `min_size`, `midpoint_guess`, `expected_q_share`, `q_share_source`,
     `expected_daily_reward`, `target_shares`, `target_capital`, `end_date_iso`, `game_start_time`, `question`.
     `recent_volatility`/`recent_sweep` only where the loop already computed them (else null — offline-backfillable
     from `book_snapshots`, the same source, so no new per-candidate DB query is added).
4. **Write** — in `simple_oversight.run_once`, **AFTER `write_allocation_json` (`:500`)** so a feature-write failure can
   never block the critical alloc file — **fail-quiet** (mirror the `snapshot_capital` try/except at `:493-497`).
   Writes `result.candidate_features` to an **isolated** `candidate_features.db` (NOT `bot_history.db`).
5. **New module** `candidate_features_log.py`: `ensure_schema(db_path)` (CREATE TABLE IF NOT EXISTS) + `append(db_path,
   cycle_ts, records, nonelig_count)` in one batched transaction. Schema: `id, ts, cycle_ts, condition_id, cohort,
   action, reason, daily_rate, max_spread, min_size, midpoint_guess, expected_q_share, q_share_source,
   expected_daily_reward, recent_volatility, recent_sweep, target_shares, target_capital, end_date_iso,
   game_start_time, question`; index `(cycle_ts)`; a per-cycle meta row for `nonelig_count`.

## 3. What it does NOT do
No behavior change; no gating; no new per-candidate computation; no write to `bot_history.db`; no logging of the full
non-eligible universe; `question` text stored as DATA (never executed; offline analysis treats it as a feature, per
CLAUDE.md §7).

## 4. Critical-evaluator pass (red-team) + hardening
| # | Failure mode | Why it bites | Hardening |
|---|---|---|---|
| A3-RT1 | capture mutates `m` / perturbs the decision | becomes a behavioral axis | pure reads into new dicts; capture AFTER deploys/avoids finalized; **byte-identical test proves it** |
| A3-RT2 | capture block RAISES | breaks `compute()` → no alloc → farmer goes stale → drawdown backstop/TTL fires | entire block in `try/except` → `candidate_features=[]`; core path untouched; fail-open test |
| A3-RT3 | oversight write RAISES | breaks `run_once` → no alloc file | write placed AFTER `write_allocation_json`; fail-quiet (mirror `:493-497`) |
| A3-RT4 | DB write latency/lock slows the 30-min planner | observer effect | isolated DB (no contention w/ bot_history); one batched txn; once per 30-min cycle; fail-quiet on lock |
| A3-RT5 | disk/volume blowup (logging ~1.7k mkts × 48/day) | DB bloat, slow queries | log only the ELIGIBLE set (~100-500/cycle); non-eligible as an aggregate count; index + retention note |
| A3-RT6 | prompt injection via `question` text | untrusted text stored as a feature | stored as DATA; offline analysis treats it as a feature, never an instruction (standing rule) |
| A3-RT7 | knob missing / wrong type | accidental on/off | default False in config.py; `cfg()` None→off; type-checked by the config loader |
| A3-RT8 | tests can't exercise compute() (needs API) | untestable | `compute(markets=<synthetic>)` bypasses discovery; no DB state → `_recent_volatility` fail-opens → deterministic |
| A3-RT9 | non-deterministic deploys break the byte-identical test | false failure | controlled synthetic candidates + no relevant DB state → deterministic; assert cids+shares+capital+order |
| A3-RT10 | candidate_features.db path unwritable | silent loss | configurable path (default beside bot_history.db); fail-quiet on unwritable (acceptable: it's instrumentation) |
| A3-RT11 | schema first-run / migration | crash on first write | `CREATE TABLE IF NOT EXISTS`; idempotent ensure_schema |

## 5. Tests (the gate)
- `test_output_byte_identical` — flag OFF vs ON → `deploys` identical (cids, target_shares, target_capital, order). **The proof of 0 behavioral axes.**
- `test_capture_fail_open` — inject a capture error → `compute()` still returns valid deploys, `candidate_features=[]`.
- `test_capture_records` — flag ON → records carry correct `action`/`cid`/`cohort` for deploys + in-loop avoids.
- `test_log_module` — `ensure_schema` idempotent; `append` writes rows; fail-quiet on a bad path.
- **Full gate** (this IS a live-planner code change, so the full gate applies — unlike the pure-offline A1/A2):
  `python3 -m simulation.run_audit_v5 --seeds 1 42 1337` (INV3/5/7) + `pytest tests/ --ignore=tests/test_simulation.py -q`.

## 6. Rollout (operator-gated; I do not deploy/restart)
1. I write + gate the code; **flag default OFF → byte-identical → safe to ship**.
2. Operator deploys (git pull + restart `polymarket-oversight`) — behavior unchanged (proven by the byte-identical test).
3. Operator enables `RF_CANDIDATE_FEATURE_LOG_ENABLED=true` (hot-reload) — logging begins, still behavior-neutral.
- **Revert:** flag off (hot-reload) stops logging instantly; `rm candidate_features.db` removes the data; with the flag
  off the new code is inert. Fully reversible at every step.
