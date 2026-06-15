# Path to Profitability — Plan of Action

**Created:** 2026-06-15 · **Owner:** Saby · **Status:** proposed, pre-flight not yet run.

> **Prime directive carries through every step: ground truth, not guesswork.** Verify before
> acting; reconcile across sources (bot snapshot / on-chain / data-api); say "unsure" explicitly;
> the live soak is the proof, never the backtest or a predicted number.

---

## 0. The thesis (why this is profitable)

**Net = Reward − Adverse-fill loss.** Both levers move in our favor and are independent:

- **Reward** on healthy *running* days has been **$9–20/day at cap-5** (postmortem §3: 06-08
  $8.58, 06-09 $11.86, 06-10 $20.33). The recent "$5/day" was a *halted-window* artifact, not the
  running rate.
- **Loss** is **concentrated and avoidable**: the 5-day net analysis showed −$213 loss dominated
  by a handful of volatile/event/repeat-fill markets (Hormuz −$48.81 over **8** fills earning
  $0.31; SpaceX-class events; $0-reward adverse-fill markets). The top ~15 markets are ~$150 of it.

So the bot isn't economically broken — its **selection ranks by reward and ignores adverse-fill
risk** (RC-2), so it deploys the highest-reward markets, which are often the worst by net. Fix
*which* markets it picks → losses collapse → the existing reward engine clears profit → then scale
breadth.

**The data chicken-and-egg is broken by sequencing:** we fix selection first with **low-data /
reactive** tools (react to bad fills + avoid bad features), which makes farming safe enough to
*run continuously*, which *generates* the per-market net data that later enables the data-hungry
per-market scoring. Run-safe-first bootstraps the data.

---

## 1. Operating discipline (applies to every change below)

1. **Single-axis** — one knob/behavior at a time; never two live at once (so effects are attributable).
2. **Gated (blocking)** — `python3 -m simulation.run_audit_v5 --seeds 1 42 1337` (INV3/5/7) +
   `pytest tests/test_simple_allocator.py -q` + fast tier, all green before deploy.
3. **Reversible** — every behavior change behind a default-off `cfg()` flag.
4. **Measured by soak, not predicted** — `soak_monitor.py` + the per-market net analysis decide;
   ≥7 days clean live = proof (P5).
5. **Recorded** — any safety-threshold change logged in `ground_rules.md`.
6. **No branding** — no Claude/Anthropic/AI in commits/code/docs. `main` only.

---

## PHASE 0 — Pre-flight verification (NO code; ground-truth the assumptions)

These confirm the levers actually do what we think before we touch anything.

- **0.1 — Verify the pre-emptive cooldown is real and wired.** Read `decision_policy.py`:
  `preemptive_cooldown()` (trigger + what counts as an "adverse fill"), `_cooldown_duration_sec()`
  (how long it cools), `is_cooled_down()`, and **where in `reward_farmer.py` it is actually called**
  on a fill. *Confirm it is not dormant* (Ground Rule 3) and that it cools on the **first** adverse
  fill. **Open question to resolve:** exact cooldown duration + what loss/ξ threshold (if any)
  triggers it.
