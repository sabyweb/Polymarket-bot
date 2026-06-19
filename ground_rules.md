# Ground Rules — Polymarket Reward-Farming Bot

**Document version:** 1.1
**Created:** 2026-05-26
**Last amended:** 2026-05-28 (implementation-status columns added; no rule
changes; 6/6 auto-correction triggers now wired per session ending
commit `ac5da22`)
**Status:** **IMMUTABLE — these rules govern every architectural and code
decision in this project. Conflicts with these rules are resolved IN FAVOR
of the rules, not the existing code.**

This file is the contract. The architecture doc describes how the system
works; the fixit doc tracks issues; this file states **what the system
must do, regardless of how it works today**. Any architectural decision
that breaks one of these rules is a defect.

---

## Rule 1 — Maximize reward farming

The single optimization target is **total daily reward earnings**, subject
to Rule 3's loss-avoidance constraint. Every architectural choice is
evaluated by its effect on this metric.

**Implications:**
- Be on as many reward-eligible markets as possible, simultaneously.
- Per-market sizing aims for the **minimum** that earns rewards
  (typically `min_size` shares), not the maximum that fits the budget.
  Spreading thin across many markets beats concentrating in few.
- A market is worth deploying if `daily_reward(market) > expected_loss(market)`,
  even if `daily_reward(market)` alone looks small.
- Aggregate sub-threshold accruals across many markets. Polymarket's $1/day
  threshold is per-user, not per-market — 100 markets earning $0.02/day each
  pays out $2/day.

**Forbidden:**
- Conservative defaults that leave reward potential on the table.
- Allocating "70% of wallet" or any similar fixed-fraction without a
  reward-yield justification.
- Filtering out markets just because their *individual* expected reward is
  small — that's an aggregate-strategy violation.

---

## Rule 2 — Leverage Polymarket's capital overcommit

Polymarket's CLOB lets us place limit orders whose total notional exceeds
the wallet balance. **When one order fills, the exchange auto-cancels the
others** (collateral rebalance). This is a fundamental property of the
exchange, not a quirk.

**Concrete example:** with $100 wallet, we can place 10 buy orders at $30
each (= $300 notional) across 10 markets. If market #3 fills, markets
1, 2, 4–10 cancel automatically. Net capital usage: $30 (the one that
filled), the remaining $70 sits in the wallet.

**Implications:**
- Total live notional **should** routinely exceed wallet balance. A
  notional/wallet ratio of 3-10× is the design target, not a kill condition.
- Per-market exposure cap is set to permit overcommit:
  `per_market_cap ≈ wallet / target_market_count`, where `target_market_count`
  is the number of markets we want to be on simultaneously, NOT
  `wallet × small_pct`.
- The kill switch's `notional_ratio` threshold must respect overcommit. A
  ratio of 5× is normal; 50× is a bug. Current `MAX_NOTIONAL_RATIO = 2.0`
  is anti-design — it caps below the design point and triggers false alarms.
- Re-placement after cascading cancels is critical. The bot must have a
  fast re-place loop that re-fills orders within ≤2 cycles after a fill.

**Forbidden:**
- `DEPLOY_RATIO` or any constant that caps `Σ per_market_cost ≤ wallet`.
- Kill triggers based on `notional_ratio > N` without considering whether
  the notional is overcommit-by-design vs. accidental over-exposure.
- Treating "live notional > wallet" as a warning sign in isolation.

---

## Rule 3 — Self-learning loop with mandatory auto-correction

The bot must detect failure modes and self-correct without operator
intervention. Failure modes that must trigger auto-correction:

1. **Insufficient reward earnings** — rolling-window rewards below target
2. **Negative capital efficiency** — rewards / capital_committed below target
3. **Realized losses** — fill+dump losses exceeding rolling-window rewards
4. **Per-market underperformance** — specific markets repeatedly producing losses
5. **Per-market over-fill** — specific markets filling too often
6. **Stale or wrong q_share assumptions** — bot's expected vs Polymarket's
   API-reported reality diverging

**The bot must:**
- Track per-market: rolling-window rewards earned, losses incurred, fill
  count, capital committed time-integrated, ROI
- Track global: daily ROI, daily reward yield, fill rate, capital efficiency
- Maintain a **decision policy** that takes these as input and adjusts:
  per-market allocations, per-market sizing, queue-depth placement, market
  count, kill-switch thresholds
