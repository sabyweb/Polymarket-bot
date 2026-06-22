# Master Plan — Kill-stack hardening → measurement → profitability (2026-06-22)

> **Status: PLAN for review/lock.** Every "verified" claim below is code-grounded this session (5 parallel
> read-only audits + direct reads); uncertainties are flagged. Discipline (etched in stone): ground truth >
> guesswork; plan → critical-evaluator → harden → build; single-axis, flag-gated default-off, reversible,
> gated (`run_audit_v5 --seeds 1 42 1337` + fast pytest, **hermetic**), soak-proven; a protective kill
> escalates to a human; I implement+gate, the OPERATOR deploys; no AI branding; `main` only.

## 0. Where we are (verified 2026-06-22 ~04:12Z)
Box LIVE on `804e3bb`, `kill_switch:false`, drawdown ~25.9% (< 28% floor), ~$20 runway. **Two safety fixes
in proving windows:** orphan-leak backstop (`72beaf4`, ~Day 1) + merge cost-basis (`804e3bb`, just enabled).
A −$75.40 wallet-reconcile desync is open-but-benign (stable, on-chain cash steady, likely a data-api
rewards-fetch false-positive; watch across the next ~00:20Z settlement).

## 1. The unifying diagnosis (why this is one coherent program, not a grab-bag)
The 2026-06-20 audit + this session's grounding show the **kill stack has clustered silent-bypass paths**,
reducible to four root primitives. The realized-loss kill's only input is `SUM(unwinds.pnl<0)`; a loss
escapes if it (a) writes no row, (b) writes `pnl≥0`, (c) is denominated against a `total_capital` that went
`None`, or (d) the kill flag is RAM-only so a restart resumes past it. Map:

| Axis | Bypass | Status |
|---|---|---|
| Accounting (pnl≥0 / no row) | merge-at-a-loss → +pnl | ✅ FIXED (804e3bb, live) |
| | startup-recovered dump → +pnl (no basis, NOT FX-066-floored) | OPEN — verified still a bypass |
| | no-row write-downs: `_reconcile_positions` set-down + STALE-CLEANUP `remove_market` | OPEN — hard (Site-B trap) |
| | dump live-notional always $0 (`"price"` vs `"fill_price"` typo) → notional-family kills blind | OPEN — 1-line |
| Denominator (`total_capital`→None) | stale/missing alloc disarms realized-loss, unrealized, cluster, notional, rapid-growth **at once** | OPEN — SPOF |
| Durability (RAM-only) | restart boots `_kill_switch_active=False` → resumes past a fired kill | OPEN — Stage-2 prereq |
| Fail-open | dump phantom-check records phantom unwind; oversight wallet-probe skips kill+alloc; unrealized-loss drops held/stale-mid legs | OPEN ×3 |

**Key cross-cut (verified):** the `total_capital` SPOF and the oversight wallet-probe skip are the *same*
failure — a wallet-probe outage skips the oversight kill AND starves the alloc file, which then disarms the
farmer's capital-relative kills. One root, fix together.

## 2. Sequencing model — two tracks under the master invariant
**Master invariant:** ≤1 *behavioral* change in its proving window. A behavioral change alters
placement/sizing/cancel OR a kill/guardrail. Pure instrumentation that only records/reports is non-behavioral
and parallel-safe.

- **Track A (non-behavioral) — unlimited parallel, start now.** Read-only / isolated-store / test-only.
- **Track B (behavioral safety) — strictly one at a time through the single slot, each its own gate + soak.**
  These are *safety-accuracy* fixes (they make kills honest), orthogonal to the A/B **net** experiment — so
  they form a sequential **safety batch** that does not consume the A/B-experiment slot. Each enable is a
  recorded `ground_rules.md` deviation (as merge-cost was). The orphan + merge-cost soaks are the batch's
  first two members.

## 3. TRACK A — measurement & hygiene (non-behavioral, parallel-safe, do now)

### A-1 — Test-hermeticity (PROVEN fix) — **do first**
- **Verified:** `config.py:481` `OVERRIDES_FILE` is `__file__`-relative, loaded import-time via the lazy
  `BotConfig` singleton → box-in-place runs read live overrides → **23 tests fail** (drawdown-frac, breadth,
  per-market cap, A/B-on, trial pct, fill window, merge-acct). Pure config pollution, not regressions.
