# Ground Rules — Polymarket Reward-Farming Bot

**Document version:** 1.0
**Created:** 2026-05-26
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

| Signal | Trigger | Required response |
|---|---|---|
| Per-market 24h ROI < −X% across N samples | per market | Mark "cooled down" for `cooldown_period`; reduce allocation to zero |
| Per-market fill_rate > target by Y× | per market | Increase queue cushion (deeper placement) OR reduce per-market size OR mark cooled |
| Global 24h reward < target × Z | global | Expand market count, lower per-market expected-reward floor, retry trial markets |
| Global 24h loss > rewards | global | Tighten filters (extreme-price, narrow-spread, persistent losers); reduce per-market exposure |
| API q_share for held position diverges > 2× from bot's internal estimate | per market | Update bot's per-market q_share to API value; recalibrate scoring |
| Realized loss in last hour > threshold | global | Kill switch (existing); requires operator restart |

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

| Metric | Formula | Window |
|---|---|---|
| `reward_earned` | sum from `/rewards/user/markets` API + DB | 1h/24h/7d per market |
| `fill_count` | count(*) from `fills` table | per market, per window |
| `fill_loss` | Σ(-pnl) from `unwinds` where pnl < 0 | per market, per window |
| `capital_committed_avg` | time-weighted avg of `est_capital_cost` | per market, per window |
| `roi` | `(reward_earned - fill_loss) / capital_committed_avg` | per market, per window |
| `fill_rate` | `fill_count / window_hours` | per market, per window |
| `q_share_actual` | latest from Polymarket `/rewards/user/percentages` API | per held market |
| `q_share_predicted` | bot's internal estimate | per market |
| `q_share_error` | `q_share_actual - q_share_predicted` | per held market |

These are inputs to the decision policy. They are observable to the
operator via structured logs (`[LEARN] {...}` per cycle) and queryable
from the DB.

---

## Capital-efficiency target

The system targets, in steady state on a $1k wallet:

| Metric | Target |
|---|---|
| Daily rewards earned | ≥ $5/day (floor) |
| Daily ROI (rewards − losses) / wallet | ≥ 0.5% (≈ 180%/year annualized) |
| Markets deployed simultaneously | 50-200 |
| Notional / wallet ratio | 2-8× (overcommit by design) |
| Fill rate per market | < 1 fill / day (rare) |
| Q-share API vs predicted error | within 2× |

If sustained metrics fall below 80% of target, the auto-correction loop
must trigger.

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