- Continuously execute that decision policy — at minimum every farmer
  cycle (30 s)

**Auto-correction triggers (mandatory):**

| Signal | Trigger | Required response | Status (as of 2026-05-28) |
|---|---|---|---|
| Per-market 24h ROI < −X% across N samples | per market | Mark "cooled down" for `cooldown_period`; reduce allocation to zero | ✅ Wired — FX-051 + FX-057. `decision_policy.evaluate_market`; X=-5%, N=1 (was 3 pre-FX-057). |
| Per-market fill_rate > target by Y× | per market | Increase queue cushion (deeper placement) OR reduce per-market size OR mark cooled | ✅ Wired — FX-059 (P4). When samples_24h/24 > 1.0/hr AND not cooled → `size_reduction_cids` set → allocator halves target_shares. |
| Global 24h reward < target × Z | global | Expand market count, lower per-market expected-reward floor, retry trial markets | ✅ Wired — FX-060 (P10). When total_reward_24h < $4 (= 80% of $5/day floor) AND NOT global_tighten → `global_reward_low=True` → allocator halves MIN_DAILY_RATE_USD + MIN_EXPECTED_PER_MARKET. |
| Global 24h loss > rewards | global | Tighten filters (extreme-price, narrow-spread, persistent losers); reduce per-market exposure | ✅ Wired — FX-059 (P4). When total_loss > 0.5×total_reward OR (loss>0 with reward=0) → `global_tighten=True` → allocator doubles MIN_DAILY_RATE_USD + halves global sizing. |
| API q_share for held position diverges > 2× from bot's internal estimate | per market | Update bot's per-market q_share to API value; recalibrate scoring | ✅ Wired — FX-061 (P11). `simple_oversight.run_once` compares API vs cumulative per held cid; on >2× ratio records `q_share_recalibration_events` row + emits `[LEARN_DIVERGENCE]` log. Next cycle: `q_share_distrust_cids` set → allocator applies extra 0.5× to non-API q_share for those cids (24h window). |
| Realized loss in last hour > threshold | global | Kill switch (existing); requires operator restart | ✅ Wired — existing FX-058 (P1). `_guardrail_check_and_log` evaluates: 24h realized loss > 10% wallet OR CF < 0.01 OR fill_rate_ratio > 3× OR rapid-growth notional burst > 5× over 5 min. |

**6 of 6 mandatory triggers now wired to behavior change** (was 2/6 pre-P4, 4/6 post-P4, 6/6 post-P11).

The kill switch is the LAST line of defense, not the first. The self-
learning loop should prevent kill-switch conditions from arising.

**Forbidden:**
- A static allocator that ranks markets purely on `daily_rate × q_share`
  without consulting historical performance.
- Re-deploying on a market that has produced consistent losses without an
  explicit re-qualification path.
- Dormant calibration models — every component in the learning pipeline
  must either produce a signal that affects bot behavior or be deleted.
  No "code that runs but isn't read" allowed.

---

## Required metrics (must be continuously computed and persisted)

All metrics computed per-market AND globally, rolling over multiple windows
(1h, 24h, 7d).

