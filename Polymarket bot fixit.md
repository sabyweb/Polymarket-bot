# Polymarket Reward Farming Bot — Fixit

## Tracker of open issues, fixes in flight, and shipped fixes

---

**Document version:** 1.25
**Created:** 2026-05-18
**Last amended:** 2026-05-28 (P10 + P11 shipped — **self-learning loop now 6/6 wired (Ground Rule 3 fully met)**. FX-060 (trigger #4): decision_policy detects `total_reward_24h < $4` (80% of $5/day floor per ground_rules.md) AND NOT `global_tighten`; sets `global_reward_low=True`; allocator halves `MIN_DAILY_RATE_USD` + `MIN_EXPECTED_PER_MARKET` to widen candidate set per "expand market count, lower per-market expected-reward floor". FX-061 (trigger #6): simple_oversight passes API q_share + cumulative DB ratio per cid to `policy.record_qshare_divergence`; on `>2×` divergence (matches ground_rules.md "diverges > 2×" text verbatim), inserts row in new `q_share_recalibration_events` DB table + emits `[LEARN_DIVERGENCE]` log. Next cycle: `evaluate()` loads recent events (24h window) into `q_share_distrust_cids`; allocator applies extra `0.5×` factor to NON-API q_share for those cids (compounds with conservative_factor). 15 adversarial tests in tests/test_p10_p11_full_self_learning.py across 6 attack families (PT-A P10 detection × 3, PT-B P10 allocator widening × 2, PT-C backward compat × 1, PT-D P11 event recording × 3, PT-E P11 distrust propagation × 4, PT-F full 6/6 integration × 2). 318 tests pass cumulative across P10+P11 + P9 + P8 + P4 + P3 + P2 + P1 + all prior FX + adjacent. Zero regressions. **Gate G-B now FULLY MET (6/6 not 4/6)** — ground_rules.md "no code that runs but isn't read" violation closed. Honest rating update: 7.5/10 today (was 6/10 pre-P10/P11 per the user's audit pushback). Path to 9/10 unchanged: live operation on Helsinki (G-C verify + G-E G1 7-day clean).

Last amended pre-P10/P11: 2026-05-28 — 9/10 plan code-level phases COMPLETE — P1+P2+P3+P4+P8 shipped + P5-P7 operator runbook delivered. **Honest current rating: 8/10** (3 of 5 gates fully met, 2 require live operation). 6 commits to main this session: 5bbded1 (P1 FX-058+FX-043), 45a7fc3 (P2 FX-052+053), bc8d169 (P3 FX-046), b1d7ddd (P4 FX-059), c68186b (P5-P7 runbook), c2358df (P8 chaos engineering). 292 tests pass / 0 failures / 2 env-skips. **Gates met at code level: G-A FX-052+053 ✓, G-B 4-of-6 triggers wired ✓, G-D FX-046 formally accepted ✓, G-C code-level FX-054 closed + chaos-verified (production confirmation still pending) ⏳, G-E requires P7 7-day clean run on Helsinki ⏳.** Path to 9/10 = operator executes runbook at `docs/runbooks/9_of_10_p5_p7_operator_runbook.md`, completes P5 shadow ≥48h clean → live cutover at full wallet → P6 fill-burst verification → P7 G1 7-day continuous clean. No remaining architectural blockers.

Last amended pre-P9: 2026-05-28 — P4 of 9/10 plan shipped — **FX-059: 4 of 6 ground-rules §3 self-correction triggers now wired to behavior change.** Pre-P4 only triggers #1 (per-market ROI < threshold → cool) and #2 (single-event large loss → fast-path cool) had behavior change. Triggers #3 and #5 were observability-only (warning logs). P4 wires both: (#3) per-market fill_rate > 1/hr AND not-already-cooled → `size_reduction_cids` set → allocator halves target_shares for those cids. (#5) global total_loss > 0.5 × total_reward (or loss > 0 with no reward) → `global_tighten=True` → allocator raises MIN_DAILY_RATE_USD floor 2× AND applies 0.5× global size multiplier this cycle. Effects compose multiplicatively when both fire (0.25× sizing on high-fill-rate markets during global stress). No new DB table — both triggers recompute each cycle from raw signals so transient anomalies self-resolve at next evaluation. decision_policy.evaluate() returns richer dict; simple_oversight passes new fields to allocator.compute(). 13 adversarial tests in tests/test_p4_self_correction_triggers.py across 4 attack families (P4-A trigger #3 × 4, P4-B trigger #5 × 5, P4-C trigger composition × 1, P4-D backward compat × 1, plus 2 helper). 126 tests pass across P4 + P3 + P2 + P1 + all prior FX + adjacent; zero regressions. **Gate G-B (4+ triggers wired) now MET.** Remaining gates: G-C (FX-054 verified in production), G-E (G1 7-day clean run) — both require live operation.

Last amended pre-P4: 2026-05-28 — P3 of 9/10 plan shipped — **FX-046 formal resolution (Accepted Risk + conservative q_share margin).** Research agent confirmed all 3 candidate formulas under-predict actual payouts by 24-94×; no clean code change disambiguates the cause. Formal acceptance: FX-046 moved to §5 Won't Fix / Accepted Risk with full rationale. Code mitigation: new `RF_OVERCOMMIT_Q_SHARE_CONSERVATIVE_FACTOR=1.0` cfg knob applied to NON-API q_share estimates (cumulative + cold-start). Default no-op preserves Ground Rule 1's max-farm posture; operators concerned about over-deployment can set to 0.5 at runtime → halve non-API expected_reward → EV gate tightens. API q_share remains ground truth (no margin). 7 adversarial tests in tests/test_p3_fx046_conservative_margin.py (P3-A conservative factor × 4, P3-B default no-op × 1, plus 2 helper). 272 tests pass across P3 + P2 + P1 + all prior FX + adjacent; zero regressions. Unblocks P4 (wire 2+ self-correction triggers to behavior change).

Last amended pre-P3: 2026-05-28 — P2 of 9/10 plan shipped — **FX-052 + FX-053 OverCommitAllocator.** SimpleAllocator class retained for import-site compatibility; semantics transformed to OverCommitAllocator per Ground Rules 1+2. Dropped `MAX_DEPLOYED_MARKETS=20` (cap → soft sanity at 500), `MAX_PER_MARKET_USD=$60`, `DEPLOY_RATIO=0.95`. Per-market notional is now cost-to-score (`min_size × midpoint × 2 × (1+buffer)`). Total notional permitted to exceed wallet 3-8× per Rule 2 design point (collateral-rebalance auto-cancel mechanism handles it). New positive-EV gate filters markets where `expected_reward × q_share < expected_fill_cost × position_notional` (default 2% slippage assumption) — keeps deploys positive-EV per Rule 1+3. 5 new cfg knobs `RF_OVERCOMMIT_*` for runtime tuning. Alloc.json v1.1 → v1.2 (adds `_notional_overcommit_ratio` + `_target_market_count_band` metadata). 18 new adversarial tests in tests/test_p2_overcommit_allocator.py across 6 attack families (AO-A overcommit guarantees × 4, AO-B EV-gate × 3, AO-C pre-P2 filters still respected × 3, AO-D kill-switch edge cases × 2, AO-E telemetry × 3, AO-F adversarial × 5). 5 existing test_simple_allocator tests updated to assert new OverCommit semantics (C7 soft cap, C8 cost-to-score, C9 overcommit verified, plus 2 new C16 EV-gate + C17 metadata). 265 tests pass across P2 + P1 + all prior FX + adjacent; zero regressions. Unblocks P3 (FX-046 conservative q_share margin) — next on critical path.

Last amended pre-P2: 2026-05-28 — P1 of 9/10 plan shipped — **FX-058 farmer kill-threshold retune + FX-043 _total_capital metadata stamping.** RF_MAX_NOTIONAL_RATIO 2.0 → 5.0 cfg-driven; RF_HARD_NOTIONAL_RATIO 2.5 → 8.0 cfg-driven; new RF_RAPID_GROWTH_KILL_RATIO=5.0 over RF_RAPID_GROWTH_WINDOW_SEC=300 acceleration-based kill (catches misconfigured-allocator bursts without false-firing on healthy overcommit). FX-043: alloc.json stamps _total_capital at top-level metadata; reader resolution chain metadata → deploy row → avoid row → None — closes the silent fail-open on 0-deploy cycles. 17 new adversarial tests in tests/test_p1_farmer_retune.py (AT-A cfg-driven × 3, AT-B rapid-growth kill × 6, AT-C FX-043 fallback × 6, AT-D end-to-end round-trip × 1). 243 tests pass across P1 + FX-054 + FX-057 + FX-051 + adjacent suites; zero regressions. Unblocks P2 OverCommitAllocator (FX-052+053) — farmer-side kill thresholds now safe for 3-8× wallet notional operation.

Last amended pre-P1: 2026-05-28 — FX-045 shipped — Priority 1 q_share over-estimation closed via Approach E presence-gate. Pre-fix Priority 1 returned `min(scoring_ratio × 0.5, 0.5)` which mapped "fraction of cycles in-zone" onto "fraction of reward pool" — two unrelated quantities. For a well-positioned bot scoring 100% of the time → q_share=0.5 max, regardless of actual queue depth. Live probe 2026-05-23 measured 1500× over-estimate vs cumulative; I6 perpetually fired SEVERELY → CALIBRATED structurally unreachable → friend-rollout G3 blocked. Fix in `oversight/data_collector.py`: windowed signal demoted from MAGNITUDE estimator to PRESENCE gate. When windowed shows we're rarely scoring (< 10% in-zone over ≥3 samples), force q_share=0 to override stale cumulative. Otherwise fall through to Priority 2 (cumulative, a real measurement). Two new constants in `data_collector.py`: `RF_WINDOWED_PRESENCE_GATE=0.10`, `RF_WINDOWED_PRESENCE_MIN_SAMPLES=3`. 13 new adversarial tests in `tests/test_audit_q_share_resolution.py` (QS-A priority resolution × 8, QS-B invariants × 3, QS-C incident regression × 2 — exactly reproduces the 2026-05-23 Helsinki probe shapes for the two deployed markets). **Note:** the fix affects the OLD oversight_agent path; SimpleAllocator (current production) uses Polymarket's `/rewards/user/percentages` API directly and is unaffected. This unblocks G3 for if/when oversight_agent comes back; FX-046 (cumulative formula investigation) remains open but is a separate concern. 201 tests pass across FX-045 + FX-054 + FX-057 + FX-051 + adjacent suites; zero regressions.)
**Companion to:** `Polymarket bot architecture v5.1.md` (now v6.0 — ground rules section at top) + `ground_rules.md` (NEW — immutable contract).
**Repo HEAD as of last amend:** `e478dc8` on `main` (FX-054); new commit pending for FX-045.

---

## 0. How to use this doc

This is a **living document**. The architecture doc describes how the system *is supposed to* work; this fixit doc tracks the gaps between that and reality — and what needs to be done to close them. Every issue we observe in production, every code smell we find on audit, every architecture-vs-implementation mismatch, lands here as a numbered entry.

**For anyone reading an entry cold:** each issue is self-contained. You should be able to act on a single entry without reading the rest of this doc or the whole codebase.

**Update protocol:**

1. **New issue found?** Add an entry under §3 with the next available `FX-NNN` ID. Fill every field. Set status `Open`.
2. **Working on it?** Update status to `In Progress`, note the branch or commit prefix.
3. **Shipped a fix?** Move the entry from §3 (Open) to §4 (Fixed), record the commit SHA, link to the architecture-doc section that should be amended (if any).
4. **Decided not to fix?** Move to §5 (Won't Fix / Accepted Risk) with reasoning.
5. **New evidence on an existing issue?** Add to the "History" sub-section of that entry; never delete prior content.
6. **Touched the doc?** Update the changelog at §7 with a one-line summary.

**ID stability is sacred.** Once `FX-007` exists, it stays as `FX-007` forever, even after it's fixed and moved. Cross-references in code comments, commits, and the architecture doc point to these IDs.

### 0.1 Change-management principles (applies to every entry, every session)

Codified during the 2026-05-19 → 2026-05-21 cascade-recovery sequence. The canonical text lives in the architecture doc §12.6; this is the condensed checklist that every fix in §3/§4 should be evaluated against.

- **P1 — Verified > assumed.** Anything not directly observed in data (log line, DB row, on-chain probe, API response) gets flagged as a hypothesis. When proposing a fix, point to the data that proves the issue exists.
- **P2 — Reversibility first.** Prefer cheap-to-undo over expensive-to-undo. Examples: hot-reloadable `config_overrides.json` (cheap) vs reverting a commit (expensive). Use the cheap path first, observe, then commit code if needed.
- **P3 — Single-axis changes.** Don't ship two FX-NNN fixes in the same commit. Don't combine a code change with a config override change. Each change pairs with one observable hypothesis ("after this, X should change") so production cycles can confirm or refute.
- **P4 — Production cycles > tests.** Tests catch architectural drift; production catches input-shape drift. The 4-day FX-035 blackout was invisible to 685 fast-tier tests. Run the first 5-10 cycles with `journalctl -f` open after every release. A change is not validated by tests alone.
- **P5 — Friend rollout = ≥7 days clean on dev wallet first.** See §6 Hardening roadmap for the G1-G7 gates. No friend turns on `--mode live` until all seven are green. Each gate's verification is in the architecture doc §12.6.

**How to use these when adding a §3 entry:**
- Cite which principle surfaced the issue (often P4 — "production cycle exposed it"; or P1 — "I noticed the data didn't match the assumption").
- For the proposed fix, evaluate against P2/P3: is it reversible? Single-axis?
- For any fix that requires production verification, the History sub-section must include the post-deploy observation that confirms (or refutes) the hypothesis from P3.

---

## 1. Legend

### Severity

| Severity | Meaning | Example |
|---|---|---|
| **Critical** | Blocks production operation OR risks capital loss OR causes silent state corruption | I9 deadlock; CF collapse loop |
| **High** | Degraded operation, recovered only by hand; or correctness bug in active path | Counter / DB lies; phantom dump_states |
| **Medium** | Operational friction, edge-case bug, or fragile assumption that may bite later | Hardcoded `$1500` fallback; dead config knobs |
| **Low** | Cosmetic, documentation, code style, or improvement-grade items | Stale repo file; log message wording |

### Status

| Status | Meaning |
|---|---|
| **Open** | Identified, not yet started |
| **In Progress** | Patch underway; may be in a branch or staging |
| **Fixed** | Shipped on `main`; verified in production where applicable |
| **Won't Fix** | Decided not to address (capacity, scope, or risk reasons) |
| **Accepted Risk** | Known issue, deliberately left in place; mitigation documented |

### Category tags

- `[BUG]` — codebase produces incorrect behavior
- `[ARCH]` — architectural fragility / structural risk, not necessarily a bug today
- `[OPS]` — operational / runbook / deployment issue
- `[DOC]` — documentation mismatch with reality
- `[TEST]` — missing or insufficient test coverage
- `[PERF]` — performance / scalability concern

---

## 2. Open issues at a glance

*(2026-05-28: 9/10 plan code-level phases SHIPPED across 8 commits (5bbded1 P1 → 45a7fc3 P2 → bc8d169 P3 → b1d7ddd P4 → c68186b runbook → c2358df P8 chaos → 7f17e1b P9 integration → ac5da22 P10+P11). Self-learning loop now 6/6 wired per Ground Rule 3 (was 0/6 pre-FX-051, 2/6 post-FX-051+057, 4/6 post-FX-059, **6/6 post-FX-060+FX-061**). Honest current rating: 7.5/10. Bot still halted on Helsinki; path to 9/10 = operator runbook execution at `docs/runbooks/9_of_10_p5_p7_operator_runbook.md` (G-C live verify + G-E G1 7-day clean run). Original 2026-05-26 context: ground rules established in `ground_rules.md`; SimpleAllocator path (`0fafa1b`) deployed 2026-05-25, kill-switched 3h27m later on fill-rate spike (lost $26 trade pnl while earning $4.78 rewards). Six FX-NNN entries opened (FX-051 through FX-056) — all now closed plus 5 more shipped (FX-058, 059, 060, 061 closed; FX-046 moved to §5 Accepted Risk).

**v6.0 + 9/10 plan ship status (as of 2026-05-28):**
- **FX-054 — SHIPPED in `e478dc8`; 3-axis root-cause fix (idempotent log_fill + balance-lag tolerance in phantom check + end-of-cycle drift catch-up sweep). 14 adversarial tests in tests/test_audit_fill_detection.py. Restart blocker LOGIC closed; production trace confirmation still recommended on next operational run.**
- FX-055 — SHIPPED (`3704cd7`); reconciler re-wired into simple_oversight.run_once
- FX-056 — SHIPPED (`80bd299`); extreme-price filter live + 5 contract tests
- FX-039 — SHIPPED (`9164f1f`); fill_type threaded + latent partial-fill alert bug closed
- **FX-051 — SHIPPED (`e4f2ee3`); per-market ROI tracker + cooldown decision policy. Ground Rule 3 self-correction loop now operational.**
- **FX-057 — SHIPPED (`d2f74f7`); adversarial audit of FX-051 closed 7 found bugs (cold-start trap × 4, cooldown gaming × 3). Retunes thresholds for ground-rules-target operating regime + adds re-cool semantics + tracker math fix. 7 new audit tests; 72 total pass.**
- **FX-058 + FX-043 — SHIPPED (`5bbded1`, P1 of 9/10 plan); farmer kill-threshold retune (2.0/2.5 → 5.0/8.0 cfg-driven + rapid-growth kill at 5× over 5 min) + `_total_capital` top-level metadata stamp closes silent fail-open on 0-deploy cycles. 17 adversarial tests in tests/test_p1_farmer_retune.py.**
- **FX-052 + FX-053 — SHIPPED (`45a7fc3`, P2 of 9/10 plan); OverCommitAllocator — dropped DEPLOY_RATIO/MAX_PER_MARKET/MAX_DEPLOYED_MARKETS caps. Per-market notional = cost-to-score. Total notional 3-8× wallet by design (Rule 2). Target 50-200 markets (Rule 1). Positive-EV gate replaces the count cap. 18 adversarial tests in tests/test_p2_overcommit_allocator.py.**
- **FX-046 — ACCEPTED RISK (`bc8d169`, P3 of 9/10 plan); moved to §5. Conservative q_share margin cfg knob (RF_OVERCOMMIT_Q_SHARE_CONSERVATIVE_FACTOR=1.0 default no-op) lets operators tune at runtime. 5 mitigations enumerated.**
- **FX-059 — SHIPPED (`b1d7ddd`, P4 of 9/10 plan); wired 2 of 6 ground-rules §3 triggers to behavior change (was 2/6, now 4/6). #3 fill_rate → reduce shares; #5 global loss > rewards → tighten filters. 13 adversarial tests in tests/test_p4_self_correction_triggers.py.**
- **FX-060 + FX-061 — SHIPPED (`ac5da22`, P10+P11 of 9/10 plan); final 2 self-correction triggers wired (4/6 → 6/6). #4 global reward < target → expand filters; #6 API q_share divergence > 2× → distrust + recalibrate. New q_share_recalibration_events DB table. 15 adversarial tests in tests/test_p10_p11_full_self_learning.py.**

**Ground Rule 3's "no code that runs but isn't read" violation now fully closed (6/6 triggers wired).** Bot's restart blockers are closed at the logic + accounting layers. Path to 9/10 = operator runbook execution (`docs/runbooks/9_of_10_p5_p7_operator_runbook.md`) for live verification (G-C) + G1 7-day clean run (G-E). 318 tests pass across the 9/10 plan stack with zero regressions; 1 pre-existing failure in legacy `LearningController` (oversight_agent path, not current production path).)*

| ID | Title | Severity | Status | Tags |
|---|---|---|---|---|
| ~~**FX-051**~~ | ~~**No loss-aware feedback / per-market ROI tracking.**~~ ✅ SHIPPED in commit `e4f2ee3` (2026-05-26). New `market_roi_tracker.py` (~470 LOC) computes per-market rolling 1h/24h/7d ROI from `fills` + `unwinds` + `capital_committed_snapshots` + `/rewards/user/markets` API. New `decision_policy.py` (~280 LOC) cools markets where 24h ROI < -5% with ≥3 samples OR fill_loss ≥ $2 single-event; allocator excludes cooled cids via new `excluded_cids` parameter. Four new DB tables. 29 contract tests + 1 end-to-end integration test. See §4 detail. | Critical → CLOSED | Fixed (`e4f2ee3`) | `[ARCH]` `[BUG]` |
| ~~**FX-057**~~ | ~~**FX-051 cooldown thresholds + lifecycle gaps surfaced by adversarial audit.**~~ ✅ SHIPPED (`d2f74f7`, 2026-05-27). 7 attack scenarios written in `tests/test_audit_cooldown_logic.py` exposed: (CS-1/2/CG-2/3) slow-bleed markets under the $2 absolute fast-path AND under the 3-sample roi gate never cooled; (CS-3) capital_avg=0 produced misleading -100 ROI in [LEARN] telemetry; (CS-4) `_capital_committed_avg` undercounted when only late-window snapshots existed; (CG-1) expired cooldown + still-bad ROI reactivated for one farmer cycle before re-cooling. 5 targeted fixes: ABS_LOSS_FAST_COOLDOWN_USD 2.0→1.0, ROI_COOLDOWN_MIN_SAMPLES 3→1, `evaluate_market` re-cools on expired+still-bad, tracker bounds ROI to 0 when capital_avg<$0.10, `_capital_committed_avg` extrapolates from latest snapshot before window. All 7 audit tests pass after fix; full 72-test sweep across FX-051 + adjacent suites green; 83 more tests pass across capital/dump/DB/wallet/oversight. See §4 detail. | High → CLOSED | Fixed (`d2f74f7`) | `[ARCH]` `[BUG]` |
| ~~**FX-052**~~ | ~~**SimpleAllocator caps total notional below wallet (anti-overcommit).**~~ ✅ SHIPPED in P2 of 9/10 plan (`45a7fc3`, 2026-05-28). Bundled with FX-053 (both are the OverCommitAllocator pair — same code change). Dropped `DEPLOY_RATIO=0.95`, `MAX_PER_MARKET_USD=$60`. Per-market notional is now cost-to-score: `min_size × midpoint × 2 × (1 + RF_OVERCOMMIT_PER_MARKET_BUFFER_FRAC=0.10)` — typically $20-50. Total notional permitted to exceed wallet 3-8× per Ground Rule 2 design point. 18 adversarial tests in tests/test_p2_overcommit_allocator.py (AO-A overcommit guarantees × 4). | Critical → CLOSED | Fixed (`45a7fc3`) | `[ARCH]` |
| ~~**FX-053**~~ | ~~**Hard-coded market count cap leaves 5000+ reward markets on the table.**~~ ✅ SHIPPED in P2 of 9/10 plan (`45a7fc3`, 2026-05-28). `MAX_DEPLOYED_MARKETS` 20 → soft sanity cap at 500 (cfg-driven via `RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS`). New positive-EV gate replaces the count cap: deploy on EVERY market where `expected_reward × q_share > expected_fill_cost × position_notional`. Default 2% slippage assumption (`RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC=0.02`). Steady-state target band 50-200 markets per Ground Rule 1. Tests AO-A1 (50 markets), AO-A2 (200 markets), AO-A3 (700 → 500 soft cap), AO-A4 (notional > wallet 3-8×). | Critical → CLOSED | Fixed (`45a7fc3`) | `[ARCH]` |
| ~~**FX-054**~~ | ~~**Fill detection misses fills in high-frequency regime.**~~ ✅ SHIPPED (pending commit, 2026-05-28). Three-axis defensive fix tackling all 4 fixit hypotheses + 2 newly-identified root causes (A: log_fill silently swallowed DB exceptions at debug level; B: phantom check zeroed legitimate fills during Polygon CTF balance-confirmation lag). **F1 idempotency:** new `order_id` + `fill_event_id` columns + partial unique index `idx_fills_event_id (fill_event_id != '')`; `log_fill` returns bool; defensive None→'' coercion (closes silent NOT NULL violation surface); `handle_fill` emits truthful `[FILL_WRITE]` log step ∈ {attempting, succeeded, duplicate, FAILED}. **F2 balance-lag tolerance:** `_check_buy_phantom_fill` fail-OPENs when on-chain delta is 0 within `FILL_BALANCE_LAG_TOLERANCE_SEC=60` of `slot.placed_at` (CTF transfer typically confirms within seconds; pre-fix the check fired immediately and zeroed legit fills). Beyond the window, FX-037 phantom defence resumes. **F3 drift catch-up:** new `_reconcile_balance_drift` runs at end of `detect_fills` for `(cid, side) ∈ cids_processed - primary_handled` (i.e., orders that disappeared this cycle but the primary path didn't record a fill — phantom_zeroed + UNKNOWN-no-surplus branches). Idempotent via 5-min-bucketed event_id. 14 new adversarial tests in `tests/test_audit_fill_detection.py` (FD-A: idempotency × 5, FD-B: lag tolerance × 4, FD-C: drift catch-up × 5, FD-D: stacked-failure invariants). Audit caught a real bug in F3 (slot.order_id=None being passed into NOT NULL column → INSERT OR IGNORE silent drop) which was closed by the defensive coercion. 227 tests pass across FX-054 + FX-057 + FX-051 + adjacent suites; zero regressions. **Production trace confirmation still recommended** on next operational run — `[FILL_DETECT_TRACE]` + `[FILL_WRITE]` log lines from `bd5a54e` are still there, plus new `[RECONCILE_DRIFT]` warnings. See §4 detail. | Critical → CLOSED | Fixed (`e478dc8`) | `[BUG]` `[ARCH]` |
| ~~**FX-055**~~ | ~~**FX-049 wallet reconciliation regression in simple_oversight.py.**~~ ✅ SHIPPED in commit `3704cd7` (2026-05-26). `reconcile_wallet_invariant` re-wired into `simple_oversight.run_once()` after wallet probe + history reads. Fail-open exception handler preserves cycle resilience. See §4 detail. | High → CLOSED | Fixed (`3704cd7`) | `[BUG]` `[SAFETY]` |
| ~~**FX-056**~~ | ~~**Extreme-price markets (midpoint < $0.10 or > $0.90) produce wide effective spreads on dump.**~~ ✅ SHIPPED in commit `80bd299` (2026-05-26). SimpleAllocator's eligible filter now rejects markets where `midpoint_guess` is outside `[0.10, 0.90]`. `fetch_reward_markets` extracts midpoint hints from `tokens[0].price`; markets without a price hint default to 0.5 and pass through fail-open. 5 new contract tests. See §4 detail. | Medium → CLOSED | Fixed (`80bd299`) | `[ARCH]` |
| ~~**FX-050**~~ | ~~**Polymarket taker fee not captured in unwind pnl.**~~ ✅ SHIPPED in commit `06d8406` (v5.1.22, 2026-05-24). Config knob `RF_POLYMARKET_TAKER_FEE = 0.009`; `dump_manager.py:89` applies multiplier. See §4 detail. | High → CLOSED | Fixed (`06d8406`) | `[BUG]` `[SAFETY]` |
| ~~**FX-049**~~ | ~~**Wallet-invariant reconciliation (defense-in-depth backstop).**~~ ✅ SHIPPED in commit `06d8406` (v5.1.22, 2026-05-24, bundled with FX-050). New `wallet_reconcile_history` table + `oversight/wallet_reconciliation.py` module + integration in `oversight_agent.run_once()`. `\|divergence\| > $0.50` → `[CRITICAL] WALLET_DESYNC`. See §4 detail. | High → CLOSED | Fixed (`06d8406`) | `[ARCH]` `[SAFETY]` |
| ~~**FX-045**~~ | ~~**Priority 1 q_share returns upper-bound heuristic, not measurement.**~~ ✅ SHIPPED (`85a23ce`, 2026-05-28). Approach E (presence-gate): windowed signal demoted from MAGNITUDE estimator to PRESENCE gate. Pre-fix `min(scoring_ratio × 0.5, 0.5)` conflated "fraction of cycles in-zone" with "fraction of reward pool" → 1500× over-estimate for well-positioned bots. Post-fix: when windowed shows < 10% in-zone over ≥3 samples → q_share=0 (override stale cumulative). Otherwise falls through to Priority 2 (cumulative, a real measurement). Two new constants in `data_collector.py`: `RF_WINDOWED_PRESENCE_GATE=0.10`, `RF_WINDOWED_PRESENCE_MIN_SAMPLES=3`. New `[Q-share]` log telemetry includes `presence_gated` count. 13 new adversarial tests in `tests/test_audit_q_share_resolution.py` (QS-A × 8, QS-B × 3, QS-C × 2 — incident regression exactly reproduces the 2026-05-23 probe shapes for both deployed markets). **Affects OLD oversight_agent path only**; SimpleAllocator (current production) uses Polymarket API directly and is unaffected. Unblocks G3 for if/when oversight_agent comes back. FX-046 cumulative-formula investigation remains open as a separate concern (would change Priority 2's accuracy, orthogonal to this fix). See §4 detail. | High → CLOSED | Fixed (`85a23ce`) | `[BUG]` `[ARCH]` |
| ~~**FX-046**~~ | ~~**Q-score formula candidate vs actual payouts diverges 24-94×.**~~ 📋 ACCEPTED RISK in P3 of 9/10 plan (pending commit, 2026-05-28). Investigation by research agent showed all 3 candidate formulas (squared / linear / size-share) under-predict actual payouts by 24-94×. No clean code fix disambiguates the cause (formula error vs market_q over-counting vs snapshot staleness). Resolution: (1) formally accepted in §5; (2) new `RF_OVERCOMMIT_Q_SHARE_CONSERVATIVE_FACTOR=1.0` cfg knob lets operators bias NON-API q_share estimates down at runtime if production shows over-deployment; (3) FX-051 per-market cooldowns catch any deploys that turn into real losers within 24h regardless of formula accuracy. **Moved to §5 Won't Fix / Accepted Risk.** | Medium → Accepted Risk | §5 | `[ARCH]` `[INVESTIGATION]` |
| **FX-047** | **I6 `est_actual_ratio` thresholds (5×/15×/50×) calibrated for matched-magnitude est_d/act_d.** With current $40 est_d vs $1-5 act_d, ratio is structurally 8-30× regardless of cadence or formula choice. CALIBRATED requires ratio < 5× — unreachable under any healthy operation at our wallet scale. If FX-045 + FX-046 land but residual mismatch persists, recalibrate thresholds against measured production distribution (e.g., P95 → CALIBRATED, P99 → SEVERELY). Skip if upstream fixes naturally land ratio in <5×. | Medium (contingent on FX-045/046 outcome) | Open | `[ARCH]` |
| ~~**FX-037**~~ | ~~**BUY-side phantom-fill defense.**~~ ✅ SHIPPED in commits `0ec898a` + `a858bb9` (v5.1.21, 2026-05-23). New helper `OrderLifecycle._check_buy_phantom_fill` mirrors DumpManager's SELL-side defense. Fail-OPEN on API error. 14 new tests. See §4 detail. | High → CLOSED | Fixed (`0ec898a`, `a858bb9`) | `[BUG]` `[SAFETY]` |
| **FX-042** | **`orders_cancelled` table never written by production codepath.** `OrderLifecycle.cancel_order` calls the V2 SDK + logs to file but never writes `db.log_order_cancelled`. Legacy `order_manager.py` is the only writer, and that path is inactive in production. Currently invisible (fill model dormant) but `calibration/features.py:117` reads this table to label fill-model training data → activates the moment the calibrator becomes ready (~50 fills, weeks away on $200 wallet, sooner on bigger). Friend-rollout multiplier — every friend's bot trains its own fill model from this corrupted label set. | Medium (latent calibrator bug + diagnostics gap) | Open | `[BUG]` `[ARCH]` |
| ~~**FX-043**~~ | ~~**`_total_capital` stamp missing → guardrails fail-open during 0-deploy alloc moments.**~~ ✅ SHIPPED in P1 of 9/10 plan (`5bbded1`, 2026-05-28). `simple_allocator.write_allocation_json` now stamps `_total_capital` at top-level metadata (in addition to per-row). `reward_farmer._guardrail_total_capital_from_alloc` resolution chain: metadata → deploy row → avoid row → None (with [GUARDRAIL_WARNING]). Net: any cycle whose allocator successfully ran (even with 0 deploys) carries a usable capital signal for all wallet-fraction guardrails. Only a genuinely missing/corrupted alloc file falls open now. 6 new tests in tests/test_p1_farmer_retune.py (AT-C1–C6) + 1 end-to-end (AT-D1). | Medium → CLOSED | Fixed (`5bbded1`) | `[BUG]` `[SAFETY]` |
| ~~**FX-058**~~ | ~~**Farmer kill thresholds anti-design for overcommit (Rule 2 violation).**~~ ✅ SHIPPED in P1 of 9/10 plan (`5bbded1`, 2026-05-28). The hardcoded `MAX_NOTIONAL_RATIO=2.0` + `HARD_NOTIONAL_RATIO=2.5` capped below the Ground Rule 2 design point (3-8× wallet notional), which would have force-triggered the kill switch the moment FX-052/053 OverCommitAllocator deployed at design. Three changes: (1) promoted MAX/HARD to cfg-driven `RF_MAX_NOTIONAL_RATIO=5.0` / `RF_HARD_NOTIONAL_RATIO=8.0` (top of overcommit design band). (2) Added NEW acceleration-based `_guardrail_rapid_notional_growth` — kill if `notional_ratio max/min over 5 min > 5×` — catches anomalous bursts (misconfigured allocator deploying 10× normal) without false-firing on healthy overcommit. (3) Fail-open semantics: missing notional_ratio leaves deque unchanged (DB hiccup can't reset burst-detection window NOR trigger false kill). 10 new tests in tests/test_p1_farmer_retune.py (AT-A cfg-driven × 3, AT-B rapid-growth × 6, AT-A3 disable-via-zero × 1). | High (Ground Rule 2 violation) → CLOSED | Fixed (`5bbded1`) | `[ARCH]` `[SAFETY]` |
| ~~**FX-059**~~ | ~~**4 of 6 ground-rules §3 self-correction triggers wired (was 2/6).**~~ ✅ SHIPPED in P4 of 9/10 plan (`b1d7ddd`, 2026-05-28). Pre-P4 triggers #3 (per-market fill_rate) and #5 (global loss > rewards) were observability-only — direct violation of "no code that runs but isn't read". P4 wires both: (#3) fill_rate > 1/hr AND not-cooled → `size_reduction_cids` set → allocator halves target_shares. (#5) total_loss > 0.5×total_reward → `global_tighten=True` → allocator doubles MIN_DAILY_RATE_USD + halves global sizing. No new DB table — recomputed each cycle. 13 adversarial tests in `tests/test_p4_self_correction_triggers.py`. | High (Ground Rule 3 partial violation) → CLOSED | Fixed (`b1d7ddd`) | `[ARCH]` |
| ~~**FX-060**~~ | ~~**Trigger #4 (global reward < target → expand) not wired to behavior.**~~ ✅ SHIPPED in P10 of 9/10 plan (`ac5da22`, 2026-05-28). `decision_policy.evaluate()` detects `total_reward_24h < RF_GLOBAL_REWARD_TARGET_24H_USD` (default $4 = 80% of $5/day floor) AND NOT `global_tighten`. Sets `global_reward_low=True`. Allocator halves MIN_DAILY_RATE_USD + MIN_EXPECTED_PER_MARKET → widens candidate set per ground_rules.md "expand market count, lower per-market expected-reward floor". Mutually exclusive with global_tighten (loss recovery wins). Tested in `tests/test_p10_p11_full_self_learning.py::TestPT_A`. | High (Ground Rule 3 trigger missing) → CLOSED | Fixed (`ac5da22`) | `[ARCH]` |
| ~~**FX-061**~~ | ~~**Trigger #6 (API q_share divergence → recalibrate) not wired to behavior.**~~ ✅ SHIPPED in P11 of 9/10 plan (`ac5da22`, 2026-05-28). New DB table `q_share_recalibration_events`. `simple_oversight` passes API + cumulative q_share per held cid to `policy.record_qshare_divergence`; on `>2×` ratio (matches ground_rules.md "diverges > 2×" text verbatim), inserts event row + emits `[LEARN_DIVERGENCE]` log. Next cycle: `_detect_qshare_divergence` loads events within 24h window into `q_share_distrust_cids` set. Allocator applies extra `0.5×` factor to NON-API q_share for those cids — minimal-behavior interpretation of "recalibrate scoring" (no destructive cumulative reset). 7 tests in `tests/test_p10_p11_full_self_learning.py::TestPT_D + TestPT_E`. | High (Ground Rule 3 trigger missing) → CLOSED | Fixed (`ac5da22`) | `[ARCH]` |
| ~~**FX-044**~~ | **~~I6 morning-SEVERELY spike at UTC day boundary.~~** **INVESTIGATED 2026-05-23 — diagnosis was wrong.** Code already uses rolling 24h cutoff (`time.time() - 24*3600` at `data_collector.py:544`), NOT UTC-day bucketing. The morning ratio jump is a real phenomenon but caused by (a) Polymarket pays REWARDs in single daily batches at ~00:20 UTC, (b) threshold-gated at $1 minimum, (c) q_share over-estimation upstream (see FX-045). FX-044 as written is a no-op. **Superseded by FX-045 / FX-046 / FX-047.** Moved to §4 with full investigation evidence. | n/a | **Closed (Investigated, not a code bug)** | `[INVESTIGATED]` |
| **FX-038** | **Reconciliation extends to fills/unwinds tables.** `_reconcile_positions` updates `positions` but not `fills`/`unwinds`. Inflated fills rows from FX-037-class incidents bias I7 hourly_loss forever (until 24h window). Fix: when reconciliation detects tracked > actual, insert a corrective unwind row. | Medium | Open | `[BUG]` `[SAFETY]` |
| ~~**FX-039**~~ | ~~**`handle_fill` hardcodes `fill_type='FULL'`** in DB write at `order_lifecycle.py:269`, regardless of whether actual match was partial.~~ ✅ SHIPPED in commit `9164f1f` (2026-05-26). `fill_type` is now a `handle_fill` parameter threaded through from the three call sites (`detect_fills`, `_check_stale_order`, `_reconcile_after_unknown`). The fix surfaced and closed a latent crash in `alerts.py:322` where the PARTIAL alert branch formatted `remaining_shares` unconditionally — pre-FX-039 the hardcode masked this dead code path. See §4 detail. | Low → CLOSED | Fixed (`9164f1f`) | `[BUG]` |
| FX-033 | Oversight allocator doesn't consult `unliquidatable_markets` table — proposes deploys the farmer silently skips | Low | Open | `[ARCH]` `[TEST]` |
| FX-034 | `_reprobe_unliquidatable` never un-marks cids even when subsequent book fetches return HTTP 200 | Low | Open | `[BUG]` |

(FX-001 through FX-036, plus FX-040 and FX-041, have been shipped — see §4. FX-027 was accepted as designed architectural risk — see §5. FX-044 was investigated 2026-05-23 and found to be misdiagnosed in the original entry; superseded by the new FX-045 / FX-046 / FX-047 chain that addresses the actual root cause.)

---

## 3. Open issues — detail

### FX-051 — No loss-aware feedback / per-market ROI tracking

- **Severity:** Critical (Ground Rule 3 violation)
- **Status:** Open
- **Tags:** `[ARCH]` `[BUG]`
- **Opened:** 2026-05-26 (surfaced by 2026-05-25 12:05 UTC kill-switch event under SimpleAllocator)
- **Principle that surfaced it:** Ground Rule 3 (self-learning loop with mandatory auto-correction).
- **Symptom:** SimpleAllocator's allocation logic (`simple_allocator.py:compute`) ranks markets by `daily_rate × q_share` only. There is no input from `unwinds.pnl` per market. Bot will re-deploy on a market that just produced losses. Verified: market `0x46c09232d356fdbe` produced a $2.13 loss at 08:46 UTC on 2026-05-25, then was STILL listed in the alloc file at 09:39 UTC and remains in current alloc as of writing.
- **Root cause:** I designed SimpleAllocator as a flat snapshot allocator with no historical-performance input. The original `LearningController` + `LossModel` + `Bandit` stack (deleted as "dormant" in the v5.2 swap) was the right SHAPE — track per-market performance, adjust allocation — but was dormant at $226 wallet due to insufficient fill data. At $1.2k wallet with ~10 fills/day, that data now exists, so the right move is to RESURRECT (not just rebuild) the per-market tracking.
- **Why it matters (Ground Rule 3):** Without ROI feedback, the bot cannot self-correct. Every architectural decision under ground_rules.md depends on this loop existing.
- **Proposed fix:** New module `market_roi_tracker.py`:
  1. Per-market rolling 1h/24h/7d windows: `reward_earned` (from `/rewards/user/markets?date=...` API), `fill_loss` (from `unwinds.pnl`), `capital_committed_time_weighted` (from `active_orders` snapshots), `roi = (reward_earned - fill_loss) / capital_committed`
  2. Persist to a new table `market_roi` keyed by `(condition_id, window)`
  3. Updated each oversight cycle (~30 min)
  4. Consumed by OverCommitAllocator (FX-052/FX-053 family): penalize/exclude markets with negative `roi_24h` AND `samples ≥ N`; reactivate after a cooldown period
- **Acceptance criterion:**
  - New table populated within 1 oversight cycle of deploy
  - Per-market ROI computable via SQL query at any time
  - Allocator filters/penalizes markets based on tracker output (verified by test: market with hardcoded negative ROI is excluded)
  - End-to-end test: simulate 3 dump events on market X; verify market X drops out of alloc on next cycle
- **Risk profile:** New tracking layer + decision policy. Reversible via feature flag `RF_USE_ROI_FEEDBACK`. Single-axis from the allocator's perspective.
- **Dependencies:** Needs FX-054 (fill detection) to actually capture the loss data. Without that, ROI tracker has no input.
- **Related:** FX-052 (overcommit allocator), FX-053 (market count cap), FX-054 (fill detection), Ground Rule 3.
- **History:**
  - 2026-05-26 — Opened.

---

### FX-052 — SimpleAllocator caps total notional below wallet (anti-overcommit)

- **Severity:** Critical (Ground Rule 2 violation)
- **Status:** Open
- **Tags:** `[ARCH]`
- **Opened:** 2026-05-26 (surfaced when ground_rules.md formalized the overcommit requirement)
- **Symptom:** Current `simple_allocator.py` has:
  - `DEPLOY_RATIO = 0.95` (deploy ≤95% of wallet)
  - `MAX_PER_MARKET_USD = 60`
  - `MAX_DEPLOYED_MARKETS = 20`
  
  Result: total live notional capped at `min(20 × 60, wallet × 0.95) = $1140` on a $1200 wallet. Polymarket's overcommit allowance permits 3-8× wallet in notional (memory file `project_capital_overcommit.md`). We are operating 3-8× *below* design point.
- **Why it matters:** Each market needs `min_size × midpoint × 2` ≈ $20-100 in notional to score for rewards. Spreading $1200 across 100 markets at min_size each = ~$50/market = $5000 notional = 4× wallet, which IS the Ground Rule 2 design point. The capped allocator misses ~80% of reward markets it could be on.
- **Root cause:** I built SimpleAllocator assuming conservative-allocation defaults made sense. Under Ground Rule 2, they are anti-pattern. The kill switch's `MAX_NOTIONAL_RATIO = 2.0` also needs to be raised — currently it's set at the design point, not above it.
- **Proposed fix:** Replace SimpleAllocator with **OverCommitAllocator**:
  - Remove `DEPLOY_RATIO`, `MAX_PER_MARKET_USD`, `MAX_DEPLOYED_MARKETS` caps
  - Per-market notional = `min_size × midpoint × 2 + small_buffer` (cost-to-score, not a fixed dollar amount)
  - Target market count derived dynamically: deploy on every eligible market until per-market expected reward drops below per-fill expected cost
  - Total notional NOT explicitly bounded by allocator — bounded by Polymarket's auto-cancel mechanism (collateral rebalance on fill)
  - Re-tune `MAX_NOTIONAL_RATIO` to permit 5-10× wallet, kill only on RAPID GROWTH (e.g., 10× in 5 min)
- **Acceptance criterion:**
  - Total live notional in production routinely >2× wallet
  - Re-place loop fires within ≤2 farmer cycles after a fill (so collateral free's up quickly)
  - No kill-switch false-positives on overcommit-by-design notional
  - Per-market expected reward × `target_market_count` > sum of expected fill losses
- **Risk profile:** High blast radius — touches allocator, kill switch thresholds, re-place logic. Need careful staged rollout: shadow mode first, then increase notional ceiling in steps (2× → 4× → 8×).
- **Dependencies:** FX-054 (need accurate fill detection — without it overcommit causes cascading silent fills). FX-051 (loss feedback to prevent runaway on bad markets).
- **Related:** Ground Rule 2, memory file `project_capital_overcommit.md`, FX-053 (market count), kill-switch thresholds in `reward_farmer.py`.
- **History:**
  - 2026-05-26 — Opened.

---

### FX-053 — Hard-coded market count cap leaves ~5000 reward markets on the table

- **Severity:** Critical (Ground Rule 1 violation)
- **Status:** Open
- **Tags:** `[ARCH]`
- **Opened:** 2026-05-26
- **Symptom:** `simple_allocator.py:MAX_DEPLOYED_MARKETS = 20` + `MIN_EXPECTED_PER_MARKET = $0.01` + ranking by `daily_rate × q_share` collectively trim Polymarket's 5,235-market reward pool down to 20 deploys. Under Ground Rule 1 (max reward farming) the bot should aggregate sub-threshold accruals across many markets — Polymarket's $1/day threshold is per-USER, not per-market. 100 markets earning $0.02/day each = $2/day total, well above threshold.
- **Root cause:** Same as FX-052 — I assumed conservative defaults. Under Ground Rule 1, the right approach is the OPPOSITE: deploy on everything that earns *anything*.
- **Proposed fix:** In OverCommitAllocator (FX-052):
  - Drop `MAX_DEPLOYED_MARKETS` entirely
  - `MIN_EXPECTED_PER_MARKET` becomes a function of estimated per-fill cost, not a constant
  - Decision rule: market m is in the deploy set iff `expected_reward(m) > expected_fill_cost(m)` where both are per-cycle quantities derived from FX-051's ROI tracker
  - Filter out extreme-price markets (FX-056) and persistent losers (FX-051) — but otherwise include everything
- **Acceptance criterion:** In steady state, bot deploys on 50-200 markets simultaneously (target band per ground_rules.md).
- **Risk profile:** High — bot will be on many more markets, with potentially much higher fill rates. Kill switch thresholds must be re-tuned alongside.
- **Dependencies:** FX-051 (loss filter), FX-052 (overcommit), FX-054 (fill detection), FX-056 (extreme-price filter).
- **Related:** Ground Rule 1, FX-052.
- **History:**
  - 2026-05-26 — Opened.

---

### FX-054 — Fill detection misses fills in high-frequency regime

- **Severity:** Critical (blocks FX-051 ROI tracker; primary accounting integrity)
- **Status:** Open
- **Tags:** `[BUG]` `[ARCH]`
- **Opened:** 2026-05-26 (surfaced by 2026-05-25 12:05 UTC kill-switch event)
- **Symptom:** On 2026-05-25, data-api/activity TRADE events for our funder over a 3.5h window show 9 BUYs totaling $844 + 8 SELLs totaling $818. Bot's `fills` DB table has only **1** row from that window (the 08:46 UTC fill); `unwinds` has only **1** row. Wallet trajectory ($1222.93 → $1196.98 = −$25.95) matches data-api truth, not bot DB. The kill switch's `fill_rate_ratio=4.80 > 3.0×` used the in-memory `ms.fill_times` (4 fills detected in-memory) — but the DB writes lagged.
- **Root cause (hypotheses — needs investigation):**
  1. `detect_fills` cycle interval (30 s) too slow vs fill arrival rate (~3/min during peak burst). Multiple fills on the same order between cycles may be detected as one.
  2. Network timeouts at 09:05-09:07 UTC (4 errors observed) may have caused `detect_fills` to skip cycles.
  3. Race between `handle_fill` DB write and next cycle's `get_open_orders` query. If `detect_fills` returns BEFORE `handle_fill` commits, the next cycle re-detects the same order and may skip the DB write.
  4. Fills happening DURING dump (the dump SELL fills count too, but routes through DumpManager which writes to `unwinds` not `fills` — but the BUY that prompted the dump should be in `fills`).
- **Why it matters:**
  - **FX-051 ROI tracker has no input without this fix.** All learning depends on knowing which markets generated losses.
  - **FX-049 wallet reconciliation can't compute drift** — `expected = baseline + Σ(unwinds - fills + rewards)` is garbage if 8 of 9 fills are missing from the table.
  - **Kill-switch fires correctly on in-memory state, but the LEARNING from that event is lost** — operator restarting the bot after kill switch loses the in-memory `ms.fill_times` and has no DB record to learn from.
- **Proposed investigation (no code yet):**
  1. Trace the `detect_fills` → `handle_fill` → DB write chain end-to-end. Identify exact failure mode.
  2. Add structured `[FILL_DETECT]` and `[FILL_WRITE]` log lines so every step is observable.
  3. Reproduce: place 5 orders in a script, fill all 5 within 30s via taker orders, verify DB has 5 rows.
- **Proposed fix (after investigation):** Likely combination of:
  - Lower `detect_fills` cycle interval to 5-10 s (currently piggybacks on 30 s cycle)
  - Make `handle_fill` write idempotent by `(order_id, ts)` so duplicate detection is safe
  - Add retry logic around the DB write with structured logging
  - If network-timeout is the cause: add a "fills backlog" queue that catches up after recovery
- **Acceptance criterion:**
  - End-to-end test: script that places 5 paired orders and triggers fills via market taker; verifies bot's `fills` table has 10 rows (5 YES + 5 NO BUY fills) within 30 s of the trades.
  - Production: `fills` count over any 24h window matches `data-api/activity` BUY TRADE count within ±1.
- **Risk profile:** High — touches the central trading loop. Test coverage critical. Reversible via feature flag.
- **Dependencies:** None. This unblocks everything else.
- **Related:** FX-049 (depends on this for accurate input), FX-051 (depends on this for ROI), FX-037 (the phantom-fill defense — note: phantom is OVER-counting, this is UNDER-counting; both are accounting bugs).
- **History:**
  - 2026-05-26 — Opened. Root cause needs investigation before code change.

---

### FX-055 — FX-049 wallet reconciliation regression in simple_oversight.py

- **Severity:** High (regression I introduced)
- **Status:** Open
- **Tags:** `[BUG]` `[SAFETY]`
- **Opened:** 2026-05-26 (regression introduced 2026-05-25 in commit `0fafa1b` swap)
- **Symptom:** `wallet_reconcile_history` table has not received a row since 2026-05-25 08:20 UTC. The last row was written by `oversight_agent.run_once()` just before the systemd swap to `simple_oversight.py` at 08:39 UTC. My new entry point doesn't call `reconcile_wallet_invariant`. All wallet drift over the ~7 hours of SimpleAllocator operation is unobserved by FX-049.
- **Root cause:** When I wrote `simple_oversight.run_once` to replace `oversight_agent.run_once`, I focused on the allocator integration and dropped the wallet reconciliation call. Pure oversight on my part.
- **Proposed fix:** Add to `simple_oversight.run_once` after wallet probe, before allocator compute:
  ```python
  from oversight.wallet_reconciliation import reconcile_wallet_invariant
  try:
      reconcile_wallet_invariant(
          db=allocator.db_path, actual_wallet_now=wallet, funder=allocator.funder,
          threshold_usd=cfg("RF_WALLET_DESYNC_THRESHOLD_USD"),
      )
  except Exception as e:
      log.warning(f"[WALLET_RECONCILE] reconciler error: {e}")
  ```
- **Acceptance criterion:** Next `simple_oversight` cycle writes a row to `wallet_reconcile_history`. Test: mock the function and verify it's called once per `run_once`.
- **Risk profile:** Trivial. ~10 LOC. Reversible by removing the call.
- **Dependencies:** None.
- **Related:** FX-049 (the original reconciler), FX-054 (the broken fill detection makes the reconciler's input garbage anyway — but the framework should be back in place).
- **History:**
  - 2026-05-25 — Regression introduced unwittingly in commit `0fafa1b`.
  - 2026-05-26 — Identified during status deep-dive.

---

### FX-056 — Extreme-price markets cause 13% slippage on dump

- **Severity:** Medium
- **Status:** Open
- **Tags:** `[ARCH]`
- **Opened:** 2026-05-26
- **Symptom:** 2026-05-25 08:46 UTC fill on `0x46c09232d356fdbe` (NO at $0.08, midpoint ~$0.08): BUY 200 sh @ $0.08 = $16, dumped at $0.07 = $13.87 → **13.3% slippage**. Mid-priced markets (midpoint $0.30-$0.70) typically have 1-2% slippage on the same flow.
- **Root cause:** Extreme-price markets have wide effective spreads (book at $0.07/$0.10 is a 30% relative spread). Any forced dump at midpoint moves the price unfavorably. Polymarket's reward formula doesn't discount these markets — they often have HIGH `rate_per_day` (the `$5000/day` markets in the alloc were Iran-related and at midpoint ~$0.10 or ~$0.90).
- **Why it matters:** SimpleAllocator's ranking by `daily_rate × q_share` heavily favors these markets (high `daily_rate`), but per-fill cost negates per-cycle reward. Net ROI on these markets is often negative.
- **Proposed fix:** In OverCommitAllocator (FX-052):
  - Filter `midpoint < 0.10 OR midpoint > 0.90` markets entirely, OR
  - Apply a `slippage_penalty` to expected reward: `effective_expected = daily_rate × q_share × (1 - estimated_slippage)`. `estimated_slippage` = `1 - 2 × min(midpoint, 1 - midpoint)` (rough; refine with real data once FX-054 ships).
  - Independent of FX-051's ROI tracker (this is a structural filter, not a learned penalty).
- **Acceptance criterion:** Markets with midpoint < $0.10 or > $0.90 are absent from deploys. Test: alloc input includes a midpoint=$0.05 market; verify it's in avoids.
- **Risk profile:** Low. Filter, reversible.
- **Dependencies:** None.
- **Related:** FX-051 (the loss feedback would penalize these eventually, but the structural filter is faster).
- **History:**
  - 2026-05-26 — Opened.

---

### FX-037 — BUY-side fill detection lacks phantom-fill defense

- **Severity:** High (silent state corruption when SDK over-reports size_matched)
- **Status:** Open
- **Tags:** `[BUG]` `[SAFETY]`
- **Opened:** 2026-05-20 (surfaced by Iran NO 158→38 incident on 2026-05-19)
- **Symptom:** `client.get_order(BUY_oid)` for a disappeared order returned `size_matched=158` for an order that only delivered 38 NO shares on-chain (verified via on-chain CTF balance probe). `order_lifecycle.detect_fills:184-209` trusted the SDK value blindly, wrote `shares=158` to `fills` DB row, posted a 158-share dump (only 38 matched), triggered LOST POSITION reconciliation. The inflated fills row biased I7 hourly_loss to phantom $60.72 damage → SafetyController demoted to DEGRADED → cascade into OpenAI thin markets → kill switch.
- **Root cause:** Asymmetric defense between BUY and SELL fill detection paths:
  - **SELL-side (DumpManager.check_dump_fills at dump_manager.py:60-87):** has a PHANTOM FILL check. After SDK reports match, queries `get_balance_allowance(CONDITIONAL, token_id)` and refuses to record the unwind if on-chain balance still shows tracked shares.
  - **BUY-side (OrderLifecycle.detect_fills at order_lifecycle.py:182-209):** trusts SDK's `size_matched` directly, calls `handle_fill(actual_shares=matched)` without on-chain verification.
- **Why it matters:** The BUY side is where the cash outflows happen. A phantom BUY fill of 158 sh × $0.49 = $77.42 in the fills table triggers I7 → state demotion → forced re-allocation to risky markets. Single incident on Iran NO so far, but the defense is essentially free to add and the asymmetry is surprising.
- **Proposed fix:** Mirror the SELL-side defense in `detect_fills`. After computing `matched` from SDK:
  ```python
  # Probe on-chain CTF balance for the token (post-fill)
  tid = ms.yes_tid if side == "yes" else ms.no_tid
  bal = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid))
  on_chain = float(bal.get("balance", 0)) / 1e6
  pre_fill_tracked = self.positions.get_shares(ms.cid, side)
  actual_delta = max(0, on_chain - pre_fill_tracked)
  if actual_delta < matched - 0.5:
      log.critical(f"PHANTOM FILL: SDK size_matched={matched:.0f} but on-chain delta only {actual_delta:.0f} | {ms.question[:30]}")
      matched = actual_delta  # prefer on-chain truth
  ```
- **Acceptance criterion:** Regression test that simulates SDK returning size_matched=158 while on-chain delta is 38 — fill is recorded as 38, not 158. Plus log line `PHANTOM FILL: ...` for operator visibility.
- **Risk:** Low. One extra API call per fill detection (matches DumpManager's existing call pattern). Adds <100ms latency.
- **Related:** FX-038 (compensates for already-recorded phantom rows), `dump_manager.py:60-87` (the symmetric defense to copy).
- **Hardening Phase:** Phase 1 (post-FX-036 cascade).
- **History:**
  - 2026-05-20 — Opened after Iran NO incident analysis.

---

### FX-038 — `_reconcile_positions` should compensate fills/unwinds tables

- **Severity:** Medium (silent state corruption persistence)
- **Status:** Open
- **Tags:** `[BUG]` `[SAFETY]`
- **Opened:** 2026-05-20 (necessary follow-up to FX-037)
- **Symptom:** When `_reconcile_positions` (reward_farmer.py:395-423) detects `tracked_shares != on_chain_balance` and corrects the `positions` table downward, the `fills` and `unwinds` tables are NOT touched. Any inflated fills row from an FX-037-class incident persists forever — biasing I7 hourly_loss until the 24h aging window clears.
- **Root cause:** `_reconcile_positions` was designed to fix the position TRACKING (which is what allocator + sizing logic reads), but I7's `_query_fill_damage` reads the `fills` and `unwinds` tables directly to compute hourly damage. Reconciliation doesn't propagate to those tables.
- **Why it matters:** On 2026-05-20 we needed a manual SQL UPDATE on the fills table to clear the Iran phantom row (see `~/.claude/projects/.../memory/phantom_fill_recovery.md`). Automating this via FX-038 closes the loop so future phantoms self-heal.
- **Proposed fix:** When `_reconcile_positions` corrects a position downward (`actual < tracked`), insert a synthetic compensating row in `unwinds`:
  ```python
  delta_to_compensate = tracked - actual  # the phantom shares
  avg_cost = self.positions.get_avg_price(cid, side)
  # Synthetic unwind: shares=delta, sell_price=0, usd_value=0, pnl=-delta*avg_cost
  # This zeros the phantom contribution to fill_damage = SUM(fills.shares*clob_cost) - SUM(unwinds.usd_value)
  self.db.log_unwind(
      condition_id=cid, side=side, shares=delta_to_compensate,
      sell_price=0, usd_value=0,
      vwap_cost=delta_to_compensate * to_clob(avg_cost, side),
      pnl=0,  # no realized P&L — this is reconciliation, not a real unwind
      unwind_type='phantom_reconcile',
  )
  ```
  Alternative (simpler but more invasive): change I7's `_query_fill_damage` to use `positions` table (truth) instead of `fills`-`unwinds` arithmetic.
- **Acceptance criterion:** Test where: (1) insert inflated fills row, (2) run `_reconcile_positions`, (3) verify `_query_fill_damage(1h) == 0` (the inflation is compensated).
- **Risk:** Low. New unwinds row is clearly typed ('phantom_reconcile'). Reversible.
- **Related:** FX-037 (closes the source), Architecture doc §4.14 / §4.18 (I7 invariant), `phantom_fill_recovery.md` memory (the manual recipe this automates).
- **Hardening Phase:** Phase 1 (post-FX-036 cascade).
- **History:**
  - 2026-05-20 — Opened.

---

### FX-039 — `handle_fill` hardcodes `fill_type='FULL'` in DB write

- **Severity:** Low (cosmetic labelling bug; doesn't affect numerical accuracy)
- **Status:** Open
- **Tags:** `[BUG]`
- **Opened:** 2026-05-20 (noticed during FX-037 investigation)
- **Symptom:** `order_lifecycle.handle_fill` at line 267-277 calls `self.db.log_fill(..., fill_type="FULL", ...)` unconditionally, regardless of whether `detect_fills` classified the match as PARTIAL or FULL. The numerical fields (`shares`, `usd_value`) are correct; only the `fill_type` column is misleadingly labelled.
- **Proposed fix:** Pass the computed `fill_type` from `detect_fills` through to `handle_fill`. Simple plumbing change.
- **Acceptance criterion:** A partial fill (`matched < slot.shares - 0.5`) writes `fill_type='PARTIAL'` to the DB, not 'FULL'.
- **Risk:** None. Cosmetic.
- **Related:** FX-037 (found during the investigation).
- **Hardening Phase:** Phase 1 (post-FX-036 cascade, low priority).
- **History:**
  - 2026-05-20 — Opened.

---

### FX-033 — Oversight allocator doesn't consult `unliquidatable_markets`

- **Severity:** Low (was Medium pre-FX-032; FX-032 prevents the upstream cause)
- **Status:** Open
- **Tags:** `[ARCH]` `[TEST]`
- **Opened:** 2026-05-19 (surfaced by Helsinki recovery diagnostics)
- **Symptom:** During Helsinki recovery, the oversight allocator at 03:39:46 proposed 3 deploys, the SafetyController scaled the top scorer to fit BOOTSTRAP's $60 cap (FX-031), but the farmer skipped it because the cid (`0xdb22a7749b83`) was in `unliquidatable_markets`. Allocator and farmer disagreed on which markets are viable.
- **Root cause:** `oversight/allocation_writer.py::compute_allocations` (and the scorer feeding it) builds the candidate slate without joining against `unliquidatable_markets`. FX-007's gate is on the consumer (farmer) side, not the producer (oversight) side.
- **Why it matters reduced:** Pre-FX-032, the bug caused real harm — 60+ healthy cids were over-marked and the allocator wasted slots on them. Post-FX-032 only the canonical FX-007 path marks unliquidatable, so the allocator-vs-farmer disagreement is rare (only on cids the farmer has confirmed dead via 400 body). Still worth closing: avoids wasted allocator cycles + clearer telemetry (no "0/3 markets" surprise).
- **Proposed fix:** In `oversight/data_collector.py` or `oversight/market_scorer.py`, filter out cids returned by `db.load_unliquidatable_set()` before scoring. Add a regression test: insert a cid in `unliquidatable_markets`, verify it doesn't appear in the allocator output.
- **Acceptance criterion:** Oversight cycle produces 0 deploys flagged unliquidatable; new test asserts behavior.
- **Risk:** None. Filter is purely subtractive.
- **Related:** FX-007, FX-032.
- **Hardening Phase:** Post-roadmap follow-up.
- **History:** —

---

### FX-044 — I6 morning-SEVERELY spike at UTC day boundary [INVESTIGATED, NOT A CODE BUG — superseded by FX-045/046/047]

- **Severity:** n/a (original diagnosis was wrong)
- **Status:** Closed (Investigated). Moved to §4.
- **Tags:** `[INVESTIGATED]`
- **Opened:** 2026-05-22
- **Closed:** 2026-05-23 (live API probe + code reading invalidated the stated root cause)
- **Original symptom (correct):** Every day at 00:00 UTC the est/actual ratio jumps from ~5-8× to ~25-30×. SafetyController demotes to SEVERELY → trials=False.
- **Original root cause (WRONG, per executed probe 2026-05-23):** The original entry claimed `act_d` is "today's partial UTC day" and proposed switching to rolling 24h. **The code at `oversight/data_collector.py:544` already uses `cutoff_ts = time.time() - hours * 3600` — a rolling 24h window.** Option A in the original entry would be a no-op.
- **Actual mechanism (executed, 2026-05-23 probe of `data-api.polymarket.com/activity?type=REWARD` over last 30 days):**
  - Polymarket pays REWARDs as a **single daily batch at ~00:00–00:20 UTC**, not continuously. 6/6 events in last 30 days were in hour 0 UTC; inter-event gap = 24.00h ± seconds.
  - Threshold-gated: days where the bot's accrual is < $1 produce no payment that day (operator-confirmed).
  - The rolling 24h `act_d` therefore behaves as a **step function**: holds for ~23.7h then transitions sub-second when yesterday's batch ages out and today's ages in.
  - The bot's `est_d` is an instantaneous rate (`Σ daily_rate × q_share_pct`) that doesn't reset, but is **structurally over-estimated upstream** (see FX-045 — Priority 1 q_share returns 0.5 cap, not measurement).
  - Even with correctly-rolling `act_d`, the ratio is permanently > 5× CALIBRATED threshold because of the est_d upstream inflation, NOT because of a windowing bug.
- **Lesson captured (P1 verified > assumed):** the original entry stated a root cause without reading the code. Code reading would have shown that "rolling vs UTC-day" was already settled. **Always read the compute site before proposing a fix on a "this is wrong" hypothesis.**
- **Superseded by:**
  - **FX-045** — fix the upstream q_share Priority 1 over-estimation. This is the high-leverage fix.
  - **FX-046** — investigate Polymarket's actual reward formula (squared vs linear vs size-share); empirical reconciliation needed before FX-045 design decision.
  - **FX-047** — if FX-045+FX-046 don't naturally land ratio < 5×, recalibrate I6 thresholds against measured production distribution.
- **Related:** FX-045 (real upstream fix), FX-046 (formula reconciliation), FX-047 (threshold recalibration as fallback).
- **History:**
  - 2026-05-22 02:00 UTC — Opened with incorrect root cause hypothesis.
  - 2026-05-23 08:30 UTC — Investigation revealed code already uses rolling 24h. Probe of Polymarket activity API confirmed daily-batch payment cadence + $1 threshold. Real root cause is upstream q_share over-estimation (FX-045).
  - 2026-05-23 — Closed as "Investigated, not a code bug". Superseding entries opened.

---

### FX-050 — Polymarket taker fee not captured in DumpManager unwind pnl

- **Severity:** High (silent under-reporting of losses by safety machinery)
- **Status:** Open
- **Tags:** `[BUG]` `[SAFETY]`
- **Opened:** 2026-05-23 (surfaced by operator-confirmed wallet delta during FX-050 investigation)
- **Principle that surfaced it:** P1 (verified > assumed) — wallet delta did not match bot-recorded pnl; investigation cross-referenced data-api activity feed against bot DB to confirm fee gap.
- **Symptom:** On the 2026-05-22 dump cycle (50 NO shares of `0x0ed3f07970b2d212`):
  - Bot recorded `fills` row: 50 sh @ $0.20 YES-equiv = $40 (cost)
  - Bot recorded `unwinds` row: 50 sh, sell=$0.78 (NO-direct), usd_value=$39, vwap_cost=$40, **pnl=−$1.00**
  - Polymarket data-api activity feed: BUY paid $40.00, SELL received **$38.6568**
  - Actual wallet delta: **−$1.34**
  - Bot under-reported loss by $0.34 (~25-34% of true loss magnitude)
- **Root cause (executed via `dump_manager.py` line 89 code reading):**
  ```python
  sell_revenue = actual_matched * actual_price if actual_price > 0 else 0
  # ...
  self.db.log_unwind(..., usd_value=sell_revenue, vwap_cost=vwap_cost)
  ```
  Where `actual_price = float(status.get("price", 0))` reads from `client.get_order(dump_oid)`. The SDK's `price` field is the **book match price**, NOT the cash actually settled to the wallet after Polymarket's taker fee.
  
  Polymarket charges taker fees on orders that cross the spread. DumpManager's passive mode at `dump_manager.py:308-327` sets the dump SELL price to the best opposite-token bid (crossing the spread → we are the taker). Empirically the fee is ~0.88% on the gross revenue ($0.3432 / $39.00 in the verified incident).
- **Why it matters:**
  - **I7 hourly_loss invariant** (`SafetyController._query_fill_damage`) computes damage as `SUM(fills.shares × clob_cost) − SUM(unwinds.usd_value)`. Under-reported unwind usd_value → over-reported damage by ~30% (the bot LOOKS worse than reality). Wait — actually that's BACKWARDS. Under-reported revenue means damage looks LARGER (more "missing" money). But the bot recorded pnl=−$1.00 (smaller loss). Let me re-trace:
    - vwap_cost = 50 × $0.80 (CLOB cost) = $40
    - usd_value = 50 × $0.78 (sell_price) = $39
    - bot's view: net = $39 received − $40 paid = −$1 loss
    - reality: $38.66 received − $40 paid = −$1.34 loss
  - The bot UNDER-reports loss magnitude. I7 hourly_loss damage = $1 (vs actual $1.34). Kill switch threshold `24h realized_loss > 0.1·T = $22.7` would under-fire on aggregated losses by ~25-30%.
  - **Friend-rollout multiplier:** every friend's bot under-reports losses identically. On a fill+dump-heavy day, this could mean kill switch doesn't fire when it should.
- **Verified evidence (Helsinki, 2026-05-22 incident + 2026-05-23 reconstruction):**
  ```
  data-api/activity TRADE events:
    2026-05-22T20:55:17  BUY  No  price=$0.80  size=50  usdcSize=$40.0000
    2026-05-22T20:56:53  SELL No  price=$0.78  size=50  usdcSize=$38.6568
  Net wallet delta: −$1.3432 (matches operator-reported $1.34)
  
  bot_history.db.unwinds row for same incident:
    sell_price=$0.78  usd_value=$39.00  pnl=−$1.00
  Gap: $0.34 (0.88% of $39 — consistent with Polymarket taker fee)
  ```
- **Proposed fix (multiple options):**

  **Option A (cleanest): Use post-fee settled amount from data-api activity.**
  Query `data-api/activity?user=<funder>&type=TRADE&limit=1` for the matching transaction by orderHash or recent timestamp, take `usdcSize` as the true cash received. Trade-off: adds an HTTP call per dump (rate-limit concern); requires order-matching logic.

  **Option B (simpler): Apply a known fee multiplier.**
  ```python
  RF_POLYMARKET_TAKER_FEE = 0.009   # 0.9% — calibrated against observed gaps
  sell_revenue = actual_matched * actual_price * (1 - RF_POLYMARKET_TAKER_FEE)
  ```
  Trade-off: hard-codes the fee rate; if Polymarket changes their fee schedule, we miscount until we update.

  **Option C (most robust): Read on-chain ERC20 transfer events.**
  Subscribe to USDC.e Transfer events on Polygon for FUNDER address; reconcile inflows/outflows against bot's expected. Most accurate but adds infrastructure complexity (web3 connection, event filtering).

  **Option D (defense-in-depth): Cycle wallet reconciliation invariant.**
  Compare on-chain wallet pUSD against bot's expected wallet (initial_cap − Σ fill_costs + Σ unwind_revenues ± rewards) each cycle. On divergence > $0.50, emit `[CRITICAL] WALLET_DESYNC` log. Catches FX-050's symptom regardless of root mechanism; also catches future unknown unknowns. **Recommend in any case as defense-in-depth, regardless of A/B/C choice.**

  **My recommendation:** **Option B + Option D combined.** Option B is the targeted accuracy fix (single-line config knob); Option D is the safety net catching any future drift the formula doesn't predict.
- **Acceptance criterion:** After Option B+D:
  - Next dump cycle: bot's recorded pnl matches actual wallet delta within $0.05
  - Reconciliation invariant emits 0 `WALLET_DESYNC` events in 7d clean operation
  - Test: simulated dump with fee applied → recorded usd_value reflects post-fee value
- **Risk profile:**
  - Option B reversibility (P2): single-line revert; `RF_POLYMARKET_TAKER_FEE=0` config knob disables
  - Option D adds a NEW safety check — could false-positive if rewards land between cycles (CF earned rewards add to wallet without bot expecting them). Mitigation: include `expected_rewards` in the reconciliation math
  - Single-axis (P3): ship B and D as separate commits per principle
- **Related:** FX-037 (BUY-side phantom defense — orthogonal but in same "fill integrity" family), `dump_manager.py:60-87` (PHANTOM FILL defense — verifies BALANCE moved, but not BY HOW MUCH cash-wise).
- **Hardening Phase:** Phase A in the Master Plan (highest priority — restoring books integrity before Phase B's FX-045 unfreeze).
- **History:**
  - 2026-05-22 20:55-20:57 UTC — Incident occurred on Helsinki (50 NO shares of `0x0ed3f07970b2d212`).
  - 2026-05-23 — Operator noticed wallet delta vs bot pnl mismatch; flagged in session.
  - 2026-05-23 — Investigation cross-referenced data-api activity vs bot DB; confirmed $0.34 gap = Polymarket taker fee.

---

### FX-045 — Priority 1 q_share returns upper-bound heuristic, not measurement

- **Severity:** High (friend-rollout G3 structural blocker, perpetual I6 false-positive)
- **Status:** Open
- **Tags:** `[BUG]` `[ARCH]`
- **Opened:** 2026-05-23 (surfaced during FX-044 investigation)
- **Principle that surfaced it:** P1 (verified > assumed) — live probe of `reward_market_stats` and `_query_windowed_scoring` against current scored markets revealed the priority semantics produce 1500× over-estimates.
- **Symptom:** SafetyController I6 fires SEVERELY perpetually under healthy operation because est_d is over-estimated by ~1500×. CALIBRATED state is structurally unreachable, blocking friend-rollout G3 gate. Bot operates in SEVERELY ↔ MILDLY oscillation but never reaches CALIBRATED.
- **Root cause (executed):**

  Priority 1 in `oversight/data_collector.py:957-961` returns `min(scoring_ratio × 0.5, 0.5)` when ≥3 windowed scoring samples exist:
  ```python
  ws = windowed.get(cid)
  if ws and ws["samples"] >= 3:
      q_share = min(ws["scoring_ratio"] * 0.5, 0.5)
  ```
  Where `scoring_ratio = our_scoring_snapshots / total_snapshots` over a 4h window — measures **our presence in the reward zone**, NOT our share of the reward pool.

  The docstring at `data_collector.py:800-803` explicitly admits this is an upper bound:
  > "scoring_ratio = count(scoring=True) / count(total) for each market.
  > This measures what fraction of the time our orders are actually scoring,
  > which is an **upper bound** on our Q-share."

  For any well-positioned bot (all orders in zone, scoring 100% of the time), `scoring_ratio = 1.0` → `q_share = min(1.0 × 0.5, 0.5) = 0.5` (the maximum). The `× 0.5` multiplier and `min(., 0.5)` cap are hand-tuned damping — they don't make the heuristic a measurement.

  Priority 2 (cumulative `total_q_score / total_market_q`) IS a real measurement. But Priority 1 trumps Priority 2 when ≥3 windowed samples exist — designed defensively against UPWARD-poisoned cumulative data (the FX-005 era bug). It over-corrects for healthy cumulative data with small values.

- **Verified evidence (Helsinki live probe, 2026-05-23 08:42 UTC):**
  ```
  Deployed market 0x475c9930 (OpenAI valuation $2.5T HIGH):
    Priority 1 returns: q_share = 0.5000  (windowed_ratio=1.0)
    Priority 2 would return: q_share = 0.000249 (120968 / 487M cumulative)
    → est_d contribution: $30 × 0.5 = $15  (Priority 1 path)
    → est_d contribution: $30 × 0.000249 = $0.0075  (Priority 2 path)
    → ratio: 2000× over-estimate from Priority 1

  Deployed market 0x0ed3f07970 (OpenAI valuation $2.0T HIGH):
    Priority 1 returns: q_share = 0.5000  (windowed_ratio=1.0)
    Priority 2 would return: q_share = 0.000405 (91057 / 225M cumulative)
    → est_d contribution: $50 × 0.5 = $25  (Priority 1 path)
    → est_d contribution: $50 × 0.000405 = $0.020  (Priority 2 path)
    → ratio: 1235× over-estimate

  Total est_d = $40/day (Priority 1) vs $0.027/day (Priority 2)
  Actual payouts: $1.24-$4.87/day (between the two — see FX-046)
  ```
- **Why it matters:**
  - **G3 friend-rollout gate** is structurally unreachable. CALIBRATED requires `est_actual_ratio < 5×`. With est_d = $40 and act_d = $1-5, ratio is always 8-30× → I6 always fires SEVERELY → CALIBRATED never reached.
  - **Trials blocked daily.** SEVERELY has `trials=False` → cold-start market discovery is gated for the duration of the SEVERELY window (most of every day).
  - **CF computation downstream.** CF = act_d / est_d ≈ $1/$40 = 0.025 (currently smoothed CF = 0.04). The bot computes a CF much lower than the long-run truth; consumers that scale by CF systematically under-credit reward expectations.
- **Proposed fix (NOT shipping yet — gated by FX-046 investigation):**

  **Option A — Swap priority order: cumulative first, windowed as fallback.**
  ```python
  # Pseudo-code in data_collector.query_reward_stats
  if total_market_q > 0 and q_score_samples > 0:
      raw_cumulative = total_q_score / total_market_q
      if raw_cumulative > RF_POISONED_Q_SHARE_THRESHOLD:
          # poisoned guard preserved
          q_share = RF_NEW_MARKET_Q_SHARE_PRIOR
      else:
          q_share = raw_cumulative   # use measurement
  elif ws and ws["samples"] >= 3:
      q_share = min(ws["scoring_ratio"] * 0.5, 0.5)  # heuristic fallback
  elif cold_start:
      q_share = RF_NEW_MARKET_Q_SHARE_PRIOR
  ```
  Risk: if cumulative is itself broken (FX-046 may surface this), Option A overshoots LOW.

  **Option B — Replace Priority 1 with size-weighted queue share** computed over the windowed scoring snapshots' associated book snapshots. More invasive (~50 LOC + new query) but conceptually clean.

  **Option C — Calibrate Priority 1's multiplier empirically.** Replace `min(scoring_ratio × 0.5, 0.5)` with `min(scoring_ratio × K, 0.5)` where K is tuned to make est_d match actual rewards over a 7d window. Crude but works.

  **Decision gated by FX-046.** Need to know whether Polymarket's reward formula matches the bot's `(weight)² × size` Q-score model before picking between A/B/C.
- **Acceptance criterion (when fix lands):**
  - I6 ratio in production stabilises < 5× under healthy operation
  - SafetyController reaches CALIBRATED for a sustained ≥24h window
  - Wallet net return continues positive
- **Risk profile:**
  - Reversibility (P2): single-commit revert; can also gate via `config_overrides.json` knob
  - Single-axis (P3): one observable change — I6 ratio distribution
  - Risk of OVERSHOOT to under-estimation: if Priority 2 cumulative is itself broken (under-counts), swapping priorities could push CF too high and mask real degradation. Investigation FX-046 settles this.
- **Related:** FX-044 (superseded), FX-046 (formula reconciliation, gates this fix), FX-047 (threshold recalibration fallback).
- **Hardening Phase:** Phase 2 (post-FX-037 ship, after FX-046 investigation lands).
- **History:**
  - 2026-05-23 08:42 UTC — Opened. Live probe of `reward_market_stats` + `_query_windowed_scoring` confirmed Priority 1 returns 0.5 (max heuristic) for all 2 deployed markets while cumulative gives 0.000249-0.000405.

---

### FX-046 — Q-score reward model formula uncertain vs Polymarket actual payouts

- **Severity:** Medium (gates the FX-045 fix design)
- **Status:** Open — investigation, no code change pending
- **Tags:** `[ARCH]` `[INVESTIGATION]`
- **Opened:** 2026-05-23 (surfaced during FX-045 investigation)
- **Principle that surfaced it:** P1 (verified > assumed) — predicted-vs-actual payouts diverged by 24-94× across three formula candidates.
- **Symptom:** The bot's `reward_tracker.q_score_order` formula `((max_spread − dist)/max_spread)² × size` predicts ~$0.02-0.05/day from the 2 currently deployed markets. Actual payouts averaged $1.24-$4.87/day across the May 20-22 window. **24-94× discrepancy is unexplained.**
- **Possible causes (need empirical disambiguation):**
  1. **Polymarket's actual formula ≠ squared-weight.** Architecture doc §4.23.1 cites a LINEAR formula: `(1 − d/s_max) × q_share × daily_rate`. Reward_tracker uses SQUARED. One of the two is wrong.
  2. **`market_q` over-counts competition.** `estimate_market_q` sums all in-zone bids+asks. Polymarket may exclude orders below `min_size` (= 20 sh per the live probe), or apply other filters.
  3. **Market state evolved between accrual and snapshot.** Yesterday's queue might have been 100× thinner than today's at the moment of probe.
  4. **Asymmetric maker/taker counting.** Polymarket may give weight only to certain order types (maker-side resting orders, not arbitrage-driven cross-book activity).
- **Verified evidence (Helsinki probe, 2026-05-23 08:55 UTC):**
  ```
  Market 0x0ed3f07970 (mid $0.21, max_spread $0.045, daily_rate $50):
    in_zone market depth: 251,410 sh
    our order in zone: 62 sh
    Squared formula:  market_q=27,203  our_q=19.14  ratio=0.000703  → $0.035/day
    Linear formula:   market_q=67,467  our_q=34.44  ratio=0.000511  → $0.026/day
    Size-share:       market_size=251,410  our_size=62  ratio=0.000247  → $0.012/day

  Market 0x475c9930 (mid $0.115, max_spread $0.045, daily_rate $30):
    in_zone market depth: 313,659 sh
    our order in zone: 74 sh
    Squared formula:  market_q=40,859  our_q=23.75  ratio=0.000581  → $0.017/day
    Linear formula:   market_q=100,053 our_q=41.11  ratio=0.000411  → $0.012/day
    Size-share:       market_size=313,659  our_size=74  ratio=0.000236  → $0.007/day

  Total predicted: $0.019-$0.052/day across all three formulas
  Actual paid (last 3 days): $3.64, $4.87, $1.24 — 24-94× HIGHER
  Today (May 23): $0 — sub-threshold accrual confirmed by operator
  ```
- **Proposed investigation (NO code change until disambiguation):**
  1. **Read Polymarket's official maker-rewards docs.** Find the exact scoring formula. ~30 min.
  2. **Capture historical book snapshots** from Helsinki's `book_snapshots` table over 7 days. Reconstruct per-cycle market_q at the moment of each reward accrual. Back-solve which formula best matches actual payouts.
  3. **Probe with controlled placement.** Place a single order at known distance/size in a known market; measure paid reward in next cycle. Repeat at varied distances. Fit the empirical curve.
  4. **Compare against independent bot data** if available.
- **Acceptance criterion (investigation complete):**
  - A confidence-tagged claim about which formula Polymarket uses, OR
  - An explicit "we cannot determine empirically; recalibrate against measured production distribution" decision.
- **Risk profile:**
  - No code change yet — this is pure investigation. P2 reversibility trivially satisfied.
  - **Risk of NOT investigating:** shipping FX-045 with the wrong formula assumption could over- or under-correct. We'd swap "always too high" for "always too low" without knowing which is correct.
- **Related:** FX-045 (depends on this for design decision), FX-044 (superseded), Architecture doc §4.23.1 (linear formula claim) vs `reward_tracker.py:165-183` (squared formula in code).
- **Hardening Phase:** Phase 2 investigation, gates FX-045 ship.
- **History:**
  - 2026-05-23 08:55 UTC — Opened. Live probe of 2 deployed markets + 30d activity API confirmed 24-94× discrepancy across three formula candidates.

---

### FX-047 — I6 `est_actual_ratio` thresholds may need recalibration

- **Severity:** Medium (contingent — only fires if FX-045 + FX-046 don't naturally close the ratio gap)
- **Status:** Open — contingent
- **Tags:** `[ARCH]`
- **Opened:** 2026-05-23 (surfaced during FX-044 investigation)
- **Symptom:** I6 thresholds (5× CALIBRATED, 15× SEVERELY, 50× UNSAFE) were calibrated for a bot where `est_d ≈ act_d`. Current operation has structural multi-× mismatch. If FX-045 + FX-046 reduce but don't eliminate the gap, residual ratio > 5× will keep CALIBRATED unreachable.
- **Proposed fix (NOT shipping until FX-045/046 outcomes known):**
  - Measure 7d distribution of `est_actual_ratio` post-FX-045 on Helsinki.
  - Set new thresholds at P95 (CALIBRATED upper bound), P98 (SEVERELY), P99 (UNSAFE). Empirical calibration rather than fixed magic numbers.
  - Alternative: replace I6 with `smoothed_cf ∈ [low, high]` band check, which is already EMA-smoothed.
- **Acceptance criterion:** CALIBRATED state reachable for ≥24h sustained on Helsinki post-fix.
- **Risk profile:** Threshold relaxation weakens the invariant. Mitigate by keeping I5 (cf_drift) and the kill-switch (cf < 0.01) as independent backstops on actual CF collapse.
- **Related:** FX-044 (superseded), FX-045 (upstream), FX-046 (formula investigation).
- **Hardening Phase:** Phase 3 (contingent — only ship if FX-045/046 don't close the gap).
- **History:**
  - 2026-05-23 — Opened as contingent backup to FX-045/046.

---

### FX-043 — `_total_capital` stamp missing during 0-deploy alloc moments → guardrails fail-open

- **Severity:** Medium (recurring failure mode of a load-bearing safety invariant)
- **Status:** Open
- **Tags:** `[BUG]` `[SAFETY]`
- **Opened:** 2026-05-22 (surfaced during 40h post-FX-041 state analysis)
- **Principle that surfaced it:** P1 (verified > assumed) — found by grepping for `missing_signal=total_capital` in the journal across the 24h window after noticing the CF spike at 20:22 UTC May 21.
- **Symptom:** For a ~5 minute window on Helsinki 2026-05-21 19:50-19:54 UTC, every farmer cycle emitted:
  ```
  [GUARDRAIL_WARNING] missing_signal=total_capital (no deploy row with _total_capital stamp)
  [GUARDRAIL] {... "total_capital": null, "notional_ratio": null, ...}
  ```
  Concurrent guardrails ALL disabled during the window:
  - **Notional ratio check** (soft + hard) — `notional_ratio = total_live_notional / total_capital` undefined
  - **Cluster cap check** — `cluster_limit_usd = CLUSTER_NOTIONAL_LIMIT_FRAC * total_capital` undefined
  - **24h-loss kill-switch trigger** — `loss_limit = MAX_DAILY_LOSS_FRAC * total_capital` undefined → kill cannot fire on this axis
  - **Shadow signal `slow_bleed`** — emits `status: missing_data`
  
  Verified in journal: 11 consecutive [GUARDRAIL_WARNING] lines across cycles 4174-4184.
- **Why no damage this time:** `total_live_notional = $8.88` during the window (well under any threshold), no fills, no losses. The fail-open guardrails were never tested. But the design invariant ("guardrails always armed in LIVE mode") was violated for the duration.
- **Root cause (hypothesis — NOT fully verified):** The alloc file at that moment had 0 deploy rows (allocator routed everything to "avoid" momentarily — possibly during a market-list refresh, or while a deploy was being promoted/demoted). Phase 2's `_total_capital` stamp lives only on deploy rows (per architecture v5.1.10 amendment). With no deploys, the stamp is absent. `_guardrail_total_capital_from_alloc` returns None per the documented fail-open behaviour.
  
  The fix at Phase 2 (`d2612e6`, FX-013 family) was supposed to close this — "post-redistribution loop in `compute_allocations` that stamps `_total_capital = round(total_capital, 2)` on every deploy row". But the loop only stamps EXISTING deploy rows. If there ARE no deploy rows, there's nothing to stamp.
- **Why it matters:**
  - **Safety:** the kill-switch trigger on 24h realized loss > 0.1·T is disabled during these windows. If a real fill+dump happens DURING a 0-deploy moment (unlikely but possible if dump_manager is still managing previously-filled inventory), the kill threshold can't fire.
  - **Friend rollout:** recurring across friends' bots. Each transition between market-sets creates a potential 0-deploy window.
  - **Diagnostics:** `[GUARDRAIL_WARNING]` log spam during the window obscures other real warnings.
- **Proposed fix:** Add a "minimum-context row" to the allocation file that's ALWAYS present, even when num_deploy=0. Options:
  
  **Option A: stamp on the metadata row of the alloc file (not the deploy rows)**:
  ```python
  # In oversight/allocation_writer.compute_allocations, after computing total_capital:
  alloc_file_payload = {
      "generated_at": ...,
      "version": "1.0",
      "total_capital_deployed": ...,
      "_total_capital_global": round(total_capital, 2),  # NEW: always present
      "num_deploy": num_deploy,
      "num_avoid": num_avoid,
      "markets": [...],
  }
  ```
  Then `_guardrail_total_capital_from_alloc` reads `_total_capital_global` first; falls back to scanning deploy rows for backward-compat. Backward-compatible.
  
  **Option B: read from `usdc_balance` table** if alloc has 0 deploys.
  ```python
  def _guardrail_total_capital_from_alloc(self):
      try:
          # ... existing alloc-file read ...
          if total_capital_from_alloc is not None:
              return total_capital_from_alloc
          # Fallback: read from usdc_balance table
          row = self.db.execute("SELECT balance FROM usdc_balance ORDER BY ts DESC LIMIT 1").fetchone()
          if row:
              return float(row[0])
      except Exception:
          pass
      return None
  ```
  Trade-off: usdc_balance table doesn't exist (per architecture §9.1) — actually the bot writes to portfolio_snapshots, not usdc_balance.
  
  **Option C: use portfolio_snapshots as fallback** (cleanest with existing infra):
  ```python
  def _guardrail_total_capital_from_alloc(self):
      # ... existing alloc-file read ...
      if total_capital_from_alloc is not None:
          return total_capital_from_alloc
      # Fallback: latest portfolio_snapshot
      try:
          row = self.db.execute("SELECT total_value FROM portfolio_snapshots WHERE ts > strftime('%s','now') - 1800 ORDER BY ts DESC LIMIT 1").fetchone()
          if row:
              return float(row[0])
      except Exception:
          pass
      return None
  ```
  Fallback only uses portfolio snapshots within last 30 min (defends against stale data). Backward-compatible.
  
  **My recommendation:** Option A + Option C combined. Option A is the structural fix (alloc file always carries total_capital); Option C is the defense-in-depth fallback (when alloc file is stale or weird, portfolio_snapshots gives the on-chain truth).
- **Acceptance criterion:**
  - Force-test: write an alloc file with `num_deploy=0`; verify `_guardrail_total_capital_from_alloc` returns a non-None value (from the metadata stamp or portfolio_snapshots fallback).
  - Regression test: existing alloc files with deploys still return the same total_capital.
  - Helsinki verification: during the next observed 0-deploy moment (or force one via temporary config), confirm no `[GUARDRAIL_WARNING] missing_signal=total_capital`.
- **Risk profile:**
  - Reversibility (P2): single-commit revert restores fail-open behavior. Easy rollback.
  - Single-axis (P3): one observable change (`missing_signal=total_capital` log lines disappear).
  - Risk of OVER-reporting: if portfolio_snapshots is stale, total_capital might lag actual. Mitigation: 30-min freshness gate.
- **Related:** FX-013 (the v5.1.10 cycle-1 USDC write that closed the OTHER `total_capital=null` race; this is the same INVARIANT violated by a DIFFERENT mechanism), architecture doc §4.18 (guardrail dependencies on total_capital), §10.3 (will add this to known operational items).
- **Hardening Phase:** Phase 1 (post-FX-036 cascade follow-up, friend-rollout prep). Ships AFTER FX-037 + FX-042.
- **History:**
  - 2026-05-22 02:00 UTC — Surfaced during 40h post-FX-041 state analysis. Single observed window so far (~5 min on 2026-05-21 19:50-19:54). Logged before shipping for observation across ≥3 occurrences to characterize frequency.

---

### FX-042 — `orders_cancelled` table never written by production codepath

- **Severity:** Medium (latent calibrator-training bug + diagnostics gap)
- **Status:** Open
- **Tags:** `[BUG]` `[ARCH]`
- **Opened:** 2026-05-21 (surfaced during the post-FX-041 state analysis when `orders_cancelled` was found empty despite 28 placements in 24h implying many cancel-and-replace cycles)
- **Principle that surfaced it:** P1 (verified > assumed) — the discrepancy between `orders_placed` (28 rows) and `orders_cancelled` (0 rows) was the data signal that prompted code reading. Then code reading (P1 again) confirmed the missing write path.
- **Symptom:** `bot_history.db.orders_cancelled` has zero rows for the entire lifetime of the production farmer (and presumably since the v3.x→v4.0 refactor). `[CYCLE_SUMMARY] orders_cancelled` is accurate because it uses the in-memory `_cycle_orders_cancelled` counter, but the DB table is empty. Verified via Helsinki SQL query on 2026-05-21 ~04:42 UTC.
- **Root cause:** During the v3.x → v4.0 (continuous allocator) / v5.0 (execution modes) refactor sequence, the cancel codepath was rewritten:
  - **NEW (production):** `_gated_cancel_order` (reward_farmer.py:1740-1784) → `OrderLifecycle.cancel_order` (order_lifecycle.py:220-242) → V2 SDK `client.cancel_order(OrderPayload(...))` → file log. **No DB write.**
  - **OLD (legacy):** `order_manager.py:413, 421, 440` → `db.log_order_cancelled(...)`. **Inactive in production.**
  
  grep confirms: ZERO occurrences of `log_order_cancelled` in `reward_farmer.py`, `order_lifecycle.py`, or `dump_manager.py` (the three files in the production cancel chain).
- **Why it matters (currently latent, future-active):**
  - **Right now:** invisible. Fill model is dormant (`FillModel.is_ready() == False` until ≥50 fills + ≥15 positives). On the $221 wallet with zero fills in 24h, this might not activate for weeks.
  - **When fill model activates:** `calibration/features.py:117` queries `orders_cancelled` to label orders as `"cancelled"` for training. Empty table → every cancelled order gets default label `"alive"`. The fill model learns that orders look "alive" longer than they actually do → systematically biases `p_fill` predictions → biases allocator `w_i = R / (1 + p·L)` → biases placement & sizing decisions.
  - **Friend-rollout multiplier:** each friend's bot has an independent DB + independent fill model. With this bug, every friend's fill model trains on the same kind of broken labels. **G2 → all HIGH-severity items must ship — this is Medium so it doesn't formally gate, but shipping it before friend rollout is the right call.**
- **Proposed fix:** Add `db.log_order_cancelled(...)` write inside `OrderLifecycle.cancel_order` AFTER the SDK call succeeds. Look up context (`condition_id, side, price, placed_at`) from `active_orders` table to populate the row fields properly; fall back to defaults on missing row (cancel-then-cleanup race).
  ```python
  # In OrderLifecycle.cancel_order, after self.client.cancel_order() succeeds:
  try:
      import time as _t
      now = _t.time()
      row = self.db.conn.execute(
          "SELECT condition_id, side, price, placed_at FROM active_orders WHERE order_id = ?",
          (order_id,),
      ).fetchone()
      if row:
          cid_v, side_v, price_v, placed_at = row
          age = max(0.0, now - float(placed_at))
          self.db.log_order_cancelled(
              ts=now, order_id=order_id, reason=reason,
              condition_id=cid_v, side=side_v, price=price_v, age_secs=age,
          )
      else:
          self.db.log_order_cancelled(ts=now, order_id=order_id, reason=reason)
  except Exception as e:
      log.debug(f"DB log_order_cancelled error: {e}")
  ```
- **Why inside `cancel_order` (not `_gated_cancel_order`):** `_gated_cancel_order` skips the actual API call in DRY/SHADOW. Logging only when the API succeeds (LIVE or kill-switch force path) keeps the table semantically = "real cancellations" exactly. No phantom rows in non-LIVE testing.
- **Acceptance criterion:**
  - Mocked successful cancel → DB row inserted with looked-up context (cid, side, price, age_secs).
  - Mocked API failure → no DB row inserted (the try-block returns False before the DB write).
  - Mocked `active_orders` lookup empty → fallback row inserted with default empty cid/side/price.
  - Mocked `db.log_order_cancelled` failure → API cancel still returns True (graceful degradation).
  - Helsinki post-deploy: `SELECT COUNT(*) FROM orders_cancelled WHERE ts > <deploy_ts>` returns non-zero within 1-2 cycles.
- **Risk profile:**
  - Forward-only — existing rows untouched; new writes flow into existing columns (all defaulted, schema verified compatible).
  - Reversibility (P2): single-commit revert restores the no-write behavior; old rows remain accurate.
  - Single-axis (P3): one observable hypothesis — "after deploy, orders_cancelled accumulates rows at roughly the rate of cancel-and-replace cycles". Easy to confirm/refute in production.
- **Estimated effort:** ~10 LOC source change + 4-5 tests + 1 Helsinki verification query. ~45 min.
- **Hardening Phase:** Phase 1 (post-FX-036 cascade follow-up). Ships AFTER FX-037 (which is HIGH severity).
- **Related:** FX-004 (counter/DB consistency for orders_placed — same class of refactor-induced gap), `calibration/features.py:117` (the downstream consumer).
- **History:**
  - 2026-05-21 — Surfaced during post-FX-041 state analysis. Code investigation under the observation hold confirmed root cause. Doc-only update during hold; fix deferred until after FX-037 ships and the hold lifts.

---

### FX-034 — `_reprobe_unliquidatable` doesn't un-mark cids on successful book fetch

- **Severity:** Low (FX-032 prevents most accumulation)
- **Status:** Open
- **Tags:** `[BUG]`
- **Opened:** 2026-05-19 (surfaced by Helsinki recovery diagnostics)
- **Symptom:** Helsinki cycle 2 logged `Unliquidatable re-probe: 0 un-marked, 60 still dead` immediately AFTER fetching ~60 book token_ids that all returned HTTP 200 OK. Direct probe of one of those cids (`0xdb22a7749b83`, the Iran market) returned a healthy book with 22 bids + 40 asks. So either the re-probe doesn't actually fetch the book it claims, or its "is alive" check is wrong.
- **Root cause:** Unclear without further reading of `_reprobe_unliquidatable` in `reward_farmer.py:833+`. Hypotheses: (a) re-probe uses a stricter "alive" check than the book-fetch path (e.g., requires bids AND asks at meaningful prices, rejecting the `$0.01 wall` that's typical for low-liquidity markets); (b) the re-probe iterates a different cid list than the active markets, and the 60 fetches we saw were unrelated; (c) the re-probe times out or fails silently and reports 0.
- **Why it matters reduced:** Pre-FX-032, the re-probe was the only path to un-mark cids that the over-aggressive dead-cleanup had flagged. Post-FX-032, far fewer cids reach the table (only canonical FX-007 resolved markets). Re-probe still useful as a safety net but no longer load-bearing.
- **Proposed fix:** Read the re-probe implementation, add structured logging per cid (`re-probe cid=X result={alive,still_dead,error}`), then either tighten the un-mark check or fix whatever silent failure mode is suppressing it. Add a regression test that pre-populates `unliquidatable_markets` with a known-alive cid and asserts the re-probe un-marks it.
- **Acceptance criterion:** Re-probe un-marks at least one cid in a test scenario where the book endpoint returns healthy data; production logs show non-zero `un-marked` counts.
- **Risk:** Low. Re-probe loop is isolated.
- **Related:** FX-007, FX-028 (introduced the re-probe), FX-032.
- **Hardening Phase:** Post-roadmap follow-up.
- **History:** —

---

## 4. Fixed issues

### FX-052 + FX-053 — OverCommitAllocator [FIXED]

- **Severity:** FX-052 Critical (Ground Rule 2 violation); FX-053 Critical (Ground Rule 1 violation)
- **Status:** Fixed (pending commit, 2026-05-28 — P2 of 9/10 plan)
- **Tags:** `[ARCH]`
- **Opened:** 2026-05-26 (after 2026-05-25 kill-switch event + ground_rules.md establishment)
- **Closed:** 2026-05-28
- **Symptom (FX-052):** Pre-P2 SimpleAllocator capped total notional via `DEPLOY_RATIO=0.95` + `MAX_PER_MARKET_USD=$60` + `MAX_DEPLOYED_MARKETS=20` → max $1140 notional on a $1.2k wallet. Polymarket permits 3-8× overcommit (collateral-rebalance auto-cancels other orders when one fills). Operating 3-8× BELOW design point.
- **Symptom (FX-053):** Pre-P2 `MAX_DEPLOYED_MARKETS=20` + `MIN_EXPECTED_PER_MARKET=$0.01` trimmed Polymarket's ~5000-market reward pool to 20 deploys. Aggregate strategy (Polymarket's $1/day per-USER payout threshold) requires being on 100-500 markets at min_size, not 20 markets at max size.
- **Root cause:** Conservative defaults from the SimpleAllocator era, when the bot was operating at $200 wallet and the priority was "don't lose money fast." Under v6.0 ground rules (max-farm + overcommit + self-learning), those defaults became architectural debt — and would have force-tripped the farmer's kill switch on cycle 1 of overcommit operation if shipped without P1's threshold retune.
- **Fix applied (single commit):**
  - **`config.py` — 5 new cfg knobs:** `RF_OVERCOMMIT_MIN_DAILY_RATE_USD=10.0`, `RF_OVERCOMMIT_MIN_EXPECTED_PER_MARKET=0.01`, `RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS=500` (soft sanity cap, not the design constraint), `RF_OVERCOMMIT_PER_MARKET_BUFFER_FRAC=0.10`, `RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC=0.02`. All hot-reloadable.
  - **`simple_allocator.py` — class name retained, semantics transformed:** SimpleAllocator class kept for import-site compatibility; module docstring updated to call it OverCommitAllocator. Module constants promoted to cfg accessors. Removed `MAX_PER_MARKET_USD`, `MIN_PER_MARKET_USD`, `DEPLOY_RATIO` entirely.
  - **`_est_cost_per_market`:** rewritten — cost-to-score = `min_size × midpoint × 2 × (1 + buffer)`. No more per-market dollar cap. Typically $20-50 per market.
  - **NEW `_estimate_fill_cost`:** `position_notional × EXPECTED_FILL_COST_FRAC` (default 2% slippage). Used by the positive-EV gate.
  - **`compute()` rewritten:** dropped budget tracking (`used += cost_per_market` against `budget`). Dropped market-count check (`if len(deploys) >= MAX_DEPLOYED_MARKETS` becomes soft cap at 500). New positive-EV gate: `if m.expected_daily_reward < expected_fill_cost: avoid`. New `[OVERCOMMIT_ALLOC]` telemetry log line per cycle.
  - **`write_allocation_json` — v1.1 → v1.2:** new top-level metadata `_notional_overcommit_ratio` (= `capital_deployed / wallet`) + `_target_market_count_band = [50, 200]` for monitoring vs Ground Rule 1.
- **Failure modes (all fail-open):**
  - 0 candidates → 0 deploys, 0 avoids, metadata still stamped (FX-043 invariant preserved)
  - q_share=0 → fails MIN_EXPECTED_PER_MARKET filter → avoid (no div-by-zero)
  - min_size=0 (API anomaly) → cost-to-score = 0 + buffer → small valid value → handled
  - High q_share doesn't inflate sizing (cost-to-score is independent of expected reward)
  - Above soft cap (500) → excess routed to avoid, [OVERCOMMIT_ALLOC] log shows the cap binding
- **Verification:**
  - **18 new adversarial tests** in `tests/test_p2_overcommit_allocator.py` across 6 attack families:
    - **AO-A (Overcommit guarantees × 4):** 50 markets all deploy; 200 markets all deploy; 700 markets hit soft cap; notional 3-8× wallet verified.
    - **AO-B (EV-gate × 3):** positive-EV deploys; negative-EV avoids; boundary case.
    - **AO-C (Pre-P2 filters respected × 3):** cooldown excluded_cids still drops; FX-056 extreme-price < 0.10 drops; FX-056 > 0.90 drops.
    - **AO-D (Kill-switch + edge × 2):** kill switch overrides even with 200 candidates; 0-candidate cycle stamps metadata cleanly.
    - **AO-E (Telemetry × 3):** [OVERCOMMIT_ALLOC] log emitted; _notional_overcommit_ratio matches real; _target_market_count_band == [50, 200].
    - **AO-F (Adversarial × 5):** min_size=0 no crash; q_share=0 EV-filters; high q_share doesn't inflate sizing; excluded_cids=None treated as empty set; 500-market compute completes in <5s.
  - **5 existing test_simple_allocator tests updated** to assert NEW OverCommit semantics: C7 soft cap at 500 (was hard 20-cap); C8 cost-to-score not wallet-fraction (was MAX_PER_MARKET_USD cap); C9 overcommit verified (was DEPLOY_RATIO cap); + 2 NEW (C16 positive-EV gate, C17 metadata stamps).
  - **265 tests pass** across P2 + P1 + all prior FX (FX-054, FX-057, FX-051, FX-045) + adjacent suites. Zero regressions.
- **What's intentionally NOT in this commit:**
  - **Conservative q_share margin (FX-046 mitigation).** That's P3 of the 9/10 plan — separate single-axis commit. For now, the OverCommitAllocator uses the existing 3-tier q_share resolution (API > cumulative > cold-start). If FX-046's residual 24-94× under-prediction matters under overcommit operation, P3 will fold in a `min(api_q, cumulative_q × 0.5)` factor.
  - **Self-correction triggers wired to behavior change (P4 scope).** Currently 2 of 6 wired (FX-051 cooldowns). P4 adds 2 more (fill_rate + global_loss). Out of P2 scope.
  - **Production deployment.** P5 of 9/10 plan handles staged rebring-up (paper → shadow → live cutover). P2 ships the code, no production change.
- **Risk profile:**
  - **Reversibility (P2):** Single revert restores the SimpleAllocator semantics. All removed constants stay removed (cfg knobs replace them). Roll back any cfg knob via `config_overrides.json`: set `RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS=20` to restore the old cap; `RF_OVERCOMMIT_PER_MARKET_BUFFER_FRAC=10` to inflate per-market sizing; etc.
  - **Single-axis (P3):** OverCommitAllocator is one architectural change addressing both FX-052 (notional cap) and FX-053 (market count cap) — they're the same fix (drop the artificial budget/count caps, replace with EV gate). Land together because shipping one without the other is incoherent (FX-053 without FX-052 would be 200 markets at $60 each = $12k notional uncapped; FX-052 without FX-053 would still cap at 20 markets).
  - **Blast radius if EV gate is wrong:** the gate uses a 2% slippage assumption that may be off. Worst case: gate is too permissive → bot deploys on small-reward markets and loses on slippage → FX-051 cooldowns kick in within 24h → market gets dropped. Worst case in the other direction: gate is too restrictive → bot under-deploys → low reward yield → observable in [OVERCOMMIT_ALLOC] log → operator raises `RF_OVERCOMMIT_MIN_DAILY_RATE_USD` or lowers `RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC`. Both directions are observable + tunable without code.
- **Related:** FX-051 (cooldown filter — still wired via `excluded_cids` param); FX-056 (extreme-price filter — still applied); FX-058 (farmer kill-threshold retune — P1 prereq); FX-043 (_total_capital metadata stamping — P1 prereq); ground_rules.md Rule 1 + 2.
- **History:**
  - 2026-05-26 — Both FX-052 and FX-053 opened after ground_rules.md established + 2026-05-25 kill-switch event surfaced the violations.
  - 2026-05-28 — Both closed together as P2 of 9/10 plan. 18 adversarial tests + 5 updated existing tests; 265 tests pass; 0 regressions. Unblocks P3 (FX-046 conservative margin).

---

### FX-058 + FX-043 — Farmer kill-threshold retune + _total_capital metadata stamping [FIXED]

- **Severity:** FX-058 High (Ground Rule 2 violation); FX-043 Medium (recurring silent fail-open)
- **Status:** Fixed (pending commit, 2026-05-28 — P1 of 9/10 plan)
- **Tags:** `[ARCH]` `[SAFETY]`
- **Opened:** FX-058 2026-05-28 (during 9/10 plan drafting — recognised as the implicit blocker for FX-052/053); FX-043 2026-05-22 (Helsinki observation)
- **Closed:** 2026-05-28
- **Principle that surfaced them:** P1 (verified > assumed) — the 9/10 plan research agents inventoried the farmer-side guardrails and confirmed the hardcoded notional thresholds (2.0/2.5) are anti-design under Ground Rule 2's overcommit target (3-8× wallet notional). Without P1, FX-052/053 would have force-tripped the kill switch on its first cycle of overcommit operation.
- **Symptom (FX-058):** SimpleAllocator's `DEPLOY_RATIO=0.95` capped total notional below the wallet, masking the underlying farmer-side fail-mode. But the moment FX-052/053 OverCommitAllocator ships and deploys at design (3-8× wallet notional), the existing `MAX_NOTIONAL_RATIO=2.0` would force-trip kill switch on cycle 1.
- **Symptom (FX-043):** Observed Helsinki 2026-05-21 19:50-19:54 UTC. Alloc file had 0 deploy rows (transition window during reallocation), so `_guardrail_total_capital_from_alloc` returned None. All wallet-fraction guardrails (notional ratio, cluster cap, 24h-loss kill, CF kill) silently failed-open. No damage that incident (zero activity in the window), but invariant "guardrails always armed in LIVE" was violated.
- **Fix applied (single commit):**
  - **`config.py` — 4 new cfg knobs:** `RF_MAX_NOTIONAL_RATIO=5.0`, `RF_HARD_NOTIONAL_RATIO=8.0`, `RF_RAPID_GROWTH_KILL_RATIO=5.0`, `RF_RAPID_GROWTH_WINDOW_SEC=300.0`. All hot-reloadable via `config_overrides.json`.
  - **`reward_farmer.py` — module constants → cfg accessors:** `MAX_NOTIONAL_RATIO` and `HARD_NOTIONAL_RATIO` become `MAX_NOTIONAL_RATIO()` and `HARD_NOTIONAL_RATIO()` zero-arg helpers. 5 call sites updated.
  - **`reward_farmer.py` — new `_guardrail_rapid_notional_growth`:** maintains a deque of `(ts, notional_ratio)` samples bounded by `RAPID_GROWTH_WINDOW_SEC`. On each cycle: append current ratio, evict stale samples, compute `max/min` over remaining window. Kill if observed > `RAPID_GROWTH_KILL_RATIO`. Fail-open semantics: `kill_ratio=0` disables entirely; `notional_ratio=None` leaves deque unchanged (DB hiccup can't reset the window NOR trigger false kill); cold-start (single sample) yields no kill; min clamped to 0.0001 to prevent div-by-zero.
  - **`reward_farmer.py` — `_guardrail_check_and_log` wiring:** rapid-growth result added to `kill_reasons` alongside the existing 3 (daily-loss, CF, fill-rate). Single kill-switch trigger surface.
  - **`simple_allocator.py:write_allocation_json` — top-level metadata stamp:** payload now includes top-level `_total_capital` (in addition to per-row stamps for backward compat). Bumped version `simple-1.0 → simple-1.1`.
  - **`reward_farmer.py:_guardrail_total_capital_from_alloc` — FX-043 resolution chain:** metadata → deploy row → avoid row → None. Single iteration over markets to find first deploy/avoid stamp. Net: any cycle whose allocator successfully ran (even with 0 deploys) carries a usable capital signal.
- **Failure modes (all fail-open):**
  - Missing alloc file → returns None, logs `[GUARDRAIL_WARNING]`, all wallet-fraction guardrails skip (legacy behaviour preserved for the genuinely-broken case)
  - Corrupted alloc JSON → returns None, logs warning with error detail
  - `kill_ratio=0` disables rapid-growth kill (operator escape hatch)
  - `notional_ratio=None` leaves deque unchanged (prevents DB hiccup from resetting burst-detection window)
- **Verification:**
  - **17 new adversarial tests** in `tests/test_p1_farmer_retune.py` across 4 attack families:
    - **AT-A (cfg-driven × 3):** defaults match overcommit design, config override retunes without code change, kill-disabled via zero.
    - **AT-B (rapid-growth × 6):** 6× burst kills; gradual 1.5× growth doesn't; cold-start single sample no kill; stale samples evict from window; missing signal no kill no deque mutation; div-by-zero guarded.
    - **AT-C (FX-043 fallback × 6):** metadata preferred; 0-deploy returns metadata capital; legacy alloc no metadata falls back to deploy row; 0-deploy + no metadata falls back to avoid row; completely empty alloc returns None with warning; missing file returns None with warning.
    - **AT-D (end-to-end × 1):** SimpleAllocator → alloc.json → farmer reader round trip with 0-deploy AllocationResult.
  - **Full 243-test sweep clean** across P1 + FX-054 + FX-057 + FX-051 + simple_allocator + simple_oversight + order_lifecycle + database_persistence + capital_flow + shutdown + wallet_reconciliation + dump_manager_fee + oversight_shadow. Zero regressions.
- **What's intentionally NOT in this commit:**
  - **Per-cluster notional ratio retune** (CLUSTER_NOTIONAL_LIMIT_FRAC=0.5). Cluster cap is per-cluster, not total, so 0.5× is fine for overcommit. Each cluster can hold 50% of capital independently of the global multiplier.
  - **Daily-loss threshold change** (MAX_DAILY_LOSS_FRAC=0.1). 10% wallet loss in 24h is a HARD kill regardless of overcommit; the design point is "lose ≤10% before halt".
  - **Per-market cap at farmer side.** Already absent (delegated to allocator policy). OverCommitAllocator's per-market sizing is the only constraint.
- **Risk profile:**
  - **Reversibility (P2):** Single revert restores hardcoded 2.0/2.5 constants. Cfg accessors stay (additive). Rapid-growth kill stays (additive, escapes to disabled via `RF_RAPID_GROWTH_KILL_RATIO=0` in `config_overrides.json`). FX-043 metadata stamp is backward compat (legacy alloc.json without metadata still resolves via per-row fallback).
  - **Single-axis (P3):** Both FX-058 + FX-043 are "make farmer safe for overcommit". They land together because (a) OverCommitAllocator without retune trips kill on cycle 1; (b) overcommit operation has more frequent 0-deploy transitions (during reallocation) where FX-043's fail-open would mask real over-exposure.
  - **False-kill risk on rapid-growth:** the 5× threshold over 5 min was chosen because: (a) normal overcommit operates at 3-5× steady state; (b) a healthy allocator can transition between regimes (e.g., new markets joining) within 1-2 cycles, so single-sample anomalies won't fire; (c) a misconfigured allocator deploying 10× normal would trip almost immediately. Operator can tune via `config_overrides.json` if production shows over-firing.
- **Related:** FX-052/053 (OverCommitAllocator — the P2 ship this fix unblocks), FX-049 (wallet reconciler — independent backstop catching cash drift), ground_rules.md Rule 2 (3-8× wallet notional design).
- **History:**
  - 2026-05-22 — FX-043 opened (Helsinki incident).
  - 2026-05-28 — FX-058 opened during 9/10 plan drafting + both shipped together as P1 with 17 adversarial tests, 243 tests pass, 0 regressions.

---

### FX-045 — Priority 1 q_share demoted from magnitude estimator to presence gate [FIXED]

- **Severity:** High (friend-rollout G3 structural blocker pre-fix; closed post-fix for the OLD oversight_agent path)
- **Status:** Fixed (pending commit, 2026-05-28)
- **Tags:** `[BUG]` `[ARCH]`
- **Opened:** 2026-05-23 (surfaced during FX-044 investigation; live probe of `_query_windowed_scoring` + `reward_market_stats`)
- **Closed:** 2026-05-28
- **Principle that surfaced it:** P1 (verified > assumed). The original entry stated a 1500× over-estimate hypothesis; live probe of two deployed markets at 08:42 UTC confirmed the exact numbers.
- **Symptom (unchanged from open entry):** Pre-fix Priority 1 returned `min(scoring_ratio × 0.5, 0.5)`. The numerator `scoring_ratio = our_scoring_snapshots / total_snapshots` measures **our presence in the reward zone over a 4h window** — not our share of the reward pool. For any well-positioned bot (all orders in zone, scoring 100% of the time), `scoring_ratio = 1.0` → `q_share = 0.5` (the cap), regardless of total queue depth. Priority 1 trumped Priority 2 (cumulative measurement) when ≥3 windowed samples existed, so the over-estimate dominated. Live probe 2026-05-23 measured Priority 1 returning 0.5 for both deployed markets while cumulative gave 0.000249–0.000405 — a **1235–2000× over-estimate**. The over-estimate fed I6 as `est_d`, blocking CALIBRATED state structurally (`est_actual_ratio > 5×` always tripped). Friend-rollout G3 gate was unreachable.
- **Root cause:** Priority 1's formula conflates two distinct quantities — "fraction of cycles we were in-zone" (a presence measurement) and "fraction of reward pool we get" (a magnitude measurement). The original `× 0.5` multiplier and `min(., 0.5)` cap were hand-tuned damping; they don't make the heuristic a measurement. Priority 2 (cumulative `total_q_score / total_market_q`) IS a real measurement of magnitude, but Priority 1 always trumped it when samples existed.
- **Fix applied (Approach E — presence-gate semantics, single commit):**
  - **`oversight/data_collector.py`** — two new module-level constants:
    ```python
    RF_WINDOWED_PRESENCE_GATE = 0.10        # if scoring_ratio < 10%, presence gate fires
    RF_WINDOWED_PRESENCE_MIN_SAMPLES = 3    # require ≥ 3 windowed samples before gating
    ```
  - `query_reward_stats`'s priority block rewritten:
    ```python
    # FX-045 presence-gate path
    ws = windowed.get(cid)
    if (ws and ws["samples"] >= RF_WINDOWED_PRESENCE_MIN_SAMPLES
            and ws["scoring_ratio"] < RF_WINDOWED_PRESENCE_GATE):
        q_share = 0.0                       # presence gate fires — override cumulative
        presence_gated += 1
    elif total_market_q > 0 and d.get("q_score_samples", 0) > 0:
        # Priority 2: cumulative (a real measurement) with poisoned-row guard
        raw_cumulative = d["total_q_score"] / total_market_q
        if raw_cumulative > RF_POISONED_Q_SHARE_THRESHOLD:
            q_share = RF_NEW_MARKET_Q_SHARE_PRIOR    # poisoned → prior
        else:
            q_share = raw_cumulative
            if on_book > 4.0 and q_share > 0.5:
                q_share = 0.5  # cumulative cap preserved
    elif on_book < 2.0 and d.get("q_score_samples", 0) == 0:
        q_share = RF_NEW_MARKET_Q_SHARE_PRIOR        # cold-start prior
    else:
        q_share = 0.0
    ```
  - `[Q-share]` log line updated to include `presence_gated` counter.
- **Why the presence-gate semantics:** The windowed signal IS useful — it measures whether we're actually in-zone right now. If we have history but the windowed signal shows we're rarely scoring, the cumulative average is stale and over-counts our current capture. The gate uses windowed for what it actually measures (presence) while ceding magnitude to the cumulative measurement.
- **Architectural blast radius:** The fix touches `oversight/data_collector.py:query_reward_stats`, which is called by `oversight_agent.collect_all`. `simple_oversight.run_once` (current production entry point) does NOT call this path — it uses `SimpleAllocator.fetch_current_q_shares` (Polymarket's `/rewards/user/percentages` API, which returns the real q_share Polymarket itself measures). So the fix has NO IMMEDIATE PRODUCTION IMPACT — it lifts a structural friend-rollout blocker for if/when oversight_agent comes back.
- **Failure modes:**
  - Windowed signal absent (no scoring_snapshots for cid) → falls through to cumulative unchanged
  - Windowed signal noisy (samples < 3) → presence gate inhibited (matches pre-fix sample gate)
  - Cumulative + windowed both absent → cold-start prior (unchanged)
  - Stale market (>6h since last scoring snapshot, on_book > 1h) → q_share=0 via existing staleness gate (unchanged)
- **Verification (201 tests pass, 0 regressions):**
  - **13 new adversarial tests** in `tests/test_audit_q_share_resolution.py`:
    - **QS-A (priority resolution × 8):** well-positioned bot uses cumulative not inflated max; presence gate fires on 0% scoring; presence gate fires on 5% (< 10% threshold); above-gate uses cumulative; below sample-gate windowed ignored; no-windowed uses cumulative; presence gate wins over cold-start prior; poisoned cumulative falls to prior.
    - **QS-B (invariants × 3):** q_share never exceeds real cumulative ratio (5 markets); staleness gate unchanged; well-positioned q_share drops < 1/100 of pre-fix value (the 1666× reduction headline).
    - **QS-C (incident regression × 2):** exactly reproduces the 2026-05-23 Helsinki probe shapes for both deployed markets (`0x475c9930` and `0x0ed3f07970`) and asserts post-fix q_share matches the cumulative ratio (no 1235–2000× over-estimate).
  - **Adjacent regression sweep:** 188 tests pass across test_data_collector + test_audit_fill_detection + test_audit_cooldown_logic + test_decision_policy + test_market_roi_tracker + test_order_lifecycle + test_database_persistence + test_simple_allocator + test_simple_oversight + test_cf_clamp + test_capital_flow + test_wallet_reconciliation + test_dump_manager_fee. Zero regressions.
- **What's intentionally NOT in this commit:**
  - **FX-046 cumulative formula investigation.** Separate concern. The cumulative path's accuracy (squared vs linear vs size-share Q-score formula) is independent from FX-045's specific bug. FX-046 stays open; if/when resolved it would refine Priority 2's accuracy without changing FX-045's gate semantics.
  - **FX-047 I6 threshold recalibration.** Contingent on whether FX-045+FX-046 close the ratio gap. With FX-045 alone, the I6 ratio shifts from `est=$40 / act=$1-5 = 8-40×` (pre-fix, perpetually firing) to `est=$0.027 / act=$1-5 = 0.005-0.027×` (post-fix, well under the 5× CALIBRATED threshold). G3 should be reachable. Recalibration only needed if production shows the new ratio is unstable.
  - **Option B (replace Priority 1 with size-weighted queue share)** — invasive (new query + book-snapshot reconstruction). Approach E (presence-gate) is the minimum-viable structural fix.
  - **Removing Priority 1 entirely** — considered but the presence gate is a valuable safety override (catches "we're not scoring anymore" markets where cumulative would be stale).
- **Risk profile:**
  - **Reversibility (P2):** Single-commit revert restores pre-fix behaviour. Constants can be tuned via `config_overrides.json` if production shows the 10% threshold too loose/tight.
  - **Single-axis (P3):** One logical change — Priority 1's role. Test file + code + log-line update land together because they're contracted as a unit.
  - **Risk of over-correction:** Approach E doesn't compute magnitude from windowed signal at all; falls through to cumulative. If cumulative is itself wrong (FX-046's open question), magnitudes will be wrong but in the OPPOSITE direction from the pre-fix bug (under-estimate instead of over-estimate). I6 stops firing because the check is one-directional (only fires on est > 5× act). CF computation in oversight_agent could over-credit if est is under-estimated, but CF is clamped to [0.01, 3.0] by `_smooth_correction_factor` — bounded blast radius.
  - **Production verification deferred:** Current production uses simple_oversight, which bypasses data_collector entirely. Verification of this fix's behaviour against real Helsinki shapes requires re-enabling oversight_agent OR running data_collector standalone against a populated DB. Out of scope for the immediate commit.
- **Related:** FX-044 (superseded by FX-045/046/047 chain), FX-046 (cumulative formula investigation — still open, would refine Priority 2 accuracy), FX-047 (I6 threshold recalibration — contingent, likely obviated by this fix), FX-005 (legacy poisoned-row source — guard preserved at lines 1003-1014).
- **History:**
  - 2026-05-23 08:42 UTC — Opened. Live probe of `reward_market_stats` + `_query_windowed_scoring` confirmed Priority 1 returns 0.5 for both deployed markets while cumulative gives 0.000249-0.000405.
  - 2026-05-28 — Fix designed via Approach E (presence-gate semantics — windowed used for what it actually measures, cumulative ceded magnitude). 13 adversarial tests written; 12 passed immediately, 1 test-fixture issue (staleness gate's `on_book > 1` strict-greater check needed time_on_book_secs=7200 not 3600) closed. 201 tests pass across FX-045 + FX-054 + FX-057 + FX-051 + adjacent suites.

---

### FX-054 — Fill-detection root-cause fix via 3-axis defensive design [FIXED]

- **Severity:** Critical (Ground Rule 3 prerequisite — without accurate fill capture, FX-051's ROI tracker has no input and the auto-correction loop has no signal)
- **Status:** Fixed (pending commit, 2026-05-28)
- **Tags:** `[BUG]` `[ARCH]`
- **Opened:** 2026-05-26 (surfaced by 2026-05-25 12:05 UTC kill-switch event: 9 on-chain BUYs, 1 row in fills table)
- **Closed:** 2026-05-28
- **Principle that surfaced it:** P1 (verified > assumed). The original entry's 4 hypotheses were stated as "needs investigation before code change" — investigation by reading the code surfaced TWO additional root causes the entry didn't enumerate.
- **Symptom (unchanged from open entry):** On 2026-05-25 during a 3.5h window, data-api/activity TRADE events for the funder showed 9 BUYs totaling $844. Bot's `fills` DB table had only **1** row. In-memory `ms.fill_times` detected 4 fills (kill switch saw them) — but the DB writes didn't keep up. Wallet trajectory ($1222.93 → $1196.98 = −$25.95) matched data-api truth, not bot DB.
- **Root cause (executed code reading):**
  - **A — Silent DB write failures.** Pre-FX-054 `database.log_fill` caught all exceptions at `log.debug` level and returned `None`. The bd5a54e `[FILL_WRITE] succeeded` instrumentation logged unconditionally after the call returned — it could not distinguish actual success from swallowed exception. WAL lock contention, schema mismatch, disk pressure, anything → silent.
  - **B — Phantom check zeroed legitimate fills on Polygon balance-confirmation lag.** Polymarket's V2 SDK reports `size_matched > 0` BEFORE the CTF transfer confirms on Polygon (2–5s nominal, longer with RPC cache lag). `_check_buy_phantom_fill` queried the on-chain balance immediately and got 0, declared phantom, returned 0 matched → routed into `phantom_zeroed` branch with NO DB write. The 4 in-memory `ms.fill_times` entries that the kill switch saw confirm SDK reported the fills; the missing DB rows confirm they were zeroed somewhere downstream.
  - **C — Fixit's listed hypotheses (cycle interval, network timeout, write race, dump-time BUY)** are also plausible but A and B explain the symptom shape better and address a wider class of future failures. All 4 are covered by the F3 drift catch-up below.
- **Fix applied (3-axis defensive design — pending commit):**
  - **F1 — Idempotent fills + truthful return value (`database.py` + `order_lifecycle.py:handle_fill`).** Added `order_id` + `fill_event_id` columns to `fills` table via migration; created partial unique index `idx_fills_event_id ON fills(fill_event_id) WHERE fill_event_id != ''` so non-empty event_ids enforce dedup but empty (legacy / test) rows still insert append-only. `log_fill` rewritten to use `INSERT OR IGNORE`, return `bool` (True=inserted, False=duplicate or error), defensively coerce `None → ''` for the NOT NULL TEXT columns (because INSERT OR IGNORE silently swallows ALL constraint violations including NOT NULL — exactly the silent-failure surface this fix is designed to close). `handle_fill` checks the return value and emits an HONEST `[FILL_WRITE]` log step: `attempting` / `succeeded` / `duplicate` (with re-query disambiguation) / `FAILED` (when log_fill returns False AND re-query confirms row absent — the smoking gun for a real DB write failure). Exceptions in log_fill now log at WARNING level + return False.
  - **F2 — Balance-lag tolerance (`order_lifecycle.py:_check_buy_phantom_fill`).** New constant `FILL_BALANCE_LAG_TOLERANCE_SEC = 60`. Phantom check now accepts an optional `slot` kwarg and computes `elapsed = time.time() - slot.placed_at`. When `actual_delta == 0 AND elapsed < tolerance`, fails-OPEN (returns SDK matched) with `[FILL_DETECT_TRACE] step=phantom_lag_tolerated` log. Past the tolerance window, FX-037 behaviour preserved (true phantom won't update balance even after 60s). Legacy callers that don't pass `slot` get the old FX-037 semantics — backwards-compatible.
  - **F3 — Drift catch-up sweep (`order_lifecycle.py:_reconcile_balance_drift` + `detect_fills`).** End-of-cycle sweep targeting `(cid, side) ∈ cids_processed - primary_handled` — markets whose order disappeared this cycle but the primary path didn't record a fill (phantom_zeroed branch + UNKNOWN-with-no-surplus branch + UNKNOWN-below-threshold branch). For each, queries on-chain CTF balance and compares to tracked shares; if `on_chain - tracked >= 1.0`, writes a synthetic catch-up fill with `fill_event_id = "drift:{cid}:{side}:{int(now / DRIFT_DEDUP_BUCKET_SEC)}"` (5-min bucket dedup via the F1 partial unique index). Bounded API cost: 1 RPC per missed-detection per cycle, not per market. New `[RECONCILE_DRIFT]` log line + tracks `step` ∈ {catching_up, balance_probe_failed, sweep_exception}.
- **Failure modes (all fail-open or fail-safe):**
  - Drift sweep RPC failure → log warning, sweep continues for other markets (no crash, no fill loss — the next cycle's sweep retries)
  - Phantom check API failure → unchanged from FX-037 (fail-OPEN, returns SDK matched)
  - log_fill DB error → returns False, [FILL_WRITE] FAILED logged at ERROR level — operator-visible, not silent
  - Drift sweep on a stale slot whose order_id was cleared → coerced to '' (defensive, F1 catches it before INSERT)
- **Verification (227 tests pass; 0 regressions):**
  - **14 new adversarial tests** in `tests/test_audit_fill_detection.py` across 4 attack families:
    - **FD-A (Idempotency × 5):** same event_id collapses to 1 row; distinct event_ids both persist; empty event_id keeps legacy append-only; log_fill returns False on DB error; [FILL_WRITE] log records actual DB outcome (not lying-succeeded).
    - **FD-B (Balance-lag tolerance × 4):** recent order + 0 on-chain trusts SDK; old order + 0 on-chain treats as phantom; no-slot defaults to FX-037 behaviour; partial on-chain delta within lag window still applies FX-037 (the bypass is specifically for actual_delta==0).
    - **FD-C (Drift catch-up × 5):** phantom-zeroed triggers drift catchup; happy path skips drift sweep (no double-count); drift sweep idempotent via 5-min bucket; drift < 1 share no catchup; RPC failure in sweep doesn't crash detect_fills.
    - **FD-D (Stacked-failure invariants × 4):** network timeout + drift catchup recovers immediately (better than legacy wait-for-UNKNOWN-threshold); burst of 3 fills all persist; retry after silent fail does not duplicate; invariant `fills_count >= on_chain_BUY_count` holds for both balance-lag scenario (F2 path) and phantom-zeroed scenario (F3 path).
  - **3 existing FX-037 phantom integration tests updated:** set `placed_at` 120s in the past so the FX-054 lag tolerance doesn't trip the FX-037 contract; mock `positions.record_fill` to bump `get_shares` so the drift sweep doesn't see false drift.
  - **The audit caught a real bug in my own fix:** the drift sweep passed `slot.order_id` (which was None — primary path cleared it after phantom_zeroed) into the `fills.order_id` TEXT NOT NULL column. INSERT OR IGNORE silently swallowed the constraint violation, no row was inserted, and the test surfaced this via the new `[FILL_WRITE] FAILED` log line (which itself was a feature of the F1 fix, validating that the instrumentation works as designed). Closed by adding defensive `None → ''` coercion both in `log_fill` (catch-all) and at the drift sweep call site (explicit, documents the cause for future readers).
  - **Adjacent regression sweep:** 127 tests pass across test_simple_allocator, test_simple_oversight, test_capital_flow, test_wallet_reconciliation, test_dump_manager_fee, test_oversight_shadow, test_shutdown.
- **What's intentionally NOT in this commit:**
  - **Faster `detect_fills` cycle interval (Hypothesis 1).** The drift catch-up sweep makes this unnecessary — fills are caught at end of the same cycle they appear in, regardless of how long the cycle takes. Lowering the cycle interval is a heavier change with broader blast radius (API rate limits, log volume, kill-switch threshold recalibration).
  - **Schema change to make order_id NULLable.** The defensive `None → ''` coercion in F1 + the explicit coercion at the drift sweep call site is strictly safer than allowing NULLs through the column — it surfaces caller bugs as observable empty-string rows rather than silently-missing rows.
  - **Eager balance-reconciliation per market per cycle.** The targeted sweep `(cids_processed - primary_handled)` is the right trade-off: bounded API cost (typically 0 RPC/cycle on healthy operation, ≤ N when N orders disappeared this cycle), and fires precisely on the failure surfaces (phantom_zeroed, UNKNOWN-below-threshold).
- **Risk profile:**
  - **Reversibility (P2):** Single revert restores the pre-FX-054 path. New columns stay (additive migration, safe). Index stays. Partial unique index never blocks empty event_id inserts (legacy behaviour preserved). To disable just F3 without reverting code: set `cids_processed = set()` at top of detect_fills (skips sweep, no RPC, no catchups).
  - **Single-axis (P3):** All 3 axes serve one goal (close the under-counting gap). Test file + code + migration land together because they're contracted as a unit — F1 alone wouldn't catch B's failure shape; F2 alone wouldn't catch A's; F3 alone wouldn't catch retries. Schema migration is additive + idempotent (CREATE INDEX IF NOT EXISTS, ALTER TABLE ADD COLUMN inside try/except).
  - **API cost amplification:** at the operating point (<1 fill/day target rate), drift sweep fires 0 RPC/cycle in steady state. Worst case: bursty failure mode where many orders disappear without primary handle_fill → N RPC/cycle where N = primary-path-missed count. Even at 100 such missed-detections in one cycle that's 100 RPC vs the rate limit budget — fine.
  - **Production verification still recommended:** the fix is sound by adversarial audit + invariant tests, but the 2026-05-25 incident shape (8/9 missed) was a real-world event we never reproduced with full instrumentation. First operational run should validate `fills_count_in_DB ≈ on_chain_BUY_count_data_api`. The `[FILL_DETECT_TRACE]` + `[FILL_WRITE]` + new `[RECONCILE_DRIFT]` log lines together provide the diagnostic trail.
- **Related:** FX-037 (BUY-side phantom defence — F2 extends it with lag tolerance), FX-049 (wallet reconciler — independent backstop catching cash drift), FX-051 (ROI tracker — now has accurate fill input for cooldown decisions), FX-057 (cooldown threshold audit — built on top of FX-051 and now backed by accurate fills).
- **History:**
  - 2026-05-26 — Opened. Instrumentation shipped in `bd5a54e` (4 trace branches in detect_fills, [FILL_WRITE] brackets in handle_fill). Root cause investigation deferred to production trace.
  - 2026-05-28 — Three-axis fix designed defensively against the 4 listed hypotheses + the 2 newly-identified root causes (A + B). 14 adversarial tests written; 4 failed initially; audit caught the slot.order_id=None silent-drop bug in F3; fixed via defensive coercion in log_fill + explicit coercion at the sweep call site. 227 tests pass, 0 regressions.

---

### FX-057 — FX-051 cooldown thresholds + lifecycle gaps surfaced by adversarial audit [FIXED]

- **Severity:** High (Ground Rule 3 — auto-correction must actually fire on losing markets at the realistic operating regime)
- **Status:** Fixed (pending commit, 2026-05-27)
- **Tags:** `[ARCH]` `[BUG]`
- **Opened:** 2026-05-27 (adversarial breakage review of FX-051 anticipated by 2026-05-26 doc amendment)
- **Closed:** 2026-05-27
- **Principle that surfaced it:** P4 (production cycles > tests). The 65 contract tests shipped with FX-051 all asserted the documented behaviour but didn't attack the calibration boundaries. The adversarial audit asks "what scenarios make the system fail to act when it should?" — different shape from "does the system do what we said."
- **Symptom:** Two attack families, 7 concrete scenarios, all reproduced as failing tests in `tests/test_audit_cooldown_logic.py`:
  - **CS-1 (cold-start trap, HIGH):** Single fill with $1.50 loss never cools. samples=1 < 3 inhibits the roi-trigger; fill_loss=$1.50 < $2 inhibits fast-path. At ground-rules-target rate of <1 fill/day this is the typical regime — 50 such markets bleed $75/day with no auto-correction.
  - **CS-2 (cold-start, HIGH):** 5 × $0.39 = $1.95 cumulative loss, samples=5 (gate cleared) but ROI=-3.9% (above -5% threshold). Neither trigger fires despite zero reward + clear bleed pattern.
  - **CS-3 (cold-start, MEDIUM):** Market with fills but no capital_committed_snapshots row gets ROI computed as `(0 - $1) / max(0, 0.01) = -100`. Alarming telemetry that misleads operator triage; no false-cooling but also no signal.
  - **CS-4 (cold-start, MEDIUM):** 1h window with a single snapshot at minute 59 yields capital_avg=$0.83 instead of $50 (pre-snapshot interval unattributed). Spurious cooldowns on healthy markets when the allocator timing happens to put both snapshots late in window.
  - **CG-1 (cooldown gaming, HIGH):** After cooldown expires, if fresh ROI is STILL bad, `evaluate_market` returns `action='reactivate'` (allowed into next alloc) instead of re-cooling. The market re-fills during the farmer cycle, takes another loss, and the next oversight cycle re-cools it. Bleed window of ~30 min × N persistent losers per day.
  - **CG-2 (cooldown gaming, HIGH):** Market with $1.99/fill, 1 fill/day for 7 days never cools by either trigger. Adversarial-market or unlucky-distribution exploit: keep loss-per-fill an epsilon under $2 to farm losses out of the bot indefinitely.
  - **CG-3 (cooldown gaming, MEDIUM):** Single fill at 100% loss of a small position ($1.95 cost → sold for $0, pnl=-$1.95) doesn't cool because absolute USD is under $2. Sizing-threshold exploit.
- **Root cause (single-source):** v1 FX-051 thresholds were calibrated against the 2026-05-25 incident ($2.13 single-fill loss) — they catch THAT pattern but assume per-market notional and fill rates that don't hold in the ground-rules-target regime (50–200 markets, <1 fill/day each, $10–$50 per-market notional under overcommit). Sample gate of 3 is structurally unreachable within the 24h ROI window at <1 fill/day. Absolute $2 fast-path is 4-20% of per-market notional, leaving the $0–$2 band uncovered. Two orthogonal logic gaps: (a) `evaluate_market` reactivates on expired-but-still-bad rather than re-cooling; (b) `_capital_committed_avg` ignores capital state from before the window.
- **Fix applied (single commit):**
  - **`decision_policy.py`:** `ABS_LOSS_FAST_COOLDOWN_USD: 2.0 → 1.0` (~2-5% of per-market notional under overcommit, the right scale). `ROI_COOLDOWN_MIN_SAMPLES: 3 → 1` (consistent with <1 fill/day target rate). New helper `_is_roi_bad(roi)` becomes the single source of truth for trigger evaluation, used both by the first-cool path and by the new expired-but-still-bad re-cool path in `evaluate_market`. On expired cooldown, if `_is_roi_bad(fresh_roi)` returns True, the row is replaced with a new cooldown_until (action='cool_down') rather than deleted (action='reactivate').
  - **`market_roi_tracker.py`:** New constant `CAPITAL_AVG_MIN_FOR_ROI = 0.10`. In `tick()`, when `capital_avg < CAPITAL_AVG_MIN_FOR_ROI`, ROI is set to 0.0 (treated as "no signal") instead of dividing by the 0.01 floor — eliminates the -100 telemetry corruption. `_capital_committed_avg` now also queries the latest snapshot BEFORE `since_ts` and uses it as the initial value for the segment from window_start to the first in-window snapshot. When no prior snapshot exists, extrapolates the first in-window snapshot backwards (acceptable approximation for the small window-start-to-first-snapshot interval). Decision policy still consults `fill_loss` directly so cooldown decisions are unaffected by the ROI cleanup.
  - **`tests/test_decision_policy.py`:** `test_P3_bad_roi_with_too_few_samples_no_cooldown` updated to reflect new sample gate (asserts samples=0 → no cool, was samples=2 → no cool). All other tests unchanged.
  - **`tests/test_audit_cooldown_logic.py` (NEW, ~430 LOC):** 7 adversarial tests (CS-1 through CG-3) asserting the DESIRED post-fix behaviour. Each test docstring states scenario, severity, why-it-matters under ground rules, and what "fixed" means. Tests pass after the 5 fixes above.
- **Failure modes (all fail-open, unchanged from FX-051):**
  - Tracker exception → log warn + empty excluded set; allocator runs as before
  - Reward API failure → reward_earned = 0; biases toward cooldown (strictly safer)
  - Cooldown table corrupted → `get_excluded_cids()` returns `{}`
- **Verification:**
  - **7 new adversarial tests** all pass after the fix.
  - **13 existing decision_policy tests** (P1-P13) all pass with the one updated for the new sample gate.
  - **16 existing market_roi_tracker tests** (R1-R16) all pass — the `_capital_committed_avg` math change doesn't break R7's time-weighted-average test or R14's formula test (both verified by re-tracing the new algorithm).
  - **Full 72-test sweep across FX-051 + adjacent suites** (test_audit_cooldown_logic + test_decision_policy + test_market_roi_tracker + test_simple_allocator + test_simple_oversight) passes in 8.7s.
  - **Adjacent regression sweep** (test_oversight_shadow + test_capital_flow + test_database_persistence + test_dump_manager_fee + test_wallet_reconciliation) 83/83 pass — no unintended interactions outside the FX-051 surface.
- **What's intentionally NOT in this commit:**
  - **Per-fill loss-ratio trigger.** Was considered (catches `pnl/vwap_cost < -50%` regardless of absolute amount) but at the realistic operating point ($10–$50 per-market notional under overcommit), C1's $1 fast-path catches every catastrophic-relative-loss case CG-3 represents. Re-evaluate if wallet scales up significantly.
  - **7d cumulative trigger.** Not needed — C1 + C2 retunes already catch the multi-day slow-bleed (CG-2 verified across 7 simulated days).
  - **Schema change.** No new columns added; everything fits the existing market_roi / capital_committed_snapshots tables.
- **Risk profile:**
  - **Reversibility (P2):** Single revert restores FX-051 v1 thresholds. The new audit-test file documents the regressions that re-appear if the change is undone — operators can see exactly what they're trading off.
  - **Single-axis (P3):** All 5 changes address one concern (FX-051 cooldown gap closure). Test file + code + tuning constants land together because they're contracted as a unit.
  - **False-positive risk:** Lowering thresholds means more cooldowns. But the cooldown is 24h-reversible, samples=1 still requires ≥1 fill (no cooldown on zero-activity markets), and the fast-path at $1 is still well above per-cycle scoring-snapshot noise. The blast-radius bound is "more markets in cooldown for 24h" — which is fine under Rule 1 because cooled markets re-evaluate on every cycle.
  - **Calibration trade-off worth flagging:** Lowering ABS_LOSS_FAST_COOLDOWN_USD to $1 means a single-fill loss of $1 cools the market for 24h. At per-market notional $50, that's a 2% per-fill loss → cool. This is intentional under ground rules — we'd rather miss 24h of reward on a market that just took a 2% loss than keep deploying. If production shows over-cooling (e.g., 50% of fill events cool the market), raise to $1.50 via constructor param without redeploying code.
- **Related:** FX-051 (parent — this closes its calibration gaps), FX-054 (still pending — without accurate fill detection, the tracker can't see the unwinds that drive cooldowns anyway; FX-057 unblocks the LOGIC layer but FX-054 is required for end-to-end correctness in production).
- **History:**
  - 2026-05-27 — Audit ran adversarial attack on cold-start trap + cooldown gaming; 7 failing tests written; 5 targeted fixes implemented; 72 tests pass; doc updated; commit prepared.

---

### FX-051 — Per-market ROI tracker + cooldown decision policy [FIXED]

- **Severity:** Critical (Ground Rule 3 — mandatory auto-correction loop)
- **Status:** Fixed (commit `e4f2ee3`, 2026-05-26)
- **Tags:** `[ARCH]` `[BUG]` `[SAFETY]`
- **Opened:** 2026-05-26 (surfaced by 2026-05-25 12:05 UTC kill-switch event)
- **Closed:** 2026-05-26
- **Symptom:** SimpleAllocator's allocation logic ranked markets by `daily_rate × q_share` only. No input from `unwinds.pnl` per market. Bot re-deployed on markets that just produced losses. Verified: market `0x46c09232d356fdbe` produced a $2.13 loss at 08:46 UTC on 2026-05-25, then was STILL listed in the alloc file at 09:39 UTC. Ground Rule 3 requires mandatory auto-correction; SimpleAllocator violated it.
- **Decision on D8 (resurrect vs rebuild):** Investigation confirmed `calibration/loss_model.py` (325 LOC) and `profit/bandit.py` (254 LOC) are dormant on disk (imported only by old `oversight_agent.py`, not by `simple_oversight.py`). LossModel computes per-share loss prediction (wrong granularity for per-market ROI). Bandit conflates reward + loss into a binary success signal (wrong shape). Verdict: **build fresh**, with Bandit's 24h-window SQL query pattern as a reference template.
- **Fix applied (commit `e4f2ee3`):**
  - **4 new DB tables in `database.py`:** `market_roi` (per-cid, per-window snapshots); `capital_committed_snapshots` (one row per cycle per deploy, time-integrated for capital_committed_avg); `market_cooldowns` (active cooldown rows; cooldown_until > now means excluded); `daily_reward_cache` (per-date per-cid cache for /rewards/user/markets API, refreshed each cycle).
  - **`market_roi_tracker.py` (~470 LOC):** Data layer. `tick()` recomputes 1h/24h/7d snapshots for every market with recent activity. `snapshot_capital(alloc_result)` records per-market est_capital_cost rows after each cycle. `prune_old_snapshots()` deletes capital snapshots older than 14d. `get_roi(cid, window)` / `get_all_for_window(window)` / `get_global_summary(window)` for reads. HTTP injected via `_http` constructor parameter so tests can stub.
  - **`decision_policy.py` (~280 LOC):** Consumer layer. `evaluate_market(cid)` returns `MarketDecision` with action ∈ {allow, cool_down, still_cooled, reactivate}. Cooldown triggers: (a) `roi_24h < -5%` AND `samples ≥ 3` OR (b) `fill_loss_24h ≥ $2` single-event fast path (the 2026-05-25 incident was $2.13 from one fill — wait-for-3-samples would have re-allowed it). Cooldown duration: 24h. Emits structured `[LEARN]` log per ground_rules.md.
  - **`simple_allocator.py`:** `compute()` gains `excluded_cids: Optional[set[str]] = None` parameter. Eligible filter adds `and m.condition_id not in excluded`. Empty set / None is pass-through (backwards-compatible).
  - **`simple_oversight.py`:** `run_once()` ticks the tracker, evaluates the policy, passes `excluded_cids` to allocator, snapshots capital after the alloc cycle. All in a fail-open try/except — any exception logs `[LEARN]` warning and the allocator gets an empty exclusion set (equivalent to pre-FX-051 behaviour).
- **Failure modes (all fail-open per architecture doc §0.1 P2):**
  - Tracker exception → log warn + empty excluded set, allocator runs as before
  - /rewards/user/markets API failure → reward_earned = 0 for that window, biases toward cooldown (strictly safer)
  - Cooldown table corrupted → `get_excluded_cids()` returns `{}`
- **Verification:**
  - **29 new contract tests** in `tests/test_market_roi_tracker.py` (R1-R16, 16 tests) + `tests/test_decision_policy.py` (P1-P13, 13 tests).
  - **3 new allocator contract tests** (C21-C23) in `tests/test_simple_allocator.py` covering the `excluded_cids` parameter.
  - **1 end-to-end integration test** (`test_O12`) in `tests/test_simple_oversight.py` that seeds a cooldown row, runs `run_once`, captures the kwargs passed to `allocator.compute`, and asserts the cooled cid is in `excluded_cids`. Locks in the full simple_oversight → tracker → policy → allocator wiring.
  - **65/65 fast-tier tests pass** in 3.39s after the change.
- **What's intentionally NOT in this commit (Phase 3 scope):**
  - Per-market notional resizing (Phase 3 OverCommitAllocator)
  - Farmer-side kill-threshold retune (Phase 3 prereq — see fixit §2 row for FX-052/053)
  - Automatic adjustment of `MIN_EXPECTED_PER_MARKET`, `MAX_DEPLOYED_MARKETS` (Phase 3)
- **Risk profile:**
  - Reversibility (P2): pure-additive; `excluded_cids` defaults to None so disabling = passing `{}` or removing the tracker call. New tables don't affect existing queries.
  - Single-axis (P3): all changes are part of "make the auto-correction loop work end-to-end" — one observable hypothesis: "markets with bad ROI get cooled and excluded".
  - Production-cycle risk: bot is halted; no production impact yet. Next step is adversarial review.
- **Operator action pending:** adversarial review (try to break the system) → fixes → re-verify → unhalt as a shadow dry-run → live.
- **Related:** Ground Rule 3 (the contract); FX-054 (the ROI tracker reads from `fills` + `unwinds`, so fill-detection accuracy is upstream); FX-056 (extreme-price filter is the other half of structural loss defense, complementary not redundant); FX-052/053 (Phase 3 OverCommitAllocator will consume the same tracker for sizing decisions).
- **History:**
  - 2026-05-26 — Opened (during v6.0 status deep-dive). Shipped same day as Phase 2 of the v6.0 plan.

---

### FX-039 — `handle_fill` hardcoded `fill_type='FULL'` [FIXED]

- **Severity:** Low (cosmetic labelling bug; surfaced an unrelated latent crash)
- **Status:** Fixed (commit `9164f1f`, 2026-05-26)
- **Tags:** `[BUG]`
- **Opened:** 2026-05-20 (noticed during FX-037 investigation)
- **Closed:** 2026-05-26
- **Symptom:** `order_lifecycle.handle_fill` called `self.db.log_fill(..., fill_type="FULL", ...)` unconditionally. `detect_fills` and `_check_stale_order` both computed the correct `fill_type` (PARTIAL vs FULL) but never passed it down. The numerical columns were correct; only the `fill_type` column was misleadingly labelled. Calibration features that train on the label set would inherit the corruption.
- **Fix applied (commit `9164f1f`):**
  - Added `fill_type: str = "FULL"` parameter to `handle_fill`. Threaded through to both `db.log_fill` and `alert_fill` calls.
  - Three call sites updated: `detect_fills` (passes computed PARTIAL/FULL), `_check_stale_order` (passes computed PARTIAL/FULL), `_reconcile_after_unknown` (keeps default `"FULL"` — the caller discovered the position from balance probe, has no SDK-reported matched size to distinguish).
- **Latent bug surfaced + fixed in the same commit:** `alerts.py:322` formats `remaining_shares:.2f` inside the PARTIAL branch without a `None` guard. Pre-FX-039 the hardcoded `"FULL"` made the PARTIAL branch unreachable in production. After the fix, `test_stale_order_partial_fill_detected` crashed with `TypeError: unsupported format string passed to NoneType.__format__`. The handle_fill caller now passes `remaining_shares = max(0.0, slot.shares - filled_shares)` so the PARTIAL alert works.
- **Verification:**
  - All 58 tests in `test_order_lifecycle.py` + `test_order_ttl.py` + `test_order_reconciliation.py` + `test_fill_rate_breaker.py` pass.
  - `test_stale_order_partial_fill_detected` now meaningfully exercises the partial-fill alert path that was previously dead.
- **Risk profile:** None. Pure correctness fix on a label that downstream consumers (calibration) wanted right.
- **Related:** FX-054 (instrumentation in same file, shipped same day as `bd5a54e`).
- **History:**
  - 2026-05-20 — Opened (noticed during FX-037 investigation).
  - 2026-05-26 — Shipped `9164f1f` after FX-054 instrumentation surfaced the file as a hotspot for inspection.

---

### FX-054 instrumentation — fill-detection trace logging [IN PROGRESS]

- **Severity:** Critical (blocks Ground Rule 3); investigation phase
- **Status:** Instrumentation shipped (commit `bd5a54e`, 2026-05-26); root-cause fix pending production trace capture
- **Tags:** `[BUG]` `[ARCH]`
- **Symptom:** 2026-05-25 had 9 BUY trades on data-api / activity but only 1 row in the `fills` DB table. 8 of 9 fills silently missing — invisible to FX-049 reconciliation, I7 hourly_loss, future ROI tracker.
- **Hypotheses (pre-trace):**
  1. `client.get_order` exception in `detect_fills` → `matched=0` → fill missed silently (the prime suspect; exception path was previously `log.debug` and invisible in default journals).
  2. Re-place loop overwrites `slot.order_id` before fill is detected on the previous order.
  3. `handle_fill` raises inside `db.log_fill` → fill logged but not written.
  4. Network timeouts at 09:05-09:07 UTC (4 errors observed) compounded with phantom-defense's extra `get_balance_allowance` API hop.
- **Instrumentation applied (commit `bd5a54e`):**
  - `[FILL_DETECT_TRACE]` log lines at every branch in `detect_fills`: `missing_from_open_ids`, `sdk_resp`, `sdk_exception` (upgraded to WARNING — was DEBUG and invisible), `phantom_adjusted`, `fill_recorded`, `phantom_zeroed`, `unknown_status`.
  - `[FILL_WRITE]` brackets around `db.log_fill` in `handle_fill`: `attempting` + `succeeded`. An `attempting` without a matching `succeeded` is the smoking gun for hypothesis 3.
- **Next steps (root-cause fix, not yet shipped):**
  1. Reproduction: script that places 5 paired orders and triggers fills via taker; verify 10 rows in `fills` within 30 s.
  2. Idempotent `handle_fill` keyed on `(order_id, ts_minute)` if duplicate detection becomes the cause.
  3. Bounded retry around `client.get_order` if exception swallow is confirmed.
  4. Catch-up queue for fills missed during network outage if hypothesis 4 confirmed.
- **Verification:** 32 tests in `tests/test_order_lifecycle.py` still pass post-instrumentation.
- **Related:** FX-051 (depends on this for ROI input), FX-055 (FX-049 reconciler — works correctly once fills are accurately recorded), FX-039 (fixed in the same file 2026-05-26).
- **History:**
  - 2026-05-26 — Instrumentation shipped (`bd5a54e`). Root-cause fix pending production trace capture.

---

### FX-055 — Re-wire FX-049 wallet reconciliation into simple_oversight [FIXED]

- **Severity:** High (regression introduced by SimpleAllocator swap)
- **Status:** Fixed (commit `3704cd7`, 2026-05-26)
- **Tags:** `[BUG]` `[SAFETY]`
- **Opened:** 2026-05-26 (identified during v6.0 status deep-dive)
- **Closed:** 2026-05-26
- **Symptom:** When `simple_oversight.py` replaced `oversight_agent.py` as the systemd ExecStart (commit `0fafa1b`), `reconcile_wallet_invariant` was dropped. `wallet_reconcile_history` had no rows since 2026-05-25 08:20 UTC. All cash-accounting drift during the SimpleAllocator window was unobserved by FX-049's permanent backstop.
- **Fix applied (commit `3704cd7`):**
  - Added the same `reconcile_wallet_invariant` call to `simple_oversight.run_once()` between wallet probe + history reads and allocator compute. Mirrors the `oversight_agent.run_once()` integration site.
  - Outer try/except keeps the reconciler non-blocking — any exception logs at WARNING and the cycle continues unaffected (matches FX-049's fail-open contract).
  - Imports done lazily inside the try block so test environments that don't have `database.get_db` configured don't break the oversight import surface.
- **Verification:** 22 tests in `tests/test_simple_oversight.py` + `tests/test_wallet_reconciliation.py` pass. The fail-open exception handler covers the test-mock surface so existing `run_once` tests don't need updating.
- **Production verification path:** Next `simple_oversight` cycle writes a fresh `'baseline'` row to `wallet_reconcile_history`. Operator monitors with: `journalctl -f -u polymarket-oversight \| grep -E "WALLET_RECONCILE\|WALLET_DESYNC"` and `sqlite3 bot_history.db "SELECT * FROM wallet_reconcile_history ORDER BY ts DESC LIMIT 5;"`
- **Risk profile:** Trivial. Reversible by removing the try block. The reconciler is observational, never gates allocation.
- **Related:** FX-049 (the original reconciler), FX-054 (the reconciler input is only accurate once fill detection is fixed — but the framework should be back in place either way).
- **History:**
  - 2026-05-25 — Regression introduced unwittingly in commit `0fafa1b`.
  - 2026-05-26 — Identified during status deep-dive + shipped same day.

---

### FX-056 — Extreme-price filter in SimpleAllocator [FIXED]

- **Severity:** Medium (loss-defender against 13.3%-slippage class)
- **Status:** Fixed (commit `80bd299`, 2026-05-26)
- **Tags:** `[ARCH]`
- **Opened:** 2026-05-26
- **Closed:** 2026-05-26
- **Symptom:** 2026-05-25 08:46 UTC fill on `0x46c09232d356fdbe` (NO at $0.08, dumped at $0.07) took 13.3% slippage on dump vs 1-2% on mid-priced markets. The high `daily_rate` of extreme-price markets ($500-$5000/day on Iran-class) attracts SimpleAllocator's `daily_rate × q_share` ranker, but per-fill cost negates per-cycle reward.
- **Fix applied (commit `80bd299`):**
  - New constants `EXTREME_PRICE_LOW = 0.10` and `EXTREME_PRICE_HIGH = 0.90` in `simple_allocator.py`.
  - `fetch_reward_markets` now attempts to extract a midpoint hint from `tokens[0].price` in the API response (same field shape that `market_discovery` and `reward_farmer` use against `/markets/{cid}`). If the field is absent (the `/rewards/markets/current` schema doesn't guarantee `tokens`), `midpoint_guess` keeps its dataclass default of 0.5.
  - The eligible filter rejects markets where `midpoint_guess` is outside `[0.10, 0.90]`. Markets with the default 0.5 (no API price hint) pass through fail-open — a follow-up filter at farmer-side book-fetch time will catch any that slip through.
- **Verification:** 5 new contract tests in `tests/test_simple_allocator.py`:
  - C16: `midpoint < 0.10` filtered out
  - C17: `midpoint > 0.90` filtered out
  - C18: mid-priced (and default 0.5) markets pass through (fail-open contract)
  - C19: `fetch_reward_markets` extracts `tokens[0].price` when API returns it
  - C20: `fetch_reward_markets` defaults to 0.5 when tokens field absent
- **Acceptance criterion:** ✓ A `midpoint=$0.05` market is in `result.avoids` not `result.deploys`.
- **Risk profile:** Low. Filter, fully reversible. Markets without midpoint data fail open — strictly safer than fail-closed (which would blackhole every API response missing tokens).
- **Related:** FX-051 (the loss-feedback ROI tracker would penalize these eventually, but the structural filter is faster).
- **History:**
  - 2026-05-26 — Opened + shipped same day as part of v6.0 Phase 1 quick-wins.

---

### FX-049 — Wallet-invariant reconciliation (defense-in-depth backstop) [FIXED]

- **Severity:** High (permanent safety invariant; catches future cash-accounting drift even if root cause is unknown)
- **Status:** Fixed (commit `06d8406`, v5.1.22, 2026-05-24)
- **Tags:** `[ARCH]` `[SAFETY]`
- **Opened:** 2026-05-23 (during FX-050 investigation; need permanent backstop in case future unknown unknowns cause drift)
- **Closed:** 2026-05-24
- **Symptom:** Bot's loss-accounting accuracy was unmeasurable without an automatic check. Operator-confirmed wallet delta vs bot-recorded pnl mismatch (the 2026-05-22 −$1.34 vs −$1.00 incident, FX-050) was caught manually after the fact. No automatic alerting; future similar gaps could persist unnoticed.
- **Fix applied (commit `06d8406`):**
  - New table `wallet_reconcile_history` in `database.py` (full audit trail: `actual_wallet`, `expected_wallet`, `divergence`, `status` ∈ `{baseline, ok, desync, fail_open}`, per-source deltas).
  - New helper methods in `BotDatabase`: `load_latest_wallet_reconcile()`, `insert_wallet_reconcile(...)`, `sum_fills_usd_since(ts)`, `sum_unwinds_usd_since(ts)`.
  - New module `oversight/wallet_reconciliation.py` with `reconcile_wallet_invariant(db, actual_wallet_now, funder, threshold_usd, *, _fetch_rewards_fn, _now_fn)`. Dependency injection for testability.
  - Algorithm: on first run, snapshot baseline (no comparison). On subsequent runs, compute `expected_delta = unwinds_delta − fills_delta + rewards_delta` (rewards from `data-api/activity?type=REWARD&MAKER_REBATE` since baseline_ts); `actual_delta = actual_wallet_now − baseline_wallet`; `divergence = actual_delta − expected_delta`. If `|divergence| > RF_WALLET_DESYNC_THRESHOLD_USD = $0.50` → `[CRITICAL] WALLET_DESYNC` log with all signals; else `[WALLET_RECONCILE] ok`.
  - Fail-OPEN on reward-fetch failure: writes `status='fail_open'` row + `log.warning`; no CRITICAL emitted (a transient data-api blip shouldn't kill the bot or false-alarm the operator).
  - Incremental (rolling window): each cycle resets baseline to `(now, actual_wallet_now)`. Divergences observed once, not double-counted.
  - Wired into `oversight_agent.run_once()` after capital resolution, before metric collection. Outer try/except ensures reconciler failure can't kill the agent loop.
- **Commit:** `06d8406` on `main`. P3 single-axis override per operator authorization (bundled with FX-050; both belong to loss-accounting integrity).
- **Diff size:** New table schema + 4 DB helpers (~80 lines `database.py`); new 220-LOC `oversight/wallet_reconciliation.py`; ~25-line integration in `oversight_agent.py`; +10 lines tests/test_wallet_reconciliation.py imports + 10 contract tests (~230 LOC).
- **Verification:**
  - **Unit:** 10 new tests in `tests/test_wallet_reconciliation.py`:
    - C1: first run → 'baseline' row, no alert, no data-api fetch
    - C2a: zero divergence → 'ok' row, no CRITICAL
    - C2b: $0.34 fee drift within $0.50 threshold → 'ok'
    - C3a: +$10 unexplained → 'desync' + CRITICAL emitted
    - C3b: −$10 phantom unwind → also 'desync' (sign-agnostic threshold)
    - C4: data-api failure → 'fail_open' row, log.warning, no CRITICAL
    - C5a: 'ok' cycle advances baseline (verifies incremental window)
    - C5b: 'desync' cycle also advances baseline (no double-counting across cycles)
    - C6a: reward inflow correctly explains wallet growth (no false-positive)
    - C6b: unexplained inflow without reward → 'desync' (edge of C3)
  - **Integration:** wired into `oversight_agent.run_once()`; outer try/except verified by inspection.
  - **CI:** run 26350996533 passed 785/785 in 5m46s on Ubuntu 24.04 + Python 3.14.
- **Acceptance criterion:** ✓ Any cash-accounting drift > $0.50/cycle emits `[CRITICAL] WALLET_DESYNC` for operator visibility, regardless of root cause.
- **Risk profile:**
  - Reversibility (P2): single-commit revert; the table + module just sit unused. Or set `RF_WALLET_DESYNC_THRESHOLD_USD = 999999` via `config_overrides.json` to silence alerts without removing instrumentation.
  - Single-axis (P3): operator-authorized bundle with FX-050. Both are "loss-accounting integrity" — FX-050 fixes the symptom, FX-049 catches future drift the formula doesn't predict.
  - False-positive risk: if rewards arrive between cycles but data-api hasn't published yet, divergence might falsely flag. Mitigation: fail-OPEN on fetch errors; threshold $0.50 absorbs single-trade noise.
- **Production verification path (after Helsinki pull):** First agent cycle (~30 min after restart) writes a `'baseline'` row in `wallet_reconcile_history`. Subsequent cycles do the comparison. Operator monitors with:
  ```
  journalctl -f -u polymarket-oversight | grep -E "WALLET_RECONCILE|WALLET_DESYNC"
  sqlite3 bot_history.db "SELECT * FROM wallet_reconcile_history ORDER BY ts DESC LIMIT 5;"
  ```
- **Related:** FX-050 (the specific symptom this reconciler was first designed to catch; both fixes shipped together), FX-037 (BUY-side phantom defense — orthogonal but in same "fill integrity" family).
- **Hardening Phase:** Phase A of Master Plan (loss-accounting integrity restoration before Phase B q_share unfreeze).
- **History:**
  - 2026-05-23 — Investigation revealed FX-050 (taker fee gap). Opened FX-049 as backstop in same fixit doc update.
  - 2026-05-24 — Shipped in commit `06d8406` bundled with FX-050. CI 26350996533 green.

---

### FX-050 — Polymarket taker fee not captured in DumpManager unwind pnl [FIXED]

- **Severity:** High (silent under-reporting of losses by safety machinery; I7 + 24h-realized-loss kill switch under-fire by ~25-30%)
- **Status:** Fixed (commit `06d8406`, v5.1.22, 2026-05-24)
- **Tags:** `[BUG]` `[SAFETY]`
- **Opened:** 2026-05-23 (surfaced by operator-confirmed wallet delta mismatch)
- **Closed:** 2026-05-24
- **Symptom:** 2026-05-22 dump cycle on `0x0ed3f07970b2d212` (OpenAI $2.0T market, 50 NO shares):
  - Polymarket data-api activity: BUY paid $40.00, SELL received **$38.6568**, actual wallet delta **−$1.34**
  - Bot DB `unwinds` row: 50 sh @ $0.78 book price, `usd_value = $39.00`, `vwap_cost = $40.00`, `pnl = −$1.00`
  - Gap: $0.34 = 0.88% of $39 gross revenue = Polymarket taker fee
- **Root cause (executed via code reading):** `DumpManager.check_dump_fills` at `dump_manager.py:89` computed `sell_revenue = matched × SDK_price`, where `SDK_price` is the book match price from `client.get_order(dump_oid).price`. This is the order-book level the trade crossed AT, not the post-fee cash credited to the wallet. Polymarket charges ~0.88-0.9% taker fee on orders crossing the spread; DumpManager's passive mode (`dump_manager.py:308-327`) sets dump SELL price to the best opposite-token bid (crosses spread → we are taker → fee applies).
- **Fix applied (commit `06d8406`):**
  - New config knob `RF_POLYMARKET_TAKER_FEE: float = 0.009` in `config.py` (default 0.9%; hot-reloadable via `config_overrides.json`; `0` reverts to pre-fix over-reporting behaviour).
  - `dump_manager.py:89` modified: `gross_revenue = matched × price`; `sell_revenue = gross_revenue × (1 − fee)`. The `log_unwind` call receives the post-fee `usd_value`.
  - Updated `[DUMP CONFIRMED]` log line to emit `gross / fee / net / cost / pnl` for operator visibility.
- **Commit:** `06d8406` on `main`. P3 single-axis override per operator authorization (bundled with FX-049).
- **Diff size:** +1 config line + ~15 lines in `dump_manager.py` (formula + log) + 5 contract tests in `tests/test_dump_manager_fee.py` (~220 LOC).
- **Verification:**
  - **Unit:** 5 new tests in `tests/test_dump_manager_fee.py`:
    - C1: default fee 0.009 → `usd_value = matched × price × 0.991`
    - C2: fee=0 escape hatch → byte-identical to pre-FX-050 (`usd_value = matched × price`)
    - C3: higher fee (5%) scales proportionally (tunability)
    - C4: phantom-fill defense (FX-007 SELL-side) still fires regardless of fee value (orthogonal contracts preserved)
    - C5: pnl reflects post-fee revenue — sanity-checked against the 2026-05-22 incident: expected `pnl ≈ −$1.349` (matches actual −$1.34 within $0.01 float rounding)
  - **Regression:** existing `tests/test_critical_fixes.py::TestDumpPhantomFillGuard` still passes (shares-contract unaffected by fee value).
  - **CI:** run 26350996533 passed 785/785 in 5m46s.
- **Acceptance criterion:** ✓ Next dump cycle's `unwinds.usd_value` reflects the cash actually settled to the wallet. I7 hourly_loss + 24h-realized-loss kill switch fire at true loss magnitude, not under-reported.
- **Risk profile:**
  - Reversibility (P2): single-commit revert; or set `RF_POLYMARKET_TAKER_FEE = 0` via `config_overrides.json` (hot-reload, no restart).
  - Single-axis (P3): operator-authorized bundle with FX-049.
  - Calibration assumption: fee is uniformly 0.9% across markets and sizes. The 2026-05-22 incident is N=1 evidence; if Polymarket has tiered fees or market-specific overrides, calibration may be slightly off. FX-049 reconciliation backstop catches residual error.
- **Operator action:** None beyond `git pull + restart`. The fix applies automatically to the NEXT dump event. Historical row from 2026-05-22 (pnl=−$1.00) is NOT backfilled by this commit — operator can manually `UPDATE unwinds SET usd_value=38.6568, pnl=−1.3432 WHERE ts=1779482240 AND condition_id='0x0ed3f07970...'` if retroactive accuracy is needed.
- **Related:** FX-049 (defense-in-depth backstop; bundled in same commit), FX-037 (BUY-side phantom defense; orthogonal — different failure axis), `dump_manager.py:60-87` (FX-007 SELL-side phantom defense; preserved unchanged), `phantom_fill_recovery.md` memory.
- **Hardening Phase:** Phase A of Master Plan (loss-accounting integrity before Phase B q_share unfreeze).
- **History:**
  - 2026-05-22 20:55-20:57 UTC — Incident on Helsinki: 50 NO shares dumped, pnl recorded −$1.00, actual wallet delta −$1.34.
  - 2026-05-23 — Operator flagged the mismatch. Investigation cross-referenced data-api activity vs bot DB; confirmed $0.34 gap = 0.88% Polymarket taker fee.
  - 2026-05-24 — Shipped in commit `06d8406`. CI 26350996533 green.

---

### FX-037 — BUY-side phantom-fill defense [FIXED]

- **Severity:** High (silent state corruption when V2 SDK over-reports size_matched; friend-rollout G2 blocker on the silent-corruption axis)
- **Status:** Fixed (commits `0ec898a` + `a858bb9`, v5.1.21, 2026-05-23)
- **Tags:** `[BUG]` `[SAFETY]`
- **Opened:** 2026-05-20 (surfaced by Iran NO 158→38 incident on 2026-05-19)
- **Closed:** 2026-05-23
- **Symptom:** On 2026-05-19, V2 SDK reported `size_matched=158` for a BUY order that only delivered 38 NO shares on-chain (verified via direct `get_balance_allowance` probe; reproducible signature). The inflated fills row cascaded: I7 hourly_loss → SafetyController demotion to DEGRADED → forced cold-start OpenAI deployments → dump slippage 5-11% → kill switch on `daily_realized_loss = $19.55 > $17.14 (10%·T)`. Bot dead ~3.5h until manual restart.
- **Root cause:** Asymmetric defense between BUY and SELL fill detection paths. SELL-side (`DumpManager.check_dump_fills` lines 60-87, shipped v5.1.9) had a PHANTOM FILL check that queried `get_balance_allowance(CONDITIONAL, token_id)` post-match and refused to record the unwind if on-chain balance still showed the tracked shares. BUY-side (`OrderLifecycle.detect_fills` lines 182-209, pre-fix) trusted SDK's `size_matched` blindly and called `handle_fill(actual_shares=matched)` without on-chain verification.
- **Fix applied (commit `0ec898a`):**
  - New helper `OrderLifecycle._check_buy_phantom_fill(ms, side, matched) → float` in `order_lifecycle.py`.
  - Pattern mirrors `DumpManager.check_dump_fills:60-87`: query `get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid))`, compute `actual_delta = max(0, on_chain - pre_fill_tracked)`, compare against SDK `matched`. If `actual_delta < matched - 0.5` (tolerance window), prefer on-chain truth and emit `log.critical("PHANTOM FILL: SDK size_matched=N but on-chain delta only M | <SIDE> | <question>")` for operator visibility.
  - Call site inserted in `detect_fills` between SDK matched-read and `handle_fill`. `matched <= 0` after the check (full phantom) treats the slot as cleared with no DB write.
  - Fail-OPEN on API exception: SDK value preserved + `log.warning`. Worst case on fail-open: phantom is recorded; orphan-scan + reconciliation catches it next cycle. Worst case on fail-closed: real fill is dropped, position diverges silently → strictly worse.
- **Fix applied part 2 (commit `a858bb9`):** Test-environment hardening. CI run `26329526380` failed 2/770 because pytest alphabetical ordering imports `test_critical_fixes.py` BEFORE `test_order_lifecycle.py`, and the sibling installs MagicMock-based partial mocks at `sys.modules["py_clob_client_v2.clob_types"]` without cleanup. The prior shim's early-return guard didn't distinguish "real SDK" from "stale MagicMock". Fix: three-step protocol — drop stale MagicMock entries first (mirrors `test_placement.py::_drop_stale_clob_mocks`), try fresh real SDK import (succeeds on Helsinki CI), fall back to passthrough dataclass stand-ins. Production code unchanged.
- **Commits:** `0ec898a` (BUY-side defense) + `a858bb9` (test shim hardening) on `main`.
- **Diff size:** +65 / +275 lines in `order_lifecycle.py` + `tests/test_order_lifecycle.py` (commit 1); +86 / -18 lines in `tests/test_order_lifecycle.py` (commit 2).
- **Verification:**
  - **Unit:** 14 new tests in `tests/test_order_lifecycle.py`:
    - `TestCheckBuyPhantomFill` × 11 (unit contracts: phantom detection, honest-SDK passthrough, fail-open, token_id routing per side, negative-delta clamping, log channel)
    - `TestDetectFillsPhantomIntegration` × 3 (end-to-end: SDK reports 158 + chain delivers 38 → handle_fill receives 38; full phantom → no fill recorded; honest SDK → unchanged)
  - **Regression:** existing 120 SDK-touching tests in critical_fixes + sports_protection + placement still pass.
  - **CI:** run 26329526380 caught the test-pollution bug (2/770 fail); run `26329901126` after `a858bb9` shim fix: **770/770 in 5m59s** ✓.
- **Acceptance criterion:** ✓ When SDK reports `size_matched > on_chain_delta`, recorded fill equals on-chain delta with `PHANTOM FILL:` log line; honest SDK → unchanged behaviour; API failure → fail-open with warning.
- **Risk profile:**
  - One extra API call per fill detection (~100ms latency). Rare event on the $227 wallet (~0-1 fills/day).
  - Fail-OPEN preserves SDK on transient errors. Strictly safer than losing legitimate fills.
- **Production status:** Helsinki has zero fills in the observation window post-deploy. The defense is dormant but armed. First phantom that triggers (if any) will produce the canonical log line and the recorded fill will reflect on-chain truth.
- **Related:** FX-007 (SELL-side symmetric defense, v5.1.9), FX-038 (`_reconcile_positions` extends to fills/unwinds — closes the loop on FX-037 so any future phantom that slips through fail-open self-heals; still open), FX-050 (taker fee accounting; bundled with FX-049 in same loss-accounting integrity family).
- **Hardening Phase:** Phase 2 of original campaign (BUY/SELL fill-detection symmetry).
- **History:**
  - 2026-05-19 — Iran NO incident; opened in fixit doc.
  - 2026-05-23 part 1 — Shipped on `main` as `0ec898a` (initial defense + 14 tests).
  - 2026-05-23 part 2 — CI run 26329526380 failed 2/770 due to sibling test pollution; shim hardening shipped as `a858bb9`; CI 26329901126 green 770/770.

---

### FX-041 — Two-sided book-depth check in FX-036 placement [FIXED]

- **Severity:** High (prerequisite for safe re-enable of FX-036)
- **Status:** Fixed (commit `3534cb5`, 2026-05-20)
- **Tags:** `[ARCH]` `[STRATEGY]` `[SAFETY]`
- **Opened:** 2026-05-20 (surfaced by OpenAI thin-market dump cascade)
- **Closed:** 2026-05-20
- **Original symptom:** FX-036 (`_queue_aware_edge` in `order_lifecycle.py`) checked the placement-side queue depth to decide placement location (sit 1 tick behind the level where cumulative queue ≥ `RF_TARGET_QUEUE_AHEAD_USD`). It did NOT check that the opposite merged-book side had enough depth to absorb a dump if filled. In the 2026-05-19 OpenAI cascade, the market had enough bid-side queue to trigger queue-aware placement at 2¢ from mid, but the opposite ASK side was thin — total in-zone ask depth was sub-$1000. When the bot got filled, the dump moved the market against us by ~11.5%, contributing to the $17.63 realized loss on OpenAI cold-start markets.
- **Root cause:** Queue-aware placement was designed for symmetric markets. In production, books can be asymmetric (deep on one side, thin on the other). FX-036's safety logic only catches the symmetric thin case (placement-side queue < target → fall back to legacy); the opposite-side absorbing capacity for a post-fill dump was never measured.
- **Why it matters:** Re-enabling FX-036 in production requires this defense. Without it, the bot will repeat the OpenAI cascade pattern as soon as it encounters another asymmetric book. With FX-041 + FX-040 (cold-start trial sizing) shipped, both the close-to-mid placement and the asymmetric-book trap are closed.
- **Fix applied (commit `3534cb5`):**
  - New config knob `RF_DUMP_DEPTH_SAFETY_FACTOR = 3.0` in `config.py`. `0` disables the check (escape hatch reverts to FX-036-only behaviour).
  - New helper `_has_sufficient_dump_depth(opposite_book_levels, midpoint, max_spread, shares_per_side, dump_price, safety_factor)` in `order_lifecycle.py`. Accumulates `Σ(price × size)` over opposite-side levels within `max_spread` of midpoint. Returns True when cumulative ≥ `shares_per_side × dump_price × safety_factor`, or when disabled (factor ≤ 0).
  - `_compute_edge_prices` gains two new kwargs (`shares_per_side=0`, `dump_depth_safety_factor=0.0` — defaulted so every pre-FX-041 caller stays byte-identical). After each queue-aware result, runs the opposite-side dump-depth check (`merged["asks"]` opposite for "bid" placement, `merged["bids"]` opposite for "ask" placement). If insufficient, that side falls back to legacy zone-edge.
  - Production call site in `place_orders_for_market` passes `ms.agent_shares if ms.agent_shares > 0 else SHARES_PER_SIDE()` and `DUMP_DEPTH_SAFETY_FACTOR()`.
  - Per-side independence preserved: one side falling back doesn't drag the other along — same shape as FX-036's queue-ahead check.
- **Diff size:** +1 line `config.py`, +1 line accessor + ~50 lines new helper + ~25 lines extended `_compute_edge_prices` + 2-line call-site update in `order_lifecycle.py`, ~190 lines new tests in `tests/test_placement.py` (10 helper unit tests + 2 backwards-compat tests + 5 integration tests + 1 end-to-end test).
- **Verification:**
  - **Unit:** new `TestHasSufficientDumpDepth` (10 tests covering escape hatches, empty book, sufficient/insufficient depth, in-zone gating, factor + shares scaling, malformed levels), `TestComputeEdgePricesDumpDepthBackwardsCompat` (2 tests pinning the no-op default behaviour), `TestComputeEdgePricesDumpDepth` (5 tests: Iran no-regression, asymmetric deep-bid-thin-ask, asymmetric thin-bid-deep-ask, factor scaling, in-zone-only accumulation), plus 1 new end-to-end test through `place_orders_for_market`.
  - **Regression:** Full fast tier 737 → **755 pass** (0 regressions). Placement suite alone: 24 → 42 tests in 0.83s.
  - **Iran market (FX-036 motivating scenario) no-regression:** With default factor 3.0 + shares 50 + midpoint 0.485, opposite-side depth threshold is $72.75 against ~$16k of in-zone asks depth → passes easily. FX-036's 3.0× reward density uplift survives.
  - **OpenAI cascade shape regression:** Asymmetric book (deep bids, thin asks in zone) now forces bid-side fall-back to legacy. Verified by `test_deep_bid_thin_ask_forces_bid_side_legacy` and the mirror.
- **Acceptance criterion:** ✓ Regression test with asymmetric book (deep bid, thin ask) — helper returns `None` for the queue-aware bid result (opposite-side check fails), bot places at legacy zone edge. Mirror test for thin-bid-deep-ask.
- **Risk profile:**
  - **Reward yield impact:** Slightly lower on asymmetric books (where FX-036 would have placed close to mid but now falls back). Same as pre-FX-036 behaviour on those markets — no worse than what the bot was doing before FX-036 shipped.
  - **No impact on symmetric deep books:** Iran-class markets still get FX-036's 3× reward density uplift.
  - **Operator-tunable:** `RF_DUMP_DEPTH_SAFETY_FACTOR` raise/lower via `config_overrides.json` to adjust the conservatism. `0` reverts to FX-036-only behaviour as an escape hatch. Hot-reloadable.
  - **Conservative direction:** more fall-backs to legacy, never less. Cannot make a bad placement worse.
- **Production verification path:** After Helsinki pulls v5.1.20:
  1. Remove `"RF_TARGET_QUEUE_AHEAD_USD": 0` from `/home/polymarket/Polymarket-bot/config_overrides.json` (re-enables FX-036 with FX-041 protection).
  2. `sudo systemctl restart polymarket-farmer`.
  3. Watch first 5-10 farmer cycles in `journalctl -f -u polymarket-farmer`. Expect close-to-mid placement on deep symmetric books (e.g., Iran-class with $200+/day rewards) and legacy zone-edge on thin/asymmetric ones.
  4. Verify reward density uplift via `[ATTRIBUTION] reward + rebate` over a 24h window vs the prior 24h.
- **Known interpretation trade-off:** The check measures depth on the OPPOSITE merged-book side, not the SAME side. DumpManager's passive mode (`dump_manager.py:308-327`) actually crosses the spread to take the SAME-side bids/asks, so SAME-side depth would be the most physically-correct dump-absorption measurement. The OPPOSITE-side check was chosen because (a) it matches FX-041's acceptance criterion narrative ("deep bid, thin ask → fall back"), (b) it's a new safety axis complementary to the existing same-side `exit_buf` check at `order_lifecycle.py:482-493`, and (c) "two-sided" in the ticket title implies it. Both interpretations catch the OpenAI cascade (asymmetric books are bad regardless of which side you check). Operator can raise the factor or override via `config_overrides.json` if production shows false positives/negatives on extreme-priced markets.
- **Known simplification:** `dump_price = midpoint` is used uniformly for both placement sides. For cheap-YES markets (midpoint $0.10), this understates NO-side inventory value by 9× (NO mid would be $0.90). The error is bounded; the operator can compensate by raising the safety factor on extreme markets.
- **Related:** FX-036 (placement strategy this protects), FX-040 (cold-start trial sizing — both ship before re-enabling FX-036), Architecture doc §4.23.7 (new).
- **Hardening Phase:** Phase 1 (post-FX-036 cascade).
- **History:**
  - 2026-05-20 — Opened after analyzing OpenAI HIGH $1.5T dump slippage.
  - 2026-05-20 — Code, 18 new regression tests, fast tier 737 → 755 pass, commit `3534cb5`.

---

### FX-040 — Cold-start trial-mode sizing [FIXED]

- **Severity:** High (biggest single contributor to 2026-05-19 cascade)
- **Status:** Fixed (commit `c2c21d7`, 2026-05-20)
- **Tags:** `[ARCH]` `[STRATEGY]`
- **Opened:** 2026-05-20 (surfaced by FX-036 production cascade)
- **Closed:** 2026-05-20
- **Original symptom:** On 2026-05-19, the SafetyController demotion to DEGRADED forced the allocator into low-priced OpenAI cold-start markets ($0.10-$0.22 underlying). The bot used `RF_NEW_MARKET_Q_SHARE_PRIOR = 0.10` to score these, leading to standard-size deployments (143 shares on one). Reality: actual q_share was <0.005 (UI showed `<$0.01/day` earnings). When takers came through these thin books, our full-size resting bids got swept, and the dump-immediately cascade burned -$17.63 across three fills.
- **Root cause:** Cold-start prior was applied uniformly via Q-Share Priority 3 (arch §4.2). The allocator's sizing logic used this prior to compute share counts as if it were measured data. There was no "trial sizing" branch that said "until we have ≥N scoring snapshots, deploy at min_size only."
- **Fix applied (commit `c2c21d7`):**
  - Three new config knobs in `config.py`:
    - `RF_TRIAL_MIN_SHARES = 20` (floor for trial deploys; market `min_size` wins when larger, for venue compliance)
    - `RF_TRIAL_SCORING_SAMPLES = 5` (graduation threshold — markets with ≥5 scoring snapshots use full sizing)
    - `RF_TRIAL_BUDGET_PCT = 0.25` (max cumulative trial exposure as fraction of `total_capital`; default 25% lets 1-3 trials fit on a $200 wallet with typical `min_size=50`)
  - `q_score_samples` propagated through `MarketMetrics` → `ScoredMarket` (the field already lived inside `reward_market_stats.data` JSON; just exposed it to the allocator). Backward-compatible default of 0.
  - Trial branch in `oversight/allocation_writer.compute_allocations`:
    - Untested markets (`q_score_samples < RF_TRIAL_SCORING_SAMPLES`) capped at `max(min_size, RF_TRIAL_MIN_SHARES)` shares regardless of `recommended_shares`
    - Cumulative trial cost tracked; over-budget trials rejected with reason `"Trial budget exhausted ($used+$next>$cap, samples=k)"`
    - Score-desc order means highest-scored trials get first dibs on the budget
    - Redistribution pass excludes trial markets so the cap actually binds (without this fix, surplus capital flowed back into the capped trials)
    - `[FX-040 trial]` summary log line per cycle showing `deployed=N rejected=M budget_used=$X/$Y`
- **Commit:** `c2c21d7` on `main` — "Add cold-start trial-mode sizing (FX-040)".
- **Diff size:** +3 lines `config.py`, +91 / -1 lines `oversight/allocation_writer.py`, +3 lines `oversight/data_collector.py`, +5 / -1 lines `oversight/market_scorer.py`, +14 / -5 lines `tests/test_market_scorer.py` (one existing test updated for new behavior), +240 lines new `tests/test_trial_sizing.py` (16 tests).
- **Verification:**
  - **Unit:** 16 new tests in `tests/test_trial_sizing.py` covering trial detection (4), trial target shares (3), trial-mode capping (1), graduated full sizing (1), trial-budget rejection (1), redistribution exclusion (1), score-ordering (1), mixed trial/graduated handling (1), backward compat (2), graduated doesn't consume budget (1).
  - **Regression:** Full fast tier 721 → **737 pass** (0 regressions).
  - **Production:** First oversight cycle on Helsinki post-deploy (08:22:40 UTC 2026-05-20):
    ```
    [FX-040 trial] deployed=1 rejected=49 budget_used=$46/$55 (25% cap)
    SafetyController [SEVERELY_MISCALIBRATED]: 2/3 markets, $88/$221 capital
    ```
    1 trial deployed + 49 cold-start markets explicitly rejected — exactly the kind of markets that caused yesterday's cascade.
- **Acceptance criterion:** ✓ Yesterday's OpenAI HIGH $1.5T (min_size=200, daily_rate=$400) — the market that lost $12.87 — is now REJECTED with reason `"Trial budget exhausted ($0+$182>$55, samples=0)"` on the $221 wallet. The 143-share trap is closed.
- **Risk profile:**
  - **Reward yield:** Slightly lower in the short term — trial sizing limits discovery exposure, so the bot earns less from new high-reward markets until they graduate. Compensated by no longer losing on cold-start traps. Net expected: positive after the first 48-72h of operation.
  - **Operator-tunable:** `RF_TRIAL_BUDGET_PCT` raise/lower via `config_overrides.json` to adjust discovery vs safety trade-off. Hot-reloadable.
  - **Backward compat:** Defaults to `q_score_samples=0` on markets with no data, which puts them in trial mode (safe). Existing tests pass except one (`test_surplus_gets_redistributed`) which was updated to explicitly mark its markets as graduated (q_score_samples=10) — that's the test's intent.
- **Related:** FX-036 (the placement strategy that exposed the cascade — still runtime-disabled until FX-041 ships), FX-037 (BUY-side phantom-fill defense — orthogonal), FX-041 (two-sided depth check — prerequisite for FX-036 re-enable).
- **Hardening Phase:** Phase 1a (post-FX-036 cascade, first ship).
- **History:**
  - 2026-05-20 ~04:00 UTC — Opened after analysis of 2026-05-19 cascade.
  - 2026-05-20 ~14:30 UTC — Code, tests, commit (`c2c21d7`), push to main, CI green.
  - 2026-05-20 08:22:40 UTC — Deployed to Helsinki via `git pull + systemctl restart`. First oversight cycle confirmed FX-040 fired with `deployed=1 rejected=49`.

---

### FX-036 — Placement formula picks far-edge of reward zone, leaves ~7× reward density on the table [FIXED]

- **Severity:** High (reward-yield impact; the bot's stated objective is reward maximization)
- **Status:** Fixed (v5.1.18, 2026-05-19)
- **Tags:** `[BUG]` `[ARCH]` `[STRATEGY]`
- **Opened:** 2026-05-19 (surfaced by operator inspection of Helsinki's first production orders on the Iran market)
- **Closed:** 2026-05-19
- **Original symptom:** The bot's first production order on Helsinki (Iran market `0xd9933a54c518...`, midpoint $0.485 at placement, `max_spread = 5.5¢`) placed YES bid at $0.44 and NO bid at $0.47 — i.e., **1 tick inside the far edge of the reward zone** on both sides. With `max_spread = 5.5¢`, that's `s_max − 1 tick = 4.5¢` from midpoint. At that distance, Polymarket's reward formula `reward_per_share ∝ (1 − d/s_max)` pays only ~18% of the theoretical maximum per share-minute (the arch-doc's earlier "~9%" estimate used a slightly different `s_max` rounding; the actual ratio for the Iran 5.5¢ zone is `1 − 4.5/5.5 = 18.2%`). At 2.5¢ from midpoint, reward density rises to `1 − 2.5/5.5 = 54.5%` — exactly 3× the legacy density. Helsinki's $24,000+ of queue ahead of our 44¢ bid meant we were heavily over-protected against fills, at the cost of nearly all reward yield.
- **Root cause:** `order_lifecycle.py:354-355` placed at the far edge of the reward zone unconditionally — no consideration of queue depth ahead of us. The original design intent was fill-avoidance; fill-avoidance and reward-maximization are different objectives and the latter is what the bot was built for.
- **Fix applied (v5.1.18):** Introduced `_compute_edge_prices` + `_queue_aware_edge` helpers in `order_lifecycle.py`. The new flow walks the merged book from best (closest to mid) outward, accumulating cumulative USD notional (`price × size`). When cumulative crosses `RF_TARGET_QUEUE_AHEAD_USD` (new config knob, default `$1000`), placement sits one tick BEHIND that level — deeper inside the reward zone, shielded from fills by the queue we sit behind. Falls back to the legacy zone-edge formula when:
  1. `RF_TARGET_QUEUE_AHEAD_USD <= 0` (operator escape hatch — byte-identical to pre-FX-036 behaviour),
  2. the book is too thin to cross threshold within the reward zone (weather-style markets, low-competition markets — no regression),
  3. the one-tick step would itself exit the zone (zone-boundary edge case — defensive).
  Final values are clamped to `[0.01, 0.99]` and rounded to `decimals` matching the market's `tick_size` — same safety invariants as the legacy formula.
- **Commit:** `8152a8b` on `main` — "Add queue-depth-aware order placement (FX-036)".
- **Diff size:** +1 line in `config.py` (new knob), +~100 / -6 lines in `order_lifecycle.py` (two helpers + 6-line call-site swap), +~350 lines new `tests/test_placement.py` (24 tests across 11 classes covering escape hatches, bid + ask symmetry, input coercion, tick variations, escape hatches, Iran-market scenario, thin-book fallback, asymmetric depth, safety invariants, end-to-end wiring).
- **Verification:**
  - **Unit:** 24 new tests in `tests/test_placement.py` exercise the helper directly + end-to-end through `place_orders_for_market`. Full fast tier: 697 → 721 pass.
  - **Inline production-shape:** ran `_compute_edge_prices` against the Iran-market shape — `bid $0.460 / ask $0.510` (vs pre-fix `$0.440 / $0.530`); reward density `54.5%` vs `18.2%` legacy = **3.00× uplift**.
  - **Thin-book regression:** ran against a 5-level book with $50 size per level — falls back to legacy `$0.440 / $0.530` placement. No behaviour change for weather-style markets where the existing `min_size + dump on fill` flow keeps working unchanged.
- **Acceptance criterion (from §3 entry):** ✓ On deep markets, post-FX-036 placement sits ~2.5¢ from midpoint (vs legacy 4.5¢). Pre/post inline computation confirms 3× density uplift. Production verification (24h soak on Helsinki) will follow next session; the change is structurally sound and disable-able via `RF_TARGET_QUEUE_AHEAD_USD = 0` if production reveals surprises.
- **Risk profile:**
  - **Fill rate:** Slightly higher on deep markets (we sit closer to mid). The `$1000` queue threshold means ~$1000 of orders must fill at our YES-equivalent price before ours can — rare under normal market dynamics. If observed fill rate is uncomfortable, raise the knob (e.g., `$2000` → 1.6¢ from mid on the Iran market) or set to `0` for legacy behaviour.
  - **Inventory management:** Existing dump pipeline (`DumpManager`) handles fills regardless of placement strategy. Higher fill rate ⇒ higher dump traffic. No new failure modes.
  - **Safety guardrails:** Unchanged. Kill switch on `{24h loss > 0.1·T, cf < 0.01, fill-rate spike > 3×}` still bounds drawdown. SafetyController's per-state `max_markets` / `capital_pct` caps unchanged.
  - **Compatibility:** Helper falls back to legacy formula whenever it can't safely improve placement. No tests broken (697 baseline → 721 with new tests).
- **Related:** Architecture doc §4.23 (full design); FX-031 (capital-cap scaling — orthogonal); FX-035 (V2 SDK book-fetch fix that finally let us see real production orders to diagnose this).
- **Hardening Phase:** Strategy / reward yield (post-roadmap follow-up to v5.1.17 LIVE-resumption).
- **History:**
  - 2026-05-19 — Surfaced by operator inspection of Helsinki orderbook showing YES bid at 44¢ on a 49¢ midpoint market. Strategy and proposal documented in arch doc §4.23 in the same session.
  - 2026-05-19 — Shipped on `main` as `8152a8b` with 24 regression tests; 721/721 fast-tier green; 3× inline density uplift confirmed.

---

### FX-001 — SafetyController I9 deadlock on fresh-DB bootstrap [FIXED]

- **Severity:** Critical
- **Status:** Fixed (commit `dd67f97`, 2026-05-15)
- **Tags:** `[BUG]` `[ARCH]`
- **Opened:** 2026-05-15
- **Closed:** 2026-05-15 (server-side verification pending as of doc creation)
- **Original symptom:** First LIVE cutover on a fresh-DB Helsinki server. Bot connected to CLOB API successfully, placed 2 orders in cycle 3 (no geoblock — Helsinki migration worked), then stopped placing orders for 2.5+ hours despite running cleanly. `safety_state` table showed continuous `DATA_UNAVAILABLE`. Allocator's `markets_deploy: 0` every cycle.
- **Root cause:** `oversight/safety_controller.py::_query_data_freshness` returned `None` when `scoring_snapshots` was empty. I9 interpreted that as a violation and forced the state machine into `DATA_UNAVAILABLE`. `DATA_UNAVAILABLE` blocks trial markets per `STATE_PERMISSIONS`. On a fresh DB, every market is a trial (`fill_count=0` + `confidence='low'`). So 0 deploys → 0 orders → no `are_orders_scoring` calls → empty `scoring_snapshots` → I9 keeps firing → permanent deadlock.
- **Fix applied:** Differentiate the two meanings of empty `scoring_snapshots`:
  - Cold-start (no orders ever placed): return `0.0`, treating freshness as N/A.
  - Orders exist but scoring pipeline broken: return `None` as before (defensive).
  - Distinction is made by `SELECT COUNT(*) FROM orders_placed`.
- **Commit:** `dd67f97` on `main`
- **Diff size:** +15 / -0 lines in `oversight/safety_controller.py`
- **Verification status:** Pending server-side pull + restart + observation of next oversight cycle. Once verified in production:
  - SafetyController state advances out of DATA_UNAVAILABLE
  - `markets_deploy > 0` in oversight cycle output
  - No more `VIOLATION: data_freshness` lines (when on cold start)
- **What this fix does NOT do (still open):**
  - Doesn't address FX-002 (I3 drawdown on cold start) — separate but adjacent
  - Doesn't add a BOOTSTRAP state (FX-003)
  - Doesn't clean up the phantom Tamilaga `dump_states` row (FX-007)
  - Doesn't add SafetyController test coverage (FX-016)
- **History:**
  - 2026-05-15 — Diagnosed via three parallel code audits + code-grounded synthesis
  - 2026-05-15 — Patch landed on main as `dd67f97`
  - 2026-05-18 — Migrated to fixit doc; server-side verification still pending

---

### FX-017 — Stale `polymarket-bot.service` file in repo root [FIXED]

- **Severity:** Low
- **Status:** Fixed (commit `3f50441`, 2026-05-18)
- **Tags:** `[OPS]` `[DOC]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** Repo root contained `polymarket-bot.service`, a systemd unit file referencing `/opt/polymarket-bot/` and running `main.py` (the deprecated legacy entry). It wasn't the file deployed by architecture doc §11.11; it was a leftover from an earlier design.
- **Root cause:** Old artifact never cleaned up.
- **Fix applied:** `git rm polymarket-bot.service`. The canonical units live at `/etc/systemd/system/polymarket-{farmer,oversight}.service` on the Helsinki server and run `reward_farmer.py` / `oversight_agent.py` from `/home/polymarket/Polymarket-bot`; the canonical text is inline in §11.11 of the architecture doc.
- **Commit:** `3f50441` on `main`
- **Diff size:** -36 lines (1 file deleted)
- **Verification:** `git ls-files | grep polymarket-bot.service` returns nothing post-commit.
- **Future-reference capture (now historical):** The two directives worth preserving from the deleted file were `KillSignal=SIGINT` and `TimeoutStopSec=30`. They were copied into the canonical §11.11 unit blocks by Phase 5 (`91bae99`, FX-014) on 2026-05-18.
- **Related:** FX-014 (subsequently shipped — see §4 entry).
- **Hardening Phase:** 0 (housekeeping).
- **History:**
  - 2026-05-18 — Removed from repo as part of Phase 0 housekeeping batch (`3f50441` + `987a844` pushed together).

---

### FX-018 — `numpy` missing from `requirements.txt` [FIXED]

- **Severity:** Medium
- **Status:** Fixed (commit `987a844`, 2026-05-18)
- **Tags:** `[OPS]`
- **Opened:** 2026-05-18 (originally documented in arch doc §10.3 v5.1.4 lessons)
- **Closed:** 2026-05-18
- **Original symptom:** `requirements.txt` had 4 lines (py-clob-client-v2, requests, python-dotenv, web3) and none pulled in `numpy`. On Mac, numpy arrived transitively via `streamlit` (in `pyproject.toml`). On a headless server install via `pip install -r requirements.txt`, numpy was missing → bot import errors at runtime. The Helsinki bring-up worked around it by manually `pip install numpy`.
- **Root cause:** Dependency declaration gap. `numpy` is a real production dependency but was only listed in `pyproject.toml`'s streamlit transitive tree, not in `requirements.txt`.
- **Fix applied:** Added `numpy>=2.0` to `requirements.txt`. The `>=2.0` floor matches what's already running on Helsinki and supports Python 3.12+; the repo targets 3.14 per `pyproject.toml`.
- **Commit:** `987a844` on `main`
- **Diff size:** +1 line in `requirements.txt`
- **Verification:** Fresh `pip install -r requirements.txt` on a clean Python 3.14 venv now installs numpy.
- **Related:** FX-020 (doc cleanup, also closed in Phase 0).
- **Hardening Phase:** 0 (housekeeping).
- **History:**
  - 2026-05-18 — Added to `requirements.txt` as part of Phase 0 housekeeping batch (`3f50441` + `987a844` pushed together).

---

### FX-020 — Architecture doc §11.4 geoblock candidates list partly wrong [FIXED]

- **Severity:** Medium
- **Status:** Fixed (architecture doc edit, landed alongside `dd67f97`, 2026-05-18 reconciled)
- **Tags:** `[DOC]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** Architecture doc §11.4 listed "Helsinki / Falkenstein / Nuremberg / Singapore" as candidate non-blocked Hetzner regions with only a caveat to verify. Verified status against https://docs.polymarket.com/developers/CLOB/geoblock on 2026-05-15: **Helsinki — Allowed; Falkenstein — Blocked; Nuremberg — Blocked; Singapore — Close-only**. Operators following the candidate list would have wasted a server purchase on 2 of 4 candidates.
- **Root cause:** Doc was written before verification.
- **Fix applied:** §11.4 in the architecture doc was rewritten as a verified status table (Helsinki ✓ Allowed; Falkenstein / Nuremberg / Ashburn / Hillsboro blocked; Singapore close-only) plus the explicit statement "**As of v5.1.5, Helsinki is the only Hetzner Cloud location that supports order placement on Polymarket.**" with a verification hint pointing at https://polymarket.com/api/geoblock for re-checking per-provisioning. This change actually shipped alongside the v5.1.5 amendments (commit `dd67f97`) and is already present in the architecture doc as of the v5.1.5 bump; this fixit entry was simply never moved from §3 to §4. Marked Fixed in Phase 0.
- **Commit:** `dd67f97` (architecture-doc edit; the bot code in `dd67f97` is the I9 deadlock fix, but the doc bump shipped in the same v5.1.5 session)
- **Diff size:** Doc-only — architecture doc §11.4 paragraph replaced with a 6-row verified-status table.
- **Verification:** Architecture doc §11.4 (≈ line 2434) now contains the verified Hetzner table; cross-referenced in §10.3 "v5.1.4 blocker resolved in v5.1.5" struck-through entry.
- **Related:** FX-022 (Ashburn → Helsinki refresh — still open for §10.3 lessons).
- **Hardening Phase:** 0 (immediate doc update; closed retrospectively).
- **History:**
  - 2026-05-15 — Doc edit shipped as part of v5.1.5 amendments (alongside `dd67f97`).
  - 2026-05-18 — Reconciled in fixit doc as part of Phase 0 housekeeping batch.

---

### FX-002 — I3 drawdown invariant on fresh-DB bootstrap [FIXED]

- **Severity:** High
- **Status:** Fixed (commit `dc78ba0`, 2026-05-18)
- **Tags:** `[BUG]` `[ARCH]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** I3 (drawdown) was firing CRITICAL → DATA_UNAVAILABLE on the first LIVE cycle of a fresh-DB server because both `total_portfolio_value` and `exchange_balance` arrive as zero during the ~30-minute window between LIVE cutover and the first `usdc_balance` row landing. There's no drawdown to compute from a zero baseline, but the violation's DATA_UNAVAILABLE severity blocks trials, and on a fresh DB every market is a trial — same deadlock pattern as the I9 issue that v5.1.5 fixed.
- **Root cause:** `evaluate_state` at `oversight/safety_controller.py:330-339` treated `_portfolio_val <= 0` as "data unavailable" without distinguishing genuine cold start (no operational history) from API failure (had data, now don't).
- **Fix applied:** New helper `_is_genuine_cold_start()` queries `orders_placed` and `fills` counts (both must be empty). When both are zero AND `_portfolio_val <= 0`, I3 logs one INFO line and skips the violation — there's nothing to draw down from. The original DATA_UNAVAILABLE behaviour is preserved verbatim for the warm-DB case (any prior orders or fills). The helper is also wired into `_query_data_freshness`, replacing the inline check from `dd67f97` so I9 and I3 share one source of truth.
- **Commit:** `dc78ba0` on `main`
- **Diff size:** +43 / -15 lines in `oversight/safety_controller.py`; new test file `tests/test_safety_controller.py` with 7 tests.
- **Verification:** Targeted pytest passes (7 new tests); full fast tier still passes (443→453 with FX-003 tests added). Production behaviour on the Helsinki server is unchanged — the server has placed orders so `_is_genuine_cold_start` returns False there. On the next genuinely fresh-DB bring-up, I3 will not deadlock the bootstrap.
- **What this fix does NOT do (still open):**
  - Doesn't address the `$1500` capital-sizing race (FX-013) — separate axis.
  - Doesn't add a `BOOTSTRAP` state — that's FX-003, shipped in the same Phase 1 batch as `541108b`.
  - Doesn't expand SafetyController test coverage beyond Phase 1 scope (FX-016 / Phase 6).
- **Related:** FX-001 (same family — I9 cold-start helper now factored out), FX-003 (BOOTSTRAP state, shipped together), FX-013 (capital sizing on cold start).
- **Hardening Phase:** 1.
- **History:**
  - 2026-05-18 — Shipped on `main` as `dc78ba0`. Phase 1 commit 1 of 2.

---

### FX-003 — No `BOOTSTRAP` state for first-time-ever cold start [FIXED]

- **Severity:** Medium
- **Status:** Fixed (commit `541108b`, 2026-05-18)
- **Tags:** `[ARCH]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** SafetyController's cold-start default was `MILDLY_MISCALIBRATED` — second-highest rung, granting 70% capital and trials on cycle 1 of a fresh-DB LIVE start. On a $201 wallet that's $140 of immediate exposure; on a $10k wallet, $7k. No "ease in" mode existed.
- **Root cause:** No bootstrap-aware state in the enum; the existing rung structure jumped directly from `SEVERELY_MISCALIBRATED` (20 markets, 40%, no trials) to `MILDLY_MISCALIBRATED` (40 markets, 70%, trials). Trials must be on for a cold-start state because on a fresh DB every market is a trial — without them the bot can't accumulate calibration data.
- **Fix applied:** New `BOOTSTRAP` state slotted between `MILDLY_MISCALIBRATED` (severity 1) and `SEVERELY_MISCALIBRATED` (now severity 3). Permissions: `max_markets=10, capital_pct=0.30, trials=True`. Cold-start default uses the new helper `_cold_start_or(MILDLY_MISCALIBRATED)` — returns `BOOTSTRAP` when `_is_genuine_cold_start()` is True, else preserves the existing MILDLY default. Exit logic added to `_handle_upgrade`: BOOTSTRAP → MILDLY on EITHER `lifetime_fills >= BOOTSTRAP_FILL_EXIT (10)` (fast path) OR `_bootstrap_clean_cycles >= UPGRADE_FROM_BOOTSTRAP (3)` (slow path, for markets-are-dry scenarios). BOOTSTRAP is once-only — recoveries from downgrades climb straight to MILDLY, not back through BOOTSTRAP.
- **Commit:** `541108b` on `main` (subsumes FX-012)
- **Diff size:** +106 / -17 lines in `oversight/safety_controller.py`; +151 lines in `tests/test_safety_controller.py` (10 new tests); 4 lines updated in `test_safety.py` root runner.
- **Verification:** All 17 Phase 1 tests pass. Full fast tier: 453/453 pass.
- **Production safety:** The Helsinki server has placed orders → `_is_genuine_cold_start` returns False → BOOTSTRAP is NOT entered on `git pull + restart`. Server stays in its current state. The next genuinely-fresh-DB bring-up will start in BOOTSTRAP and ease in cleanly.
- **Related:** FX-001, FX-002, FX-012 (subsumed).
- **Hardening Phase:** 1.
- **History:**
  - 2026-05-18 — Shipped on `main` as `541108b`. Phase 1 commit 2 of 2 (closes Phase 1).

---

### FX-004 — `orders_placed` counter increments on attempt, not success [FIXED]

- **Severity:** High
- **Status:** Fixed (commit `e7fc3d2`, 2026-05-18)
- **Tags:** `[BUG]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** `[CYCLE_SUMMARY]` JSON reported `orders_placed: N` but the `orders_placed` DB table had fewer (or zero) corresponding rows. Observed in production cycle 3 of the v5.1.5 Helsinki bootstrap: `orders_placed: 2` while DB had 0 rows — both placement attempts had 400'd on resolved orderbooks. Operator couldn't trust the telemetry; any dashboard or alert built on `[CYCLE_SUMMARY]` would be misled.
- **Root cause:** `_gated_place_orders_for_market` in `reward_farmer.py` did `self._cycle_orders_placed += 1` unconditionally after calling `OrderLifecycle.place_orders_for_market`. The wrapped function had no return value; its internal API-success check (the `if oid:` branches around `order_lifecycle.py:379` and `:421`) gated only the DB insert into `orders_placed`, not the caller's counter.
- **Fix applied:** `OrderLifecycle.place_orders_for_market` now returns `int` — the count of API-confirmed placements (0, 1, or 2). Only LIVE-mode paths where `create_and_post_order` returned a valid `orderID` AND `log_order_placed` wrote to the DB contribute. Every early return (no book, wide spread, sports block, resolution proximity, has-both shortcut) and DRY-run path returns 0 because they don't write to the `orders_placed` DB table. `_gated_place_orders_for_market` accumulates the value: `self._cycle_orders_placed += n_placed`. A defensive `isinstance(n_placed, int)` check treats pre-FX-004 stub returns (None) as 0 so the counter never advances on stale plumbing.
- **Commit:** `e7fc3d2` on `main`
- **Diff size:** +34 / -12 lines in `order_lifecycle.py` (1 function signature change, 5 return-statement updates, 2 counter increments); +26 / -8 lines in `reward_farmer.py` (wrapper plumbing + docstring); +270 lines in new `tests/test_order_lifecycle.py`.
- **Verification:** New test file `tests/test_order_lifecycle.py` has 17 tests across 4 classes — returned-count semantics (5 tests covering 0/1/1/2/missing-orderID), early-return paths (5 tests covering no-book / empty-book / wide-spread / has-both-fresh / resolution-proximity), dry-run-path (1 test), gated-wrapper accumulation (6 tests covering 0/1/2 increments, multi-call accumulation, None-tolerance, DRY-mode skip). All 17 pass. Full fast tier 453 → 470 (no regressions).
- **Acceptance criterion (from §3 entry):** ✓ `[CYCLE_SUMMARY] orders_placed` == `SELECT COUNT(*) FROM orders_placed WHERE ts BETWEEN cycle_start_ts AND cycle_end_ts` for any cycle, including failed placements. Verified by direct return-value assertions in the lifecycle tests and accumulator semantics in the wrapper tests.
- **What this fix does NOT do (still open):**
  - Doesn't change book-failures accounting on placement failures (FX-005, Phase 3).
  - Doesn't add the broader OrderLifecycle test build-out — these 17 tests are scoped to the FX-004 counter surface only.
- **Related:** FX-005 (sibling — same failure-path family, scheduled with the dump-state lifecycle work).
- **Hardening Phase:** 2.
- **History:**
  - 2026-05-18 — Shipped on `main` as `e7fc3d2`. Phase 2 complete.

---

### FX-012 — Cold-start defaults to MILDLY_MISCALIBRATED, not conservative [FIXED]

- **Severity:** Low
- **Status:** Fixed (commit `541108b`, 2026-05-18 — subsumed by FX-003)
- **Tags:** `[ARCH]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** `SafetyController.__init__` set `self.state = MILDLY_MISCALIBRATED` and `_load_state` defaulted to the same on every "no row / old row / exception" path — giving fresh-DB starts the second-highest rung's 70% capital + trials.
- **Fix applied:** Subsumed by FX-003. `_load_state` now uses `_cold_start_or(MILDLY_MISCALIBRATED)` which routes to `BOOTSTRAP` on a genuine cold start and preserves MILDLY otherwise. `__init__`'s field default remains MILDLY (used only when `_load_state` is bypassed — test fixtures, programmatic poking).
- **Commit:** `541108b` on `main`
- **Diff size:** Part of the FX-003 commit; the `_cold_start_or` helper added +6 lines + 4 call-site changes inside `_load_state`.
- **Verification:** `tests/test_safety_controller.py::TestBootstrapEntry::test_fresh_db_defaults_to_bootstrap` (pass) and `test_warm_db_with_orders_defaults_to_mildly` (pass).
- **Related:** FX-003 (parent).
- **Hardening Phase:** 1.
- **History:**
  - 2026-05-18 — Shipped on `main` as part of `541108b`.

---

### FX-007 — Orphan scan creates persistent dumps for resolved markets [FIXED]

- **Severity:** Critical
- **Status:** Fixed (commit `7d8d38d`, 2026-05-18)
- **Tags:** `[BUG]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** Production had a `dump_states` row for "Will Tamilaga Vettri Kazhagam" with side=no, shares=3826.0, fill_price=0.0. The bot retried the dump every 30 s, got 400 "the orderbook X does not exist," logged the error, repeated indefinitely. The position came from on-chain CTF balance held by FUNDER from a previous deployment for a now-resolved market; manual SQL DELETE on `dump_states` didn't help because the orphan scan re-discovered the on-chain balance on every restart and re-queued the dump.
- **Root cause:** The bot had no DB-backed memory of "this market is dead" — every retry path (orphan scan, dump-state restore, exchange-position sync) blindly re-tried. CTF redemption is manual UI-only, so the on-chain balance never clears, so the loop is permanent without an external mechanism.
- **Fix applied:** Introduced `unliquidatable_markets` DB table (cid PK, reason, marked_at, last_retry_at) + 6 BotDatabase methods (mark, is, delete, update_retry, load_set, get_for_reprobe). Producers: `DumpManager.dump_position` and `OrderLifecycle.place_orders_for_market` exception handlers detect the canonical V2 SDK 400 body ("orderbook" AND "does not exist" both present) and mark on first failure. Consumers: every order path (BUY in OL, SELL in DM, orphan scan, exchange-position sync, dump-state restore) now gates on `db.is_unliquidatable(cid)` and skips. Detection is intentionally strict — "insufficient balance", "rate limit", and "market does not exist" all leave the cid unmarked (regression tests cover each).
- **Commit:** `7d8d38d` on `main` (subsumes FX-005, FX-006, FX-008, FX-009, FX-028)
- **Diff size:** +984 / -20 lines across 12 files. Core: +107 in `database.py`, +45 in `dump_manager.py`, +49 in `order_lifecycle.py`, +131 in `reward_farmer.py`, +1 in `config.py`. Tests: +606 in new `tests/test_unliquidatable_markets.py`, +small fixture updates to 6 existing test files.
- **Verification:** 31 new tests in `tests/test_unliquidatable_markets.py` covering every integration point (DB methods, gate semantics, mark-on-exception, no-mark on transient errors, re-probe un-marking on healthy book, retry-stamp on still-dead, CLOB fallback, orphan-scan gate, exchange-sync gate, dead-market cleanup cascade). Full fast tier 470 → 501 (no regressions).
- **Production impact (expected on next Helsinki `git pull + restart`):**
  1. `_restore_dump_states` loads the Tamilaga row, hits "orderbook does not exist", marks unliquidatable + deletes dump_state.
  2. Next `_scan_orphaned_positions` / `_sync_exchange_positions` sweep skips Tamilaga.
  3. Spam stops within ~1 cycle.
- **Comprehensive audit findings (all fixed pre-commit):**
  1. Detector was over-tight (strict "orderbook does not exist" missed the canonical "the orderbook X does not exist" form with cid in the middle). Rewrote to require both substrings; negative regression tests added.
  2. `_sync_exchange_positions` had no gate — would re-spawn `self.markets` entries for unliquidatable cids every 30 min indefinitely. Gate added.
  3. `load_unliquidatable_set` docstring described a non-existent startup cache. Rewritten.
  4. Test coverage gaps for orphan-scan gate, sync-exchange-positions gate, CLOB-fallback branch, and detector tightening — all closed.
- **Related:** FX-005, FX-006, FX-008, FX-009, FX-028 (all subsumed); FX-023 (architecture doc).
- **Hardening Phase:** 3.
- **History:**
  - 2026-05-18 — Shipped on `main` as `7d8d38d`. Closes Phase 3.

---

### FX-005 — `book_failures` doesn't increment on order-placement failures [FIXED]

- **Severity:** High
- **Status:** Fixed (commit `7d8d38d`, 2026-05-18 — subsumed by FX-007)
- **Tags:** `[BUG]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** Markets whose orderbook had been removed (resolved markets) returned HTTP 400 on `create_and_post_order`. The exception was caught at `order_lifecycle.py:377-388, 419-431`, logged, but `ms.book_failures` was never incremented. The dead-market cleanup mechanism at `reward_farmer.py:1940-1953` therefore never fired for these markets — infinite retry.
- **Fix applied:** Subsumed by FX-007. `OrderLifecycle.place_orders_for_market` now marks `unliquidatable` on the canonical 400 body for both YES and NO BUY paths; that mark plus the gate at function entry retire the cid after a single attempt — strictly better than the original "retry at most 3 times" proposal. The companion `book_failures` counter is unchanged but irrelevant now since the cid is already filtered.
- **Commit:** `7d8d38d` on `main`
- **Verification:** `tests/test_unliquidatable_markets.py::TestOrderLifecycleMarkOnException::test_marks_on_yes_buy_orderbook_gone` and `test_marks_on_no_buy_orderbook_gone`.
- **Related:** FX-007 (parent).
- **Hardening Phase:** 3.
- **History:**
  - 2026-05-18 — Shipped on `main` as part of `7d8d38d`.

---

### FX-006 — Dead-market cleanup orphans `dump_states` rows [FIXED]

- **Severity:** High
- **Status:** Fixed (commit `7d8d38d`, 2026-05-18)
- **Tags:** `[BUG]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** When `ms.book_failures >= 3`, the cleanup at `reward_farmer.py:1940-1953` cancelled active orders, deleted from `active_orders`, and removed the market from `self.markets` — but did NOT delete `dump_states` rows. The dump entry persisted in the DB and kept the dump retry loop alive even after the market was gone from active tracking.
- **Fix applied:** The dead-market cleanup loop now (a) calls `self.db.delete_dump_state(cid, side)` for both sides as part of the cleanup, and (b) calls `self.db.mark_unliquidatable(cid, reason="dead_market_book_failures")` so any future orphan-scan / sync / restore path skips the cid permanently.
- **Commit:** `7d8d38d` on `main`
- **Verification:** `tests/test_unliquidatable_markets.py::TestDeadMarketCleanupCascade::test_cleanup_loop_cascades`.
- **Related:** FX-007 (sibling).
- **Hardening Phase:** 3.
- **History:**
  - 2026-05-18 — Shipped on `main` as part of `7d8d38d`.

---

### FX-008 — `dump_states` reload on restart re-creates failing dumps [FIXED]

- **Severity:** High
- **Status:** Fixed (commit `7d8d38d`, 2026-05-18 — subsumed by FX-007)
- **Tags:** `[ARCH]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** Manually deleting a phantom `dump_states` row via SQL didn't help — on next restart, `_restore_dump_states` reloaded from DB AND the orphan scan re-discovered the same on-chain CTF position and re-created the dump. Whack-a-mole.
- **Fix applied:** Subsumed by FX-007. `_restore_dump_states` now gates each row on `db.is_unliquidatable(cid)` and silently deletes the dump_state row if the cid is marked. Combined with the orphan-scan + sync-exchange-positions gates, the restart loop is closed end-to-end. Manual SQL DELETE on `dump_states` followed by a normal restart is now sufficient to clear a stuck dump (no need to also mark unliquidatable manually — the first dump attempt on the next cycle will mark it automatically on the 400 response).
- **Commit:** `7d8d38d` on `main`
- **Verification:** `tests/test_unliquidatable_markets.py::TestRestoreDumpStatesGate::test_skips_and_deletes_dump_state_on_unliquidatable_cid` and `test_restores_dump_state_for_normal_cid`.
- **Related:** FX-007 (parent).
- **Hardening Phase:** 3.
- **History:**
  - 2026-05-18 — Shipped on `main` as part of `7d8d38d`.

---

### FX-009 — `dump_state` row saved BEFORE the SELL is posted [FIXED]

- **Severity:** Medium
- **Status:** Fixed (commit `7d8d38d`, 2026-05-18 — subsumed by FX-007)
- **Tags:** `[BUG]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** `dump_manager.py:273` wrote to `dump_states` table. `dump_manager.py:328` posted the SELL order. If the post failed, the `dump_states` row was already persisted, creating an orphan if no cleanup followed.
- **Fix applied:** Took option 2 from the §3 proposal (preserves retry semantics). The save-before-post ordering is kept (legitimate retries still need the saved state), but the `DumpManager.dump_position` exception handler now distinguishes definitive failure ("orderbook does not exist") from transient failure (everything else). On definitive failure: mark unliquidatable + delete the dump_state row + clear `ms.dump_state[side]`. On transient: leave the row in place for next-cycle retry.
- **Commit:** `7d8d38d` on `main`
- **Verification:** `tests/test_unliquidatable_markets.py::TestDumpManagerMarkOnException::test_marks_unliquidatable_on_orderbook_gone` (asserts cleanup), `test_does_not_mark_on_transient_exception` (asserts retry preservation).
- **Related:** FX-007 (parent).
- **Hardening Phase:** 3.
- **History:**
  - 2026-05-18 — Shipped on `main` as part of `7d8d38d`.

---

### FX-028 — Re-probe mechanism for unliquidatable markets [FIXED]

- **Severity:** Low
- **Status:** Fixed (commit `7d8d38d`, 2026-05-18 — bundled with FX-007)
- **Tags:** `[ARCH]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** Once FX-007 would mark a cid unliquidatable, the cid would stay marked until manual intervention. If the mark was actually triggered by a transient Polymarket outage (rather than a true resolved orderbook), the bot would never retry.
- **Fix applied:** New `RewardFarmer._reprobe_unliquidatable` method, invoked from the main loop on a 30-min cadence (`_last_unliquidatable_reprobe` field). Inside the method, `db.get_unliquidatable_for_reprobe(stale_secs=RF_UNLIQUIDATABLE_REPROBE_SECS)` returns only cids whose `last_retry_at` is older than 6 h. For each, the method tries to find token_ids (first from `self.markets`, falling back to a CLOB `/markets/{cid}` lookup), then calls `get_merged_book`. Non-None book → un-mark via `db.delete_unliquidatable`. Still-dead or missing tids → `db.update_unliquidatable_retry` (stamps `last_retry_at` and leaves the row). Result: any cid whose orderbook ever returns to life gets automatically re-enabled on the next ~6h sweep.
- **Commit:** `7d8d38d` on `main`
- **Verification:** `tests/test_unliquidatable_markets.py::TestReprobeUnliquidatable::test_unmarks_on_healthy_book`, `test_stamps_retry_on_still_dead_book`, `test_skips_in_dry_run`, `test_no_op_when_no_stale_candidates`, plus `TestReprobeTokenIdFallback` for the CLOB-fallback branch (success + 404).
- **Related:** FX-007 (parent).
- **Hardening Phase:** 3.
- **History:**
  - 2026-05-18 — Shipped on `main` as part of `7d8d38d`.

---

### FX-013 — Capital-sizing race: `$1500` fallback active up to 30 min on cold start [FIXED]

- **Severity:** High
- **Status:** Fixed (commit `d4d1541`, 2026-05-18)
- **Tags:** `[BUG]` `[ARCH]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** On a fresh-DB LIVE start, the first `[GUARDRAIL]` JSON line showed `total_capital: 1500.0` even when the wallet held $201.35. Observed for ~30 min until oversight read the freshly-written `usdc_balance`. Safety thresholds (kill switch, notional ratio, cluster cap) were calibrated to $1500 during the window — kill switch at $150 = 75% of actual wallet, not the intended 10%.
- **Root cause:** Two-part race.
  1. Farmer wrote `usdc_balance` only every 10 cycles (~5 min), so the row didn't exist for the first ~5 min after LIVE cutover.
  2. Agent's `--capital` CLI default was `$1500.0`, silently used as the fallback whenever no fresh row was present.
- **Fix applied:**
  - **Farmer side**: writes `usdc_balance` on cycle 1 (in addition to every 10 cycles thereafter). Fresh-DB window between LIVE cutover and the first balance row drops from ~5 min to <30 s.
  - **Agent side**: `--capital` CLI default changes from `$1500.0` to `None`. The silent fallback is gone. Resolution flow is wallet-read → flag-override → skip-cycle (`status: "no_capital"`), each emitting a structured `[CAPITAL_SOURCE]` log line so the operator knows exactly which path fired.
  - We did NOT take the fixit doc's proposed "agent does its own SDK call" approach. The agent has no CLOB client today; adding one would expand the planner's responsibility profile and introduce auth/network dependencies. The chosen cycle-1-write + None-default approach reaches the same outcome via the existing farmer→DB→agent flow with strictly less surface area.
- **Commit:** `d4d1541` on `main` (subsumes FX-025).
- **Diff size:** +17/-3 lines in `reward_farmer.py` (cycle-1 write); +53/-21 lines in `oversight_agent.py` (resolution flow + `[CAPITAL_SOURCE]` log + arg help); +34/-10 lines in `oversight/data_collector.py` (None handling).
- **Verification:** `tests/test_capital_flow.py::TestComputeAvailableCapitalNoneHandling` (4 tests), `TestCliCapitalDefault::test_capital_flag_default_is_none`, `TestCapitalSourceLog` (3 tests covering each source code path), `TestFarmerWritesBalanceOnCycle1::test_cycle_1_branch_exists`.
- **Acceptance criterion (from §3 entry):** ✓ Fresh DB + LIVE start → first `[GUARDRAIL]` JSON has `total_capital ≈ wallet_balance`, not `$1500`. Verified by combined effect: cycle-1 write closes the 5-min window; agent skips the cycle on `source=none` rather than silently using $1500.
- **Related:** FX-010 (capital floor), FX-011 (dead config knobs), FX-024 (log line), FX-025 (CLI default — subsumed).
- **Hardening Phase:** 4.
- **History:**
  - 2026-05-18 — Shipped on `main` as `d4d1541`. Phase 4.

---

### FX-025 — `--capital` CLI default `1500.0` should be `None` [FIXED]

- **Severity:** Medium
- **Status:** Fixed (commit `d4d1541`, 2026-05-18 — subsumed by FX-013)
- **Tags:** `[ARCH]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** `oversight_agent.py:554` defined `--capital` default as `1500.0`. This was the silent-fallback half of the FX-013 race.
- **Fix applied:** Subsumed by FX-013. `--capital` now defaults to `None`; the agent's resolution flow uses the wallet-read first, then the explicit `--capital` override if provided, then skips the cycle. The argparse help text was rewritten to explain the new semantics. `compute_available_capital(total_capital=None)` returns `0.0` defensively rather than crashing on `None * 2`.
- **Commit:** `d4d1541` on `main`.
- **Verification:** `tests/test_capital_flow.py::TestCliCapitalDefault::test_capital_flag_default_is_none`.
- **Related:** FX-013 (parent).
- **Hardening Phase:** 4.
- **History:**
  - 2026-05-18 — Shipped as part of `d4d1541`.

---

### FX-024 — Inconsistent capital-source logging [FIXED]

- **Severity:** Low
- **Status:** Fixed (commit `d4d1541`, 2026-05-18)
- **Tags:** `[OPS]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** The pre-FX-024 log line `f"Exchange balance stale ({age_min:.0f}m old) — falling back to --capital=${capital:.0f}"` only fired on fallback. The normal success path emitted a different format ("Exchange USDC balance: $X (age=Ym)") and the no-balance path had no structured logging at all. Operator couldn't grep a single tag to see capital-source state across cycles.
- **Fix applied:** Every cycle now emits exactly one structured line:
  `[CAPITAL_SOURCE] source={usdc_db|flag|none} value=$X.XX age_min=Y [extra]`.
  - `source=usdc_db` (INFO) — fresh row present, agent uses it.
  - `source=flag` (WARNING) — operator override active, no fresh row; cycle proceeds with the explicit value.
  - `source=none` (WARNING) — no fresh row, no flag; cycle short-circuits with `{"status": "no_capital", "markets": 0}`.
- **Commit:** `d4d1541` on `main`.
- **Verification:** `tests/test_capital_flow.py::TestCapitalSourceLog` (3 tests, one per source code path).
- **Related:** FX-013 (parent), FX-025.
- **Hardening Phase:** 4.
- **History:**
  - 2026-05-18 — Shipped as part of `d4d1541`.

---

### FX-010 — `CAPITAL_FLOOR_USD` is absolute `$50`, not wallet-scaled [FIXED]

- **Severity:** Medium
- **Status:** Fixed (commit `d4d1541`, 2026-05-18)
- **Tags:** `[ARCH]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** `CAPITAL_FLOOR_USD = 50.0` was a hardcoded module-level constant. SafetyController fired I4 critical when `exchange_balance < $50`. On a $201 wallet that's a 25% drawdown floor; on a $1500 wallet 3.3%; on a $10k wallet 0.5%. Safety meaning differed wildly across wallet sizes.
- **Fix applied:** New helper `SafetyController._capital_floor(exchange_balance, portfolio_value)` returns `max($50, max(_portfolio_peak, portfolio_value, exchange_balance) * 0.10)`. I4 uses the helper instead of the absolute constant. The `$50` constant survives as `CAPITAL_FLOOR_USD` (now interpreted as a minimum) plus a new `CAPITAL_FLOOR_PCT = 0.10`. Reference uses the LARGEST of `(peak, portfolio, exchange)` so a drawdown doesn't shrink the floor as the wallet shrinks. Backwards-compatible: for wallets ≤ $500, the 10% scale never exceeds the $50 minimum (the Helsinki server's $201 wallet sees identical behaviour). The original literal `$50` is kept at `_query_last_known_balance`'s query filter — that's a fixed "had real money recently" operational sentinel, semantically distinct from the drawdown floor.
- **Commit:** `d4d1541` on `main`.
- **Verification:** `tests/test_capital_flow.py::TestCapitalFloorScaling` (6 tests across $200 / $500 / $1500 / $10000 references + peak-vs-current cases), `TestCapitalFloorI4FiresCorrectly` (3 tests including Test-16 backward compat).
- **Related:** FX-013.
- **Hardening Phase:** 4.
- **History:**
  - 2026-05-18 — Shipped as part of `d4d1541`.

---

### FX-011 — `RF_MAX_TOTAL_EXPOSURE` / `RF_MAX_COST_PER_MARKET` defined but unused [FIXED]

- **Severity:** Low
- **Status:** Fixed (commit `d4d1541`, 2026-05-18)
- **Tags:** `[ARCH]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** `config.py:233,238` defined `RF_MAX_COST_PER_MARKET: float = 50.0` and `RF_MAX_TOTAL_EXPOSURE: float = 1500.0`. Both had callable accessors in `reward_farmer.py:51-52` (`MAX_COST_PER_MARKET()`, `MAX_TOTAL_EXPOSURE()`) — neither was called by any production code. Dead config knobs that would confuse new contributors trying to tune capital.
- **Fix applied:** Took option B (delete). Both config constants and their accessors are gone. The v5.0 runtime guardrails (notional ratio, cluster cap, hard-enforcement multi-cancel, kill switch) own per-market and total exposure today, and the allocator's `MAX_PER_MARKET = $200` is the actual per-market cap. grep confirmed zero callers before deletion. A 4-line comment is left in place of each deletion explaining what was removed and why.
- **Commit:** `d4d1541` on `main`.
- **Verification:** `tests/test_capital_flow.py::TestDeadConfigKnobsRemoved` (3 tests: constants absent from config; accessors absent from reward_farmer).
- **Related:** FX-013.
- **Hardening Phase:** 4.
- **History:**
  - 2026-05-18 — Shipped as part of `d4d1541`.

---

### FX-014 — systemd units lack `KillSignal=SIGINT` + `TimeoutStopSec` [FIXED]

- **Severity:** Medium
- **Status:** Fixed (commit `91bae99`, 2026-05-18 — operator must re-tee unit blocks on the server to apply the new directives)
- **Tags:** `[OPS]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** `/etc/systemd/system/polymarket-farmer.service` and `polymarket-oversight.service` (per the architecture doc §11.11 install instructions) didn't set `KillSignal=SIGINT` or `TimeoutStopSec`. systemd defaulted to SIGTERM with a 90s grace, and the Python side only handled SIGINT — so `systemctl stop` left up to one full run_cycle of latency before the main loop noticed, potentially overrunning the grace window and getting SIGKILL'd with live orders still resting.
- **Fix applied:** Architecture doc §11.11 unit blocks now include three new directives in both services: `KillSignal=SIGINT` (matches the Python handler), `TimeoutStopSec=30` (tight grace for `_shutdown_cleanup`), `KillMode=mixed` (main process gets the signal directly; threads inherit `self._shutdown`). A new "Operational stop procedure" subsection documents the expected `journalctl` sequence on clean stop and explains the SIGKILL escalation case. The bot's Python-side SIGTERM handler (FX-015 below) means the directive change is forward-compatible: even without the operator re-tee'ing the units, `systemctl stop` (still SIGTERM by default) now triggers a clean shutdown.
- **Operator action required:** Re-run the `sudo tee` blocks from §11.11 of the architecture doc (or `sudo systemctl edit polymarket-farmer.service` and add the three lines under `[Service]`), then `sudo systemctl daemon-reload && sudo systemctl restart polymarket-farmer polymarket-oversight`. The bot will start using the new directives on next reload.
- **Commit:** `91bae99` on `main` (doc-only — the canonical units are operator-managed on the server, not in the repo since FX-017 removed the stale repo-side copy).
- **Verification:** Doc text matches the code's actual log lines; cross-checked by Phase 5 audit Q8.
- **Related:** FX-015 (the Python-side handler), FX-017 (cleaned up stale repo-side unit).
- **Hardening Phase:** 5.
- **History:**
  - 2026-05-18 — Shipped as part of `91bae99`.

---

### FX-015 — No signal handler for graceful shutdown in bot processes [FIXED]

- **Severity:** Medium
- **Status:** Fixed (commit `91bae99`, 2026-05-18)
- **Tags:** `[OPS]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** The §3 entry overstated this — `reward_farmer.run()` did register a SIGINT handler that set `self._shutdown = True`, and `oversight_agent.run_loop` already handled both SIGINT and SIGTERM. But: (a) `reward_farmer` only handled SIGINT, not SIGTERM (systemd's default), so `systemctl stop` only stopped the bot after Python's default SIGTERM behaviour produced a KeyboardInterrupt; (b) `_shutdown_cleanup`'s "kill-switch override" path was advertised but broken — `OrderLifecycle.cancel_order` had a hard `if self.dry_run: return True` shortcut that defeated the override, leaking orders in SHADOW; (c) `rate_limiter._RATE_LIMITED_METHODS` listed V1's `cancel` but not V2's `cancel_order` / `cancel_orders`, so 429 storms during shutdown bypassed the retry machinery.
- **Fix applied (5 changes in one commit):**
  1. `reward_farmer.run()` registers SIGTERM alongside SIGINT. The same handler routes both signals; logs `[SHUTDOWN] {SIGINT|SIGTERM} received — exiting at next cycle boundary` so journalctl shows which one fired.
  2. `_shutdown_cleanup` now uses the V2 SDK `cancel_orders` batch endpoint — one API call cancels every tracked order, fitting comfortably under `TimeoutStopSec=30` even at the worst-case 60-markets × 4 sides = 240 orders. Per-order `_gated_cancel_order` is the fallback when the batch call raises. Structured `[SHUTDOWN]` log lines at entry and exit with cancel counts.
  3. `OrderLifecycle.cancel_order` gains a `force: bool = False` parameter. When True, the dry_run shortcut is bypassed. `_gated_cancel_order` propagates `force=self._kill_switch_active` so the kill-switch and shutdown paths now actually do what the docstring claims.
  4. `rate_limiter._RATE_LIMITED_METHODS` expanded to cover every V2 SDK method production code calls — both cancel paths and read paths.
  5. `oversight_agent.run_loop`'s existing handler now emits structured `[SHUTDOWN]` log lines (was ad-hoc "Shutdown requested..." strings).
- **Comprehensive audit findings (all addressed pre-commit):** Phase 5 audit surfaced 3 real bugs that this entry's fix subsumes:
  - SHADOW/DRY kill-switch override didn't bypass OL's dry_run short-circuit — fixed by `force=True` propagation.
  - V2 SDK cancel methods missing from rate-limiter protected set — fixed by enumerating every V2 name.
  - Worst-case shutdown latency could exceed `TimeoutStopSec=30s` on a 60-market portfolio — fixed by switching to the batch endpoint.
- **Commit:** `91bae99` on `main`.
- **Verification:** `tests/test_shutdown.py` adds 22 tests across 5 classes covering signal-handler registration, batch-cancel happy path + fallback, force-execute behaviour, `[SHUTDOWN]` log assertions, OL cancel_order force flag, and rate-limiter V2 coverage.
- **Acceptance criterion (from §3 entry):** ✓ SIGINT or SIGTERM during a running cycle → cycle completes (loop boundary check), then orders are cancelled (batch call), then exit. No orphan orders left on Polymarket. Operator-visible signal via `[SHUTDOWN]` log lines in journalctl.
- **Related:** FX-014 (the systemd-side directive that makes `systemctl stop` use SIGINT).
- **Hardening Phase:** 5.
- **History:**
  - 2026-05-18 — Shipped as part of `91bae99`.

---

### FX-021 — Architecture doc §11.13 "exit path" claim incomplete [FIXED]

- **Severity:** Medium
- **Status:** Fixed (architecture doc edit, landed alongside Phase 1 + Phase 4 work; reconciled retrospectively 2026-05-18)
- **Tags:** `[DOC]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** Architecture doc §11.13 documented the bootstrap exit chain as a single chicken-and-egg (`portfolio_snapshots` write unblocks the state machine), but in practice I9 (`data_freshness`) fires independently and would keep the state machine in DATA_UNAVAILABLE even after `portfolio_snapshots` was written.
- **Fix applied:** §11.13 was rewritten in v5.1.10 (alongside `d4d1541`) to document the complete bootstrap exit chain. The current §11.13 enumerates four steps: (1) cold-start state = BOOTSTRAP, (2) I3 drawdown clears via `_is_genuine_cold_start()` skip or portfolio_snapshots write, (3) I9 data_freshness closed by `dd67f97` and refactored through the same helper, (4) BOOTSTRAP exit via ≥10 fills or ≥3 clean cycles. The paragraph that previously described the "remaining capital-sizing race" was also rewritten to note that race is closed in v5.1.10.
- **Commit:** No dedicated code commit — doc was updated as part of v5.1.7 (`541108b`) and v5.1.10 (`d4d1541`) amendment passes. Reconciled in fixit doc 2026-05-18.
- **Verification:** Architecture doc §11.13 line 3177 describes the full four-step bootstrap-exit chain; the v5.1.10 paragraph at line 3186 closes out the capital-sizing race noted there previously.
- **Related:** FX-001 (the I9 fix the doc now reflects), FX-002, FX-003 (BOOTSTRAP state the doc now references).
- **Hardening Phase:** Cross-phase doc accuracy (originally targeted at Phase 8).
- **History:**
  - 2026-05-18 — Doc edits landed organically alongside Phase 1 + Phase 4. Reconciled in fixit doc as part of post-Phase-5 sweep.

---

### FX-022 — Architecture doc references Ashburn as current production server [FIXED]

- **Severity:** Medium
- **Status:** Fixed (architecture doc edit, landed alongside Phase 1; reconciled retrospectively 2026-05-18)
- **Tags:** `[DOC]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** Architecture doc top-of-document v5.1.4 scope referred to the Hetzner CCX13 in Ashburn, with §11.13 verification blocks and §10.3 lessons all naming the Ashburn server. The Ashburn → Helsinki migration was real but not in the doc.
- **Fix applied:** v5.1.5 amendment block (alongside `dd67f97`) documented the Ashburn → Helsinki migration. "Current Production State" table was rewritten to show "Hetzner CCX13 in **Helsinki** (`hel1`, Finland)" as the production server. §11.4 candidate-list paragraph was replaced with a verified status table. Every subsequent v5.1.x amendment block (5.1.6 through 5.1.11) consistently refers to "the Helsinki server" in production-impact paragraphs.
- **Commit:** No dedicated code commit — doc-only edits in v5.1.5 amendment pass. Reconciled in fixit doc 2026-05-18.
- **Verification:** Architecture doc line 226 ("**Server deployment**" row in Current Production State table) reads "Hetzner CCX13 in **Helsinki** (`hel1`, Finland)". §11.4 verified-region table shows Helsinki as the only allowed location.
- **Related:** FX-020 (sibling — both updated in the v5.1.5 doc pass).
- **Hardening Phase:** Cross-phase doc accuracy (originally targeted at Phase 0 / 8).
- **History:**
  - 2026-05-18 — Reconciled in fixit doc as part of post-Phase-5 sweep.

---

### FX-023 — Orphan scan behavior undocumented in architecture doc [FIXED]

- **Severity:** Medium
- **Status:** Fixed (architecture doc edit, landed alongside Phase 1; expanded in Phase 3; reconciled retrospectively 2026-05-18)
- **Tags:** `[DOC]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-18
- **Original symptom:** `_scan_orphaned_positions` (then `_scan_for_orphans`) queried on-chain CTF balances and created `dump_states` rows for any non-zero positions — significant production behaviour that the architecture doc didn't describe anywhere.
- **Fix applied:** New §4.22 "Orphan position recovery" section added in v5.1.5 (alongside `dd67f97`). Section describes the scan trigger, what it queries, what it does with results, and the failure mode on resolved markets. The "Planned fix" subsection was rewritten in v5.1.9 (alongside `7d8d38d`) into "Shipped fix" describing the four-touchpoint `unliquidatable_markets` architecture (mark-on-exception, gate at every producer, cleanup cascade, periodic re-probe). Cross-references to §11 server-bringup notes added.
- **Commit:** No dedicated code commit — doc-only edits in v5.1.5 + v5.1.9 amendment passes. Reconciled in fixit doc 2026-05-18.
- **Verification:** Architecture doc §4.22 exists at line 1671; "Shipped fix (v5.1.9, `7d8d38d`)" subsection at line ~1700 documents the unliquidatable_markets mechanism end-to-end.
- **Related:** FX-007 (the bug §4.22 describes), FX-008 (sibling).
- **Hardening Phase:** Cross-phase doc accuracy (originally targeted at Phase 8).
- **History:**
  - 2026-05-18 — Reconciled in fixit doc as part of post-Phase-5 sweep.

---

### FX-016 — No dedicated test coverage for SafetyController [FIXED]

- **Severity:** High
- **Status:** Fixed
- **Tags:** `[TEST]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-19
- **Symptom:** `tests/` directory had 457 tests but no dedicated `test_safety_controller.py` (until Phase 1 seeded a 17-test stub). The 14 invariants, the state-transition logic, the bootstrap path, `filter_allocations`, persistence round-trip — almost all of it untested. Why FX-001's I9 deadlock wasn't caught before production.
- **Fix applied:** Phase 6 part 2 build-out across two commits + an audit-driven third commit:
  - `4aff918` — Blocks A+B+C+D (88 tests): per-invariant coverage (I1-I14 happy/breach/query-failure), state machine (permissions, upgrade ladder, UNSAFE auto-recovery slow + fast paths, `_transition` counter resets), `filter_allocations` end-to-end, `evaluate()` multi-violation precedence + backward-compat wrapper.
  - `f3630c9` — Blocks E+F+G (44 tests): `_persist_state`/`_load_state` age-branch round-trip + 100-row trim, query helpers (`_query_fill_damage` arithmetic, `_query_data_freshness` cold-start vs warm-DB-empty distinction, `_query_lifetime_fills_count`, `_query_last_known_balance`, `_compute_portfolio_value`, `_capital_floor` wallet-scaling), `confidence_score` per-component zeroing, public query methods, alert-file writers.
  - `1c4ae7e` — Audit-driven hardening (closes FX-029 + FX-030 surfaced by Phase 6 part 2 audit).
- **Coverage:** 58% → **94%** on `oversight/safety_controller.py` (525 → 530 stmts, 218 → 34 miss). The 34 remaining lines are defensive `except` handlers for DB-corruption scenarios that aren't reachable from unit-test fixtures (e.g., `_persist_state` lines 1162-1163, `_load_state` 1202-1207, `_write_alert_file` 1236-1237). All 14 invariants, the full state-machine ladder, `filter_allocations` 100% of branches, `_handle_upgrade` BOOTSTRAP + UNSAFE + standard paths, and all the helpers are now exercised. Target was ≥80%; cleared by 14 points.
- **Test count:** 522 (post-Phase-5) → 679 (post-FX-030) fast-tier across 3 commits. `tests/test_safety_controller.py` grew 17 → 152 tests. Local runtime ~78s. CI runs at every push (workflow shipped in v5.1.12).
- **Commit:** `4aff918` + `f3630c9` + `1c4ae7e`.
- **Verification:** All three commits' CI runs green on the Ubuntu 24.04 runner with Python 3.14. Coverage report attached to the commit-2 message.
- **Related:** FX-001 (the deadlock that motivated this); FX-026 (CI gates these tests on every push); FX-029 + FX-030 (the two bugs the new tests surfaced via the audit pass).
- **Hardening Phase:** 6 part 2 (final part).
- **History:**
  - 2026-05-18 — Opened.
  - 2026-05-19 — Shipped across 3 commits; both audit findings resolved pre-doc-lock.

---

### FX-029 — `filter_allocations` per-market $200 cap can be exceeded with mismatched caller input [FIXED]

- **Severity:** Medium
- **Status:** Fixed
- **Tags:** `[BUG]` `[SAFETY]`
- **Opened:** 2026-05-19 (surfaced by Phase 6 part 2 audit)
- **Closed:** 2026-05-19
- **Symptom:** Pre-fix `oversight/safety_controller.py:839-850` computed the per-market $200 scaling decision from the CALLER's `est_capital_cost` (`scale = 200 / input_est_cost`) but recomputed the post-cap value from an internal formula (`shares × est_price × 2`). When caller and internal formulas disagreed, the post-cap cost overshot $200. Audit's 4-line repro: `shares=500, est_capital_cost=300, max_spread=0.045` → final `est_capital_cost = $303.03`. Narrow spreads were worse: `max_spread=0.001, est_cost=201` → final $496.01. The cap is the LAST gate in `filter_allocations`, so the overshoot survives to the placement layer.
- **Root cause:** The cap was authored assuming callers would compute `est_capital_cost` with the same formula. The contract was implicit and unenforced.
- **Why it matters:** The whole point of the per-market cap is to bound single-market exposure. A broken cap means the SafetyController is silently failing to enforce the $200 ceiling — exactly the class of bug FX-001 hardened against (silent-violation of an invariant).
- **Fix applied:** Refactored `filter_allocations` per-market block (`1c4ae7e`). Both the scaling decision and the post-cap value now derive from the same internal formula `shares × est_price × 2`. Caller's `est_capital_cost` becomes informational only. min_size floor still wins by design (sub-min_size orders aren't accepted by the venue, so capping below min_size is operationally meaningless). +13 / -7 lines.
- **Commit:** `1c4ae7e` — "Close two audit-surfaced bugs in SafetyController (FX-029, FX-030)".
- **Verification:** Two new regression tests in `tests/test_safety_controller.py`: `test_per_market_cap_holds_with_mismatched_caller_est_cost` (audit's repro: shares=500, est_cost=300, spread=0.045) and `test_per_market_cap_holds_with_narrow_spread` (narrow-spread variant). Both pass post-fix; both would have failed pre-fix. The existing `test_per_market_over_200_scaled_down` rewritten to use the new contract (caller's est_cost is now informational).
- **Production impact:** Zero on Helsinki — the production allocator (`profit/allocator.py`, `oversight/allocation_writer.py`) computes `est_capital_cost` from the same `shares × est_price × 2` formula, so caller and controller agreed and the bug never fired. The fix is correctness hardening, not a behaviour change in practice. Any future caller refactor that diverges from this formula now safely funnels into the cap.
- **Related:** FX-016 (the test pass that surfaced this); FX-001 (the silent-invariant class of bug); the architecture doc §4.18 documents `MAX_PER_MARKET_EXPOSURE_USD = 200` as a runtime guardrail.
- **Hardening Phase:** 6 part 2 (audit findings).
- **History:**
  - 2026-05-19 — Surfaced by Phase 6 part 2 audit pass on the FX-016 test build-out.
  - 2026-05-19 — Shipped in `1c4ae7e` with 2 regression tests.

---

### FX-030 — SafetyController `_handle_upgrade` UNSAFE→MILDLY fast path bypasses documented DEGRADED auto-recovery cap [FIXED]

- **Severity:** Medium
- **Status:** Fixed
- **Tags:** `[BUG]` `[SAFETY]`
- **Opened:** 2026-05-19 (surfaced by Phase 6 part 2 audit)
- **Closed:** 2026-05-19
- **Symptom:** The architecture doc §10.3 / §4.14 / §7 (lines 1045 + 1919-1920) document the UNSAFE recovery contract as a 5-cycle minimum:
  > UNSAFE → (`UNSAFE_RECOVERY_CYCLES = 3`) → DEGRADED → (`UPGRADE_STEP = 2`) → MILDLY_MISCALIBRATED → ... → CALIBRATED
  But pre-fix, `_handle_upgrade`'s else-branch (`oversight/safety_controller.py:752`) caught UNSAFE alongside SEVERELY/DEGRADED/DATA_UNAVAILABLE and jumped it straight to MILDLY in `UPGRADE_STEP = 2` cycles when inputs were fully calibrated (`cf_raw` in zone, est/actual<5, ≥5 scoring markets, fd24 ≤ max(reward*2, 50)). The auto-recovery cap at `evaluate_state:644-652` only fires inside the `if violations` branch, so the clean-cycle path through `_handle_upgrade` went uncapped.
- **Root cause:** `UPGRADE_STEP` was authored for the standard severity ladder (SEVERELY/DEGRADED/DATA_UNAVAILABLE → MILDLY). UNSAFE wasn't carved out as a special case — the else-branch fell through to its else-clause unchanged when state == UNSAFE.
- **Why it matters:** UNSAFE is the bot's "proven risk" state. The 5-cycle minimum exit (3 cycles auto-recovery to DEGRADED + 2 cycles step-upgrade to MILDLY) is the documented operator-observation window for a recovering bot. The fast path collapsed it to 2 cycles, halving the safety margin and skipping the intermediate DEGRADED state entirely. The architecture doc had been authoritative on this; the code disagreed.
- **Fix applied:** Special-case UNSAFE at the top of `_handle_upgrade`'s post-BOOTSTRAP block (`1c4ae7e`): if `self.state == UNSAFE`, return immediately. The slow auto-recovery path in `evaluate_state:658-664` becomes the SOLE exit from UNSAFE on a no-violations cycle, restoring the documented 3+2 = 5-cycle minimum. +9 lines (mostly the explanatory comment).
- **Commit:** `1c4ae7e` — "Close two audit-surfaced bugs in SafetyController (FX-029, FX-030)".
- **Verification:** Two new regression tests: `test_unsafe_to_degraded_after_3_cycles_fully_calibrated` asserts that even with fully-calibrated inputs UNSAFE → DEGRADED requires 3 cycles (not 2 to MILDLY), and `test_full_recovery_unsafe_to_mildly_takes_at_least_5_cycles` pins the 5-cycle minimum end-to-end. The previously-passing `test_fast_path_unsafe_to_mildly_after_2_calibrated_cycles` (which pinned the BUG as a contract per the audit's note) was deleted.
- **Production impact:** Zero on Helsinki — the bot has been in BOOTSTRAP / MILDLY throughout, never UNSAFE. The fix tightens recovery semantics for any future UNSAFE event so the operator gets the documented graduated-response window. Subsequent UNSAFE → MILDLY transitions now take a guaranteed 5+ cycles instead of as few as 2.
- **Related:** FX-016 (the test pass that surfaced this); architecture doc §4.14 + §10.3 (the document the code was disagreeing with).
- **Hardening Phase:** 6 part 2 (audit findings).
- **History:**
  - 2026-05-19 — Surfaced by Phase 6 part 2 audit pass on the FX-016 test build-out. Audit explicitly identified this as "a real cap gap" with architecture-doc evidence.
  - 2026-05-19 — Shipped in `1c4ae7e` with 2 regression tests; the test that pinned the bug as a contract removed.

---

### FX-026 — No CI: tests don't run automatically on push [FIXED]

- **Severity:** Medium
- **Status:** Fixed
- **Tags:** `[OPS]` `[TEST]`
- **Opened:** 2026-05-18
- **Closed:** 2026-05-19
- **Symptom:** Repo had no `.github/workflows/` directory. Tests had to be run manually before each commit; any push could introduce regressions caught only on next server pull + restart.
- **Fix applied:** New `.github/workflows/test.yml` runs the fast-tier suite (`pytest tests/ --ignore=tests/test_simulation.py --tb=short`) on every push to `main` and every pull request. Single Python 3.14 job on `ubuntu-24.04`, pip cache keyed on `requirements.txt`, 15-minute timeout. New `README.md` carries the workflow status badge so build health is visible from the repo landing page.
- **Commit:** `a580bdb` — "Add GitHub Actions CI for fast-tier tests (FX-026)" (`.github/workflows/test.yml` + `README.md`, +54 lines).
- **Verification:** First CI run `26046878949` triggered by the push completed green in 7m17s. 544/544 fast-tier tests passed on the Ubuntu runner — matches local result. One Node.js 20 deprecation warning surfaces in the annotations (GitHub Actions runners deprecating Node 20 by 2026-06-02 and removing it by 2026-09-16); actions already at latest major versions (`checkout@v4`, `setup-python@v5`), monitor for action updates before the cliff.
- **Related:** FX-016 (CI gates the SafetyController coverage build-out to come). FX-027 indirectly (CI catches regressions, but not the 30-min/30-s process-lag class).
- **Hardening Phase:** 6 (first of two; FX-016 is the other).
- **History:**
  - 2026-05-18 — Logged.
  - 2026-05-19 — Shipped (`a580bdb`); first CI run green.

---

### FX-019 — `check_wallet.py` cosmetic 400 error on conditional asset query [FIXED]

- **Severity:** Low
- **Status:** Fixed
- **Tags:** `[BUG]`
- **Opened:** 2026-05-18 (originally documented in arch doc §10.3 v5.1.4 lessons)
- **Closed:** 2026-05-19
- **Symptom:** `python check_wallet.py` printed `[py_clob_client_v2] request error status=400 ... 'GetBalanceAndAllowance invalid params: assetId invalid value -1...'` at the top of its output. Harmless but alarmed first-time operators.
- **Root cause:** Line 243-246 called `client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL))` with no `token_id`. The SDK substituted `-1` as a placeholder and the API rejected it with a 400. The COLLATERAL call directly above worked correctly.
- **Fix applied:** Removed the 4-line CONDITIONAL block; replaced with a comment cross-referencing FX-019. The CONDITIONAL balance/allowance is checked at trade-time against a specific token_id; pre-trade with no token_id was a no-op anyway. Diagnostic still prints the COLLATERAL pUSD balance + allowances (the actually useful pre-trade check).
- **Commit:** `38fc63c` — "Close remaining hardening items: FX-019 + FX-027 (v5.1.14 — roadmap complete)".
- **Verification:** Syntax check `python -m py_compile check_wallet.py` clean. Full fast-tier 679/679 unaffected (no test covered the diagnostic script).
- **Risk:** None. The CONDITIONAL call was dead code from the operator's perspective.
- **Related:** —
- **Hardening Phase:** 0 (housekeeping; closed in the roadmap-closure pass).
- **History:** Architecture doc v5.1.4 amendment 7 documented this. Closed in v5.1.14 alongside FX-027.

---

### FX-035 — V2 SDK `get_order_book` returns dict; `get_merged_book` assumed object (THE ROOT CAUSE) [FIXED]

- **Severity:** Critical (silent, 4-day production blackout)
- **Status:** Fixed
- **Tags:** `[BUG]` `[SAFETY]` `[V2_SDK]`
- **Opened:** 2026-05-19 (surfaced by Helsinki recovery diagnostics)
- **Closed:** 2026-05-19
- **Symptom:** Helsinki bot **placed zero orders in production for the entire 4-day LIVE window** (2026-05-15 04:03 UTC → 2026-05-19 04:36 UTC). DB `orders_placed` table: 0 rows. The 4-day hardening campaign chased downstream symptoms (Tamilaga orphan-dump 400-spam → FX-007 family; dead-market over-marking → FX-032; capital-cap wholesale-reject → FX-031; oversight/farmer disagreement → FX-033 hypothesis), all of which were real bugs but none of them was the root cause. The actual root cause: `client.get_order_book(token_id)` in **py-clob-client-v2 v1.0.0 returns a `dict`** with string-valued `'bids'`/`'asks'` entries, but `market_discovery.py:get_merged_book` was written assuming an OrderBook **object** with `.bids` / `.asks` attributes (`getattr(ob, "bids", [])` returns `[]` on a dict because dicts don't expose keys as attributes).
- **How the bug manifested:** Every farmer cycle, for every market the bot tried to evaluate: `get_merged_book` → `client.get_order_book` returns dict → `getattr(dict, "bids", [])` returns `[]` → `all_bids` stays empty → `if not all_bids or not all_asks: return None` → returns `None` → `ms.book_failures += 1` → after 3 cycles per market, marked dead. **Every market always failed this check.** The bot couldn't place a single order because it couldn't fetch a single book.
- **Production verification of the bug:** On Helsinki at 2026-05-19 04:36 UTC, called `client.get_order_book(yes_tid)` directly for the Iran market (`0xd9933a54c518...`). Returned a `dict`: `{'market': '0xd9933a54c518...', 'asset_id': '...', 'timestamp': '1779165454000', 'hash': '4be2458ef8a1...', 'bids': [{'price': '0.02', 'size': '2250'}, ...], 'asks': [...]}`. `type=dict, truthy=True, getattr(ob, 'bids') → None`. Definitive.
- **Root cause classification:** This is **the V1→V2 SDK migration miss equivalent of B9 (`get_orders` → `get_open_orders`)** but in the book-fetching path. The V2 migration in commit `2a6baf6` (v5.1.2, 2026-04-29) changed `client.get_order_book`'s return shape, but `market_discovery.py:get_merged_book` was never updated to match. The bot ran in DRY mode for ~17 days after the V2 migration without surfacing this (DRY mode places no orders; the silent book-fetch failure didn't matter). First LIVE cutover on 2026-05-15 surfaced the I9 deadlock (FX-001) which masked everything else — DATA_UNAVAILABLE blocked all deploys regardless of what `get_merged_book` returned. The whole 4-day hardening campaign closed FX-001 + 30 downstream issues, finally cleared the deadlock chain, and then **the very first cycle where the bot actually tried to deploy** showed: `get_merged_book` still returns None, no order placed, 4 more days of $0 in production.
- **Why the FX-016 audit + 685 fast-tier tests missed it:** Every test that touches `get_merged_book` either:
  1. Mocks the function itself via `@patch("order_lifecycle.get_merged_book")` returning a pre-built dict result, OR
  2. Uses a stub client whose `get_order_book` returns an object with `.bids`/`.asks` attributes (matching the dead code path's assumption, not the real SDK's behaviour).
  
  No test ever called the real `get_merged_book` with the actual V2 SDK return shape. The smoke-test gap was: **production input shape was never exercised**. This is the canonical "tests pass for the wrong reason" failure mode — coverage tools count the function as covered, but coverage isn't correctness.
- **The discovery path:** Helsinki post-FX-032 was still at 0 orders placed. Diagnosis sequence (took ~30 min of focused triage):
  1. Verified alloc file had a deploy on cid `0xd9933a54c518...` (Iran market June 15)
  2. Direct HTTP probe via `requests.get('https://clob.polymarket.com/book?token_id=...')` returned HTTP 200 with deep books (active, accepting orders, paying rewards)
  3. Direct SDK call `client.get_order_book(yes_tid)` on Helsinki: returned `dict`, NOT an object
  4. Traced through `market_discovery.get_merged_book`: `getattr(dict, "bids", [])` → `[]` → return None
  5. Root cause confirmed: code-vs-SDK shape mismatch since v5.1.2 (April 29).
- **Fix applied:** New `_book_entries(ob, key)` helper in `market_discovery.py` normalizes both dict-form (V2 SDK) and object-form (test mocks) into `[(price, size), ...]` tuples. `get_merged_book` uses it for all four iteration sites (YES bids, YES asks, NO asks→bids, NO bids→asks). Backward-compat preserved — object-form mocks still work. `paper_trader_v2.py:get_merged_book` reduced to a delegation call to `market_discovery`'s implementation; `paper_client.py`'s fill simulator updated to use `_book_entries` directly. +335 / -72 lines across 4 files (most of the addition is the new `tests/test_get_merged_book.py` file).
- **Commit:** `647b1e2` — "Handle V2 SDK dict-return in get_merged_book (FX-035)".
- **Verification:** Two layers:
  1. **Unit:** new `tests/test_get_merged_book.py` (12 tests). Calls the real `get_merged_book` with both V2 SDK dict-form and object-form mock inputs. Includes the realistic Iran-market shape (bids/asks at $0.29/$0.31, mirror NO). Dict-form tests fail pre-fix, pass post-fix; object-form tests stay green throughout. CI green in 5m5s.
  2. **Production:** ran the patched `get_merged_book` inline on Helsinki against the live V2 SDK for the Iran June 15 market (cid `0xd9933a54c518...`) before pulling. Returned `bids=36 asks=46, midpoint=$0.4950, spread=$0.0100`. Definitively tradeable. Pre-fix this had been returning `None` on every farmer cycle for 4 days.
- **Production impact (after Helsinki pulls v5.1.17 and restarts):** Bot can finally fetch books. Combined with FX-031 (capital-cap scaling) and FX-032 (no spurious dead-marking), the bot can finally place orders, earn fills, and accrue rewards. From "live but dormant" to "live and farming" in one commit.
- **Lessons:**
  - **The hardening campaign's audit framework worked AS DESIGNED, but had a structural blind spot.** Every audit-surfaced bug (FX-029, FX-030, FX-032) was found by reading the architecture doc and looking for code-vs-doc divergence. FX-035 was found by running production against real inputs. **Code-level audits catch architectural drift; production diagnostics catch input-shape drift.** Both are necessary.
  - **A bug that hides behind another bug is invisible until the cover is removed.** The I9 deadlock (FX-001) prevented any deploys regardless of book-fetch behaviour. FX-035 was always there, but undetectable until FX-001 + FX-002/003 + FX-031 + FX-032 were all closed. The fact that we ran into FX-035 *immediately* after closing FX-032 means our recovery procedure worked — each layer of cover removed exposed the next bug.
  - **V1→V2 SDK migrations need a systematic audit of every wrapper function's return-shape assumptions.** B9 (`get_orders` → `get_open_orders`) was the first such miss caught. FX-035 is the second. There may be more. **Suggested follow-up (out of scope here):** sweep every `getattr(.*, "<field>")` against an SDK return value, normalize to use the dict-form accessor.
- **Related:** B9 / FX-009 (the V1→V2 sibling miss). All of FX-031 / FX-032 / FX-033 / FX-034 (which are now subsumed or made trivially recoverable by FX-035's fix). FX-016 (the test suite that should have caught this and didn't).
- **Hardening Phase:** Post-roadmap (the bug that ended the bug hunt).
- **History:**
  - 2026-04-29 — Introduced by the V2 SDK migration in commit `2a6baf6` (silent — DRY mode didn't expose it).
  - 2026-05-15 04:03 UTC — First LIVE cutover; bug active but masked by FX-001 (I9 deadlock).
  - 2026-05-15 → 2026-05-19 — 4-day hardening campaign closes FX-001 + 30 related bugs; bug still active in production, undetectable.
  - 2026-05-19 04:36 UTC — Surfaced via direct SDK probe on Helsinki during recovery diagnostics.
  - 2026-05-19 04:43 UTC — Shipped in `647b1e2` with 12 regression tests; CI green; production verified.

---

### FX-032 — Dead-market cleanup over-marks healthy cids as unliquidatable [FIXED]

- **Severity:** High
- **Status:** Fixed
- **Tags:** `[BUG]` `[SAFETY]`
- **Opened:** 2026-05-19 (surfaced by Helsinki recovery diagnostics)
- **Closed:** 2026-05-19
- **Symptom:** Empirical, on Helsinki's v5.1.14 farmer startup at 2026-05-19 03:23:38 UTC: 60 cids got flagged in `unliquidatable_markets` with reason `dead_market_book_failures`, all at the same instant — clearly a mass-marking event, not the gradual "3 failures over many cycles" the design intended. Direct probe of one of those cids (`0xdb22a7749b83`, the "Iran closes its airspace by May 27?" market): `active=True, closed=False, accepting_orders=True, end_date_iso=2026-05-27, rewards_rate=$200/day`, deep books on both sides (22 bids + 40 asks YES; mirror NO). FX-028's re-probe couldn't un-mark — `Unliquidatable re-probe: 0 un-marked, 60 still dead` immediately after the bot fetched 60 books that all returned HTTP 200 OK. **Helsinki lost access to a market paying $200/day in rewards.**
- **Root cause:** FX-006 cascaded `self.db.mark_unliquidatable(cid, reason="dead_market_book_failures")` into the dead-market cleanup at `reward_farmer.py:2093`. The cleanup fires when `ms.book_failures >= 3`, where `book_failures` is incremented whenever `get_merged_book` returns `None` or empty bids/asks. That check fires for a much wider class of conditions than the canonical FX-007 "orderbook does not exist" body — SDK parse errors, transient network blips, brief empty-book windows, rate-limit retry failures the wrapper swallows. The canonical FX-007 marking path (in `OrderLifecycle` and `DumpManager` exception handlers) only fires on the V2 SDK 400 with both `"orderbook"` AND `"does not exist"` substrings — the actual resolved-market signal. FX-006's cascade conflated transient-failure with resolved-market.
- **Why FX-016's audit missed it:** Every test in `TestDeadMarketCleanupCascade` was a "logic-shape replay" — the test re-constructed the loop body locally and asserted the side-effects, instead of exercising the actual `run_cycle` code. The test passed regardless of what the source did. Plus no test scenario had `mark_unliquidatable` over-fire for transient failures — the failure mode requires production-scale market churn to manifest.
- **Fix applied:** Removed the `self.db.mark_unliquidatable` call from `reward_farmer.py` Step 4b dead-market cleanup. FX-006 cascade for `delete_dump_state` (both sides) is preserved — that's what FX-006 was actually solving. Markets removed from `self.markets` here can still reappear via the next `_refresh_reward_markets` call and get another chance, which is appropriate for transient failure modes. The FX-007 canonical path catches genuinely-dead markets on the next placement attempt. +5 / -23 lines in `reward_farmer.py`. Test rewrite + new source-inspection test in `tests/test_unliquidatable_markets.py::TestDeadMarketCleanupCascade`: the new tests assert (a) cascade preserves `delete_dump_state` but calls no `mark_unliquidatable`, and (b) the actual `RewardFarmer.run_cycle` source code contains no `mark_unliquidatable` in its Step 4b block — catches future regression.
- **Commit:** `75d03c7` — "Stop dead-market cleanup from marking cids unliquidatable (FX-032)".
- **Verification:** 685/685 fast-tier (was 684, +1 net). CI green in 7m53s on Ubuntu runner. Direct API probe confirmed the Iran market is healthy and paying rewards.
- **Production impact:** After Helsinki pulls v5.1.16 + clears existing `unliquidatable_markets` rows: bot stops mass-marking healthy markets at startup; Iran-class markets ($200/day rewards) become deployable. The 61 stale entries currently in the table will be cleared one-time during the recovery; the new code won't recreate them.
- **Related:** FX-006 (the cascade that introduced the over-marking), FX-007 (the canonical path that remains), FX-028 (the re-probe that couldn't recover from this), FX-016 (the test suite that should have caught the test-replay deficiency), FX-033 (oversight allocator should also consult unliquidatable_markets), FX-034 (re-probe doesn't un-mark on healthy books).
- **Hardening Phase:** Post-roadmap follow-up (surfaced by Helsinki recovery diagnostics).
- **History:**
  - 2026-05-19 03:23:38 — Helsinki v5.1.14 farmer mass-marked 60 healthy markets.
  - 2026-05-19 ~03:44 — diagnosed via direct CLOB API probe + DB query.
  - 2026-05-19 04:12 — shipped in `75d03c7` with 2 regression tests.

---

### FX-031 — `filter_allocations` per-state capital cap wholesale-rejects oversized deploys [FIXED]

- **Severity:** High (structural — left bot at 0 deploys in BOOTSTRAP)
- **Status:** Fixed
- **Tags:** `[BUG]` `[SAFETY]`
- **Opened:** 2026-05-19 (surfaced empirically on Helsinki post-v5.1.14 restart)
- **Closed:** 2026-05-19
- **Symptom:** On the first oversight cycle after the Helsinki recovery pull to v5.1.14, the allocator proposed 3 deploys at $84-$89 each (sized for the full $201 available_capital). The SafetyController was in BOOTSTRAP (capital_pct=0.30 → $60 cap). The running-cost loop in `filter_allocations` wholesale-rejected all 3 because each individual `est_capital_cost` exceeded the cap (89 > 60). Log: `SafetyController [BOOTSTRAP]: 0/3 markets, $0/$201 capital`. Cycle complete: `markets_deploy: 0`. The bot was structurally unable to deploy in BOOTSTRAP — and would have continued so until BOOTSTRAP exited to MILDLY (~90 min), at which point only 1 of 3 deploys would still fit ($140 cap vs 2 × $89 = $178). Only CALIBRATED ($201 full cap) was unaffected.
- **Root cause:** Two coupled issues at `oversight/safety_controller.py:829-843` pre-fix:
  1. **Wholesale-reject semantics:** `if running_cost + est_cost > max_capital: a["action"] = "avoid"; ...; else: running_cost += est_cost`. Any deploy whose individual cost exceeded the cap was rejected entirely instead of scaled down. The probe-mode block at line 819 and the per-market exposure block at line 856 (FX-029) BOTH already use scale-down semantics; the running-cost block was the only wholesale-reject in `filter_allocations`.
  2. **Iteration order:** `deploys.sort(key=score, reverse=True)` runs earlier in the function, but the running-cost loop iterated the unsorted `allocations` list. So a low-score deploy at the front of `allocations` could starve the budget before the high-score deploy was reached. Only mattered under the wholesale-reject regime (when something actually fit, sort order was irrelevant); becomes load-bearing under the new scale-down regime.
- **Why it matters:** **This is exactly the FX-001 class of bug** — a silent invariant violation where the SafetyController's contract ("respect per-state capital_pct") is met but the spirit ("scale activity to fit the budget") is not. The hardening campaign's whole purpose was to defend against this class. The FX-016 test suite missed it because no test scenario had `individual_deploy_cost > per_state_cap`.
- **Fix applied:** Rewrote the running-cost block at `oversight/safety_controller.py:829-873` (commit `d5eabea`):
  1. **Scale shares down to fit `remaining` budget** instead of wholesale-reject. Both the scaling decision and the post-scale `est_capital_cost` recomputation use the same internal formula `shares × est_price × 2` — matching FX-029's contract. min_size floor still wins (sub-min orders are venue-rejected anyway).
  2. **Iterate `deploys` (already score-desc sorted)** so the highest-scoring market gets first claim on the constrained budget. Earlier filters that flipped `action` to "avoid" are honored via the existing `if a["action"] != "deploy": continue` guard.
  3. **Reject only when `remaining < min_cost`** — even min_size shares wouldn't fit. New reason string: "capital exhausted (${remaining:.0f} < min ${min_cost:.0f})". Distinguishes from the legacy "capital cap" rejection (which no longer fires).
- **Commit:** `d5eabea` — "Scale oversized deploys to fit per-state capital cap (FX-031)".
- **Verification:** Five new regression tests in `TestFilterAllocationsCapitalCapScaling`: oversized top-scorer scales to fit; subsequent deploys rejected as "capital exhausted" once budget drained; iteration order is score-desc regardless of input order; remaining < min_cost rejects cleanly; min_size floor respected even when remaining is smaller. Test count 679 → 684 fast-tier. Coverage on `safety_controller.py`: 539 stmts, 32 miss, 94% (added 5 stmts, all covered).
- **Production impact:** Closes the structural gap that left Helsinki at 0 deploys/cycle in BOOTSTRAP. Post-v5.1.15 pull on Helsinki, expect 1-3 deploys at ~$60 total (scaled top scorer + any that fit in remaining budget).
- **Related:** FX-029 (per-market $200 exposure cap — same scale-down pattern, different layer); FX-016 (the test suite that missed this); FX-003 (BOOTSTRAP entry which made this surface).
- **Hardening Phase:** Post-roadmap (surfaced by Helsinki recovery observation).
- **History:**
  - 2026-05-19 ~03:20 UTC — surfaced on Helsinki first cycle after v5.1.14 restart.
  - 2026-05-19 ~03:33 UTC — shipped in `d5eabea` with 5 regression tests.

---

## 5. Won't fix / Accepted risk

### FX-046 — Q-score reward model formula uncertain vs Polymarket actual payouts [ACCEPTED RISK]

- **Severity:** Medium → Accepted Risk
- **Status:** Accepted as unresolved 2026-05-28 (P3 of 9/10 plan)
- **Tags:** `[ARCH]` `[INVESTIGATION]`
- **Opened:** 2026-05-23
- **Accepted:** 2026-05-28
- **Symptom:** All 3 candidate formulas in the codebase / architecture doc (squared via `reward_tracker.q_score_order`, linear via architecture doc §4.23.1, and size-share via `market_q` accounting) under-predict actual Polymarket reward payouts by 24-94×. Live probes 2026-05-23 measured:
  - Market `0x0ed3f07970` ($50/day pool): predictions $0.012-0.035/day vs actual $1.24-4.87/day → 35-406× under-prediction
  - Market `0x475c9930` ($30/day pool): predictions $0.007-0.052/day vs actual $1.24-4.87/day → 24-70× under-prediction
- **Investigation outcome (research agent, 2026-05-28):** Three candidate explanations:
  1. **Polymarket's formula ≠ what the code implements.** Code uses squared; architecture doc §4.23.1 quotes linear (`reward_per_share_per_minute ∝ (1 − d/s_max)`). One of them is wrong. Linear formula correction would close ~3× of the gap, not the remaining 8-30×.
  2. **`market_q` over-counts competition.** `estimate_market_q` sums all visible in-zone bids+asks. Polymarket may exclude orders below `min_size` (~20 shares per live probe) or apply filters the bot doesn't observe. Most likely explanation for the residual 8-30× gap.
  3. **Market state evolved between accrual and snapshot.** The queue depth visible today may have been 100× thinner when yesterday's reward accrued. Probable but unmeasurable without historical book snapshots.
  4. **Asymmetric maker/taker counting.** Polymarket may weight only certain order types (resting maker only, not cross-book arb). Plausible but no evidence either way.
- **Why we're not fixing:** No clean code change disambiguates which explanation (or combination) is correct. The architecture doc claims linear but doesn't cite Polymarket's source. There's no formal Polymarket reward-formula spec in the repo or dependencies. Empirical reconciliation against historical `book_snapshots` table would take ≥7 days of post-G1 production data to be statistically meaningful, and would still be one observational study against one wallet on one strategy. **Better to accept the uncertainty and design the system to be robust to it** than ship a wrong formula change that introduces a new failure mode.
- **Mitigations in place:**
  1. **API q_share is ground truth.** `/rewards/user/percentages` returns Polymarket's own per-market q_share measurement. Priority 0 in `estimate_q_share` uses API value when available — no formula assumption needed for currently-held markets.
  2. **Conservative q_share margin (cfg knob, P3).** New `RF_OVERCOMMIT_Q_SHARE_CONSERVATIVE_FACTOR` (default 1.0 = no-op) applied to NON-API q_share estimates. Operators concerned about over-deployment can set to 0.5 → halve expected_reward from cumulative + cold-start sources → EV gate tightens → fewer deploys but more confidence in each.
  3. **FX-051 per-market cooldowns** catch markets that turn into losers regardless of formula accuracy. Worst case if the formula's wrong direction is "over-predict" (i.e., q_share lower than thought): bot deploys, takes losses, FX-051 cools the market within 24h. Worst case if direction is "under-predict": bot under-deploys, observable in `[OVERCOMMIT_ALLOC]` log, operator raises `RF_OVERCOMMIT_MIN_DAILY_RATE_USD` floor or lowers `RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC` to capture more markets.
  4. **FX-045 Priority 1 demotion (already shipped).** The Priority 1 over-estimation that was firing I6 SEVERELY is closed — bot no longer assumes worst-case q_share for the kill-switch trigger.
  5. **G1 7-day clean run (P7)** will produce 7 days of (predicted_reward, actual_reward) pairs. If a clear winning formula emerges from that data, open FX-060 to switch code; if not, the conservative-margin approach remains the answer.
- **What success looks like (if we re-open):** ≥30 days of post-G1 production data with bot deployed on 50+ markets per cycle. Per-market reconciliation `(predicted, actual)` pairs analysed for systematic bias. If a single formula explains ≥80% of variance, switch. Otherwise, keep accepted risk.
- **Related:** FX-045 (Priority 1 demotion, already shipped); FX-047 (I6 threshold recalibration — likely obviated post-FX-045 as ratio shifted from 8-40× to 0.005-0.027×); FX-052/053 OverCommitAllocator (uses the q_share resolution chain this entry concerns).
- **History:**
  - 2026-05-23 — Opened during FX-045 investigation when live probes showed the 24-94× gap.
  - 2026-05-28 — Research agent (in 9/10 plan drafting) confirmed no code change cleanly resolves the cause. Formally accepted with conservative-margin cfg knob as the practical mitigation. **Empirical reconciliation deferred to post-G1 (P7).**

---

### FX-027 — Process-boundary lag (agent 30 min, farmer 30 s) [ACCEPTED]

- **Severity:** Low (intentional architectural choice)
- **Status:** Accepted as designed risk
- **Tags:** `[ARCH]`
- **Opened:** 2026-05-18
- **Accepted:** 2026-05-19
- **Symptom:** The oversight agent writes the allocation file once per ~30 min; the farmer reads it every 30 s. Worst-case lag between safety-state re-evaluation and farmer execution is one agent cycle (~30 min).
- **Decision rationale:** The 30-min/30-s asymmetric cadence is intentional design (architecture doc §2 + §4.21.6). The actually time-critical safety responses live on the **farmer's 30-s cadence**:
  - Runtime guardrails (notional cap, cluster cap, kill switch on 24h-loss / CF / fill-rate spike) evaluate every farmer cycle (§4.18).
  - Order placement / cancellation gates fire at farmer cadence.
  - SafetyController's `filter_allocations` runs at allocation-write time (agent cadence) but the state itself is consumed by the farmer every 30 s via the allocation file.
- **What the agent's 30-min cadence affects:** allocation **revisions** (which markets to deploy into, scaled capital_pct), not allocation **enforcement** (the actual cap-aware shaping + safety filter happens at write-time and is enforced by the farmer on every read). A failure scenario where the agent's response lags the farmer's exposure growth would need the farmer's guardrails to also be silent — which Phase 4's wallet-first capital flow and Phase 3's dump-state lifecycle hardening have closed.
- **Mitigations already in place:**
  - Farmer-side notional/cluster caps + kill switch (§4.18) — react in seconds, not minutes.
  - `oversight_agent.evaluate(guard)` hook (§4.21) — Stage 2/3 promotion adds pause/kill actions on the farmer side, gated by the agent but **executed** by the farmer.
  - SafetyController persistence (`safety_state` table) — state survives agent restarts; farmer always reads the latest allocation file.
- **What "accept" means in practice:** No code change. The 30-min agent cadence stays. If a pathological scenario emerges that the farmer-side guardrails can't bound, FX-027 reopens with a specific scenario + targeted mitigation (e.g., reduce agent interval to 10 min, or migrate a specific evaluator to the farmer side).
- **Related:** FX-015 (graceful shutdown — orthogonal; closed in v5.1.11). FX-018 in §4.18 (runtime guardrails — the primary mitigation).
- **Hardening Phase:** 9 (continuous improvement — review if a real pathological scenario emerges).
- **History:**
  - 2026-05-18 — Logged as "Accepted Risk candidate".
  - 2026-05-19 — Explicitly accepted in v5.1.14 with this rationale; closes the hardening roadmap.

---

## 6. Hardening roadmap

This section sequences the open issues into a deliverable plan. Updated whenever priorities shift.

### Vision: target state

| Property | Today | Target |
|---|---|---|
| Bootstrap from fresh DB → first deploy | manual unblock needed (FX-001 partially closes; FX-002/003 still open) | autonomous within 30 min |
| Counter/DB consistency | counters lie (FX-004) | counters match DB row-counts |
| Failed placement handling | spam forever (FX-005/006/007) | mark unliquidatable after 1-3 attempts |
| Capital sizing on cold start | $1500 fallback for ≤30 min (FX-013) | wallet balance from cycle 1 |
| Safety threshold scaling | absolute $50 floor (FX-010) | scales with wallet |
| SafetyController test coverage | ~0% direct (FX-016) | ≥80% line coverage |
| Graceful shutdown | unsafe (FX-014/015) | orders cancelled before exit |
| Architecture doc accuracy | stale (FX-020/021/022/023) | reflects observed behavior |

### Phase 0 — Immediate (~1h) — COMPLETE

- ✅ **FX-001 patch shipped** — commit `dd67f97`
- ✅ **FX-017** — stale `polymarket-bot.service` deleted from repo root — commit `3f50441` (2026-05-18)
- ✅ **FX-018** — `numpy>=2.0` added to `requirements.txt` — commit `987a844` (2026-05-18)
- ✅ **FX-020** — architecture doc §11.4 verified geoblock table — shipped alongside `dd67f97` (v5.1.5); reconciled in fixit doc 2026-05-18

### Phase 1 — Complete SafetyController bootstrap (~4h) — COMPLETE

- ✅ **FX-002** — I3 drawdown handles genuine cold start (skips violation when `orders_placed` AND `fills` both empty) — commit `dc78ba0` (2026-05-18)
- ✅ **FX-003** — `BOOTSTRAP` state added (10 markets, 30% capital, trials=True), inserted between `SEVERELY` and `MILDLY` in severity ordering — commit `541108b` (2026-05-18)
- ✅ **FX-012** — Cold-start default routed through `_cold_start_or(MILDLY)` helper; fresh DB → BOOTSTRAP, warm restart → MILDLY (subsumed by FX-003) — commit `541108b` (2026-05-18)
- Acceptance reached: fresh DB → BOOTSTRAP for ≤3 cycles or ≤10 fills → MILDLY → CALIBRATED via existing upgrade ladder. 453/453 fast-tier tests pass (was 443 pre-Phase-1, +10 new Phase 1 tests).

### Phase 2 — Counter / DB consistency (~3h) — COMPLETE

- ✅ **FX-004** — `orders_placed` counter accumulates the return value of `OrderLifecycle.place_orders_for_market` — commit `e7fc3d2` (2026-05-18)
- Acceptance reached: 17 new tests in `tests/test_order_lifecycle.py` verify return-value semantics (0/1/2) and accumulator behaviour; full fast tier 453 → 470 (no regressions).

### Phase 3 — Dump-state lifecycle correctness (~10h, riskiest) — COMPLETE

- ✅ **FX-007** — `unliquidatable_markets` table + 6 BotDatabase methods + DumpManager/OrderLifecycle gates + orphan/sync/restore gates + dead-market cleanup cascade — commit `7d8d38d` (2026-05-18)
- ✅ **FX-005** — Subsumed by FX-007. OrderLifecycle marks unliquidatable on canonical 400; the new gate filters the cid on subsequent cycles, strictly better than the original "retire after 3 attempts" proposal.
- ✅ **FX-006** — Dead-market cleanup loop now calls `delete_dump_state` on both sides + `mark_unliquidatable` with reason `dead_market_book_failures` — commit `7d8d38d`
- ✅ **FX-008** — `_restore_dump_states` gates each row on `is_unliquidatable(cid)` and silently deletes stale dump_state rows for marked cids — commit `7d8d38d`
- ✅ **FX-009** — `DumpManager.dump_position` exception handler distinguishes definitive ("orderbook does not exist") from transient failure; definitive failures clean dump_state + mark unliquidatable + clear `ms.dump_state[side]` — commit `7d8d38d`
- ✅ **FX-028** — `RewardFarmer._reprobe_unliquidatable` runs on a 30-min loop-sweep with per-cid 6h staleness gating; un-marks cids whose `get_merged_book` returns data, stamps `last_retry_at` otherwise — commit `7d8d38d`
- Acceptance reached: Tamilaga error spam will stop within ~1 cycle of the next Helsinki `git pull + restart`. 31 new tests in `tests/test_unliquidatable_markets.py` exercise every integration point. Comprehensive code-review audit surfaced 4 findings, all fixed pre-commit.

### Phase 4 — Capital flow correctness (~3h) — COMPLETE

- ✅ **FX-013** — farmer writes `usdc_balance` on cycle 1; agent `--capital` default `None`; silent `$1500` fallback removed — commit `d4d1541` (2026-05-18)
- ✅ **FX-025** — Subsumed by FX-013. `--capital` defaults to `None`; argparse help rewritten — commit `d4d1541`
- ✅ **FX-010** — `SafetyController._capital_floor(exchange_balance, portfolio_value)` returns `max($50, max(peak, portfolio, exchange) * 0.10)`. I4 uses helper; absolute $50 kept as minimum + as the `_query_last_known_balance` sentinel — commit `d4d1541`
- ✅ **FX-011** — `RF_MAX_TOTAL_EXPOSURE` + `RF_MAX_COST_PER_MARKET` deleted from `config.py` + accessors deleted from `reward_farmer.py` — commit `d4d1541`
- ✅ **FX-024** — Per-cycle `[CAPITAL_SOURCE] source={usdc_db|flag|none} value=$X.XX age_min=Y` log line — commit `d4d1541`
- Acceptance reached: pre-fix `[GUARDRAIL]` JSON showed `total_capital: 1500.0` on cold start; post-fix, cycle-1 write closes the 5-min window and the agent reads `~$201` from `usdc_balance` instead. 21 new tests in `tests/test_capital_flow.py`. Comprehensive code-review audit ran after initial implementation; no code findings.

### Phase 5 — Operational hardening (~2h) — COMPLETE

- ✅ **FX-014** — Architecture doc §11.11 unit blocks now include `KillSignal=SIGINT`, `TimeoutStopSec=30`, `KillMode=mixed`; new "Operational stop procedure" subsection documents expected `journalctl` sequence. Operator must re-tee on the server to apply (doc-only commit; Python-side SIGTERM handler makes the change forward-compatible). — commit `91bae99` (2026-05-18)
- ✅ **FX-015** — `reward_farmer.run()` handles SIGTERM alongside SIGINT; `_shutdown_cleanup` uses V2 batch `cancel_orders` endpoint (with per-order fallback); `OrderLifecycle.cancel_order` gains `force=True` parameter to bypass dry_run shortcut on kill-switch override; rate-limiter coverage expanded for V2 SDK names; structured `[SHUTDOWN]` log lines. — commit `91bae99` (2026-05-18)
- Acceptance reached: `systemctl stop polymarket-farmer` (post-§11.11 update + restart) issues SIGINT → `_sig` handler flips `_shutdown` → main loop exits → `_shutdown_cleanup` issues one `cancel_orders` batch call → exit. Worst-case latency: ~one run_cycle (60s) for the loop boundary, then sub-second for the batch cancel. Fits comfortably under `TimeoutStopSec=30` for the cleanup itself. Phase 5 audit surfaced 3 real bugs (kill-switch override broken in SHADOW; V2 SDK names missing from rate-limiter; latency cliff at 60+ markets) — all addressed pre-commit. 22 new tests in `tests/test_shutdown.py`.

### Phase 6 — Test coverage build-out (~12h) — COMPLETE

- ✅ **FX-016** — Dedicated SafetyController test suite — shipped `4aff918` + `f3630c9` (v5.1.13); 17 → 152 tests; coverage 58% → 94%
- ✅ **FX-026** — GitHub Actions CI on push — shipped `a580bdb` (v5.1.12); first run 26046878949 green in 7m17s
- ✅ **FX-029** — `filter_allocations` per-market cap bypass — shipped `1c4ae7e` (audit-surfaced)
- ✅ **FX-030** — `_handle_upgrade` UNSAFE→MILDLY fast-path bypass — shipped `1c4ae7e` (audit-surfaced)
- Other test additions: DumpManager, orphan-scan, fresh-DB bootstrap, counter consistency
- Acceptance: ≥80% coverage on safety_controller.py + dump_manager.py + order_lifecycle.py; CI green

### Phase 7 — Audit remaining subsystems (~12h) — DEFERRED (backlog)

Subsystems not covered by the original hardening audit. No specific FX-NNN entries yet — items move into §3 as they're discovered. The full Phase 0-6 sweep didn't surface unactioned regressions in these areas, so deferred to "when a real signal emerges":

- Calibration models (fill/loss/reward) on cold DB
- BanditLayer initialization
- LearningController β/η update race conditions
- V2 SDK boundary signature audit (analogous to the get_orders V1→V2 miss)
- WAL ordering and cross-process read consistency

### Phase 8 — Architecture doc refresh to v5.2 (~3h) — COMPLETE

- ✅ **FX-019** — `check_wallet.py` 400 error closed in v5.1.14 (removed dead CONDITIONAL query)
- ✅ **FX-021** — Correct §11.13 "exit path" claim — closed organically by v5.1.10 §11.13 rewrite
- ✅ **FX-022** — Update server references Ashburn → Helsinki — closed organically by v5.1.5 Amendments block + Current Production State rewrite
- ✅ **FX-023** — Add §4.X orphan scan behavior documentation — closed organically by v5.1.5 new §4.22, expanded in v5.1.9
- v5.2 amendment block landed in-place across v5.1.5 → v5.1.13 (8 minor versions on top of v5.1.4)

### Phase 9 — Continuous improvement (ongoing) — ROADMAP CLOSED + POST-ROADMAP FOLLOW-UPS

- ✅ **FX-027** — Process-boundary lag accepted as designed risk in v5.1.14; moved to §5 with explicit mitigation note.
- ✅ **FX-031** — Capital-cap scaling shipped in v5.1.15 (`d5eabea`).
- ✅ **FX-032** — Dead-market cleanup over-marking shipped in v5.1.16 (`75d03c7`).
- ✅ **FX-035** — V2 SDK dict-return root cause shipped in v5.1.17 (`647b1e2`) — bot finally placing real orders.
- ✅ **FX-036** — Queue-depth-aware placement shipped in v5.1.18 (`8152a8b`); runtime-disabled via `config_overrides.json` until FX-041 ships.
- ✅ **FX-040** — Cold-start trial-mode sizing shipped in v5.1.19 (`c2c21d7`) — first Phase 1 fix from the 2026-05-19 cascade.
- ✅ **FX-041** — Two-sided book-depth check shipped in v5.1.20 (`3534cb5`) — prerequisite for safely re-enabling FX-036 in production. Operator removes `"RF_TARGET_QUEUE_AHEAD_USD": 0` from Helsinki's `config_overrides.json` to re-enable.
- 🔲 **FX-033** — Oversight allocator should consult `unliquidatable_markets` (Low, subsumed by FX-035).
- 🔲 **FX-034** — `_reprobe_unliquidatable` doesn't un-mark on healthy books (Low, subsumed by FX-035).
- Daily journal review for new failure-mode signatures → new fixit entries (ongoing)
- Monthly: prune dead config knobs
- Quarterly: re-audit production paths

### Phase 10 — Friend rollout (added 2026-05-21, gates derived from §0.1 P5)

The bot is currently single-operator (the dev wallet on Helsinki). The path to multi-operator rollout is gated on seven verifiable conditions. **No friend turns on `--mode live` until ALL seven are green.** Re-evaluate weekly until ready.

| Gate | Verification command/check | Current status (2026-05-21) |
|---|---|---|
| **G1** — Bot has run **7 days** clean on dev wallet | `journalctl -u polymarket-farmer --since "7 days ago" \| grep -E "CRITICAL\|kill_switch.*true"` returns 0 lines | ❌ Only 19h post-FX-041 deploy |
| **G2** — All HIGH-severity open `FX-NNN` items shipped | fixit §3 has 0 entries with severity High | ❌ FX-037 still open (silent-corruption-blocking) |
| **G3** — SafetyController observed in CALIBRATED for ≥24h | `SELECT state, COUNT(*) FROM safety_state WHERE ts > now-86400 GROUP BY state` shows CALIBRATED dominant | ❌ Currently SEVERELY_MISCALIBRATED. **NOTE: blocked by FX-044** — until the morning UTC-boundary I6 spike is fixed, CALIBRATED is structurally unreachable for ≥24h. G3 effectively becomes "either FX-044 ships AND CALIBRATED ≥24h, OR G3 relaxes to DEGRADED-or-higher ≥24h with no UNSAFE events". Decision deferred until FX-044 observation completes. |
| **G4** — At least one fill+dump cycle with slippage ≤ 3% | `SELECT MAX(ABS(pnl/usd_value)) FROM unwinds WHERE ts > now-7d` < 0.03 | ❌ Zero fills observed post-FX-041 |
| **G5** — FX-036 firing on ≥3 distinct deep markets without cascade | book probe + placement comparison on 3+ different cids | ❌ Only 1 market (`0x475c9930`) so far |
| **G6** — Operator runbook written | review of `OPERATOR_RUNBOOK.md` (file does not yet exist) | ❌ Not started |
| **G7** — Wallet recovery procedure tested | tabletop exercise documented in runbook | ❌ Not done |

**Rollout sequence (after gates pass):**
- **Cohort 1:** ONE friend. Same code, their own server, their own wallet. They run `--mode dry` ≥24h → `--mode shadow` ≥24h → `--mode live`. We monitor both servers for ≥7 days.
- **Cohort 2:** 2-3 more friends. Only after cohort 1 has been clean for ≥7 days.
- **Wider opening:** only after cohort 1 + 2 have collectively logged 30+ bot-days clean.

**Decision points along the way:**
- If any gate fails after the 7-day window starts, reset the window. G1 is a continuous condition; G3 is a continuous condition.
- If cohort 1 hits a `kill_switch` event, cohort 2 starts ONLY after the root cause is shipped + verified on dev wallet for another 7 days.

---

## 7. Architecture-doc update tracking

This section lists explicit changes that need to land in `Polymarket bot architecture v5.1.md` once corresponding fixes ship, OR independently of fixes (pure doc corrections).

**Status of items targeted at v5.1.20 (FX-041 two-sided book-depth check — prerequisite for re-enabling FX-036):**

| Architecture-doc section | Change needed | Driving fixit ID | Status |
|---|---|---|---|
| Top-of-doc v5.1.x amendments | Add v5.1.20 scope block — FX-041 closure + production re-enable instructions for FX-036 | FX-041 | ✅ Done |
| HEAD chain on line 9 | Extended to v5.1.19 → v5.1.20 (`3534cb5`) | FX-041 | ✅ Done |
| Current Production State | Header v5.1.20; note FX-036 now safely re-enabled via FX-041 | FX-041 | ✅ Done |
| Companion paragraph | Reflect FX-041 closure; remove FX-041 from "open" list | FX-041 | ✅ Done |
| §4.23 Order placement strategy | Add §4.23.7 documenting the two-sided dump-depth check | FX-041 | ✅ Done |
| §8.1 RF config table | Add `RF_DUMP_DEPTH_SAFETY_FACTOR` row | FX-041 | ✅ Done |
| §10.1 commit list | Add `3534cb5` row at top with FX-041 narrative | FX-041 | ✅ Done |
| §10.2 Known-fixed bugs | Add B24 (FX-041 asymmetric-book trap) | FX-041 | ✅ Done |

**Status of items targeted at v5.1.17 (THE ROOT CAUSE — FX-035 V2 SDK dict-return) + FX-036 design doc:**

| Architecture-doc section | Change needed | Driving fixit ID | Status |
|---|---|---|---|
| Top-of-doc v5.1.x amendments | Add v5.1.17 block — the root-cause fix discovery sequence + lessons | FX-035 | ✅ Done |
| HEAD chain on line 9 | Extended to v5.1.15 → v5.1.16 → v5.1.17 | FX-035 | ✅ Done |
| Current Production State | Header v5.1.17 at `647b1e2`; Fast-tier 685 → 697; "bot is actually farming rewards as of 2026-05-19 04:58 UTC" | FX-035 | ✅ Done |
| Companion paragraph | Reflect FX-035 root-cause closure + FX-036 open + FX-033/FX-034 downgraded | FX-035, FX-036 | ✅ Done |
| §10.1 commit list | `647b1e2` row at top with full root-cause narrative | FX-035 | ✅ Done |
| §10.2 Known-fixed bugs | B23 (FX-035) added — V1→V2 SDK migration miss in book-fetching | FX-035 | ✅ Done |
| §10.3 Operator notes | Lessons captured in v5.1.17 amendment block (coverage isn't correctness; production diagnostics; SDK migration sweep recommendation) | FX-035 | ✅ Done |
| **NEW §4.23** | Order placement strategy — reward-farming positioning (Polymarket reward formula, current vs proposed placement, FX-036 algorithm, capital_pct interaction, verification plan) | FX-036 | ✅ Done |

**Status of items targeted at v5.1.16 (Helsinki-recovery-surfaced FX-032):**

| Architecture-doc section | Change needed | Driving fixit ID | Status |
|---|---|---|---|
| Top-of-doc v5.1.x amendments | Add v5.1.16 block documenting commit `75d03c7` | FX-032 | 🔲 Pending |
| Current Production State | HEAD bump to `75d03c7`; Fast-tier tests 684 → 685; note FX-032 + FX-031 + FX-029 + FX-030 all in same family (post-FX-016-audit) | FX-032 | 🔲 Pending |
| Companion paragraph | Reflect FX-032 + open FX-033/FX-034 follow-ups; revise "all closed" framing | FX-032 | 🔲 Pending |
| §10.1 commit list | Add `75d03c7` row at top | FX-032 | 🔲 Pending |
| §10.2 Known-fixed bugs | Add B22 (FX-032 over-marking) | FX-032 | 🔲 Pending |
| §10.3 Operator notes | Add Helsinki recovery sequence (pull + clear unliquidatable + restart) as a known operational procedure | FX-032 | 🔲 Pending |

**Status of items targeted at v5.1.15 (Helsinki-recovery-surfaced FX-031):**

| Architecture-doc section | Change needed | Driving fixit ID | Status |
|---|---|---|---|
| Top-of-doc v5.1.x amendments | Add v5.1.15 closure-followup block documenting commit `d5eabea` | FX-031 | ✅ Done |
| Current Production State | HEAD bump to `d5eabea`; Fast-tier tests 679 → 684 | FX-031 | ✅ Done |
| Companion paragraph | Note FX-031 (post-roadmap fix surfaced by Helsinki recovery observation) | FX-031 | ✅ Done |
| §10.1 commit list | Add `d5eabea` row at top | FX-031 | ✅ Done |
| §10.2 Known-fixed bugs | Add B21 (FX-031 wholesale-reject) | FX-031 | ✅ Done |
| §4.18 / §4.14 cross-reference | Note the capital-cap scaling contract | FX-031 | ✅ Covered in v5.1.15 scope block |

**Status of items targeted at v5.1.14 (Hardening roadmap closure — FX-019 fix + FX-027 acceptance):**

| Architecture-doc section | Change needed | Driving fixit ID | Status |
|---|---|---|---|
| Top-of-doc v5.1.x amendments | Add v5.1.14 closure block | FX-019, FX-027 | ✅ Done |
| Current Production State | HEAD bump to v5.1.14 commit; note hardening complete | FX-019, FX-027 | ✅ Done |
| Companion paragraph | Reflect FX-019 + FX-027 closure; remove the "X open items" sentence | FX-019, FX-027 | ✅ Done |
| §10.1 commit list | Add v5.1.14 row at top | FX-019, FX-027 | ✅ Done |
| §10.2 Known-fixed bugs | Add B20 (FX-019 dead CONDITIONAL query) | FX-019 | ✅ Done |
| §10.3 (or new subsection) | Document FX-027 acceptance rationale + cross-reference §4.18 mitigations | FX-027 | ✅ Done |

**Status of items targeted at v5.1.13 (Phase 6 part 2 — SafetyController test build-out + audit fixes):**

| Architecture-doc section | Change needed | Driving fixit ID | Status |
|---|---|---|---|
| Top-of-doc v5.1.x amendments | Add v5.1.13 block documenting commits `4aff918` + `f3630c9` (FX-016 test build-out) + `1c4ae7e` (FX-029 + FX-030 audit-driven fixes) | FX-016, FX-029, FX-030 | ✅ Done — full v5.1.13 scope block with 3-commit breakdown + Production impact + Lessons captured paragraph |
| Current Production State | HEAD reference bumped `a580bdb` → `1c4ae7e`; Fast-tier tests row updated 544 → 679; SafetyController row notes the 94% coverage + the FX-030 hardening | FX-016, FX-030 | ✅ Done — header bumped; Fast-tier tests row rewritten to mention the 152-test SafetyController suite + 94% coverage |
| §4.14 SafetyController | Note FX-030 — UNSAFE recovery is now exclusively via the slow path through DEGRADED; `_handle_upgrade` no-ops on UNSAFE | FX-030 | ✅ Covered in v5.1.13 scope block; §4.14 prose reflects pre-fix design which is preserved (the doc was the authoritative source — code now agrees with it) |
| §4.18 Runtime guardrails | Note FX-029 — per-market $200 cap now uses internal formula for both decision and post-cap value, independent of caller's est_capital_cost | FX-029 | ✅ Covered in v5.1.13 scope block + §10.2 B18 |
| §10.1 commit list | Add 3 rows (`4aff918`, `f3630c9`, `1c4ae7e`) at top of descending list | FX-016, FX-029, FX-030 | ✅ Done |
| §10.2 Known-fixed bugs | Add B18 (FX-029 per-market cap) + B19 (FX-030 UNSAFE fast-path) | FX-029, FX-030 | ✅ Done |
| §10.3 Active operational items | None to strike (FX-029 + FX-030 were both surfaced + closed within v5.1.13, never sat in §10.3) | FX-029, FX-030 | ✅ Done (no-op) |
| Companion paragraph | Reflect FX-016 + FX-029 + FX-030 closure; update open-item count to 2 (FX-019, FX-027); note Phase 6 is COMPLETE | FX-016 | ✅ Done |

**Status of items targeted at v5.1.12 (Phase 6 part 1 — CI):**

| Architecture-doc section | Change needed | Driving fixit ID | Status |
|---|---|---|---|
| Top-of-doc v5.1.x amendments | Add v5.1.12 block documenting commit `a580bdb` (FX-026 — GitHub Actions CI + README) | FX-026 | ✅ Done |
| Current Production State | HEAD reference bumped `91bae99` → `a580bdb`; note CI now gates pushes; first green run `26046878949` (7m17s) | FX-026 | ✅ Done — header bumped; Fast-tier tests row updated to 544/544; new "CI / build gate" row added |
| §10.1 commit list | Add `a580bdb` row at top of descending list | FX-026 | ✅ Done |
| §10.2 Known-fixed bugs | Add B17: "no automated test gate on push" | FX-026 | ✅ Done |
| §10.3 Active operational items | None to strike (FX-026 was a Phase 6 backlog item, not an operational risk) | FX-026 | ✅ Done (no-op) |
| Companion paragraph | Reflect FX-026 closure; update open-item count to 3 (FX-016, FX-019, FX-027); note Phase 6 is now half done | FX-026 | ✅ Done |

**Status of items targeted at v5.1.5 (this session):**

| Architecture-doc section | Change needed | Driving fixit ID | Status |
|---|---|---|---|
| Top-of-doc v5.1.x amendments | Add v5.1.5 block documenting commit `dd67f97` (I9 fix) | FX-001, FX-022 | ✅ Done — version/date/HEAD updated; v5.1.5 scope paragraph + Amendments in v5.1.5 block + Lessons captured all added |
| "Current Production State" table | Helsinki, not Ashburn; LIVE active, not blocked | FX-022 | ✅ Done — header bumped to v5.1.5 at `dd67f97`; **Server deployment** row rewritten for Helsinki; **SafetyController** row rewritten to describe the new bootstrap-aware branch |
| §10.1 commit list | Add `dd67f97` row at top of descending list | FX-022 | ✅ Done |
| §10.2 Known-fixed bugs | Add B10: "I9 deadlock on fresh-DB bootstrap" | FX-001 | ✅ Done |
| §10.3 Known limitations | Remove geoblock-on-US blocker (resolved by Helsinki); strike v5.1.5 I9 deadlock (resolved by `dd67f97`); add v5.1.5 observations subsection (FX-004 / FX-007 / FX-013 / FX-016) | FX-020, FX-022, FX-001 | ✅ Done — heading renamed to (v5.1.5); both blockers struck-through with resolution notes; new "Active operational items new in v5.1.5" subsection added |
| §11.4 Server provisioning | Update Hetzner location verdict (only Helsinki works) | FX-020 | ✅ Done — replaced the candidate-list paragraph with a verified status table |
| §11.13 LIVE cutover, "exit path" prose | Correct the incomplete claim about portfolio_snapshots being the only gate | FX-021 | ✅ Done — rewritten to describe both chicken-and-eggs explicitly and reference `fixit.md::FX-001` + `FX-002` |
| New §4.22 "Orphan position recovery" | Describe `_scan_for_orphans` behaviour + planned unliquidatable_cids fix | FX-023 | ✅ Done — full new section inserted between §4.21.7 and §5 |
| §10.3 lessons | Add "I9 deadlock is the second chicken-and-egg, distinct from the portfolio_value one" | FX-001 | ✅ Done — present in both the "Lessons captured in v5.1.5" block at top and in the §10.3 cross-reference within the SafetyController + DRY chicken-and-egg paragraph |

**Result:** architecture doc bumped from v5.1.4 (3,077 lines) → v5.1.5 (3,184 lines, +107). All §7 items closed for this iteration.

**Status of items targeted at v5.1.11 (Phase 5 operational hardening):**

| Architecture-doc section | Change needed | Driving fixit ID | Status |
|---|---|---|---|
| Top-of-doc v5.1.x amendments | Add v5.1.11 block documenting commit `91bae99` (FX-014 + FX-015 — graceful shutdown via SIGTERM + batch cancel + 3 audit findings) | FX-014, FX-015 | ✅ Done |
| Current Production State | HEAD reference bumped `d4d1541` → `91bae99`; note graceful-stop semantics + new directives in §11.11 | FX-014, FX-015 | ✅ Done |
| §11.11 systemd unit blocks | Add `KillSignal=SIGINT`, `TimeoutStopSec=30`, `KillMode=mixed` to both farmer + oversight unit blocks; new "Operational stop procedure" subsection | FX-014 | ✅ Done |
| §10.1 commit list | Add `91bae99` row at top of descending list | FX-014 + FX-015 | ✅ Done |
| §10.2 Known-fixed bugs | Add B16: graceful shutdown / batch cancel | FX-014 + FX-015 | ✅ Done |
| §10.3 Active operational items | Strike through FX-014 and FX-015 entries (no current §10.3 mentions — they were just in §3 of fixit) | FX-014 + FX-015 | ✅ Done (no-op — no §10.3 entries existed) |

**Status of items targeted at v5.1.10 (Phase 4 capital flow correctness):**

| Architecture-doc section | Change needed | Driving fixit ID | Status |
|---|---|---|---|
| Top-of-doc v5.1.x amendments | Add v5.1.10 block documenting commit `d4d1541` (FX-013/025/010/011/024 — wallet-first capital + scaled I4 floor + dead-knob deletion + CAPITAL_SOURCE log) | FX-013 family | ✅ Done |
| Current Production State | HEAD reference bumped `7d8d38d` → `d4d1541`; note about removal of $1500 fallback and wallet-scaled I4 floor | FX-013, FX-010 | ✅ Done |
| §4.14 SafetyController | Add `_capital_floor` helper docs; clarify I4 threshold is now wallet-scaled | FX-010 | ✅ Done |
| §4.18 Runtime safety guardrails | Note `[CAPITAL_SOURCE]` log line | FX-024 | ✅ Done |
| §8.1 RF config table | Remove `RF_MAX_COST_PER_MARKET`, `RF_MAX_TOTAL_EXPOSURE` rows | FX-011 | ✅ Done |
| §8.2 SafetyController constants | Add `CAPITAL_FLOOR_PCT` row | FX-010 | ✅ Done |
| §10.1 commit list | Add `d4d1541` row at top of descending list | FX-013 family | ✅ Done |
| §10.2 Known-fixed bugs | Add B15: $1500 silent fallback removed | FX-013 | ✅ Done |
| §10.3 Active operational items | Strike through FX-010/011/013/024/025 items | FX-013 family | ✅ Done |
| §11.13 LIVE cutover guidance | Update the FX-013 "remaining bootstrap gap" paragraph to note the gap is closed | FX-013 | ✅ Done |

**Status of items targeted at v5.1.9 (Phase 3 dump-state lifecycle):**

| Architecture-doc section | Change needed | Driving fixit ID | Status |
|---|---|---|---|
| Top-of-doc v5.1.x amendments | Add v5.1.9 block documenting commit `7d8d38d` (FX-007/005/006/008/009/028 — the unliquidatable_markets mechanism) | FX-007 family | ✅ Done |
| Current Production State | HEAD reference bumped from `e7fc3d2` → `7d8d38d`; brief note on the 7-state SafetyController + Tamilaga-spam closure | FX-007 | ✅ Done |
| §4.22 Orphan position recovery | Update the "Planned fix" section now that the fix has shipped; describe the four-touchpoint architecture (gate / mark-on-exception / cleanup-cascade / re-probe) | FX-007, FX-023 | ✅ Done |
| §9 Database Schema Reference | Add `unliquidatable_markets` to the §9.1 table list with column reference | FX-007 | ✅ Done |
| §10.1 commit list | Add `7d8d38d` row at top of descending list | FX-007 family | ✅ Done |
| §10.2 Known-fixed bugs | Add B14: orphan-dump 400-spam closed by unliquidatable_markets | FX-007 family | ✅ Done |
| §10.3 Active operational items | Strike through the "Orphan-scan creates persistent failing dumps" item under v5.1.5 observations | FX-007 | ✅ Done |

**Status of items targeted at v5.1.8 (Phase 2 counter consistency):**

| Architecture-doc section | Change needed | Driving fixit ID | Status |
|---|---|---|---|
| Top-of-doc v5.1.x amendments | Add v5.1.8 block documenting commit `e7fc3d2` (FX-004 counter-on-success) | FX-004 | ✅ Done |
| Current Production State | HEAD reference bumped from `541108b` → `e7fc3d2`; Telemetry row notes the counter-truth semantics | FX-004 | ✅ Done |
| §4.20 Telemetry Stream | Note that `orders_placed` field in `[CYCLE_SUMMARY]` is API-confirmed count, not attempt count | FX-004 | ✅ Done |
| §10.1 commit list | Add `e7fc3d2` row at top of descending list | FX-004 | ✅ Done |
| §10.2 Known-fixed bugs | Add B13: counter / DB inconsistency | FX-004 | ✅ Done |
| §10.3 Active operational items | Strike through the "Counter / DB inconsistency" item under v5.1.5 observations | FX-004 | ✅ Done |

**Status of items targeted at v5.1.7 (Phase 1 SafetyController bootstrap):**

| Architecture-doc section | Change needed | Driving fixit ID | Status |
|---|---|---|---|
| Top-of-doc v5.1.x amendments | Add v5.1.7 block documenting commits `dc78ba0` (FX-002 I3 cold-start skip) + `541108b` (FX-003 BOOTSTRAP state, FX-012 cold-start default) | FX-002, FX-003, FX-012 | ✅ Done |
| Current Production State | HEAD reference bumped from `987a844` → `541108b`; SafetyController row mentions 7-state machine + BOOTSTRAP | FX-003 | ✅ Done |
| §4.14 SafetyController | State table 6 → 7 rows; new BOOTSTRAP row; exit-conditions note; cold-start sentence reflects FX-002 + FX-003 | FX-002, FX-003 | ✅ Done |
| §10.1 commit list | Add `dc78ba0` + `541108b` rows at top of descending list | FX-002, FX-003 | ✅ Done |
| §10.2 Known-fixed bugs | Add B11: I3 drawdown deadlock fix; B12: BOOTSTRAP state | FX-002, FX-003 | ✅ Done |
| §10.3 Active operational items | Strike through FX-002 + FX-003 + FX-012 items in the v5.1.5 observations subsection | FX-002, FX-003, FX-012 | ✅ Done |

**Status of items targeted at v5.1.6 (Phase 0 housekeeping):**

| Architecture-doc section | Change needed | Driving fixit ID | Status |
|---|---|---|---|
| Top-of-doc v5.1.x amendments | Add v5.1.6 block documenting commits `3f50441` (FX-017 stale service removed) + `987a844` (FX-018 numpy added) | FX-017, FX-018 | ✅ Done |
| Current Production State | HEAD reference bumped from `dd67f97` → `987a844` | FX-017, FX-018 | ✅ Done |
| §10.1 commit list | Add `3f50441` + `987a844` rows at top of descending list | FX-017, FX-018 | ✅ Done |
| §10.3 Active operational items | Strike through "`numpy` not in `requirements.txt`" item | FX-018 | ✅ Done |

**Recommended next versioning:**
- v5.2 — major refresh after Hardening Phase 6 (test coverage + CI: `FX-016` SafetyController full coverage, `FX-026` GitHub Actions CI).

---

## 8. Changelog of this fixit doc

| Date | Author | Change |
|---|---|---|
| 2026-05-28 | session | **P10 + P11 of 9/10 plan shipped — FX-060 + FX-061: full 6/6 ground-rules §3 self-correction triggers now wired to behavior change.** Operator pushed back on the 8/10 claim asking "are you certain the system has a self-learning loop to maximize rewards and capital efficiency?" — honest re-rating revealed only 4/6 triggers were wired (#1 ROI cool, #2 fast-path cool, #3 fill_rate size reduction, #5 global loss tighten). The other 2 (#4 global reward < target, #6 q_share divergence > 2×) were observability-only, directly violating ground_rules.md "no code that runs but isn't read". P10 + P11 close that gap. **FX-060 (trigger #4):** when `total_reward_24h < RF_GLOBAL_REWARD_TARGET_24H_USD` (default $4 = 80% of $5/day floor for $1k wallet) AND `global_tighten` is False, decision_policy sets `global_reward_low=True`. Allocator halves both `MIN_DAILY_RATE_USD` (floor for market eligibility) and `MIN_EXPECTED_PER_MARKET` (floor for per-market expected reward), widening the candidate set per ground_rules.md "expand market count, lower per-market expected-reward floor". Mutually exclusive with `global_tighten` — if losses > rewards, tighten wins (cooling losers more critical than widening). **FX-061 (trigger #6):** new DB table `q_share_recalibration_events` (cfg `RF_QSHARE_DIVERGENCE_RATIO=2.0` matches ground_rules.md "diverges > 2×" text). simple_oversight passes API q_share + cumulative DB ratio per cid to `policy.record_qshare_divergence`; on breach inserts event row + emits `[LEARN_DIVERGENCE]` log. Next cycle: `evaluate()._detect_qshare_divergence` loads events within 24h window into `q_share_distrust_cids`. Allocator applies extra `0.5×` factor to NON-API q_share for those cids — implements ground_rules.md "recalibrate scoring" via the LEAST destructive interpretation (no cumulative reset; just heightened caution when allocator falls back from API). **15 adversarial tests** in `tests/test_p10_p11_full_self_learning.py` covering 6 attack families. **Verification:** 318 tests pass across P10+P11 + P9 + P8 + P4 + P3 + P2 + P1 + all prior FX + adjacent. Zero regressions. **Honest rating update: 7.5/10 today** (was 6/10 in the post-pushback honest assessment, was 8/10 in original optimistic certification). **Gate G-B fully met (6/6 not 4/6).** Path to 9/10 unchanged: live operation on Helsinki (G-C FX-054 production verify + G-E G1 7-day clean run). **Flagged uncertainties documented in commit/tests:** (a) absolute vs wallet-relative threshold for trigger #4 — chose absolute + cfg-tunable; (b) "recalibrate scoring" interpretation — chose minimal-behavior distrust flag rather than destructive cumulative reset. Operator can re-spec via config_overrides.json or future FX entry. |
| 2026-05-28 | session | **P9 of 9/10 plan — final certification + handoff to operator.** All offline code phases complete; 6 commits to main this session totaling +4000 lines across allocator + decision policy + tracker + chaos tests + operator runbook. **Cumulative tests:** 292 pass / 0 failures / 2 env-skips (network-dependent + SQLite version). **Gates met at code level (3 of 5):** G-A FX-052+053 OverCommitAllocator (50-200 markets, 3-8× wallet notional); G-B 4-of-6 ground-rules §3 self-correction triggers wired to behavior change (was 2-of-6 pre-session); G-D FX-046 formally accepted as Won't Fix + conservative-margin cfg knob mitigation. **Gates requiring live operation (2 of 5):** G-C FX-054 production verification (code-level closed + chaos-tested with 11 attack vectors all absorbed; production confirmation against a real fill burst still pending operator P5/P6); G-E G1 7-day clean run on Helsinki (P7). **Honest current rating: 8/10** — components shipped are well-tested and adversarially audited (would self-rate 9-9.5/10 on the code), system's full-pipeline readiness is 7.5-8/10 because the live ops phases are non-negotiable for the 9/10 target. **No remaining architectural blockers** — path to 9/10 is execution of the operator runbook at docs/runbooks/9_of_10_p5_p7_operator_runbook.md: shadow ≥48h clean → live cutover at full wallet → P6 fill-burst verification → P7 7-day continuous clean. **P8 adversarial sweep result:** 11 chaos attacks (CE-A API failures × 3, CE-B RPC outage × 1, CE-C config corruption × 1, CE-D stale alloc × 2, CE-E adversarial alloc data × 3, CE-F clock skew × 1, CE-G schema drift × 1 skipped) ALL absorbed by the defensive P1-P4 work. No new FX-NNN entries opened from chaos engineering. **Session deliverables ready for operator:** (1) all code merged to main; (2) runbook with step-by-step systemctl commands + emergency halt + cfg knob tuning guide; (3) P6 verification script (SQL + curl); (4) P7 G1 monitoring script (cron-friendly). |
| 2026-05-28 | session | **P4 of 9/10 plan shipped — FX-059: 4 of 6 ground-rules §3 self-correction triggers wired to behavior change.** Pre-P4 only 2 of 6 triggers had behavior change (FX-051 cooldowns: ROI threshold + fast-path). Triggers #3 (per-market fill_rate) and #5 (global loss > rewards) were observability-only (warning logs) — direct ground_rules.md violation of "no code that runs but isn't read". P4 wires both. **Trigger #3:** `decision_policy.evaluate()` adds cid to `size_reduction_cids` when samples_24h / 24 > 1.0/hr AND market is NOT already cooled. Allocator halves target_shares for these cids (clamped at min_size for venue eligibility). **Trigger #5:** sets `global_tighten=True` when total_loss > 0.5 × total_reward (or loss > 0 with no reward). Allocator raises MIN_DAILY_RATE_USD floor 2× AND applies 0.5× global size multiplier. Both compose multiplicatively (0.25× sizing on high-fill-rate markets during global stress). **No new DB table** — both triggers recompute each cycle from raw signals so transient anomalies self-resolve at next evaluation without manual cleanup. **decision_policy.evaluate()** now returns richer dict with `size_reduction_cids: set[str]` + `global_tighten: bool`. **simple_oversight.run_once()** extracts both, passes to `allocator.compute()` as new kwargs. **SimpleAllocator.compute()** gains 2 kwargs: `size_reduction_cids: Optional[set[str]] = None` + `global_tighten: bool = False` — both default to None/False for backward compat. **13 adversarial tests** in `tests/test_p4_self_correction_triggers.py` (P4-A trigger #3 × 4, P4-B trigger #5 × 5, P4-C trigger composition × 1, P4-D backward compat × 1, plus 2 helper). Telemetry: `[OVERCOMMIT_ALLOC]` log now includes `p4_size_reduction_cids` + `p4_global_tighten` counts. **Verification:** 126 tests pass across P4 + P3 + P2 + P1 + all prior FX + adjacent suites (test_decision_policy, test_market_roi_tracker, test_audit_cooldown_logic, test_simple_allocator, test_simple_oversight). Zero regressions. **Gate G-B (4+ triggers wired) NOW MET.** Of 5 gates for 9/10: G-A (FX-052+053) ✓, G-B (4 triggers) ✓, G-D (FX-046 resolved) ✓ — 3 of 5 code-level gates closed. Remaining: G-C (FX-054 production verification) + G-E (G1 7-day clean run) — both require live operation on Helsinki. **Next: hand off to operator for P5 staged rebring-up (paper → shadow → live cutover at full wallet).** |
| 2026-05-28 | session | **P3 of 9/10 plan shipped — FX-046 formally resolved (Accepted Risk + conservative q_share margin cfg knob).** Research agent investigation confirmed all 3 candidate q_share formulas under-predict actual Polymarket payouts by 24-94× — no clean code change disambiguates which (formula error vs market_q over-counting vs snapshot staleness vs maker/taker asymmetry). **FX-046 moved to §5 Won't Fix / Accepted Risk** with full rationale + 5 mitigations enumerated (API ground truth, conservative-margin knob, FX-051 cooldowns, FX-045 already-shipped Priority-1 demotion, post-G1 empirical reconciliation deferred). **Code mitigation:** new `RF_OVERCOMMIT_Q_SHARE_CONSERVATIVE_FACTOR=1.0` cfg knob in `simple_allocator.py:compute()`. Applied to NON-API q_share estimates only (cumulative + cold-start); API q_share is ground truth (Polymarket's own measurement, no margin). Default 1.0 = no-op to preserve Ground Rule 1 max-farm posture. Operators concerned about over-deployment can set to 0.5 at runtime via `config_overrides.json` → halve non-API expected_reward → EV gate tightens → fewer deploys but more confidence. 7 adversarial tests in `tests/test_p3_fx046_conservative_margin.py` (P3-A1 default 1.0, P3-A2 factor halves cumulative, P3-A3 factor does NOT apply to API, P3-A4 factor applies to cold-start, P3-B1 default no-op preserves cumulative). **Verification:** 272 tests pass across P3 + P2 + P1 + all prior FX + adjacent suites. Zero regressions. **Next: P4 — wire 2+ self-correction triggers to behavior change** (currently 2/6 wired; need 4/6 for G-B gate). |
| 2026-05-28 | session | **P2 of 9/10 plan shipped — FX-052 + FX-053 OverCommitAllocator.** Bot's allocator now obeys Ground Rules 1+2: deploys on 50-200 markets simultaneously (was capped at 20) with total notional permitted 3-8× wallet (was capped at 0.95× wallet). **SimpleAllocator class name retained for import-site compatibility**; semantics transformed to OverCommitAllocator. **Five changes:** (1) dropped `MAX_DEPLOYED_MARKETS=20` → soft sanity cap `RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS=500` (cfg-driven); (2) dropped `MAX_PER_MARKET_USD=$60` and `MIN_PER_MARKET_USD=$10` — per-market notional is now cost-to-score (`min_size × midpoint × 2 × 1.10` typically $20-50); (3) dropped `DEPLOY_RATIO=0.95` budget concept entirely — total notional bounded by Polymarket's collateral-rebalance auto-cancel mechanism per Ground Rule 2; (4) new positive-EV gate: `deploy iff expected_reward × q_share > expected_fill_cost × position_notional` (default 2% slippage); (5) alloc.json v1.1→v1.2 adds `_notional_overcommit_ratio` + `_target_market_count_band=[50,200]` metadata for monitoring. **18 adversarial pytest cases** in `tests/test_p2_overcommit_allocator.py` across 6 attack families (AO-A overcommit guarantees × 4, AO-B EV-gate × 3, AO-C pre-P2 filters respected × 3, AO-D kill-switch edge cases × 2, AO-E telemetry × 3, AO-F adversarial × 5). **5 existing test_simple_allocator tests updated** to assert new OverCommit semantics: C7 soft cap (was hard 20-cap); C8 cost-to-score (was MAX_PER_MARKET_USD cap); C9 overcommit verified (was DEPLOY_RATIO cap); + 2 NEW (C16 positive-EV gate boundary, C17 metadata stamps). **Verification:** 265 tests pass across P2 + P1 + all prior FX (FX-054, FX-057, FX-051, FX-045) + adjacent suites (test_simple_oversight, test_order_lifecycle, test_database_persistence, test_capital_flow, test_wallet_reconciliation, test_dump_manager_fee, test_oversight_shadow, test_shutdown). Zero regressions. **Risk profile:** all 5 new cfg knobs `RF_OVERCOMMIT_*` are hot-reloadable via `config_overrides.json` so the EV gate / soft cap can be tuned without redeploy. Worst case if gate too permissive: FX-051 cooldowns catch losers within 24h. Worst case if gate too restrictive: observable in [OVERCOMMIT_ALLOC] log, operator can tune live. **Next: P3 (FX-046 conservative q_share margin + formal resolution).** |
| 2026-05-28 | session | **P1 of 9/10 plan shipped — FX-058 farmer kill-threshold retune + FX-043 _total_capital metadata stamping.** The 9/10 plan (operator-approved 2026-05-28) is a 9-phase autonomous-execution plan to take the bot from honest ~5/10 to honest ≥9/10. P1 = unblock P2 OverCommitAllocator (FX-052/053) by making the farmer-side kill thresholds safe for 3-8× wallet notional operation. **Three changes in one commit:** (1) `RF_MAX_NOTIONAL_RATIO 2.0 → 5.0` and `RF_HARD_NOTIONAL_RATIO 2.5 → 8.0` — promoted from hardcoded constants to cfg-driven (hot-reloadable via config_overrides.json). Old defaults were anti-design per Ground Rule 2 — would have force-tripped kill switch on cycle 1 of FX-052/053. (2) New acceleration-based `_guardrail_rapid_notional_growth` — kill if `notional_ratio max/min over 5 min > 5×` — catches misconfigured-allocator bursts (e.g., deploying 10× normal) without false-firing on healthy overcommit. Configurable via `RF_RAPID_GROWTH_KILL_RATIO=5.0` (set 0 to disable) and `RF_RAPID_GROWTH_WINDOW_SEC=300`. Fail-open: missing notional_ratio leaves deque unchanged (DB hiccup can't reset window or trigger false kill); cold-start single sample no kill; min clamped to 0.0001 to prevent div-by-zero. (3) **FX-043 closed:** `simple_allocator.write_allocation_json` stamps `_total_capital` at top-level metadata (in addition to per-row); reader resolution chain metadata → deploy row → avoid row → None. Net: any cycle whose allocator successfully ran (even with 0 deploys) carries a usable capital signal. Bumped alloc-file version `simple-1.0 → simple-1.1`. Backward compat: legacy alloc.json without metadata still resolves via per-row fallback. **17 adversarial pytest cases** in `tests/test_p1_farmer_retune.py` across 4 attack families (AT-A cfg-driven × 3, AT-B rapid-growth × 6, AT-C FX-043 fallback × 6, AT-D end-to-end round-trip × 1). **Verification:** 243 tests pass across P1 + FX-054 + FX-057 + FX-051 + adjacent suites (test_audit_fill_detection, test_audit_cooldown_logic, test_decision_policy, test_market_roi_tracker, test_simple_allocator, test_simple_oversight, test_order_lifecycle, test_database_persistence, test_capital_flow, test_shutdown, test_wallet_reconciliation, test_dump_manager_fee, test_oversight_shadow). Zero regressions. **Next: P2 OverCommitAllocator (FX-052+053).** |
| 2026-05-28 | session | **FX-045 shipped — Priority 1 q_share over-estimation closed via Approach E (presence-gate).** Following the user-stated build → audit → fix workflow. **Bug shape:** pre-fix Priority 1 in `oversight/data_collector.py:query_reward_stats` returned `min(scoring_ratio × 0.5, 0.5)` where `scoring_ratio = our_scoring_snapshots / total` over a 4h window. This mapped "fraction of cycles we were in-zone" onto "fraction of reward pool we get" — two unrelated quantities. For any well-positioned bot scoring 100% of the time → q_share=0.5 (max), regardless of total queue depth. Live probe 2026-05-23 measured 1235–2000× over-estimate vs cumulative measurement for both deployed markets. The over-estimate fed I6 invariant as `est_d=$40/day` vs `act_d=$1–5/day` → ratio 8–40× perpetually firing SEVERELY → CALIBRATED state structurally unreachable → friend-rollout G3 gate blocked. **Fix (Approach E presence-gate):** windowed signal DEMOTED from magnitude estimator to safety override. New module-level constants `RF_WINDOWED_PRESENCE_GATE=0.10`, `RF_WINDOWED_PRESENCE_MIN_SAMPLES=3` in `data_collector.py`. New branch in `query_reward_stats`: when windowed has ≥ 3 samples AND scoring_ratio < 0.10 → force q_share=0 (override stale cumulative — we're confidently NOT earning right now). Otherwise fall through to Priority 2 (cumulative `total_q_score / total_market_q`, a real measurement, with the FX-005 poisoned-row guard preserved) then Priority 3 (cold-start prior). Updated `[Q-share]` log telemetry includes `presence_gated` counter. **13 adversarial pytest cases** in new `tests/test_audit_q_share_resolution.py` covering 3 attack families: QS-A priority resolution × 8 (well-positioned-bot uses cumulative not max; presence gate fires on 0% / 5% scoring; above-gate uses cumulative; below-sample-gate noise ignored; no-windowed unchanged; gate wins over cold-start prior; poisoned-cumulative falls to prior); QS-B invariants × 3 (q_share never exceeds real cumulative; staleness gate unchanged; well-positioned drops < 1/100 of pre-fix); QS-C incident regression × 2 (exact reproduction of the 2026-05-23 Helsinki probe shapes for both deployed markets `0x475c9930` and `0x0ed3f07970`, asserting post-fix q_share matches the cumulative ratio with no 1235–2000× inflation). **Architectural blast radius (flagged in commit):** the fix affects `oversight/data_collector.py:query_reward_stats` which is called by `oversight_agent.collect_all`. The current production entry point `simple_oversight.run_once` does NOT call this path — it uses `SimpleAllocator.fetch_current_q_shares` (Polymarket's `/rewards/user/percentages` API) directly. **So FX-045 has NO IMMEDIATE PRODUCTION IMPACT** — it lifts a structural friend-rollout (G3) blocker for if/when oversight_agent comes back. **FX-046 cumulative formula investigation remains open** as a separate concern (would refine Priority 2's accuracy independently from FX-045's gate semantics). **FX-047 I6 threshold recalibration likely obviated** — post-fix ratio shifts from 8–40× to 0.005–0.027× (well under the 5× CALIBRATED threshold, in the OPPOSITE direction). **Verification:** 201 tests pass across FX-045 + FX-054 + FX-057 + FX-051 + adjacent suites (data_collector, audit_fill_detection, audit_cooldown_logic, decision_policy, market_roi_tracker, order_lifecycle, database_persistence, simple_allocator, simple_oversight, cf_clamp, capital_flow, wallet_reconciliation, dump_manager_fee). Zero regressions. One test-fixture issue surfaced and fixed during the audit (staleness gate `on_book > 1` is strict-greater; time_on_book_secs=3600 didn't trip the gate so the test bumped to 7200). |
| 2026-05-28 | session | **FX-054 shipped — fill-detection root-cause fix via 3-axis defensive design.** Following the user-stated build → audit → fix workflow on the bot's last restart-blocker. **Hypotheses revisited (P1):** the open entry's 4 hypotheses (cycle interval, network timeout, write race, dump-time BUY) were investigated by reading the code — surfaced 2 ADDITIONAL root causes the entry didn't enumerate. **Root cause A:** `database.log_fill` caught all exceptions at `log.debug` level and returned `None` — the bd5a54e `[FILL_WRITE] succeeded` log fired unconditionally, lying about actual outcome. Silent on lock contention, schema drift, disk pressure. **Root cause B:** `_check_buy_phantom_fill` queried on-chain CTF balance immediately after SDK reported match; on Polygon CTF transfers confirm 2–5s later, so the check zeroed legitimate fills during the lag window → `phantom_zeroed` branch → no DB write. The 4 in-memory `ms.fill_times` entries the kill switch saw confirm SDK reported them; missing DB rows confirm downstream zeroing. **3-axis fix:** (F1) idempotent `log_fill` with partial unique index on `fill_event_id` (non-empty), defensive `None → ''` coercion (closes silent NOT NULL violation), bool return value, honest `[FILL_WRITE]` log step ∈ {attempting, succeeded, duplicate, FAILED}; (F2) phantom-check fail-OPEN within `FILL_BALANCE_LAG_TOLERANCE_SEC=60` of `slot.placed_at` (closes B), FX-037 behaviour preserved beyond window; (F3) end-of-cycle drift catch-up sweep on `(cids_processed - primary_handled)` — fires precisely on phantom_zeroed + UNKNOWN-no-surplus + UNKNOWN-below-threshold branches, 1 RPC per missed-detection, idempotent via 5-min-bucketed event_id. **14 adversarial pytest cases** in `tests/test_audit_fill_detection.py` covering 4 attack families (FD-A idempotency × 5, FD-B lag tolerance × 4, FD-C drift catch-up × 5, FD-D stacked-failure invariants × 4). **Audit caught a real bug in F3:** the drift sweep passed `slot.order_id=None` (cleared by primary phantom_zeroed path) into the `fills.order_id` NOT NULL column → INSERT OR IGNORE silently dropped the row → my own `[FILL_WRITE] FAILED` log fired (validating that the F1 instrumentation works as designed); closed by defensive coercion in both `log_fill` (catch-all) and at the F3 call site (explicit). **3 existing FX-037 phantom integration tests updated** to set `placed_at` 120s in the past (so FX-054 lag tolerance doesn't trip the FX-037 contract) and to mock `positions.record_fill` so the drift sweep sees consistent tracked-vs-on-chain pairs. **Verification:** 227 tests pass across FX-054 + FX-057 + FX-051 + adjacent suites (test_order_lifecycle, test_database_persistence, test_decision_policy, test_market_roi_tracker, test_audit_cooldown_logic, test_simple_allocator, test_simple_oversight, test_capital_flow, test_wallet_reconciliation, test_dump_manager_fee, test_oversight_shadow, test_shutdown). Zero regressions. **Production trace confirmation still recommended on next operational run** — log lines from bd5a54e are still there, plus new `[RECONCILE_DRIFT]` warnings. **Restart blockers status:** logic + accounting closed; FX-052/053 OverCommitAllocator (Phase 3) remains gated by farmer-side kill-threshold retune. |
| 2026-05-27 | session | **FX-057 shipped — adversarial audit of FX-051 closes 7 found bugs in one commit.** Following the user-stated workflow ("build → audit → break → fix"), Phase 2 (FX-051) got a focused breakage review against two attack families: cold-start trap (#6) and cooldown gaming (#4). **7 adversarial pytest cases** in new `tests/test_audit_cooldown_logic.py` (~430 LOC) each ASSERT the desired post-fix behaviour; all 7 FAIL on the FX-051 v1 code, demonstrating concrete bugs: **CS-1** single-fill $1.50 loss never cools (samples<3 AND fill_loss<$2); **CS-2** 5×$0.39=$1.95 cumulative with samples=5 stays at ROI=-3.9% (above -5% threshold); **CS-3** capital_avg=0 produces ROI=-100 (alarming [LEARN] telemetry); **CS-4** late-window snapshot makes capital_avg=$0.83 instead of $50; **CG-1** expired cooldown + still-bad ROI returns 'reactivate' instead of 'cool_down' (1-cycle bleed window per expiry); **CG-2** persistent $1.99/fill loss for 7 days never cools (adversarial threshold-edge exploit); **CG-3** single 100%-loss fill at $1.95 absolute doesn't cool (small-position attack). **5 targeted fixes** in `decision_policy.py` + `market_roi_tracker.py`: (1) `ABS_LOSS_FAST_COOLDOWN_USD: 2.0 → 1.0` (sized for per-market notional under overcommit); (2) `ROI_COOLDOWN_MIN_SAMPLES: 3 → 1` (consistent with <1 fill/day target); (3) new `_is_roi_bad` helper as single source of truth; `evaluate_market` now re-cools on expired-but-still-bad rather than reactivating; (4) tracker sets ROI to 0 when `capital_avg < CAPITAL_AVG_MIN_FOR_ROI` ($0.10) — telemetry cleanup, decisions still use `fill_loss`; (5) `_capital_committed_avg` queries the latest snapshot BEFORE `since_ts` and uses it as initial value, extrapolating the first in-window snapshot backwards when no prior exists. **Test results:** 7/7 audit tests now pass; 13/13 P-tests + 16/16 R-tests still pass (one P-test updated for new sample gate); 72-test sweep across FX-051+adjacent green; 83-test adjacent regression sweep (oversight/capital/dump/DB/wallet) clean. **Risk profile:** lower thresholds mean more cooldowns — but cooldown is 24h-reversible and bot re-evaluates each cycle; over-cooling can be tuned at runtime via constructor params without code redeploy. **Calibration trade-off flagged in §4 entry:** raising fast-path from $1 toward $1.50 if production shows >50% of fills cool the market. **Adjacent micro-issues noted but not fixed:** `_ensure_reward_cache_fresh` refetches infinitely when API legitimately returns `{}` (wasteful, not corrupting); 1h-reward approximation = (24h)/24 is biased low for 99% of UTC day (Polymarket batches at 00:20 UTC). Filed as FX-058/059 candidates if production shows them mattering. |
| 2026-05-26 | session | **v6.0 Phase 2 shipped — FX-051 per-market ROI tracker + cooldown decision policy (`e4f2ee3`, +1915 LOC).** Ground Rule 3's mandatory auto-correction loop is now implemented end-to-end. **(1) Salvage assessment first:** confirmed `calibration/loss_model.py` (per-share loss prediction, wrong granularity) and `profit/bandit.py` (binary success signal, wrong shape) are not directly salvageable; built fresh using Bandit's 24h-window SQL pattern as reference. **(2) Data layer (`market_roi_tracker.py`, ~470 LOC):** per-market rolling 1h/24h/7d snapshots of `reward_earned` (from `/rewards/user/markets` API, cached in new `daily_reward_cache` table), `fill_loss` (SUM(-pnl) from unwinds where pnl<0), `capital_committed_avg` (time-weighted from new `capital_committed_snapshots` table), `roi` = (reward - loss) / max(capital_avg, 0.01), `fill_count`, `fill_rate_per_hour`. `tick()` is idempotent and fail-quiet on API failure. **(3) Decision layer (`decision_policy.py`, ~280 LOC):** two cooldown triggers — `roi_24h < -5% AND samples ≥ 3` (sample-gated noise filter) OR `fill_loss_24h ≥ $2` single-event fast path (the 2026-05-25 0x46c09232 incident was $2.13 from ONE fill; waiting for 3 samples would have re-allowed the market and caused a repeat). Cooldown duration 24h. Reactivation on cooldown expiry. Emits structured `[LEARN]` log per ground_rules.md. **(4) Integration:** `SimpleAllocator.compute()` gains `excluded_cids: Optional[set[str]] = None` parameter; eligible filter adds `and m.condition_id not in excluded`. `simple_oversight.run_once()` ticks tracker → evaluates policy → passes `policy.get_excluded_cids()` to allocator → snapshots capital after alloc cycle. All in fail-open try/except: any exception → log warn + empty exclusion set → allocator behaves exactly as pre-FX-051. **(5) Schema (4 new tables in `database.py`):** `market_roi (cid, window, ...)`, `capital_committed_snapshots`, `market_cooldowns`, `daily_reward_cache`. **(6) Test coverage:** 16 new R-tests for tracker contracts (R1-R16), 13 new P-tests for policy contracts (P1-P13), 3 new C-tests for allocator integration (C21-C23), 1 new O-test (O12) for end-to-end run_once flow. **65/65 fast-tier pass in 3.39s.** **What's NOT in this commit (Phase 3 scope):** per-market notional resizing, farmer-side kill-threshold retune (the absolute MAX_NOTIONAL_RATIO = 2.0 that violated Rule 2), OverCommitAllocator. **Bot remains halted** pending adversarial review of FX-051 + FX-054 root-cause; user-agreed workflow is "build → audit → break → fix → lock → deploy". |
| 2026-05-26 | session | **v6.0 Phase 0 + Phase 1 quick-wins shipped — four single-axis commits on `main`.** Following the v6.0 sequencing plan validation, four commits landed back-to-back, each pairing with one observable hypothesis per the §0.1 P3 single-axis rule. **(1) `bd5a54e` — FX-054 instrumentation:** `[FILL_DETECT_TRACE]` log lines at every branch in `order_lifecycle.detect_fills` (missing_from_open_ids / sdk_resp / sdk_exception / phantom_adjusted / fill_recorded / phantom_zeroed / unknown_status) + `[FILL_WRITE]` brackets around `db.log_fill` in `handle_fill`. The `log.debug` on `client.get_order` exception (prime suspect for the 8 missing 2026-05-25 fills) was elevated to `log.warning` so the path is visible in default journals. Observability only — 32 `test_order_lifecycle.py` tests still pass. Root-cause fix pending production trace capture. **(2) `3704cd7` — FX-055:** `reconcile_wallet_invariant` re-wired into `simple_oversight.run_once()` between wallet probe + history reads and allocator compute. Mirrors the `oversight_agent.run_once()` integration. Outer try/except matches FX-049's fail-open contract — reconciler errors log + cycle continues. 22 `test_simple_oversight.py` + `test_wallet_reconciliation.py` tests pass. **(3) `80bd299` — FX-056:** `EXTREME_PRICE_LOW = 0.10` / `EXTREME_PRICE_HIGH = 0.90` constants added; `fetch_reward_markets` extracts `tokens[0].price` midpoint hint when API returns it; eligible filter rejects markets outside `[0.10, 0.90]`; markets without a price hint default to 0.5 → pass through fail-open. 5 new contract tests (C16-C20). The 13.3% slippage class (2026-05-25 fill on `0x46c09232` at $0.08) is now structurally excluded. **(4) `9164f1f` — FX-039:** `fill_type` threaded through `handle_fill` as a parameter; three call sites pass the correct PARTIAL/FULL value (detect_fills, _check_stale_order, _reconcile_after_unknown keeps "FULL" default). Fix surfaced a latent crash in `alerts.py:322` where the PARTIAL alert branch formatted `remaining_shares` unconditionally — pre-fix the hardcode masked this dead code path. `remaining_shares = max(0, slot.shares - filled_shares)` now passed; 58 tests pass including `test_stale_order_partial_fill_detected` which now meaningfully exercises the PARTIAL alert path. **What remains open per v6.0 plan:** FX-054 root-cause fix (pending trace capture), FX-051 ROI tracker + decision policy (Phase 2), FX-052 + FX-053 OverCommitAllocator (Phase 3 — requires farmer-side kill threshold retune as prerequisite). **Bot remains halted.** First production trace capture happens on next operational run (whether shadow or live). |
| 2026-05-24 | session | **Phase A of Master Plan COMPLETE — FX-050 + FX-049 shipped in single commit per operator P3 authorization** (`06d8406`, CI 26350996533: 785/785 in 5m46s). **(1) FX-050 — Polymarket taker fee in DumpManager**: new `RF_POLYMARKET_TAKER_FEE = 0.009` config knob; `dump_manager.py:89` applies `sell_revenue = matched × price × (1 − fee)`. Closes the ~25-30% under-reporting of dump losses that I7 hourly_loss + 24h-realized-loss kill switch were operating on. Calibrated against 2026-05-22 incident: post-fix pnl = −$1.349 vs actual −$1.34 (within $0.01 float rounding). **(2) FX-049 — Wallet-invariant reconciliation**: new `wallet_reconcile_history` table + `oversight/wallet_reconciliation.py` module + integration in `oversight_agent.run_once()`. Runs once per agent cycle; compares actual wallet delta vs bot-DB expected (`Σ unwinds − Σ fills + data-api Σ REWARD + Σ MAKER_REBATE`); `\|divergence\| > $0.50` → `[CRITICAL] WALLET_DESYNC`. First-run path snapshots baseline (no false-positive on cold start). Fail-OPEN on data-api errors. Incremental rolling window. Defense-in-depth backstop catching ANY future cash-accounting drift even when root cause is unknown. **Single-commit bundle justified per operator authorization**: FX-050 fixes symptom, FX-049 catches future drift the formula doesn't predict — both belong to same "loss-accounting integrity" pass. **(3) 15 new tests** (5 in `tests/test_dump_manager_fee.py` for FX-050 contracts; 10 in `tests/test_wallet_reconciliation.py` for FX-049 contracts). Fast tier 770 → **785 pass** (0 regressions). **(4) FX-037, FX-050, FX-049 all moved from §3 to §4** with full retrospectives. §2 at-a-glance table updated. Architecture doc v5.1.20 → v5.1.22 (top-of-doc amendment blocks for v5.1.21 + v5.1.22, Current Production State table updated, §8 config table + §9 schema table + §10.1 commit list all updated). **Master Plan Phase A complete; Phase B (FX-045 q_share priority swap) is NEXT** — single highest-leverage remaining code change, structural G3 unfreezer. |
| 2026-05-23 | session | **Phase 1 doc-lock + Phase 2 FX-037 ship under META v2.0 ZP execution.** **(1) Investigation results:** the FX-044 entry's stated root cause was wrong. Code at `data_collector.py:544` ALREADY uses rolling 24h cutoff (`cutoff_ts = time.time() - 24*3600`), not UTC-day bucketing. Probe of `data-api.polymarket.com/activity?type=REWARD` over last 30d showed 6/6 events at hour 0 UTC (00:00-00:20 window), inter-event gap = 24.00h, threshold-gated at $1 minimum (operator-confirmed). The morning ratio jump is real but caused by **upstream q_share over-estimation** in Priority 1, not a windowing bug. **(2) FX-044 moved to §4 as Investigated/Not-a-bug.** **(3) Three new entries opened in §3:** **FX-045** (HIGH) — Priority 1 windowed q_share returns `min(scoring_ratio × 0.5, 0.5) = 0.5` upper-bound heuristic for any well-positioned bot, trumps healthy Priority 2 cumulative (live probe: 0.5 vs 0.000249-0.000405 cumulative → 1500× over-estimate of est_d). G3 structural blocker. **FX-046** (Medium, INVESTIGATION) — Q-score formula candidates (squared/linear/size-share) all predict $0.02-0.05/day vs actual $1-5/day. 24-94× discrepancy unexplained. Architecture doc §4.23.1 cites LINEAR formula but `reward_tracker.q_score_order` uses SQUARED. Needs empirical reconciliation before FX-045 design decision. **FX-047** (Medium, CONTINGENT) — I6 thresholds (5×/15×/50×) may need recalibration against measured production distribution if FX-045 + FX-046 don't naturally close the ratio gap. **(4) FX-037 SHIPPED** in same session: new `_check_buy_phantom_fill` helper in `order_lifecycle.py` mirrors `DumpManager.check_dump_fills`' on-chain probe. detect_fills now queries `get_balance_allowance` after SDK reports a fill, compares on-chain delta vs reported `size_matched`, prefers on-chain truth on discrepancy, emits `log.critical("PHANTOM FILL: ...")` for operator visibility, fails-OPEN on API error to preserve legitimate fills during network blips. 13 new tests in `tests/test_order_lifecycle.py` (`TestCheckBuyPhantomFill` × 11 + `TestDetectFillsPhantomIntegration` × 3) encoding contracts for: phantom detected → on-chain delta returned (the 2026-05-19 Iran 158→38 shape), honest SDK → unchanged, API exception → fail-open, matched=0 → no-op, token_id routing per side, negative delta clamped, log channel verified. Added `_ensure_clob_types_mock()` shim at top of test file (mirrors `test_sports_protection.py`) so SDK-dependent tests run on local dev without the V2 SDK installed. Fast tier passes locally. **(5) Architecture doc §4.2 updated** with warning that Priority 1 is an upper-bound heuristic per FX-045 evidence. **(6) Architecture doc §12.6 + fixit §6 G3 framing** unchanged for now — FX-045 investigation must complete before G3 redefinition. **Operating principle reinforced (P1):** the original FX-044 entry was a hypothesis stated as fact. Investigation under P1 surfaced three real bugs the original framing missed. Always read the compute site before proposing a "this is wrong" fix. |
| 2026-05-22 | session | **40h post-FX-041 state analysis surfaced two new Medium issues + corrected one prior P1 violation.** Wallet $221.33 → $227.43 (+2.76% in 40h, ~1.66%/day on-chain). Zero fills, zero kill-switch events, zero `would_pause=True` cycles in 24h. **FX-043 opened (Medium):** `_total_capital` stamp disappears from alloc file during 0-deploy moments → `[GUARDRAIL] total_capital=null` → notional + cluster + 24h-loss-kill-switch all disabled fail-open. Observed once (~5 min on 2026-05-21 19:50-19:54 UTC); no damage because no activity in the window. Proposed fix combines metadata-stamp + portfolio_snapshots fallback. **FX-044 opened (Medium):** I6 morning-SEVERELY spike at UTC day boundary — every day at 00:00 UTC, est/actual ratio jumps from ~6× to ~27× because `act_d` resets while `est_d` stays at full-day rate. Structurally blocks the G3 friend-rollout gate (CALIBRATED ≥24h unreachable). Proposed fix: 24h rolling window for `act_d`. **P1 violation corrected:** yesterday I claimed `0x0ed3f07970` was "resolved/de-listed" based on CLOB `/markets/{cid}` returning 404. Reality: market is fine — bot has live scoring orders on it RIGHT NOW (and earned 7.25 scoring hours on it today). The 404 is a Polymarket API inconsistency on the metadata endpoint; the rewards + order book endpoints work fine. Lesson logged: never conclude market resolution from a single endpoint's 404; verify across `/rewards/markets/current` + order book + live order placement. **CF smoothing asymmetric blind spot observed:** raw CF spiked 0.69 → 9.63 in one cycle at 20:22 UTC May 21 → smoothed peaked at 3.145 (above CALIBRATED upper bound 3.0). Self-recovered over ~5h. Same root cause as FX-043 (est_d collapse during 0-deploy moment). No invariants fired during the spike. Logged in architecture doc §10.3 as a known pattern. **G3 friend-rollout gate description updated** to note FX-044 dependency. All updates doc-only; observation hold still in effect until ~05:00 UTC 2026-05-22. |
| 2026-05-21 | session | **§0.1 change-management framework added** (P1-P5). Canonical text in architecture doc §12.6. Codified during the post-FX-041 observation hold; applies to every entry going forward — each new §3 entry should cite the principle that surfaced it (typically P1 or P4). Also added **§6 Phase 10 — Friend rollout** with the G1-G7 gates as a verification table. **FX-042 opened (Medium):** `orders_cancelled` table is never written by the production codepath (legacy `order_manager.py` is the only writer, inactive in production). Surfaced via P1 during state analysis — the discrepancy between 28 placements and 0 cancellation rows over 24h prompted code reading. Currently latent (fill model dormant); `calibration/features.py:117` will train on miscalibrated labels once the fill model activates. Doc-only update during the 2026-05-21 → 2026-05-22 observation hold; fix ships after FX-037. |
| 2026-05-20 | session | **FX-041 two-sided book-depth check SHIPPED (`3534cb5`, v5.1.20).** Prerequisite for safely re-enabling FX-036 (queue-depth-aware placement) in production. One new config knob (`RF_DUMP_DEPTH_SAFETY_FACTOR = 3.0`), one new helper (`_has_sufficient_dump_depth`) in `order_lifecycle.py`, two new kwargs on `_compute_edge_prices` (defaulted for backwards compat — every pre-FX-041 caller stays byte-identical), production call site wires `ms.agent_shares or SHARES_PER_SIDE()` + `DUMP_DEPTH_SAFETY_FACTOR()`. After each queue-aware result, runs an opposite-side dump-depth check (`Σ price × size` over the opposite merged-book side within `max_spread` of midpoint, threshold `shares × midpoint × factor`). If insufficient, that side falls back to legacy zone-edge — per-side independence preserved. 18 new tests in `tests/test_placement.py` (10 helper unit + 2 backwards-compat + 5 integration + 1 end-to-end). **Fast tier 737 → 755 pass (0 regressions).** Iran market (FX-036 motivating scenario) still passes queue-aware with FX-041 enabled at default factor 3.0 — no reward-density regression. Asymmetric books (the OpenAI cascade shape) now correctly fall back to legacy. **Operator action to re-enable FX-036 in production:** remove `"RF_TARGET_QUEUE_AHEAD_USD": 0` from Helsinki's `config_overrides.json` and restart polymarket-farmer. FX-036's 3× reward density uplift on deep symmetric books returns; asymmetric books safely fall back. **Known interpretation trade-off:** the check uses OPPOSITE-side depth (not SAME-side, which would be the most physically-correct dump-absorption measurement per DumpManager's passive mode). OPPOSITE-side was chosen because it matches the FX-041 acceptance criterion narrative and is a new safety axis complementary to the existing same-side `exit_buf` check. Both interpretations catch the OpenAI cascade. **Known simplification:** `dump_price = midpoint` is used uniformly for both sides; for cheap-YES markets (midpoint $0.10) this understates NO-side inventory value — operator can raise the factor via `config_overrides.json` if production shows false negatives on extreme-priced markets. **Open Phase 1 items remaining after FX-041:** FX-037 (BUY-side phantom-fill defense — silent-corruption-blocking), FX-038 (reconciliation extends to fills/unwinds), FX-039 (cosmetic `fill_type='FULL'` labelling). |
| 2026-05-20 | session | **FX-040 cold-start trial-mode sizing SHIPPED (c2c21d7, v5.1.19).** Single most important Phase 1 fix from the 2026-05-19 cascade analysis. Three new config knobs (`RF_TRIAL_MIN_SHARES=20`, `RF_TRIAL_SCORING_SAMPLES=5`, `RF_TRIAL_BUDGET_PCT=0.25`). `q_score_samples` propagated through `MarketMetrics` → `ScoredMarket`. New trial-mode branch in `oversight/allocation_writer.compute_allocations`: untested markets cap at `max(min_size, RF_TRIAL_MIN_SHARES)` shares; cumulative trial budget gate; redistribution pass excludes trials. New `[FX-040 trial]` telemetry. +351 / -5 lines across 5 files + 1 new test file (`tests/test_trial_sizing.py` with 16 tests). Full fast tier 721 → **737 pass** (0 regressions). **Production verification on Helsinki at 08:22:40 UTC**: first oversight cycle on c2c21d7 logged `[FX-040 trial] deployed=1 rejected=49 budget_used=$46/$55 (25% cap)`. The 49 rejected cold-start markets are the same kind that lost $17.63 yesterday. Yesterday's OpenAI HIGH $1.5T (min_size=200) now rejected with `Trial budget exhausted ($0+$182>$55, samples=0)`. **143-share trap closed.** FX-036 still runtime-disabled (`RF_TARGET_QUEUE_AHEAD_USD=0` in config_overrides.json) until FX-041 (two-sided depth check) ships — that's the remaining Phase 1 prerequisite. |
| 2026-05-20 | session | **FX-036 production cascade analysis + Phase 0 conservative restart + 5 new tickets opened.** At 2026-05-20 00:30 UTC, kill switch fired on `daily_realized_loss=$19.55 > $17.14 (=10%·T=$171.40)`. Traced cascade: FX-036 placed Iran NO bid at 2¢ from mid → taker hit → V2 SDK reported size_matched=158 but only 38 NO shares delivered on-chain → inflated `fills` row triggered I7 phantom $60.72 damage → SafetyController demoted to DEGRADED → tight per-market cap forced cold-start OpenAI markets → thin-market dumps at 5-11% slippage → kill. Bot dead 3.5h until manual restart. **Actions taken:** (1) Wrote `config_overrides.json` on Helsinki with `RF_TARGET_QUEUE_AHEAD_USD = 0` to disable FX-036 at runtime (legacy zone-edge placement). (2) Restarted both services at 04:07:43 UTC, kill switch cleared. (3) Investigated allocator file: trial cap was binding filter (1846/1912 avoids), not CF as initially hypothesized. Iran was rejected with `Net negative dmg=$60.72` (the phantom). (4) Applied direct SQL UPDATE to fills table: Iran NO row corrected from `shares=158, usd_value=77.42, fill_type='FULL'` → `shares=38, usd_value=18.62, fill_type='PARTIAL'`. Damage dropped to $1.92 (matches real). (5) Bot resumed trading 2 OpenAI cold-start markets at $22 notional. **Five new tickets opened**: FX-037 (BUY-side phantom-fill defense — symmetric with DumpManager's existing check), FX-038 (reconciliation extends to fills/unwinds), FX-039 (handle_fill hardcoded fill_type='FULL'), FX-040 (cold-start trial-mode sizing — biggest leverage), FX-041 (two-sided depth check in FX-036). Re-enabling FX-036 in production requires FX-037 + FX-040 + FX-041 to ship first. Memory captured at `~/.claude/projects/.../memory/phantom_fill_recovery.md` (SQL recipe) + `thin_market_cold_start_lesson.md` (strategic analysis). |
| 2026-05-19 | session | **FX-036 shipped (v5.1.18, queue-depth-aware placement).** Replaces the fixed-distance formula at `order_lifecycle.py:354-357` with two helpers (`_queue_aware_edge`, `_compute_edge_prices`) that walk the merged book from best (closest to mid) outward, accumulate cumulative USD notional (`price × size`), and sit one tick behind the level where queue first crosses `RF_TARGET_QUEUE_AHEAD_USD` (new config knob, default `$1000`). Falls back to the legacy zone-edge formula on thin books, escape-hatch (`knob <= 0`), and zone-boundary edge cases — so weather-class markets and other low-competition regimes see zero behaviour change. Inline production-shape verification against the Iran market: bid moved from `$0.440` → `$0.460`, ask from `$0.530` → `$0.510`; reward density `18.2%` → `54.5%` = **3.0× uplift**. +1 line in `config.py`, +100 / -6 lines in `order_lifecycle.py`, +350 lines new `tests/test_placement.py` (24 tests). Full fast tier 697 → **721 pass**. **Note on test pollution:** `test_critical_fixes.py` + `test_sports_protection.py` patch `sys.modules["py_clob_client_v2"]` with partial MagicMock stand-ins and don't clean up. New `_drop_stale_clob_mocks()` setup helper in `test_placement.py` drops the partial mocks so the real SDK re-imports on dev / CI venvs (where it's actually installed). |
| 2026-05-19 | session | **FX-036 logged (doc-only).** Operator inspection of Helsinki's first production orders revealed placement formula picks the far edge of the reward zone (4.5¢ from mid for the Iran market's 5.5¢ `max_spread`), earning ~9% of theoretical reward density. The stated objective is reward maximization, not fill avoidance — these are different. Proposed fix: queue-depth-aware placement (`TARGET_QUEUE_USD ≈ $1000` of bids ahead of us; sit 1 tick behind the level where cumulative queue first crosses the threshold). Full strategy section added to `Polymarket bot architecture v5.1.md` as new §4.23 "Order placement strategy — reward-farming positioning" (~150 lines covering: Polymarket reward formula, current placement code, trade-off table, FX-036 algorithm, capital_pct interaction, verification plan). Bot continues farming on the current (conservative) placement overnight; code change deferred to next session for safety. |
| 2026-05-19 | session | **FX-035 — V2 SDK `get_order_book` returns dict; `get_merged_book` assumed object (THE ROOT CAUSE, post-roadmap, v5.1.17).** The bug that caused **4 days of zero orders placed in production**. Discovered by tracing through Helsinki's persistent 0-orders state after FX-031 + FX-032 were both shipped and didn't fix the dormancy. Direct SDK probe on Helsinki at 04:36 UTC: `client.get_order_book()` returns a dict; the code's `getattr(ob, "bids", [])` returns `[]` on dict input. Every book fetch failed silently since the V2 migration in `2a6baf6` (2026-04-29). DRY mode masked it for ~17 days; FX-001's I9 deadlock masked it during LIVE bootstrap; FX-031/FX-032 closures finally exposed it. Same class as B9 (`get_orders → get_open_orders`) — V1→V2 SDK migration miss. Fix in `647b1e2`: new `_book_entries(ob, key)` helper normalizes dict-form (V2 SDK production shape) + object-form (test mocks). `get_merged_book` uses it for all 4 iteration sites. Backward-compat preserved. paper_trader_v2 delegates to market_discovery; paper_client refactored. +335 / -72 lines. **12 new regression tests** in `tests/test_get_merged_book.py` that call the REAL function with both shapes — dict-form tests fail pre-fix, pass post-fix. Test count 685 → 697. Production verification BEFORE pull: ran the patched function inline against live SDK for the Iran market — returned bids=36, asks=46, midpoint=$0.4950, spread=$0.0100. Definitively tradeable. The lesson worth tattooing: **coverage isn't correctness; the FX-016 audit's 152 SafetyController tests + 685 fast-tier total all stayed green while the bot was unable to fetch a single book in production for 4 days.** Code-level audits catch architectural drift; production diagnostics catch input-shape drift. Both are necessary. FX-033 + FX-034 (Helsinki-recovery follow-ups) are subsumed/made trivially recoverable by this root-cause fix. |
| 2026-05-19 | session | **FX-032 — Dead-market cleanup over-marks healthy cids as unliquidatable (post-roadmap follow-up, v5.1.16).** Surfaced empirically during Helsinki recovery: 60 cids got mass-marked at 03:23:38 UTC via FX-006's cascade, including the Iran market (paying $200/day in rewards). FX-028 re-probe couldn't un-mark them despite the books returning HTTP 200 OK. Diagnosed via direct CLOB API probe of `0xdb22a7749b83`: market active, accepting orders, deep books — healthy in every measurable way. Root cause: FX-006 cascaded `mark_unliquidatable` into the dead-market cleanup, where it fires for any `get_merged_book` failure (SDK parse errors, transient blips, brief empty-book windows) — much wider than the canonical FX-007 "orderbook does not exist" body. Fix in `75d03c7`: removed the `mark_unliquidatable` call from the dead-market path; FX-006 cascade for `delete_dump_state` preserved. Tests rewritten + new source-inspection test that reads `RewardFarmer.run_cycle` and asserts no `mark_unliquidatable` in the Step 4b block. +5 / -23 lines in `reward_farmer.py`. Test count 684 → 685. Two related follow-ups opened in §3: **FX-033** (oversight allocator should consult `unliquidatable_markets`) and **FX-034** (re-probe doesn't un-mark on healthy books) — both Low severity post-FX-032 since the upstream cause is closed. The lesson: **"logic-shape replay" tests where the test re-constructs the loop body locally pass regardless of what the source does.** The new source-inspection test pattern catches this — read the actual function source via `inspect.getsource` and assert structural properties. Adding this to the test toolkit for any safety-critical replay test in the suite. |
| 2026-05-19 | session | **FX-031 — `filter_allocations` per-state capital cap wholesale-rejects oversized deploys (post-roadmap follow-up, v5.1.15).** Surfaced empirically on Helsinki's first oversight cycle after the v5.1.14 recovery pull: BOOTSTRAP cap = $60, allocator proposed 3 deploys at $84-$89 each, running-cost loop rejected all 3 because each individual cost exceeded the cap. `markets_deploy: 0`. Fix in `d5eabea`: scale shares down to fit `remaining` budget (same scale-down pattern as FX-029 per-market cap), iterate `deploys` in score-desc order so the top scorer claims the budget, reject cleanly with "capital exhausted" only when `remaining < min_cost`. +141 / -9 lines across `oversight/safety_controller.py` (29-line block rewrite) and `tests/test_safety_controller.py` (5 new regression tests in `TestFilterAllocationsCapitalCapScaling`). Test count 679 → 684 fast-tier. Coverage on `safety_controller.py` unchanged at 94% (added 5 stmts, all covered). Architecture doc bumped v5.1.14 → v5.1.15 in lock-step. This is exactly the FX-001 class of bug — silent contract-met-but-spirit-violated, missed by FX-016's test suite because no scenario had `individual_deploy_cost > per_state_cap`. The Helsinki post-restart observation IS the test that catches it. Demonstrates the value of treating "first production cycle after a major release" as an explicit verification step, not just a deploy. |
| 2026-05-19 | session | **Hardening roadmap closure — v5.1.14.** Two remaining open items closed: **FX-019** (`check_wallet.py` cosmetic 400) — removed dead `AssetType.CONDITIONAL` query (4 lines + replaced with cross-reference comment); zero functional impact, eliminates the alarming-but-harmless startup error. **FX-027** (process-boundary lag) — accepted as designed architectural risk; moved to §5 with full rationale documenting why the farmer-side guardrails (notional/cluster caps + kill switch on 30-s cadence) make the 30-min agent cadence safe for non-pathological scenarios. §3 (Open issues — detail) now empty; §2 footer "shipped" list updated; §6 marks Phases 7/8/9 closed (Phase 7 deferred to "when a real signal emerges"). Architecture doc bump v5.1.13 → v5.1.14 in lock-step. **Hardening campaign Phase 0-6 + closure of remaining items COMPLETE across 2 days (2026-05-18, 2026-05-19) — 28 of 30 fixit entries shipped as code/doc/tests, 1 closed organically (FX-020 + 3 from the doc-accuracy retroactive sweep), 1 accepted (FX-027). Test suite: 449 (v5.1.4) → 679 fast-tier tests (+230). Coverage on SafetyController: 0 dedicated tests → 152 tests / 94% coverage. CI now gates every push. From "first LIVE bootstrap deadlock" (FX-001 on 2026-05-15) to "all open items closed" in 4 calendar days.** |
| 2026-05-19 | session | Phase 6 part 2 shipped (closes Phase 6): FX-016 + FX-029 + FX-030 all addressed in `4aff918` + `f3630c9` + `1c4ae7e`. SafetyController test coverage 58% → 94% (525 → 530 stmts, 218 → 34 miss; 17 → 152 tests in `tests/test_safety_controller.py`). All 14 invariants now have happy/breach/query-failure cases; state machine ladder pinned; `filter_allocations` end-to-end covered; persistence round-trip, query helpers, confidence_score, alert-file writers all exercised. Audit pass on the build-out surfaced TWO real safety bugs both fixed pre-doc-lock: **FX-029** — per-market $200 cap could be overshot when caller's `est_capital_cost` was inconsistent with internal `shares × est_price × 2` formula (audit's repro: shares=500, est=300, spread=0.045 → final $303.03); fixed by deriving both scaling and recomputation from the internal formula. **FX-030** — `_handle_upgrade` UNSAFE→MILDLY fast path bypassed the documented `UNSAFE_RECOVERY_CYCLES=3` cap to DEGRADED (architecture doc §10.3 + lines 1919-1920 had explicitly documented the 5-cycle minimum); fixed by making `_handle_upgrade` no-op on UNSAFE so the slow auto-recovery in `evaluate_state` is the sole exit. Full fast tier 632 → 676 → 679 across the three commits. Architecture doc bumps v5.1.12 → v5.1.13. §2 / §3 / §4 / §6 / §7 updated in lock-step; §6 Phase 6 marked COMPLETE. Two open issues remain (FX-019 cosmetic 400, FX-027 process-lag — Phase 8/9 backlog). |
| 2026-05-19 | session | Phase 6 part 1 shipped: FX-026 closed by `a580bdb`. New `.github/workflows/test.yml` runs fast-tier suite on every push to `main` + every PR (ubuntu-24.04, Python 3.14, pip-cached, 15-min timeout). New `README.md` carries the workflow status badge. First CI run `26046878949` green in 7m17s, 544/544 fast-tier tests pass on the runner. +54 lines / 2 new files. One Node.js 20 deprecation annotation logged (action runners deprecate Node 20 by 2026-06-02); on latest action major versions already. §2 / §3 / §4 / §6 / §7 updated in lock-step; architecture doc bump v5.1.11 → v5.1.12 to follow. Phase 6 now half done (FX-016 SafetyController coverage build-out remains). |
| 2026-05-18 | session | Post-Phase-5 doc reconciliation: FX-021, FX-022, FX-023 retroactively moved from §3 (Open) → §4 (Fixed) — they were doc-accuracy items resolved organically by prior phases (FX-021 by v5.1.10 §11.13 rewrite; FX-022 by v5.1.5 Amendments + Current Production State; FX-023 by v5.1.5 new §4.22 + v5.1.9 expansion) but never moved to §4. Three stale "FX-014 will copy" forward-looking references in arch doc §10.1 + §10.3 also fixed to past tense. §2 open table cut 7 → 4 rows (FX-016, FX-019, FX-026, FX-027 remain). Phase 8 in §6 ticks updated. Final §2 footer count synced. No code changes. |
| 2026-05-18 | session | Phase 5 operational hardening shipped: FX-014 + FX-015 closed by `91bae99`. reward_farmer adds SIGTERM handler; `_shutdown_cleanup` uses V2 batch `cancel_orders` (one API call replaces 240 per-order cancels); OL.cancel_order gains `force=True` parameter; rate_limiter covers V2 method names; structured `[SHUTDOWN]` log lines. Architecture doc §11.11 unit blocks updated with `KillSignal=SIGINT` + `TimeoutStopSec=30` + `KillMode=mixed` + new "Operational stop procedure" subsection. +493 / -18 lines across 5 files. +22 new tests in `tests/test_shutdown.py`. Full fast tier 522 → 544 (no regressions). Comprehensive audit ran post-implementation; 3 real bugs surfaced (SHADOW kill-switch override broken, V2 SDK names missing from rate-limiter, 60+ market latency cliff) and all addressed pre-commit. Architecture doc bumped v5.1.10 → v5.1.11; fixit doc at v1.6. |
| 2026-05-18 | session | Phase 4 capital flow correctness shipped: FX-013 + FX-025 + FX-010 + FX-011 + FX-024 all closed by `d4d1541`. Farmer writes `usdc_balance` on cycle 1 (eliminates 5-min cold-start window); agent `--capital` default → None (no more silent $1500); per-cycle `[CAPITAL_SOURCE]` log line; SafetyController I4 floor wallet-scaled (max($50, ref*10%)); dead config knobs `RF_MAX_TOTAL_EXPOSURE` + `RF_MAX_COST_PER_MARKET` deleted. +514 / -30 lines across 6 files. +21 new tests in `tests/test_capital_flow.py`. Full fast tier 501 → 522 (no regressions). Architecture doc bumped v5.1.9 → v5.1.10; fixit doc at v1.5. Comprehensive audit ran post-implementation; zero code findings. |
| 2026-05-18 | session | Phase 3 dump-state lifecycle correctness shipped: FX-007 + FX-005 + FX-006 + FX-008 + FX-009 + FX-028 all closed by `7d8d38d`. New `unliquidatable_markets` DB table + 6 methods; gates added at every order/dump path; mark-on-exception in OL+DM; dead-market cleanup cascades; 30-min re-probe sweep with 6h per-cid staleness. +984 / -20 lines across 12 files. +31 new tests in `tests/test_unliquidatable_markets.py`. Full fast tier 470 → 501. Comprehensive code-review audit ran after initial implementation surfaced 4 real findings (detector tightness, `_sync_exchange_positions` gate, misleading docstring, test gaps) — all addressed before commit. Architecture doc bumped v5.1.8 → v5.1.9; fixit doc at v1.4. |
| 2026-05-18 | session | Phase 2 counter consistency shipped: FX-004 (`e7fc3d2` — `place_orders_for_market` returns int, wrapper accumulates). +34 / -12 lines in `order_lifecycle.py`; +26 / -8 lines in `reward_farmer.py`; +270 lines new `tests/test_order_lifecycle.py` (17 tests across 4 classes). Full fast tier 453 → 470 passing. §2 / §3 / §4 / §6 / §7 / §8 updated; architecture doc bumped v5.1.7 → v5.1.8. |
| 2026-05-18 | session | Phase 1 SafetyController bootstrap completion shipped: FX-002 (I3 cold-start skip, `dc78ba0`) + FX-003 (BOOTSTRAP state with 10/30%/trials permissions, `541108b`) + FX-012 (cold-start default → BOOTSTRAP, subsumed by FX-003). +149 / -32 lines in `oversight/safety_controller.py`, +151 lines new `tests/test_safety_controller.py` (17 tests across two commits), 4-line update to root `test_safety.py`. 453/453 fast-tier tests pass (was 443 pre-Phase-1, +10 from Phase 1 tests under pytest collection). §2 / §3 / §4 / §6 / §7 / §8 updated in lock-step; architecture doc bumped v5.1.6 → v5.1.7. |
| 2026-05-18 | session | Phase 0 housekeeping closed: FX-017 + FX-018 shipped (`3f50441`, `987a844` pushed to `main`); FX-020 reconciled as Fixed (the §11.4 doc edit had already shipped alongside `dd67f97` in v5.1.5 but was never moved to §4 here). §2 status table rows removed; §3 entries deleted; §4 entries added with full retrospectives; §6 Phase 0 ticks all complete; §7 v5.1.6 subsection added. Architecture doc bumped v5.1.5 → v5.1.6 in lock-step (top-of-doc amendments + §10.1 + §10.3 strike-through). |
| 2026-05-18 | session | Architecture-doc amendments shipped: doc bumped v5.1.4 → v5.1.5 with all §7 items closed (top-of-doc scope + amendments, Current Production State table, §10.1 commits, §10.2 B10, §10.3 limitations, §11.4 server provisioning, §11.13 exit-path prose, new §4.22 Orphan position recovery). §7 status column updated to ✅ Done across the board. |
| 2026-05-18 | initial | Created doc. Imported 28 issues from session audit. FX-001 marked Fixed (commit `dd67f97`). Hardening roadmap drafted. Architecture-doc update tracker added. |

(Add new rows on top as the doc evolves.)

---

## 9. How fixes flow

Visual reminder of the lifecycle for an entry:

```
Found in production / audit
        │
        ▼
Add entry in §3 (Open) with FX-NNN ID and detail
        │
        ▼  (work starts)
Update status: In Progress
        │
        ▼  (commit lands on main)
Update entry in §3 → move to §4 (Fixed) with commit SHA
        │
        ▼  (architecture doc amendment)
Tick the row in §7 (Architecture-doc update tracking)
        │
        ▼
Update §8 changelog
```

---

## 10. Quick-reference IDs (cross-reference)

| Cluster | Related IDs |
|---|---|
| Bootstrap deadlock chain | FX-001 (✓), FX-002, FX-003, FX-012, FX-013 |
| Failure-path bookkeeping | FX-004, FX-005, FX-006, FX-007, FX-008, FX-009 |
| Capital sizing | FX-010, FX-011, FX-013, FX-024, FX-025 |
| Operational gaps | FX-014, FX-015, FX-017, FX-018 |
| Test coverage / CI | FX-016, FX-026 |
| Architecture doc accuracy | FX-019, FX-020, FX-021, FX-022, FX-023 |
| Architecture smells (lower priority) | FX-027, FX-028 |

---

*End of document. Update freely.*