- **Fix (agent-prototyped + proven, then reverted):** add `tests/conftest.py` with an **autouse fixture that
  snapshots → clears → restores `BotConfig.instance()._overrides`** per test. ~12 lines, one new file, zero
  production change, value-agnostic. Proven: with the box override present → all 23 pass; full tier 1195/0-fail.
- **Payoff:** every future box deploy gates **in-place** (no worktree dance). Reduces (not increases) masking
  risk — it closes the gap where a test that forgot to set a knob silently read a live value.
- **Gate:** fast pytest with + without an override file present. **Slot:** none (test-only).

### A-2 — Promotion-gate clock re-verify (read-only)
- **Verified:** `ab/promotion_gate.py` breaks the consecutive-reconciling-day run on **either** a `gap`
  (no reward_snapshots row) **or** an `out`-of-band day — the ~20h halt (06-20→21) **resets the clock**; the
  numbers self-heal (data-api backfills `__TOTAL__`) but the ≥7-day count restarts *after* the halt.
- **Action:** run `ab/promotion_gate.py` on a fresh post-halt snapshot → record the true forward-clock start
  date so promotion timing is honest. **Slot:** none.

### A-3 — A4 held-to-resolution forward ledger (the dominant UNMEASURED loss term)
- **Verified:** `ab/held_to_res.py` is magnitude-unreliable (DB-rotation seam, deposits-not-in-feed, phantom
  fills) — trusts *identity* only. `ab/fetch_redeem.py` pulls `/activity?type=REDEEM` (needs browser UA).
  Dump-basis net (`net_engine`/`cohort_net`) **overstates** because held positions have no unwind row.
- **Design:** forward from the frozen baseline (confounds vanish post-baseline). Per held position as it
  resolves, record `cid, side, held_shares, cost_basis (captured BEFORE the position vanishes — destructive),
  redeem_proceeds, realized_held_pnl, cohort`. **Isolated store** (separate DB, `reward_snapshots.db` pattern;
  never the live WAL). Resolution signal: redeem feed (magnitude) + `/positions` vanish (timing); the
  `positions` table is live-state-only, unusable historically.
- **Critical synergy + caveat:** the vanish point is the SAME stale-clean path as Change-3 Site B (§4 B-5).
  A4 only **reads/records** (non-behavioral); Change-3 **writes a kill-input row** (behavioral). Keep them
  separate even though same code region. A4 first makes Change-3 Site-B valuation settlement-aware.
- **Gate:** unit tests on the ledger; read-only proof (no bot_history writes). **Slot:** none.

## 4. TRACK B — kill-stack hardening (behavioral; one at a time; recommended order)
Order = highest (safety-value ÷ risk), prerequisites respected. Each: flag-gated default-off (byte-identical),
gated hermetically, operator-deployed, recorded deviation. **Reorderable by the operator.**

### B-1 — Finish the accounting family: startup-dump + dump-notional typo (cheap, coherent)
- **Verified:** startup-recovered dump (`reward_farmer.py:357-363`) logs with `vwap_cost` omitted → `pnl=+amount`,
  NOT FX-066-floored → still a bypass. Fix mirrors the merge fix exactly (lookup `get_avg_price`; floor `pnl≤0`
  if basis unknown). The typo (`reward_farmer.py:1393` reads dead key `"price"`; should be `"fill_price"`,
  `shares` already correct) → 1-line key-swap behind a flag.
- **Why first:** lowest risk, same primitive we just shipped, completes a coherent theme. Two flags
  (`RF_KILL_ACCT_STARTUP_DUMP_*`, `RF_GUARDRAIL_DUMP_NOTIONAL_FIX_*`), each default-off/byte-identical.
- **Red-team:** startup-dump basis often 0 (orphan) → floor is load-bearing; typo enabling raises notional-
  family sensitivity (tiny in current regime). **Gate:** unit + run_audit_v5 + fast pytest.

### B-2 — `total_capital` SPOF (highest systemic value)
- **Verified:** a `None` from `_guardrail_total_capital_from_alloc` (`reward_farmer.py:1319-1376`) skips
  realized-loss, unrealized-loss, cluster, notional-block, hard-enforce, rapid-growth — **simultaneously**.
  Fill-rate is fail-SAFE (kills ratio-only); CF + oversight-silence use own signals. Independent oversight
  kill (live probe) still stands — the SPOF is the farmer's *own* stack going dark between oversight writes.