| Metric | Formula | Window | Implementation (as of 2026-05-28) |
|---|---|---|---|
| `reward_earned` | sum from `/rewards/user/markets` API + DB | 1h/24h/7d per market | ✅ `market_roi_tracker.tick` + `daily_reward_cache` table |
| `fill_count` | count(*) from `fills` table | per market, per window | ✅ `_fill_count_for_window` |
| `fill_loss` | Σ(-pnl) from `unwinds` where pnl < 0 | per market, per window | ✅ `_fill_loss_for_window` |
| `capital_committed_avg` | time-weighted avg of `est_capital_cost` | per market, per window | ✅ `_capital_committed_avg` + `capital_committed_snapshots` table (FX-057 lookback fix) |
| `roi` | `(reward_earned - fill_loss) / capital_committed_avg` | per market, per window | ✅ Bounded to 0.0 when capital_avg < $0.10 per FX-057 (avoids div-by-tiny telemetry corruption) |
| `fill_rate` | `fill_count / window_hours` | per market, per window | ✅ Computed in `tick`; consumed by P4 trigger #3 |
| `q_share_actual` | latest from Polymarket `/rewards/user/percentages` API | per held market | ✅ `SimpleAllocator.fetch_current_q_shares` (Priority 0) |
| `q_share_predicted` | bot's internal estimate | per market | ✅ Cumulative DB ratio (Priority 1) + cold-start prior (Priority 2) in `estimate_q_share` |
| `q_share_error` | `q_share_actual - q_share_predicted` | per held market | ✅ Detected as ratio in `record_qshare_divergence` (P11 trigger #6); FX-061 records breach events in `q_share_recalibration_events` table |

These are inputs to the decision policy. They are observable to the
operator via structured logs (`[LEARN] {...}`, `[LEARN_COOLDOWN]`,
`[LEARN_DIVERGENCE]`, `[LEARN_WARN]`, `[OVERCOMMIT_ALLOC]` per cycle)
and queryable from the DB tables: `market_roi`, `market_cooldowns`,
`capital_committed_snapshots`, `daily_reward_cache`,
`q_share_recalibration_events` (all introduced by FX-051 + FX-061).

---

## Capital-efficiency target

The system targets, in steady state on a $1k wallet:

| Metric | Target | Mechanism (as of 2026-05-28) | Verified |
|---|---|---|---|
| Daily rewards earned | ≥ $5/day (floor) | OverCommitAllocator (FX-052+053) deploys on 50-200 markets at cost-to-score sizing; FX-060 (P10) expands filters when reward falls below 80% of target | ⏳ Empirical — pending P7 G1 7-day clean run |
| Daily ROI (rewards − losses) / wallet | ≥ 0.5% (≈ 180%/year annualized) | EV gate (FX-053) filters negative-EV; FX-051 cooldowns drop persistent losers; P4 triggers reduce sizing on high-fill-rate / global-loss markets | ⏳ Empirical — pending P7 |
| Markets deployed simultaneously | 50-200 | FX-053 dropped 20-cap; soft sanity cap at 500. Adversarially tested at 50 + 200 markets in `tests/test_p2_overcommit_allocator.py::AO-A1, AO-A2` | ✅ Code path verified; production count pending P7 |
| Notional / wallet ratio | 2-8× (overcommit by design) | FX-052 dropped `DEPLOY_RATIO`; FX-058 raised farmer notional caps 2/2.5 → 5/8 cfg-driven + rapid-growth kill catches misconfig | ✅ Code path verified at 4.4× in `AO-A4`; production ratio pending P7 |
| Fill rate per market | < 1 fill / day (rare) | FX-036 queue-aware placement biases away from fills; P4 trigger #3 reduces sizing on high-fill-rate markets | ⏳ Empirical — depends on Polymarket book depth + competitor behavior |
| Q-share API vs predicted error | within 2× | FX-061 (P11) detects divergence > 2× and applies distrust factor; FX-046 formal-accepted residual uncertainty; conservative-factor cfg knob available for runtime tuning | ⏳ Pending observation under P5/P7 live run |

If sustained metrics fall below 80% of target, the auto-correction loop
must trigger. As of 2026-05-28, **all 6 auto-correction triggers are
wired to behavior change** (see table above). What remains is empirical
validation that the bot ACTUALLY HITS these targets under live operation
— the G1 7-day clean run on Helsinki is the verification path.

---

## What the bot is NOT

To clarify what we are not optimizing for, to prevent design drift:

- **Not a directional trader.** Makes no calls on which side wins.
- **Not capital-conservative.** A wallet earning 0% returns isn't success.
- **Not single-axis safety-first.** Safety is Rule 3's domain (auto-correct
  on loss), not Rule 1's (max rewards).
- **Not a backtester.** All signals are live-trading-derived. Sim is hygiene
  only.

---

## Update protocol

This document is **append-only** for the three rules. Implications,
metrics, and targets may be refined; the three core rules may not be
weakened without explicit operator authorization recorded in the change log
below.

### Change log

