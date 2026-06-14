# Post-Mortem — Extended downtime + net-negative window (2026-06-09 → 06-12)

**Author:** ops review · **Window:** 2026-06-09 ~19:00 UTC → 2026-06-12 ~03:40 UTC
**Status of bot at write time:** recovered and farming on the new loss-gated kill
(06-12 03:43, `kill_switch:false`, 5 markets, ~$323 notional, wallet ~$1,122).

> **Update 2026-06-13.** Extended after deeper investigation: three further root causes were
> *proven* against live `compute()`, the data-api, and the portfolio ledger — **RC-3** (price-blind
> EV gate), **RC-4** (sentinel/null `end_date` defeats the resolution filter; the SpaceX IPO
> trades), and **RC-5** (the loss/drawdown safety metric is blind to held-to-resolution losses —
> a −$88 position that evaded every kill). See §5; the locked plan of action (FIX-1→FIX-2→FIX-3)
> is in §11.

> **Update 2026-06-13b (18:20 UTC) — LIVE INCIDENT: FX-095/RC-5's *other* face, a false-kill
> deadlock.** The farmer halted on `oversight:drawdown 16.1% > 15% (portfolio=$1024.57,
> cash=$1024.57)`. **It is a FALSE kill.** Ground-truthed on-chain: at 17:58:46 the bot bought
> $22.17 of "JD Vance signs a U.S.×Iran deal" YES @0.47 and still holds it (data-api `/value`
> $22.41, pnl +$0.24). Cash −$22 simply converted into $22 of inventory, so **true portfolio =
> $1024.57 + $22.41 = $1046.98 ≈ 14.2% drawdown — UNDER the 15% line.** The fill was never written
> to the DB `fills`/`positions` tables (the kill fired ~35 s after the buy → the farmer began
> skipping cycles before recording it), and `simple_oversight.run_once` sources inventory from the
> **DB** (`_load_positions_and_mids`, line 272) → empty positions dict → `compute_portfolio_value`
> returns **cash-only** → false 16.1%. `portfolio_value.py` (FX-095) is correct; it was *starved of
> position data*. **Deadlock:** halted → skips cycles → never records the fill → metric never
> corrects → stays killed; **it will NOT auto-clear.** A farmer **restart** would clear it (startup
> `_sync_exchange_positions`, reward_farmer.py:2647, registers the on-chain orphan → next oversight
> cycle reads $1047 → 14.2% → kill clears) — **but that resumes the RC-2 leak at a real ~14%
> drawdown with the RC-5 safety hole still open, so it is an operator decision, not an unsupervised
> reflex.** Capital is safe halted ($1,047 true). **This upgrades FIX-3 to a both-ways defect
> (under-fires on real held losses AND over-fires/deadlocks on benign fills) → prioritize FIX-3
> ahead of FIX-2.** (NB: the earlier chat read `portfolio=cash=$1024.57` and concluded "real 16%
> erosion" — it did not check on-chain positions and missed the $22 held inventory.)
>
> **Sourcing note.** Every figure below comes from read-only probes run against the
> live Helsinki DB / data-api *during* this investigation (timestamps cited inline).
> A couple of aggregates may have moved slightly since they were pulled; the
> "Refresh" section at the end lists the one probe that re-pulls them. The
> conclusions do not depend on the last dollar.

---

## 1. TL;DR

Two **distinct, compounding** failures produced a window that looked like "lots of
losses, almost no rewards":

1. **A mis-calibrated safety kill switched the bot OFF for most of two days.** The
   fill-rate spike kill fired on *benign* fills (fills we exited flat), it's
   *sticky* (needs a human restart), and the alerts were muted — so each trip ran
   for ~12 hours of **zero farming**. This is the bulk of the "inactivity" and most
   of the reward shortfall: **you cannot earn scoring rewards while halted.**
2. **The allocator kept deploying volatile geopolitical markets that took real
   losses.** The Iran/Hormuz/rials cluster adversely filled us and one market
   ("40 ships Hormuz") crashed 0.73→0.21, producing the bulk of the realized loss.

**Net:** the *strategy* loss was real but bounded (~$100/week, concentrated in a few
markets); the *reward collapse* was mostly downtime, not bad farming. No money-kill
ever fired; drawdown stayed ~5–7%, well inside the 15% limit. The downtime cause is
now **fixed**; the market-selection leak is **still open** and is the real remaining
work.

---

## 2. Timeline (UTC)

| When | Event | Source |
|---|---|---|
| 06-09 ~19:00–06-10 03:36 | **Hormuz "40 ships" crash.** YES price fell ~0.73→0.21 on Iran news. Bot thrashed — frantic buy/sell ~$22 lots at 0.22, accumulated then dumped 381- and 418-share blocks. ~1,119 sh bought / ~1,114 sold. | on-chain TRADE ledger |
| 06-10 ~02:52 | **Fill-rate kill #1** (`fill_rate_ratio=6.0`, Iran-airspace fill cluster). Sticky → bot idle. | farmer journal |
| 06-10 ~14:58 | Manual restart → recovered → farmed ~1 hour. | journal / CYCLE_SUMMARY |
| 06-10 15:52:17 | **Fill-rate kill #2** (`fill_rate_ratio=6.00 > 3.0×`, cancelled 10 orders). Sticky → idle ~12h. | journal (`KILL SWITCH ACTIVATED`) |
| 06-11 ~04:08 | Manual restart → recovered (burst had aged out of the 6h window). | CYCLE_SUMMARY cycle 1 |
| 06-11 13:11:22 | **Fill-rate kill #3** (`fill_rate_ratio=6.00`). Fills were **benign** — 9 fills, all exited flat, total realized **−$1.81**. | journal + unwinds probe |
| 06-11 ~15:30 | `pytest tests/` run **on the prod box** fired false merge/drawdown Telegram alerts and wrote test rows ($1000/$500 wallet) into live `bot_history.db`. Self-healed by 16:28. | reconcile probe |
| 06-11 ~15:56–18:07 | Farming (~5h) on Trump/al-Sharaa/MBS/Russia **news** markets; small adverse losses accumulating (12h realized −$10.27). | unwinds (12h) |
| 06-11 18:07:47 | **Loss-gated kill fired — CORRECTLY.** `fill_rate_ratio=6.0 (short=8, baseline=8) + 1h_loss=$8.53 > $5.65 gate`. The NEW logic killing on *real* loss, not benign fills. Proves the loss-gate was already live by this point (earlier than first thought). | journal |
| 06-11 18:07 → 06-12 03:37 | **~9.5h killed overnight.** A real-loss kill is sticky; it fired while the operator was asleep. Telegram paged at 18:07 but couldn't be actioned until morning. | cycle-state counts: 1180 `kill_switch:true` vs 572 `false` over 12h (~2/3 killed) |
| 06-12 03:37 | Manual restart → farming by 03:43 (cycle 19, `kill_switch:false`, 5 markets, $323 notional). | journal |

**Downtime tally:** Kill #1 (~02:52→14:58 ≈ **12h**) + Kill #2 (~16:00→04:08 ≈ **12h**)
+ Kill #3 + restart churn ≈ **~24+ hours of zero farming** across 06-10/06-11. That is
the "12 hours inactive" you saw (in reality closer to a full day across two episodes).

---

## 3. The reward picture

Authoritative daily reward (data-api `/activity?type=REWARD`+`MAKER_REBATE`), as
pulled 06-11/06-12:

```
06-06: $9.15     06-09: $11.86
06-07: $9.48     06-10: $20.33   (settled 06-10 00:20 for 06-09 farming — a full day)
06-08: $8.58     06-11: $6.58    (settled for 06-10 farming — CRIPPLED by the kills)
```

Read it carefully: the **$6.58** day is the tell. 06-10 was farmed for only a few of
its 24 hours (killed ~02:52→14:58, then ~16:00 on), so the reward it generated
(settled at 06-11 00:20) collapsed to a third of a normal day. **The poor reward was
not the strategy — it was the bot being switched off.** A healthy day on this canary
is ~$9–20.

---

## 4. The loss picture

Realized P&L from `unwinds` (verified against the on-chain ledger and the wallet
reconciler — see §6):

- **7-day realized: −$102.58** (as pulled 06-11).
- **Concentrated in a handful of volatile markets.** The single worst:
  **"40 ships Strait of Hormuz" ≈ −$36** (on-chain cashflow −$36.54 ≈ unwinds −$38.31).
  The rest of the loss clusters on the same theme — USD/rials, other Hormuz/Iran
  airspace variants, a couple of elections.
- **The crucial nuance (per-market net):** the markets that *lose* are largely the
  same volatile markets that *earn*. Hormuz "40 ships" earned only **$0.31/day** but
  lost ~$36 → clearly **net-bad**. "Iran airspace July 15" earned **$2.04/day** and
  lost only ~$0.59 → **net-positive**. So the answer is not "stop trading geopolitics"
  — it's "cut the net-bad ones, keep the net-good ones," which requires per-market net
  data we were not previously persisting.

---

## 5. Root causes

### RC-1 — Fill-rate kill mis-calibrated for a min-size canary (→ the downtime)
The global fill-rate spike kill compared short-window (1h) fill **count** to the 6h
baseline rate and halted at >3×, with a baseline floor of only 5 fills. On a quiet
5-market canary placing `min_size`, the baseline sits right at that floor, so an
ordinary cluster of ~5 fills reads as a **6× spike** → kill. Worse, it keyed purely on
fill **count**, never on whether the fills **lost money** — so it halted on fills we
exited flat (06-11: −$1.81 total). And the kill is **sticky** (a human must restart),
which by design is fine *if* the human is alerted — but the alert channel was muted, so
each trip ran ~12 hours dark. **This produced the inactivity and most of the reward
loss.**

### RC-2 — Market selection over-weights volatile/news markets (→ the real losses)
The allocator ranks roughly `daily_rate × q_share`, which favors high-reward-pool
markets — disproportionately volatile geopolitical/news markets. Those adversely fill
us and occasionally gap hard (Hormuz 0.73→0.21). The 30-minute volatility filter
(`RF_ALLOC_MAX_RECENT_VOLATILITY=0.15`) didn't catch them (`vol_excluded=0`) because
the spike came *after* entry. **This is the genuine strategy leak (~$100/week).**

### Contributing — alerting blindness + sticky-kill response time
The single Discord channel mixed routine fills with critical kills; muting the noise
muted the kills, so a sticky kill sat unnoticed for ~12h instead of ~5 minutes.

### RC-3 — Price-blind EV gate over-rejects good markets (proven 06-13)
The candidate feed (`/rewards/markets/current`) carries **no price**, so every candidate
keeps the `CandidateMarket.midpoint_guess` default of **0.5**. `_est_cost_per_market`
derives cost-to-score from `cost_per_share = max(0.10, min(mid, 1−mid) × 2)`, which is
**maximised at mid=0.5** (→ 1.0). So fill-cost is pinned to the worst case (e.g. **$2.20**
for a `min_size=200` market), and the positive-EV gate (`expected_daily_reward ≥
fill_cost`) rejects any market whose `daily_rate × q_share` can't clear that inflated cost.
A live `compute()` run showed **eligible=1,828–3,425 but positive_ev=5, deploys=5** —
~**99.7% of eligible markets fail EV**. A per-market trace flipped several to positive-EV
once the *real* price was substituted: e.g. *Israel closes airspace by June 30* (real mid
0.12, expR **$1.50** vs real fill-cost **$0.53**) and *Israel×Hezbollah peace deal* (0.131,
**$1.00** vs **$0.58**) — both good, both wrongly rejected. Compounded by the cold-start
q_share (0.005), which FX-046 notes under-predicts 24–94×, depressing EV further. **This is
why the canary deploys only ~5 of thousands of eligible markets — the gate judges every
market on a worst-case price it never measured.** (This is the *over-rejection* face of
RC-2; the volatile-market *over-weighting* is the other face.)

### RC-4 — Sentinel/null `end_date_iso` defeats the 48h resolution filter (proven 06-13)
`_timing_excluded` blocks a market when `end_date_iso` is within
`RF_ALLOC_MIN_HOURS_TO_RESOLUTION` (48h). But for **event-driven markets** (the SpaceX
IPO-day suite — "…above $2T?", "…on IPO day", "…First Day") Polymarket sets `end_date_iso`
to a **far-future sentinel** (`2027-12-31`) or leaves it **null**, even though they resolve
at market close *that same day*. The filter therefore computed **~13,590h to resolution**
(sentinel) or skipped the check entirely (null → fail-open) and let them through. The
farmer's two backstops don't cover this: its `resolution_proximity` block is **price-based**
(mid <0.10/>0.90; these traded 0.22–0.74) and its expiry/game guard is **sports-keyword-only**
("SpaceX IPO" isn't sports). **Result:** on 06-12 the bot farmed the SpaceX IPO-cap markets
hours before resolution and they adversely filled — and **this was the dominant driver of a
true 24h loss of −$72.58** (authoritative: portfolio total_value $1,120.42→$1,047.84,
confirmed to the cent by the data-api net cash flow; gross position loss ≈ −$83 before +$10.38
rewards). ⚠ **The `unwinds`-based `realized_loss_24h` ($13.54, of which the `$2T` dump was
−$6.76) materially undercounts this** — `unwinds` records only active dumps, not the positions
held to resolution and **redeemed at a loss** (the bulk of the SpaceX IPO loss). The `$2T`
market's 13:35–13:47 fill burst is the likely trigger of the ~14:00 fill-rate kill. *Lesson:
`unwinds.pnl` is a subset of realized P&L; the authoritative 24h figure is the portfolio
total_value delta / data-api net cash flow.* **Proof the filter logic is sound:** *SpaceX IPO: Will Elon
Musk Ring the Bell?* carried a *real* `end_date` (2026-06-13) and was traded at **72h out →
correctly allowed**. Same filter, good date, right call — the defect is the input date, not
the logic.

### RC-5 — Loss/drawdown safety metric is blind to held-to-resolution losses (proven 06-13)
The realized-loss kill and the loss-gate on the fill-rate kill both key off `realized_loss_24h`,
computed from the **`unwinds` table — active dumps only.** A position **held to resolution and
redeemed at a loss produces no `unwinds` row**, so it is invisible to those kills. On 06-13 this
was decisive, not academic: a single position — *"SpaceX IPO closing market cap above $2.4T?"* —
was bought for **~$88** (YES @0.22), **held to resolution and redeemed for $0** (resolved
worthless) = a **−$88** loss that was **~the entire −$72.58 24h loss**, yet produced **zero loss
signal.** It evaded every guard:
- **realized-loss kill** — `unwinds`-blind (no dump);
- **drawdown kill** — cash-based (per the FX-095 caveat): the inventory mark-down that took
  *portfolio* value to a **$907 trough** (~−26% from the $1,220 peak, well past the 15% line)
  never showed in *cash* (~$1,077), so it didn't fire;
- **unrealized-loss kill** — $88 ≈ 8% of book, under the 20% threshold.

The fill-rate kill that *did* fire (~14:00) was triggered by a **different, smaller** event — the
`$2T` **dump** burst ($7.64/1h) — not the $88 that actually hurt. The authoritative loss *was*
visible (portfolio `total_value` delta and data-api net cash flow both pegged −$72.58 to the
cent); the safety stack simply wasn't reading those sources. **This is a safety gap, not just a
selection one.** (Confirm the exact kill input bases in code when building FIX-3; the
drawdown-cash basis is asserted per FX-095, not re-verified this session.)

---

## 6. How we know the losses were real (not a tooling artifact)

Mid-investigation we (correctly) challenged whether the loss was even real. It is,
confirmed three independent ways:
- **On-chain TRADE ledger:** Hormuz net cashflow **−$36.54**.
- **`unwinds.pnl`:** −$38.31 for the same market (≈ the on-chain figure).
- **Wallet reconciler:** actual on-chain balance vs recorded cash returns to ~$0
  divergence (mean |div| ~$9 over 7d), so the recorded losses match real cash that
  left the wallet. If `unwinds.pnl` were phantom, the reconciler would show a
  persistent gap. It doesn't.

(Caveat retained: per-row `unwinds.pnl` is imprecise during merge/dump events — the
`hold=0m` artifact — so for *exact* per-market figures the data-api is the gold
standard, but the order of magnitude is solid.)

---

## 7. What was fixed during this window

- **Loss-gated fill-rate kill** (live by 06-11 18:07; suite-verified 1149 pass/0 fail in
  a clean checkout; **production-validated** by a correct real-loss kill at 18:07 —
  `1h_loss=$8.53 > $5.65` — with zero benign false-kills observed since): a fill-rate
  spike now only halts if it coincides with >0.5%-of-wallet realized loss in 1h; benign
  bursts log a warning and keep farming. `MIN_FILL_BASELINE` 5→8. Money backstops
  (10%/24h realized, 20% unrealized) untouched. Reversible via `RF_FILL_RATE_KILL_LOSS_FRAC=0`.
- **Severity-tiered alerting:** critical alerts (kill/crash/stale-heartbeat/merge) →
  **Telegram** + a dedicated Discord channel; routine fills stay on the muted channel.
  Watchdog re-pings every 30 min while killed → worst-case blind window ~30 min, not 12h.
- **Dead-man's-switch:** watchdog pings Healthchecks.io each run; if the *box* halts,
  silence pages you externally.
- **Reboot survival:** all systemd units `enabled`.
- **Per-market reward collection started** (`reward_snapshot.py`, hourly → separate DB)
  — the data foundation needed to solve RC-2.
- **Read-only monitoring/dashboard** (`soak_monitor.py`, dashboard, weekly digest).

## 8. What is still open

- **RC-2 (market selection) is unsolved** — the ~$100/week adverse-fill leak. The plan
  (`LOOP_PLAN.md`): let `reward_snapshot.py` accumulate ~1–2 weeks of per-market reward,
  compute true per-market **net** (reward − loss), then trial **one** knob
  (pre-emptive cooldown on net-bad repeat-losers, or a per-market cap) through the
  staged Wave-4 process, with the soak monitor measuring the effect. Cut the net-bad
  markets (Hormuz "40 ships"), keep the net-good ones (Iran airspace).
- **Test hygiene:** the suite writes to the live DB path and calls real alert functions
  when run on the box (it contaminated prod + paged the operator). Tests should use a
  temp DB and unset creds; never run the full suite on the production box again.
- **Benign-desync watchdog noise:** the reconciler flags ~$1 taker-fee desyncs as
  `desync`, which the watchdog pages on. Worth a small threshold so it only alarms on a
  persistent/growing divergence.
- **Sticky kill + overnight = guaranteed downtime.** Even with perfect alerting, a
  *correct* real-loss kill that fires while the operator is asleep halts until morning
  (the 06-11 18:07 → 06-12 03:37 ~9.5h gap). The cardinal rule says "never auto-clear a
  kill," so the durable answer is to stop *causing* real-loss kills (fix selection),
  not to weaken the kill. A bounded auto-recovery for the canary is a possible future
  discussion but would need careful, single-axis safety design.

---

## 8.1 Last-12 hours — close-up (06-11 15:52 → 06-12 03:52)

The most instructive window, because it shows the loss-gate working *and* the limit of
what it fixes:

- **Uptime: ~1/3 farming, ~2/3 killed** (572 vs 1180 cycle-states).
- **One kill, and it was the right one.** 18:07:47, the NEW loss-gate fired on a *real*
  $8.53/1h loss (Trump-announce dumps −$2.62, −$3.37 back-to-back) — exactly its job. No
  benign false-kills in the window. The recalibration is validated in production.
- **But it cost ~9.5h overnight** because it's sticky and fired while you were asleep.
  So the loss-gate eliminated *false* kills; *real*-loss kills overnight still cost the
  night.
- **What was traded/lost:** −$10.27 realized, entirely on volatile *news* markets
  (Alibaba, Trump↔al-Sharaa, Trump↔MBS, Trump-announce, Russia-Ukraine) — i.e. **RC-2
  again, just different markets.** The geopolitical-news appetite is the through-line.
- **Reward:** 06-10 $6.58 → 06-11 $9.50 (recovering toward normal); 06-12 will be
  suppressed by the overnight kill.

**Conclusion of the close-up:** fixing the *false* kills doesn't fix the *real* ones —
only market selection does. This window is the strongest evidence yet that selection is
the priority, not a side-quest.

---

## 9. Bottom line

This window was **net-negative but not broken**: drawdown ~5–7% (peak $1,213 → ~$1,122),
no money-kill ever fired, and the bot self-protected throughout. The scary headline
("lots of losses, no rewards") decomposes into a **real but bounded** strategy loss
(~$100/week on a few volatile markets) and a **much larger reward loss from downtime**
caused by a mis-calibrated kill — and the downtime cause is now fixed. The remaining
focus is singular: **market selection** (RC-2), which the per-market net data is being
collected to solve properly rather than by guess.

---

## 10. Refresh (optional — re-pull the exact current numbers)

Run the read-only probe in the chat (sections A–E: kill timeline, daily rewards, daily
P&L+activity, worst-loss markets, current drawdown) and I'll slot the freshest figures
into §2–§4. The narrative and conclusions stand regardless.

---

## 11. Fix backlog & plan of action (locked 2026-06-13)

**Operating rule — why we do NOT fix it all at once.** ground_rules P3: single-axis
changes only. P5: a fix is unproven until ≥7 days clean on the live canary. Two changes
live at once means we cannot attribute the result (or a regression). So fixes ship **one
at a time** through the gate below; this document is the ledger, not a worklog.

**Per-candidate pipeline (every fix runs this, in order):**

1. Build the single-axis change on `main`.
2. **Invariant gate (blocking):** `python3 -m simulation.run_audit_v5 --seeds 1 42 1337`
   (INV3/5/7) + fast tests (`pytest tests/ --ignore=tests/test_simulation.py`).
3. **Backtest** on a `sqlite3 .backup` snapshot via `backtest.py --override`. This is a
   *filter* (rejects regressions), **not proof**.
4. If it passes the gate and improves the metric, deploy to the **Wave-4 canary**.
5. **≥7 days clean live = proof.**
6. Operator decides rollout. **Never two candidates live simultaneously.**

**Sequence (approved 2026-06-13): FIX-1 → FIX-2 → FIX-3 → operational.** (FIX-3 is a *safety*
gap and may warrant jumping ahead of FIX-2, since FIX-2 widens deployment — operator's call.)

### FIX-1 (first) — Sentinel/null `end_date` handling  [RC-4]
- **Why first:** defensive (stops trading mis-dated near-resolution markets), small and
  contained, currently causing real losses + kills, and a **prerequisite** — we must not
  widen deployment (FIX-2) while this adverse-selection leak is open.
- **Single axis:** timing-validation only. Treat a missing or implausibly-far
  `end_date_iso` as **suspicious, not safe** (close the null fail-open); where feasible
  enrich the true event/resolution date (events API / question-text date parse) before the
  48h check. No change to sizing, ranking, or the EV gate.
- **Success metric:** zero placements within 48h of *true* resolution on event-driven
  markets across the soak; no drop in eligible-market count for correctly-dated markets.
- **Reversibility:** new behavior behind a config flag defaulting to current behavior.

### FIX-2 (second) — Real-price enrichment for the EV gate  [RC-3]
- **Why second:** bigger lever (could lift deploys from ~5 to many) but bigger blast
  radius; only safe once FIX-1 has closed the near-resolution leak.
- **Single axis:** populate `midpoint_guess` from a real price (the `/rewards/…`
  `tokens[].price` already available, or a cheap book fetch) **before**
  `_est_cost_per_market`. EV-gate logic itself unchanged — it just receives a real cost
  instead of the worst-case 0.5 cost. Keep cold-start q_share conservative so EV doesn't
  over-loosen.
- **Success metric (backtest first):** how many additional *correctly-dated, non-extreme,
  not-cooled* markets would deploy and their modelled net; promote to canary only if net is
  non-negative and the invariant gate passes.
- **Reversibility:** price source behind a flag; fall back to 0.5 default if disabled.

### FIX-3 (queued) — Loss/drawdown kills off authoritative value, not `unwinds`/cash  [RC-5]
- **Why:** the worst loss type (held-to-resolution) is invisible to the current loss metric, so
  the kills can under-fire by ~10× on redemption-heavy days (06-13: $13.54 measured vs −$72.58
  real; the −$88 position fired *no* loss signal). This is a **safety** gap — weigh prioritizing
  it ahead of FIX-2 despite the locked order, since FIX-2 *widens* deployment. **Update 06-13
  18:00 — the same DB-sourced metric also OVER-fired:** a $22 on-chain fill not yet written to the
  DB made the drawdown kill read cash-only and FALSE-trip into a deadlock (true dd 14.2%; see the
  top-of-doc 06-13b update). The defect cuts both ways. **Exact locus:** `simple_oversight.run_once`
  line 272 — `_load_positions_and_mids(db_path)` feeds the kill's inventory from the DB; reconcile
  it against the data-api `/positions` (authoritative) for the kill input, **fail-safe** if the API
  is unavailable (a missing reading must not silently disable *or* falsely fire the kill).
  **Prioritize FIX-3 ahead of FIX-2.**
- **Single axis (pick ONE measurement change):** feed the realized-loss kill + the fill-rate
  loss-gate from an authoritative source (portfolio `total_value` delta, or the data-api net cash
  flow) instead of `unwinds`-only; and/or make the drawdown kill portfolio-based rather than
  cash-only (intersects FX-095).
- **Adversarial cases:** data-api latency/availability must **fail-safe, not fail-open** (a
  missing reading must not silently disable the kill); avoid double-counting realized vs
  unrealized; net out the reward credit so it doesn't mask a loss.
- **Reversibility:** new loss-basis behind a flag defaulting to the current `unwinds`/cash basis.
- **Success metric:** on a replay of 06-13, the measured 24h loss tracks the −$72.58 portfolio
  delta (not $13.54), and the kill logic responds to held-to-resolution losses.

### Operational (not a code change) — chronic-blocked markets
Several markets are parked in `chronic_blocked: manual clear required` (incl. the
Trump-ceasefire market showing ~$87/day expected reward). Review periodically and clear by
hand **only** after confirming they no longer adverse-fill. Deliberately manual, per the
cardinal kill rule.

**Status (2026-06-13):** **FIX-1 built** (default-off `RF_ALLOC_EVENT_DATE_GUARD`; invariant gate
+ unit tests green; commit + 7-day canary trial pending). **FIX-2 and FIX-3 scoped, not started.**
RC-5 logged after the −$72.58 / −$88 attribution. Next action = commit FIX-1, then its canary
trial; decide FIX-3-vs-FIX-2 ordering.