- **Fix (recommended):** **(a) persist last-known `total_capital`** (with an age cap) and reuse on `None`, so
  all limbs stay armed across an alloc gap; if the cache is *also* absent/expired, fall through to the existing
  block-new-placements (don't silently fail-open). Pair with **B-4's wallet-probe fallback** (same persisted
  value). Defer on-chain-balance fallback (separate axis). Flag `RF_KILL_PERSIST_TOTAL_CAPITAL_*`.
- **Red-team:** cached `T` goes stale-*high* during a real drawdown → limits sit loose by the drawdown % —
  bounded, self-correcting, capped by age. Must NOT false-halt a healthy bot (so cache-reuse, not halt, is the
  primary path). **Gate:** unit (None→reuse cached; cache-expired→block-new) + run_audit_v5 + fast pytest.

### B-3 — Durable kill flag (high value + Stage-2 prerequisite)
- **Verified:** `_kill_switch_active` is RAM-only (init False `:224`, set `:1974`, never persisted; startup
  re-reads nothing → restart resumes). Only durable kill = oversight alloc kill (auto-clears).
- **Fix:** single-row `kill_state` table in `bot_history.db`, written in `_activate_kill_switch`, **read at
  startup → boot HALTED if active**; **fail-safe to halted on a read error**. CLEAR only via explicit logged
  human action (CLI/sentinel) — never auto on restart/timer.
- **Design tension (verified, must honor):** persist ONLY the farmer's *own* guard kills; **do NOT persist the
  oversight-sourced promotion** (`reason` starts `"oversight:"`) so it keeps its auto-clear semantics —
  otherwise routine drawdown recoveries become permanent human-clear halts. Gate the DB write on
  `not reason.startswith("oversight:")`.
- **Red-team:** deadlock risk (sticky kill never clears) → explicit human CLI + Discord page on clear; DB
  corruption → fail-safe halted-and-escalate. **Gate:** unit (restart re-reads active; oversight kill not
  persisted; read-error→halted) + run_audit_v5 + fast pytest. Flag `RF_DURABLE_KILL_ENABLED`.

### B-4 — Fail-open kills (×3)
- **4a dump phantom-check** (`dump_manager.py:77-78`): RPC error → records a possibly-phantom unwind. Fix: on
  verification error treat as indeterminate — do **not** record this cycle; retry with a bounded timeout.
- **4b oversight wallet-probe skip** (`simple_oversight.py:349-353`): probe failure skips the kill check AND
  the alloc write (feeds the SPOF). Fix: retry/backoff; fall back to last-good wallet (share B-2's persisted
  value) so `compute()`/the kill still run; escalate sustained failure to a page.
- **4c unrealized-loss leg-drop** (`reward_farmer.py:1702-1718`): blind to held legs not in `self.markets` or
  with stale/0 mid — exactly the held-to-res losses it should catch. Fix: fall back to last-known mid /
  data-api `/positions`; emit a signal when held notional exists but legs are unmarkable.
- **Red-team:** each is a behavioral kill change → separate single-axis sub-steps (4a/4b/4c sequenced), each
  gated. 4b pairs naturally with B-2. **Gate:** per-fix unit + run_audit_v5 + fast pytest.

### B-5 — No-row write-downs (Change-3) — HARDEST, do last
- **Verified (two sites, different valuation needs):**
  - **Site A** `_reconcile_positions` set-down (`reward_farmer.py:443-446`): partial reduction, cid still on
    exchange → likely external sale / missed dump fill; **cost basis preserved** (set_shares w/o avg_price);
    no mid on hand. Value at mid-or-cost; floor `pnl≤0`.
  - **Site B** STALE-CLEANUP `remove_market` (`reward_farmer.py:831-840`): cid gone from exchange → **usually
    a redemption, often in-the-money**; basis+shares readable from `db_positions[cid]` BUT only *before* the
    delete (destructive). **TRAP:** valuing as full-cost-loss fabricates losses on winning redemptions →
    spurious kills. Default **neutral (pnl 0)** or **settlement-aware** (use A4's resolution outcome), NEVER
    full-cost.
- **Why last:** the Site-B trap needs A4 (A-3) resolution data to value correctly; highest false-kill risk.
- **Red-team:** capture basis before the mutating call (the merge-fix pattern); Site-A vs Site-B need distinct
  defaults under one flag. **Gate:** unit per site + run_audit_v5 + fast pytest. Flag `RF_KILL_ACCT_WRITEDOWN_ROW_*`.

## 5. Phase 2 — Stage-2 halt supervisor (ONLY after B-3)
`ab/halt_diagnose.py` (diagnosis-only) is built + read-only. The auto-execute supervisor is authorized
(`ground_rules.md` 06-19) but **not built**, and its HARD prerequisite is **B-3 durable kill** (without it,
"auto-resume" is a blind restart that also clears unrelated real kills). Invariants (verified from the 06-19
authorization): FALSE_POSITIVE only; re-kill hard-stop; max 2/24h; recency gate; always paged; re-fetch
ground truth at decision time; targeted clear; override-disable file; fail-safe escalate. Build after B-3.

## 6. Phase 3 — profitability / the A/B loop (after the safety floor is solid)
- Reopen the promotion clock (A-2) — ≥7 reconciling days from the post-halt restart.
- Evaluate C0 vs C1 on **real net** (A1 cohort_net + A4 held-to-res), hardened promotion checklist.
- Lever queue (single-axis, after a cohort proves net-positive): `RF_COOLDOWN_CHRONIC_COUNT` 3→2 →
  revolving-door fix → breadth scaling. Net-positive remains UNPROVEN — the soak is the proof.

## 7. Master red-team of THIS plan
| # | Risk | Mitigation |
|---|---|---|
| RT-1 | Stacking many safety fixes muddies attribution / violates single-axis | Track A is non-behavioral (parallel-safe); Track B strictly one-at-a-time, each its own gate+soak; the safety batch is explicitly distinct from the A/B net slot; each enable recorded |
| RT-2 | Change-3 Site-B fabricates losses on winning redemptions → false kills | neutral/settlement-aware valuation (never full-cost); A4 (A-3) first so resolution data exists; do B-5 last |
| RT-3 | B-2 cached `total_capital` stale-high during real drawdown → loose limits | age-capped cache; self-correcting; cache-absent → block-new, not fail-open |
| RT-4 | B-3 durable kill deadlocks or over-escalates the auto-clear oversight kill | source-gate (persist only farmer-own kills); explicit human CLI clear + page; fail-safe halted on read error |
| RT-5 | A behavioral change gated non-hermetically → 24 false failures mistaken for regressions | **A-1 first** → box gate passes in-place; otherwise gate via clean worktree |
| RT-6 | Thin ~$20 runway: kill-accuracy fixes make kills MORE sensitive → could trip mid-soak near the floor | **Operator decision (below):** widen floor or pause the soak clock during the safety batch; each fix is forward-only + flag-revertible |
| RT-7 | A4 (non-behavioral) and Change-3 (behavioral) touch the same vanish path → accidental coupling | A4 reads/records only; Change-3 writes the kill row; ship A4 first, Change-3 as its own gated behavioral step |
| RT-8 | Stage-2 supervisor built before its durable-kill prereq → blind-restart risk | hard-ordered: Phase 2 only after B-3 |
| RT-9 | Completeness: are these ALL the bypasses? | two-pass + 5-agent grounding, NOT a proof of exhaustiveness → run a **completeness sweep** ("what else reduces capital without a pnl<0 row or disarms a kill?") before declaring the family closed |
| RT-10 | Each behavioral fix resets that fix's soak clock | accepted; safety batch is sequential; the A/B net clock is independent |

## 8. Decisions to lock
1. **Track A now, in parallel?** Recommend yes: A-1 (hermeticity) → A-2 (clock) → A-3 (A4 ledger). Non-behavioral.
2. **Track B order** — recommend B-1 (accounting family) → B-2 (SPOF) → B-3 (durable kill) → B-4 (fail-opens) → B-5 (write-downs). Reorder freely.
3. **Thin-runway policy during the safety batch (RT-6)** — widen the drawdown floor temporarily, pause the soak, or accept the risk? Operator's call (threshold change = recorded authorization).
4. **Stacking vs strict single-axis** — confirm the "safety batch stacks, recorded" framing (as merge-cost) vs one-fix-fully-soaked-before-the-next.

## 9. Recommended immediate next step
**A-1 (test-hermeticity conftest)** — non-behavioral, agent-proven, unblocks clean in-place box gating for
every subsequent fix, zero slot cost. Then A-3 (A4 ledger) in parallel while you decide the Track-B order
and the runway policy.