- 2026-05-28 v1.1 — Implementation-status columns added to three tables
  (auto-correction triggers, required metrics, capital-efficiency target)
  cross-referencing the FX entries that wire each item. No rule changes;
  no rule weakening. Operator-authorized refinement per the "implications,
  metrics, and targets may be refined" clause.

  Status snapshot of the three rules as of this amendment:

  - **Rule 1 (max-farm rewards):** OverCommitAllocator (FX-052+053) deploys
    on 50-200 markets at cost-to-score sizing. Test-verified at 50, 200,
    700 (soft-cap) market counts. Production deployment pending P5/P7.

  - **Rule 2 (capital overcommit 3-8×):** Farmer kill-threshold retune
    (FX-058) raised `MAX_NOTIONAL_RATIO` 2.0 → 5.0 cfg-driven + new
    rapid-growth kill at 5× over 5 min. Allocator's design point is
    cost-to-score per market, no total budget cap. Test-verified
    overcommit at 4.4× wallet notional. Production verification pending.

  - **Rule 3 (mandatory self-learning):** All 6 of 6 auto-correction
    triggers wired to behavior change (was 0/6 pre-FX-051, 2/6 after
    FX-051+057, 4/6 after FX-059, 6/6 after FX-060+FX-061). "No code
    that runs but isn't read" violation resolved.

  Honest current rating: 7.5/10 (was 5/10 pre-session). Path to 9/10:
  G-C (FX-054 production verify against real fill burst) + G-E (G1
  7-day clean run on Helsinki). Both require live operation; runbook
  at `docs/runbooks/9_of_10_p5_p7_operator_runbook.md`.

- 2026-06-14 — **TEMPORARY, TIME-BOUNDED safety loosening (operator-authorized).**
  Operator (Saby) explicitly authorized loosening the **oversight drawdown kill**
  from 15% to **20%** for **8 hours only**, to keep the canary farming through a
  deliberately-accepted ~15% drawdown (peak $1,220.52 is stale; portfolio has held
  ~$1,020–1,050 for days) and continue gathering the per-market net data needed for
  RC-2 market selection. NOT a change to the three core rules; this is a bounded
  override of a *safety threshold*, recorded per the clause above.
  - Mechanism: new `RF_KILL_DRAWDOWN_FRAC` config knob (default 0.15 = unchanged);
    set to **0.20** in `config_overrides.json` for the window; `RF_FARMER_DRAWDOWN_KILL_FRAC`
    raised to 0.20 to match the FX-082 backstop.
  - **Unaffected / still armed:** realized-loss kill (10%/24h), unrealized-loss kill
    (20%), loss-gated fill-rate kill, per-market fill breaker. Only the drawdown-from-peak
    threshold is loosened, and only to 20% (floor ≈ $976, not removed).
  - **Revert:** auto-revert at +8h to 0.15 (remove the overrides; hot-reload). After
    revert the drawdown kill returns to 15% and will fire if still breached — by design.
  - Risk acknowledged: the bot is net-negative (RC-2); farming through the drawdown may
    bleed further. The override trades capital risk for uptime + data, by operator choice.

- 2026-06-15 — **Operator-authorized baseline acceptance + cooldown-soak resume.** (Supersedes the
  2026-06-14 8h entry, which was NOT executed.) Verified ground truth: the $1,220.52 peak is only
  3.3 days old and the portfolio declined steadily ($1,220→$1,117→$1,048→$1,018) — a **real,
  recent ~16% drawdown**, not a stale high-water mark (a windowed peak would hide a real loss, so
  it was rejected). Operator (Saby) explicitly **accepts the ~$200 realized drawdown as the new
  baseline** and authorizes resuming the canary with:
  - **`RF_PREEMPTIVE_COOLDOWN_ENABLED=true`** — the *actual fix* (cools a market after its first
    >0.05/share-slippage fill; addresses the repeat-fill bucket = 66% / ~$153 of 14d losses).
    This is the single measured lever of the soak.
  - **`RF_KILL_DRAWDOWN_FRAC=0.20` + `RF_FARMER_DRAWDOWN_KILL_FRAC=0.20`** — bounded drawdown
    tolerance so it can resume past the real 16.6% drawdown. Kill re-fires at ~$976 (20% of peak),
    i.e. **bounded ~$42 of further downside** while the cooldown is tested.
  - **Still armed:** realized-loss (10%/24h), unrealized-loss (20%), loss-gated fill-rate kill.
    Only the drawdown-from-peak threshold is loosened, to 20%, not removed.
  - **Revert:** `RF_KILL_DRAWDOWN_FRAC`/`RF_FARMER_DRAWDOWN_KILL_FRAC` → 0.15 once the portfolio
    recovers stably above $1,037 (the 15% line), restoring full drawdown protection at the new
    baseline. If instead it bleeds to the 20% line, the re-kill is the signal that cooldown alone
    is insufficient → add the volatility lever (Phase 1.3).
  - Rationale vs the declined 8h override: that was "run and bleed with no fix"; this pairs the
    bounded tolerance with the fix that addresses the majority of the bleed, as a measured soak.

