# A/B Resume Design — bring the bot back online as a bounded learning experiment

> **Status: DESIGN ONLY. No code yet.** Goal: resume from the current halt so the bot collects
> real data and *learns*, not bleeds. It runs parallel cohorts, measures net-per-dollar, and promotes
> winners — and it is bounded so it **cannot** recreate the 20% drawdown that halted it.
>
> Grounded in the findings of `FINDINGS_AND_DECISION_2026-06-17.md` + this session's probes:
> prediction at entry is exhausted; the loss is post-fill adverse selection; the only live levers are
> the *pond* (calmer markets) and *real-time reaction*. No AI/Anthropic branding in any commit.

---

## 0. The drawdown reality (must be handled first)

Verified: portfolio **$974.87**, all-time peak **$1,220.52**, drawdown **20.1%**, reconciled on-chain,
all cash, zero positions, oversight drawdown kill ACTIVE. The kill measures drawdown from the *all-time
peak* (`MAX(total_value)`), so **any** resume is already at/over 20% — it will instantly re-trip.

The $1,220 peak reflects a different era/config and is **not a meaningful recovery target for a
deliberately-bounded experiment**. So the experiment measures risk as **incremental loss from the
resume point ($975)**, not recovery to the stale peak. Concretely (see §5): set a hard floor at
**~$880** (≈10% below resume) and keep most capital in cash so the floor is a backstop, not the
primary control. This is a recorded safety-threshold override per the ground-rules change-log
discipline.

---

## 1. Design principles

1. **Bounded downside first.** The experiment risks a small, fixed slice of capital; the rest sits in
   cash. It physically cannot drive a large portfolio drawdown.