- **0.2 — Snapshot the relevant knobs (live):** `RF_PREEMPTIVE_COOLDOWN_ENABLED`,
  `RF_ALLOC_MAX_RECENT_VOLATILITY` (=0.15; why did it not catch Hormuz?), `RF_RANK_VOL_PENALTY_K`
  (is ranking already vol-penalized?), `RF_ALLOC_EVENT_DATE_GUARD` (FIX-1 state),
  `RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS` (=5; confirm it's a `config_overrides.json` setting).
- **0.3 — Authoritative per-market net, full window.** Re-run the net analysis using the
  **data-api** per-market loss (not `unwinds`, which undercounts — RC-5), joined with
  `reward_snapshots.earnings_usd`. Output: each loss bucketed by lever it would respond to —
  **repeat-fill** (cooldown), **event** (FIX-1), **volatile-single-shot** (vol cap), **other** —
  and the $ each lever would have saved. *This sets the thresholds in Phase 1.*
- **0.4 — Decide the resume / stale-peak mechanism.** The drawdown kill uses an all-time peak
  ($1,220.52); we're ~16.6% below it, so resuming requires clearing it. Two options to evaluate:
  **(a)** one-time recorded baseline reset to current (~$1,018); **(b)** a small code change making
  the peak *windowed* (e.g. max over trailing N days) so a stale high self-heals and this never
  recurs. (b) is the principled fix; (a) is faster. Pick one — recorded decision.

**Exit criterion:** cooldown mechanics confirmed, thresholds chosen on real net, resume mechanism decided.

### Phase 0 FINDINGS (verified 2026-06-15)

- **0.1 — cooldown is real, wired, reactive, and a single flip to enable.** Trigger
  (`order_lifecycle.py:682,705`): per-fill `slip = clob_cost − mkt`; if `slip >
  RF_PREEMPTIVE_SLIPPAGE_USD` (default **0.05/share**, already non-zero) it calls
  `DecisionPolicy.preemptive_cooldown(cid)` → 24h cooldown (`RF_COOLDOWN_BASE_SEC=86400`;
  escalation to 72h/7d exists, off). **The only off-switch is `RF_PREEMPTIVE_COOLDOWN_ENABLED=False`.**
  So Phase 1.1 is a **single hot-reload config flip, no code, fully reversible.**
- **0.3 — losses bucket overwhelmingly to the cooldown.** 14d (unwinds floor):
  **repeat-fill −$152.97 (66%)**, single-fill −$70.80 (30%), event −$7.67 (understated; unwinds
  misses held-to-resolution losses → data-api refines). **Est. cooldown savings ≈ $113.80
  (upper bound — assumes fill-#1 trip; actual depends on per-fill slippage vs 0.05).** The
  repeat-fill losers are the RC-2 pattern by name (Hormuz ×2, Iran peace, Trump↔al-Sharaa,
  Fujimori, Alibaba).
- **Order confirmed:** cooldown (lead, 66%) → volatility cap (the −$71 single-fill bucket) →
  FIX-1 (event, value understated here). Threshold 0.05/share is the starting value; refine on soak.

---

## PHASE 1 — Make it net-positive at cap-5 (the core selection fix)

Single-axis, in order. Do **not** stack; let each soak before the next.

- **1.0 — Resume prerequisite: clear the stale-peak drawdown** via the Phase-0.4 decision
  (recorded in `ground_rules.md`). Bot must be able to run for any of the below to be measured.
- **1.1 — Enable the pre-emptive cooldown (LEAD FIX).** `RF_PREEMPTIVE_COOLDOWN_ENABLED=true`
  (config flip if 0.1 confirms it's wired; else a small flagged change). This is the highest-leverage,
  **zero-data** fix — it turns repeat-loss markets (Hormuz −$48→~−$6) into one-strike-and-cool.
  Gate → deploy → resume → watch `soak_monitor` for 3–5 days. **Metric:** repeat-fill losses
  (markets with >1 adverse fill) should collapse toward single-fill.
- **1.2 — Enable FIX-1 event guard (if event losses persist).** `RF_ALLOC_EVENT_DATE_GUARD=true`
  (already built, tested, deployed, default-off). Cuts SpaceX-IPO-class same-day event markets.
  Single-axis; only after 1.1 has soaked. **Metric:** event-market entries → ~0.
- **1.3 — Tune the volatility lever (if single-shot volatile losses persist).** Either lower
  `RF_ALLOC_MAX_RECENT_VOLATILITY` or raise `RF_RANK_VOL_PENALTY_K` (net-penalize volatile markets
  in the *ranking*, so they don't win the 5 slots). Pick ONE; value from Phase 0.3.

**Exit criterion:** over a non-crisis stretch at cap-5, daily loss is materially below daily reward.

---

## PHASE 2 — Prove net-positive (the soak)

- Run **1–2 weeks clean at cap-5** with the Phase-1 fixes on, in a **non-crisis window** (the
  5-day −$186 sample spanned the Iran crisis + SpaceX IPO and is not representative).
- **Instruments:** `soak_monitor.py` daily digest; the authoritative per-market net analysis
  (reward_snapshots + data-api) weekly; watch drawdown stays off the kill line on its own.
- **Decision gate (made on the measured number, not a prediction):**
  - **Net-positive** → Phase 3 (scale).
  - **Marginal / break-even** → tune the next single knob (1.3, or sizing), re-soak.
  - **Still deeply negative after a representative window** → genuine strategy reassessment
    (scale of q_share/capital, or market universe) — but decided on real data, not the crisis sample.

---

## PHASE 3 — Scale breadth (raise the cap = scale the reward)

Only after Phase 2 proves net-positive. This is where reward grows (Ground Rule 1: many thin
positions aggregating).

- Raise `RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS` **step-wise: 5 → 15 → 30 → …**, each step its own
  **≥7-day soak**, confirming net stays positive as breadth grows (more markets = more reward *and*
  more adverse-fill surface; the cooldown/filters must hold at scale).
- **Stop / back off** the moment a step degrades net. Capital cap (`RF_MAX_CAPITAL_PER_MARKET_USD`,
  currently 0/disabled) can bound per-market exposure as breadth rises.
- **Reset the peak baseline** at each scale-up so the drawdown kill tracks the new operating level.

---

## PHASE 4 — Data-driven refinement (now that running has produced data)

With weeks of clean per-market net from Phases 2–3 (the data the reactive fix bootstrapped):

- **Net-aware ranking:** replace/augment the raw `expected_daily_reward` ranking with a
  net-adjusted score (reward discounted by observed adverse-fill cost per market/feature), so the
  *top-N selected* are the best by **net**, not by reward. This is the data-hungry fix that was
  impossible at the start and is now feasible.
- **q_share maturity:** track whether per-market reward grows as continuous presence accrues real
  q_share (cold-start 0.005 → measured share); feed it into sizing.
- Each is single-axis + gated + soaked, as above.

---

## Risks & honest unknowns

- **Cooldown may eat one fill per bad market** before cooling — single-shot big losses need the
  feature filters (1.2/1.3), and some residual loss is cost-of-business. We target net, not zero loss.
- **Whether react+filter alone reaches net-positive is unproven** — Phase 2 measures it; do not
  pre-commit to a profit figure.
- **Scaling (Phase 3) can re-introduce loss** if the filters don't hold across more markets — hence
  step-wise soaks, not a jump.
- **The 5-day sample is crisis-skewed** — all conclusions get re-checked on the representative soak.

---

## Immediate next action

Run **Phase 0.1 + 0.3** (read-only): confirm the pre-emptive cooldown is wired + its duration, and
produce the authoritative per-market net bucketed by lever. That output sets the Phase-1 thresholds
and confirms the cooldown is the right lead fix — before any code or config changes.