- 2026-06-19 — **Operator-authorized resume as a bounded A/B soak + Halt-Doctor Stage-2
  auto-recovery (recorded per the cardinal-rule clause).** Operator (Saby) authorized, after a
  full ground-truth investigation (snapshot `snapshots/2026-06-19/`, see [[net_unrecoverable_offline]]):
  - **Baseline reset:** accept ~$985 as the resume reference. The $1,220.52 peak is stale deposit-era
    (verified: portfolio $201→$985 over the month was mostly deposits, not P&L; held-to-resolution net
    is NOT recoverable offline). Recorded; deposits FROZEN for the experiment (no money in/out) so the
    forward net is measurable (the only clean net path).
  - **Drawdown floor ~$880:** `RF_KILL_DRAWDOWN_FRAC` AND `RF_FARMER_DRAWDOWN_KILL_FRAC` → **0.28**
    (= $878.77 vs the $1,220.52 peak; ~$105 runway from $985). Real breaches escalate (the Halt-Doctor
    does NOT auto-recover a real drawdown).
  - **Bounded A/B from start:** `RF_AB_EXPERIMENT_ENABLED=true`, `RF_AB_COHORT_COUNT=2` (C0 baseline vs
    C1 calmer-pond `RF_AB_C1_MAX_RECENT_VOLATILITY=0.03`); breadth `RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS=20`,
    per-market cap `RF_MAX_CAPITAL_PER_MARKET_USD=60` (ground truth: live reward-market min_size is
    dominated by min_size-50 = 43% of 1,738 eligible; a $25 cap deploys only min_size-20 = 26% of the
    universe, $60 unlocks min_size<=50 = 70% so the A/B covers the representative bulk; per-market
    notional stays small vs the $880 floor).
  - **Still armed (unchanged):** realized-loss 10%/24h, unrealized-loss 20%, fill-rate spike,
    per-market breaker, per-cohort breaker. Only the drawdown-from-(stale)-peak threshold is loosened.
  - **Halt-Doctor Stage-2 auto-recovery envelope (graduated autonomy, KILL axis):** the supervisor may
    auto-clear + restart ONLY on a `FALSE_POSITIVE` diagnosis (claimed metric POSITIVELY contradicted by
    authoritative on-chain/data-api ground truth AND trip is recent — e.g. the verified 2026-06-13
    DB-missed-fill drawdown deadlock). Bounds: **max 2 auto-recoveries / 24h**, a **re-kill within T
    minutes → permanent hard-stop + escalate**, **always Discord-paged**, **never loosens a threshold**,
    **instant operator override file**, and the **full kill stack stays armed**. `REAL_ACTIVE` /
    `REAL_RESOLVED` / `UNCERTAIN` ALWAYS escalate to a human (cardinal rule intact). The supervisor runs
    only AFTER the first MANUAL operator resume, and is operator-reviewed before deploy. Stage 3
    (unattended widening) remains out of scope.
  - **Revert:** flip the A/B/cap flags off and `*_DRAWDOWN_FRAC` → 0.15 once net-positive + recovered;
    disable auto-recovery via the override file. All reversible (config + flags), recorded, gated
    (`run_audit_v5 --seeds 1 42 1337` + fast `pytest`).
  - Risk acknowledged: strategy is net-negative/unproven; the sweep lever's offline edge rested on ~1
    market and its J was lookahead-inflated; the tight floor may halt mid-soak. The soak is the proof.

- 2026-05-26 v1.0 — Created. Three core rules defined after the
  SimpleAllocator kill-switch event of 2026-05-25 demonstrated that the
  deployed code violated all three rules simultaneously.

---

## Cross-references

- Architecture doc: `~/Downloads/Polymarket bot architecture v5.1.md` (or
  v6.0 once amended). Describes how the system is built.
- Fixit doc: `~/Downloads/Polymarket bot fixit.md`. Tracks issues; new FX-
  IDs FX-051+ document the gaps to current code under these rules.
- Memory: `project_capital_overcommit.md` already documents Rule 2's
  underlying mechanism — this file makes the architectural implication
  explicit.