2. **Parallel cohorts** (operator's choice) for same-period, market-mix-controlled comparison.
3. **Prove first, then push** (operator's choice): overcommit stays at the current ratio until a
   cohort demonstrates positive net-per-dollar; only then do we scale it.
4. **Learn and promote**: the winning cohort's rule becomes the next baseline; a fresh challenger
   replaces it. That cycle *is* the recursive self-improvement — each round the baseline gets better.
5. **The soak is the proof.** Offline is a filter; net-positive must be shown live over ≥1–2 weeks.

---

## 2. Cohort model — assignment & measurement

**Assignment (random but stable):** `cohort(cid) = int(sha1(condition_id), 16) % N`. Every market is
deterministically and pseudo-randomly assigned to one cohort and stays there. Random assignment means
each cohort sees a *representative* market mix, so differences in net are attributable to the **rule**,
not to which markets happened to land where.

**Measurement is now explicit, not offline-only.** A dedicated `ab_cohort_pnl.py` module joins
`candidate_features.db` (which records the cohort a market was deployed under), `reward_snapshots.db`
(per-market actual earnings), and `bot_history.db` `fills`/`unwinds` to produce a continuously-updated
`cohort_pnl` table. Because cohort assignment is a stable function of `condition_id`, the treatment
branches on `cohort(cid)` and the analyzer verifies the recorded cohort matches the deterministic
assignment. A separate `volume_24h_cache` table feeds the C1 volume cap with real per-cid volume.

---

## 3. The three cohorts

Each gets ~1/N of eligible markets via the hash. Each tests one of the surviving levers.

**C0 — Baseline (control).** Current selection rules, unchanged. We already know it's ~net-negative;
it is the **bar to beat** and the control that proves the other cohorts' differences are real. Its
bleed is the cost of having a control, and it is bounded by §5.

**C1 — Calmer pond / trader rules** (tests Lesson 3: high-reward = high-activity = adverse). Within
its bucket, apply the trader-rule bundle: `RF_ALLOC_MAX_RECENT_VOLATILITY` 0.10 → **0.03** (only
markets whose mid barely moved over the vol window), `RF_AB_C1_MIN_HOURS_TO_RESOLUTION` 4h (looser
than the 48h baseline, so C1 can trade shorter-dated calm markets), `RF_AB_C1_MAX_VOLUME_24H`
$250k (exclude the highest-volume / highest-competition markets),
`RF_AB_C1_TARGET_QUEUE_AHEAD_USD` $400 (sit closer to mid than the baseline $1000 queue shield),
and `RF_AB_C1_SECOND_BEST_COURT_ENABLED` (never post a strictly better quote than the current best;
join behind or at the best level). This is the "quiet, selective, closer-to-mid, second-in-line"
cohort — the closest automatable version of the manual edge. *Allocator + placement side; small branch
on `cohort(cid)`, no new runtime behavior when the A/B experiment is off.* (If very few markets qualify,
that itself is a finding: calm + $10-reward markets are now scarce.)

**C2 — Real-time reaction** (tests Lesson 1: react to the move, since the move is observable even if
not predictable). Baseline selection, but the **farmer pulls all quotes on a market when its mid is
actively moving** — `|Δmid|` over the last K ~30s cycles exceeds a threshold — and does not replace
until it's been calm for M cycles. Sheds fills *during* moves (when adverse selection happens).
*Farmer-side; new reflex, must be gated and behind a flag.*

---

## 4. Capital & risk bounds

| Bound | Value | Rationale |
|---|---|---|
| Experiment deployed-notional budget | **~$400** total | Caps data-rate exposure; rest of the ~$975 stays cash. |
| Cash reserve (untouched) | **~$575+** | Keeps the portfolio well above the floor regardless of cohort behavior. |
| Per-market cap (`RF_MAX_CAPITAL_PER_MARKET_USD`) | **~$15–20** | Forces min-size, spreads thin (Ground Rule 1) → maximizes *fills/data per dollar of risk*. |
| Overcommit ratio | **current, NOT pushed** | Prove-first; raise only after a cohort proves positive. |
| Per-cohort circuit breaker | pause a cohort if its rolling net-per-dollar < a set floor | Contains a losing arm without halting the whole experiment. |
| Experiment halt floor | **portfolio < ~$880** | Hard backstop (~$95 / ~10% from resume); halts all cohorts. |
| Existing kill stack | **fully intact** | Realized-loss 10%/24h, fill-rate breaker, per-market breaker, etc. all remain. |

Because realized loss (not notional) is what moves the portfolio, the **floor + small per-market cap**
are the real containment; deployed notional just sets how fast we gather data.

---

## 5. Drawdown decision (exact)

- **Reference reset:** treat $975 (resume) as the experiment baseline; protect a **$880 floor**.
- **Mechanism without code change:** the floor of $880 against the stale $1,220 peak equals a drawdown
  of (1220−880)/1220 = **27.9%**, so set `RF_KILL_DRAWDOWN_FRAC = 0.28` (hot-reload). Documented
  explicitly: this is *not* loosened risk appetite — it encodes an **absolute $880 floor** on a
  deliberately small experiment, with most capital in cash.
- **Recorded** in `ground_rules.md` change-log with this reasoning.
- **Revert plan:** once a cohort proves net-positive and the portfolio recovers above **$1,037**,
  restore `RF_KILL_DRAWDOWN_FRAC = 0.15` and the normal regime.

---

## 6. Net-per-dollar metric (per cohort, daily + cumulative)

For each cohort over a rolling window:
- **dump P&L** — `SUM(unwinds.pnl)` on the cohort's markets. *Exact, hard signal.*
- **reward** — per-market reward estimate (`reward_market_stats` / `reward_earned_est`) summed per
  cohort. *Estimate (FX-046: the q_share prior is 24–94× off), so it's directionally useful for
  comparing cohorts but not a precise absolute; cross-checked against the authoritative data-api total
  reward (which is portfolio-wide, not per-cohort).*
- **capital** — average deployed (from `fills.position_usd_after` / allocator target).
- **net_per_dollar = (reward_est + dump_pnl) / avg_capital**, per day.

**Robust comparator (no estimate):** also report each cohort's **dump-loss per dollar** and **fill
rate** — exact, estimate-free. The winner must look better on the hard cost-side numbers, not only on
the reward estimate.

**Winner = highest net-per-dollar, positive sign, sustained over the window** (≥7 days, ideally 14).

---

## 7. The learning loop (this is the self-improvement you asked for)

Each cycle (≈ every 1–2 weeks):
1. Score all cohorts on §6.
2. If a challenger (C1/C2) **beats baseline and is net-positive** → its rule becomes the **new
   baseline (C0)** for the next cycle.
3. Spin up a **fresh challenger** in the vacated slot (e.g. a tighter calmer-pond, a different
   reaction threshold, a sizing variant).
4. **Only now** (a cohort is proven net-positive) push overcommit on the winning rule's markets, per
   prove-first.
5. Repeat. The baseline ratchets upward; the system gets measurably better each cycle.

Decisive negative outcome (stated up front): **if, after the window, no cohort — including the
calmer-pond that mirrors your manual edge — is net-positive**, that is a strong, ground-truthed signal
that the strategy is structurally negative-EV on the markets we can currently reach, and we stop and
rethink the venue/approach rather than keep bleeding.

---

## 8. Phased rollout (get online fast, add the harder lever next)

- **Phase 1 (online quickly):** cohorts **C0 + C1** only (both allocator-side, cfg + a small
  `cohort(cid)` branch — no new runtime behavior). Resume with the §4 bounds and §5 drawdown.
  Gate: `run_audit_v5 --seeds 1 42 1337` + fast `pytest` + a unit test that C1's tighter gate fires
  only on its bucket and C0 is byte-identical to today. This alone gets the bot online, collecting
  data, and testing the #1 hypothesis (quiet markets) within a day.
- **Phase 2:** build + gate the **C2** real-time-reaction reflex (the only new runtime behavior),
  add it as the third cohort.
- **Phase 3:** promote the winner (§7) and push overcommit on it (prove-first).

---

## 9. Gate, safety invariants, and stop conditions

- **Blocking gate before any deploy:** `python3 -m simulation.run_audit_v5 --seeds 1 42 1337`
  (INV3/5/7 pass) + `pytest tests/ --ignore=tests/test_simulation.py -q`. Never the full suite on prod.
- **Single new behavior per phase** (P3 discipline): Phase 1 changes selection only; Phase 2 adds the
  one reflex.
- **All existing kills remain armed**; the experiment adds the $880 floor + per-cohort breakers on top.
- **Reversible:** every cohort treatment behind a default-off flag; revert = flip flags + restore
  `RF_KILL_DRAWDOWN_FRAC=0.15`.
- **Recorded:** the resume + drawdown override go in `ground_rules.md`.
- **Stop conditions:** experiment halts if portfolio < $880; a cohort pauses on its breaker; the whole
  thing stops and escalates on any sticky kill (cardinal rule — never blind-restart).

---

## 10. Open decisions for you

1. **Experiment budget:** ~$400 deployed / ~$575 cash, or a different split? (Smaller = safer/slower
   data; larger = faster data/more risk.)
2. **Floor:** $880 (≈10% from resume) — tighter or looser?
3. **Phase 1 scope:** launch with C0+C1 now (fast, no new runtime code) and add C2 next — or wait and
   launch all three together?
4. **Cohort count / budget split:** equal thirds, or weight more capital to the challenger you believe
   in most (e.g. calmer-pond)?
