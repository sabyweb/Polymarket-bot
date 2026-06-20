# Recursive Learning Loop — Build Plan (2026-06-20)

> **Status: DESIGN ONLY. No code, nothing deployed. For review + lock.**
> Extends `LOOP_PLAN.md` (Loops A/B) and `docs/RECURSIVE_SELECTION_LOOP.md` (Loop C autonomy contract).
> Operator answers locked this session: **autonomy = Stage 1** (loop ranks; a human applies every
> promotion + threshold change); **sequencing = both tracks in parallel**. Prime directive applies:
> ground truth not guesswork; single-axis, gated, reversible, soak-proven; kills escalate to a human;
> no AI/Anthropic branding; `main` only.

---

## 0. What this is (grounded, not aspirational)

The "recursive self-improving loop" you asked for is **not a price/market predictor** — that path is
**refuted by recorded evidence** (`RECURSIVE_SELECTION_LOOP.md §11–12`): no decision-time feature
(volatility, spread, depth, price-extremity, time-to-event, category) separates net-positive from
net-negative markets, and a capital cap is mis-targeted because the loss scales with *re-entries*, not
position size. The convergent diagnosis is **re-entry tuition** + an **unmeasured held-to-resolution
tail**.

So the loop is a **disciplined closed-loop experimenter**: it runs single-axis A/B experiments on
levers, measures **real per-market net per $ capital**, ranks winners under a **breadth/reward floor**,
and a **human promotes** within the graduated-autonomy envelope. That *is* recursive self-improvement —
of the only kind the ground truth permits. The A/B cohort machinery **is** the recursion; it is
~half-built. This plan completes and hardens it.

---

## 1. The master invariant — how "both in parallel" stays single-axis (P3)

This is the rule-compliance spine. Read it first.

> **At most ONE behavioral change is in its proving window at any time.** A "behavioral change" is
> anything that alters which orders get placed / sized / cancelled (a selection lever, a sizing knob,
> a cooldown parameter, a kill/guardrail change). Pure instrumentation that only *records and reports*
> is **not** a behavioral change and runs freely alongside.

Therefore:
- **Track A (measurement substrate) = 0 behavioral axes.** Read-only / append-only-to-isolated-store /
  offline. Always parallel-safe. *Invariant: if any Track-A item ever starts gating selection or
  sizing, it has become a behavioral axis and must be sequenced, not parallelized.*
- **Track B (the experiment) = exactly 1 behavioral axis at a time** — the cohort treatment currently
  under test. The behavioral slot is **already occupied** by the live C1 vol-gate A/B, so Track B's
  immediate job is to *measure the change already running*, not stack a second one.
- **Safety-stack changes** (per-cohort breaker, durable-kill fix) are behavioral changes too; they
  share the single slot and are **sequenced**, never landed concurrently with a lever rotation or each
  other.

"Both in parallel" = **Track A instrumentation ∥ the one active Track-B behavioral change.** That is
the honest reading of the operator's answer, and it does not violate P3.

---

## 2. What already exists (build less than it sounds)

| Component | State | Anchor |
|---|---|---|
| A/B cohort assignment `sha1(cid)%N` (byte-parity tested) | **built, live** | `simple_allocator.py:496`, `ab/cohort.py:15`, `tests/test_ab_cohort_parity.py` |
| C0 baseline vs C1 calmer-pond (vol gate 0.03) | **built, live** | `simple_allocator.py:917-923` |
| A/B total-capital budget ($400), per-market cap ($60) | **built, live** | `simple_allocator.py:993`, `:979` |
| `reward_snapshots.db` per-market reward (canonical, ~85% capture) | **built, live (hourly)** | `reward_snapshot.py` |
| `capture_per_market_reward.py` + capture-ratio gate [0.7,1.3] | **built** (verify it's cron'd) | `capture_per_market_reward.py:135` |
| `ab_cohort_metrics.py` cohort analyzer | **built — but `net/$` = "n/a"** (reads aggregate only) | `ab_cohort_metrics.py:71,114` |
| `ab/` offline net spine (net_engine/lever_replay/net_reconcile/held_to_res/fetch_redeem) | **built** | `ab/*` |
| FX-097 chronic block (≥3 strikes → permanent) | **built, live — WORKS, do not fix** | `decision_policy.py:544` |
| `RF_COOLDOWN_CHRONIC_COUNT` 3→2 | knob exists, **not deployed** | `config.py:370` |
| Revolving-door fix (decouple cooldown from 24h ROI window) | **proposed, not built** | `RECURSIVE_SELECTION_LOOP.md:295` |
| `candidate_features` log (deployed AND avoided) | **proposed, not built** | `RECURSIVE_SELECTION_LOOP.md:75` |
| Held-to-resolution forward tracker | **proposed, not built** | `ab/held_to_res.py` (offline, magnitude-unreliable) |
| Per-cohort circuit breaker | **proposed, not built (Phase 1b)** | `AB_RESUME_HANDOFF.md:85` |
| Predictive feature→risk model; capital cap as *selector*; β/η bandit | **REJECTED / shelved** | `RECURSIVE_SELECTION_LOOP.md:266,283,106` |

---

## 3. TRACK A — Measurement substrate (instrumentation; 0 behavioral axes)

The loop can only optimize toward net-positive once **real per-market net** exists. Every item here
only records/reports; none change trading behavior.

- **A0 — Verify the live measurement is real (read-only, ground truth first).** Re-verify at source,
  not from these docs: (a) `polymarket-reward-snapshot` timer healthy + forward capture reconciling;
  (b) `capture_per_market_reward.py` actually runs daily (is it cron'd?); (c) **which live-enabled
  knobs actually fire** — count `market_cooldowns` inserts to confirm whether `RF_PREEMPTIVE_COOLDOWN`
  is inert on maker fills (`COOLDOWN_TRIAL_PLAN.md` says it "never fires"; `[inferred]` — verify by
  row count). *Exit:* we know what the live config is really doing, measured not assumed.

- **A1 — KEYSTONE: make per-cohort `net/$` real.** Wire the cohort analyzer to `reward_snapshots.db`
  (the canonical per-market reward), joining on `(date, condition_id)`, so `net/$ = (reward_measured +
  dump_pnl)/avg_capital` per cohort instead of "n/a". Present `net/$` as a **band**, not a point
  (carry `reward_snapshots`' ~85%/+1-day capture as the uncertainty, exactly as `ab/net_engine`
  already does), and keep **`dump_loss/$` (exact) as the primary comparator**, `net/$` (banded) as
  secondary. *Without A1 there is no trustworthy net and the loop cannot rank a winner.* Read-only.
  *Exit:* `ab_cohort_metrics.py` prints a real net/$ band per cohort, cross-checked vs data-api aggregate.

- **A2 — Promotion-gate as a machine-checkable artifact.** A read-only check that, per forward day,
  reports whether per-market reward reconciles to the data-api aggregate within [0.7,1.3] and counts
  **consecutive reconciling days** — the ≥7-day promotion precondition becomes queryable, not eyeballed.
  *Exit:* a one-command "are we allowed to promote yet?" answer with the day count + capture ratio.

- **A3 — `candidate_features` log (survivorship fix; append-only, isolated, fail-open).** Log every
  candidate's deploy-time feature vector at allocation time — **deployed AND avoided** — so the avoided
  set is observable. Write to an **isolated store** (separate DB, `reward_snapshots.db` pattern — NOT a
  new table in the live WAL) with the write wrapped fail-open so it can never raise into the allocator
  cycle. A test must assert the allocator's **deploy output is byte-identical** with logging on vs off.
  *Exit:* the counterfactual surface is recorded; still zero behavior change (gated, output-identical).

- **A4 — Held-to-resolution forward ledger (the dominant UNMEASURED loss).** A read-only per-position
  resolution tracker (extends `ab/fetch_redeem` + `ab/held_to_res`) that, **forward from the frozen
  baseline**, records held-position outcomes as they resolve, so the third objective term
  (`held_to_resolution_loss`) becomes measurable going forward. Records only; never gates. *Exit:* a
  forward held-to-res ledger that can retroactively correct dump-basis net (see red-team RT-5).

**All of A0–A4 are non-behavioral → parallel-safe with Track B.**

---

## 4. TRACK B — The experiment (exactly 1 behavioral axis at a time)

- **B0 — Measure the change already running.** The behavioral slot is occupied by C1 (vol-gate 0.03 vs
  C0 0.10). Do **not** add a second concurrent lever. Once A1 makes net/$ real, evaluate C0 vs C1 on
  the hardened criterion (B1). Define a **decision deadline**: if after the capture-gate passes (A2)
  C1 shows no separation on the exact `dump_loss/$` comparator, declare it **null** and rotate the slot
  — a weak lever must not hog the single behavioral slot indefinitely.

- **B1 — Hardened promotion criterion (Stage 1; a CHECKLIST the human applies, not a gut call).** The
  recorded design promotes on point-estimate net/$ + sign, with **no significance test** — a real
  weakness at ~10 markets/cohort where one fat-tail market dominates. Harden to ALL of:
  1. **Primary, exact:** challenger beats baseline on `dump_loss/$` (estimate-free).
  2. **Secondary, banded:** challenger's net/$ band is ≥ baseline's and positive-signed.
  3. **Outlier-robust:** the sign of (1) and (2) survives dropping the worst-1 and worst-3 markets
     (the `ab/lever_replay` outlier test — one Hormuz-class market can flip a 7-day result).
  4. **Min-sample:** ≥ floor markets + fills per cohort (set in B-config; thin cohorts → "insufficient").
  5. **Capture-gate passed:** A2 reports ≥7 consecutive reconciling days.
  6. **Anti-Goodhart floor:** challenger does not collapse deploy count or aggregate reward below
     baseline (a "do-nothing" cohort is disqualified, never ranked #1).
  7. **Provisional:** promotion is on **dump-basis** net; the A4 held-to-res tail can **retroactively
     invalidate** it (RT-5). The human accepts a promotion as provisional-pending-resolution.
  Every promotion is recorded in `ground_rules.md`; the loop only **proposes the ranked card**.

- **B2 — Lever queue (single-axis rotation through the one slot), grounded in the evidence:**
  1. **C1 calm-book / sweep-rate selection** — live now; fragile (offline edge rested on ~1 market,
     J lookahead-inflated). Being tested.
  2. **`RF_COOLDOWN_CHRONIC_COUNT` 3→2** — primary surviving cheap knob (built, off); one fewer tuition
     strike per repeat loser.
  3. **Revolving-door fix** — decouple cooldown duration from the 24h ROI window (judge reactivation on
     a 7d window) so one-off losers stop being re-admitted; code change, gated.
  4. **Breadth scaling** (`RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS` step-wise) — Phase 3, **only after** a
     cohort proves net-positive; each rung its own ≥7-day soak.
  Each takes the behavioral slot one at a time; rotating resets that lever's soak clock (acknowledged).

- **B3 — Per-cohort circuit breaker (safety; sequenced, not concurrent).** A guardrail that auto-pauses
  a cohort whose realized loss breaches a per-cohort threshold (containment finer than the $880 floor).
  It is a *kill-class* change (it changes behavior by halting), so it lands as its **own** gated
  single-axis step in the behavioral slot, default-off, not concurrently with a lever rotation.

---

## 5. Governance & autonomy (Stage 1 locked; Stage 2 prerequisites tracked)

- **Stage 1 only.** The loop logs, measures, ranks, and writes a model/cohort card. **A human applies
  every promotion and every threshold change**, recorded in `ground_rules.md`. No loop deploys capital,
  edits live config, restarts a service, or clears a kill. (`RECURSIVE_SELECTION_LOOP.md:218`.)
- **Stage 2 (bounded auto-apply) is OUT OF SCOPE here** and requires a separate recorded authorization.
  Two hard prerequisites are tracked, NOT built now:
  1. **Durable kill flag.** The farmer's own guard kills are RAM-only (`reward_farmer.py:222,1893,2301`)
     — a crash-restart resumes trading without the human review the cardinal rule assumes. Any
     auto-acting tier must sit on a kill that survives a restart. *(Also worth fixing on its own safety
     merit; it is a kill-stack change → its own sequenced single-axis step, not concurrent with B.)*
  2. **The auto-execute halt-supervisor** (authorized, not built; `ground_rules.md:264`).
- **Stage 3 (unattended widening): out of scope, full stop.**

---

## 6. Critical-evaluator pass (red-team) + hardening

Per rule #2, here is the adversarial pass on *this* plan (new cases; `LOOP_PLAN.md §5` covers the
generic research-loop cases and still applies). Each: failure → why it bites → hardening (folded into
§§3–4 above).

| # | Failure mode | Why it bites | Hardening |
|---|---|---|---|
| RT-1 | **Biased net from `reward_snapshots` (~85%, +1-day shift)** | A1 join under-counts reward ~15% and shifts a day → a real winner looks losing, or vice-versa | net/$ presented as a **band** w/ capture ratio as uncertainty; **`dump_loss/$` (exact) is the primary gate**; net/$ never *alone* flips a promotion (B1.1–1.2) |
| RT-2 | **Thin-cohort false winner** (~10 mkts/cohort; one fat-tail dominates; no stat test) | 7-day net on 10 markets is noise; the design has only point-estimate+sign | **outlier-robust** (drop worst-1/worst-3) + **min-sample** + sign-stability; promotion labeled "point estimate, not significance-tested" (B1.3–1.4) |
| RT-3 | **Weak lever hogs the single slot** (C1 may separate nothing) | the one behavioral slot is finite; an inconclusive C1 blocks testing chronic-3→2 forever | **decision deadline** (B0): C1 declared null on the exact comparator after the capture-gate window → rotate the slot |
| RT-4 | **Track A silently becomes behavioral** (e.g., candidate_features logging crashes the cycle, or someone wires it to gate selection) | breaks the "0 axes" claim → 2 concurrent changes, or a live crash | isolated store + **fail-open** wrapper; **output-byte-identical test**; master invariant forbids any A-item gating selection (A3, §1) |
| RT-5 | **Held-to-resolution lags weeks** → 7–14-day promotion is still missing the third term | we promote on dump-basis net; a held position resolves badly later | promotion is **provisional**; A4 forward ledger can **retroactively invalidate**; operator accepts this is inherent (can't measure resolution before resolution) (B1.7) |
| RT-6 | **Capture-gate never passes** (settlement timing keeps ratio outside [0.7,1.3]) | promotion blocked indefinitely → loop stalls | A2 surfaces it daily; a structural reconcile failure is a **measurement bug to fix first**, and is itself a finding — never promote on un-reconciled net |
| RT-7 | **Inert knob mis-attribution** (preemptive cooldown "never fires", S-9) | carrying a dead knob; wrong attribution of cohort difference | A0 **counts whether each enabled knob actually fires**; if inert, it cancels across cohorts (note it, don't credit it) |
| RT-8 | **Two reward numbers in the system** (triggers use capital-pro-rata `market_roi`; promotion uses `reward_snapshots`) | crossing them corrupts a decision | **authoritative-source map**: triggers→pro-rata (fine for relative cooldown); promotion→`reward_snapshots` measured. Never cross. |
| RT-9 | **Accidental multi-axis** (enable per-cohort breaker *while* C1 runs) | violates P3; un-attributable result | **pre-change checklist** asserts no other behavioral change is mid-soak; B3 sequenced (§1, §4) |
| RT-10 | **Human is the weak link** (eyeballs a thin table, promotes on noise → whack-a-mole returns) | Stage 1 leans on human judgment | B1 is a **codified checklist**, not a gut call; the loop refuses to rank a candidate that fails any gate |
| RT-11 | **Single-source trust** (data-api stale/wrong taken as ground truth) | wrong net conclusion | cross-check `net_reconcile` farming_pnl vs data-api reward+dump; flag divergence; never auto-act on one source (carries `LOOP_PLAN` C11) |
| RT-12 | **Snapshot/WAL hazards** (read the live WAL; torn `.backup`; disk) | corrupt input or observer-effect on the 30s farmer | offline runs on `sqlite3 .backup` opened `immutable=1`; never attach the live WAL; capacity pre-check (carries `LOOP_PLAN` C7/C8) |

**Completeness honesty:** this is a strong two-pass effort, not a proof of exhaustiveness. The standing
control is that this table is living, and each build phase re-runs the adversarial pass before it lands.

---

## 7. Phased rollout (each reversible; stop-and-confirm between phases)

Single behavioral slot = currently C1. Track A is parallel-safe throughout.

- **P1 (Track A, non-behavioral, do first):** A0 verify → A1 wire net/$ → A2 promotion-gate artifact.
  Pure measurement. *Lands the keystone: a real net comparator + a machine-checkable promotion gate.*
- **P2 (Track A, non-behavioral):** A3 candidate_features (isolated, output-identical) → A4 held-to-res
  forward ledger. Closes survivorship + makes the third objective term measurable.
- **P3 (Track B, the one slot):** evaluate C0 vs C1 on the hardened criterion (B1); decide promote /
  null-and-rotate. If rotate → next lever = `RF_COOLDOWN_CHRONIC_COUNT` 3→2 (gated, recorded).
- **P4 (safety, the one slot, sequenced):** per-cohort breaker (B3); and — on its own safety merit —
  the durable-kill fix. Each gated, each alone in the slot.
- **P5 (Track B):** revolving-door fix → then breadth scaling, only after a cohort proves net-positive.

Gate before **any** behavioral change (P3+): `python3 -m simulation.run_audit_v5 --seeds 1 42 1337`
(INV3/5/7) + `pytest tests/ --ignore=tests/test_simulation.py -q`. Never the full suite on prod.
I do not restart services or edit live config — the operator does, after the gate passes.

---

## 8. What I need from you to lock

1. Confirm the **master invariant** (§1) is the right reading of "both in parallel."
2. Confirm **P1 first** (verify + wire net/$ + promotion-gate) — it's read-only, it's the keystone, and
   it unblocks every promotion decision.
3. Two plan-internal calls (I have recommendations; override freely): (a) the C1 **decision deadline**
   for B0/RT-3 — how long before a non-separating C1 is declared null; (b) whether the **durable-kill
   fix** is pulled forward as a standalone safety step (recommend: yes, on its own merit) or deferred
   to the Stage-2 prerequisite list.
4. Anything the red-team (§6) is missing that you're worried about — your adversarial pass on top of mine.

No code lands until this is locked and you sign off phase by phase.
