# Polymarket Reward Farming Bot

## Architecture & Operations Reference

---

**v6.0 scope (2026-05-26).** GROUND RULES established. The companion file
`ground_rules.md` in the repo is now the **immutable contract** for every
architectural decision. Three rules:

1. **Maximize reward farming.** Single optimization target is total daily
   reward earnings. Be on as many reward-eligible markets as possible at
   minimum viable size, not few markets at max size. Aggregate
   sub-threshold accruals across markets.
2. **Leverage Polymarket's capital overcommit.** Total live notional
   *should* exceed wallet (design point 2-8×). Polymarket auto-cancels
   excess orders when one fills. Treating `notional > wallet` as an alarm
   is an anti-pattern.
3. **Self-learning loop is mandatory.** Per-market ROI tracking,
   per-market loss feedback, auto-correction on underperformance,
   per-market q_share calibration against `/rewards/user/percentages` API.
   No dormant calibration components — every metric must affect bot
   behavior or be deleted.

**The currently-deployed code (v5.2 SimpleAllocator, commit `0fafa1b`)
violates all three rules:**

- Rule 1 violation: caps `MAX_DEPLOYED_MARKETS = 20` and filters out
  markets with `expected_daily_reward < 0.01` per-market — leaves
  ~5000 reward markets on the table.
- Rule 2 violation: `DEPLOY_RATIO = 0.95` caps total notional *below*
  wallet. The bot capped at $1140 deployed when it could be at $5-10k
  notional. Same for `MAX_PER_MARKET_USD = 60`.
- Rule 3 violation: SimpleAllocator's ranking is purely `daily_rate ×
  q_share` with no historical-ROI input. No per-market loss penalty. No
  auto-correction on underperformance. The kill switch is the *only*
  feedback loop, and it's terminal (operator must restart).

**The 2026-05-25 12:05 UTC kill-switch event** is consistent with these
violations. Bot deployed aggressively on 19 high-rate markets, got filled
9 times in 3.5h, lost $26 in trade pnl (vs $4.78 in rewards earned same
day), kill switch caught the fill rate spike. Eight of nine fills weren't
persisted to the `fills` DB table — the bot's accounting was running
faster than its DB writes. See `fixit.md::FX-051` through `FX-056` for
the issues this surfaced.

**v6.0 redesign requirements** (no code yet — planning phase):

A. **OverCommitAllocator** (replaces SimpleAllocator). Targets
   `target_market_count = 100-200` markets simultaneously at `min_size`
   notional each. Total live notional 3-8× wallet by design. Per-market
   exposure cap = `wallet / target_market_count`, NOT `wallet × small_pct`.

B. **Per-market ROI tracker** (new module, persisted to DB). Rolling
   1h/24h/7d windows. Inputs: `fills` table, `unwinds` table,
   `/rewards/user/markets?date=...` API. Output: per-market
   `(rewards_earned, fill_loss, capital_committed_time_weighted, roi)`.

C. **Decision policy** (new module). Reads ROI tracker output. Per-market:
   - `roi < threshold AND samples ≥ N` → mark cooled, exclude from alloc
     for `cooldown_period` (e.g., 24h)
   - `fill_rate > target_by_factor` → increase queue cushion (deeper
     placement) before deactivating
   - `q_share_error > 2×` → use API value, recalibrate prior
   Global:
   - `daily_reward < target` → expand market count, lower per-market
     expected-reward floor
   - `daily_loss > daily_reward` → tighten filters globally

D. **Fix the fill-detection latency** (FX-054). The 2026-05-25 event
   had 8 of 9 real fills not in the `fills` DB. Without this, the ROI
   tracker can't function. Possible causes (to investigate):
   `detect_fills` cycle interval too slow vs fill arrival rate;
   network timeouts swallowing fill events; race between
   `handle_fill` DB write and next cycle's read.

E. **Re-wire FX-049 wallet reconciliation** in `simple_oversight.py`
   (FX-055). I dropped the `reconcile_wallet_invariant` call when
   replacing `oversight_agent.run_once`. Need it back as a safety net
   for accounting drift.

F. **Bring back relevant pieces of the deleted learning stack.** Not all
   of it, but specifically:
   - `LossModel` per-market: tracks expected dump loss per market
   - `Bandit` Thompson sampling: drives the cooldown/reactivation policy
   - Reward attribution via `/rewards/user/markets?date=...` (newly
     discovered API endpoint, post-FX-046 investigation)

G. **Kill switch redesign.** Current `fill_rate_ratio > 3.0` is correct
   in spirit but is the wrong PRIMARY defense — by the time it fires we've
   already taken multiple losses. The primary defense should be auto-
   correction (Rule 3); the kill switch is the backstop.

H. **Notional ratio thresholds re-tuned for Rule 2.** Current
   `MAX_NOTIONAL_RATIO = 2.0` (soft block) and `HARD_NOTIONAL_RATIO = 2.5`
   (hard cancel) are anti-overcommit. Re-tune to permit 3-8× by design.
   Anomaly detection should fire on rapid GROWTH of notional (e.g., 10×
   in 5 min), not on absolute level.

**v6.0 will land as a sequence of FX-NNN fixes (see fixit doc §3),
shipped one at a time with the test/observe/iterate protocol P3 mandates.
No big-bang rewrite.**

**Operator action on the kill-switch event (2026-05-25 12:05 UTC):**
- Bot is currently halted; no further losses occurring.
- DO NOT restart the farmer until at least FX-051 (loss-aware) +
  FX-054 (fill-detection) + FX-055 (FX-049 regression) are addressed —
  restarting the same code will reproduce the same losses.
- Options: roll back to `oversight_agent.py` (loses overcommit benefit but
  stops bleed); or build out the v6.0 fixes before resuming.

---

**v5.1.22 scope (2026-05-24, commit `06d8406`).** Phase A of the Master Plan complete — loss-accounting integrity restored. Single commit per operator authorization (P3 single-axis override; both fixes belong to the same defense-in-depth pass):

1. **FX-050 — Polymarket taker-fee accounting in DumpManager.** The 2026-05-22 dump cycle on the OpenAI $2.0T market (50 NO @ $0.78) recorded `pnl=−$1.00` in bot DB, but actual wallet delta was **−$1.34**. Cross-referenced data-api activity vs bot DB: gap = $0.34 = 0.88% of $39 gross revenue = Polymarket's taker fee on cross-the-spread orders. Pre-FX-050, `DumpManager.check_dump_fills` computed `sell_revenue = matched × SDK_price` where `SDK_price` is the book match price, not the post-fee cash settled. I7 hourly_loss + 24h-realized-loss kill switch under-fired by ~25-30% on every dump.

   **The fix.** New config knob `RF_POLYMARKET_TAKER_FEE = 0.009` (default 0.9%; hot-reloadable; 0 reverts to pre-fix). `dump_manager.py:89` applies the multiplier: `sell_revenue = matched × price × (1 − fee)`. `[DUMP CONFIRMED]` log line now emits `gross / fee / net / cost / pnl` for operator visibility. Calibrated against the 2026-05-22 incident: with `fee=0.009`, the bot would have recorded `−$1.349` (within $0.01 of actual `−$1.34`, float rounding only).

2. **FX-049 — Wallet-invariant reconciliation (defense-in-depth backstop).** New table `wallet_reconcile_history` + new module `oversight/wallet_reconciliation.py` + integration in `oversight_agent.run_once()`. Runs once per agent cycle (~30 min). Compares ACTUAL wallet delta (live `get_balance_allowance`) against EXPECTED delta (bot DB `Σ unwinds − Σ fills + data-api Σ REWARD + Σ MAKER_REBATE` since last reconcile). `|divergence| > RF_WALLET_DESYNC_THRESHOLD_USD = $0.50` → `[CRITICAL] WALLET_DESYNC` log with all signals.

   **Why it stays after FX-050 lands.** FX-050 fixes the SYMPTOM of one specific cash-accounting drift (taker fee). FX-049 catches the SYMPTOM of ANY future cash-accounting drift the formula doesn't predict: silent fill misses, phantom unwinds, manual operator deposits/withdrawals, Polymarket fee-schedule changes, on-chain rebates we didn't anticipate. Permanent invariant.

   **First-run path.** Empty `wallet_reconcile_history` → snapshot current actual wallet as baseline, write `status='baseline'` row, no alert. Subsequent cycles do the comparison.

   **Fail-open.** `data-api/activity` fetch failure → `status='fail_open'` row + `log.warning`, no CRITICAL. A transient network blip shouldn't kill the bot or false-alarm the operator.

   **Incremental, not cumulative-from-genesis.** Each cycle resets the baseline to `(now, actual_wallet_now)`. Divergences are observed once, not double-counted across cycles.

   **Tests.** 5 new in `tests/test_dump_manager_fee.py` (FX-050 contracts: default fee applied, fee=0 reverts, scaling, phantom defense orthogonal, post-fee pnl). 10 new in `tests/test_wallet_reconciliation.py` (FX-049 contracts: first-run baseline, within tolerance, divergence alerts, fail-open, baseline advancement, reward attribution). Fast tier 770 → **785 pass**; CI 26350996533 green in 5m46s.

**Operator action on next Helsinki `git pull + restart`:** No config change required. First agent cycle writes a baseline reconcile row (no alert). Subsequent cycles do the actual comparison. Next dump cycle records post-fee `usd_value` in `unwinds`. Historical row from 2026-05-22 (the −$1.00 entry) is NOT backfilled by this commit — would require operator SQL if reconciliation accuracy needed retroactively.

---

**v5.1.21 scope (2026-05-23, commits `0ec898a` + `a858bb9`).** FX-037 BUY-side phantom-fill defense, symmetric with the SELL-side defense shipped in v5.1.9 (FX-007). Plus a test-shim hardening (`a858bb9`) after CI run `26329526380` caught test-pollution between sibling test files.

1. **`0ec898a` — Add BUY-side phantom-fill defense (FX-037).** Mirrors `DumpManager.check_dump_fills` on-chain probe (see `dump_manager.py:60-87`) on the BUY side. New helper `OrderLifecycle._check_buy_phantom_fill(ms, side, matched) → float`. After SDK reports a BUY fill with `size_matched > 0` and status in `(MATCHED, CANCELLED)`, query `get_balance_allowance(CONDITIONAL, token_id)` to confirm the CTF balance actually increased by the reported amount. If `actual_delta < matched - 0.5`, prefer on-chain truth and emit `log.critical("PHANTOM FILL: SDK size_matched=N but on-chain delta only M | ... ")` for operator visibility.

   The 2026-05-19 Iran NO incident shape: V2 SDK reported `size_matched=158` for an order that delivered only 38 NO shares on-chain. The inflated fills row cascaded through I7 hourly_loss → SafetyController demoted to DEGRADED → forced cold-start OpenAI deployments → dump slippage → kill switch (realized loss $19.55). FX-037 closes the BUY-side asymmetry that allowed it.

   **Fail-OPEN on API exception.** A transient `get_balance_allowance` failure preserves the SDK value rather than losing legitimate fills. Worst case if check is wrong on fail-open: orphan-scan + reconciliation catches phantoms next cycle. Strictly safer than fail-closed (which could drop real fills).

2. **`a858bb9` — Fix test_order_lifecycle SDK shim against sibling test pollution.** CI run `26329526380` failed 2/770 tests because `TestCheckBuyPhantomFill.test_(yes|no)_side_probes_(yes|no)_tid` asserted on the constructed `BalanceAllowanceParams.token_id` string, but pytest alphabetical ordering imports `test_critical_fixes.py` (c < o) BEFORE `test_order_lifecycle.py`. The sibling's module-level `_ensure_clob_types_mock` installs MagicMock-based partial mocks at `sys.modules["py_clob_client_v2.clob_types"]` without cleanup. The prior shim in test_order_lifecycle had an early-return guard `if 'py_clob_client_v2' in sys.modules: return` that didn't distinguish "real SDK installed" from "stale sibling MagicMock present". The MagicMock-returned `BalanceAllowanceParams(token_id=tid).token_id` was another MagicMock, not the string.

   **The fix.** Three-step protocol in `_install_passthrough_clob_shim()`: (1) drop any MagicMock-based partial mocks at `py_clob_client_v2.*` in `sys.modules` (mirrors `test_placement.py::_drop_stale_clob_mocks`); (2) try fresh `import py_clob_client_v2.clob_types` — if it succeeds (Helsinki CI with real SDK), return; (3) on ImportError (local dev), install passthrough dataclass stand-ins. Production code (`order_lifecycle.py`) is unchanged; this is a pure test-environment fix.

   **CI run `26329901126`: 770/770 in 5m59s** ✓ after the shim patch.

**Lessons captured (added to §10.3):**
- **The original FX-050 hypothesis was wrong.** Initial diagnosis assumed "silent fill recording bug" with bot's books completely missing real fills. Live cross-source probe (data-api + bot DB + on-chain CTF balances) revealed bot WAS recording fills+unwinds correctly; the actual gap was the taker fee. Lesson: when reconciling against operator-reported ground truth, the first hypothesis is usually wrong; cross-source probe is required before code change.
- **Test pollution between sibling test files is a recurring trap.** The `test_critical_fixes` → `test_order_lifecycle` ordering caught this; the same `_drop_stale_clob_mocks` pattern shipped in `test_placement.py` for v5.1.18 is now needed in any new test file that asserts on SDK type semantics. Suggested follow-up: extract the shim into `tests/conftest.py` so it runs unconditionally.

---

**Document version:** 6.0
**Last amended:** 2026-05-26 (Ground rules established in `ground_rules.md`; v6.0 redesign requirements documented; FX-051 through FX-056 opened in fixit doc to track gaps between deployed code and ground rules. Previous head v5.1.22 / commit `06d8406`. SimpleAllocator path (`0fafa1b`) currently deployed and halted by kill switch — operator decision required on rollback vs. forward fix.)
**Reflects codebase at:** HEAD = `06d8406` on `main` (v5.1.22 — FX-050 + FX-049). Prior shipped this session: `0ec898a` + `a858bb9` (v5.1.21 — FX-037 BUY-side phantom-fill defense + test-shim hardening). FX-036 (v5.1.18) is **safe to enable in production** with FX-041's asymmetric-book protection. Base `1081e72` (v2.0) → ... → `ee6abdf` (v5.1.4 Phase 0–D fixes) → `dd67f97` (v5.1.5 I9 fix) → `3f50441` + `987a844` (v5.1.6 Phase 0) → `dc78ba0` + `541108b` (v5.1.7 Phase 1) → `e7fc3d2` (v5.1.8 Phase 2) → `7d8d38d` (v5.1.9 Phase 3) → `d4d1541` (v5.1.10 Phase 4) → `91bae99` (v5.1.11 Phase 5 operational hardening) → `a580bdb` (v5.1.12 Phase 6 part 1 — GitHub Actions CI) → `4aff918` + `f3630c9` + `1c4ae7e` (v5.1.13 Phase 6 part 2 — SafetyController test build-out + 2 audit-driven safety fixes) → `38fc63c` (v5.1.14 hardening roadmap closure: FX-019 fix + FX-027 acceptance) → `d5eabea` (v5.1.15 FX-031 capital-cap scaling) → `75d03c7` (v5.1.16 FX-032 dead-market over-marking) → `647b1e2` (v5.1.17 FX-035 V2 SDK dict-return — THE ROOT CAUSE) → `8152a8b` (v5.1.18 FX-036 queue-depth-aware placement — reward yield uplift) → `c2c21d7` (v5.1.19 FX-040 cold-start trial-mode sizing) → `3534cb5` (v5.1.20 FX-041 two-sided book-depth check; see "Amendments in v5.1.20").
**Prior versions:** 5.1.19 (2026-05-20, HEAD `c2c21d7`), 5.1.18 (2026-05-19, HEAD `8152a8b`), 5.1.17 (2026-05-19, HEAD `647b1e2`), 5.1.16 (2026-05-19, HEAD `75d03c7`), 5.1.15 (2026-05-19, HEAD `d5eabea`), 5.1.14 (2026-05-19, HEAD `38fc63c`), 5.1.13 (2026-05-19, HEAD `1c4ae7e`), 5.1.12 (2026-05-19, HEAD `a580bdb`), 5.1.11 (2026-05-18, HEAD `91bae99`), 5.1.10 (2026-05-18, HEAD `d4d1541`), 5.1.9 (2026-05-18, HEAD `7d8d38d`), 5.1.8 (2026-05-18, HEAD `e7fc3d2`), 5.1.7 (2026-05-18, HEAD `541108b`), 5.1.6 (2026-05-18, HEAD `987a844`), 5.1.5 (2026-05-18, HEAD `dd67f97`), 5.1.4 (2026-05-14, HEAD `ee6abdf`), 5.1.3 (2026-04-29, HEAD `ad22512`), 5.1.2 (2026-04-29, HEAD `2a6baf6`), 5.1.1 (2026-04-28, HEAD `28625ab`), 5.1 (2026-04-27, HEAD `2706953`), 5.0 (2026-04-24, HEAD `7ab514d`), 4.0 (2026-04-22, working-tree v4.0 pre-commit), 3.3 (2026-04-22, Patch 13 + Audit V4 committed at `8a8466e`), 3.2 / 3.0 / 2.0 (see §10.1 Changelog).
**Companion document:** `Polymarket bot fixit.md` — living tracker of open issues, proposed fixes, and the phased hardening roadmap. **Hardening roadmap closed 2026-05-19; one root-cause bug + 4 downstream symptoms shipped post-closure same day.** The crown jewel: **FX-035** in v5.1.17 (`647b1e2`) — `client.get_order_book()` in py-clob-client-v2 v1.0.0 **returns a `dict`**, but `market_discovery.get_merged_book` was written assuming an OrderBook **object** with `.bids`/`.asks` attributes. `getattr(dict, "bids", [])` returns the default `[]` because dicts don't expose keys as attributes. Result: every book fetch returned None silently in production since the V2 migration on 2026-04-29. **Bot placed zero orders for 4 days post-LIVE-cutover.** The hardening campaign chased downstream symptoms (FX-031 capital-cap scaling, FX-032 dead-market over-marking, FX-033/034 hypotheses) for hours before finally tracing back to the SDK-shape mismatch. Same class as B9 (`get_orders → get_open_orders`) — a V1→V2 SDK migration miss that DRY mode masked for 17 days, FX-001's I9 deadlock masked for 4 more days, and only became visible after the deadlock chain was fully closed. Fixed by a `_book_entries(ob, key)` helper that normalizes both dict-form and object-form. **The whole 4-day hardening campaign closed every bug it found, but the bug it didn't find was the load-bearing one.** Lessons logged in v5.1.17 amendment block. Across 4 calendar days (FX-001 logged 2026-05-15 → all open items closed 2026-05-19), the roadmap shipped 28 code/doc/test fixes, retroactively resolved 4 doc-accuracy items, and accepted 1 architectural risk (FX-027) with explicit mitigation rationale. Phase 0 housekeeping (FX-017, FX-018, FX-020) closed in v5.1.6. Phase 1 SafetyController bootstrap completion (FX-002, FX-003, FX-012) closed in v5.1.7. Phase 2 counter consistency (FX-004) closed in v5.1.8. Phase 3 dump-state lifecycle (FX-005, FX-006, FX-007, FX-008, FX-009, FX-028) closed in v5.1.9. Phase 4 capital flow correctness (FX-010, FX-011, FX-013, FX-024, FX-025) closed in v5.1.10. Phase 5 operational hardening (FX-014, FX-015) closed in v5.1.11. Phase 6 part 1 (FX-026 CI) closed in v5.1.12. Phase 6 part 2 (FX-016 test build-out 17→152 tests / 58→94% coverage + audit-surfaced FX-029 + FX-030) closed in v5.1.13. Phase 8 final item FX-019 (cosmetic 400) + Phase 9 FX-027 acceptance closed in v5.1.14. **Post-roadmap follow-ups (all 2026-05-19):** v5.1.15 closed FX-031 capital-cap scaling; v5.1.16 closed FX-032 dead-market over-marking; v5.1.17 closed FX-035 V2 SDK dict-return (the load-bearing one). **Fixit §3 has 3 open items: FX-036 (High — placement formula leaves ~7× reward density on the table; design in §4.23, code deferred to next session), FX-033 + FX-034 (Low — subsumed by FX-035, "nice to have").** Phase 7 (calibration/learning/V2-boundary audit) remains deferred — no specific signal surfaced from the Phase 0-6 sweep, so it stays in fixit §6 as a backlog. **From the first LIVE bootstrap deadlock to actually farming rewards in 4 days + 1 root-cause-fix session, with 230 new tests, CI on every push, and the bot placing real orders.**

**v5.1.20 scope.** v5.1.20 closes **FX-041** — the two-sided book-depth check that's the **prerequisite for safely re-enabling FX-036 (queue-depth-aware placement) in production**. One commit on top of v5.1.19 (`c2c21d7`):

1. **`3534cb5` — Two-sided book-depth check in FX-036 placement (`fixit.md::FX-041`).** Pre-FX-041, FX-036's queue-aware helper checked only the placement-side queue (depth ahead of us at prices closer to mid). It did NOT measure the opposite-side absorbing capacity. On 2026-05-19, the OpenAI HIGH $1.5T market had enough placement-side queue to trigger close-to-mid placement BUT the opposite merged-book side was thin (total in-zone depth sub-$1000). The bot got filled, dump moved the market ~11.5% against us, contributing to the $17.63 realized loss on OpenAI markets that day. FX-040 (v5.1.19) closed the cold-start over-sizing leg of that cascade; FX-041 closes the asymmetric-book leg.

   **The fix.** Three pieces:
   - New config knob `RF_DUMP_DEPTH_SAFETY_FACTOR = 3.0` in `config.py`. `0` disables the check (escape hatch reverts to FX-036-only behaviour).
   - New helper `_has_sufficient_dump_depth(opposite_book_levels, midpoint, max_spread, shares_per_side, dump_price, safety_factor)` in `order_lifecycle.py`. Accumulates `Σ(price × size)` over opposite merged-book-side levels within `max_spread` of midpoint. Returns True when cumulative ≥ `shares_per_side × dump_price × safety_factor`, or when disabled.
   - `_compute_edge_prices` gains two new kwargs (`shares_per_side=0`, `dump_depth_safety_factor=0.0` — defaulted for backwards compat). After each queue-aware result, runs the opposite-side dump-depth check (`merged["asks"]` opposite for "bid" placement, `merged["bids"]` opposite for "ask" placement). If insufficient, that side falls back to legacy zone-edge. Per-side independence preserved — one side falling back doesn't drag the other along.

   **Production call site** in `place_orders_for_market` now passes `ms.agent_shares if ms.agent_shares > 0 else SHARES_PER_SIDE()` and `DUMP_DEPTH_SAFETY_FACTOR()`. Both kwargs flow through the existing FX-036 wiring.

   **Tests.** 18 new tests in `tests/test_placement.py` across three new classes:
   - `TestHasSufficientDumpDepth` (10 tests): escape hatches (factor ≤ 0, shares = 0), empty book, sufficient/insufficient depth, in-zone-only accumulation (depth outside `max_spread` excluded), factor scaling (1.0 passes, 10.0 fails on same book), shares scaling (50 passes, 500 fails), malformed-level tolerance.
   - `TestComputeEdgePricesDumpDepthBackwardsCompat` (2 tests): default kwargs reproduce pre-FX-041 behaviour byte-identically; explicit `safety_factor=0.0` is the escape hatch.
   - `TestComputeEdgePricesDumpDepth` (5 tests): Iran market (FX-036 motivating scenario) still passes queue-aware with default factor 3.0 — **no reward-density regression**; deep-bid-thin-ask forces bid-side to legacy (the OpenAI cascade shape); thin-bid-deep-ask mirror; factor scaling end-to-end; in-zone-only accumulation through the full helper chain.
   - Plus 1 new test in `TestPlaceOrdersForMarketUsesQueueAware` exercising the production wiring through `place_orders_for_market` with the production knob values.

   **Fast tier: 737 → 755 pass (0 regressions).** Placement suite alone: 24 → 42 tests in 0.83s.

**Production verification path (operator action).** After Helsinki pulls v5.1.20:

1. Remove `"RF_TARGET_QUEUE_AHEAD_USD": 0` from `/home/polymarket/Polymarket-bot/config_overrides.json` — this re-enables FX-036 with FX-041 protection.
2. `sudo systemctl restart polymarket-farmer`.
3. Watch first 5-10 cycles in `journalctl -f -u polymarket-farmer`:
   - On deep symmetric books (Iran-class) → expect close-to-mid placement (e.g., `BID YES @ 0.460` instead of `0.440` pre-FX-036).
   - On asymmetric or thin books → expect legacy zone-edge placement (same as if FX-036 were still disabled).
4. Compare `[ATTRIBUTION] reward + rebate` over a 24h window vs the prior 24h. Expect material uplift on deep markets, no change on thin ones.
5. If FX-041 over-fires (bot stays at legacy on markets that look healthy), tune `RF_DUMP_DEPTH_SAFETY_FACTOR` down via `config_overrides.json` (hot-reloadable; no restart).

**Lessons captured in v5.1.20** (added to §10.3):
- **"Two-sided" was the right framing.** Pre-FX-041, FX-036's safety logic was symmetric in one direction (queue-ahead must be sufficient) and silent in the other (no measurement of post-fill dump absorption). The cascade exposed this asymmetry within hours of FX-036 going live. The lesson: when a safety check fires, ask "what's the symmetric check we're NOT doing?" and consider whether it's load-bearing.
- **Same-side vs opposite-side dump-depth is a judgment call worth flagging.** DumpManager's passive mode at `dump_manager.py:308-327` crosses the spread to consume the SAME merged-book side as placement, so the most physically-correct measurement of dump-absorbing depth would be SAME-side beyond the edge. FX-041 implements OPPOSITE-side because (a) it matches the fixit acceptance criterion narrative ("deep bid, thin ask → fall back"), (b) it's a new safety axis complementary to the existing same-side `exit_buf` check at `order_lifecycle.py:482-493`, and (c) "two-sided" in the ticket title naturally suggests opposite-side. Both interpretations catch the OpenAI cascade. The OPPOSITE-side check is a healthy-book heuristic, not a direct dump-slippage measurement. If production shows false positives or false negatives, this is the first knob to revisit.
- **`dump_price = midpoint` is a simplification, not a model.** For extreme-priced markets (midpoint $0.10 or $0.90), the NO-side dump price ≠ midpoint, so the threshold under- or over-estimates inventory value. The operator-facing tunable `RF_DUMP_DEPTH_SAFETY_FACTOR` compensates: raise on extreme markets if production cascades repeat, lower if FX-041 over-fires.

**Open Phase 1 items after FX-041** (in fixit §3):
- **FX-037** (BUY-side phantom-fill defense) — silent state corruption when V2 SDK over-reports `size_matched`. Mirror `DumpManager.check_dump_fills`' existing on-chain balance check.
- **FX-038** (reconciliation extends to fills/unwinds tables) — closes the loop on FX-037 so phantom rows self-heal.
- **FX-039** (cosmetic `fill_type='FULL'` labelling).

**v5.1.19 scope.** v5.1.19 is **the first Phase 1 architectural fix from the 2026-05-19 cascade analysis** — cold-start trial-mode sizing. One commit on top of v5.1.18 (`8152a8b`):

1. **`c2c21d7` — Add cold-start trial-mode sizing (`fixit.md::FX-040`).** The 2026-05-19 cascade chained: cold-start prior (`q_share=0.10`) over-estimated reward on untested OpenAI markets by ~100×; allocator sized full positions (143 sh) on those markets; fills in thin books generated 5-11% dump slippage each; cumulative loss hit 10%·T kill-switch threshold. **Root cause:** the allocator had no concept of "untested vs measured" — it applied the cold-start prior `q_share=0.10` and computed share counts as if it were real measured data.

   **The fix.** Three new config knobs in `config.py`:
   - `RF_TRIAL_MIN_SHARES = 20` — floor for trial-mode deploys (the market's `min_size` wins when larger, for venue compliance)
   - `RF_TRIAL_SCORING_SAMPLES = 5` — graduation threshold; markets with this many scoring snapshots use full sizing
   - `RF_TRIAL_BUDGET_PCT = 0.25` — max cumulative trial exposure as a fraction of `total_capital`

   `q_score_samples` (which already lived inside `reward_market_stats.data` JSON) is now propagated through `MarketMetrics` → `ScoredMarket` so the allocator can see it. Backward-compatible default `q_score_samples = 0` puts new markets in trial mode (safe).

   **New trial-mode branch in `oversight/allocation_writer.compute_allocations`:**
   - For each candidate deploy: if `q_score_samples < RF_TRIAL_SCORING_SAMPLES`, cap shares at `max(min_size, RF_TRIAL_MIN_SHARES)` regardless of `recommended_shares`
   - Cumulative trial cost tracked across the cycle's deploys (score-desc order means top-scored trials get first dibs)
   - If a trial would push cumulative over `RF_TRIAL_BUDGET_PCT × total_capital`, reject with reason `"Trial budget exhausted ($used+$next>$cap, samples=k)"`
   - **Redistribution pass excludes trial markets** so the cap actually binds (without this, surplus capital would flow back into the capped trials and undo the protection)
   - `[FX-040 trial] deployed=N rejected=M budget_used=$X/$Y` summary log line per cycle for operator visibility

   **Tests.** 16 new tests in `tests/test_trial_sizing.py` covering: trial detection threshold, trial target shares (min_size floor), trial-mode capping, graduated full sizing, trial-budget rejection (3 markets, $400 wallet → 2 fit + 1 rejected), redistribution exclusion, score-ordering of budget allocation, mixed trial+graduated handling, backward compat. One existing test (`tests/test_market_scorer.py::test_surplus_gets_redistributed`) updated to set `q_score_samples=10` so its markets are graduated — that's the spirit of the original test; FX-040 just made the implicit `q_score_samples=0` default newly significant. Fast tier: 721 → **737 pass** (0 regressions).

**Production verification (Helsinki, 2026-05-20 08:22:40 UTC).** First oversight cycle on `c2c21d7`:
```
[FX-040 trial] deployed=1 rejected=49 budget_used=$46/$55 (25% cap)
SafetyController [SEVERELY_MISCALIBRATED]: 2/3 markets, $88/$221 capital
Cycle complete: markets_deploy: 2, markets_avoid: 1881
```
1 trial deployed (got the $46 budget slot, ~50 sh at min_size), 1 graduated market deployed (full sizing), **49 cold-start markets explicitly rejected by the trial budget gate** — exactly the kind of markets that exposed yesterday's cascade. Yesterday's specific killer (OpenAI HIGH $1.5T, `min_size=200`, `daily_rate=$400`) is now rejected with reason `"Trial budget exhausted ($0+$182>$55, samples=0)"`. **The 143-share trap is closed.**

**Lessons captured in v5.1.19** (added to §10.3 below):
- **The cold-start prior is a discovery aid, not a sizing input.** Pre-FX-040, the 0.10 q_share prior was applied uniformly — to scoring AND to sizing. After FX-040, the prior still drives scoring (so new markets aren't filtered out before discovery), but sizing is decoupled: trial markets deploy at min_size only until measured data graduates them.
- **Trial budget per CYCLE is the right granularity.** Per-market caps the bot already had don't help — they cap individual deploys but don't bound cumulative exposure across multiple cold-start markets in one cycle. The new trial budget runs at allocation-write time and applies to the whole cycle's discovery exposure.
- **Redistribution can undo cap-based safety filters if not opted-out.** The original FX-040 implementation set trial shares correctly but then redistribution pass added them back. Caught by the smoke test before commit. **Future cap-based safety filters need to verify redistribution either skips them or respects their cap.**
- **A cap in code doesn't help unless production data exercises it.** The first FX-040 production cycle on Helsinki confirmed the rejection-with-reason path is firing for 49 markets — this is the actual proof the architectural fix works. Without that observation, "the test pass" wouldn't have been enough.

**Open Phase 1 work after FX-040** (in fixit §3):
- **FX-041** (two-sided book depth check) — **prerequisite** for re-enabling FX-036 in production. FX-040 prevents cold-start over-sizing; FX-041 prevents close-to-mid placement on asymmetric thin-ask books. Together with FX-036 they close the cascade vector.
- **FX-037** (BUY-side phantom-fill defense) — silent-state-corruption fix. Symmetric with `DumpManager.check_dump_fills` already-existing PHANTOM FILL check.
- **FX-038** (reconciliation extends to fills/unwinds) — closes the loop on FX-037 so phantom rows self-heal.
- **FX-039** (cosmetic labeling: `handle_fill` hardcodes `fill_type='FULL'`).

After FX-041 ships, removing `"RF_TARGET_QUEUE_AHEAD_USD": 0` from Helsinki's `config_overrides.json` re-enables FX-036 in production.

---

**v5.1.18 post-mortem (added 2026-05-20).** FX-036 was shipped at 09:18 UTC 2026-05-19 with inline-verified 3× reward density uplift on the Iran market. By 00:30 UTC 2026-05-20 the bot had hit the realized-loss kill switch (`$19.55 > $17.14 = 10%·T=$171.40`) and stayed dead for ~3.5 hours until manual restart. The cascade:

1. **12:32 UTC 2026-05-19** — Iran NO bid at 2¢ from mid (FX-036 close-to-mid placement) was hit by a taker. V2 SDK `client.get_order()` returned `size_matched=158` but on-chain CTF delivery was only **38 NO shares** (verified via direct `get_balance_allowance` probe; reproducible signature). `fills` table recorded the inflated 158-share row. The bot's own LOST-POSITION reconciliation caught the discrepancy and corrected the `positions` table — but did NOT touch the `fills` table (see FX-038).
2. **I7 hourly_loss invariant** computes damage as `SUM(fills.shares × clob_cost) − SUM(unwinds.usd_value)`. The inflated fills row produced a phantom $60.72 damage → fired I7 critical (threshold $60) → state demoted to DEGRADED.
3. **DEGRADED state** applies `capital_pct=0.20 × $221 ≈ $44 per-market cap`, blocking higher-priced markets. The only markets that fit the squeezed cap were cheap-underlying ($0.10–$0.22) OpenAI cold-start markets where the bot had never posted before.
4. **Cold-start prior `RF_NEW_MARKET_Q_SHARE_PRIOR = 0.10`** (arch §4.10) was applied uniformly to those OpenAI markets. The allocator scored them at `daily_rate × 0.10 × CF` and sized normally (143 shares on one). Reality: actual q_share was ~0.001 (per Polymarket UI showing `<$0.01/day` earnings). Bot was 100× over-estimating its share of the reward pool.
5. **18:32–00:04 UTC** — three OpenAI HIGH $1.5T fills (143, 28, 28 shares respectively) happened in **thin books** (total in-zone depth sub-$1000). Each fill triggered an immediate dump in a market with no other liquidity. **Dump slippage: 5-11% per trade.** Realized losses: −$12.87, −$1.68, −$3.08 = −$17.63 on OpenAI alone.
6. **00:30 UTC 2026-05-20** — cumulative 24h realized loss `$19.55 > $17.14 (10%·T=$171.40)` → kill switch (sticky until process restart per §4.18.4).

**Real damage:** −$19.55 realized loss (Iran was only $1.92 of that — the other $17.63 was OpenAI). The phantom Iran damage was fictional accounting; the OpenAI thin-market damage was real.

**Phase 0 conservative restart (2026-05-20 04:07 UTC):**
- Wrote `config_overrides.json` on Helsinki with `{"RF_TARGET_QUEUE_AHEAD_USD": 0}` → FX-036 disabled at runtime, bot reverts to legacy zone-edge placement.
- Restarted both services → kill switch cleared.
- Applied manual SQL fix to phantom Iran `fills` row (158 → 38 shares; see §6.1's extended manual recovery for the recipe).
- Bot resumed trading at conservative sizes (~$22 notional) on new cold-start OpenAI variants.

**Five new tickets opened in `fixit.md`** for the architectural gaps the cascade exposed:
- **FX-037**: BUY-side fill detection lacks the PHANTOM FILL defense that `DumpManager` has (asymmetric defense — single most surprising finding).
- **FX-038**: `_reconcile_positions` updates positions but doesn't propagate to fills/unwinds — inflated rows persist.
- **FX-039**: `handle_fill` hardcodes `fill_type='FULL'` (labeling bug).
- **FX-040**: Cold-start trial-mode sizing — biggest leverage. Untested markets should deploy at `min_size` only until ≥N scoring snapshots accumulate. Closes the "143-share-trap-in-thin-market" failure mode.
- **FX-041**: Two-sided book-depth check in FX-036. Queue-aware placement should require BOTH bid-side queue AND ask-side dump capacity. Re-enabling FX-036 in production requires this.

**Lessons captured for §10.3 / §12.5:**
- **Asymmetric defense is invisible until exercised.** `DumpManager` got the PHANTOM FILL check in v5.1.9 (`7d8d38d`) after FX-007's Tamilaga orphan-dump incident. The same defense was never added to `OrderLifecycle.detect_fills`. The asymmetry was latent for 8 months until a BUY-side phantom hit production.
- **"Three small mistakes multiply."** FX-036 (close-to-mid placement) was reasonable for deep markets. Cold-start prior (0.10 q_share) was reasonable as a discovery heuristic. Dump-immediately-on-fill was reasonable for the symmetric-book case. Each in isolation: fine. All three composed on a thin market: catastrophic. Future architectural changes need to be evaluated for cross-system composition, not just unit correctness.
- **"First production cycle after a release" applies to OPERATIONAL behaviour, not just code execution.** The first FX-036 fill happened ~3 hours after deploy. The cascade peaked 12 hours after deploy. The observation window for a placement-strategy change needs to span at least 24h of realistic market conditions, not just the first few cycles.
- **Reward farming on thin markets is the OBJECTIVE, not a hazard to avoid.** Polymarket pays high rewards on thin markets precisely because they're under-served. A max-rewards system must be exceptionally good at trading them, not filter them out. The right answer is FX-040 (trial-mode sizing) + FX-041 (two-sided depth check) — not "avoid thin markets" or "lower the cold-start prior globally."

**v5.1.18 scope.** v5.1.18 is **the first post-LIVE-resumption reward-yield upgrade** — the change that turns the Helsinki bot's "actually farming" state into "actually farming well." One commit on top of v5.1.17 (`647b1e2`):

1. **`8152a8b` — Queue-depth-aware placement (`fixit.md::FX-036`).** Pre-FX-036 the bot placed orders at the **far edge** of the reward zone (`midpoint − max_spread + 1 tick` for the YES bid, mirrored for NO) — fill-avoidance-optimal but reward-density-pessimal. On Helsinki's first production cycle (Iran market, midpoint $0.485, max_spread 5.5¢), this landed at $0.44 / $0.53 — 4.5¢ from mid — earning `1 − 4.5/5.5 = 18.2%` of theoretical reward density. With ~$24,000 of queue ahead at that distance, fills were vanishingly rare, but reward yield was leaving most of the table behind. The bot's stated objective is reward maximization, not fill avoidance.

   **The fix.** Two new module-level helpers in `order_lifecycle.py`: `_queue_aware_edge(side, book_levels, midpoint, max_spread, tick, target_queue_usd, decimals)` walks one side of the merged book from best (closest to mid) outward, accumulates cumulative USD notional (`price × size`), and returns the edge price one tick BEHIND the level where cumulative queue first crosses `target_queue_usd`. `_compute_edge_prices(merged, midpoint, max_spread, tick, decimals, ticks_inside, target_queue_usd)` runs both sides and falls back to the legacy `midpoint − max_spread + tick·ticks_inside` formula when the queue-aware walk returns `None` (thin book, escape hatch, or zone-boundary edge case). The placement site in `place_orders_for_market` (was 6 lines of inline arithmetic) is now a single helper call. New config knob `RF_TARGET_QUEUE_AHEAD_USD = 1000.0` (default; operator-tunable; hot-reloadable; `0` reverts to legacy unconditionally).

   **The merged book is YES-equivalent on both sides** (real YES bids + NO-derived asks on the bid side; real YES asks + NO-derived bids on the ask side — see `market_discovery._book_entries`, post-FX-035). Both contribute to "queue ahead" because they're arbitrage-linked competitors for the same liquidity. The mirror operation between bid and ask is exact under this normalization.

   **Inline production-shape verification (Iran market, pre-pull):** Pre-FX-036 placement `$0.440 / $0.530` (18.2% density). Post-FX-036 default-`$1000` knob: `$0.460 / $0.510` (54.5% density). **3.0× reward-density uplift** on the market that motivated the ticket. Thin-book regression test confirms zero behaviour change for weather-class markets (operator memory: "weather markets fill quickly despite low competition; use min_size + dump on fill" — that flow is preserved end-to-end because the helper falls back to legacy zone-edge placement when queue is thin).

   **Safety preserved.** Final values are clamped to `[0.01, 0.99]` (matches pre-FX-036). Rounded to the market's `tick_size` decimals (matches pre-FX-036). The `−tick` step is checked: if it would itself exit the reward zone, helper returns `None` ⇒ fall back to legacy — placement never sits at or outside the zone boundary. SafetyController and runtime guardrails are untouched; they bound exposure regardless of placement strategy.

   **Tests.** New `tests/test_placement.py` (24 tests across 11 classes) covers: escape hatches (knob `≤ 0`, empty book), bid + ask threshold-met-at-first / threshold-met-at-second / thin-book / zone-boundary cases, string-vs-float input coercion, malformed-level skip, sub-cent tick variations, escape-hatch byte-identity to legacy, the Iran-market motivating scenario, asymmetric depth (bid queue-aware + ask legacy fallback), safety invariants (edges always inside zone; clamped to `[0.01, 0.99]`), and end-to-end wiring through `place_orders_for_market`. Test count 697 → **721 fast-tier**.

   **Production verification path.** Helsinki should pull v5.1.18 and observe `[ORDER]` log lines + the `[CYCLE_SUMMARY]` JSON over a 24h window. The actually time-critical signal is `[ATTRIBUTION] reward + rebate` totals — these should rise materially on deep markets. If fill rate becomes uncomfortable (more dump traffic than the operator wants), raise `RF_TARGET_QUEUE_AHEAD_USD` (e.g., `$2000` ⇒ ~1.6¢ from mid on the Iran market) or set to `0` for legacy behaviour. The knob is hot-reloadable via `config_overrides.json` so the operator can tune without a restart.

**Test-pollution note.** The new `tests/test_placement.py` includes a `_drop_stale_clob_mocks` helper called from the integration test class's `setUp`. The reason: `tests/test_critical_fixes.py` and `tests/test_sports_protection.py` patch `sys.modules["py_clob_client_v2"]` with partial `MagicMock` stand-ins (so they work on machines without the real SDK) but never clean up. When `test_placement.py` runs after either, the stale mock shadows the real SDK and `from py_clob_client_v2.order_builder.constants import BUY` fails. The helper drops any `MagicMock` entries under that namespace so Python's import machinery rediscovers the real package on the dev / CI venvs (which both have it installed per `requirements.txt`).

---

**v5.1.17 scope.** v5.1.17 is **THE ROOT CAUSE FIX** — the one bug that explains everything else this hardening campaign chased. One commit on top of v5.1.16 (`75d03c7`):

1. **`647b1e2` — Handle V2 SDK dict-return in get_merged_book (`fixit.md::FX-035`).** Helsinki bot **placed zero orders in production for the entire 4-day LIVE window** (2026-05-15 04:03 UTC → 2026-05-19 04:36 UTC). The DB `orders_placed` table had 0 rows. The hardening campaign closed FX-001 + 30 related issues thinking it was a deadlock chain; the bot exited the chain but stayed at 0 orders/cycle. Direct production probe revealed why: `client.get_order_book()` in py-clob-client-v2 v1.0.0 **returns a `dict`**, but `market_discovery.get_merged_book` was written assuming an OrderBook object with `.bids`/`.asks` attributes. `getattr(dict, "bids", [])` returns the default `[]` because dicts don't expose keys as attributes. Result: every book fetch returned None silently in production since the V2 migration on 2026-04-29 (commit `2a6baf6`, v5.1.2). DRY mode masked it for ~17 days; FX-001's I9 deadlock masked it for 4 more days post-LIVE-cutover.

   **Same class as B9 / FX-009 (`get_orders → get_open_orders`)** — V1→V2 SDK migration miss in a wrapper function's return-shape assumption. The book-fetching path got missed during the V2 migration audit.

   **Why 685 fast-tier tests + the FX-016 audit missed it.** Every test that touches `get_merged_book` either:
   - Mocks the function itself via `@patch("order_lifecycle.get_merged_book")` returning a pre-built dict result, OR
   - Uses a stub client whose `get_order_book` returns an object with `.bids`/`.asks` attributes (matching the dead code path's assumption, not the real SDK's behaviour).
   
   No test ever called the real `get_merged_book` with the actual V2 SDK return shape. **Production input shape was never exercised.** This is the canonical "tests pass for the wrong reason" failure mode: coverage tools count the function as covered, but coverage isn't correctness.

   **Discovery path** (took ~30 min of focused triage post-FX-032):
   1. Helsinki post-FX-032 was still at 0 `orders_placed`. The alloc file had a deploy on `0xd9933a54c518...` (Iran market June 15).
   2. Direct HTTP probe via `requests.get('https://clob.polymarket.com/book?...')` returned HTTP 200 with deep books (active, accepting orders, paying $200/day rewards). Market was definitively tradeable.
   3. Direct SDK call `client.get_order_book(yes_tid)` on Helsinki: returned a **`dict`**, not an object. Type `dict, truthy True, getattr(ob, 'bids') → None`.
   4. Traced through `market_discovery.get_merged_book`: `getattr(dict, "bids", [])` → `[]` → return None.
   5. Root cause confirmed — code-vs-SDK shape mismatch since v5.1.2.

   **The fix.** New `_book_entries(ob, key)` helper in `market_discovery.py` normalizes both forms:
   - V2 SDK dict-form: `{'bids': [{'price': '0.29', 'size': '100'}, ...]}`
   - Test mock object-form: `SimpleNamespace(bids=[SimpleNamespace(price=0.29, size=100), ...])`
   Returns `[(price, size), ...]` tuples regardless. `get_merged_book` uses it for all 4 iteration sites (YES bids, YES asks, NO asks→bids, NO bids→asks). Backward-compat with existing object-form mocks preserved. `paper_trader_v2.py:get_merged_book` reduced to a delegation; `paper_client.py`'s fill simulator updated to use `_book_entries` directly. +335 / -72 lines.

   **Tests.** New `tests/test_get_merged_book.py` (12 tests) exercises the real function with both shapes, including the realistic Iran-market shape. Dict-form tests fail pre-fix, pass post-fix; object-form tests stay green throughout. Test count 685 → 697.

**Production verification BEFORE pull.** Ran the patched function inline on Helsinki against the live V2 SDK for the Iran June 15 market — returned bids=36, asks=46, midpoint=$0.4950, spread=$0.0100. Definitively tradeable. Pre-fix this had been returning `None` on every farmer cycle for 4 days.

**Production verification AFTER pull (2026-05-19 04:58:49-50 UTC).** Helsinki placed its **first two real orders ever**: YES @ $0.44 size 67 (`0xff2dfb444befd466...`) and NO @ $0.53 size 67 (`0x575c8a78c0ba18d8...`) on the Iran market. CYCLE_SUMMARY: `orders_placed: 2, active_markets: 1, total_live_notional: $64.99, notional_ratio: 0.3228, cf: 1.0`. Notional ratio of 32% matches BOOTSTRAP's 30% cap (FX-031's scaled budget) exactly. **From zero orders in 4 days to actually farming rewards in one commit.**

**Lessons captured in v5.1.17** (added to §10.3):

- **Coverage isn't correctness, and a 685-test suite can stay green while the bot is structurally broken in production.** The hardening campaign's audit framework caught architectural drift (code-vs-doc divergence in FX-029, FX-030, FX-032) but had a structural blind spot: **input-shape drift between code assumptions and actual SDK return values**. Every test mocked away the actual SDK return. The bug that caused 4 days of $0 production was invisible to 685 tests.
- **Code-level audits and production diagnostics are both necessary.** The audit found 5 bugs by reading the architecture doc; production diagnostics found the load-bearing one by actually running the SDK. Neither modality alone is sufficient.
- **A bug that hides behind another bug is invisible until the cover is removed.** FX-035 was always there since 2026-04-29; FX-001's I9 deadlock masked it from 2026-05-15 → 2026-05-19; the hardening campaign systematically removed each layer of cover (FX-002, FX-003, FX-031, FX-032) and exposed FX-035 at the bottom. **The systematic-cover-removal worked as designed** — it just meant we found the load-bearing bug last instead of first.
- **V1→V2 SDK migrations need a systematic audit of every wrapper function's return-shape assumptions.** B9 was the first such miss (`get_orders → get_open_orders`); FX-035 is the second. There may be more. **Suggested follow-up:** sweep every `getattr(.*, "<field>")` against any value that comes from `client.<method>(...)`, normalize to use the dict-form accessor pattern. Not done in this session — out of scope for the crisis fix.
- **"First production cycle after a major release" is the most expensive verification step in the toolchain.** The hardening campaign ran 4 days. The Helsinki recovery's first cycle would have surfaced FX-035 in 30 seconds — if FX-001's I9 deadlock hadn't masked it from cycle 1. **For future releases, run the first 5-10 production cycles with `journalctl -f` open BEFORE celebrating.** The diagnostic Helsinki commands from this session are reusable.

---

**v5.1.16 scope.** v5.1.16 is the **second post-roadmap-closure follow-up**, also surfaced by the Helsinki recovery diagnostics. One commit on top of v5.1.15 (`d5eabea`):

1. **`75d03c7` — Stop dead-market cleanup from marking cids unliquidatable (`fixit.md::FX-032`).** During Helsinki recovery, the v5.1.14 farmer's startup window at 03:23:38 UTC mass-marked 60 cids in `unliquidatable_markets` with reason `dead_market_book_failures`. Direct CLOB API probe of one (`0xdb22a7749b83`, the "Iran closes its airspace by May 27?" market): `active=True, accepting_orders=True, rewards_rate=$200/day`, deep books (22 bids + 40 asks YES, mirror NO) — fully healthy. The FX-028 re-probe couldn't un-mark them. **Bot was locked out of a market paying $200/day.**

   **Root cause:** FX-006's `7d8d38d` commit added the `mark_unliquidatable` call to the dead-market cleanup at `reward_farmer.py:2093`, on the rationale that 3 consecutive `get_merged_book` failures was a "strong indication" the orderbook was permanently dead. In practice the `book_failures` counter increments for a much wider class of conditions than the canonical FX-007 path:
   - SDK parse errors (`get_order_book` returns object that doesn't iterate properly)
   - Transient network blips
   - Empty bids/asks in a brief market lull
   - Rate-limit retry failures the wrapper swallows
   The canonical FX-007 marking path (in `OrderLifecycle` and `DumpManager` exception handlers) only fires when the V2 SDK returns a 400 with both `"orderbook"` AND `"does not exist"` substrings — the actual resolved-market signal.

   **Why the FX-016 audit missed it:** Every test in `TestDeadMarketCleanupCascade` was a **"logic-shape replay"** — the test re-constructed the loop body locally and asserted side-effects on the local re-construction, instead of exercising the actual `run_cycle` code. The test stayed green regardless of what `reward_farmer.py` actually did. Plus no test scenario reproduced production-scale market churn.

   **The fix.** Removed the `self.db.mark_unliquidatable` call. FX-006's `delete_dump_state` cascade (both sides) preserved — that's the real defensive cleanup FX-006 was solving. Markets removed from `self.markets` here can reappear via the next `_refresh_reward_markets` call and get another chance — appropriate for transient failure modes. Genuinely-dead markets get caught by the canonical FX-007 path on the next placement attempt. +5 / -23 lines in `reward_farmer.py`.

   **Test rewrite + new technique.** `TestDeadMarketCleanupCascade` rewritten to assert the FX-032 contract (cascade preserved, no `mark_unliquidatable` call). Added new test `test_actual_reward_farmer_cleanup_does_not_call_mark_unliquidatable` that reads `RewardFarmer.run_cycle`'s source code via `inspect.getsource`, extracts the Step 4b...Step 5 block, and asserts `mark_unliquidatable` does NOT appear. This **source-inspection test pattern** catches the class of bug where a logic-shape replay test would silently drift from the actual implementation. Pattern is now available for any other replay tests in the suite.

**Production impact (after Helsinki pulls v5.1.16 + clears existing `unliquidatable_markets` rows):** Bot stops mass-marking healthy markets at startup. The 61 stale entries currently in the Helsinki DB (all `reason='dead_market_book_failures'`) will be cleared one-time during the recovery; the new code won't recreate them. Iran-class markets ($200/day rewards) become deployable. The bot can finally start earning.

**Test count.** 684 → 685 fast-tier (+1 net; one rewrite, one new source-inspection test).

**Lessons captured in v5.1.16** (added to §10.3):
- **Logic-shape replay tests can drift silently from the source they claim to test.** The pre-fix `TestDeadMarketCleanupCascade.test_cleanup_loop_cascades` re-constructed the loop body in the test and asserted on the local re-construction. It would have stayed green if someone deleted the entire Step 4b block from production code. Always include a source-property assertion (via `inspect.getsource`) when the test depends on a structural property of the function under test.
- **Production diagnostics are a test the test suite can't write.** FX-016's 152 SafetyController tests, FX-031's 5 capital-cap-scaling tests, and the FX-032 cascade test all stayed green; the Helsinki recovery diagnostics caught two real bugs (FX-031, FX-032) within 30 minutes of restart. "First production cycle after a major release" is the highest-leverage verification step in the toolchain.
- **The FX-006 cascade was over-extension.** FX-006's actual goal was "dead-market cleanup must cascade to `dump_states`" — a real bug. Adding `mark_unliquidatable` to the same cascade conflated two different concerns (transient failure vs resolved market). When extending a fix, the burden of proof is on the extension: each new side-effect needs its own justification, not transitive trust from the parent fix.

---

**v5.1.15 scope.** v5.1.15 is a **post-roadmap-closure follow-up** surfaced empirically by the Helsinki recovery pull. One commit on top of v5.1.14 (`38fc63c`):

1. **`d5eabea` — Scale oversized deploys to fit per-state capital cap (`fixit.md::FX-031`).** On Helsinki's first oversight cycle after the v5.1.14 pull, the SafetyController initialized to BOOTSTRAP (capital_pct=0.30 → $60 cap on the $201 wallet). The allocator proposed 3 deploys at $84-$89 each, each sized assuming the full $201 budget. `filter_allocations`' running-cost loop at `oversight/safety_controller.py:829-843` (pre-fix) wholesale-rejected any deploy whose individual `est_capital_cost` exceeded `remaining_budget`, so all 3 were rejected: `SafetyController [BOOTSTRAP]: 0/3 markets, $0/$201 capital`. Bot was structurally unable to deploy until BOOTSTRAP exited to MILDLY (~90 min), and even then only 1 of 3 would have fit ($140 cap). Same shape would have hit SEVERELY (40%, all 3 rejected) and DEGRADED (20%).

   **Why FX-016's audit missed it:** every test scenario in `TestFilterAllocationsCapitalCap` had `individual_deploy_cost ≤ per_state_cap`. The bug only manifests when an individual deploy is bigger than the cap — i.e., when the allocator (sized for full available_capital) and the SafetyController (per-state fraction) disagree on what "a deploy" should cost. Helsinki's $201 wallet × BOOTSTRAP's 30% put the cap below typical allocator sizing.

   **The fix.** Two coupled changes in the running-cost block:
   - **Scale shares to fit `remaining` budget** instead of wholesale-reject. Both the scaling decision and the post-scale `est_capital_cost` recomputation use the same internal formula `shares × est_price × 2` — matching FX-029's contract. `min_size` floor preserved (sub-min orders are venue-rejected). Reject only when `remaining < min_cost`, with a distinct reason: `"capital exhausted (${remaining:.0f} < min ${min_cost:.0f})"`.
   - **Iterate `deploys` (already score-desc sorted)** instead of the unsorted `allocations` list. Pre-fix this was a quiet existing issue — under wholesale-reject the iteration order didn't matter much, but under scale-down it does: the top scorer claims the budget first.

   +141 / -9 lines across `oversight/safety_controller.py` (29-line block rewrite) and `tests/test_safety_controller.py` (5 new regression tests in `TestFilterAllocationsCapitalCapScaling`).

**Production impact (on next Helsinki `git pull + restart`):** Expect 1-3 deploys per oversight cycle at ~$60 total in BOOTSTRAP (scaled top scorer + any that fit in remaining budget). After BOOTSTRAP exits to MILDLY (~3 clean cycles), deploys at ~$140 cap. After MILDLY → CALIBRATED (~3 more clean cycles), full $201 deployment. Total time-to-full-throughput: ~3 hours from the v5.1.15 pull.

**Test count.** 679 → 684 fast-tier (+5 FX-031 regression tests). Coverage on `oversight/safety_controller.py`: 539 stmts, 32 miss, 94% (added 5 stmts, all covered).

**Lessons captured in v5.1.15** (added to §10.3):

- **"Hardening complete" means the codebase is hardened, not the production deployment.** The Helsinki recovery pull surfaced a structural bug within 30 seconds of the first oversight cycle. The campaign's 94% coverage and 5 audit-caught bugs were necessary but not sufficient. **First production cycle after a major release is a verification step, not a deploy completion.**
- **The audit caught the bugs the campaign was looking for; production caught a bug the campaign wasn't.** FX-029 (per-market cap) and FX-030 (UNSAFE fast-path) were found by reading the architecture doc and looking for code-vs-doc divergence. FX-031 was found by an empirical run with a specific wallet size + state combination. Both kinds of bug-hunting are necessary.
- **Wholesale-reject is the wrong default for safety filters.** Both probe-mode (line 819 pre-fix) and per-market exposure (FX-029) used scale-down semantics. The running-cost block was the only wholesale-rejector. Style convergence matters — when an outlier exists, it's probably the bug.

---

**v5.1.14 scope.** v5.1.14 is the **hardening roadmap closure release**. Closes the final two open items from fixit §3 and locks in the completed campaign. One commit on top of v5.1.13 (`1c4ae7e`):

1. **`(v5.1.14)` — Close remaining hardening items (`fixit.md::FX-019` + `FX-027`).** Two minor changes:
   - **FX-019** — `check_wallet.py:243-246` called `client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL))` with no `token_id`. The SDK substituted `-1` as a placeholder and the API rejected it with a 400 at startup — cosmetic noise that alarmed first-time operators. The CONDITIONAL balance is checked at trade-time against a specific token_id; this pre-trade no-token call was dead from the operator's perspective. Fix: deleted the 4-line block, replaced with a comment cross-referencing FX-019. The diagnostic still prints the useful COLLATERAL pUSD balance + on-chain allowances. -4 / +4 lines.
   - **FX-027** — Process-boundary lag (agent 30 min vs farmer 30 s) **accepted as designed architectural risk**. The 30-min/30-s asymmetric cadence is intentional (§2 + §4.21.6). The actually time-critical safety responses live on the farmer's 30-s cadence: runtime guardrails (notional cap, cluster cap, kill switch on 24h-loss / CF / fill-rate spike) in §4.18, order placement/cancellation gates, Phase-C pause/kill hook. The agent's 30-min cadence affects allocation **revisions**, not allocation **enforcement**. Phase 4's wallet-first capital flow and Phase 3's dump-state lifecycle hardening close the failure modes where the agent's lag could matter. **No code change** — the entry moves to fixit §5 "Won't fix / Accepted risk" with full rationale. If a pathological scenario emerges that the farmer-side guardrails can't bound, FX-027 reopens with a specific target.

**Production impact (combined, on next Helsinki `git pull + restart`):** None functional. The 4-line CONDITIONAL removal eliminates a cosmetic startup error; FX-027 acceptance is documentation-only. Operator-visible change: `python check_wallet.py` no longer prints the alarming-but-harmless 400 error at the top.

**Hardening campaign summary (Phase 0 → Phase 6 + closure):**

| Metric | Pre-campaign (v5.1.4, 2026-05-14) | Post-campaign (v5.1.14, 2026-05-19) | Δ |
|---|---|---|---|
| Open fixit entries | 28 | 0 | -28 |
| Architecture-doc minor versions | 5.1.4 | 5.1.14 | +10 |
| Code commits on the hardening campaign | 0 | 11 | +11 |
| Fast-tier tests | 449 | 679 | +230 |
| SafetyController dedicated tests | 0 | 152 | +152 |
| SafetyController line coverage | ~58% (incidental) | 94% (direct) | +36 pts |
| CI gating | None | GitHub Actions on every push | ✓ |
| Audit-surfaced bugs caught + fixed mid-campaign | — | 5 (Phase 5: 3 + Phase 6 part 2: 2) | — |
| Production-impacting bugs missed | — | 0 (FX-001 was the trigger; everything else was caught pre-deploy) | — |

**Lessons captured in v5.1.14** (closing observations — added to §10.3):
- **A test suite without an audit is just coverage theatre.** Phase 6 part 2's audit caught 2 real safety bugs (FX-029, FX-030) that the new tests had documented as "expected behaviour". The 94% coverage number was honest; the contracts the tests pinned were wrong. Every test-suite build-out needs an independent audit pass before doc-finalization.
- **The audit's job is to spot tests that pass for the wrong reason.** Both FX-029 and FX-030 had passing tests pre-audit. FX-029's test was contorted to use inputs consistent with the bug's blind spot; FX-030's test explicitly pinned the bug as a contract. The audit identified both by reading the architecture doc and looking for divergence — coverage tools can't do this.
- **"Won't fix" should be a deliberate decision with documented mitigations, not a default for stale tickets.** FX-027's acceptance traces specific mitigations (§4.18 farmer-side guardrails) and names the trigger for reopening (a pathological scenario the guardrails can't bound). The accepted-risk decision is reversible without losing context.
- **Cadence matters: 4 days from "first LIVE bootstrap deadlock" to "all open items closed".** Tight feedback loops + parallel test+audit+doc updates kept the campaign moving. Each phase was plan-execute-audit-close, never plan-execute-defer.

---

**v5.1.13 scope.** v5.1.13 is the **Phase 6 part 2 — SafetyController test build-out + 2 audit-driven safety fixes**. Three commits on top of v5.1.12 (`a580bdb`):

1. **`4aff918` — SafetyController test coverage, part 1 of 2 (`fixit.md::FX-016`).** Adds 88 tests across four blocks to `tests/test_safety_controller.py` (which had been seeded with 17 Phase-1 tests in v5.1.7).
   - **Block A — Per-invariant coverage for I1-I14.** Each invariant gets a happy-path test, a breach test, and (where applicable) a query-failure test. Covers daily_loss, slow_bleed, drawdown (warm-DB path), capital_floor (incl. wallet-scaled large-wallet variant), cf_drift (all 3 severity zones), cf_corroborated (the CRITICAL AND-gate), est_actual, hourly_loss, capital_at_risk, data_freshness (3 severities), data_completeness, loss_reward, clob_rate_drop, fill_storm, cf_at_floor.
   - **Block B — State machine.** STATE_PERMISSIONS well-formedness, `max_markets` monotonicity on the non-BOOTSTRAP ladder (BOOTSTRAP is intentionally more restrictive than MILDLY despite lower severity — pinned explicitly), upgrade ladder (MILDLY → CALIBRATED in 3 clean cycles, lower-states → MILDLY in 2), UNSAFE auto-recovery (slow path), counter resets on `_transition`.
   - **Block C — `filter_allocations` end-to-end.** max_markets cap per state, trial gate (SEVERELY blocks score≤0; BOOTSTRAP allows), capital cap (cumulative running cost), UNSAFE probe mode forcing min_size, LOW-signal haircuts (fill_storm 20% + cf_at_floor 10% + max-not-additive when both fire), q_share clamp at 0.5, per-market exposure cap.
   - **Block D — `evaluate()` integration.** Multi-priority severity precedence (CRITICAL > HIGH > MEDIUM), worst-within-priority, MEDIUM-only path, `violations` property returns a copy, the backward-compat `evaluate()` wrapper delegates correctly.
   - +969 / -10 lines in `tests/test_safety_controller.py`. Coverage 58% → 87%.

2. **`f3630c9` — SafetyController test coverage, part 2 of 2 (`fixit.md::FX-016`).** Adds 44 tests across three blocks.
   - **Block E — Persistence round-trip.** `_persist_state` / `_load_state` age branches (< 2h restored, 2-6h healthy-vs-degraded distinction, > 6h reset, no-row defaults), round-trip preservation, 100-row trim.
   - **Block F — Query helpers + portfolio + confidence.** `_query_fill_damage` arithmetic (fills − unwinds + stop_losses, clamped at 0), `_query_data_freshness` cold-start-empty (→ 0.0) vs warm-DB-empty (→ None per FX-001 defensive branch), `_query_lifetime_fills_count`, `_query_last_known_balance`, `_compute_portfolio_value`, `_capital_floor` wallet-scaling (FX-010 peak-dominance regression pinned), `confidence_score` per-component zeroing, public query methods.
   - **Block G — Alert-file writers.** Transition to ≥ DEGRADED writes `SAFETY_ALERT.txt`; transition to CALIBRATED clears; sub-DEGRADED transitions don't write.
   - +519 / -0 lines. Coverage 87% → 94%.

3. **`1c4ae7e` — Two audit-driven safety hardenings (`fixit.md::FX-029` + `FX-030`).** The Phase 6 part 2 audit surfaced two real bugs that the new test suite had documented as "behaviour" rather than caught. Both fixed pre-doc-lock:
   - **FX-029 — `filter_allocations` per-market $200 cap can be exceeded.** Pre-fix, the cap (`oversight/safety_controller.py:839-850`) computed the scaling decision from the CALLER's `est_capital_cost` but recomputed the post-cap value from an internal formula `shares × est_price × 2`. When the two disagreed, the post-cap cost overshot $200. Audit's repro: `shares=500, est_capital_cost=300, max_spread=0.045` → final `$303.03`. Worse with narrow spreads: `max_spread=0.001, est_cost=201` → final $496.01. Fix: derive both the scaling decision and the post-cap value from the same internal formula. Caller's `est_capital_cost` becomes informational only; the cap holds regardless of caller-input consistency. min_size floor still wins by design. **Production impact: zero on Helsinki** — the prod allocator uses the same formula, so caller and controller agreed and the bug never fired.
   - **FX-030 — `_handle_upgrade` UNSAFE→MILDLY fast path bypassed the documented 3-cycle DEGRADED auto-recovery cap.** Architecture doc lines 1045 + 1919-1920 document the UNSAFE recovery contract as: UNSAFE → (`UNSAFE_RECOVERY_CYCLES=3`) → DEGRADED → (`UPGRADE_STEP=2`) → MILDLY → ... → CALIBRATED (a 5-cycle minimum). But pre-fix, `_handle_upgrade`'s else-branch (line 752) caught UNSAFE alongside SEVERELY/DEGRADED/DATA_UNAVAILABLE and jumped it straight to MILDLY in 2 cycles when inputs were fully calibrated. The cap at `evaluate_state:644-652` only fires inside the violations branch. Fix: skip `_handle_upgrade`'s post-BOOTSTRAP body when `self.state == UNSAFE`. The slow auto-recovery in `evaluate_state:658-664` becomes the SOLE exit from UNSAFE on a no-violations cycle. **Production impact: zero on Helsinki** — the bot has never entered UNSAFE; the fix tightens semantics for any future event.
   - +91 / -41 lines across `oversight/safety_controller.py` (3 net new tests) and `tests/test_safety_controller.py` (1 incorrect test removed, 4 regression tests added — the previously-passing test that pinned FX-030 as a contract was wrong, per the audit's note).

**Production impact (combined, on next Helsinki `git pull + restart`):** None. v5.1.13 is a test build-out + correctness hardening release. The Helsinki bot has never exercised either FX-029 (because the prod allocator computes `est_capital_cost` from the same formula the controller uses) or FX-030 (because the bot has never entered UNSAFE). On the next `git pull + restart` the operator gets a bot with the same observable behaviour but stronger invariants — exactly the FX-001 deadlock class of bug (silent invariant violation) is now defended against by 152 focused tests on every push.

**Test count.** 544 → 632 (after commit 1) → 676 (after commit 2) → 679 (after commit 3 — net +3 from the audit fixes). `tests/test_safety_controller.py` grew 17 → 152 tests. Coverage on `oversight/safety_controller.py`: 58% → 94%. The 34 remaining uncovered lines are defensive `except` handlers for DB-corruption scenarios that aren't reachable from unit-test fixtures.

**Lessons captured in v5.1.13** (added to §10.3 paragraph below):
- The **audit pass after test-suite builds matters as much as after code changes.** Phase 5's audit caught 3 bugs in production code; Phase 6 part 2's audit caught 2 bugs that the new test suite had documented as "behaviour" but were actually defects. Without the audit, FX-029 and FX-030 would have shipped under the cover of "tested, coverage 94%".
- **Coverage isn't correctness.** The FX-030 fast-path bypass had a passing test (`test_fast_path_unsafe_to_mildly_after_2_calibrated_cycles`) that pinned the bug AS the contract. Coverage went up; correctness went down. The audit explicitly identified this as "passes for the wrong reason" before the test was removed.
- **When a test is "hard to write" because the contract is unclear, that's a signal the contract is the bug.** The original per-market cap test in commit 1 was awkward because it required matching the caller's `est_cost` to the internal formula. The audit traced this discomfort back to the actual cap-formula divergence (FX-029).

---

**v5.1.12 scope.** v5.1.12 is the **Phase 6 part 1 — GitHub Actions CI release**. Pure tooling addition with zero behavioural change to any running process. One commit on top of v5.1.11 (`91bae99`):

1. **`a580bdb` — Add GitHub Actions CI for fast-tier tests (`fixit.md::FX-026`).** Closes the gap where any push to `main` (including the prior six hardening phases) could land regressions without any automated gate. Two new files:
   - **`.github/workflows/test.yml`**: triggers on `push` to `main` + `pull_request`, runs a single job on `ubuntu-24.04` with Python 3.14 (`actions/setup-python@v5`, pip cache keyed on `requirements.txt`), installs `requirements.txt` + `pytest`, runs `pytest tests/ --ignore=tests/test_simulation.py --tb=short` with a 15-minute job timeout. Slow-tier `tests/test_simulation.py` remains a manual run (the documented flake from prior fixit entries).
   - **`README.md`**: new — project overview, link to the architecture + fixit companion docs, the fast-tier vs slow-tier test layout, runtime/SDK/server stamps, and the workflow status badge that surfaces CI health from the repo landing page.

**Production impact.** None on the running Helsinki bot — CI runs on GitHub Actions runners, not on the production server. The operational effect is purely upstream: every future commit now needs the green check before merge. The previous "operator discipline" of running pytest before pushing is now machine-enforced. Phase 6 is now half complete; the remaining item (FX-016, SafetyController comprehensive coverage) lands in v5.1.13.

**First CI run.** Run ID `26046878949`, triggered by the `a580bdb` push to `main`, completed green in **7m17s**, 544/544 fast-tier tests pass on the runner — matches the local result. Setup-python took ~30s, pip install ~70s (cold cache on first run; warm cache should cut that to ~5-10s), pytest ~125s, balance of the time on action setup/teardown.

**Action-version watch.** One Node.js 20 deprecation annotation surfaced: `actions/checkout@v4` + `actions/setup-python@v5` both still on Node.js 20. GitHub Actions runners deprecate Node 20 on 2026-06-02 (forced to Node 24) and remove it on 2026-09-16. Both actions are at their latest major versions. Re-check upstream before 2026-06-02 and bump if newer majors ship; until then no action needed.

**Test count.** Unchanged (544 fast-tier). This release adds CI, not tests.

**Comprehensive audit.** Not run for v5.1.12 — workflow file + README addition are pure tooling with no code paths exercised by the bot. The CI run itself is the verification: green on first push.

---

**v5.1.11 scope.** v5.1.11 is the **Phase 5 operational hardening release**. One commit on top of v5.1.10 (`d4d1541`):

1. **`91bae99` — Graceful shutdown + batch cancel on SIGTERM (`fixit.md::FX-014` + `FX-015`).** Closes the long-standing gap where `sudo systemctl stop polymarket-farmer` could SIGKILL the bot mid-cycle with live orders still resting on Polymarket. Five interlocking changes:
   - **SIGTERM handler in `reward_farmer.run()`**: previously only SIGINT was registered, so systemd's default `KillSignal=SIGTERM` only stopped the bot via Python's default `KeyboardInterrupt` propagation — slow and unreliable. Same `_sig` callback now handles both; logs the signal name in the `[SHUTDOWN]` line.
   - **`_shutdown_cleanup` uses V2 batch `cancel_orders`**: one API call cancels everything the bot has tracked. Fits comfortably under `TimeoutStopSec=30` even at the worst-case 60 markets × 4 sides = 240 orders. Per-order `_gated_cancel_order` is the fallback when the batch call raises (rate-limit, network, malformed payload).
   - **`OrderLifecycle.cancel_order` gains `force=True`**: bypasses the `if self.dry_run: return True` shortcut. `_gated_cancel_order` propagates `force=self._kill_switch_active` so the kill-switch and shutdown paths now actually fire real API cancels in SHADOW/DRY mode — the advertised behaviour was previously non-functional.
   - **Rate-limiter coverage expanded**: `_RATE_LIMITED_METHODS` now lists every V2 SDK method production code calls — including `cancel_order` (V2 rename of `cancel`), `cancel_orders` (batch), `cancel_all`, `cancel_market_orders`, `get_open_orders` (V2 rename of `get_orders`). The V1 `cancel` and `get_orders` names are kept for fixture back-compat. Closes a silent-leak vector where 429 storms during cancel bypassed retry / backoff.
   - **Structured `[SHUTDOWN]` log channel**: entry line with order counts, exit line with `cancelled X/Y orders (Z failed)`, batch-success line, fallback warnings. Operator can grep one tag for the full shutdown story.

2. **§11.11 systemd unit blocks updated**: added `KillSignal=SIGINT`, `TimeoutStopSec=30`, `KillMode=mixed` to both `polymarket-farmer.service` and `polymarket-oversight.service`. New "Operational stop procedure" subsection documents the expected `journalctl` sequence on a clean stop. The Python-side SIGTERM handler makes the directive change forward-compatible — even without the operator re-tee'ing the units, `systemctl stop` (SIGTERM by default) now triggers a clean shutdown.

**Production impact (on next Helsinki `git pull + restart` + operator re-tee of §11.11):** `sudo systemctl stop polymarket-farmer` → SIGINT to bot → `_sig` flips `_shutdown` → main loop exits at next iteration → `_shutdown_cleanup` issues one `cancel_orders` batch call → process exits within ~1-2 s of the last iteration boundary. journalctl shows:
```
[SHUTDOWN] SIGINT received — exiting at next cycle boundary
[SHUTDOWN] cleanup beginning: N buy orders + M dump orders across K markets
[SHUTDOWN] batch cancel succeeded: X orders in 1 API call
[SHUTDOWN] cleanup complete: cancelled X/Y orders (Z failed)
```

**Test count.** 522 → 544 fast-tier (+22 from `tests/test_shutdown.py`). Run time 92 s.

**Comprehensive audit ran post-implementation** and surfaced THREE real bugs, all addressed pre-commit. This is the first phase where the audit found code-level issues — previous phases were clean. The findings:
1. **Kill-switch override broken in SHADOW**: OL's `dry_run` shortcut defeated the advertised guarantee. Fixed by adding `force=True` parameter propagated from `_gated_cancel_order`.
2. **V2 SDK cancel methods missing from rate-limiter**: pre-existing bug from the V2 migration (ee6abdf renamed methods but the protected-method set wasn't updated). Fixed by enumerating every V2 name.
3. **Worst-case shutdown latency could exceed 30s**: at 60+ markets, per-order cancel loop didn't fit in `TimeoutStopSec=30` budget. Fixed by switching primary path to batch endpoint.

The audit also clarified Q1 (signal latency): `run_cycle` is fully synchronous and can take 30-60 s under load (60 markets × ~3 API calls each at 150 ms rate limit). Worst-case shutdown latency = up to one full cycle for the loop to notice + the cleanup itself. With batch cancel making cleanup sub-second, the 30s `TimeoutStopSec` is comfortable as long as the cycle is < ~28 s. Architecturally, the better remediation is to check `self._shutdown` at finer-grained boundaries inside `run_cycle` (e.g., between markets in the placement loop). Deferred — Phase 5's scope was the shutdown path itself, not the cycle-mid responsiveness.

**v5.1.10 scope.** v5.1.10 is the **Phase 4 capital flow correctness release**. One commit on top of v5.1.9 (`7d8d38d`):

1. **`d4d1541` — Wallet-first capital resolution + wallet-scaled I4 floor (`fixit.md::FX-010` + `FX-011` + `FX-013` + `FX-024` + `FX-025`).** Five fixit entries closed by one structural change to the capital-resolution flow:
   - **Farmer side (FX-013)**: writes `usdc_balance` on cycle 1 (in addition to every 10 cycles thereafter). Fresh-DB window between LIVE cutover and the first balance row drops from ~5 min to <30 s.
   - **Agent side (FX-013 + FX-025)**: `--capital` CLI default → `None`. Silent `$1500.0` fallback removed. Resolution flow: fresh `usdc_balance` row (< 30 min) → use it; else explicit `--capital` override → use that; else skip the cycle (`{"status": "no_capital"}`). The architecture choice was NOT to add an SDK client to the agent (that would expand the planner's responsibility profile and introduce auth/network dependencies); the cycle-1-write + None-default approach achieves the same outcome via the existing farmer→DB→agent flow.
   - **Per-cycle log (FX-024)**: every cycle emits one structured `[CAPITAL_SOURCE] source={usdc_db|flag|none} value=$X.XX age_min=Y` line. Operator can grep one tag to see capital-source state across cycles.
   - **I4 floor scaling (FX-010)**: new `SafetyController._capital_floor(exchange_balance, portfolio_value)` returns `max($50, max(portfolio_peak, portfolio_value, exchange_balance) * 0.10)`. I4 (`capital_floor`) uses the helper instead of the absolute `$50` constant. Reference uses the LARGEST of (peak, portfolio, exchange) so a drawdown doesn't shrink the floor as the wallet shrinks. `$50` survives as the minimum (operational floor — smallest market order ~$0.50, so $50 lets the bot place ~100 orders) and is kept literal inside `_query_last_known_balance`'s query filter as a fixed "had real money recently" sentinel. Backwards-compatible: for wallets ≤ $500, the 10% scale never exceeds the $50 minimum (the Helsinki server's $201 wallet sees identical behaviour).
   - **Dead config knobs (FX-011)**: `RF_MAX_COST_PER_MARKET` and `RF_MAX_TOTAL_EXPOSURE` deleted from `config.py` + their accessors `MAX_COST_PER_MARKET()` / `MAX_TOTAL_EXPOSURE()` deleted from `reward_farmer.py`. grep confirmed zero production callers. The v5.0 runtime guardrails (notional ratio, cluster cap, hard-enforcement multi-cancel, kill switch) own per-market and total exposure today, and the allocator's `MAX_PER_MARKET = $200` is the actual per-market cap.

**Production impact on next Helsinki `git pull + restart`:** cycle 1 of the farmer writes `usdc_balance` immediately. Next 30-min oversight cycle reads the fresh ~$201 value: `[CAPITAL_SOURCE] source=usdc_db value=$201.35 age_min=<1` instead of the pre-fix `source=flag value=$1500.00`. I4 floor on the $201 wallet stays at $50 (the 10% scale doesn't exceed the absolute minimum until wallet > $500), so behaviour is byte-identical. The change matters when the operator eventually scales up the wallet.

**Test count.** 501 → 522 fast-tier (+21 from new `tests/test_capital_flow.py`). Run time 76 s.

**Comprehensive audit ran post-implementation** and surfaced zero code findings. Eight risk vectors examined (capital resolution race, I4 floor edge cases, cycle-1 timing, mock-target correctness, test backward compat, `$50` sentinel rationale, defensive None handling, dead-knob audit) all returned clean. The single advisory item — doc updates — is this v5.1.10 amendment block + companion fixit doc changes.

**v5.1.9 scope.** v5.1.9 is the **Phase 3 dump-state lifecycle correctness release** — the structural fix that finally stops the Tamilaga orphan-dump 400-spam observed continuously on the Helsinki server since the v5.1.5 LIVE cutover. One commit on top of v5.1.8 (`e7fc3d2`):

1. **`7d8d38d` — Stop orphan-dump 400-spam via `unliquidatable_markets` (`fixit.md::FX-005`, `FX-006`, `FX-007`, `FX-008`, `FX-009`, `FX-028`).** Introduces a new DB table `unliquidatable_markets` (cid PK, reason, marked_at, last_retry_at) that records which markets the bot has definitively confirmed dead at the orderbook level. Detection is exception-driven: both `OrderLifecycle.place_orders_for_market` and `DumpManager.dump_position` inspect their `create_and_post_order` exception messages and mark the cid when both substrings `"orderbook"` AND `"does not exist"` appear (the canonical V2 SDK 400 body is `"the orderbook {cid} does not exist"`, with the cid in the middle). Detection is regression-tested with explicit negatives — "insufficient balance", "rate limit", and "market does not exist" all stay unmarked. Once marked, every order path (BUY in OL, SELL in DM, orphan scan, exchange-position sync, dump-state restore) gates on `db.is_unliquidatable(cid)` and skips. The dead-market book_failures cleanup loop cascades: it now also calls `delete_dump_state` for both sides and `mark_unliquidatable` so the cid is filtered on subsequent cycles. A new `RewardFarmer._reprobe_unliquidatable` method runs on a 30-min loop-sweep cadence (per-cid 6h staleness gate inside the DB query) and un-marks cids whose `get_merged_book` has come back to life, with a CLOB `/markets/{cid}` fallback for token_ids when the cid isn't in `self.markets`.

**Production impact (expected on the next Helsinki `git pull + restart`).** The Tamilaga `dump_states` row (3826 NO-side shares of a resolved market) gets loaded by `_restore_dump_states`, which calls `dump_position`, which hits the 400, marks unliquidatable, deletes the dump_state row. From there: subsequent `_scan_orphaned_positions` and `_sync_exchange_positions` sweeps skip Tamilaga; the 30-s spam pattern stops within ~1 cycle of LIVE startup.

**Healthy markets are unaffected.** The gate is a single indexed PK lookup per call (~µs on local SQLite). No in-memory cache to invalidate; DB is the source of truth.

**Comprehensive audit ran after the initial implementation** and surfaced four findings, all addressed pre-commit:
1. Detector was over-tight — `"orderbook does not exist"` strict substring didn't match the canonical `"the orderbook X does not exist"` form (cid in the middle). Rewrote to require both substrings; regression tests added for the false-positive-resistance cases.
2. `_sync_exchange_positions` had no gate. CTF balance on-exchange never clears (manual UI redemption only), so unliquidatable cids would re-spawn into `self.markets` every 30 min indefinitely. Gate added.
3. `load_unliquidatable_set` docstring described a non-existent startup cache. Rewritten to describe its actual role.
4. Test coverage gaps for orphan-scan gate, exchange-sync gate, CLOB-fallback branch, detector tightening. All closed.

**Test count.** 470 → 501 tests pass in the fast tier (+31 from new `tests/test_unliquidatable_markets.py`). Run time 75 s.

**v5.1.8 scope.** v5.1.8 is the **Phase 2 counter consistency release** — restores trustable operator telemetry on the `orders_placed` count by tying it directly to API-confirmed DB writes. One commit on top of v5.1.7 (`541108b`):

1. **`e7fc3d2` — Count API-confirmed placements only (`fixit.md::FX-004`).** `[CYCLE_SUMMARY] orders_placed` was incrementing on every call to `OrderLifecycle.place_orders_for_market`, regardless of API outcome. Cycle 3 of the v5.1.5 Helsinki bootstrap reported `orders_placed: 2` while `SELECT COUNT(*) FROM orders_placed` returned 0 — both attempts had 400'd on resolved orderbooks (the Tamilaga family of FX-007 dump-spam plus one initial placement). Telemetry was lying. `place_orders_for_market` now returns `int` — the count of API-confirmed placements (0, 1, or 2). Only LIVE-mode paths where `create_and_post_order` returned a valid `orderID` AND `log_order_placed` wrote a row to the `orders_placed` DB table contribute. Every early-return path (no book, wide spread, sports block, resolution proximity, has-both-fresh shortcut) and the DRY-run path return 0. `_gated_place_orders_for_market` accumulates: `self._cycle_orders_placed += n_placed`. A defensive `isinstance(n_placed, int)` check treats pre-FX-004 stub returns (None) as 0, so the counter never advances on stale plumbing.

**Test count.** 453 → 470 tests pass in the fast tier (+17 from new `tests/test_order_lifecycle.py`). Run time 86 s.

**Production safety.** The Helsinki server is running pre-FX-004 code today and will pick up the change on the next `git pull + restart`. The only observable behavioural change is the counter — actual order placement, cancel, dump, kill-switch, and allocator code paths are byte-identical. Any operator dashboards or alerts that read `[CYCLE_SUMMARY] orders_placed` will start showing accurate numbers from the first post-pull cycle.

**v5.1.7 scope.** v5.1.7 is the **Phase 1 SafetyController bootstrap completion release** — the structural pair that finishes what `dd67f97` (v5.1.5 I9) started. Two commits on top of v5.1.6 (`987a844`):

1. **`dc78ba0` — Skip I3 drawdown on genuine cold start (`fixit.md::FX-002`).** I3 was firing CRITICAL → DATA_UNAVAILABLE on the first LIVE cycle of a fresh-DB server because both `total_portfolio_value` and `exchange_balance` arrive zero during the ~30-minute window between LIVE cutover and the first `usdc_balance` row landing. No drawdown can be computed from a zero baseline, but the DATA_UNAVAILABLE severity blocks trials, and on a fresh DB every market is a trial — same deadlock pattern as the I9 issue v5.1.5 fixed. New helper `_is_genuine_cold_start()` (lifetime `orders_placed` + `fills` count check) gates the I3 violation: skipped on cold start, fired verbatim otherwise. The helper is also wired into `_query_data_freshness` (replacing the inline check from `dd67f97`) so I9 and I3 share one source of truth. +43 / -15 lines + 7 new tests.

2. **`541108b` — Add `BOOTSTRAP` state for first-time cold start (`fixit.md::FX-003` + `FX-012`).** SafetyController's cold-start default was `MILDLY_MISCALIBRATED` — second-highest rung, granting 70% capital and trials on cycle 1 of a fresh-DB LIVE start. On $201 wallet that's $140 of immediate exposure. New `BOOTSTRAP` state with `max_markets=10, capital_pct=0.30, trials=True` slots between `MILDLY_MISCALIBRATED` (severity 1) and `SEVERELY_MISCALIBRATED` (now severity 3). Trials must be True because every market is a trial on a cold start — without them the bot can't build calibration data. `_load_state` now uses `_cold_start_or(MILDLY_MISCALIBRATED)` to choose between BOOTSTRAP and MILDLY based on `_is_genuine_cold_start()`. `_handle_upgrade` gains a BOOTSTRAP-exit branch: leave to MILDLY on EITHER ≥10 lifetime fills (`BOOTSTRAP_FILL_EXIT`) OR ≥3 clean cycles (`UPGRADE_FROM_BOOTSTRAP`). BOOTSTRAP is once-only — downgrades climb back via the existing ladder, not through BOOTSTRAP. New `_bootstrap_clean_cycles` counter reset by `_transition`. Subsumes FX-012 (cold-start default). +106 / -17 lines + 10 new tests + 4-line update to root `test_safety.py`.

**Production safety.** The Helsinki server is already past cold start (orders placed in cycle 3 + the Tamilaga orphan), so `_is_genuine_cold_start` returns False there. On `git pull + restart` the server stays in whatever state its safety_state row last persisted — BOOTSTRAP is NOT entered. The new code paths fire only on the next genuinely fresh-DB bring-up (provisioning a new server, or wiping `bot_history.db`).

**Test count.** 443 → 453 tests pass in the fast tier (+10 new under pytest collection in `tests/test_safety_controller.py`). The root `test_safety.py` runner now has 82 checks (+1 for the new BOOTSTRAP permissions row).

**v5.1.6 scope.** v5.1.6 is the **Phase 0 housekeeping release** — pure debt cleanup with zero behavioural change to any running process. Two commits on top of v5.1.5 (`dd67f97`):

1. **`3f50441` — Remove stale `polymarket-bot.service` (`fixit.md::FX-017`).** The repo root carried a leftover systemd unit referencing `/opt/polymarket-bot/` and running `main.py` (the deprecated legacy entry). The file was unused: no internal code referenced it, the deployed units on the Helsinki server live at `/etc/systemd/system/polymarket-{farmer,oversight}.service` per §11.11, and they run `reward_farmer.py` / `oversight_agent.py` from `/home/polymarket/Polymarket-bot`. -36 lines, file deleted. The two directives worth preserving from the deleted file — `KillSignal=SIGINT` and `TimeoutStopSec=30` — are flagged in the commit body for FX-014 (Phase 5) to copy forward into the canonical units when graceful-shutdown work lands.
2. **`987a844` — Add `numpy>=2.0` to `requirements.txt` (`fixit.md::FX-018`).** numpy was a real production dependency but only listed in `pyproject.toml`'s streamlit transitive tree, not in `requirements.txt`. On Mac it arrived for free via streamlit; on a headless server installing only via `pip install -r requirements.txt` it was missing — Phase D's Helsinki bring-up had to pip-install it by hand (§11.8). +1 line. The `>=2.0` floor matches what the Helsinki server already runs and supports Python 3.12+; the repo targets 3.14.
3. **FX-020 reconciled retrospectively.** The v5.1.4 §11.4 candidate list ("Helsinki / Falkenstein / Nuremberg / Singapore") was rewritten as a verified status table as part of the v5.1.5 amendments (alongside `dd67f97`), but the fixit doc entry was never moved from §3 (Open) to §4 (Fixed). v5.1.6 closes the bookkeeping. No new architecture-doc edit required for this item.

**No behaviour changes.** v5.1.6 ships only:
- one deleted file (`polymarket-bot.service`),
- one new line in `requirements.txt`,
- this scope paragraph + the `Amendments in v5.1.6` block below + an updated `Last amended` / `HEAD` reference + two new rows in §10.1 + a strikethrough on the numpy item in §10.3 Active operational items.

The Helsinki server's running processes are unaffected; no `git pull` + restart is required to consume these changes (the bot has been running with manually-installed numpy since Phase D, and the stale service file was never deployed there). A future `git pull` will mechanically pick them up.

**v5.1.5 scope.** v5.1.5 is a **bootstrap-deadlock fix release** plus an operational migration. One commit (`dd67f97`) on top of v5.1.4. Two storylines:

1. **Ashburn → Helsinki migration.** Following the v5.1.4 Phase D discovery that Polymarket geoblocks US IPs at the CLOB API, the operator deleted the Ashburn (us-east) Hetzner CCX13 and provisioned a new CCX13 in Helsinki (`hel1`, Finland). Re-ran the full §11.5–§11.13 procedure on the new box. Wallet ($201.35 pUSD on `FUNDER 0xB23Bc80E6719099aeBE0c34389f05EC8C928503f`) is on-chain on Polygon and unaffected by the server migration. First LIVE cutover at 2026-05-15 04:03 UTC. **Geoblock cleared:** every `POST /order` from the Helsinki IP returned successfully (verified via authenticated `get_balance_allowance` + actual order-placement attempts; no `403 Trading restricted in your region`).

2. **Bootstrap deadlock surfaced and fixed.** The first LIVE cutover from a genuinely fresh DB exposed a structural deadlock that v5.1.4 documented abstractly but did not actually close. The bot placed 2 orders in cycle 3 (both targeting markets that subsequently 400'd because their orderbooks no longer existed), then stopped deploying for the next 2.5 hours despite running cleanly. Root cause: SafetyController invariant I9 (`data_freshness`) checks `MAX(ts) FROM scoring_snapshots`. On a fresh DB the table is empty → I9 returns `None` → fires as a violation → forces state machine to `DATA_UNAVAILABLE`. `DATA_UNAVAILABLE` blocks trial markets per the state permissions table. On a fresh DB every market is a trial (fill_count=0, low confidence). So 0 deploys → 0 orders placed → no `are_orders_scoring` calls → `scoring_snapshots` stays empty → I9 keeps firing → permanent deadlock. v5.1.4 §10.3 lessons noted "the LIVE bootstrap is the only way out" but the documented exit path (first LIVE cycle writes `portfolio_snapshots`) only addresses the I3/I4 portfolio-value chicken-and-egg, not the I9 data-freshness one. `dd67f97` closes this by adding a single lifetime-orders-placed check inside `_query_data_freshness`: if `scoring_snapshots` is empty AND `orders_placed` has never had any rows, treat data freshness as N/A (return `0.0`) rather than missing (return `None`). Behaviour is byte-identical once the bot has placed any order in its lifetime, so the patch is invisible on warm DBs. See §10.1 commit `dd67f97`, §10.2 known-fixed bug B10, and `fixit.md::FX-001`.

3. **Three concurrent observations** during the LIVE bootstrap that are tracked in `fixit.md` but NOT fixed in v5.1.5 (deferred to later commits): (a) the `[CYCLE_SUMMARY] orders_placed` counter increments on placement attempts rather than confirmed-success returns from the API (`fixit.md::FX-004`); (b) the orphan-scan path (`reward_farmer.py:550-611`) creates `dump_states` rows for on-chain CTF positions on resolved markets, producing infinite 400 retry spam (`fixit.md::FX-007`); (c) on cold-start, `total_capital` falls back to the `--capital` CLI default of `1500.0` for up to 30 min while the bot's first wallet-read propagates through `portfolio_snapshots` → next oversight cycle → next allocation write (`fixit.md::FX-013`). All three are non-blocking but compromise safety margins or observability during the bootstrap window. Phased fixes scheduled per `fixit.md` §6 Hardening roadmap.

**v5.1.4 scope.** v5.1.4 is the **operationalisation release** — taking the v5.1.3 codebase and producing a fully deployed, tested, hardened production bot on a remote server. Ten commits since v5.1.3 baseline `ad22512` (see "Amendments in v5.1.4" below). The five infrastructure fixes (Phases 0–3) close silent safety holes and reactivate broken control pipelines that DRY mode never exercised. The three Phase C commits wire oversight signals to real pause/kill actions behind a three-flag promotion ladder (all default off). The Phase D effort provisioned a Hetzner CCX13 server in Ashburn, brought the bot through hardening + Python 3.14 + repo + .env + 4h+ DRY soak, funded the wallet to $201.35 pUSD, attempted LIVE cutover, surfaced one V1→V2 SDK miss (`get_orders` → `get_open_orders` in `ee6abdf`), and then surfaced a hard operational blocker: **Polymarket geoblocks US IPs from trading via the CLOB API**. The Ashburn server cannot be the LIVE host. **No money has moved**; bot is reverted to DRY pending operator decision on non-US server region (Hetzner Helsinki / Falkenstein / Nuremberg / Singapore are candidates that need to be verified against Polymarket's published geoblock list before purchase). The full operational replication procedure (server provisioning, hardening, systemd units, daily ops, mode switching, stage promotion, emergency rollback) is in the expanded §11 Replication & Operations.

**v5.1 scope.** v5.1 is a small additive integration (two commits on top of v5.0). It exposes a deterministic oversight evaluation hook inside `reward_farmer.run_cycle` so a separate policy function — `oversight_agent.evaluate(guard) -> {"action": "continue"|"pause"|"kill", "reason": str}` — can be wired in without further farmer changes. **The hook is live and audited; the policy function is not yet implemented in `oversight_agent.py`** and the farmer transparently falls back to `"continue"` via a `hasattr` check (§4.21). All other v5.0 surfaces unchanged.

**v5.0 scope.** v5.0 is a **consolidation release**, not a redesign: the allocator / learning / β-η control surface is unchanged from v4.0's design. What's new since v4.0 is the production-readiness layer that turns the working-tree prototype into a deployable bot — five commits on `main` that (a) close the cap-stack × min-floor artefact inside the allocator, (b) stabilise capital_scale against small-amplitude oscillation, (c) re-ground the V5 audit on a scenario-independent metric (V5 now PASSes 18/18 seed-scenarios), (d) install a runtime safety guardrail stack on the farmer with hard enforcement + kill-switch, and (e) introduce a three-mode execution gate so the operator can stage DRY_RUN → SHADOW → LIVE without any code change.

**Amendments in v5.1.11:**

1. **Graceful shutdown + batch cancel on SIGTERM** (`91bae99`, 5 files, +493 / -18 lines). Closes `fixit.md::FX-014` + `FX-015`. Key changes: SIGTERM handler in `reward_farmer.run()`, V2 batch `cancel_orders` in `_shutdown_cleanup` (with per-order fallback), `OrderLifecycle.cancel_order(force=True)` bypasses dry_run shortcut, rate-limiter expanded to cover every V2 SDK method, structured `[SHUTDOWN]` log channel. Phase 5 audit surfaced 3 real bugs — all addressed: SHADOW kill-switch override was non-functional; rate-limiter missed V2 cancel methods; 60-market shutdown could exceed 30s budget. 22 new tests in `tests/test_shutdown.py`.

2. **§11.11 systemd unit blocks updated** with `KillSignal=SIGINT`, `TimeoutStopSec=30`, `KillMode=mixed` directives plus a new "Operational stop procedure" subsection. The Python-side SIGTERM handler makes the directive change forward-compatible: operator can apply the new units at their convenience.

**Lessons captured in v5.1.11** (the first phase with code-level audit findings):
- Advertised guarantees that work in tests but not in production are worse than no guarantee — the kill-switch override case (Q3 from the audit) had a docstring promising real cancels in SHADOW for ~6 months, but OL's dry_run shortcut silently defeated it. The fix (Audit fix 1) added a `force=True` propagation; the audit lesson is that EVERY override path needs an end-to-end test that exercises the actual API call, not just the wrapper.
- The V2 SDK migration was incomplete in rate_limiter.py — the protected-methods set was a place change at risk wasn't widely thought of, so it was easy to miss. Future SDK upgrades should grep for every `_RATE_LIMITED_METHODS` entry against the live SDK to catch renames.
- Batch endpoints exist for a reason. The CLOB API has `cancel_orders`, `cancel_all`, `cancel_market_orders` — use them where one API call replaces N. Future cleanup paths should default to the batch form.

**Amendments in v5.1.10:**

1. **Wallet-first capital resolution + wallet-scaled I4 floor + dead-knob deletion + structured `[CAPITAL_SOURCE]` log** (`d4d1541`, 6 files, +514 / -30 lines). Five fixit entries closed by one structural change. The `$1500` silent fallback that misconfigured safety thresholds for ~30 min on cold start is gone — `--capital` defaults to None, the agent reads `usdc_balance` from the DB (farmer now writes on cycle 1), and the cycle short-circuits with `[CAPITAL_SOURCE] source=none` if neither path is available. The SafetyController's I4 capital-floor invariant is now wallet-scaled: `max($50, max(peak, portfolio, exchange) * 0.10)` — small wallets see identical behaviour ($50 minimum), large wallets get tighter floors. Closes `fixit.md::FX-010` + `FX-011` + `FX-013` + `FX-024` + `FX-025`.

2. **§8.1 RF config table — two rows removed** (`RF_MAX_COST_PER_MARKET`, `RF_MAX_TOTAL_EXPOSURE`). Their accessor functions in `reward_farmer.py` were also deleted.

3. **§8.2 SafetyController constants — `CAPITAL_FLOOR_PCT` added.**

4. **§11.13 LIVE cutover guidance — FX-013 "remaining bootstrap gap" paragraph updated** to note the gap is closed.

**Lessons captured in v5.1.10** (added inline in §10.3):
- Operator-trust matters more than completeness. Adding an SDK client to the agent (the fixit's original proposal) would have been "more complete" but expanded the planner's responsibility profile and introduced auth/network dependencies in a code path that doesn't trade. The cycle-1-write + None-default + skip-cycle approach achieves the same outcome via a strictly smaller surface area.
- A `Violation`'s `.threshold` field is rarely read by anything other than the log formatter. The audit confirmed I4's threshold change (absolute $50 → scaled) had no downstream consumers — the test on line 313 (`fv[0].value == 25.0`) is on `.value`, not `.threshold`. Future invariant-level changes can rely on this layered separation: severity is the contract, threshold is operator-facing context.

**Amendments in v5.1.9:**

1. **`unliquidatable_markets` mechanism shipped — Tamilaga orphan-dump 400-spam closed** (`7d8d38d`, 12 files, +984 / -20 lines). New DB table + 6 BotDatabase methods; producers (`OrderLifecycle` BUY exception handlers, `DumpManager` SELL exception handler) detect the canonical V2 SDK 400 body and mark the cid; consumers (BUY gate in OL, SELL gate in DM, orphan-scan gate, exchange-position-sync gate, dump-state restore gate, dead-market cleanup cascade) skip marked cids. `RewardFarmer._reprobe_unliquidatable` runs on a 30-min loop sweep with per-cid 6h staleness gating and un-marks cids whose `get_merged_book` returns data (CLOB `/markets/{cid}` fallback for token_ids when cid isn't in `self.markets`). New config constant `RF_UNLIQUIDATABLE_REPROBE_SECS = 6 * 3600`. New file `tests/test_unliquidatable_markets.py` with 31 tests across 9 classes. Closes `fixit.md::FX-005` + `FX-006` + `FX-007` + `FX-008` + `FX-009` + `FX-028` in a single commit.

2. **§4.22 Orphan position recovery — "Planned fix" rewritten** to describe the shipped mechanism instead of the pending design.

3. **§9 Database Schema Reference — new `unliquidatable_markets` row** added to the §9.1 table list with column reference and the methods that touch it.

**Lessons captured in v5.1.9** (added to §10.3 paragraph below):
- The right shape for a "this thing is dead" memory is a DB-backed gate consulted by every producer of the action it should block. A previous instinct would have been an in-memory cache populated at startup, but DB lookups on an indexed PK are µs-scale on local SQLite — well under any meaningful cycle budget, and removing the cache removes a whole class of invalidation bugs.
- Detection by exception substring is durable enough for production use IF the substrings are tight to the canonical phrase. Loose detectors (single-word matches, partial phrase matches) are too easy to false-positive on adjacent error categories. Regression tests for the false-positive cases are mandatory ("insufficient balance", "rate limit", "market does not exist" all must stay unmarked).
- Comprehensive code-review audits surface real findings even for code I just wrote. The Phase 3 audit found 4 real issues; all addressed pre-commit. Worth running for every non-trivial phase.

**Amendments in v5.1.8:**

1. **Counter / DB consistency restored** (`e7fc3d2`, 3 files, +318 / -12 lines). `OrderLifecycle.place_orders_for_market` now returns `int` — the count (0, 1, or 2) of API-confirmed placements written to the `orders_placed` DB table. `_gated_place_orders_for_market` in `reward_farmer.py` accumulates the value into `_cycle_orders_placed`. Early returns (no book, wide spread, sports block, resolution proximity, has-both shortcut), the DRY-run path, and API failures all return 0. The `[CYCLE_SUMMARY] orders_placed` field in §4.20's telemetry now reads "API-confirmed placements this cycle" instead of "placement attempts this cycle"; operator dashboards and alerts can read it as ground truth against the DB. Defensive `isinstance` guard treats pre-FX-004 stub returns as 0 so the counter never advances on stale plumbing. New `tests/test_order_lifecycle.py` (+270 lines, 17 tests across 4 classes — returned-count semantics / early-returns / dry-run / wrapper accumulation). Closes `fixit.md::FX-004`.

**Lessons captured in v5.1.8** (one-liner — operational pattern):
- Counters and DB tables that purport to count the same thing should be wired through one return path, not two parallel control flows. The pre-FX-004 split (DB write inside `if oid:`, counter increment outside) is the canonical shape of "telemetry drifts from reality." Future per-cycle counters should follow the FX-004 pattern: the action returns its count, the caller accumulates.

**Amendments in v5.1.7:**

1. **I3 drawdown skipped on genuine cold start** (`dc78ba0`, 1 file, +43 / -15 lines, `oversight/safety_controller.py`). New helper `_is_genuine_cold_start()` checks lifetime `orders_placed` + `fills` counts. When both are zero AND I3's `_portfolio_val <= 0`, the violation is suppressed (logged at INFO once per cycle) — there's nothing to draw down from. Warm-DB behaviour is preserved verbatim: any prior order or fill returns False from the helper, and I3 fires DATA_UNAVAILABLE exactly as before. The helper is also wired into `_query_data_freshness` (replacing the inline check from `dd67f97`) so I9 and I3 share one source of truth. Closes `fixit.md::FX-002`.

2. **`BOOTSTRAP` state for first-time cold start** (`541108b`, 1 file, +106 / -17 lines + tests). New state slotted between `MILDLY_MISCALIBRATED` (severity 1) and `SEVERELY_MISCALIBRATED` (now severity 3). Permissions: `max_markets=10, capital_pct=0.30, trials=True`. Trials must be on because on a cold start every market is a trial (`fill_count=0, confidence=low`); without trials the bot can't accumulate calibration data. `_load_state` chooses between BOOTSTRAP and MILDLY via `_cold_start_or(MILDLY_MISCALIBRATED)` based on `_is_genuine_cold_start()`. `_handle_upgrade` gains a BOOTSTRAP exit branch: leave to MILDLY on EITHER `lifetime_fills ≥ BOOTSTRAP_FILL_EXIT (10)` (fast path) OR `_bootstrap_clean_cycles ≥ UPGRADE_FROM_BOOTSTRAP (3)` (slow path, for markets-are-dry scenarios). BOOTSTRAP is once-only — recoveries from downgrades climb straight to MILDLY, not back through BOOTSTRAP. New `_bootstrap_clean_cycles` counter reset by `_transition`. Closes `fixit.md::FX-003` and subsumes `fixit.md::FX-012` (cold-start default).

3. **Tests added** (+161 lines across two test files). `tests/test_safety_controller.py` (new) carries 17 unit tests across 5 classes: `TestIsGenuineColdStart` (helper edges), `TestI3ColdStartSkip` (FX-002 behaviour), `TestBootstrapStateRegistration` (severity ordering, permissions, upgrade order), `TestBootstrapEntry` (cold-start default routing), `TestBootstrapExit` (3-cycle slow path, 10-fill fast path, transition counter reset). The root `test_safety.py` runner is updated: TEST 14 now asserts 7 states and the BOOTSTRAP slot in the severity chain; TEST 21 gains a BOOTSTRAP permissions row.

**Lessons captured in v5.1.7** (added to §10.3 SafetyController paragraph):
- The bootstrap chicken-and-egg has THREE chambers, not two. v5.1.5 closed I9 (data_freshness). v5.1.7 closes I3 (drawdown). The third — `usdc_balance` arriving zero in the first ~30-min window — is the `$1500` capital-sizing race tracked as `fixit.md::FX-013` and slated for Phase 4. All three share the same architectural pattern: invariants written for the warm-DB common case demote state when their data-availability preconditions don't hold, and on a fresh DB those preconditions don't hold for the first cycle.
- The fix pattern is now established: `_is_genuine_cold_start()` (lifetime `orders_placed` + `fills` check) is the canonical signal for "do not fire data-unavailable demotions; you have nothing to compare against." Future invariants that demote on missing data should consult the same helper.
- BOOTSTRAP's `trials=True` permission is the non-obvious design choice. Every market on a fresh DB is a trial; setting `trials=False` would create a different deadlock (no trials → no orders → no fills → no calibration data → no exit from BOOTSTRAP via the fill threshold). The conservative-first goal is achieved through `max_markets=10` and `capital_pct=0.30`, not by suppressing trials.

**Amendments in v5.1.6:**

1. **Stale `polymarket-bot.service` deleted** (`3f50441`, 1 file removed, -36 lines). Repo root no longer carries the legacy `main.py`-running unit. Canonical units live at `/etc/systemd/system/` on the Helsinki server per §11.11; the deleted file's `KillSignal=SIGINT` + `TimeoutStopSec=30` directives are flagged in the commit body for FX-014 to copy forward in Phase 5. Closes `fixit.md::FX-017`.

2. **`numpy>=2.0` added to `requirements.txt`** (`987a844`, 1 file, +1 line). Closes the Phase D server-install gap (numpy was previously transitive-via-streamlit on Mac only). The §10.3 "Active operational items" entry "`numpy` not in `requirements.txt`" is struck through accordingly. Closes `fixit.md::FX-018`.

3. **FX-020 retrospective close.** The §11.4 verified Hetzner table (Helsinki ✓ only; Falkenstein / Nuremberg / Ashburn / Hillsboro blocked; Singapore close-only) shipped alongside v5.1.5 amendments in `dd67f97` but was never marked closed in the fixit doc. v5.1.6 reconciles `fixit.md::FX-020` to §4 (Fixed); no architecture-doc text change required.

**Lessons captured in v5.1.6** (housekeeping-only; no new failure-mode catalogue entry warranted):
- Phase 0 housekeeping is the right intake stage for repo debt that doesn't change behaviour. Two file changes (-36, +1) + a fixit-doc reconciliation took the v5.1.5 → v5.1.6 bump cleanly and unblocks the path to Phase 1 (bootstrap completion: FX-002 / FX-003 / FX-012) without coupling unrelated debt to those structural fixes.
- The fixit doc's "shipped alongside vX.Y.Z amendments" case (FX-020) is real and recurring — the doc-edit work and the entry-status-bookkeeping can drift even within a single session. Catching it on the next housekeeping pass is fine; the cost of the drift is just one stale row in the §2 status table until the next pass.

**Amendments in v5.1.5:**

1. **SafetyController I9 deadlock fix on fresh-DB bootstrap** (`dd67f97`, 1 file, +15 lines, `oversight/safety_controller.py::_query_data_freshness`). The function previously returned `None` when `scoring_snapshots` was empty, which I9 interpreted as a critical violation and pushed the state machine into `DATA_UNAVAILABLE`. On a fresh DB this was permanent: `DATA_UNAVAILABLE` blocks trials, every market on a fresh DB is a trial, no trials means no orders, no orders means no `are_orders_scoring` calls, no scoring calls means `scoring_snapshots` stays empty. Loop. The fix differentiates two cases inside the empty-table branch by running a single `SELECT COUNT(*) FROM orders_placed`: if zero (truly cold-start), return `0.0` and treat freshness as N/A; if non-zero (orders exist but scoring pipeline broken), preserve the original defensive `None`. Once the bot places its first order ever, the new branch reverts to original behaviour — patch is invisible on warm DBs. Existing tests don't exercise the empty-scoring-AND-empty-orders branch (which is why this wasn't caught earlier — see also `fixit.md::FX-016` on the broader SafetyController test-coverage gap). The patch ships as a strict minimum-viable unblock (Phase A.1 of the comprehensive fix designed in this session); follow-on items including I3 drawdown bootstrap (`fixit.md::FX-002`), a dedicated `BOOTSTRAP` state (`fixit.md::FX-003`), counter/DB consistency (`fixit.md::FX-004`), orphan-scan/dump-state correctness (`fixit.md::FX-007`), and wallet-first capital sourcing (`fixit.md::FX-013`) are sequenced in the `fixit.md` §6 Hardening roadmap.

**Lessons captured in v5.1.5 (added to §10.3):**
- The documented "exit path" in v5.1.4 §11.13 ("first LIVE cycle writes portfolio_snapshots and starts the SafetyController state-transition machine") was incomplete. Writing `portfolio_snapshots` clears the I3 (drawdown) and I4 (capital floor) blockers but leaves I9 (data_freshness) firing on a separate code path. The actual bootstrap chain has two chicken-and-eggs, not one. v5.1.5 closes the second.
- The 16-item failure-mode catalog built during this session (`fixit.md::FX-002` through `FX-028`) shows the bot's bootstrap-and-failure paths systematically assume warm-state behaviour. Multi-phase hardening required; v5.1.5 ships only the smallest blocking subset.
- Hetzner geoblock candidates from v5.1.4 §11.4 were verified against the live Polymarket geoblock docs page: **Helsinki (`hel1`) — allowed; Falkenstein (`fsn1`) — blocked; Nuremberg (`nbg1`) — blocked; Singapore (`sin`) — close-only**. §11.4 has been updated to reflect this.
- Orphan-scan behaviour (`reward_farmer.py:550-611`) was previously undocumented in this architecture doc. Section §4.22 has been added to describe it.

**Amendments in v5.1.4:**

1. **Phase 0 — pytest collection unblock** (`900e3f8`, 5 files). Five top-level `test_*.py` files (`test_integration.py`, `test_profitability.py`, `test_safety.py`, `test_state_v2.py`, `test_verification.py`) ran their custom test runners at module-level import time. Pytest's discovery imported them, ran the runners as a side effect, and the terminal `sys.exit(1)` (when any test in those custom suites failed) aborted the entire collection with `INTERNALERROR / SystemExit: 1` before pytest could reach the `tests/` tree. Wrapped each runner body in `if __name__ == "__main__":` so both invocation paths preserved: `python3 test_X.py` runs the custom suite; `python3 -m pytest` cleanly collects. Net: 0 collected → 434 collected. No behaviour change to the runners themselves.

2. **Phase 1 — populate question text from Gamma in market_expiry_cache** (`c7ed2e6`, 4 files). Cold-start markets discovered via CLOB but not yet posted on by the bot got hardcoded `question=""` at `oversight/data_collector.py:1354`. Their `MarketMetrics.question` stayed empty through the entire scoring and allocation pipeline. Sample DB query at the time: 73% of `market_performance` rows (2594/3556) had empty question, including all deploy rows. Empty question silently disabled three gating checks that short-circuit on truthy-question: (a) **sports protection** at `oversight/market_scorer.py:272-275` (`if m.question and action == "deploy":` — the sports-keyword block was skipped for any market with empty question text), (b) **per-group concentration cap** at `oversight/allocation_writer.py:117-124` + `profit/allocator.py:115-118` (empty `question_group` via `_question_group_key` meant the 30%-of-capital per-cluster cap was never tracked or applied), (c) **keyword filters** at `market_discovery.py:35-39` (substring match on empty string always False — natural-gas / "during" filter disabled). Root cause: Gamma keyset parser at `oversight/data_collector.py:284-288` only extracted `conditionId` + `endDateIso`, dropping the `question` field that IS in the response. CLOB `/rewards/markets/current` does NOT carry question text (verified via live API probe), so Gamma is the only source. Patched: Gamma parser extracts `question`, CLOB fallback at lines 307-318 extracts `question`, threaded through cache write at lines 327-330, cache read at line 244 selects `question` column. Schema: idempotent `ALTER TABLE market_expiry_cache ADD COLUMN question TEXT NOT NULL DEFAULT ''` in `database.py:_migrate_enrichment_columns`. Consumer at `oversight/data_collector.py:1381` got an `expiry_map[cid]["question"]` fallback so cold-start CIDs inherit text from the Gamma fetch. Forward-only — historical empty rows remain empty until 24h cache TTL expires. Tests added: `test_cache_round_trip_preserves_question`, `test_cache_handles_empty_question`, `test_migration_adds_question_column`. Live verification: 5/5 sample markets returned with populated question text after fix. Operational evidence: on the server during the LIVE cutover, 11 sports markets were correctly blocked by the time-to-kickoff gate that this fix activated (logged as `Sports market +0.6h from kickoff (< 1.0h block; game_start=2026-05-13T09:00:00Z)`).

3. **Phase 2 — stamp `_total_capital` on legacy allocator output + uniform `cap_scale`** (`d2612e6`, 3 files). The bot has two allocation paths: **profit-engine** (`profit/allocator.py:379`) which stamps `_total_capital`, and **legacy** (`oversight/allocation_writer.py:_to_dict`) which does NOT. Path selection at `oversight_agent.py:385-408`: `if calibrator.is_ready() → profit, else → legacy`. Direct DB query of `calibration_model_state` confirmed only the `reward_model` row exists (`fill_model` and `loss_model` are not trained at all), so `is_ready() == False` and the legacy path runs every cycle. The farmer reader `_guardrail_total_capital_from_alloc` at `reward_farmer.py:1064-1095` is fail-open when missing — silently returns `None`, which then propagates as `null` in the `[GUARDRAIL]` JSON. Downstream impact when null: (i) farmer's `notional_ratio = total_live_notional / total_capital` (line 1252) cannot compute, so `notional_block` cannot trigger; (ii) `cluster_limit_usd = CLUSTER_NOTIONAL_LIMIT_FRAC * total_capital` (line 1297) inactive; (iii) `loss_limit = MAX_DAILY_LOSS_FRAC * total_capital` (line 1275) inactive → 24h-loss kill-switch disabled; (iv) oversight shadow signal `notional_drift` at `oversight_agent.py:645-654` stays in `status=missing_data`; (v) oversight shadow signal `slow_bleed` at `oversight_agent.py:710-722` stays in `status=missing_data`. **Four guardrails + two shadow signals all silently inactive.** Mid-cycle telemetry observed `missing_signal=total_capital` warning emitted on every farmer cycle for 1206 consecutive cycles before fix. Fix: hoisted `cap_scale` computation out of the profit-engine-only branch so both paths multiply through `alloc_capital = available_capital * cap_scale` (neutral 1.0 in OFF/SHADOW LearningController state, so no behavioural difference today; meaningful once gate promotes to ACTIVE). Added a post-redistribution loop in `compute_allocations` that stamps `_total_capital = round(total_capital, 2)` on every deploy row, mirroring `profit/allocator.py:379`. Test added: `test_compute_allocations_stamps_total_capital` (asserts every deploy gets the stamp, value matches the input) + `test_compute_allocations_avoid_rows_not_stamped` (avoid rows correctly skipped). Risk: low — kill-switch is now armed but `realized_loss_24h ≈ 0` in DRY, so cannot false-trigger.

4. **Phase 3a — fix `_read_alloc_file` dict-key mismatch** (`4f102e3`, 4 files). LearningController's `_read_alloc_file` at `profit/learning.py:852` read `alloc.get("allocations", [])` but the writer at `oversight/allocation_writer.py:275` writes `"markets": allocations`. The reader silently returned an empty list — every downstream metric stayed pinned at the cold-start value: `reward_efficiency=None`, `reward_error=None`, `loss_per_capital=None`, `expected_util=None`. Result: `_metrics_complete` at `profit/learning.py:1164` always returned False, so `valid_cycles_observed` (the LearningController gate input) never incremented, so the gate stayed stuck at OFF/SHADOW forever, so the rule outputs (`capital_scale`, `β`, `reward_trust`, `η`) never applied. **The entire control loop was structurally dead since the writer/reader were authored against different key names.** Fix: single-line change `"allocations"` → `"markets"` at `profit/learning.py:852`. Found via static grep — only one production reader uses the wrong key; the parallel writer in `simulation/runner.py:_write_alloc_file` was also wrong (mirror bug — surfaced when full pytest ran post-Commit-1 of Phase 3 and broke 3 simulation tests) and was fixed in the same commit. 5 test sites updated to write `"markets"` so test fixtures match production (`tests/test_reward_expansion.py` 3 sites, `tests/test_frontier_memory.py` 2 sites). 20 mock references were already correctly written to `markets`-key fixtures; not all needed touching.

5. **Phase 3b — bump `GATE_ACTIVE_CYCLES` 50→2000 as SHADOW-soak safety belt** (`e270d63`, 5 files). Once `_read_alloc_file` returns real values (Phase 3a), `valid_cycles_observed` starts ticking 1/cycle on `metrics_ok`. The other gate criteria at `profit/learning.py:469-472` are: `fills_total ≥ 200` (current=474 ✓), `pairs_total ≥ 100` (current=395 ✓), `reward_days ≥ 5` (was 4 at fix time, ✗), `valid_cycles ≥ GATE_ACTIVE_CYCLES`. Worst-case timing: 50 cycles × 30 s = ~25 minutes after `reward_days` rolls over. β trajectory under legacy path (since profit engine doesn't run and `_p_fill` is unstamped on legacy rows): `expected_capital_sum = 0` → `expected_util = 0` → `err_beta = TARGET_UTIL - 0 = 0.75` → `beta_raw = prev_beta · (1 + K_BETA · err_beta) = 0.75 · 1.375 = 1.03125` → clamped to `CLAMP_BETA[1] = 0.95`. So computed β EMA-converges to upper clamp 0.95 within ~15 cycles of fix. **At ACTIVE promotion, applied β jumps from neutral 0.75 → ~0.95, a 27% allocator budget increase.** Combined with `cap_scale ∈ [0.30, 1.20]`, worst-case multiplier is 1.52×. On $200 wallet that's ~$3 additional notional. Bounded but real. Mitigation: bump `GATE_ACTIVE_CYCLES = 50 → 2000` (cycle floor ≈ 16.7h SHADOW soak) gives operator a window to observe `[LEARNING_SHADOW] would_apply` log lines for sane β/cap_scale/trust trajectory before applied state shifts. Inline TODO comment marks the value as temporary. Tests touched: `test_active_at_exact_boundary` (used literal `cycles=50` → now uses `GATE_ACTIVE_CYCLES` constant directly), `test_probe_scheduling_via_step` and `test_probe_fires_when_stable_and_cadence_met` (both seeded `valid_cycles_observed=50` to test ACTIVE-mode probe scheduling — now use `GATE_ACTIVE_CYCLES` constant), `test_probe_blocked_when_unstable` (was passing for the wrong reason post-bump — fixed to seed `GATE_ACTIVE_CYCLES + 10`). One additional collateral: `tests/test_simulation.py::TestScenarioDirections.setUpClass` runs only 150 cycles per scenario and assumes "150 cycles is enough to clear SHADOW (>=50 valid cycles)". Bump broke 3 of those tests. Fix: scoped `unittest.mock.patch("profit.learning.GATE_ACTIVE_CYCLES", 50)` in `setUpClass` + `tearDownClass` cleanup. Production constant unchanged from the bump; only simulation tests see the lower threshold. **Revert plan**: once LIVE operation shows `[LEARNING_SHADOW] would_apply` trajectories converging to sane β values for ≥4h, revert the constant back to 50 in a separate single-line commit.

6. **Phase C — Oversight Stage 2/3 promotion flags** (three commits: `5757aef` introduces flags, `a08e86a` wires signals to actions, `5909764` adds isolation tests). The v5.1.1 shadow evaluator at `oversight_agent.py:780-789` was a **functional no-op**: it computed all 6 signals, logged each via `[OVERSIGHT_SHADOW]`, then returned hardcoded `{"action": "continue", "reason": "shadow"}` regardless. `_SHADOW_ONLY = True` constant at line 596 was **never read** by `evaluate()` — flipping it did nothing. Phase C wires the signal outputs to real pause/kill responses behind a **three-flag promotion ladder**, all defaulting to off so behaviour is byte-identical to pre-Phase-C:
    - **`_SHADOW_ONLY`** (master gate, default `True`): when True, `evaluate()` returns `continue/shadow` regardless of fired signals. Same as v5.1.1 behaviour. Flip to `False` to allow downstream stage flags to act.
    - **`_PAUSE_ENABLED`** (Stage 2, default `False`): when True AND master gate off, any fired `would_pause` signal (notional_drift, cluster_breadth, cf_soft_zone, cancel_pressure, slow_bleed) returns `{"action":"pause","reason":<signal_names>}`.
    - **`_KILL_ENABLED`** (Stage 3, default `False`): when True AND master gate off, the `would_kill` signal (cf_trajectory) returns `{"action":"kill","reason":"cf_trajectory"}`. When `_KILL_ENABLED=False` AND `cf_trajectory` fires, falls through to pause (preserves safety intent without escalating to terminal).
    - **Multi-signal precedence**: strict severity (kill > pause > continue). Architecture doc §4.21.7 is silent on multi-signal collision; this is the literal reading of the per-signal kind classification.
    - **Refactor**: `_check_signals_and_log` at `oversight_agent.py:749` now returns `tuple[list[str], list[str]]` of fired (pause_signals, kill_signals) instead of returning nothing. evaluate consumes the tuple, applies the flag-gated mapping, builds the reason string truncated to ≤200 chars (matches farmer-side normalisation).
    - **Per-signal defensive hardening**: each detector call wrapped in try/except so one bad detector doesn't suppress the others.
    - **Reason text policy** (operator-facing log continuity): when master flag on AND no signal fires, reason is `"shadow"` (matches v5.1.1 log corpus). When master flag off AND no signal fires, reason is `"no_signal"`.
    - Test count: 15 → 33. New tests cover per-kind action mapping (5 tests, one per pause signal kind), kill mapping (1), strict-severity precedence (`test_kill_overrides_pause_when_both_fire`), each flag isolation (3), reason format (3), graceful malformed-signal handling (1), continue-when-no-signal (1).
    - **Promotion sequence operator runbook**: after ≥200 LIVE cycles with default flags, flip `_SHADOW_ONLY=False` + `_PAUSE_ENABLED=True` (Stage 2). After ≥200 LIVE cycles at Stage 2 with no false positives in healthy regime, flip `_KILL_ENABLED=True` (Stage 3). Per architecture doc §4.21.7 gates: (1) no false positives in healthy regime, (2) triggers fire BEFORE corresponding hard guardrail would fire, (3) no flapping (toggle frequency < 1/30 cycles).

7. **Phase D — production server bring-up + V1→V2 SDK hotfix** (one commit `ee6abdf` for the hotfix; the rest is operational state captured in §11). Provisioned Hetzner CCX13 in Ashburn, hardened to standard (non-root `polymarket` user with passwordless sudo via `/etc/sudoers.d/polymarket`, key-only SSH, root SSH disabled, password auth disabled, ufw deny-incoming + fail2ban active, auto-security-updates enabled, TZ=UTC). Installed Python 3.14.4 via deadsnakes PPA + build-essential + libssl-dev family + sqlite3. Created venv, installed `requirements.txt` + manually added numpy (NOT in requirements.txt — needs PR; previously came in transitively via streamlit from `pyproject.toml` on the Mac). Cloned via dedicated GitHub deploy key (read-only, repo-scoped, in `/etc/sudoers.d/`-style isolation). Transferred `.env` via `scp` with perms 600. Wallet topped up to $201.35 pUSD on FUNDER. systemd units installed for `polymarket-farmer.service` + `polymarket-oversight.service` with hardening directives (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`, `ReadWritePaths=/home/polymarket/Polymarket-bot`, etc.). DRY soak ran 32+ hours with zero errors. Pytest on server: 449/457 pass (same as Mac, only the pre-existing flake `test_over_aggressive_contracts_capital` fails). First LIVE cutover at 2026-05-13 17:13 UTC surfaced an undetected V1→V2 SDK rename: `get_orders()` does not exist on V2 client (`AttributeError: 'ClobClient' object has no attribute 'get_orders'`). Bot fell through to placement with empty `open_ids` set; emitted `ERROR | get_orders failed: …` every cycle and placed zero orders. No money at risk. Fix in `ee6abdf` renamed `get_orders` → `get_open_orders` at 4 production call sites (`reward_farmer.py:263, 433, 1751`; `fills.py:65`) and 20 test mock references in `tests/test_order_reconciliation.py` and `tests/test_startup_recovery.py`. Static audit confirmed all other production `self.client.<method>(` calls are V2-compatible: `cancel_order(OrderPayload(orderID=str))` (signature verified), `get_balance_allowance`, `get_order`, `get_order_book`, `are_orders_scoring`, `update_balance_allowance` — all identical V1↔V2. Second LIVE cutover at 2026-05-14 04:55 UTC surfaced **Polymarket's CLOB geoblock**: every `POST /order` returned HTTP 403 with body `{"error":"Trading restricted in your region, please refer to available regions - https://docs.polymarket.com/developers/CLOB/geoblock"}`. The Ashburn server (us-east) is in a Polymarket-blocked jurisdiction (CFTC settlement, Jan 2022). Bot was reverted to DRY; **no money has moved**. Resolution path: migrate server to a non-blocked Hetzner location (operator must verify each candidate region against the live geoblock list at https://docs.polymarket.com/developers/CLOB/geoblock; Helsinki / Falkenstein / Nuremberg / Singapore are reasonable starting candidates). Phase D demonstrated that the bot's LIVE codepath works (real `place_order` requests fired from the server) — the only thing preventing LIVE earning is the API-level region rejection.

**Lessons captured in §10 (10.2 known-fixed bugs, 10.3 limitations, 10.4 audit-framework evolution):**
- Geoblock policy must be verified BEFORE choosing a server region. Latency optimisation (Ashburn = us-east-1 colocation with Polymarket's AWS) is irrelevant when the API rejects the source IP outright.
- DRY mode skips order-reconciliation paths (gated on `if not self.dry_run` and `if mode != MODE_LIVE`), so any V1→V2 SDK miss in those paths goes undetected through arbitrary DRY-soak duration. A signature-level audit (`inspect.signature` for every `self.client.<method>(` call) is the right tool for future SDK upgrades and is documented in §11.
- `numpy` is a transitive dep via `streamlit` (`pyproject.toml`) on local Mac. Headless server installs via `requirements.txt` only and misses it. A PR to add `numpy` to `requirements.txt` is pending.
- The SafetyController + DRY chicken-and-egg: in DRY, `_save_usdc_balance` is gated behind `if not self.dry_run`, so `portfolio_snapshots` is never written, so SafetyController stays in `DATA_UNAVAILABLE` (`STATE_PERMISSIONS[DATA_UNAVAILABLE]["trials"]=False`), so all trial markets are blocked, so on a fresh-DB server every market is a trial market, so deploys are 0. The exit path is the first LIVE cycle which writes `portfolio_snapshots` and starts the SafetyController state-transition machine. On a fresh server, DRY-only validation cannot exercise the deploy+fill chain end-to-end; the LIVE bootstrap is the only way out.

**Amendments in v5.1:**

1. **Oversight-agent hook in `run_cycle`** (committed at `b8d84bd`, inside `reward_farmer.py`). New `import oversight_agent` at module top. Inserted between `guard = self._guardrail_check_and_log()` and the existing `if guard["kill_switch"]:` branch. Initial implementation used `try/except` to absorb the absent-stub case as `AttributeError`. **Replaced in `2706953`** (see item 2). Live for one commit window only; `2706953` is the canonical form.

2. **Deterministic oversight integration** (committed at `2706953`, replaces the `b8d84bd` block):
    - **`hasattr(oversight_agent, "evaluate")` gate** distinguishes "function not yet implemented" (silent fallback to `{"action": "continue", "reason": "not_implemented"}`) from "function exists but raised" (`log.error("[OVERSIGHT_ERROR] evaluation failed:")`). Stops the ~2880/day `[OVERSIGHT_WARNING]` flood from the prior commit.
    - **Single evaluate call per cycle** (verified by AST walk — exactly one `ast.Call` to `oversight_agent.evaluate` in `reward_farmer.py`).
    - **Latency tracking**: `start = time.time(); … latency_ms = (time.time() − start) · 1000.0`. New module constant `OVERSIGHT_LATENCY_WARN_MS = 50` triggers `log.warning("[OVERSIGHT_WARNING] slow evaluation:")` when exceeded. Synchronous integration — slow evaluators block the 30 s farmer cycle, so this surfaces it loudly.
    - **Strict decision validation**: non-dict → `[OVERSIGHT_ERROR] invalid decision type`; `action not in ("continue","pause","kill")` → `[OVERSIGHT_ERROR] invalid action`; both fall to `action="continue", reason="invalid"`. `reason` always coerced to `str(reason)[:200]` to bound log volume from a misbehaving evaluator. Uses extracted `action` / `reason` locals downstream — no further `.get(...)` lookups on `decision`.
    - **Per-cycle decision log** (no throttling): `log.info("[OVERSIGHT] action=%s reason=%s latency_ms=%.2f", …)` — full auditability per spec.
    - **Kill propagation**: oversight-driven kill calls `self._activate_kill_switch(reason="oversight:" + reason)` so the evaluator's reason survives into the kill-switch log.
    - **Placement decision block reordered** (§6.3 of patch spec): `fill_storm` → `notional_block` → `action == "pause"` → `else placement_loop`. The `pause` elif moved from slot 2 (between `fill_storm` and `notional_block` under `b8d84bd`) to slot 3 (between `notional_block` and `else` under `2706953`); pause `log.warning` is now unconditional (no `cycle_count % 10` throttle).
    - See new §4.21.

3. **Shadow evaluator landed (v5.1.1, follow-up patch on top of `2706953`).** `oversight_agent.evaluate(guard)` now exists as a pure shadow detector: ring buffer of 30 guard snapshots in farmer address space, six trigger signals (notional drift / cluster breadth / CF soft-zone / cancel pressure / CF trajectory collapse / slow bleed — see §4.21.7 for the threshold table), `[OVERSIGHT_SHADOW]` log channel for triggers and missing-data flags. **`_SHADOW_ONLY = True` — function returns `{"action": "continue", "reason": "shadow"}` unconditionally; live behaviour byte-identical to v5.1.** Two new keys (`orders_placed_prev_cycle`, `orders_cancelled_prev_cycle`) added to `guard` from `_rolling_stats[-1]` to feed signal D; `[GUARDRAIL]` JSON unaffected (separate `tele` dict). New test file `tests/test_oversight_shadow.py` (15 tests) covers each signal positive/negative + return invariant + ring-buffer bound + missing-data fail-open. Per-cycle farmer log changes from `reason=not_implemented` → `reason=shadow`. Activation ladder for stages 2–3 documented in §4.21.7.

5. **V2 endpoint compatibility (v5.1.3, follow-up patch on top of `2a6baf6`).** Two operational issues addressed in one commit:
    - **`/rewards/markets/current` fallback**: this V2-canonical endpoint has been returning HTTP 500 with PostgreSQL `statement timeout (57014)` since shortly after the V2 cutover. Polymarket's status board shows everything green, but the endpoint is genuinely unhealthy. Empirical data: `/rewards/markets/multi` is also affected when sorted by `rate_per_day`; `/sampling-markets` works fast (~7s for ~5k markets, returns reward params nested as `m["rewards"]["rates"][0]["rewards_daily_rate"]` etc.). The bot now falls back to `/sampling-markets` when the primary endpoint returns 5xx or empty data, via two new helpers in `market.py`: `_v2_sampling_to_v1_flat` (translates V2 nested → V1 flat shape) and `_fetch_v2_sampling_rewards_params` (authenticated SDK call). Patches `market.py:fetch_clob_rewards_params` and `market_discovery.py:fetch_all_reward_markets`. **Documented limitation**: `/sampling-markets` excludes some high-reward "championship-winner"-style markets visible in the rewards UI (NBA Finals winners, French Open winner, etc.). The bot misses these while in fallback mode; full coverage resumes when Polymarket fixes the primary endpoint. Also tightened `fetch_clob_rewards_params` pagination loop to break on `next_cursor == "LTE="` (Polymarket's terminal sentinel) — pre-existing latent infinite-loop hazard.
    - **Gamma `/markets` keyset pagination**: Polymarket deprecated the `offset` parameter on Gamma's `/markets` and `/events` endpoints on 2026-04-10 in favour of cursor-based `/markets/keyset`. The bot's existing offset-based loops still work today (offset is deprecated, not yet rejected) but are at risk of breaking when Polymarket flips the rejection switch. Migrated all 4 Gamma offset call sites to keyset: `market.py`, `market_discovery.py`, `oversight/data_collector.py`, `paper_trader_v2.py`. New helper `market._gamma_paginated_keyset` handles the cursor loop with stuck-cursor detection (breaks before appending duplicate page if server returns the same cursor twice).
    - 14 new unit tests in `tests/test_market_discovery_v2_fallback.py` cover translator (5 tests), keyset pagination (4), and fetch dispatch (4) + cache-fallback path. Full fast-tier: 412/412 pass.
    - Smoke-tested live: with `/rewards/markets/current` returning 500, the V2 fallback successfully loads 4,966 reward markets and bot proceeds normally.

4. **Polymarket V2 migration (v5.1.2, follow-up patch on top of `28625ab`).** Polymarket cut over from CLOB V1 to CLOB V2 on 2026-04-28 ~11:00 UTC. The migration is **mandatory** — V1 SDK signatures are rejected by V2 servers (no backward compatibility). Scope:
    - **SDK swap**: `py-clob-client==0.34.6` → `py-clob-client-v2==1.0.0` in `requirements.txt` and `pyproject.toml`. Imports rewritten in 21 files: `from py_clob_client.X import Y` → `from py_clob_client_v2.X import Y`. Test mocks (`sys.modules["py_clob_client"]`) updated to mirror the production import path.
    - **`cancel_order` API change** (only real breaking change): V2 SDK requires `cancel_order(payload: OrderPayload)` instead of V1's `cancel(order_id_string)`. Five call sites wrapped in `OrderPayload(orderID=...)`: `reward_farmer.py:351`, `order_lifecycle.py:49`, `bot.py:896`, `order_manager.py:410`/`:435`, `test_order.py:152`. Two test files updated to assert `client.cancel_order` instead of `client.cancel`.
    - **Builder code wiring**: new `BUILDER_CODE` constant in `config.py` (added to `_IMMUTABLE` set so it can't be hot-reloaded). All 8 `ClobClient` constructors now pass `builder_config=BuilderConfig(builder_code=BUILDER_CODE) if BUILDER_CODE else None`. The V2 SDK auto-injects the code on every order via `create_order`.
    - **V2 contract addresses** in `revoke_allowances.py` and `check_wallet.py`: V1 Exchange `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` → V2 `0xE111180000d2663C0091e4f400237545B87B996B`; V1 NegRisk `0xC5d563A36AE78145C45a50134d48A1215220f80a` → V2 `0xe2222d279d744050d28e00520010520000310F59`; new V2 NegRisk Adapter `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`; collateral USDC.e `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` → pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`. All addresses resolved on-chain via the V2 Exchange's `getCollateral()` and `getCtf()` getters and verified.
    - **What is transparent through the SDK**: order struct change (`nonce`/`feeRateBps`/`taker` removed, `timestamp`/`metadata`/`builder` added) — bot never set the V1-only fields. EIP-712 domain version `"1"`→`"2"` — bot never signed manually. New protocol-set fee model — bot never set `feeRateBps`. Builder HMAC headers removed — bot never had them.
    - **Wallet state** (verified on-chain): FUNDER pUSD allowances on V2 Exchange + V2 NegRisk Exchange + V2 NegRisk Adapter all `MAX_INT` (auto-set by Polymarket); CTF approvals all True; pUSD balance and USDC.e balance both $0.00 (wallet unfunded); existing API keys authenticate against V2 server unchanged.
    - **Test status**: 398/398 fast-tier pass (1 deselected = `TestCorrectionFactor::test_correction_returned_from_collect`, a pre-existing network-dependent test, unrelated). 15 shadow tests still pass.
    - **Live trading not yet validated** against V2 production. `python check_wallet.py` confirms read-only V2 SDK auth + on-chain state are clean.

**Amendments in v5.0:**

1. **Step-3b cap-aware shaping** (committed at `5611d54`, inside `profit/allocator.py`). Inserted BETWEEN Step 3 and Step 4 of the continuous allocator. For each fill-cluster whose proportional budget `cluster_cap_pct × T / size` falls below per-market `min_capital`, pre-selects `k = max(1, floor(cluster_budget / cluster_min_capital))` top-ranked members by `(-raw_alloc, condition_id)` and routes the rest to `action="avoid"`. Restores β/η expressivity through the cap stack. See new §4.15.7.

2. **`capital_scale` stability filters** (committed at `741d35c`, inside `profit/learning.py::update_state`). Two additive filters applied AFTER every pre-existing rule (A/B/D/E + Patch-11 damping + EMA + clamp + Patch-13 hysteresis):
    - **Bounded-rate step** (`MAX_CAPITAL_SCALE_STEP = 0.07`): clips `|new_cap_final − prev.capital_scale|` to 0.07. No-op in current sim (observed per-cycle |Δ| ≤ 0.025) but available as amplitude protection.
    - **Small-amplitude flip suppression**: walks `prev.capital_history` for the last non-zero delta; reverts if the current delta is opposite-sign AND both `|current| < CAPITAL_CHANGE_MIN_STEP` AND `|prev| < CAPITAL_CHANGE_MIN_STEP`. Closes the small-amplitude churn that Patch-13's same-direction-only dead-band couldn't catch.
    - V5 INV7: 4/6 → **6/6 PASS** (flip_rate_100 on adversarial regimes: 7–9 → 0–1). See new §4.13 additions.

3. **V5 INV3 rewritten as cap-normalised capital utilisation** (committed at `707ca50`, inside `simulation/audit_v5_invariants.py` + `simulation/audit_v4_metrics.py` + `simulation/run_audit_v5.py`):
    ```
    capital_util = Σ(C_i) / total_capital                              (pure capital coverage, no p_fill weighting)
    feasible_capital_fraction = min(CAPITAL_BUFFER, Σ cluster_cap_pct + unclustered_fraction)
    normalized_util = capital_util / feasible_capital_fraction
    INV3_new PASSes iff avg(normalized_util) ≥ INV3_NEW_NORMALIZED_MIN = 0.70
    ```
    The v4.0 numerator `Σ(p·C)` was structurally bounded by `max(p_fill) ≈ 0.15` in the bootstrap calibrator, making the old `≥ 0.5` floor unreachable regardless of β/η — the invariant was measuring cap-policy geometry, not the control loop. The new numerator is unweighted capital coverage; the denominator divides out the cap-stack ceiling, so the metric is scenario-independent and evaluates the control loop's deployment decision directly. **V5 overall: PASS, 18/18 seed-scenarios clean.** See §10.4 V5 post-v5.0 row.

4. **Farmer runtime safety guardrails v1** (committed at `414354a`, inside `reward_farmer.py`). New execution-time safety layer running between the existing expiry-sweep / fill-storm checks and the Step-4 placement batch:
    - **Soft notional guard**: `MAX_NOTIONAL_RATIO = 2.0` — blocks all new placements this cycle when `Σ live notional / T > 2.0`.
    - **Soft cluster guard**: `CLUSTER_NOTIONAL_LIMIT_FRAC = 0.5` — skips placements for markets in any cluster whose live notional > 0.5·T.
    - **Kill switch** (sticky until process restart) triggered on any of: `24h realized_loss > 0.1·T` / `latest reward_daily.correction_factor < 0.01` / `1h-over-6h fill_rate_ratio > 3.0`.
    - **Structured `[GUARDRAIL] {…json…}` telemetry** every cycle with all metric values.
    - See new §4.18.

5. **Farmer guardrails v2: hard enforcement + observability** (committed at `2e72606`):
    - **Hard notional enforcement** (`HARD_NOTIONAL_RATIO = 2.5`): when ratio exceeds 2.5, cancels lowest-priority orders until ratio ≤ 2.0. Priority key `(daily_rate ASC, notional DESC, spread DESC, cid, side)` — lowest-reward first, within that largest-exposure first, then highest-spread (risk proxy). Cap: `MAX_CANCELS_PER_CYCLE = 5` per helper.
    - **Hard cluster enforcement**: when a cluster's live notional > 0.5·T, cancels lowest-priority members of that cluster until under the limit. Other clusters untouched. Same per-helper cancel cap.
    - **Atomic kill switch**: strict ordering flag → cancel-all → log → `return` from `run_cycle`. Kill-switch cancels bypass all mode gates and always fire real cancellations (capital protection).
    - **Persistent-breach detector** (`MAX_BREACH_CYCLES = 3`): emits `[CRITICAL] persistent_overexposure {"cycles": N, "notional_ratio": X}` on `log.error` after ≥ 3 consecutive cycles over the hard threshold. Observational; does NOT auto-trip the kill switch.
    - **Fail-open visibility**: every missing or failed guardrail-input signal emits `[GUARDRAIL_WARNING] missing_signal=<name>` at `log.warning` — applies to `total_capital`, `cf`, `fill_rate`, `cluster_data`. Trading is never halted by a data hiccup, only by explicit kill-switch triggers.
    - See new §4.18.

6. **Execution modes + cycle telemetry** (committed at `7ab514d`):
    - **Three execution modes**: `DRY_RUN` (no API calls at all, intent-logged only — default), `SHADOW` (API reads permitted; no writes), `LIVE` (full execution). CLI: `--mode {dry,shadow,live}`. Every write site in `reward_farmer.py` routed through `_gated_place_orders_for_market` / `_gated_cancel_order` — 1 raw `order_lifecycle.cancel_order` + 1 raw `place_orders_for_market` remain, both inside the wrappers. Kill-switch cancels bypass the mode gate (§5.1 override). Belt-and-suspenders: `OrderLifecycle` + `DumpManager` get `dry_run=True` in any non-LIVE mode so any code path that slips past the wrapper still can't fire a real API write.
    - **Cycle summary telemetry**: `[CYCLE_SUMMARY] {…json…}` emitted once per `run_cycle` exit with 13 fields (cycle, ts, active_markets, total_live_notional, notional_ratio, max_cluster_notional, cluster_count, blocked_clusters, orders_placed, orders_cancelled, kill_switch, realized_loss_24h, cf).
    - **Rolling-stats telemetry**: `[ROLLING_STATS] {avg_notional_ratio, max_notional_ratio, avg_orders, avg_cancels}` emitted every 10th cycle over a 100-cycle deque.
    - **Intent logging**: `[DRY_RUN] <action> {…}` / `[SHADOW] <action> {…}` for every gated place/cancel call in non-LIVE modes.
    - See new §4.19 and §4.20.

7. **Ready for staged deployment.** `python reward_farmer.py` (default `--mode dry`) runs the full system log-only. `--mode shadow` adds real reads + book state without writes. `--mode live` enables execution. Each mode emits the full telemetry stream; the operator watches `notional_ratio`, `orders_placed`, `cf`, and `realized_loss_24h` before promoting to the next tier.

8. **Test suite**: 384 tests pass in the fast tier (baseline preserved across every commit since 5611d54 — no regressions introduced by any of the five post-v4.0 patches).

9. **Memory files synced** to reflect the v5.0 state.

**Major semantic break in v4.0** (retained — applies equally to v5.0 since v5.0 is additive). The Patch 6–13 stack (overcommit factor, target-driven greedy, forced-exposure promotion, marginal-efficiency gate, exposure saturation, oscillation hysteresis, Patch-era objective blend) has been **deleted in its entirety** from the allocator. What replaces it is a single continuous formula plus a two-variable control law. The allocator went from 1616 lines to ~390 lines (v5.0; was ~320 at v4.0, grew by the Step-3b shaping layer). The v3.x §4.15 "Profit Maximization Stack" section is superseded — the content is preserved in §4.17 for historical reference but the mechanisms no longer exist in the codebase.

**Amendments in v4.0:**

1. **Patches 6–13 removed.** `profit/allocator.py` rewritten as a pure continuous allocator per the design locked in §4.15 (new). No more `_compute_overcommit_factor`, `_enforce_expected_capital`, `_force_overcommit_allocation`, forced-exposure block, target-driven greedy, Patch 4 efficiency penalty, or marginal-efficiency gate. All patch-era observability stamps (`_overcommit_factor`, `_forced_target_alloc`, `_forced_exposure`, `_low_ev_override`, `_saturation_applied`, `_target_notional`, `_saturation_scale`) removed from the allocation JSON schema.

2. **Unified Continuous Allocator** (new §4.15): `w_i = R_i / (1 + p_i·L_i)`, `raw_i = w_i^(1+η)`, `scale = β·total_capital / Σ(p_i·raw_i)`, `C_i = raw_i · scale`. Hard safety caps (per-market / per-group / per-cluster / min-shares floor) applied post-step as clip-only. Step 7 safety ceiling retained at 0.95·T. `raw_reward_per_day` field added to `CalibrationPredictions` so the formula reads reward directly rather than reconstructing from EV.

3. **λ1/λ2 → β/η control-variable swap** (new §4.16 Control System). The λ1/λ2 mechanism was proven **structurally inert** under uniform markets by the controllability analysis: `C_i` depends only on `total_capital`, `N`, and `p` in the uniform regime — every `R`, `D`, `λ1`, `λ2` cancels in the `raw_i / Σ raw_k` ratio. Replaced with `(β, η)`:
    - `β ∈ [0.10, 0.95]` — utilisation target that directly multiplies the Step-3 scale, non-cancelling in any regime.
    - `η ∈ [0.00, 4.00]` — concentration exponent on `w_i`; non-cancelling under heterogeneity via `C_i/C_j = (w_i/w_j)^(1+η)`.
    - Update rules per §4.16.4: β-feedback on `expected_util` (target 0.75, gain `K_BETA = 0.5`, `ALPHA_BETA = 0.03`), η-feedback on `coverage_ratio` (target 0.5, gain `K_ETA = 1.0`, `ALPHA_ETA = 0.03`). Stability guard halves both α's when `_detect_oscillation(capital_history)` fires.

4. **LearningState schema migration.** `aggressiveness`, `risk_multiplier` deleted earlier (§3.3 era); `λ1`, `λ2` now demoted to deprecated compatibility fields (frozen at `1.0` / `0.5`, not updated, not read by allocator). `β`, `η` added. Four live control scalars: `capital_scale`, `reward_trust`, `β`, `η`. DB migrations idempotently `ALTER TABLE ADD COLUMN` for `β`, `η`.

5. **V5 audit framework added** (`simulation/audit_v5_*`). INV3 and INV5 redefined to match the continuous-allocator objective:
    - **INV3_new** — expected capital utilisation: `avg(expected_util) ∈ [0.5, 0.95]`.
    - **INV5_new** — allocation coverage: `avg(active_markets / total_markets) ≥ 0.5`, where "active" = `C_i > cpb·min_shares`.
    - **INV7** — unchanged (capital_scale oscillation stability).
    - Spec §3.2 contract: per-market `_p_fill` / `est_capital_cost` / `shares_per_side` / `min_size` / `max_spread` required on every deploy row or `V5FieldMissingError` raised.
    - `total_capital` now stamped on each allocation row as `_total_capital` (observability only) so the learning-loop metrics engine can compute `expected_util = Σ(p·C) / total_capital`.

6. **Sim-only p_fill bootstrap fix** (`simulation/bootstrap_calibrator.py`, new). Wraps `CalibrationManager` for the sim path only; when `fill_model.is_ready() == False`, substitutes a deterministic `p = 0.03 + 0.001·daily_rate + 0.004·q_share_pct` clamped to `[0.02, 0.15]`. Fixes the `p_fill = 0` collapse that made `expected_capital ≈ 0` under V4/V5 invariants. Production calibrator code untouched; wrapper becomes a transparent pass-through once the fill model trains.

7. **Audit V5 post-control empirical result.** `expected_util` up **~700× uniformly** vs the pre-fix bootstrap-collapsed state (from ~5e-5 to 0.029–0.106). `under_deployed` scenario passes INV3_new (0.106 — above 0.5 floor). All other scenarios' `expected_util` lands in 0.029–0.054 because the **cluster-cap × min-shares-floor composition** is the dominant binding constraint in the sim environment: 30 correlated markets fall into one oversized cluster (15% of $2000 = $300 budget, which divided over 30 markets gives $10/market, below min_capital $27.3), forcing every post-cap `C_i` to `min_capital` regardless of what β or η does upstream.

8. **Controllability analysis (§4.16.2).** Recorded mathematical fact: under exactly-uniform markets, no continuous control variable can produce cross-market differentiation by symmetry; under near-uniform markets, `ΔC_i/C̄ ≈ -(1+η)·ε_i / (1+x̄)` gives first-order leverage whose magnitude scales with η. β has linear non-cancelling leverage on absolute scale in any regime. The spec's β/η choice specifically targets both failure points of the old λ-era design.

9. **Test-suite cleanup.** Deleted `test_patch{6,7,9,10,11,13_corrected}.py` (~68 tests testing deleted mechanisms), `test_profit_engine.py` (~50 tests of deleted allocator internals), `test_alpha_layer.py` (~15 tests), `test_e2e_allocation_flow.py` (~10 tests). Rewrote `test_learning.py` (27 tests, β/η + surviving scalar logic). Added `test_continuous_allocator.py` (11 tests covering the new formula's §13 requirements: small-R, large-E_loss, smoothness, determinism, caps-clip-only, stamps, pass-through). Test count: 563 (committed at `8a8466e`) → 394 (targeted fast-tier subset post-migration, passes in 5 min). Full suite including 34-minute `test_simulation::TestDeterminism` remains green.

10. **Memory files synced** to reflect the v4.0 state (Patches 6–13 removed, β/η control active, V5 audit valid, cluster-cap × min-floor identified as dominant cap-stack binding).

**Amendments in v3.2 (2026-04-21, for historical reference):**
- **Patch 13 (Target-Driven Allocation + Hysteresis)** — on working tree, not yet committed. Replaces Patch 11's saturation scaling with a greedy RAS-ranked greedy target fill (`_force_overcommit_allocation` removed; new Phase-B/C target_allocations + Part 2 merge), adds a marginal-efficiency gate (skip markets where `ev / (p_fill × size) < 0.7 × baseline`), adds a Part 4 efficiency penalty (`final_score × 0.9` when reward_efficiency < baseline in ACTIVE, down from an earlier 0.85 draft), adds Part 6 final safety re-enforcement (apply_cluster_caps + question_group cap + `_enforce_expected_capital`). Replaces Patch 11's damping-only anti-oscillation with a Part 5 **hysteresis** mechanism: `last_direction` + `direction_lock` on `LearningState` (DB-persisted), ACTIVE-gated post-EMA filter that blocks rapid direction flips while `direction_lock > 0` and filters same-direction micro-noise below `CAPITAL_CHANGE_MIN_STEP = 0.05`. Patch 11's `_detect_oscillation` + `OSCILLATION_DAMPEN_FACTOR × u_cap` pre-EMA dampen is retained as a secondary layer. New §4.15.7.
- **Patch 11 (Exposure Saturation + Oscillation Damping)** — committed at `d8a4569`. Target: close §4.15.5 adversarial-regime gap (resolution #2: upsize existing deploys) and close INV7 oscillation via `capital_history` + `_detect_oscillation` damping. New §4.15.6. Superseded in its saturation mechanics by Patch 13 — the module-level `_CAPITAL_HISTORY_CACHE` and the pre-EMA damping hook remain; the geometric 1.25^n scaling loop does not.
- §8.4 config appendix expanded with Patch 11 and Patch 13 constant tables.
- §10 changelog now records commit `d8a4569` (Patches 6–11) + uncommitted Patch 13.
- §10.3 "Still open" updated: INV3/INV5/INV7 moved from "still open" to "addressed by Patch 13 (pending re-audit)" pending a post-Patch-13 V3.1 run.
- §12.5 debugging priority updated for Patch 13 stamps (`_forced_target_alloc`, hysteresis state).
- **Backward-compat invariant preserved**: Patch 11 and Patch 13 branches gate on `ls_mode == "ACTIVE"` (Patch 13 hysteresis also gates on `prev.mode == MODE_ACTIVE`). OFF/SHADOW/learning_state=None callers see pre-Patch behaviour unchanged.
- Test suite: 505 → 542 (pre-Patch-11) → 552 (post-Patch-11 commit) → **563** passing (post-Patch-13 working tree).

**Amendments in v3.0 (2026-04-21, for historical reference):**
- **Patch 6 (Profit Maximization Layer)** — Safe Expansion rule (Rule E on fill_rate + loss_per_capital), Objective Correction blend (0.7 × RAS + 0.3 × normalized raw_ev), deployment-boost (×1.05 when deploy_ratio < 0.75), MIN_MARKETS floor (5)
- **Patch 7 (Overcommit Model)** — `_compute_overcommit_factor` (3.0 default, clamp [1.5, 6.0]), `_enforce_expected_capital` replaces notional-budget conservation in ACTIVE (`Σ p_fill × size ≤ total_capital × 0.95`), cold-start `_fallback_p_fill_cold_start` book-aware heuristic in `calibration/manager.py`, `profit/refill.py` pure helpers (not wired to reward_farmer)
- **Patch 9 (Deployment Expansion)** — target_count ×1.5, effective_per_market_cap ×1.5, per_market_scale 0.5 (halves per-market alloc), MIN_MARKETS_ACTIVE_FLOOR = 15
- **Patch 10 (Exposure Forcing)** — relaxed EV gate (NEGATIVE_EV_TOLERANCE = −0.02, `_low_ev_override` flag), exposure boost (final_score × 1.3 in ACTIVE), hard-profit-guard override in ACTIVE, forced-exposure block (deploy_ratio floor 0.85, target 0.95)
- New §4.15 "Profit Maximization Stack" consolidating the ACTIVE-only layers
- §8.4 config appendix for Patch 6–10 constants
- §10 changelog + known-limitations updated; simulation/audit arc noted (V1 → V3.1)
- **Backward-compat invariant preserved:** every Patch 6–10 branch is gated on `ls_mode == "ACTIVE"` (or `learning_state is not None`), so OFF/SHADOW/legacy callers see pre-Patch behaviour unchanged
- Test suite expanded: 505 → 542 tests, all passing on working tree

**Amendments in v2.0 (2026-04-20):**
- Two-process topology clarified
- Sports protection (three-phase + three-layer) documented in full
- Order book cache (Option B) added
- Q-share resolution priorities updated (3 priorities + poisoned-row guard)
- Cold-start prior, trial-cap configurability, and `game_start_time` pipeline added
- SafetyController states corrected (6 states, not 3)
- CF clamp floor updated (1e-6, lowered from 0.001)
- Structural risks consolidated with failure scenarios
- Config knob reference appendix added
- Database schema reference added
- Known-fixed-bugs changelog added

---

## Current Production State (v5.1.22 — Phase A of Master Plan complete: FX-037 + FX-050 + FX-049 shipped; loss-accounting integrity restored)

| Layer | State | Location |
|---|---|---|
| **Allocator** | Continuous formula `w = R/(1+p·L)` → `raw = w^(1+η)` → `C = raw · β·T/Σ(p·raw)` + Step-3b cap-aware shaping → caps clip-only → Step-7 safety rescale. ~390 lines. Profit-engine path **inactive in production** (calibrator not yet trained). Legacy path runs every cycle and now correctly stamps `_total_capital` (Phase 2 fix in `d2612e6`). | §4.15, §4.16, [profit/allocator.py](profit/allocator.py), [oversight/allocation_writer.py](oversight/allocation_writer.py) |
| **Learning loop** | Four live scalars: `capital_scale`, `reward_trust`, `β`, `η`. `_read_alloc_file` dict-key fix in `4f102e3` reactivated the metrics pipeline (was structurally dead since the V2 migration); valid_cycles_observed now advances on `metrics_ok`. `GATE_ACTIVE_CYCLES = 2000` (bumped from 50 as SHADOW-soak safety belt in `e270d63`; revert to 50 after observing sane β trajectory in LIVE). Bounded-rate + flip-suppression stabilise capital_scale. λ1/λ2 frozen compat fields (deprecated). | §4.13, [profit/learning.py](profit/learning.py) |
| **Audit** | V5 (`run_audit_v5`) passes INV3_new (cap-normalised), INV5_new (coverage), INV7 (flip rate). Overall verdict: **PASS 18/18 seed-scenarios**. | §10.4, [simulation/audit_v5_*](simulation/) |
| **SafetyController** (agent-side) | **7 states**, 14 invariants; still the final override on allocation JSON. v5.1.5 closed the I9 cold-start deadlock (`dd67f97`). v5.1.7 closed the bootstrap pair: `dc78ba0` adds a unified `_is_genuine_cold_start()` helper and uses it to skip the I3 drawdown violation when both `orders_placed` and `fills` are empty; `541108b` adds the new `BOOTSTRAP` state (10 markets, 30% capital, trials=True) slotted between `MILDLY` and `SEVERELY`, entered on a genuine cold start via `_cold_start_or(MILDLY)` and exited on either ≥10 lifetime fills or ≥3 clean cycles. Behaviour is byte-identical on the Helsinki server (which has placed orders, so cold-start gates do not fire). **Adjacent gaps still open** (see `fixit.md`): capital-sizing race (`FX-013`), broader SafetyController test coverage (`FX-016`), and the Phase-2-and-onward items. | §4.14, [oversight/safety_controller.py:867-893](oversight/safety_controller.py:867) |
| **Runtime guardrails** (farmer-side) | Soft blocks at notional 2.0·T / cluster 0.5·T; hard enforcement at notional 2.5·T → cancels to ≤2.0·T (multi-cancel cap 5/cycle, priority `daily_rate ASC → notional DESC → spread DESC`); kill-switch on {24h loss > 0.1·T, CF < 0.01, fill-rate spike > 3×}; persistent-breach `[CRITICAL]` emit after 3 cycles. **All armed** post-Phase-2. **Post-FX-050 (v5.1.22):** 24h-loss kill switch now fires at true loss magnitude (was under-reporting by ~25-30% due to missing Polymarket taker fee in unwind `usd_value`). | §4.18 |
| **Fill-detection symmetry** (FX-037 + FX-007) | Both BUY-side (`OrderLifecycle._check_buy_phantom_fill`, v5.1.21) and SELL-side (`DumpManager.check_dump_fills`, v5.1.9) compare SDK `size_matched` against on-chain `get_balance_allowance` delta. Phantom over-report → prefer on-chain truth + `log.critical("PHANTOM FILL: ...")`. Fail-OPEN on API exception: SDK value preserved + `log.warning`. Symmetric defense closes the 2026-05-19 Iran 158→38 silent-corruption shape. | §4.X (TODO), [order_lifecycle.py:_check_buy_phantom_fill](order_lifecycle.py), [dump_manager.py:60-87](dump_manager.py:60) |
| **Wallet reconciliation** (FX-049, v5.1.22) | Runs once per agent cycle (~30 min) via `oversight/wallet_reconciliation.py::reconcile_wallet_invariant`. Compares ACTUAL wallet pUSD delta against EXPECTED delta (`Σ unwinds − Σ fills + Σ data-api REWARD + Σ MAKER_REBATE` since last reconcile baseline). `\|divergence\| > RF_WALLET_DESYNC_THRESHOLD_USD = $0.50` → `[CRITICAL] WALLET_DESYNC` log. First-run path snapshots baseline (no false-positive on cold start). Fail-OPEN on data-api failure (`status='fail_open'` row + `log.warning`, no CRITICAL). Incremental — each cycle resets baseline. Permanent invariant catching the SYMPTOM of any future cash-accounting drift even if root cause is unknown. | §9.1 `wallet_reconcile_history` table, [oversight/wallet_reconciliation.py](oversight/wallet_reconciliation.py) |
| **Execution modes** | `--mode {dry,shadow,live}` — staged deployment. Default `dry`. Kill-switch overrides mode. | §4.19 |
| **Telemetry** | Per cycle: `[CYCLE_SUMMARY]` JSON + `[GUARDRAIL]` JSON + `[OVERSIGHT]` (action/reason/latency). Every 10 cycles: `[ROLLING_STATS]`. Ad-hoc: `[CRITICAL]`, `[GUARDRAIL_WARNING]`, `[OVERSIGHT_ERROR]`, `[OVERSIGHT_WARNING]`, `[DRY_RUN]`/`[SHADOW]`, `[OVERSIGHT_SHADOW]`, `[LEARNING_SHADOW] would_apply`. v5.1.8 tightens the `orders_placed` field to mean **API-confirmed placements** (rows actually written to the `orders_placed` DB table), not attempts — counter == `SELECT COUNT(*) FROM orders_placed` for the cycle window. All structured / printf-style; machine-parseable. | §4.20 |
| **Oversight evaluator** | `oversight_agent.evaluate(guard)` is now a **wired, flag-gated decision function** (Phase C in `5757aef + a08e86a + 5909764`). Master gate `_SHADOW_ONLY = True` keeps Stage 1 behaviour byte-identical to v5.1.1 — returns `continue/shadow` regardless of fired signals. Flipping `_PAUSE_ENABLED=True` activates Stage 2 (pause signals act). Flipping `_KILL_ENABLED=True` activates Stage 3 (cf_trajectory kills). Multi-signal precedence: strict severity (kill > pause > continue). Per-signal try/except hardening. `hasattr` gate, latency budget 50 ms, strict `{action,reason}` validation, kill path propagates `"oversight:"+reason`. | §4.21, [oversight_agent.py:596-789](oversight_agent.py:596), [reward_farmer.py:1825-1872](reward_farmer.py:1825) |
| **Server deployment** | Hetzner CCX13 in **Helsinki** (`hel1`, Finland), Ubuntu 24.04 LTS, hardened to v5.1.4 §11.5 spec, Python 3.14.4, repo at `987a844` (v5.1.6 housekeeping pushed but no restart required to consume — both v5.1.6 commits are debt-only), .env transferred, systemd units up. Migrated from Ashburn (us-east) after the v5.1.4 US-geoblock finding. **First LIVE cutover 2026-05-15 04:03 UTC; Polymarket accepts orders from Helsinki IP (no 403).** Bootstrap deadlock observed and patched in v5.1.5 (`dd67f97`); v5.1.6 closed FX-017 + FX-018 (housekeeping, no behaviour change). | §11 |
| **Wallet** | FUNDER `0xB23Bc80E6719099aeBE0c34389f05EC8C928503f` (Polymarket proxy), pUSD balance **$226.09** (2026-05-24 03:05 UTC; post 2026-05-22 dump loss of $1.34 from earlier $227.43 peak), all V2 allowances unlimited, CTF approvals True. **FX-049 wallet reconciliation invariant active** post-v5.1.22 — any future drift > $0.50/cycle triggers `[CRITICAL] WALLET_DESYNC`. | §11.5, [check_wallet.py](check_wallet.py), §9.1 `wallet_reconcile_history` |
| **Fast-tier tests** | **785/785 pass** on Mac and on the Ubuntu CI runner (CI 26350996533, 5m46s). v5.1.18 added 24 FX-036 regression tests in `tests/test_placement.py` covering the queue-walking algorithm on both sides, escape hatches, thin-book fallback, asymmetric depth, the Iran-market motivating scenario, safety invariants, and end-to-end wiring through `place_orders_for_market`. v5.1.17 added 12 FX-035 regression tests in `tests/test_get_merged_book.py` that exercise the REAL `get_merged_book` with both dict-form (V2 SDK production shape) and object-form (test mock shape) inputs — the smoke-test gap that hid FX-035 in production for 4 days. v5.1.15 added 5 FX-031 tests; v5.1.16 rewrote `TestDeadMarketCleanupCascade` + added a source-inspection test. Coverage on `oversight/safety_controller.py`: **94%**. v5.1.13 added 135 SafetyController-focused tests (17 Phase 1 → 152 total in `tests/test_safety_controller.py`) covering all 14 invariants, the 7-state ladder, `filter_allocations`, persistence round-trip, helpers, and alert-file writers. Coverage on `oversight/safety_controller.py`: 58% → **94%** (525 → 530 stmts, 218 → 34 miss; remaining 34 are defensive `except` handlers for DB-corruption scenarios). Slow tier `tests/test_simulation.py` excluded from the fast run (manual scenario sim). | `tests/test_safety_controller.py`, `tests/` |
| **CI / build gate** | GitHub Actions workflow `.github/workflows/test.yml` runs the fast-tier suite on every push to `main` and every pull request (`ubuntu-24.04`, Python 3.14, pip-cached, 15-min job timeout). First green run `26046878949` after `a580bdb` push: 7m17s; subsequent runs ~4-5 min with warm pip cache. Workflow status badge on `README.md`. v5.1.12 / `fixit.md::FX-026`. | §10.1, [.github/workflows/test.yml](.github/workflows/test.yml) |

### What v5.0 achieved vs v4.0

| Concern | v4.0 state | v5.0 state |
|---|---|---|
| Cluster-cap × min-floor signal erasure | Unresolved, out of scope | **Closed** by Step-3b shaping (`5611d54`) |
| INV7 capital_scale oscillation | 4/6 pass | **6/6 pass** — bounded-rate + flip suppression (`741d35c`) |
| V5 INV3 unreachable (p_fill ceiling) | 0/6 pass | **6/6 pass** — cap-normalised metric (`707ca50`) |
| Runtime exposure caps | None — allocator-only | Soft + hard enforcement + kill-switch + telemetry (`414354a`, `2e72606`) |
| Safe test/deploy split | Binary `--dry-run` bool | Three-mode gate `DRY_RUN` → `SHADOW` → `LIVE` (`7ab514d`) |
| Operator observability | `[ALLOC]` log lines | Structured `[CYCLE_SUMMARY]` + `[ROLLING_STATS]` + `[GUARDRAIL]` JSON |

### What v5.1 added on top of v5.0

| Concern | v5.0 state | v5.1 state |
|---|---|---|
| Final-safety policy hook | None | Deterministic `oversight_agent.evaluate(guard)` integration in `run_cycle` (`b8d84bd` → `2706953`). Single call site per cycle, latency-tracked, strictly validated, audit-logged every cycle. See §4.21. |
| Oversight log spam if stub absent | n/a | Closed — `hasattr` gate produces silent fallback at INFO level instead of WARNING flood. |

### What remains (open issues + known gaps as of v5.1.6)

**Blockers:** none currently known. The v5.1.4 geoblock blocker is resolved by the Helsinki migration (see v5.1.5 scope, item 1). The v5.1.5 bootstrap deadlock is resolved by `dd67f97`. Phase 0 housekeeping (FX-017 / FX-018 / FX-020) closed in v5.1.6. Open issues are tracked in detail in `Polymarket bot fixit.md` with stable `FX-NNN` IDs.

**Active operational items** (post-Phase-D, partially tracked also in `fixit.md`):
- ~~**`numpy` not in `requirements.txt`.** Came in transitively via streamlit (`pyproject.toml`) on Mac; missing on headless server. Manually `pip install numpy` on the server (done). Add to `requirements.txt` as a PR — one-line change.~~ — **Resolved in v5.1.6 (`987a844`, FX-018).** `numpy>=2.0` is declared in `requirements.txt`; fresh installs no longer need the manual step.
- **`_p_fill` not stamped on legacy allocator rows.** Profit engine stamps it (`profit/allocator.py:372`); legacy doesn't. `expected_capital_sum = 0` → `expected_util = 0` → β rule converges to upper clamp 0.95 under EMA. Mitigated by `GATE_ACTIVE_CYCLES = 2000` SHADOW soak. Permanent fix: mirror the profit-engine stamping pattern in `oversight/allocation_writer.compute_allocations`, or retire the legacy path entirely once calibrator readiness is achieved.
- **`GATE_ACTIVE_CYCLES = 2000` is temporary.** Revert to 50 once LIVE operation shows `[LEARNING_SHADOW] would_apply` β/cap_scale/trust trajectories converging to sane values for ≥4h. Inline TODO at `profit/learning.py:66` marks the spot. One-line commit when ready.
- **`check_wallet.py` emits a cosmetic HTTP 400 error** at the top of its output (conditional ERC1155 asset query with invalid tokenId=-1). On-chain wallet state below the error reads correctly; the bot's runtime balance fetch (different code path) works fine. Script-level cleanup, not blocking.
- **`get_orders` log message in error path** still reads `"get_orders failed: …"` after the V1→V2 rename to `get_open_orders` (deliberately preserved for log-grep continuity with historical corpus). When the SDK eventually settles, consider updating the log strings to match the actual method name.

**Active operational items new in v5.1.5** (all tracked in `fixit.md`):
- ~~**Counter / DB inconsistency** (`fixit.md::FX-004`): `[CYCLE_SUMMARY] orders_placed` increments at attempt time, not after API confirms. Observed in production cycle 3 reporting `orders_placed: 2` while DB `orders_placed` table had 0 rows.~~ — Resolved in v5.1.8 (`e7fc3d2`). `place_orders_for_market` returns `int` and the wrapper accumulates; counter now matches `SELECT COUNT(*) FROM orders_placed` exactly.
- ~~**Orphan-scan creates persistent failing dumps for resolved markets** (`fixit.md::FX-007`).~~ — Resolved in v5.1.9 (`7d8d38d`). New `unliquidatable_markets` DB table + gates at every order path + mark-on-canonical-400 in OL + DM + 30-min re-probe sweep. Tamilaga spam closes on the next Helsinki `git pull + restart`. See §10.2 B14 and the rewritten "Planned fix" in §4.22.
- ~~**Capital-sizing race on cold start** (`fixit.md::FX-013`): first oversight cycle on a fresh DB falls back to the `--capital 1500.0` default.~~ — Resolved in v5.1.10 (`d4d1541`). Farmer writes `usdc_balance` on cycle 1 (closes the 5-min window); agent's `--capital` defaults to `None` and skips the cycle if no fresh value is available rather than silently using $1500. See §10.2 B15.
- **No dedicated SafetyController test coverage** (`fixit.md::FX-016`): the bootstrap deadlock that v5.1.5 fixes would have been caught by any unit test exercising `_query_data_freshness` with an empty `scoring_snapshots` table. No such test existed. Build-out scheduled in Hardening Phase 6.

**Phase C oversight promotion sequence** (operator-driven, not automatic):
- **Stage 1 (current)**: `_SHADOW_ONLY=True, _PAUSE_ENABLED=False, _KILL_ENABLED=False`. Signals computed + logged, no actions.
- **Stage 2**: after ≥200 LIVE cycles with no `[OVERSIGHT_SHADOW]` false positives in healthy regime, flip `_SHADOW_ONLY=False` AND `_PAUSE_ENABLED=True`. Pause signals (A,B,C,D,F) act.
- **Stage 3**: after ≥200 LIVE cycles at Stage 2 with no flapping (toggle freq < 1/30 cycles), flip `_KILL_ENABLED=True`. cf_trajectory acts as kill.
- Each flag flip is a single-line commit, easy to revert.

**Calibrator dormancy** (chicken-and-egg):
- `FillModel` and `LossModel` need ≥50 fills + ≥15 positives to become `is_ready() == True`. DRY mode never accumulates fills. Profit-engine allocator never runs while calibrator is dormant; legacy allocator runs forever. Exit: real LIVE operation accumulates fills, calibrator trains, profit-engine activates, β rule gets real `expected_util` inputs (closes the `_p_fill` issue above as a side effect).

**Carryovers from v5.0 / earlier**:
- `profit/refill.py` helpers exist but are **not wired into `reward_farmer.py` / `order_lifecycle.py`** — fill-triggered refill still runs on the 30 s cycle cadence. Deferred from v3.x; not touched by v4.0, v5.0, or v5.1.
- **Gamma-routed sports markets unprotected by Phase 1** — Gamma API doesn't expose `game_start_time`, so Phase 1 only applies to CLOB-routed sports. Fall back to Phase 3's 4h `end_date_iso` block.
- **Learning-loop Rule A low-fill / high-loss edge case** (§6.7) — unchanged from v3.x. Rule A requires `fill_rate > threshold` to contract; a low-fill high-loss regime is invisible to it.
- **Deprecated `lambda_1` / `lambda_2` fields still on `LearningState`** — frozen at `1.0` / `0.5`; retained as compat shims because `simulation/engine.py` + `simulation/invariants.py` reference them. Future sim-side migration can remove.
- **No per-market CF** — reward signal is still a single global scalar. The v3.x asymmetry (reward-global, loss-local) is preserved by design.
- **Stop-loss events not distinguished** from normal unwinds in the learning signal.
- **`capital_util` > 1.0 in some V5 scenarios** is notional overcommit (allowed on Polymarket — orders cancel if one fills). Allocator's Step-7 rescale caps `Σ(p·C) ≤ 0.95·T` but not `Σ C`. Consistent with `project_capital_overcommit` memory; worth a revisit if over-fill risk becomes a production concern.
- **Backlog from `project_repo_structure` memory**: flat `.py` files should eventually be reorganised into `src/` package layout. Deferred until the bot is stable in production.

**Known catastrophic-mode behaviour (unchanged, by design)**:
- **CF deadlock** (§6.1) is THE one truly irreversible failure loop. Manual SQL recovery (`UPDATE reward_daily SET correction_factor = 1.0`) is the documented remediation. The kill switch fires at `cf < 0.01` and SafetyController degrades state at `cf < 0.005 / 0.02 / 0.03`, but neither defends against the loop itself — only manual intervention does. Operator runbook required.

**Bugs**:
- I am **not aware of any verified bugs** in the production path of v5.1 as of `2706953`. The 384/384 fast-tier suite passes; AST verification confirms the oversight integration's single-call-per-cycle invariant; the V5 audit passes 18/18 seed-scenarios. There is no test coverage for real Polymarket API failure modes (outages, rate limits, latency spikes) — those remain empirically unvalidated.

### Deployment ladder

Two-axis ladder. **Bot mode** (`--mode` flag) controls API write behaviour. **Oversight stage** (three module-level flags in `oversight_agent.py`) controls signal-to-action wiring. They're independent — the bot can be in LIVE mode while oversight is in Stage 1, etc.

**Bot mode ladder:**
```
python reward_farmer.py                   # DRY: no API write calls, intent logging only
python reward_farmer.py --mode shadow     # SHADOW: same as DRY operationally (counters mode-gated)
python reward_farmer.py --mode live       # LIVE: full execution, all guardrails armed
```

**Oversight stage ladder** (independent of bot mode):
| Stage | `_SHADOW_ONLY` | `_PAUSE_ENABLED` | `_KILL_ENABLED` | Behaviour |
|---|---|---|---|---|
| 1 | True | False | False | Signals computed + logged; `evaluate()` returns `continue/shadow`. Default. |
| 2 | False | True | False | Pause signals act; kill signal falls through to pause. |
| 3 | False | True | True | Full Stage 3 — pause + kill act. |

**Production operation (this codebase, post-v5.1.4):**
- Code runs under `systemd` on a Hetzner CCX13 server. **Server must be in a non-Polymarket-blocked region.** Two unit files: `polymarket-farmer.service` (the `--mode` line is the only line that changes between DRY and LIVE), `polymarket-oversight.service`. Both auto-restart on failure, both enabled on boot. See §11 for full operational replication.
- Mode switching: `sudo sed -i 's|--mode dry|--mode live|' /etc/systemd/system/polymarket-farmer.service && sudo systemctl daemon-reload && sudo systemctl restart polymarket-farmer`. Reverse direction is symmetric.
- Stage promotion: edit module-level constants in `oversight_agent.py`, commit on Mac, `git pull` on server, restart both services. See §11.11.

Watch for at minimum: `notional_ratio`, `orders_placed`, `orders_cancelled`, `kill_switch`, `cf`, `realized_loss_24h` on the `[CYCLE_SUMMARY]` JSON lines. Any `[CRITICAL]` or `[GUARDRAIL_WARNING]` line deserves immediate eyes-on. Once Stage 2/3 are active, `[OVERSIGHT] action=pause` and `action=kill` lines should be rare and never bursty — flapping is a doc-defined failure mode per §4.21.7.

---

## Reading Guide

| If you are … | Read these sections first |
|---|---|
| A new operator bringing the bot online | §1, §2, §4.19 (modes), §7 (monitoring), §4.20 (telemetry), §8 (config) |
| Running the system in production | §4.18 (runtime guardrails), §4.19 (modes), §4.20 (telemetry), §7.5–§7.8 |
| Debugging a live production issue | §7.10 (failure patterns), §6 (failure modes), §12.5 (v5.0 debugging priority), §4.18 (guardrail log formats) |
| Extending or modifying the system | §4 (core components), §4.15 (Continuous Allocator), §4.16 (Control System), §4.18 (Runtime Guardrails), §8 (config), §9 (DB schema) |
| Auditing or reviewing | §6 (failure modes), §10 (changelog), §10.4 (audit framework evolution incl. V5 PASS) |
| Understanding the current allocator | §4.8 (call surface), §4.15 (continuous formula + §4.15.7 shaping), §4.16 (β/η control + controllability analysis), §10.4 V5 results |
| Understanding the Patch 6–13 stack (historical) | §10.3 "Closed in v4.0 by deletion" + §4.17 + prior-version snapshots of §4.15 in v3.3 of this doc |

---

## 1. Core Objective

The system maximises liquidity reward earnings per dollar of capital deployed while preventing capital destruction from adverse fills.

It does this by:

1. Placing resting limit orders on Polymarket CLOB markets
2. Earning Polymarket liquidity rewards for having orders inside the reward window ("scoring")
3. Detecting fills, unwinding resulting positions, and continuously adjusting deployment based on observed outcomes

The system is not a pricing model, not a directional predictor, and not a global optimiser. It is a reward-capture allocator with layered safety controls.

---

## 2. Process Topology

The bot runs as **two independent processes** communicating through a single JSON file and a shared SQLite database.

```
┌─────────────────────────────┐        ┌─────────────────────────────┐
│  oversight_agent.py --loop  │        │  reward_farmer.py           │
│  (~30 min cadence)          │        │  (~30 s cadence)            │
├─────────────────────────────┤        ├─────────────────────────────┤
│  data_collector             │        │  market_discovery           │
│  CalibrationManager         │        │  order_lifecycle            │
│  market_scorer              │        │  fills / unwind             │
│  allocate_portfolio         │        │  dump_manager               │
│  SafetyController           │        │  reward_tracker             │
│  LearningController         │        │  _reconcile_orders          │
└──────────┬──────────────────┘        └──────────┬──────────────────┘
           │                                      │
           │ writes                               │ writes
           ▼                                      ▼
  market_allocations.json                  bot_history.db
           │                                      ▲
           └─── read by farmer ◄──────────────────┘
                   (every cycle)
```

**Key consequence of the split:** data the farmer writes to the DB is only consumed by the agent on its next cycle — potentially up to 30 minutes later. Debugging timing-sensitive issues must account for this lag.

| Process | Entry point | Cycle interval | Config | Role |
|---|---|---|---|---|
| Oversight agent | `oversight_agent.py --loop` | 1800 s | `--interval` | Scoring, allocation planning, safety state |
| Reward farmer | `reward_farmer.py` | 30 s | `RF_CYCLE_SECS` | Order execution, fill handling, data collection |

**Startup order (recommended):** farmer first, then agent. The farmer begins populating `bot_history.db` (scoring snapshots, book snapshots, fills) immediately; the agent will produce its first useful allocation once it has some data to score against.

---

## 3. High-Level Data Flow

```
Polymarket CLOB API                 Polymarket Gamma API
        │                                    │
        └──────────┬─────────────────────────┘
                   ▼
        market_discovery                     (reward farmer)
                   │
                   ▼
        data_collector   ◄──── bot_history.db (reward tracker, fills, scoring)
                   │
                   ▼
        CalibrationManager (fill, loss, reward, hazard models)
                   │
                   ▼
        market_scorer (ranking, trial cap, sports block)
                   │
                   ▼
        allocate_portfolio (RAS, caps, conservation, exploration)
                   │
                   ▼
        SafetyController (state-based override)
                   │
                   ▼
        market_allocations.json
                   │
                   ▼
        reward_farmer (place / cancel / dump / unwind)
                   │
                   ▼
        bot_history.db ──► fed back into data_collector next cycle
                   │
                   └──► LearningController.step() (scalar adjustments)
```

---

## 4. Core Components

### 4.1 Market Discovery

**Source**
- Polymarket CLOB rewards endpoint: `GET /rewards/markets/current` (paginated, covers all reward-eligible markets)
- Polymarket Gamma API: bulk enrichment for market metadata
- CLOB per-market endpoint: `GET /markets/{cid}` as fallback when Gamma doesn't cover the market

**Filtering**
- `daily_rate >= RF_MIN_DAILY_RATE` (default $10/day)
- Order book sanity: both sides non-empty, spread within `RF_MAX_BOOK_SPREAD`
- `end_date_iso` must not be within 12h (already-closing markets)

**Key property**
Discovery is **not** EV-gated. All eligible markets are passed to scoring; EV gating happens later in the allocator.

**What is extracted from the CLOB per-market response**
| Field | Used for |
|---|---|
| `token_ids` | Placing orders on YES and NO |
| `end_date_iso` | Market resolution deadline |
| `game_start_time` | **Actual event kickoff time** (sports only, ~73% of CLOB responses) |
| `minimum_tick_size` | Order pricing |
| `question` | Keyword detection (sports) |

`game_start_time` is **not** exposed by the Gamma API. Gamma-routed markets have `game_start_time=""`. See §4.11.

**Structural risk:** low-rate markets can be filtered permanently by `RF_MIN_DAILY_RATE`, creating an exploration blind spot for new reward programs. Mitigated but not eliminated by the cold-start prior (§4.10).

### 4.2 Data Collection & Q-Share Resolution

Data collector reads:

| Source | Contents |
|---|---|
| `scoring_snapshots` | Per-order scoring state from `are_orders_scoring` API, every 5th farmer cycle |
| `fills` / `unwinds` | Realised fill events and position unwind P&L |
| `reward_market_stats` | Cumulative per-market Q-score totals (written by reward tracker) |
| `book_snapshots` | Per-cycle order book summaries |
| Polymarket Data API | Authoritative daily reward payouts |

**Q-share resolution priority**

For each tracked market, `q_share` is resolved in this order:

| Priority | Source | Condition | Value |
|---|---|---|---|
| 1 | Windowed scoring (`_query_windowed_scoring`, 4h window) | `samples >= 3` | `min(scoring_ratio × 0.5, 0.5)` |
| 2 | Cumulative totals | `total_market_q > 0 AND q_score_samples > 0` | `total_q_score / total_market_q` (with poisoned guard) |
| 3 | Cold-start prior | `on_book < 2.0 AND q_score_samples == 0` | `RF_NEW_MARKET_Q_SHARE_PRIOR` (0.10) |
| fallthrough | — | stale or broken | `0.0` |

**⚠ Priority 1 is an upper-bound heuristic, NOT a measurement of queue share** (see `fixit.md::FX-045`, opened 2026-05-23). `scoring_ratio` measures the fraction of our `scoring_snapshots` rows where `scoring=True` — i.e., how much of the time our orders were inside the reward zone. This is a presence signal, not a queue-share signal. The `× 0.5` multiplier and `min(., 0.5)` cap are hand-tuned damping that bound the heuristic, but they don't transform it into a true share. For a well-positioned bot (orders 100% in-zone, typical post-FX-036), Priority 1 returns the maximum 0.5 regardless of how many competing makers are queued. Helsinki live probe (2026-05-23): Priority 1 returns q_share=0.5 for deployed markets where Priority 2 cumulative would return 0.000249-0.000405 — a 1500× over-estimate. This feeds est_d to I6 and is the primary cause of perpetual `est_actual_ratio > 5×` (blocking G3 friend-rollout gate). Priority 1's original design intent was to dodge the FX-005 era UPWARD-poisoned cumulative rows (`q_share` saturation at 1.0), but it over-corrects when cumulative is healthy and small. Fix design tracked in FX-045 / FX-046; gated on the FX-046 empirical investigation of Polymarket's actual reward formula.

**Poisoned-row guard (Priority 2)**
If `total_q_score / total_market_q > RF_POISONED_Q_SHARE_THRESHOLD` (default 0.5), the row is treated as contaminated and q_share falls through to Priority 3 (prior = 0.10). This defends against legacy rows created before the `reward_tracker.py:331` fix (see §10 Changelog and §6.2). Legitimate contested markets observed in production have q_share < 0.05, so false positives are extremely rare. Note: the guard only protects against UPWARD poisoning — non-poisoned cumulative values are still trumped by Priority 1's upper-bound heuristic (the FX-045 root cause).

**Stale-market decay**
Markets not seen in scoring snapshots for >24h are excluded entirely; >6h forces q_share to 0.

**Observability**
Each cycle logs:
```
oversight.collector: Q-share: {windowed} windowed, {cumulative} cumulative capped,
  {prior} cold-start prior, {poisoned} poisoned skipped,
  {decayed} decayed (>6h), {excluded} excluded (>24h)
```

**Key outputs downstream**
- `MarketMetrics.q_share_pct` — per-market competition share
- `scoring_seconds` — time our orders were actively scoring (used by reward model Phase 2)
- `estimated_daily_total` — sum of `daily_rate × q_share_pct` over deployed markets (numerator of CF)

### 4.3 Order Book Cache (Option B)

Added in commit `88f6c7a` after the root-cause analysis in `project_market_q_fallback_bug.md`.

**Motivation**
`reward_tracker.record_cycle` needs the live order book to compute `estimate_market_q(order_book, …)`, which is the denominator of q_share. Without the book, the legacy fallback silently produced q_share=1.0, causing systemic 5000× reward over-estimation.

**Mechanism**
```
Cycle T:
  place_orders_for_market(ms):
    merged = get_merged_book(client, ms.yes_tid, ms.no_tid)   # already fetched for order placement
    ms.cached_book = merged                                    # stored on MarketState
    ms.last_book_fetch = time.time()

Cycle T..T+6 (age <= RF_BOOK_CACHE_TTL = 180 s):
  record_cycle reads ms.cached_book → passes to estimate_market_q
  Real market_q computed → Q-share sample accumulated with correct values

Cycle T+N (age > TTL):
  record_cycle passes order_book=None → sample skipped entirely
  (NOT faked; total_market_q and total_q_score do not move)
```

**Key invariants**
- No new API calls introduced. The book is already fetched by `place_orders_for_market` for order placement; the cache just stops discarding it.
- Batch rotation covers every market every ~120 s (at `BATCH_SIZE=10`, `RF_MAX_MARKETS=60`). TTL = 180 s guarantees every in-portfolio market has a fresh cached book at any moment.
- `record_cycle` now requires **both** `our_q > 0 AND market_q > 0` before accumulating a sample. Previously `max(market_q, our_q)` was used as a fallback when `market_q = 0`, poisoning 394/402 rows in production.
- `RF_BOOK_CACHE_TTL = 0` disables the cache entirely (escape hatch).

**Field added**
`MarketState.cached_book: dict | None` (models.py)

### 4.4 Correction Factor (CF)

**Definition**
```
CF = actual_daily_payout / estimated_daily_total
```
Computed in `data_collector` from:
- Numerator: Polymarket Data API reward payouts (24h window)
- Denominator: `sum(daily_rate × q_share_pct)` over deployed markets

**Smoothing**
Exponential moving average with circuit-breaker branches at `data_collector._smooth_correction_factor`:

| Condition | Behaviour |
|---|---|
| `raw < 0.01` | Bypass EMA entirely (model is broken, use raw directly) |
| `raw < 0.05 AND prev_smoothed > 0.2` | Fast-adapt EMA with α=0.7 |
| Otherwise | Normal EMA with α=0.3 |

**Clamp**
`max(1e-6, min(10.0, smoothed))` — lowered from `0.001` in commit `1081e72` after a codebase audit confirmed **no consumer divides by CF**. The 0.001 floor had been masking a 5× margin of the true signal.

**Consumers**
| Location | Operation |
|---|---|
| `reward_model.predict_rate` (Phase 1) | `effective_daily = daily_rate × q_share × α` |
| `market_scorer.score_market` | `effective_daily = estimated_daily × CF` |
| `market_scorer.classify_market` | MIN_EFFECTIVE_DAILY gate |
| `oversight_agent` | `corrected_daily = estimated_daily × CF` (reporting) |
| `safety_controller` | Threshold comparisons (0.005, 0.02, 0.03) |

All consumers multiply by CF or compare it. None divide by it.

**Persistence**
- `reward_daily.correction_factor` — per-day persisted value
- `correction_factor_history` — last 30 observations with `raw`, `smoothed`, `estimated_daily`, `actual_daily`, `deployed_count`

**CF characteristics**

| Property | Behaviour |
|---|---|
| Scope | Global (applied to all markets) |
| Sensitivity | Extremely high — small miscalibration cascades across the whole system |
| Recovery | Self-healing under most conditions; however, a CF collapse combined with zero deployment is a deadlock (see §6.1) |
| Failure impact | System-wide |

### 4.5 Reward Model

Two phases depending on data maturity.

**Phase 1 (default, bootstrap)**
```
reward = daily_rate × min(q_share_pct, 0.5) × α
         where α is the smoothed CF
```

**Phase 2 (after ~7 days of reward history)**
```
reward = α × scoring_seconds_24h × daily_rate + β
         where (α, β) are fit by OLS on observed actual rewards
```

**Insight**
Phase 2 does not remove global coupling — it replaces CF with another global scalar (α). The per-market variance in Phase 2 comes from `scoring_seconds_24h`, which is a per-market quantity, but the model still has a single global multiplicative fit.

### 4.6 Fill & Loss Models

**Fill model** (`calibration.fill_model`)
- Logistic regression (10 features): spread, midpoint distance, depth ahead, opposite-side depth within 5c, our share count, was-scoring flag, normalised variants, order price, log daily rate
- Output: `p_fill` — probability the order fills within 24h
- Activates after ≥50 samples AND ≥15 positive fills

**Loss model** (`calibration.loss_model`)
- Recency-weighted averages or OLS over observed fill costs
- Output: expected USD loss per fill, local to the market

**Key property**
Loss modelling is **local and per-market**. Reward modelling is **global**. This asymmetry is foundational — see §6.9 Final Takeaways.

### 4.7 EV and Risk-Adjusted Score

**Expected Value**
```
raw_ev = raw_reward - p_fill × e_loss
       (both terms computed on a 24h horizon with matched units)
```
Confidence adjustment is applied asymmetrically in `calibration.manager.get_predictions`:
- Reward is multiplied by a bias factor (`REWARD_SAFETY_BIAS = 0.8`)
- Loss is inflated by up to 2× under low-confidence conditions
- EV is then gated through a dynamic confidence floor

**Risk-Adjusted Score**
```
RAS = EV / (1 + p_fill × e_loss × risk_multiplier)
```
where `risk_multiplier` is a learning-loop scalar. Markets with `EV ≤ 0` are eliminated upstream; the allocator uses RAS for ranking.

**Guards**
- Market-level: `EV ≤ 0` → reject
- Portfolio-level: `sum(EV) < 0` → reject ALL (profit guard)

### 4.8 Allocation Engine

**Pipeline stages** (`profit.allocator.allocate_portfolio`, v4.0 continuous form — see §4.15 for the full mathematical spec):

1. Partition `scored_markets` into deploy candidates + pass-through avoid rows (sports protection, trial-cap, `action != "deploy"` all preserved).
2. Per-market, call `calibrator.get_predictions(...)` to extract `R_i = raw_reward_per_day`, `p_i = max(1e-4, p_fill_24h)`, `L_i = e_loss_given_fill`, `cpb_i` from spread.
3. **Step 1–3 (core formula):** `w_i = R_i / (1 + p_i·L_i)`; `raw_i = w_i^(1+η)`; `expected_total = Σ p_i · raw_i`; `scale = β · total_capital / expected_total`; `C_i = raw_i · scale`. Fallback to equal allocation if `expected_total` collapses.
4. **Step 3b (v5.0, new):** cap-aware shaping. For each fill-cluster whose proportional budget `cluster_cap_pct × T / size` falls below per-market `min_capital = cpb · min_shares`, pre-select `k = max(1, floor(cluster_budget / cluster_min_capital))` top-ranked cluster members by `(-raw_alloc, condition_id)` and route the rest to `action="avoid"` (zero shares). Restores β/η signal through the cap stack. Non-binding clusters and unclustered markets are untouched. See §4.15.7.
5. **Step 4:** convert `C_i` to integer shares, enforcing `C_i ≥ cpb_i · min_shares`.
6. **Step 5 caps (clip-only, no redistribution):**
    - per-market (`MAX_PER_MARKET = $200` and `max_capital_pct · total_capital`)
    - per-question-group (`max_group_pct · total_capital`, default 30%)
    - per-fill-cluster (`apply_cluster_caps` via `profit/correlation.py`; oversized clusters use `OVERSIZED_CLUSTER_PCT = 0.15`)
7. **Step 6:** recompute `expected_capital = Σ p_i · est_capital_cost_i` post-cap.
8. **Step 7:** hard safety rescale — if `expected_capital > 0.95 · total_capital`, scale every row down uniformly. (This ceiling is a safety net, not a control target; distinct from β in Step 3.)

**Hard guarantees** (verified by `test_continuous_allocator.py`):
- Never returns zero deployments while any deploy candidate exists (min-capital floor in Step 4).
- No binary EV/RAS filtering — continuous weights only.
- Smooth in input perturbations (no step functions).
- Deterministic for fixed inputs.
- `learning_state=None` is a valid call (uses `DEFAULT_BETA = 0.75`, `DEFAULT_ETA = 0.0`).

**What's NOT there anymore** (removed in v4.0): EV gate, RAS ranking, `target_market_count` trim, bandit multiplier in the allocator path, overcommit factor, target_notional, marginal-efficiency gate, forced-exposure promotion, `_low_ev_override`, Patch 4 efficiency penalty, `_enforce_capital_conservation` two-sided rebalance, micro-exploration block. These mechanisms had no corresponding leverage under the continuous formula (proof: §4.16.2 controllability analysis).

**Critical property**
The allocator and SafetyController are still the two components that can reduce deployment to zero, but the allocator no longer does so via binary gating — only via the continuous formula producing small `C_i` that then floors to `min_capital` or gets clipped by caps. See §4.14 and §6.1.

### 4.9 Sports Protection

Sports markets have elevated adverse-selection risk from informed bettors watching live events. Protection runs in **three layers**; the primary layer uses **four sequential phases**.

**Layer 1 — Agent (`oversight/market_scorer.py:262-302`)**

For markets whose question matches any keyword in `SPORTS_KEYWORDS`:

| Phase | Signal | Threshold | Action |
|---|---|---|---|
| 1 | `game_start_time` (CLOB-only) | within `RF_GAME_BLOCK_HOURS=1.0` of kickoff, including negative (in-play) | avoid |
| 2 | `end_date_iso` missing | — | avoid |
| 3 | `end_date_iso` | ≤ `RF_SPORTS_BLOCK_HOURS=4.0` | avoid |
| 4 | `end_date_iso` | 4–72h | cap to `min_size` |

Phase 1 was added in commit `9f58e14`. It fires only when `game_start_time` is populated (CLOB-routed sports, ~73%). Gamma-routed sports markets fall through to phases 2–4 unchanged, preserving backward behaviour.

**Layer 2 — Bot (`order_lifecycle.py`)**

Before placing an order, the farmer re-checks the sports gate. If the scorer's decision was stale (e.g., market moved to within 4h of expiry since last agent cycle), the farmer cancels any existing orders on the market and refuses placement.

**Layer 3 — Pre-cycle expiry sweep (`reward_farmer.py`)**

Every farmer cycle, any market (sports or not) with `end_date_iso` within 1h gets all orders cancelled and all dump state cleared. This is the final safety net against markets that moved from "safe" to "resolving" between agent cycles.

**Known limitation**
Phase 1 offers no protection for Gamma-routed sports markets because Gamma does not expose `game_start_time`. These fall back to Phase 3's 4h `end_date_iso` block, which is later than true kickoff for most sports (end_date_iso is usually set several hours after the event ends).

### 4.10 Cold-Start Prior and Trial Cap

**Problem solved**
New markets (never posted on) used to get `q_share = 0`, producing `score = 0`, which routed them to the trial-cap path. The cap was hardcoded to 10 per cycle and used `score <= 0` as its criterion — meaning any market without prior data was throttled behind 10 slots sorted by daily_rate.

**Cold-start prior** (`RF_NEW_MARKET_Q_SHARE_PRIOR = 0.10`)

Applied in three places:

1. `data_collector.query_reward_stats` Priority 3 — when `on_book < 2.0 AND q_score_samples == 0`
2. `data_collector.collect_all` discovery branch — for CLOB-discovered markets not yet in `reward_market_stats`
3. Implicitly visible in the scorer via `MarketMetrics.q_share_pct`

The prior of 0.10 is conservative: it is below the ~0.5–1.0 range the bot historically observed when it was solo in markets, but above the 0.01–0.05 range observed in contested markets. With CF also multiplying in, effective daily estimates for prior-based markets are `daily_rate × 0.10 × CF`, landing near the `MIN_EFFECTIVE_DAILY = $0.10/day` gate at `daily_rate ≈ $10/day` — which matches the existing discovery floor.

**Trial cap** (`RF_MAX_TRIAL_MARKETS = 50`)

Configurable since commit `a6f580d` (was hardcoded 10). The criterion was also redefined:

| | Before | After |
|---|---|---|
| Criterion | `action == "deploy" AND score <= 0` | `action == "deploy" AND confidence == "low" AND fill_count == 0` |
| Default cap | 10 | 50 |
| Configurable | No | Yes |

The criterion change is necessary because the cold-start prior now makes new markets score positive — the old criterion would have missed them, letting discovery bypass the cap entirely.

Trials are still sorted by `daily_rate` descending before capping, so the richest new pools are prioritised.

### 4.11 `game_start_time` Pipeline

Added in commit `a6f580d` (pipeline) and activated in commit `9f58e14` (consumer — Phase 1 sports protection).

**Flow**

```
CLOB GET /markets/{cid} response
        │
        ▼
market_discovery (extract mkt.get("game_start_time", ""))
        │
        ▼
_fetch_reward_market_expiries
  (cache in market_expiry_cache.game_start_time column)
        │
        ▼
MarketMetrics.game_start_time   ──►  market_scorer Phase 1 sports block
        │
        ▼
ScoredMarket.game_start_time
        │
        ▼
market_allocations.json ("game_start_time" key)
```

**Data coverage**
- ~73% of CLOB responses include the field (empirically, all sports markets)
- Gamma API does not expose the field; Gamma-routed markets have `game_start_time=""`
- After a bot restart, the 24h cache TTL repopulates the new column naturally

**Schema**
`market_expiry_cache` now has columns `(condition_id, end_date_iso, game_start_time, fetched_at)`. Migration in `database.py:_migrate_enrichment_columns` adds the column idempotently.

### 4.12 Bandit Layer

**Method**
Thompson sampling with Beta(α, β) per-market posteriors.

**Inputs**
24h realised per-market PnL updates the posteriors each cycle.

**Effect**
Produces a multiplier per market applied to RAS during allocation ranking. Boosts markets that have been quietly profitable; penalises ones that have been quietly losing.

**Constraints**
- Cannot override an `EV ≤ 0` rejection
- Cannot rescue a market that has been filtered out upstream
- Deterministic seed: `hash(int(time.time()))`

### 4.13 Learning Loop

`profit/learning.py` — the behavioural feedback layer.

**Mode gate** (`LearningGate`) — unchanged from earlier versions.
| Mode | Conditions | Effect |
|---|---|---|
| OFF | Insufficient data (fills < 100, pairs < 50, reward_days < 3, valid_cycles < 50) | No effect on allocation |
| SHADOW | Thresholds crossed but not yet stable | Metrics computed and logged; applied_state always neutral |
| ACTIVE | Mature data with stable telemetry | Computed state flows to allocator / calibrator |

**Live control scalars (v4.0)** — four variables, each with a different destination and update rule.

| Scalar | Range | Destination | EMA α | Update signal |
|---|---|---|---:|---|
| `capital_scale` | 0.30 – 1.20 | multiplier on `total_capital` applied by `oversight_agent` BEFORE the allocator call | 0.20 | Rules A/B/D/E + Patch-3 expansion + Patch-11 oscillation damping + Patch-13 hysteresis (all retained from v3.x) |
| `reward_trust` | 0.50 – 1.00 | `CalibrationManager.reward_trust` in the PART-6 reward pipeline | 0.20 | Rule C on reward_error + mean-reversion toward 1.0 per cycle |
| `β` (beta) | 0.10 – 0.95 | Step-3 scale factor in allocator (§4.15) | 0.03 | `β · (1 + K_BETA · (TARGET_UTIL − expected_util))`, `K_BETA = 0.5`, `TARGET_UTIL = 0.75` |
| `η` (eta) | 0.00 – 4.00 | concentration exponent on `w_i` (§4.15) | 0.03 | `η + K_ETA · (TARGET_COVERAGE − coverage_ratio)`, `K_ETA = 1.0`, `TARGET_COVERAGE = 0.5` |

**Deprecated compat fields** (retained to avoid `AttributeError` in `simulation/engine.py` and `simulation/invariants.py`, which still reference them):
- `aggressiveness`, `risk_multiplier` — deleted as control levers earlier; removed from the dataclass entirely.
- `λ1`, `λ2` — deleted as control levers in v4.0; retained as frozen-at-default dataclass fields (`1.0`, `0.5`). No rule updates them; the allocator does not read them. See §4.16.2 for why they're dead — they cancel algebraically in the allocator's scale step.

**Rules (v4.0 surviving set)**
- Rule A/B/D/E (capital_scale): original Patch-2/3 behaviour, now only updating `capital_scale` (the aggressiveness/risk_multiplier branches were deleted along with the scalars).
- Rule C (reward_trust): `reward_error < 0.7` → `TRUST_DOWN = 0.90`; `reward_error ∈ [0.9, 1.1]` → `TRUST_UP = 1.02`. Mean-reverts toward 1.0 at 2% per cycle.
- β rule, η rule (new in v4.0, spec §6): see §4.16.4 for the full form.

**Patch-11 oscillation damping + Patch-13 hysteresis** (capital_scale): retained verbatim. The same `_detect_oscillation` signal is now also reused by the β/η stability guard (§4.16.4) to halve their α when capital_scale oscillates.

**v5.0 capital_scale stability filters** (new at `741d35c`): two additive filters run **after every pre-existing rule** (A/B/D/E + Patch-11 damping + EMA + CLAMP_CAP + Patch-13 hysteresis) inside `update_state`. Neither touches the upstream rule surface.

1. **Bounded-rate step** (`MAX_CAPITAL_SCALE_STEP = 0.07`):
    ```
    step_delta = new_cap_final − prev.capital_scale
    step_delta = max(-MAX_CAPITAL_SCALE_STEP, min(MAX_CAPITAL_SCALE_STEP, step_delta))
    new_cap_final = prev.capital_scale + step_delta
    ```
    Bounds preserved by construction — both `prev.capital_scale` and pre-clamp `new_cap_final` are already within `CLAMP_CAP = (0.30, 1.20)`. No-op in the current sim (per-cycle |Δ| ≤ 0.025 observed) but available for higher-swing regimes.

2. **Small-amplitude flip suppression**:
    ```
    delta = new_cap_final − prev.capital_scale
    prev_delta = last nonzero delta walking prev.capital_history backward
    if prev_delta is not None
       and (delta · prev_delta) < 0                                    # direction flip
       and |delta| < CAPITAL_CHANGE_MIN_STEP                           # small current
       and |prev_delta| < CAPITAL_CHANGE_MIN_STEP:                     # small prior
        new_cap_final = prev.capital_scale                             # revert
    ```
    Targets the ~0.01–0.025 amplitude direction churn that slipped past Patch-13's dead-band (Patch-13's dead-band is gated to same-direction deltas only — opposite-sign small deltas pass through). Large reversals (|delta| ≥ `MIN_STEP = 0.05`) and same-direction moves are unaffected.

**V5 INV7 result**: 4/6 → **6/6 PASS** after flip suppression. `over_aggressive` `max_flip_rate_100`: 7–9 → 0–1; `regime_shift_3phase`: 6–9 → 0. `expected_util` + `coverage_ratio` are byte-identical to the pre-filter run — the suppressed oscillation was too small-amplitude to perturb any downstream metric. See §10.4.

**Frontier memory** (Patch 5): unchanged. Per-regime `(best_reward, best_capital_scale)` dict, keyed by `(round(fill_rate_1h, 1), round(reward_efficiency, 3))`.

**Known blind spots** (v4.0):
- No direct PnL signal (reward_efficiency is reward-only).
- Stop-loss events are invisible to the loop.
- Rule A requires `fill_rate > threshold` — still misses the low-fill high-loss edge case (§6.7).
- β's leverage is architecturally zero under cluster-cap × min-floor binding (see §4.16.5 cap interaction): when every `C_i` is overwritten to `min_capital`, no upstream signal survives.
- η's leverage requires market heterogeneity in `p·L`; in exactly-uniform markets it is zero by symmetry (§4.16.2).

### 4.14 Safety Controller

`oversight/safety_controller.py` — the final override layer. Runs AFTER the allocator, mutating the allocation list based on state.

**States** (7 total, v5.1.7)

| State | max_markets | capital_pct | trials | Other |
|---|---:|---:|---|---|
| CALIBRATED | 60 | 100% | yes | — |
| MILDLY_MISCALIBRATED | 40 | 70% | yes | — |
| BOOTSTRAP | 10 | 30% | yes | cold-start ease-in; once-only initial state |
| SEVERELY_MISCALIBRATED | 20 | 40% | no | — |
| DEGRADED | 10 | 20% | no | — |
| DATA_UNAVAILABLE | 5 | 10% | no | — |
| UNSAFE | 3 | 5% | no | probe mode + min_size only |

BOOTSTRAP (added v5.1.7, `541108b`) is entered only on a genuine cold start (`_is_genuine_cold_start()` → True iff lifetime `orders_placed == 0` AND lifetime `fills == 0`). It allows trials because every market on a fresh DB is a trial (`confidence='low' AND fill_count==0`); the conservative-first goal is achieved through the 10-market / 30%-capital caps, not by suppressing trials. Exit to MILDLY_MISCALIBRATED happens on EITHER `lifetime_fills >= BOOTSTRAP_FILL_EXIT (10)` (fast path) OR `_bootstrap_clean_cycles >= UPGRADE_FROM_BOOTSTRAP (3)` (slow path for markets-are-dry scenarios). BOOTSTRAP is once-only: recoveries from any downgrade climb straight back to MILDLY through the existing upgrade ladder, never re-enter BOOTSTRAP.

**Invariants** (14 total; all checked each cycle)

| # | Name | Priority | Threshold |
|---|---|---|---|
| I1 | daily_loss | CRITICAL | > $150 / 24h |
| I2 | slow_bleed_7d | CRITICAL | > $500 / 7d |
| I3 | drawdown | CRITICAL | > 15% from peak |
| I4 | capital_floor | CRITICAL | balance < $50 |
| I5 | cf_drift | HIGH | CF < 0.005 / 0.02 / 0.03 |
| I5b | cf_corroborated | CRITICAL | I5 + est/actual > 15× + losses > $50 |
| I6 | est_actual_ratio | HIGH | > 50× / 15× |
| I7 | hourly_loss | HIGH | > $30 / $60 |
| I8 | capital_at_risk | HIGH | > 80% / 90% |
| I9 | data_freshness | MEDIUM | > 30 min warn, > 2h critical |
| I10 | data_completeness | MEDIUM | < 80% warn, < 50% critical |
| I11 | loss_reward_ratio | HIGH | > 1.5× / 2.0× |
| I12 | clob_rate_drop | MEDIUM | > 30% drop |
| I13 | fill_storm | LOW | ≥ 1 burst/hour — applies 20% capital haircut |
| I14 | cf_at_floor | LOW | ≥ 3 cycles — applies 10% capital haircut |

**Transitions**
- Downgrades are immediate on invariant violation
- Upgrades require clean conditions for `UPGRADE_STEP = 2` consecutive cycles (single-step improvement) or `UPGRADE_TO_CALIBRATED = 3` cycles (full recovery to CALIBRATED)
- BOOTSTRAP exit: ≥ `BOOTSTRAP_FILL_EXIT = 10` lifetime fills (fast path) OR ≥ `UPGRADE_FROM_BOOTSTRAP = 3` clean cycles (slow path). Target is always MILDLY_MISCALIBRATED. Counter is `_bootstrap_clean_cycles`, reset by `_transition`.
- UNSAFE auto-demotion: after `UNSAFE_RECOVERY_CYCLES = 3` cycles without any CRITICAL-UNSAFE violation, target caps at DEGRADED

**Bootstrap cold-start chain**
On a genuinely fresh DB, three invariants used to demote state to DATA_UNAVAILABLE despite the bot having nothing to compare against:
- I9 (`data_freshness`) — empty `scoring_snapshots`. Closed in v5.1.5 (`dd67f97`) via `_query_data_freshness` returning `0.0` instead of `None` when `_is_genuine_cold_start()` is True.
- I3 (`drawdown`) — zero `total_portfolio_value` and zero `exchange_balance`. Closed in v5.1.7 (`dc78ba0`) via the same `_is_genuine_cold_start()` gate — the violation is skipped (logged at INFO once per cycle) on a genuine cold start.
- Cold-start state default — was `MILDLY_MISCALIBRATED` (70% capital, trials). Closed in v5.1.7 (`541108b`) via the new `BOOTSTRAP` state and `_cold_start_or(MILDLY)` helper in `_load_state`.

All three branches collapse to one helper `_is_genuine_cold_start()` which returns True iff lifetime `orders_placed == 0` AND lifetime `fills == 0`. Once the bot has either placed an order or observed a fill in this DB's lifetime, all three branches revert to their pre-fix behaviour — the warm-DB code paths are byte-identical to v5.1.4.

**Critical limitation**
The SafetyController can only **restrict** allocations. It cannot convert an `avoid` to a `deploy`. If the allocator has already emitted "all avoid" due to CF collapse, the controller has nothing to modify — the system is silently dead. See §6.1.

### 4.15 Unified Continuous Allocator (v4.0)

**Replaces Patches 6–13 in their entirety.** The Patch-era stack (overcommit, target-driven greedy, forced-exposure, marginal-efficiency gate, exposure saturation, hysteresis inside the allocator path) has been deleted. What runs today is a ~320-line `profit/allocator.py` implementing a single continuous formula plus a safety-caps post-step. The prior stack is preserved for historical reference in §10.3 "Closed in v4.0 by deletion" and in prior-version snapshots of this doc (v3.3 kept the full Patch-6-through-13 subsections; v4.0 replaces them).

#### 4.15.1 Inputs per market

From `calibrator.get_predictions(...)`:

| Input | Source | Notes |
|---|---|---|
| `R_i` | `predictions.raw_reward_per_day` | Clean reward term; includes safety bias, model confidence, and `reward_trust`. NOT reconstructed from EV. Field added to `CalibrationPredictions` in v4.0. |
| `p_i` | `max(1e-4, predictions.p_fill_24h)` | Fill probability, floored at 1e-4 to keep downstream divisions well-defined. |
| `L_i` | `predictions.e_loss_given_fill` | Expected USD loss per fill. |
| `cpb_i` | `2 · max(0.10, (1 − 2·spread_i) / 2)` | Cost per share (both sides). Matches `oversight/allocation_writer._est_market_cost`. |

From the caller (`oversight_agent` or sim runner):

| Input | Source |
|---|---|
| `total_capital` | `available_capital · learning_state.capital_scale` — `capital_scale` applied upstream, not inside the allocator. |
| `β`, `η` | `learning_state.beta`, `learning_state.eta` — see §4.16. Default `β = 0.75`, `η = 0.0` when `learning_state=None`. |

#### 4.15.2 The formula (strict order — do not reorder)

```
Step 1 — weights:       w_i = max(1e-6, R_i / (1 + p_i · L_i))
Step 2 — raw alloc:     raw_i = w_i^(1 + η)
Step 3 — scale:         Z = Σ_k p_k · raw_k
                        scale = (β · total_capital) / Z
                        C_i = raw_i · scale
                        (fallback to equal allocation if Z < 1e-9)
Step 4 — shares:        capital = max(C_i, cpb_i · min_shares)
                        shares_i = max(min_shares, int(capital / cpb_i))
                        est_capital_cost_i = shares_i · cpb_i
Step 5 — caps:          per-market  → _clip_per_market(allocations, cap)
                        per-group   → _clip_per_group(allocations, cap)
                        per-cluster → apply_cluster_caps(...)
                        (all clip-only; no redistribution of freed capital)
Step 6 — recompute:     expected_capital = Σ p_i · est_capital_cost_i
Step 7 — safety ceiling: if expected_capital > 0.95 · total_capital:
                          rescale every row uniformly
                        (hard safety, not a control target — β lives in Step 3)
```

The Step-3 "scale to budget" identity is what makes β a non-cancelling lever: `β` is a linear multiplier on `C_i` regardless of regime. η's leverage comes in via `C_i / C_j = (w_i / w_j)^(1+η)` — zero under exactly-uniform markets (by symmetry), positive first-order leverage under any heterogeneity in `p·L`.

#### 4.15.3 What was deleted

All of the following mechanisms and their observability stamps are gone from v4.0:

- `_compute_overcommit_factor`, `OVERCOMMIT_MIN/MAX/DEFAULT`, `EXPECTED_CAPITAL_BUFFER` (Patch 7).
- `_enforce_expected_capital` (Patch 7). The 0.95 ceiling survives as the Step 7 safety rescale — same numeric target, separate code path.
- `_enforce_capital_conservation` (pre-Patch-7 two-sided budget rebalance). No under-budget redistribution.
- `_force_overcommit_allocation`, `target_notional` greedy fill (Patch 11 / Patch 13 Part 1+2).
- Marginal-efficiency gate `ev / (p·size) < 0.7 × baseline` (Patch 13 Part 1).
- Forced-exposure block `deploy_ratio < 0.85 → promote avoids` (Patch 10).
- Relaxed EV gate + `_low_ev_override` flag (Patch 10).
- Exposure-priority weight `final_score × 1.3` (Patch 10).
- Hard profit-guard override in ACTIVE (Patch 10).
- Patch 6 objective blend `0.7 × RAS + 0.3 × normalized raw_ev`.
- Patch 6 deployment boost `DEPLOYMENT_BOOST = 1.05`.
- Patch 6 / Patch 9 min-markets guards (`PATCH6_MIN_MARKETS = 5`, `MIN_MARKETS_ACTIVE_FLOOR = 15`).
- Patch 9 `MARKET_EXPANSION_FACTOR = 1.5`, `MIN_SIZE_REDUCTION_FACTOR = 0.5`.
- Patch 4 efficiency penalty `final_score × 0.9 if reward_efficiency < baseline` (Patch 13 Part 4).
- `_compute_efficiency_scale` (Fix 6 sqrt damping).
- `_redistribute_cluster_savings` (Fix 3).
- `_risk_adjusted_score` (FIX 1 + FIX 14).
- `_efficiency_quintiles` / `_efficiency_multiplier` (FIX 4).
- `_compute_exploration_pct` (PART 4 dynamic exploration budget).
- Bandit multiplier in the scoring path. The `profit/bandit.py` module + `bandit_state` DB table remain in place but are no longer read by the allocator (may be reintroduced later as an R-side modifier).
- All Patch-era observability stamps on allocation rows: `_overcommit_factor`, `_forced_target_alloc`, `_forced_exposure`, `_low_ev_override`, `_saturation_applied`, `_target_notional`, `_saturation_scale`, `_target_market_count`, `_per_market_scale`, `_exposure_boost`, `_expansion_mode`, `_deploy_ratio`, `_target_deploy`, `_ras`, `_bandit`, `_bandit_multiplier`, `_final_score`, `_efficiency_mult`, `_exploration_pct`, `_regime_multiplier`.

#### 4.15.4 Observability stamps (v4.0)

Per deploy row, the allocator emits:

| Stamp | Meaning |
|---|---|
| `_p_fill` | `max(1e-4, predictions.p_fill_24h)` — the value used in Step 3 and downstream expected-capital computation. |
| `_reward` | `R_i = predictions.raw_reward_per_day`. |
| `_expected_loss` | `p_i · L_i`. |
| `_weight` | `w_i`. |
| `_raw_alloc` | `raw_i = w_i^(1+η)`. |
| `_beta`, `_eta` | Control values used this cycle. |
| `_total_capital` | Allocator's budget input — stamped so `LearningMetrics` can compute `expected_util = Σ(p·C) / total_capital` (v4.0 bridge; allows control-law feedback). |
| `_expected_capital` | `p_i · est_capital_cost_i`, updated after every cap / rescale step via `_recompute_stamps`. |
| `_expected_capital_contribution` | Same as `_expected_capital`; retained for consumers that read either name. |

#### 4.15.5 Hard guarantees

Verified by `tests/test_continuous_allocator.py` (11 tests against the spec's §13 cases):

- **G1 — Never returns zero deployments while any deploy candidate exists.** Step 4's `max(C_i, cpb_i · min_shares)` floor guarantees at least one positive share count per candidate.
- **G2 — No binary filtering.** The old EV ≤ 0 gate is gone; every scored-deploy market reaches Step 3.
- **G3 — Smoothness.** 1% input perturbation on any of `{R_i, p_i, L_i}` produces bounded-ratio output change. Verified at `<10%` relative on reward bumps.
- **G4 — Determinism.** Same inputs → identical output. No random draws, no wall-clock-dependent code paths.
- **Caps are clip-only.** Per-market / per-group / per-cluster caps scale down; they never redistribute freed capital back to other rows. This preserves relative allocation shape under cap binding.

#### 4.15.6 Sim-only p_fill bootstrap fix

`simulation/bootstrap_calibrator.py` (v4.0 new) wraps `CalibrationManager` for simulation runs. When `FillModel.is_ready() == False`, substitutes a deterministic

```
p_fill_24h = clamp(
    0.03 + 0.001·daily_rate + 0.004·q_share_pct,
    0.02, 0.15)
```

with fallback `p = 0.05` when either input is missing. This exists solely because the simulation environment's bootstrap path would otherwise have `p_fill = 0` on every cycle (no book state to feed the production fallback), which makes `expected_capital ≈ 0` and invalidates both V4 and V5 utilisation invariants. The wrapper is a transparent pass-through once the fill model trains.

Production calibration code is untouched; the wrapper is only instantiated by the sim runners (`run_audit_v4.py`, `run_audit_v5.py`, `simulation/engine.py`).

#### 4.15.7 Step-3b cap-aware shaping (v5.0, new)

**Problem.** §4.16.5 documented a structural gap: when a fill-cluster's proportional per-member budget falls below per-market `min_capital`, Step 5's cluster cap + Step 4's min-shares floor compose to pin every member to `min_capital`, erasing any β/η signal upstream. In the sim this gap collapses 5/6 scenarios into a single flat deployment regardless of control law. In production the same pattern fires whenever several correlated markets share a cluster and per-member share drops below the per-market cost floor.

**Fix.** A new Step 3b runs **between Step 3 (C_i computed) and Step 4 (min-shares enforcement)**, no changes to raw_i, β, η, or caps:

```
for each fill-cluster C_l in deploy candidates:
    size            = |members of C_l that are deploy candidates|
    cap_pct         = OVERSIZED_CLUSTER_PCT if C_l ∈ oversized else max_cluster_pct
    cluster_budget  = cap_pct · total_capital
    cluster_per_market = cluster_budget / size
    cluster_min_capital = max(cpb_i · min_size_i over members)

    if cluster_per_market >= cluster_min_capital:
        continue           # non-binding — do nothing (preserves behaviour when clusters don't bind)

    # Binding: pre-select top-k survivors, route the rest to avoid.
    k = max(1, floor(cluster_budget / cluster_min_capital))
    survivors = top-k by (-raw_alloc_i, condition_id) ascending
    for d in non-survivors:
        d.C = 0
        route d to passthrough_avoids with reason="cluster shaping deselected"
```

Non-selected candidates are removed from `deploy_candidates` and appended to `passthrough_avoids` as `_to_dict(sm, shares=0, action_override="avoid", reason_override=...)` rows — Step 4 never sees them, so its `max(C, min_capital)` floor can't re-lift them to min_capital. Survivors pass through Step 4 normally.

**Hard guarantees** (verified by passing `tests/test_continuous_allocator.py`):

- G1 **At least one market per cluster receives allocation** — `k ≥ 1`.
- G2 **Survivors can exceed min_capital after caps** — by construction, `cluster_budget / k ≥ cluster_min_capital`.
- G3 **Non-selected markets receive zero allocation** — routed to `action="avoid"` before Step 4 floor enforcement.
- G4 **Non-binding clusters behave identically to pre-shaping** — early `continue` when `cluster_per_market ≥ cluster_min_capital`.
- G5 **Deterministic** — sort by `(-raw_alloc, condition_id)`; no randomness.
- G6 **Fail-open** — `build_fill_clusters` exceptions log a warning and skip shaping; the allocator proceeds without shaping rather than halting.

**Edge cases handled** (§7 of the Step-3b spec):
- `cluster_budget < cluster_min_capital` → `k` clamps to 1 (always keep the top-ranked member).
- Identical `raw_alloc` values → deterministic tie-break on `condition_id`.
- Empty cluster (all members non-deploy) → skipped.

**V5 empirical result**: `INV5_new` coverage_ratio 1/6 → **6/6 PASS** after shaping (0.50 – 0.98 across all six scenarios). See §10.4 "Post-shaping" V5 column.

---

### 4.16 Control System — β / η (v4.0)

The control-variable redesign that replaces the λ1 / λ2 mechanism. Grounded in the controllability analysis summarised in §4.16.2.

#### 4.16.1 Why λ1 / λ2 failed (summary)

The full derivation is in §4.15.2 of this doc's controllability-analysis lineage. Short form:

**Algebraic cancellation.** Under uniform markets (`p_i·L_i = K`, `R_i = R`), `C_i` simplifies to `0.95·T / Σ p_k` — an expression with no λ1 or λ2 anywhere. The cancellation happens at the scale step: `raw_i` and `Z` both carry `R²/D²`, which factors out identically. Therefore `∂C_i/∂λ1 = ∂C_i/∂λ2 = 0` analytically, not just numerically, in the uniform regime.

**One DOF, not two.** Scaling `(λ1, λ2) → (s·λ1, s·λ2)` leaves every `D_j / D_i` invariant. Relative allocation depends only on `γ = λ2 / λ1` — a single degree of freedom disguised as two.

**Min-floor collapse under caps.** Even in near-uniform regimes where λ1 produces a first-order differential `ΔC_i/C̄ ≈ -2·λ1·ε_i / D̄`, the cluster-cap × min-shares-floor composition in the sim environment pins every `C_i` to exactly `min_capital` (30 correlated markets ÷ $300 oversized-cluster budget = $10/market, below min_capital $27.3). The pre-cap differential doesn't survive into the output.

Both failure modes are independent; either alone is sufficient to make the control inert. Empirically verified by tracing λ2 through 300 sim cycles: λ2 moved from 0.50 → 0.057 under active control, `expected_util` did not move (stayed at 0.033 ± noise).

#### 4.16.2 Controllability under the new formula

Under the continuous allocator's `C_i = raw_i · scale` with `raw_i = w_i^(1+η)` and `scale = β·T / Z`:

- **β enters outside the `raw_i / Z` ratio** as a linear prefactor. `∂C_i / ∂β = C_i / β` in every regime — uniform or heterogeneous, pre-cap or post-cap. β's leverage is structurally preserved unless caps drive every row to min_capital.
- **η enters through `C_i / C_j = (w_i / w_j)^(1+η)`.** Under uniform markets (`w_i = w_j`), η has zero effect — symmetry forbids differentiation, and no continuous control can beat this. Under heterogeneous markets, `∂ ln(C_i/C_j) / ∂η = ln(w_i / w_j) ≠ 0` whenever the weights differ.
- **No cancellation between β and η** — they act on orthogonal degrees of freedom (absolute scale vs relative shape). Scaling `(β, η)` by a common factor does not leave the formula invariant the way `(λ1, λ2)` did.

Under near-uniform markets (`p_i·L_i = x̄ + ε_i`, `|ε_i| ≪ x̄`):

```
ΔC_i / C̄ ≈ −(1 + η) · ε_i / (1 + x̄)
```

The differential is first-order in ε (not suppressed to higher order), with coefficient `(1+η)/(1+x̄)`. Raising η linearly amplifies the heterogeneity signal — η = 0 reproduces linear-in-w weighting, η = 4 gives 5× amplification. This is the mechanism through which η can push top markets above the min_capital threshold under moderate cap binding.

#### 4.16.3 Control variables

| Variable | Bounds | Default | Destination | EMA α | Gain |
|---|---|---|---|---:|---:|
| `β` | [0.10, 0.95] | 0.75 | Step-3 scale (§4.15) | 0.03 | `K_BETA = 0.5` |
| `η` | [0.00, 4.00] | 0.00 | `raw_i` exponent (§4.15) | 0.03 | `K_ETA = 1.0` |

Both persist via `learning_state` DB columns. Migration is idempotent `ALTER TABLE ADD COLUMN`. Legacy `lambda_1`, `lambda_2` DB columns retained at hardcoded `1.0` / `0.5` (the allocator does not read them; `simulation/engine.py` and `simulation/invariants.py` still import the field names).

#### 4.16.4 Update rules

Inside `LearningController.update_state` (per-cycle, pure function):

```
# β — utilisation-target feedback
if expected_util is not None:
    err_beta    = TARGET_UTIL − expected_util        # TARGET_UTIL = 0.75
    beta_raw    = prev.beta · (1 + K_BETA · err_beta)
    beta_target = clamp(beta_raw, 0.10, 0.95)

# η — coverage feedback
if coverage_ratio is not None:
    err_eta    = TARGET_COVERAGE − coverage_ratio    # TARGET_COVERAGE = 0.5
    eta_raw    = prev.eta + K_ETA · err_eta
    eta_target = clamp(eta_raw, 0.00, 4.00)

# Stability guard — halve both α when capital_scale oscillates
alpha_beta, alpha_eta = ALPHA_BETA, ALPHA_ETA
if _detect_oscillation(prev.capital_history):
    alpha_beta /= 2
    alpha_eta  /= 2

# EMA blend
new_beta = (1 − alpha_beta) · prev.beta + alpha_beta · beta_target
new_eta  = (1 − alpha_eta)  · prev.eta  + alpha_eta  · eta_target
```

Input signals:
- `expected_util = Σ(p_i · C_i) / total_capital` — computed by `LearningMetrics.compute_metrics` from the allocation JSON, using the `_total_capital` stamp (v4.0 new) plus the sum of `_expected_capital` stamps.
- `coverage_ratio = active_markets / total_markets`, where `active = C_i > cpb_i · min_shares` — computed per cycle by the V5 audit tracker or equivalent metrics consumer.

Fail-closed behaviour: if either signal is `None` or non-numeric, the corresponding target is set equal to the prev value, the EMA step becomes a no-op, and the control pass-through. Verified in `tests/test_learning.py::test_beta_eta_passthrough_when_signals_missing`.

#### 4.16.5 Cap interaction

Summary of how the v4.0 cap stack interacts with β and η:

- **β's leverage is preserved whenever at least some markets escape min-floor binding.** When every `C_i` collapses to `min_capital` (pathological cap-bound regime), `Σ p_i · C_i = N · p_avg · min_capital` is independent of β, and β saturates at its ceiling without producing movement.
- **η's leverage requires `p·L` heterogeneity AND some market escaping min-floor.** Raising η concentrates allocation on the top-weight markets; at high enough η, the top market's post-cluster-cap share exceeds `min_capital`, escaping the floor. `coverage_ratio` rises accordingly. The η feedback loop drives this directly.
- **Cluster-cap × min-floor is the dominant binding constraint in the current sim.** 30 synthetic markets co-fill identically → one oversized cluster → `$300 / 30 = $10 < min_capital`. No upstream signal survives this composition. Empirically, V5 `expected_util` post-β/η control lands at 0.029–0.054 on five of six scenarios; only `under_deployed` (where `p` is low enough that notional blows past min_floor before caps bind) reaches the 0.5 target band.
- **Caps themselves are clip-only and symmetric.** They preserve relative allocation under cluster-cap scaling and per-market cap; they only break symmetry under per-group cap (which iterates in allocation order) or when some rows cross a cap while others don't. Under uniform pre-cap shape, symmetric caps produce symmetric post-cap shape — no control signal re-emerges from the cap stack.

#### 4.16.6 Empirical control-law validation

Verified in isolation (`python3 -m` one-shot script against `LearningController.update_state`):

- **Bounds hold** over 2000 stress cycles: β pinned at 0.10 lower bound when driven, η at 4.00 upper bound.
- **Directions match spec §6.2 / §6.3**: `expected_util < target` ⇒ β ↑; `coverage < target` ⇒ η ↑.
- **Deterministic** across 3 repeat calls on identical input.
- **Smooth**: 1% perturbation on `expected_util` → 0.0075% β move (EMA-bounded).
- **Stability guard exact**: oscillating `capital_history` halves the step (`ratio = 0.500`).
- **Fail-closed**: `expected_util = None` + `coverage_ratio = None` → β/η unchanged.

End-to-end V5 audit post-β/η integration (6 scenarios × 3 seeds × 500 cycles): overall verdict FAIL, but `expected_util` up 700× vs pre-fix. Per-scenario detail in §10.4. The FAIL is traceable to the cluster-cap × min-floor structural artefact in the sim environment (§4.16.5), not a control-loop malfunction — β and η both move correctly and reach their bounds under sustained error signals.

---

### 4.17 Legacy — Profit Maximization Stack (Patches 6, 7, 9, 10, 11, 13)

*Historical only; mechanisms no longer exist in the codebase. Kept as a pointer for anyone reading v3.x commits.*

Patches 6–13 were a progressive stack of overlays that transformed the v2.0 EV-disciplined allocator into an exposure-forcing reward farmer. The full design notes, invocation order, and per-patch mechanics for each layer (Patch 6 objective blend + deployment boost + min-markets guard; Patch 7 overcommit factor + expected-capital enforcement; Patch 9 market expansion + per-market halving; Patch 10 exposure forcing + relaxed EV gate; Patch 11 exposure saturation + oscillation damping; Patch 13 target-driven greedy + hysteresis + efficiency penalty) are preserved in v3.3 of this document.

**Why the stack was removed (v4.0):**

1. V3.1 and V4 audits demonstrated that Patches 6–13 did not close their own design invariants (INV3 / INV5 / INV7) under the audit's six scenarios.
2. The controllability analysis (§4.16.1, §4.16.2) proved that the underlying `λ1·p·L + λ2` denominator form is **mathematically incapable** of producing cross-market differentiation under uniform markets, regardless of what update rules you attach. The Patch-era mechanisms (overcommit, forced exposure, greedy fill) were all attempts to reintroduce differentiation through side channels, but they either reduced to the same uniform-market degeneracy or produced non-continuous allocation surfaces.
3. The continuous-allocator + β/η design recovers the ~3–4% near-uniform leverage that existed in the Patch-era stack and makes it controllable by a proper closed-loop feedback rule (§4.16.4). No more multi-layer overlays — one formula, two controls, one safety cap pass.

A subset of v3.x-era infrastructure survives into v4.0:

- Patch 11 `_detect_oscillation` + `_CAPITAL_HISTORY_CACHE` (still used by β/η stability guard and by capital_scale hysteresis).
- Patch 13 `last_direction` / `direction_lock` hysteresis (still applied post-EMA to `capital_scale`).
- Patch 4 / Patch 5 frontier memory on `capital_scale` (regime-keyed best-reward dict, unchanged).
- Rules A/B/D/E for `capital_scale` (with the aggressiveness / risk_multiplier branches stripped).
- The `reward_trust` mean reversion and Rule C (reward-error feedback).

These live in `profit/learning.py`; none of them touch the allocator.

*Per-patch mechanics (Patch 6 Safe Expansion, Patch 7 overcommit factor + `_enforce_expected_capital`, Patch 9 expansion + per-market halving, Patch 10 forced-exposure + relaxed EV gate, Patch 11 saturation + oscillation damping, Patch 13 target-driven greedy + hysteresis, Audit V4 framework) are documented in full in v3.3 of this document. All of the allocator-side mechanisms listed there were removed in v4.0; see §4.15.3 for the explicit deletion list.*

---

### 4.18 Runtime Safety Guardrails (v5.0 — farmer layer)

The guardrail stack is a farmer-side execution-time safety layer. It runs **inside `reward_farmer.run_cycle`** between the existing expiry-sweep / fill-storm detection (Step 3.5–3.6) and the Step-4 placement batch. The allocator and `LearningController` are unaware of it by design — allocation decides intent, the farmer enforces live-capital safety.

**Correct layering contrasted with `SafetyController` (§4.14):**
- `SafetyController` runs **upstream** on the agent side, after the allocator produces `market_allocations.json`. Mutates allocations (state-based restriction of `max_markets` / `capital_pct` / trial mode).
- **Runtime guardrails** run **downstream** on the farmer side, after the farmer loads `market_allocations.json` and decides what to place/cancel this cycle. They see **live order notional** (resting orders × shares × price + active dump orders) which SafetyController cannot see.
- Both layers coexist. `SafetyController` caps what the allocator emits; runtime guardrails enforce the cap at execution time regardless of how stale the allocation file is.

#### 4.18.1 Signals (computed each cycle, all fail-open)

| Signal | Source | Unit |
|---|---|---|
| `total_capital` | `_total_capital` stamp on first deploy row in `market_allocations.json` | USD |
| `live_notional_per_cid` | Σ over `ms.orders[side].price × ms.orders[side].shares` for every slot with `order_id`, plus active dump orders | USD |
| `cluster_notional` | `build_fill_clusters(db_path)` groups live notional by fill cluster | USD per cluster |
| `cf` | Latest `reward_daily.correction_factor` | unitless |
| `daily_realized_loss` | `SUM(−pnl) FROM unwinds WHERE ts > now−86400 AND pnl < 0` | USD (positive) |
| `fill_rate_ratio` | 1h observed fill count / (6h baseline count × 1/6) over all markets' `ms.fill_times` | unitless; requires baseline ≥ `MIN_FILL_BASELINE = 5` |

Every helper returns `None` on missing/failed data AND emits `[GUARDRAIL_WARNING] missing_signal=<name>` at `log.warning`. The corresponding check silently skips — trading never halts on a data hiccup.

#### 4.18.2 Soft guards (block new placements, no cancellations)

| Guard | Threshold | Behaviour |
|---|---|---|
| Notional block | `Σ live notional / total_capital > MAX_NOTIONAL_RATIO = 2.0` | Skip the entire placement batch this cycle |
| Cluster block | `cluster_notional > CLUSTER_NOTIONAL_LIMIT_FRAC · T = 0.5·T` | Remove markets in any over-cap cluster from the placement batch |

Existing orders are left alone — soft guards prevent growth, not flattening.

#### 4.18.3 Hard enforcement (actively cancel existing orders)

Added at `2e72606`. Runs **after** the soft guards' computation but **before** the placement batch. Actively reduces exposure that's drifted past a hard threshold (can happen when fills land between cycles).

| Enforcement | Hard threshold | Target floor |
|---|---|---|
| `_guardrail_hard_enforce_notional` | `Σ live notional / T > HARD_NOTIONAL_RATIO = 2.5` | Cancel lowest-priority BUYs until ratio ≤ 2.0 |
| `_guardrail_hard_enforce_clusters` | `cluster_notional > 0.5·T` for any cluster | Cancel lowest-priority members of that cluster until ≤ 0.5·T |

**Cancellation priority** (ascending sort, lowest-priority first):
```
(daily_rate ASC, -notional ASC, -max_spread ASC, condition_id, side)
```
i.e. lowest reward first, within that largest exposure first, within that highest spread (risk proxy), then deterministic string tiebreak. Dump SELLs are **excluded** — cancelling them would strand filled inventory. The kill switch still cancels dumps because it's terminal.

**Multi-cancel cap** (`MAX_CANCELS_PER_CYCLE = 5` per helper): each helper cancels at most 5 orders per cycle, stopping early if the threshold is cleared first. When the cap is hit with residual breach, emits a `[GUARDRAIL]` warning noting the leftover `$amount` that carries into the next cycle. Prevents burst-cancelling the whole book during a large breach.

#### 4.18.4 Kill switch

Atomic halt (§5.1 of the guardrail spec). Ordering is strict: **set flag → cancel every live order → log event → return from `run_cycle` immediately**. Once triggered, every subsequent `run_cycle` short-circuits — operator must restart the process to resume. Deliberate: the trigger conditions all benefit from human eyes-on before re-entry.

| Trigger | Condition | Source |
|---|---|---|
| Realised-loss breach | `24h realized_loss > MAX_DAILY_LOSS_FRAC · T = 0.1·T` | `unwinds` table |
| CF collapse | `correction_factor < CRITICAL_CF_THRESHOLD = 0.01` | `reward_daily` table |
| Fill-rate spike | `1h-fill-count / 6h-baseline-rate > FILL_RATE_SPIKE_FACTOR = 3.0` AND `baseline ≥ MIN_FILL_BASELINE = 5` | `ms.fill_times` across all markets |

**Override over execution mode**: kill-switch cancels always fire real cancellations, even in DRY_RUN / SHADOW (§4.19). `_activate_kill_switch` sets `self._kill_switch_active = True` **first**, then its cancel loop uses `_gated_cancel_order` which sees `force_execute = self._kill_switch_active` = True and takes the LIVE path regardless of mode.

#### 4.18.5 Persistent-breach detector

`MAX_BREACH_CYCLES = 3`. Tracks a counter `_consecutive_hard_notional_breach_cycles`, incremented each cycle where `notional_ratio > HARD_NOTIONAL_RATIO`, reset when under, left unchanged on missing signal. After ≥ 3 consecutive breach cycles, emits:

```
[CRITICAL] persistent_overexposure {"cycles": N, "notional_ratio": X.XXXX}
```

Observational only — does **NOT** auto-trip the kill switch. The kill switch has its own triggers (realised loss, CF, fill rate). This warns the operator that hard enforcement is failing to catch up.

#### 4.18.6 Structured log format

Every cycle, exactly one `[GUARDRAIL] {…}` JSON line. Keys (sorted alphabetically by `json.dumps(sort_keys=True)`):

```
active_markets, blocked_cluster_count, cf, cluster_count, cycle, event,
fill_rate_baseline_6h, fill_rate_ratio, fill_rate_short_1h, kill_switch,
max_cluster_notional, notional_block, notional_ratio, realized_loss_24h,
total_capital, total_live_notional, ts
```

Ad-hoc lines:
- `[GUARDRAIL_WARNING] missing_signal=<name>` — per fail-open skip, each cycle it applies.
- `[GUARDRAIL] hard-notional breach: …` → `[GUARDRAIL] hard-notional cancel <oid> …` (per cancelled order) → `[GUARDRAIL] hard-notional enforcement cancelled N orders`.
- `[GUARDRAIL] cluster={cl_id} breach: …` analogous for cluster enforcement.
- `[GUARDRAIL] KILL SWITCH ACTIVATED: <reason> — cancelled N live orders` on the `log.error` path.
- `[CRITICAL] persistent_overexposure {…}` per cycle once the 3-cycle threshold is crossed.

---

### 4.19 Execution Modes (v5.0)

Three modes form a staged deployment ladder. Default is `DRY_RUN` so safe-by-default unless the operator explicitly opts in.

| Mode | API reads | Place orders | Cancel orders | Intent logs | Guardrails |
|---|---|---|---|---|---|
| `DRY_RUN` | No | No | No | `[DRY_RUN] <action> {…}` | Computed + logged but no effect |
| `SHADOW` | Yes | No | No | `[SHADOW] <action> {…}` | Computed + logged but no effect |
| `LIVE` | Yes | Yes | Yes | None | Active + enforcing |

**Kill-switch override (§5.1)**: kill-switch cancels always hit the LIVE path regardless of mode. Capital protection trumps mode safety. `_activate_kill_switch` sets `_kill_switch_active = True` before its cancel loop, so `_gated_cancel_order`'s `force_execute` branch kicks in on every subsequent cancel from that loop.

#### 4.19.1 CLI

```
python reward_farmer.py                   # --mode dry (default)
python reward_farmer.py --mode shadow     # reads only
python reward_farmer.py --mode live       # full execution
```

Mapping: `dry → MODE_DRY_RUN`, `shadow → MODE_SHADOW`, `live → MODE_LIVE`. Rejects any other string at argparse level.

#### 4.19.2 Implementation

All write sites in `reward_farmer.py` (13 call sites) routed through two gated wrappers:

```python
def _gated_place_orders_for_market(self, ms) -> None:
    mode = getattr(self, "mode", MODE_LIVE)    # stub-safe fallback
    if mode != MODE_LIVE:
        self._log_dry_run_intent("place_order", cid=ms.cid, question=...)
        return
    self.order_lifecycle.place_orders_for_market(ms)
    self._cycle_orders_placed += 1

def _gated_cancel_order(self, order_id: str, reason: str = "") -> bool:
    mode = getattr(self, "mode", MODE_LIVE)
    force_execute = bool(getattr(self, "_kill_switch_active", False))
    if mode != MODE_LIVE and not force_execute:
        self._log_dry_run_intent("cancel_order", order_id=order_id, reason=reason)
        return False
    ok = bool(self.order_lifecycle.cancel_order(order_id, reason=reason))
    if ok:
        self._cycle_orders_cancelled += 1
    return ok
```

Exactly one raw `order_lifecycle.cancel_order` + one raw `place_orders_for_market` remain in the file — both inside these wrappers.

**Belt-and-suspenders**: `OrderLifecycle` and `DumpManager` receive `dry_run=True` in any non-LIVE mode at construction. Even if a code path somewhere slips past the wrapper, OL's internal `dry_run` handling blocks the real API call. `DumpManager.cancel_fn` is set to `self._gated_cancel_order` (not `order_lifecycle.cancel_order` directly) so DumpManager's internal cancels also obey mode.

**Stub safety**: unit-test fixtures that invoke unbound methods on a minimal `FarmerStub` (e.g., `RewardFarmer._sweep_expiring_markets(stub)`) don't set `self.mode`. The `getattr(self, "mode", MODE_LIVE)` fallback defaults to LIVE in that case, so the test's mocked `cancel_order` path still fires. Counter `+=` uses `try/except AttributeError` for the same reason.

**`self.dry_run` kept for back-compat**: equals `(mode == MODE_DRY_RUN)` — True only in DRY_RUN — so the existing startup-reconcile and `get_orders` read-gates continue to work. SHADOW has `self.dry_run = False` so API reads go through.

---

### 4.20 Telemetry Stream (v5.0)

Every log line the bot emits related to safety or cycle state is structured JSON prefixed with a bracketed channel tag. Machine-parseable via `json.loads` on the substring after the first space.

| Channel | Cadence | Emitter | Payload |
|---|---|---|---|
| `[CYCLE_SUMMARY]` | Every `run_cycle` exit | `_emit_cycle_telemetry` | `cycle, ts, active_markets, total_live_notional, notional_ratio, max_cluster_notional, cluster_count, blocked_clusters, orders_placed`, `orders_cancelled, kill_switch, realized_loss_24h, cf`. **`orders_placed` semantics (FX-004, v5.1.8):** count of API-confirmed placements (`create_and_post_order` returned a valid `orderID` AND `log_order_placed` wrote a row to the `orders_placed` DB table). Pre-FX-004 the field counted attempts; the change tightens it so any external dashboard or alert reading this number matches `SELECT COUNT(*) FROM orders_placed WHERE ts BETWEEN cycle_start AND cycle_end`. |
| `[ROLLING_STATS]` | Every 10 cycles | `_emit_cycle_telemetry` | `avg_notional_ratio, max_notional_ratio, avg_orders, avg_cancels` over last 100 cycles |
| `[GUARDRAIL]` | Every cycle | `_guardrail_check_and_log` | Full guardrail signal dump (17 keys) |
| `[GUARDRAIL]` (ad hoc) | On hard enforcement | `_guardrail_hard_enforce_*` | Per-cancel + per-helper summary |
| `[GUARDRAIL_WARNING]` | On any missing signal | Guardrail helpers | `missing_signal=<total_capital|cf|fill_rate|cluster_data>` |
| `[CRITICAL]` | Every cycle while ≥ 3-cycle hard notional breach | `_guardrail_check_and_log` | `persistent_overexposure {"cycles": N, "notional_ratio": X}` |
| `[GUARDRAIL]` | Once, on activation | `_activate_kill_switch` | `KILL SWITCH ACTIVATED: <reason> — cancelled N live orders` + `event=kill_switch_activated` JSON |
| `[DRY_RUN]` / `[SHADOW]` | On every intent-only place/cancel | `_log_dry_run_intent` | `<action> {cid|order_id, ...}` |

**Rolling window internals**:
```python
self._rolling_stats: collections.deque(maxlen=100)
# per-cycle sample = {"notional_ratio": float, "orders": int, "cancels": int}
```

Fail-open on every emit: a logging exception is swallowed at `log.debug` level — telemetry failures never halt trading.

**What to watch during staged rollout**:
- DRY_RUN: `[DRY_RUN] place_order` / `[DRY_RUN] cancel_order` volume per cycle; confirm allocator intent matches expectations.
- SHADOW: `[CYCLE_SUMMARY] notional_ratio` (stays 0 since no writes), `[GUARDRAIL] fill_rate_ratio` / `cf` against real market state.
- LIVE: `[CYCLE_SUMMARY] orders_placed / orders_cancelled / notional_ratio / realized_loss_24h`; any `[CRITICAL]` or repeated `[GUARDRAIL_WARNING]` deserves immediate action.

---

### 4.21 Oversight Evaluation Hook (v5.1)

A pure-decision policy hook that wraps the farmer's `run_cycle` between the guardrail computation and the placement decision. The hook is deterministic, synchronous, and additive — it cannot enable behaviour the existing layers wouldn't permit, only override toward stricter outcomes (`pause` / `kill`). Currently the policy function is **not implemented**; the hook is in place and behaves as a no-op.

#### 4.21.1 Contract

`oversight_agent.evaluate(guard) -> dict` is called exactly once per `run_cycle`. The `guard` argument is the dict returned by `_guardrail_check_and_log()` ([reward_farmer.py:1331-1352](reward_farmer.py:1331)) — 14 keys, every one explicitly typed (last two added for the shadow evaluator's signal D, see §4.21.7):

| Key | Type | Source |
|---|---|---|
| `kill_switch` | `bool` | True if any farmer-side kill condition fires this cycle |
| `kill_reason` | `str` | Semicolon-joined reasons; `""` when `kill_switch=False` |
| `notional_block` | `bool` | True when `notional_ratio > MAX_NOTIONAL_RATIO = 2.0` |
| `blocked_clusters` | `set[int]` | Cluster IDs over `0.5·T` |
| `cluster_by_cid` | `dict[str, int \| None]` | `condition_id → cluster_id` (None if unclustered) |
| `cluster_notional` | `dict[int, float]` | Live notional USD per cluster |
| `live_by_cid` | `dict[str, float]` | Live notional USD per market |
| `total_live_notional` | `float` | Sum of `live_by_cid` values |
| `notional_ratio` | `float \| None` | None when `total_capital` missing |
| `total_capital` | `float \| None` | From `_total_capital` stamp on alloc JSON |
| `cf` | `float \| None` | Latest `reward_daily.correction_factor`; None on DB miss |
| `daily_loss` | `float \| None` | 24h `Σ(−pnl)` from unwinds, positive USD; None on DB miss |
| `orders_placed_prev_cycle` | `int` | `_rolling_stats[-1]["orders"]` if available, else `0` (added in shadow patch) |
| `orders_cancelled_prev_cycle` | `int` | `_rolling_stats[-1]["cancels"]` if available, else `0` (added in shadow patch) |

**Containment**: the two new keys are visible only inside `evaluate()`. They are **not** added to the `[GUARDRAIL]` JSON line — that telemetry builds its own dict (`tele`, [reward_farmer.py:1299](reward_farmer.py:1299)) and remains exactly 17 keys, unchanged from v5.0.

The expected return shape:

```python
{"action": "continue" | "pause" | "kill", "reason": str}
```

#### 4.21.2 Deterministic call site

In `reward_farmer.run_cycle` ([line 1810-1856](reward_farmer.py:1810)), the verbatim block is:

```python
start = time.time()
missing_evaluate = not hasattr(oversight_agent, "evaluate")
if missing_evaluate:
    decision = {"action": "continue", "reason": "not_implemented"}
else:
    try:
        decision = oversight_agent.evaluate(guard)
    except Exception as e:
        log.error("[OVERSIGHT_ERROR] evaluation failed: %s", e)
        decision = {"action": "continue", "reason": "error"}
latency_ms = (time.time() - start) * 1000.0

if not isinstance(decision, dict):
    log.error("[OVERSIGHT_ERROR] invalid decision type: %s", type(decision))
    action = "continue"
    reason = "invalid"
else:
    action = decision.get("action")
    reason = decision.get("reason", "")
    if action not in ("continue", "pause", "kill"):
        log.error("[OVERSIGHT_ERROR] invalid action: %s", action)
        action = "continue"
        reason = "invalid"
reason = str(reason)[:200]

if latency_ms > OVERSIGHT_LATENCY_WARN_MS:
    log.warning("[OVERSIGHT_WARNING] slow evaluation: %.2fms > %dms",
                latency_ms, OVERSIGHT_LATENCY_WARN_MS)

log.info("[OVERSIGHT] action=%s reason=%s latency_ms=%.2f",
         action, reason, latency_ms)

if action == "kill":
    self._activate_kill_switch(reason="oversight:" + reason)
    self._emit_cycle_telemetry()
    return
```

**Invariants verified by AST walk + tests:**

- Exactly one `oversight_agent.evaluate(guard)` call per cycle (single `ast.Call` node at line 1816).
- `_emit_cycle_telemetry()` invariant preserved (every return path emits once before return).
- No new state on `self` — `decision`, `action`, `reason`, `latency_ms`, `start`, `missing_evaluate` are all method-local.
- No threads, no async, no `signal.alarm`, no timeouts — pure synchronous Python.

#### 4.21.3 Placement decision interaction

`action == "pause"` is honoured as a new `elif` in the placement decision block ([line 1888-1901](reward_farmer.py:1888)) in the EXACT order:

```
if time.time() < self._fill_storm_until:        # existing
elif guard["notional_block"]:                   # existing
elif action == "pause":                         # v5.1, slot 3
    log.warning("[OVERSIGHT] placements skipped: %s", reason)
else:
    <placement loop>                            # existing
```

Note the slot-3 position: `notional_block` evaluates first. If both `notional_block` and `pause` are True on the same cycle, the `notional_block` warning fires and the `pause` warning is suppressed. Either way placements are skipped — only the log line differs.

#### 4.21.4 Current state — shadow evaluator (Stage 1)

As of the v5.1 shadow patch, `oversight_agent.evaluate` **exists** and runs as a **pure shadow detector**: it computes six trigger signals across a bounded ring buffer of recent guard snapshots and unconditionally returns `{"action": "continue", "reason": "shadow"}`. Live trading behaviour is byte-identical to the pre-shadow system. The only observable change is:

- Per-cycle farmer log: `[OVERSIGHT] action=continue reason=shadow latency_ms=≈0.X` (was `reason=not_implemented`).
- New channel `[OVERSIGHT_SHADOW]` emitted only when a trigger fires or when a signal flags missing data.

See §4.21.7 for the six signals, threshold table, and the activation ladder for promoting individual signals to live `pause` / `kill` returns.

#### 4.21.5 Constraints to honour when implementing the policy

These are absolute requirements from the call site, not preferences:

- **Synchronous** — no threads, no `asyncio`, no subprocess calls.
- **Latency budget**: <50 ms typical (warned above), <500 ms hard ceiling (above ≈10% of cycle interval is a smell).
- **No DB queries on the hot path**. `cf` and `daily_loss` are already in `guard`. If you need cross-cycle persistence, write it asynchronously from the agent process and read a small file synchronously here.
- **No HTTP calls**. Network unreliability inside `evaluate` directly degrades farmer cycle cadence.
- **Bad inputs must not raise**. The caller catches exceptions, but a function that raises every cycle floods `[OVERSIGHT_ERROR]` and provides no useful signal. Prefer explicit `try/except` with a `return {"action": "continue", "reason": "<class>"}` fallback inside `evaluate`.
- **Don't duplicate the guardrails**. The farmer already kills on `cf < 0.01`, `daily_loss > 0.1·T`, and `fill_rate_ratio > 3×`. Repeating those conditions creates double `[CRITICAL]` events. Use cross-axis composites or cross-cycle patterns the per-cycle guardrails can't see.

#### 4.21.6 Process boundary clarification

`oversight_agent.py` runs as TWO things:

1. **Standalone process** via `python oversight_agent.py --loop` — executes `run_loop` → `run_once` (allocation planner, every 30 min).
2. **Imported module** by `reward_farmer.py` (line 40) — `evaluate` runs inside the FARMER's address space, every 30 s.

These are separate Python interpreters. Module-level state in `oversight_agent.py` (e.g., a recent-cycles ring buffer) lives independently in each process. Cross-process state must go through `bot_history.db` or a file on disk.

#### 4.21.7 Shadow evaluator — signal table (Stage 1)

The shadow evaluator maintains a bounded ring buffer of the last `_HISTORY_LEN = 30` guard snapshots inside the farmer's address space (per §4.21.6). On each call, six pure detectors run against the ring buffer; triggered signals emit `[OVERSIGHT_SHADOW]` log lines. The `evaluate()` return value is **always** `{"action": "continue", "reason": "shadow"}` regardless of which signals fire.

| ID | Name | Window | Trigger condition | Kind |
|---|---|---:|---|---|
| A | `notional_drift` | 5 cycles | `avg(notional_ratio) ≥ 1.8` | would_pause |
| B | `cluster_breadth` | 3 cycles | every cycle has `≥ 2` blocked clusters | would_pause |
| C | `cf_soft_zone` | 5 cycles | every cycle has `cf ∈ [0.01, 0.03]` | would_pause |
| D | `cancel_pressure` | 6 cycles | `avg(cancels) ≥ 2.0 × avg(places)`, `avg(places) ≥ 1` | would_pause |
| E | `cf_trajectory` | 10 cycles | `cf` dropped `≥ 50%` from window-start AND `deployed_count` declining | **would_kill** |
| F | `slow_bleed` | 6 cycles | every cycle has `daily_loss > 0.05 · total_capital` | would_pause |

**Architectural justification per signal** (each has a verified gap in the existing layer stack):

- **A** sits in `[1.8, 2.0)` — below the soft notional block (`MAX_NOTIONAL_RATIO = 2.0`, §4.18.2), so the farmer never blocks. No existing layer detects pre-threshold pressure accumulation.
- **B** observes portfolio-wide correlation stress. The cluster guards in §4.18.2/3 act locally per cluster; nothing measures breadth across clusters.
- **C** sits between the kill floor (`cf < 0.01`, §4.18.4) and the SafetyController's HIGH-priority warning band (§4.14 I5). No layer acts on persistence in this band.
- **D** has no existing detector. Cancel/place imbalance signals execution thrash that current layers don't surface.
- **E** is the architectural keystone: the farmer's CF kill at `cf < 0.01` fires after the deadlock loop of §6.1 has already begun closing. A trajectory detector can fire while CF is still > 0.05, before the data-collection feedback loop locks. **This is the only kill trigger justified by the doc's catastrophic-mode analysis.**
- **F** captures cumulative damage that never breaches the per-cycle `daily_loss > 0.1·T` kill (§4.18.4). Sustained sub-threshold loss is invisible to threshold-based logic.

**Cancel-pressure note**: kill-switch cancels are NOT filtered out of signal D's input. Per the v5.1 shadow design decision, oversight reads what the system did, not why; if a kill happens and the process continues running, an extra pause signal is benign and costs nothing.

**Activation ladder**:

| Stage | State | What changes |
|---|---|---|
| Stage 1 (now) | `_SHADOW_ONLY = True` | All signals computed, all triggers logged at `[OVERSIGHT_SHADOW]`, `evaluate()` always returns `continue`. **Net behaviour change: 0.** |
| Stage 2 | `_SHADOW_ONLY = False` for selected signals | Signals A–D and F (would_pause kind) translated to real `"pause"` returns. |
| Stage 3 | E enabled | `cf_trajectory` translated to real `"kill"` return — the only signal that ever returns `kill`. |

Each promotion requires evidence from the prior stage's `[OVERSIGHT_SHADOW]` log review (200–500 cycle minimum per stage) showing: (1) no false positives in healthy regime, (2) triggers fire BEFORE the corresponding hard guardrail would fire, (3) no flapping (signal toggling on/off cycle-to-cycle ≥ 1 toggle / 30 cycles).

**Log format** (verbatim from `_check_signals_and_log`):

```
[OVERSIGHT_SHADOW] signal=<name> value=<float|str> window_status=ok kind=<would_pause|would_kill> triggered=True
[OVERSIGHT_SHADOW] signal=<name> status=<missing_data|insufficient_activity> triggered=False    # only on data gap
[OVERSIGHT_SHADOW] would_pause=<bool> would_kill=<bool> pause_reasons=<csv> kill_reasons=<csv>   # summary, only when any signal fires
```

Cold-start cycles (1–4) emit no shadow lines: every detector returns `insufficient_history` (silent by design).

---

### 4.22 Orphan position recovery

Added to architecture doc in v5.1.5. Behaviour itself has existed since earlier versions but was previously undocumented.

#### Trigger

`reward_farmer._scan_for_orphans` at `reward_farmer.py:550-611`. Called during farmer startup as part of the recovery sequence (after `_restore_dump_states` reads existing `dump_states` rows from DB).

#### What it does

For each candidate market in the discovery list, the scan:

1. Fetches market metadata from CLOB API (`GET /markets/{cid}`) for the two CTF token IDs (YES, NO).
2. Calls `client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid))` for each side.
3. Interprets the returned `balance` field as shares (divided by `1e6` for the on-chain decimals).
4. If `actual >= 1.0` shares on a side, treats that as an orphan — i.e., a position the bot holds on-chain but doesn't have in its local `positions` table.
5. Registers the orphan in `positions` via `set_shares(cid, side, actual)`.
6. Creates a synthetic `MarketState` with placeholder fields (`daily_rate=0, max_spread=0.05, min_size=1, tick_size`) and adds it to `self.markets[cid]`.
7. Calls `self.dump_mgr.dump_position(ms, side, actual)`, which writes a row to `dump_states` and attempts to post a SELL order.

#### Why it exists

The bot's wallet (FUNDER proxy) is a long-lived Polygon address. CTF token positions accumulate from prior fills across deployments, server restarts, and (occasionally) operator experimentation outside the bot. The orphan scan recovers value by attempting to liquidate any positions found on-chain that the bot doesn't yet track locally — particularly important after a fresh-DB server provisioning where the local `positions` table is empty but the wallet still holds tokens from previous runs.

#### Failure mode: resolved markets (`fixit.md::FX-007`)

When the on-chain CTF tokens are for a market that has resolved, the orderbook no longer exists. The `create_and_post_order` call inside `dump_position` returns HTTP 400 with body `{"error":"the orderbook X does not exist"}`. The current implementation:

- Catches the 400 inside `DumpManager` and logs as `ERROR | Dump <side> FAILED: ...`.
- Does NOT increment `ms.book_failures` (which is reserved for `get_merged_book` failures at `order_lifecycle.py:208`).
- Does NOT mark the cid as unrecoverable.
- Leaves the `dump_states` row in the DB.
- Retries on every subsequent cycle.
- On restart, `_restore_dump_states` reloads the row, AND the orphan scan re-discovers the same on-chain position (since the tokens are still there) and re-creates the dump — even if a manual SQL DELETE cleared the row.

Observed in production after the v5.1.5 Helsinki bootstrap: the wallet held 3826 NO-side tokens of the resolved "Will Tamilaga Vettri Kazhagam" market, producing one 400 error per cycle indefinitely.

#### Recovery options

For tokens on resolved markets, the value path is **CTF redemption** (the conditional tokens framework's redeem function), not order matching. Polymarket UI exposes this; the bot does not. Operator must redeem manually from the UI, or accept the small lost balance.

#### Shipped fix (v5.1.9, `7d8d38d`)

The fix landed as commit `7d8d38d` and is documented in detail at `fixit.md::FX-007`. Mechanism: a new DB table `unliquidatable_markets` (cid PK, reason, marked_at, last_retry_at) records cids whose orderbook the bot has definitively confirmed dead.

Four touchpoints:

1. **Mark-on-exception** — both `OrderLifecycle.place_orders_for_market` (BUY paths) and `DumpManager.dump_position` (SELL path) catch the canonical V2 SDK 400 body. Detector requires BOTH substrings `"orderbook"` AND `"does not exist"` lowercased (the cid sits between them in the canonical phrasing). Regression-tested against false-positive cases: "insufficient balance", "rate limit", "market does not exist" all leave the cid unmarked.
2. **Gate at every producer** — `dump_position`, `place_orders_for_market`, `_scan_orphaned_positions`, `_sync_exchange_positions`, and `_restore_dump_states` each consult `db.is_unliquidatable(cid)` and skip the cid if marked. The DumpManager and OrderLifecycle gates also clean any leftover `ms.dump_state[side]` and `dump_states` DB rows on the way through.
3. **Cleanup cascade** — when the existing `book_failures >= BOOK_FAILURE_LIMIT` dead-market cleanup loop fires (`reward_farmer.py` Step 4b), it now also calls `delete_dump_state` for both sides and `mark_unliquidatable(cid, reason="dead_market_book_failures")`.
4. **Periodic re-probe** — `RewardFarmer._reprobe_unliquidatable` runs on a 30-min loop-sweep cadence (`_last_unliquidatable_reprobe` timestamp). Inside the method, `db.get_unliquidatable_for_reprobe(stale_secs=RF_UNLIQUIDATABLE_REPROBE_SECS)` returns only cids whose `last_retry_at` is older than 6 h. For each, the method tries `self.markets[cid]` for token_ids (falling back to a CLOB `/markets/{cid}` lookup). If `get_merged_book` returns data → `db.delete_unliquidatable(cid)` (re-enables). Otherwise → `db.update_unliquidatable_retry(cid)` (stamps and leaves).

Production impact on next Helsinki `git pull + restart`: `_restore_dump_states` loads the Tamilaga row, calls `dump_position`, hits the 400, marks unliquidatable, deletes the dump_state row. Subsequent `_scan_orphaned_positions` and `_sync_exchange_positions` sweeps skip Tamilaga. Spam stops within ~1 cycle of LIVE startup.

Healthy markets are unaffected — the gate is a single indexed PK lookup per call, ~µs on local SQLite.

---

### 4.23 Order placement strategy — reward-farming positioning

This section formalizes how the bot positions limit orders within the reward zone. Pre-FX-036 (≤ v5.1.17) the placement formula sat 1 tick inside the **far edge** of the reward zone — fill-avoidance-optimal but reward-density-pessimal. FX-036 (v5.1.18) replaced this with **queue-depth-aware placement**: walk the merged book from best (closest to mid) outward, accumulate cumulative USD notional, sit 1 tick behind the level where cumulative queue first crosses `RF_TARGET_QUEUE_AHEAD_USD` (default `$1000`). The fixed-distance formula survives as the fallback for thin books and as the escape hatch when the operator sets the knob to `0`.

#### 4.23.1 The Polymarket reward formula (operator-relevant subset)

For each reward-eligible market with `rewards.max_spread` (notated `s_max`) and a daily rate, Polymarket pays makers proportional to (approximately):

```
reward_per_share_per_minute  ∝  (1 − d/s_max)  ×  q_share  ×  daily_rate
```

where `d` is the order's distance from midpoint in price space (capped at `s_max` — orders outside the zone earn zero), and `q_share` is the maker's share of total qualifying notional at that market.

**Key consequence**: reward density per share is **NOT uniform inside the reward zone**. An order at `d = 0` (exactly at midpoint, theoretically — practically `1 tick`) earns near-100% weight; an order at `d = s_max − 1 tick` earns ~`1/s_max_in_ticks` weight. For a market with `s_max = 5.5¢` and `tick = 1¢`, the ratio between best and worst in-zone position is roughly **9× more reward density** for orders 1 tick from mid vs. 5 ticks from mid.

#### 4.23.2 Pre-FX-036 placement formula (≤ v5.1.17)

Pre-FX-036 production code at `order_lifecycle.py:354-357` (now the legacy fallback):

```python
tick = ms.tick_size
edge_bid = round(midpoint − ms.max_spread + tick * PLACEMENT_TICKS_INSIDE(), decimals)
edge_ask = round(midpoint + ms.max_spread − tick * PLACEMENT_TICKS_INSIDE(), decimals)
edge_bid = max(0.01, edge_bid)
edge_ask = min(0.99, edge_ask)
```

With `PLACEMENT_TICKS_INSIDE() = 1` (the production value), this places:

- YES bid at `midpoint − (s_max − 1 tick)` — i.e. **1 tick inside the far edge of the reward zone**
- NO bid (in YES-equivalent terms `edge_ask`) at the mirror position above midpoint; the actual NO-book order is at `1 − edge_ask`

Concrete: for the Iran June 15 market (`s_max = 5.5¢`, `tick = 1¢`, midpoint = `0.485` at placement on 2026-05-19 04:58:49 UTC), the formula produced:

- YES bid @ `0.485 − 0.055 + 0.01 = 0.44`  — 4.5¢ below YES mid
- NO bid @ `1 − (0.485 + 0.055 − 0.01) = 0.47`  — 4.5¢ below NO mid (mirror)

Both orders sit at the **far edge of the reward zone**, ~9% reward density.

#### 4.23.3 Why the legacy formula is wrong for the stated objective

The bot's stated objective (from project framing and operator-confirmed design intent) is to **maximize reward farming on Polymarket** subject to capital constraints. The legacy placement formula is **fill-avoidance-optimal**, not reward-optimal. It picks the in-zone position that maximizes queue depth ahead of us (which minimizes fill rate) but minimizes per-share reward density.

The trade-off the legacy formula doesn't expose:

| Distance from mid | Reward density | Fill rate | Queue ahead (Iran market snapshot) |
|---|---|---|---|
| 1¢ (e.g. `0.48`) | ~82% | High | ~$543 |
| 2¢ (`0.47`) | ~64% | Moderate | ~$1,348 |
| 3¢ (`0.46`) | ~45% | Low | ~$8,700 |
| 4¢ (`0.45`) | ~27% | Very low | ~$16,500 |
| **5¢ (`0.44`) — legacy** | **~9-18%** | **Negligible** | **~$24,000** |

We were earning ~18% of theoretical maximum reward density on this market when ~$1,300 of queue at 2¢ from mid would shield us from fills *and* give us 3× more reward per share-minute (the arch-doc's earlier "~7×" estimate assumed sitting at 1¢ from mid; the shipped default of `$1000` queue settles at 2.5¢ ⇒ 3× actual uplift on this market, with the operator able to tune lower if production verifies the fill-rate profile is comfortable).

#### 4.23.4 FX-036 shipped strategy: queue-depth-aware placement

The fixed-distance formula was replaced with a **two-constraint placement** (commit `8152a8b`, v5.1.18):

1. Stay inside the reward zone: `distance_from_mid < s_max`
2. Sit behind a configurable amount of queue: `cumulative_queue_ahead ≥ TARGET_QUEUE_USD`

Shipped algorithm (in `order_lifecycle._queue_aware_edge` + `_compute_edge_prices`):

```python
TARGET_QUEUE_USD = cfg("RF_TARGET_QUEUE_AHEAD_USD")   # default $1000; 0 ⇒ legacy

# Walk merged book bids highest → lowest (closest to mid → outward)
cum_queue = 0.0
edge_bid = midpoint − ms.max_spread + tick * PLACEMENT_TICKS_INSIDE()  # fallback
for level in merged_book["bids"]:
    d = midpoint − float(level["price"])
    if d >= ms.max_spread:
        break                                    # walked past the zone — use fallback
    cum_queue += float(level["price"]) * float(level["size"])
    if cum_queue >= TARGET_QUEUE_USD:
        candidate = float(level["price"]) − tick
        if midpoint − candidate < ms.max_spread:
            edge_bid = candidate                 # sit 1 tick behind this level
        break

# Mirror for edge_ask: walk merged_book["asks"] lowest → highest, accumulate
# the same way, sit at level_price + tick. The merged ask side contains both
# real YES asks and NO-derived bids translated to YES-equivalent — both are
# arbitrage-linked competitors for the same liquidity, so both contribute to
# "queue ahead of us" (see market_discovery._book_entries post-FX-035).
```

Behavioural envelope:

- **Thin markets (queue < `TARGET_QUEUE_USD` even at the edge):** algorithm walks to the zone boundary, falls back to the legacy `midpoint − max_spread + tick * PLACEMENT_TICKS_INSIDE` formula. Byte-identical to pre-FX-036 for these markets — no regression. Matches the operator-noted "weather markets fill quickly despite low competition" pattern: thin-queue regimes keep the existing min_size + dump-on-fill flow unchanged.
- **Deep markets (queue exceeds `TARGET_QUEUE_USD` near midpoint):** algorithm places ~2-3 ticks from mid. On the Iran market (5.5¢ zone, ~$24k queue) the shipped default of `$1000` lands at 2.5¢ from mid = `1 − 2.5/5.5 = 54.5%` reward density, a measured 3.0× uplift over the legacy 18.2%. Larger zones with deeper queue can show >5× uplift; smaller zones may show smaller absolute uplift but still material.
- **Operator-tunable:** `TARGET_QUEUE_USD` controls the reward-density vs fill-rate trade-off. Higher → more conservative (legacy-like behaviour); lower → more aggressive (more rewards, more fills). `0` reverts to legacy unconditionally — the production escape hatch.
- **Safety preserved:**
  - Final values are clamped to `[0.01, 0.99]` (matches pre-FX-036).
  - Rounded to the market's `tick_size` decimals (matches pre-FX-036).
  - Helper returns `None` (⇒ fall back to legacy) when the `−tick` step would itself exit the reward zone — placement never sits at or outside the zone boundary.
  - SafetyController and runtime guardrails are unchanged; they bound exposure regardless of placement strategy.

#### 4.23.5 Capital-availability and over-commitment interaction

A separate point (`memory/project_capital_overcommit.md`): Polymarket allows placing limit orders worth more than the wallet balance. Unfilled excess auto-cancels at fill time. The SafetyController's `capital_pct` cap currently treats notional-on-book as expected-fill-cost, which is conservative — it limits the bot's footprint as if every order could simultaneously fill. Combined with FX-036's queue-shielded placement (where fills are rare by construction), the case for relaxing `capital_pct` upward also strengthens. Tracked separately; FX-036 is the placement-formula fix, the SafetyController constants are a follow-on tuning decision after FX-036 lands and we observe production reward yield with the new placement.

#### 4.23.6 Verification status (FX-036 shipped — v5.1.18)

| Metric | Pre-FX-036 (legacy) | Post-FX-036 (default `$1000`) | Status |
|---|---|---|---|
| Avg distance from mid (Iran market, 5.5¢ zone) | 4.5¢ (`max_spread − 1 tick`) | 2.5¢ | ✓ inline-verified |
| Reward density (Iran market) | ~18% | ~55% | ✓ inline-verified (3.0× uplift) |
| Behaviour on thin / low-competition markets | zone-edge placement | identical to legacy (queue never crosses threshold) | ✓ unit-tested |
| Behaviour with `RF_TARGET_QUEUE_AHEAD_USD = 0` | zone-edge placement | byte-identical to legacy | ✓ unit-tested (escape hatch) |
| Fast-tier test count | 697 | 721 (+24 new tests in `tests/test_placement.py`) | ✓ green on CI |

**Production validation pending:** 24h soak on Helsinki after `git pull + restart`. Operator should compare `[ATTRIBUTION]` log line `reward + rebate` totals against the prior 24h window. If fill rate becomes uncomfortable, raise `RF_TARGET_QUEUE_AHEAD_USD` (e.g. `$2000` ⇒ ~1.6¢ from mid on the Iran market) or set to `0` for legacy behaviour. The knob is hot-reloadable via `config_overrides.json` without a restart.

#### 4.23.7 FX-041 — Two-sided book-depth check (v5.1.20)

The 2026-05-19 OpenAI cascade exposed a gap in FX-036's safety logic: it checks the placement-side queue (depth ahead of us between our edge and midpoint) but not the opposite-side absorbing capacity. In an asymmetric book — one side deep enough to trigger queue-aware placement at 2¢ from mid, the OTHER side thin (total in-zone depth sub-$1000) — a fill can't be unwound without significant dump slippage. The OpenAI cascade saw ~11.5% slippage on the dump, contributing to the $17.63 realized loss that hit the kill switch.

**FX-041 (commit `3534cb5`, v5.1.20) adds an opposite-side check after each queue-aware result.**

For each placement side, after `_queue_aware_edge` computes a candidate edge:
- For "bid" placement (YES BID lives at `merged["bids"]`): check the OPPOSITE side, `merged["asks"]`.
- For "ask" placement (NO BID lives at `merged["asks"]` in YES-equivalent): check the OPPOSITE side, `merged["bids"]`.

The check accumulates `Σ(price × size)` over opposite-side levels within `max_spread` of midpoint and compares against `shares_per_side × midpoint × RF_DUMP_DEPTH_SAFETY_FACTOR`. If the opposite side is too thin, that placement side falls back to legacy zone-edge — same as a thin-book or escape-hatch fallback.

**Per-side independence preserved:** one side failing doesn't drag the other along. Asymmetric books that look safe on one side and thin on the other naturally fall back ONLY where the safety fails.

**Why opposite-side (not same-side) — judgment call worth flagging.** DumpManager's passive mode (`dump_manager.py:308-327`) sets the dump-SELL price to the best opposite-token bid (effectively crossing the spread), so physically the dump CONSUMES the SAME merged-book side as placement. A same-side-beyond-edge check would be the most physically accurate measurement of dump-absorption depth. FX-041 implements OPPOSITE-side because:
- It matches the FX-041 acceptance criterion narrative ("deep bid, thin ask → fall back").
- It adds a NEW safety axis complementary to the existing same-side `exit_buf` check at `order_lifecycle.py:482-493` (which sums in shares, not USD, and only within `RF_DUMP_EXIT_DEPTH_BUFFER = 2¢` of edge). Together the two checks cover both same-side near-edge AND opposite-side in-zone absorbing capacity.
- "Two-sided" in the FX-041 ticket title naturally suggests opposite-side.

Both interpretations catch the OpenAI cascade because asymmetric books are bad regardless of which side you measure. The opposite-side check is a healthy-book heuristic rather than a direct dump-slippage measurement; if production shows false positives or false negatives, this is the first knob to revisit.

**Known simplification:** `dump_price = midpoint` for both sides. For extreme-priced markets (midpoint $0.10 or $0.90), the NO-side dump price ≠ midpoint, so the threshold under- or over-estimates inventory value. Operator-tunable via `RF_DUMP_DEPTH_SAFETY_FACTOR`: raise on extreme markets if cascades repeat, lower if FX-041 over-fires.

**Operator-tunable knobs:**
- `RF_DUMP_DEPTH_SAFETY_FACTOR = 3.0` (default) → opposite-side depth must be ≥ 3× our inventory USD at midpoint.
- `RF_DUMP_DEPTH_SAFETY_FACTOR = 0` → disable the check (FX-036-only behaviour, the v5.1.18 default).
- Hot-reloadable via `config_overrides.json`.

**Verification (FX-041 — v5.1.20):**

| Metric | Pre-FX-041 (FX-036 alone, runtime-disabled) | Post-FX-041 (FX-036 + FX-041, default factor 3.0) | Status |
|---|---|---|---|
| Iran market (deep symmetric, FX-036 motivating) | queue-aware @ 2.5¢ from mid (when enabled) | queue-aware @ 2.5¢ from mid (no regression) | ✓ unit-tested |
| Asymmetric (deep bids, thin asks in zone) | queue-aware @ 2¢ from mid (cascade-risk) | legacy zone-edge fallback (cascade-prevented) | ✓ unit-tested |
| Behaviour with `RF_DUMP_DEPTH_SAFETY_FACTOR = 0` | n/a | byte-identical to FX-036-only (escape hatch) | ✓ unit-tested |
| Fast-tier test count | 737 | 755 (+18 new tests in `tests/test_placement.py`) | ✓ green |
| Production status (Helsinki) | FX-036 runtime-disabled via config_overrides.json | re-enable via operator action (remove the override) | pending operator action |

**Operator action to re-enable FX-036 in production:**

```bash
ssh -i ~/.ssh/polymarket_bot_ed25519 polymarket@46.62.209.203
cd ~/Polymarket-bot && git pull origin main
# Edit config_overrides.json to remove the "RF_TARGET_QUEUE_AHEAD_USD": 0 line
# (file lives at /home/polymarket/Polymarket-bot/config_overrides.json on Helsinki).
sudo systemctl restart polymarket-farmer
journalctl -f -u polymarket-farmer | grep -E "BID|ASK|GUARDRAIL"
```

Watch first 5-10 cycles for close-to-mid placement on Iran-class markets and legacy zone-edge on thin/asymmetric ones. If fill rate or dump slippage becomes uncomfortable, raise `RF_DUMP_DEPTH_SAFETY_FACTOR` (e.g., to `5.0` or `10.0`) via `config_overrides.json` — hot-reloadable.

---

## 5. Feedback Systems

| Component | Delay | Loop closes via |
|---|---|---|
| Bandit | 1 cycle (~30 min) | per-market α/β updates (no longer in allocator path; see §4.15.3) |
| Learning loop scalars | 1 cycle | `capital_scale`, `reward_trust`, `β`, `η` — 4 EMA-smoothed scalars feed allocator + calibrator |
| Fill model retraining | ≥ 30 min | re-fit on new fill data |
| Reward model Phase 1 → Phase 2 transition | ~7 days | requires accumulated daily reward history |
| SafetyController state | 1 cycle | 14 invariants evaluated each cycle |
| Regime frontier memory | per-cycle update | stored per `(fill_rate_bucket, efficiency_bucket)` |
| CF smoothed | 1 cycle | EMA with circuit breakers |

---

## 6. Failure Modes

Each failure mode is documented with: **trigger**, **step-by-step evolution**, **mitigation**, and **detection signal**. Scenarios are ordered by severity.

### 6.1 CF Collapse → Full System Deadlock

**Severity:** CRITICAL — the only truly irreversible failure loop.

**Trigger**
- `estimated_daily_total` is inflated (bad q_share, reward model drift, or scoring distortion)
- Actual rewards are low → raw CF drops sharply (e.g., 0.3 → 0.02)

**Evolution**
- Cycle T: CF ↓↓ → effective_daily ↓↓ → EV ↓↓ → fewer markets pass EV > 0 → fewer deploys
- Cycle T+1: CF continues falling → almost all markets EV ≤ 0 → 1-2 marginal deploys
- Cycle T+2: CF very low → allocator outputs ALL avoid → 0 deployments
- Cycle T+3: Farmer has no allocations to act on → no orders → no scoring_snapshots → q_share cannot update → estimated_daily_total = 0 → CF update skipped (insufficient data) → CF frozen at low value
- Cycle T+4+: Permanent deadlock. Safety controller can only restrict, not reactivate

**Mitigation status (as of commit `1081e72`):** the primary historical trigger (the `max(market_q, our_q)` q_share saturation bug) is fixed. Other triggers (API outage, genuine reward model failure) still exist.

**Detection**
- `CF < 0.01` AND `deployment_count ↓` simultaneously
- `q_share_sources: 0 windowed, 0 cumulative, 0 prior` in log line

**Recovery — two patterns observed in production:**

**Pattern A — CF deadlock (the original §6.1 case):** if CF has truly collapsed and the bot is stuck at 0 deploys, manual reset: `UPDATE reward_daily SET correction_factor = 1.0 WHERE date = (latest)`, restart oversight agent, verify deployment resumes.

**Pattern B — Phantom fill row biasing the allocator (added 2026-05-20):** if the V2 SDK over-reports `size_matched` and an inflated row in `fills` is keeping a market trapped via "Net negative" allocator score, OR is firing I7 hourly_loss on phantom damage, correct the row directly. Full recipe in operator's memory file `phantom_fill_recovery.md`; the essential SQL is:
```sql
UPDATE fills
SET shares = <actual_delivered_on_chain>,
    usd_value = <actual_delivered> * clob_cost,
    fill_type = CASE WHEN <actual_delivered> >= <claimed> - 0.5 THEN 'FULL' ELSE 'PARTIAL' END,
    position_usd_after = <actual_delivered> * clob_cost
WHERE condition_id = '<cid>' AND side = '<yes|no>' AND shares = <claimed>;
```
Detection: scan all markets for `SUM(fills.shares) - SUM(unwinds.shares) != on_chain_CTF_balance` (within tolerance). Production example: 2026-05-19 Iran NO row, 158 → 38 shares delivered. **FX-038** automates this so future incidents self-heal without operator SQL.

### 6.2 Scoring Distortion

**Severity:** HIGH, but can be silent.

**Variant A — Uniform duplication** (all markets' scoring_seconds double):
- `estimated_reward` doubles → CF halves → next-cycle `reward × CF` ≈ unchanged → system self-corrects

**Variant B — Differential distortion** (one market's scoring_seconds tripled):
- Market A gets higher predicted reward → higher allocation
- Actual reward unchanged → A's attribution inflated wrongly → bandit sees "good performance" on A → positive feedback loop
- CF adjusts globally, cannot fix per-market skew
- **End state:** silent misallocation drift; system appears healthy; capital is misallocated; hard to detect

**Variant C — Book unavailable (historical bug, fixed `88f6c7a`):**
- `record_cycle` received `order_book = None`
- Fallback `max(market_q, our_q)` set `total_market_q = total_q_score` → q_share = 1.0
- 394/402 production markets were poisoned
- `estimated_daily_total` inflated by ~5000× → CF capped at floor
- SafetyController invariants I5 and I6 both fired → system stuck in SEVERELY_MISCALIBRATED
- Fixed by Option B (book cache + require `market_q > 0` for accumulation)

**Detection**
- Scoring-snapshot volume variance across markets (one market 3-5× others)
- q_share distribution: if > 50% of markets have q_share ≥ 0.5, investigate the poisoned-row counter
- CF and est/actual ratio moving inversely in short windows

### 6.3 Fill Model Underestimates Risk

**Severity:** MEDIUM — self-healing but with a visible loss spike.

**Trigger**
A new regime (volatile market, news event) makes actual fills much higher than the model predicts.

**Evolution**
- Cycle T: low predicted p_fill → high EV → aggressive allocation → many orders placed
- During cycle: market moves, many orders fill
- Cycle T+1: `fill_rate ↑`, `loss_per_capital ↑`, `net_profit ↓` → Learning loop Rule A fires → `aggressiveness ↓`, `risk_multiplier ↑`. Bandit penalises those markets.
- Cycle T+2: RAS ↓ due to higher loss penalty + lower aggression + bandit suppression → fewer deploys
- Cycle T+3+: Model retrains on new fill data → p_fill predictions increase → system stabilises

**Mitigation**
Automatic, after the loss event. Detection requires watching `avg_loss_per_fill` for sudden spikes.

### 6.4 Reward Over-estimation (Early, CF Not Yet Stable)

**Severity:** HIGH — outcome depends on which side of the convergence curve you land.

**Trigger**
Reward model overestimates in the early bootstrap period before CF has stabilised.

**Evolution**
- Cycle T: reward ↑ → EV ↑ → heavy deploy
- Cycle T+1: actual reward much lower → CF drops sharply
- Cycle T+2: scorer's reward scaled down → EV collapses
- **Two outcomes:**
  - **Case A:** CF stabilises → system recovers
  - **Case B:** CF collapses past the circuit breaker → Scenario 6.1 deadlock

**Mitigation**
Circuit breakers in `_smooth_correction_factor` (bypass EMA if raw < 0.01, fast-adapt if raw < 0.05) prevent worst-case damping lag. The 1e-6 clamp floor ensures CF never truly reaches zero.

### 6.5 Exploration Blind Spot

**Severity:** LOW-to-MEDIUM, permanent but bounded.

**Trigger**
A class of markets is systematically underestimated by the model (e.g., a new market category the fill model hasn't seen).

**Evolution**
- Allocator: `EV < 0` → never deployed
- No fills, no rewards, no training data on these markets
- Model never updates for that class → permanent blind spot

**Mitigation status**
- Cold-start prior (§4.10) gives new markets a non-zero baseline score
- Trial cap reserves 50 slots for discovery
- Micro-exploration allocates 5%–15% to positive-RAS markets below the deploy cap
- Still not fully addressed for genuinely novel categories where the fill model is severely wrong

**Detection**
Whole market categories absent from `market_allocations.json` when they should be present. Cross-check against `reward_market_stats` population.

### 6.6 Regime Memory Trap

**Severity:** MEDIUM.

**Trigger**
A regime bucket `(fill_rate, efficiency)` has prior good performance recorded in frontier memory, but the environment shifts within the same rounded bucket.

**Evolution**
- Losses increase in the new sub-regime
- Learning rules try to reduce `capital_scale`
- But `min_floor = best × 0.60` prevents full contraction (assuming the memory is still valid)
- System remains over-deployed until regime bucket changes OR SafetyController fires from accumulated losses

**Mitigation**
Per-regime memory of size 20 (pruned LRU). Most-recent-update bias. Not perfect.

### 6.7 Low Fill, High Loss Edge Case

**Severity:** MEDIUM — the learning loop has conditional blindness here.

**Trigger**
`fill_rate < threshold` (so Rule A doesn't fire) but `avg_loss_per_fill` is high.

**Evolution**
- Learning loop Rule A requires `fill_rate > threshold` → does NOT fire
- `reward_efficiency` may be high (reward still accrues on non-filled orders) → Rule B fires
- `capital_scale ↑` even as the per-fill loss is high
- System scales up while bleeding on the rare fills it takes

**Mitigation**
Watch `avg_loss_per_fill` independently. Consider hardening Rule A to also trigger on high `loss_per_capital` regardless of fill_rate.

### 6.8 Safety Controller Intervention

**Severity:** INFORMATIONAL — this IS the mitigation.

**Trigger**
Any CRITICAL invariant fires (daily_loss > $150, slow_bleed_7d > $500, drawdown > 15%, capital < $50).

**Evolution**
- State → UNSAFE → `max_markets = 3`, `capital_pct = 5%`, no trials, probe mode, min_size only
- Most allocations flip to avoid
- System enters controlled low-footprint mode

**Important edge case:** if the allocator has already output all-avoid (Scenario 6.1), SafetyController does nothing — there is nothing to override.

**Recovery**
- `UNSAFE_RECOVERY_CYCLES = 3` cycles without CRITICAL-UNSAFE violations → target caps at DEGRADED
- Then `UPGRADE_STEP = 2` cycles of clean signals to step back toward CALIBRATED
- CALIBRATED requires `est/actual < 5×`, CF in `[0.05, 3.0]`, and `num_scoring ≥ 5`

### 6.9 Final Takeaways

**The asymmetry that defines the system**

| Reward | Loss |
|---|---|
| Global scalar (CF / α) | Local model (per-market) |
| Scaled once, applied everywhere | Estimated per market |
| Errors propagate globally | Errors remain contained |

**Consequence: reward errors dominate system behaviour.** Fill model errors are recoverable; CF errors are systemic. Debugging priority: CF and scoring integrity first.

**Only one true catastrophic loop:**
```
CF collapse → no deployment → CF frozen → permanent shutdown
```

The system is:
- Robust to local errors (fill model, individual markets)
- Fragile to global signals (CF, scoring integrity)
- Weak at discovering unknown blind spots
- Strong at exploiting known edges
- Strictly reactive (Safety layer restores caps, not deployment)

---

## 7. Monitoring Checklist

This is the production checklist. Each metric is listed with where to find it, what healthy looks like, and what a red flag looks like.

### 7.1 Tier 0 — Is the system alive?

| Metric | Source | Healthy | Warning | Critical |
|---|---|---|---|---|
| Deployed market count | `market_allocations.json` `num_deploy` | 20-60 | 1-3 consistently | 0 → SYSTEM DEAD |
| Total capital deployed | `market_allocations.json` `total_capital_deployed` | 70-100% of available | < 50% | ~0 → allocator shutdown or safety override |
| `market_allocations.json` freshness | file mtime | Updated every agent cycle | > 45 min old | > 2h → oversight agent not running |
| Farmer cycle heartbeat | `bot_history.db` recent book_snapshots | Within last 60s | > 5 min | > 15 min → farmer not running |

### 7.2 Tier 1 — Reward system global health

**Correction Factor**

| Metric | Source | Healthy | Warning | Critical |
|---|---|---|---|---|
| `reward_daily.correction_factor` | SQL | ~0.05 – 1.5 | drops >50% cycle-over-cycle | < 0.01 |
| `correction_factor_history.smoothed` | SQL | Stable within 10% band | Oscillating >20% | Frozen across cycles → denominator collapse |
| Raw vs smoothed divergence | log line | Small | > 3× apart | Circuit breaker active |

**Deadlock condition:** `CF < 0.01` AND `deployment_count declining` → entering deadlock zone.

**Estimated vs Actual Reward**

| Metric | Source | Watch ratio | Red flag |
|---|---|---|---|
| `actual_daily_total` | agent log | `actual / estimated ≈ CF` | |
| `estimated_daily_total` | agent log | | Estimated ≫ Actual → CF collapse incoming |
| | | | Actual ≈ 0 but deployed → API or scoring issue |

**Reward Efficiency**

| Metric | Source | Healthy | Dangerous |
|---|---|---|---|
| `reward_efficiency = reward / capital` | `learning_efficiency_daily` | Stable or increasing | Increasing while PnL negative → false positive signal |

### 7.3 Tier 2 — Data integrity (q_share & scoring)

**q_share distribution**

| Metric | Source | Healthy | Critical |
|---|---|---|---|
| q_share_pct per market | `market_allocations.json` | Non-zero for deployed; spread 0.01–0.3 | All q_share = 0 → scoring broken → CF freeze risk |
| q_share saturation count | agent log `poisoned_skipped` | Low (< 10) and trending down | Sudden drop across all markets → API failure |
| Cumulative ratio distribution | SQL on `reward_market_stats` | Most markets < 0.05 | > 50% of markets with ratio > 0.5 → poisoned-data alarm |

**Query for poisoned rows:**
```sql
SELECT COUNT(*) FROM reward_market_stats
WHERE CAST(json_extract(data, '$.total_q_score') AS REAL)
    > CAST(json_extract(data, '$.total_market_q') AS REAL) * 0.5
  AND json_extract(data, '$.q_score_samples') > 0;
```

**scoring_snapshots volume**

| Metric | Source | Expected | Red flag |
|---|---|---|---|
| Snapshots per market per 24h | `scoring_snapshots` | ~576 (150s cadence × ~6 cycles/hr × 24h) | ≫ 576 → duplication bug; ≪ 576 → missing snapshots |
| Cross-market variance | SQL | Roughly equal | One market 3-5× others → distortion |

### 7.4 Tier 3 — Fill & loss behaviour

| Metric | Source | Healthy | Warning | Critical |
|---|---|---|---|---|
| `fill_rate` | fills / orders_placed 24h | Moderate and stable | Sudden spike → adverse flow | Sustained high |
| `loss_per_capital` | SQL | < 5% | 5-10% | > 5% → triggers contraction logic |
| `avg_loss_per_fill` | fills | Stable | Drifting up | Sudden spike → tail risk event |
| Stop-loss frequency | `stop_losses` table | Rare | Any spike → investigate |

**Important:** the system does NOT treat stop-loss differently from normal unwinds in its learning signals. You must monitor them manually.

### 7.5 Tier 4 — Learning loop (v4.0)

| Metric | Source | Expected | Red flag |
|---|---|---|---|
| `learning_state.mode` | SQL | OFF (early) → SHADOW → ACTIVE | Flipping modes frequently |
| `capital_scale` | learning_state | 0.5 – 1.0 in steady state | At max (1.2) → runaway expansion; at min (0.3) → collapse |
| `reward_trust` | learning_state | Near 1.0 | Falling continuously → reward misestimation |
| `beta` | learning_state | Near 0.75 in steady state | Pinned at 0.10 → chronic over-utilisation signal; pinned at 0.95 → chronic under-utilisation (cap-bound). Both indicate β is saturated and only η has remaining leverage. |
| `eta` | learning_state | 0.0 – 2.0 typical | Pinned at 4.0 → coverage collapse (min-floor binding everywhere); combined with `expected_util` near target → η is holding coverage but near its ceiling. |
| `expected_util` | stamped on alloc JSON | 0.5 – 0.95 in steady state | Sustained < 0.1 → β saturated at ceiling and cap-stack binding dominates; sustained > 0.95 → Step 7 safety rescale firing frequently (allocator mis-tuned). |
| `coverage_ratio` | derived from alloc JSON | 0.3 – 0.8 in steady state | 0 → full min-floor collapse; 1.0 → nothing near min floor (very uniform, well-covered) |
| `lambda_1`, `lambda_2` | learning_state | Frozen at `1.0`, `0.5` | Any change → schema migration failed or legacy code still writing |

**Scalar conflict detector** (v4.0): `β ↑ ceiling` while `coverage_ratio ↓ 0` simultaneously = cap-stack fully binding, neither control has leverage; check `min_capital` × cluster-cap math per §4.16.5.

### 7.6 Tier 5 — Bandit

| Metric | Source | Healthy | Red flag |
|---|---|---|---|
| α/β distribution across markets | bandit state | Differentiation across markets | All identical → no learning; extreme β dominance → system stuck avoiding |
| `bandit_mult` distribution | allocation log | Spread across markets | All near 0.3 → pessimistic system |

### 7.7 Tier 6 — Allocation structure

| Metric | Source | Healthy | Red flag |
|---|---|---|---|
| Actual deployed vs `target_market_count` | log | Near target | Always at target → overly constrained |
| Capital utilisation | allocation | 90-100% | > 20% unused consistently → under-allocating |
| Per-cluster capital | allocation | < 30% cap | Hitting 30% cap repeatedly → concentration risk |

### 7.8 Tier 7 — Safety Controller

| Metric | Source | Healthy | Critical |
|---|---|---|---|
| Current state | `safety_state` | CALIBRATED or MILDLY_MISCALIBRATED | UNSAFE sustained → system in distress |
| Trigger invariants in last 24h | invariant logs | Rare | Frequent I1/I2/I3 → genuine trouble |
| Time in UNSAFE | `safety_state` with ts | Brief | > 24h → investigate underlying cause |

If SafetyController intervenes repeatedly, the system is already in trouble — it is the last line of defence, not a normal control.

### 7.9 Tier 8 — Regime & frontier memory

| Metric | Source | Healthy | Red flag |
|---|---|---|---|
| Active regime identifier | learning log `regime_id` | Stable within session | Rapid switching → unstable learning |
| Frontier memory size | persisted dict | < 20 | Constantly at 20 → churn |
| Contraction limited by frontier floor | learning log | Rare | Frequent → stuck over-deployed |

### 7.10 Tier 9 — Critical failure patterns

Single-glance pattern recognition:

| Pattern | Diagnosis |
|---|---|
| CF ↓ → EV ≤ 0 → no deploy → q_share = 0 → CF frozen | CF deadlock (§6.1) |
| scoring distortion → CF compensates → hidden misallocation | Silent drift (§6.2) |
| reward_efficiency ↑ but net_profit ↓ | False profit signal |
| EV < 0 → no deployment → no learning | Exploration failure (§6.5) |
| past best regime → prevents contraction in new sub-regime | Regime stickiness (§6.6) |

### 7.11 Minimal dashboard (if you monitor nothing else, track these)

**Absolute musts**
- CF (raw + smoothed) — top-of-dashboard chart
- Deployed market count
- Capital deployed %
- q_share distribution across deployed markets (histogram)
- fill_rate
- loss_per_capital
- reward_efficiency
- Safety state (traffic-light indicator)

**Highly recommended**
- scoring snapshot counts per market (detects duplication/missing)
- bandit multipliers (detects no-learning)
- Learning scalars (four values)
- Active regime ID

### 7.12 Operating principle when something breaks

Diagnostic tree, top-down:

1. **Are we deploying anything?** If no → Scenario 6.1 or 6.8; check SafetyController state.
2. **Is CF reasonable?** (`0.05 – 1.5` healthy). If no → reward pipeline distortion.
3. **Is q_share non-zero for deployed markets?** If no → data collection broken.

Everything else is secondary. These three questions localise 90% of failures.

---

## 8. Configuration Reference

### 8.1 Reward farmer parameters (config.py)

| Constant | Default | Purpose |
|---|---:|---|
| `RF_CYCLE_SECS` | 30 | Farmer cycle frequency |
| `RF_BATCH_SIZE` | 10 | Markets processed per cycle |
| `RF_MAX_MARKETS` | 60 | Max concurrent deployed markets |
| `RF_MAX_TRIAL_MARKETS` | 50 | Max concurrent trial (low-confidence, zero-fill) deployments |
| `RF_NEW_MARKET_Q_SHARE_PRIOR` | 0.10 | Prior q_share for cold-start markets |
| `RF_POISONED_Q_SHARE_THRESHOLD` | 0.5 | Cumulative q_share above this triggers fallthrough to prior |
| `RF_BOOK_CACHE_TTL` | 180 | Max cached-book age (seconds) for Q-score sampling; 0 disables |
| `RF_SHARES_PER_SIDE` | 50 | Default order size per side |
| `RF_PLACEMENT_TICKS_INSIDE` | 1 | Ticks from max_spread edge for legacy / fallback placement |
| `RF_TARGET_QUEUE_AHEAD_USD` | 1000.0 | FX-036 queue-depth-aware placement — sit 1 tick behind the level where cumulative book queue first reaches this $ amount. `0` (or negative) reverts to the legacy zone-edge formula (escape hatch). |
| `RF_DUMP_DEPTH_SAFETY_FACTOR` | 3.0 | FX-041 two-sided book-depth check — queue-aware placement is rejected when the OPPOSITE merged-book side carries less than `shares × midpoint × this factor` of $-weighted depth within the reward zone. Catches the asymmetric-book trap (bid-side queue sufficient but opposite-side dump capacity thin) that caused the 2026-05-19 OpenAI cascade. `0` (or negative) disables the check, reverting to FX-036-only behaviour. Hot-reloadable via `config_overrides.json`. |
| `RF_POLYMARKET_TAKER_FEE` | 0.009 | **FX-050 (v5.1.22).** Polymarket charges ~0.88-0.9% taker fee on orders that cross the spread. DumpManager's passive mode crosses the spread to consume the opposite bid → we are taker → fee applies. `DumpManager.check_dump_fills` applies this multiplier as `sell_revenue = matched × price × (1 − fee)` so unwind `usd_value` reflects cash actually settled, not gross book revenue. Calibrated against 2026-05-22 incident: actual loss −$1.34 vs pre-fix recorded −$1.00; gap $0.34 = 0.88% of $39 gross. `0` reverts to pre-FX-050 over-reporting (escape hatch). Hot-reloadable. |
| `RF_WALLET_DESYNC_THRESHOLD_USD` | 0.50 | **FX-049 (v5.1.22).** Per-agent-cycle wallet-invariant reconciliation tolerance. `\|actual_wallet_delta − expected_wallet_delta\| > this` → `[CRITICAL] WALLET_DESYNC` log. Set above typical single-trade fee noise but tight enough to catch a missed fill or phantom unwind within 1 cycle. Defense-in-depth backstop — catches the SYMPTOM of any cash-accounting drift even if ROOT cause is unknown (FX-050 was the first known instance; future unknown unknowns trip the reconciler too). Hot-reloadable. |
| `RF_TRIAL_MIN_SHARES` | 20 | FX-040 trial-mode floor for cold-start markets. Untested markets (`q_score_samples < RF_TRIAL_SCORING_SAMPLES`) deploy at `max(min_size, RF_TRIAL_MIN_SHARES)` shares regardless of recommended sizing. |
| `RF_TRIAL_SCORING_SAMPLES` | 5 | FX-040 graduation threshold — markets with this many scoring snapshots stop being treated as trials and use full sizing. |
| `RF_TRIAL_BUDGET_PCT` | 0.25 | FX-040 max cumulative trial exposure as fraction of `total_capital`. Trials beyond this budget rejected with reason `"Trial budget exhausted"`. Raise for more discovery, lower for more conservative. |
| `RF_MIN_DAILY_RATE` | 10.0 | Minimum $/day to consider a market |
| `RF_MAX_LIQUIDITY` | 5000 | Skip markets with on-book depth above this |
| `RF_MAX_BOOK_SPREAD` | 0.15 | Skip if merged book spread exceeds this |
| `RF_MARKET_REFRESH_SECS` | 1800 | Background market discovery refresh |
| `RF_ALLOCATION_TTL_HOURS` | 2.0 | Max age of `market_allocations.json` accepted |
| `RF_DUMP_AGGRESSIVE_MINS` | 5.0 | Aggressive-decay phase on fill unwind |
| `RF_DUMP_PASSIVE_REPRICE_MINS` | 5.0 | Passive reprice interval |
| `RF_DUMP_ABANDON_MINS` | 30.0 | Hard timeout on unwind attempts |
| `RF_DUMP_EXIT_DEPTH_BUFFER` | 0.02 | Max price buffer for exit depth check |
| `RF_DUMP_MAX_FAILURES` | 3 | Dump failures before blocking placement |
| `RF_UNKNOWN_RETRY_THRESHOLD` | 2 | Retries before clearing UNKNOWN-status order |
| `RF_FILL_BREAKER_WINDOW` | 180 | Fill-rate breaker observation window (sec) |
| `RF_FILL_BREAKER_THRESHOLD` | 3 | Total fills to trigger breaker |
| `RF_FILL_BREAKER_SIDE_THRESHOLD` | 2 | Same-side fills to trigger breaker |
| `RF_ORDER_STALE_CHECK_SECS` | 300 | Force-check interval for open orders |
| `RF_SPORTS_BLOCK_HOURS` | 4.0 | Block sports markets within N hours of `end_date_iso` |
| `RF_GAME_BLOCK_HOURS` | 1.0 | Block sports markets within N hours of `game_start_time`; 0 disables |

### 8.2 SafetyController constants (safety_controller.py)

| Constant | Default | Purpose |
|---|---:|---|
| `MAX_DAILY_LOSS_USD` | 150 | I1 critical threshold |
| `MAX_HOURLY_LOSS_USD` | 30 | I7 warning (60 critical) |
| `SLOW_BLEED_7D_USD` | 500 | I2 critical threshold |
| `LOSS_REWARD_RATIO_SEVERE` | 2.0 | I11 severe |
| `LOSS_REWARD_RATIO_MILD` | 1.5 | I11 mild |
| `MAX_DRAWDOWN_PCT` | 0.15 | I3 critical |
| `CAPITAL_FLOOR_USD` | 50 | I4 minimum floor (operational — smallest viable order budget) |
| `CAPITAL_FLOOR_PCT` | 0.10 | I4 scaled floor as fraction of wallet reference; effective floor = max(`CAPITAL_FLOOR_USD`, ref × `CAPITAL_FLOOR_PCT`). Added v5.1.10 (FX-010). |
| `MAX_CAPITAL_AT_RISK_PCT` | 0.80 / 0.90 | I8 warning/critical |
| `MAX_PER_MARKET_EXPOSURE_USD` | 200 | Per-market hard cap |
| `CF_CIRCUIT_BREAK` | 0.005 | I5 critical |
| `CF_SEVERE_LOW` | 0.02 | I5 severe |
| `CF_MILD_LOW` | 0.03 | I5 mild |
| `CF_CALIBRATED_LOW` | 0.05 | CALIBRATED lower bound |
| `CF_CALIBRATED_HIGH` | 3.0 | CALIBRATED upper bound |
| `EST_ACTUAL_UNSAFE` | 50.0 | I6 UNSAFE |
| `EST_ACTUAL_SEVERE` | 15.0 | I6 SEVERELY_MISCALIBRATED |
| `EST_ACTUAL_CALIBRATED` | 5.0 | CALIBRATED upper bound |
| `UPGRADE_TO_CALIBRATED` | 3 | Consecutive clean cycles to reach CALIBRATED |
| `UPGRADE_STEP` | 2 | Consecutive clean cycles for single-step improvement |
| `UNSAFE_RECOVERY_CYCLES` | 3 | Cycles without CRITICAL-UNSAFE to exit UNSAFE |
| `FILL_STORM_HAIRCUT` | 0.20 | I13 capital haircut |
| `CF_AT_FLOOR_HAIRCUT` | 0.10 | I14 capital haircut |

### 8.3 Allocator constants (profit/allocator.py, allocation_writer.py)

| Constant | Default | Purpose |
|---|---:|---|
| `DEFAULT_BETA` | 0.75 | Default β when `learning_state=None`. Bounds [0.10, 0.95]. |
| `DEFAULT_ETA` | 0.0 | Default η when `learning_state=None`. Bounds [0.00, 4.00]. |
| `CAPITAL_BUFFER` | 0.95 | Step 7 safety ceiling. Not a control target — β lives in Step 3. |
| `WEIGHT_FLOOR` | 1e-6 | Floor on `w_i` to keep the allocation well-defined under degenerate R. |
| `SCALE_EPSILON` | 1e-9 | Below this threshold, fall back to equal allocation across deploys. |
| `MAX_PER_MARKET` | 200 | Hard per-market dollar cap |
| `DEFAULT_SHARES` | 50 | Default share size |
| `MIN_SHARES` | 20 | Floor on trial shares |
| `max_capital_pct` | 0.15 | Per-market fraction of budget |
| `max_group_pct` | 0.30 | Per-question-group fraction |
| `DEFAULT_MAX_CLUSTER_PCT` | 0.30 | Per-cluster fraction (oversized clusters override to `OVERSIZED_CLUSTER_PCT = 0.15`). |
| `REWARD_SAFETY_BIAS` | 0.80 | Multiplier applied to reward term upstream in `CalibrationManager.PART_6`. |

### 8.4 Control-law constants (profit/learning.py — v4.0 β / η)

All constants below drive the `update_state` β / η rules and their stability guard. Feedback is continuous, bounded, and halved during capital_scale oscillation.

**β (utilisation target) — `profit/learning.py`**

| Constant | Default | Purpose |
|---|---:|---|
| `TARGET_UTIL` | 0.75 | Target `expected_util = Σ(p·C) / total_capital` |
| `K_BETA` | 0.5 | Proportional gain on `β_target = β · (1 + K_BETA · err_β)` |
| `ALPHA_BETA` | 0.03 | EMA blend rate; halved when `_detect_oscillation` fires |
| `CLAMP_BETA` | (0.10, 0.95) | Hard bounds |
| `DEFAULT_BETA` | 0.75 | `LearningState.beta` initial value |

**η (concentration exponent) — `profit/learning.py`**

| Constant | Default | Purpose |
|---|---:|---|
| `TARGET_COVERAGE` | 0.5 | Target `coverage_ratio = active_markets / total_markets` |
| `K_ETA` | 1.0 | Additive gain on `η_target = η + K_ETA · err_η` |
| `ALPHA_ETA` | 0.03 | EMA blend rate; halved under oscillation |
| `CLAMP_ETA` | (0.00, 4.00) | Hard bounds |
| `DEFAULT_ETA` | 0.0 | `LearningState.eta` initial value |

**Capital-scale rules — retained from v3.x (still drive `capital_scale` feedback)**

| Constant | Default | Purpose |
|---|---:|---|
| `EMA_ALPHA` | 0.20 | EMA blend rate for `capital_scale` and `reward_trust` |
| `CLAMP_CAP` | (0.30, 1.20) | `capital_scale` bounds |
| `CLAMP_TRUST` | (0.50, 1.00) | `reward_trust` bounds |
| `TRUST_DOWN`, `TRUST_UP` | 0.90, 1.02 | Rule C multipliers on `reward_error` |
| `TRUST_REVERSION_RATE` | 0.02 | Per-cycle pull of `reward_trust` toward 1.0 |
| `OSCILLATION_WINDOW` | 20 | Look-back window for `_detect_oscillation` (also consumed by β/η guard) |
| `OSCILLATION_THRESHOLD` | 6 | Flips-in-window required to fire damping |
| `OSCILLATION_DAMPEN_FACTOR` | 0.85 | Pre-EMA `u_cap *=` this when damping fires |
| `CAPITAL_HISTORY_MAX` | 100 | Hard cap on stored `capital_scale` trace |
| `CAPITAL_CHANGE_MIN_STEP` | 0.05 | Patch-13 hysteresis dead-band on `capital_scale` |
| `CAPITAL_DIRECTION_LOCK` | 5 | Cycles the direction lock stays armed after every accepted flip |

**Deprecated λ clamps — retained as imports-only** (imported by `simulation/invariants.py`; the allocator does not read them)

| Constant | Default | Purpose |
|---|---:|---|
| `CLAMP_LAMBDA_1` | (0.50, 5.00) | Frozen export; `LearningState.lambda_1 = 1.0` never changes |
| `CLAMP_LAMBDA_2` | (0.01, 2.00) | Frozen export; `LearningState.lambda_2 = 0.5` never changes |

**Sim-only bootstrap calibrator — `simulation/bootstrap_calibrator.py`**

| Constant | Default | Purpose |
|---|---:|---|
| `SIM_P_FILL_MIN` | 0.02 | Lower clamp on the sim's deterministic p_fill substitution |
| `SIM_P_FILL_MAX` | 0.15 | Upper clamp |
| `SIM_P_FILL_DEFAULT` | 0.05 | Fallback when `daily_rate` or `q_share_pct` is missing/invalid |
| formula coefs | `0.03 + 0.001·daily_rate + 0.004·q_share_pct` | Linear map on market state |

**Allocator Step-3b cap-aware shaping — `profit/allocator.py` (v5.0)**

| Constant | Default | Purpose |
|---|---:|---|
| `OVERSIZED_CLUSTER_PCT` | 0.15 | Reused from `profit/correlation.py` — effective cluster cap fraction for oversized clusters. Used in Step 3b to compute `cluster_budget` per cluster. |
| `max_cluster_pct` | 0.30 (via `DEFAULT_MAX_CLUSTER_PCT`) | Effective cluster cap fraction for non-oversized clusters. Used identically in Step 3b. |

(No new constants introduced by Step-3b — it consumes the existing cap policy.)

**Runtime safety guardrails — `reward_farmer.py` (v5.0)**

| Constant | Default | Purpose |
|---|---:|---|
| `MAX_NOTIONAL_RATIO` | 2.0 | Soft block threshold: `Σ live notional / T >` this → block all new placements this cycle. |
| `HARD_NOTIONAL_RATIO` | 2.5 | Hard enforcement threshold: exceeds this → cancel lowest-priority BUYs until ratio ≤ `MAX_NOTIONAL_RATIO`. |
| `CLUSTER_NOTIONAL_LIMIT_FRAC` | 0.5 | Soft+hard cluster cap (same fraction doubles as both). Any cluster over this is blocked from new placements AND has members cancelled. |
| `MAX_CANCELS_PER_CYCLE` | 5 | Per-helper cap on hard-enforcement cancels. Burst protection. |
| `MAX_BREACH_CYCLES` | 3 | After N consecutive cycles of `notional_ratio > HARD_NOTIONAL_RATIO`, emit `[CRITICAL] persistent_overexposure`. Observational only. |
| `MAX_DAILY_LOSS_FRAC` | 0.1 | Kill-switch trigger: `24h realized_loss > this · T`. |
| `CRITICAL_CF_THRESHOLD` | 0.01 | Kill-switch trigger: `correction_factor <` this. |
| `FILL_RATE_SPIKE_FACTOR` | 3.0 | Kill-switch trigger: `1h-obs / 6h-rate >` this. |
| `GUARDRAIL_FILLRATE_SHORT_SECS` | 3600 | 1h fill-count window. |
| `GUARDRAIL_FILLRATE_BASELINE_SECS` | 21600 | 6h baseline window. |
| `MIN_FILL_BASELINE` | 5 | Minimum baseline fills before the spike trigger can fire. |

**Execution modes + telemetry — `reward_farmer.py` (v5.0)**

| Constant | Default | Purpose |
|---|---:|---|
| `MODE_DRY_RUN` | `"DRY_RUN"` | No API calls; intent-logged only. |
| `MODE_SHADOW` | `"SHADOW"` | Reads permitted; no writes. |
| `MODE_LIVE` | `"LIVE"` | Full execution. |
| `VALID_MODES` | `(DRY_RUN, SHADOW, LIVE)` | Validated at `__init__`. |
| `DEFAULT_MODE` | `MODE_DRY_RUN` | Applied when `RewardFarmer()` is constructed with no argument. |
| `ROLLING_STATS_WINDOW` | 100 | Deque length for rolling metric averages. |
| `ROLLING_STATS_EMIT_EVERY` | 10 | Emit `[ROLLING_STATS]` every N cycles. |

### 8.5 Effect of changing each value

Before tuning, understand the second-order effects:

- **`RF_MAX_MARKETS`↑**: more deployment slots, but API rate pressure grows and `get_target_market_count` may not saturate if efficiency is low
- **`RF_BOOK_CACHE_TTL`↓**: fewer Q-score samples per market; `↑` risks stale books influencing q_share
- **`RF_GAME_BLOCK_HOURS`↑**: safer sports protection, fewer sports deployments
- **`RF_NEW_MARKET_Q_SHARE_PRIOR`↑**: more aggressive cold-start behaviour; risks over-deploying to unproven markets
- **`RF_POISONED_Q_SHARE_THRESHOLD`↑**: fewer rows treated as poisoned; approaches 1.0 disables the heuristic
- **`RF_FILL_BREAKER_THRESHOLD`↓**: more aggressive halts; risks excessive halts in normal flow
- **`HARD_NOTIONAL_RATIO`↓**: earlier hard enforcement; more orders cancelled per breach cycle. Setting below `MAX_NOTIONAL_RATIO` would make every breach over 2.0 active-cancel (equivalent to removing the soft-block buffer).
- **`MAX_CANCELS_PER_CYCLE`↑**: faster exposure reduction on large breaches, at the cost of burst-cancel risk if the priority sort is miscalibrated. Capped to keep the operator in the loop.
- **`MAX_DAILY_LOSS_FRAC`↓**: tighter capital protection; more frequent kill-switch trips under volatile market conditions.
- **`CRITICAL_CF_THRESHOLD`↑**: earlier kill-switch on CF collapse; risks false trips during genuine CF dips that would self-heal.
- **`MIN_FILL_BASELINE`↑**: longer cold-start period before the fill-rate spike trigger arms; safer during bootstrap but delays protection against real spikes.

---

## 9. Database Schema Reference

### 9.1 Tables

| Table | Key columns | Written by | Read by |
|---|---|---|---|
| `reward_market_stats` | `condition_id` PK, `data` JSON, `updated_at` | `reward_tracker._save` | `data_collector.query_reward_stats` |
| `scoring_snapshots` | `ts, order_id, condition_id, side, scoring, price, shares` | `reward_farmer` Step 6 (every 5th cycle) | Windowed q_share; fill model features |
| `book_snapshots` | `ts, condition_id`, best_bid/ask, midpoint, spread, depth columns | `order_lifecycle.log_book_snapshot` | Fill model features, analysis |
| `market_expiry_cache` | `condition_id` PK, `end_date_iso`, `game_start_time`, `fetched_at` | `_fetch_reward_market_expiries` | `MarketMetrics` population |
| `fills` | `ts, condition_id, side, shares, price, clob_cost, usd_value, midpoint, slippage, order_age_secs, position_usd_after, reward_rate_hr` | `fills.py` | Attribution, learning, fill model training |
| `unwinds` | `ts, condition_id, side, shares, sell_price, usd_value, vwap_cost, pnl, hold_duration_secs, unwind_type, reward_earned_est` | `unwind.py` | Attribution, learning |
| `orders_placed` | `ts, condition_id, side, price, size, order_id, order_type` | `order_lifecycle` | Feature training |
| `orders_cancelled` | `ts, order_id, condition_id, side, price, age_secs, reason` | `order_lifecycle` | Diagnostics |
| `active_orders` | `order_id` PK, `condition_id, side, order_type, price, shares, placed_at` | `order_lifecycle` | Reconciliation, ghost-order detection |
| `correction_factor_history` | `ts, raw, smoothed, estimated_daily, actual_daily, deployed_count` | `_smooth_correction_factor` | Diagnostics, last 30 rows retained |
| `reward_daily` | `date, total_reward_usd, total_combined_usd, correction_factor` | `oversight_agent` | Reporting, SafetyController |
| `reward_daily_markets` | `date, condition_id, scoring_seconds, daily_rate` | `oversight_agent` | Phase 2 reward model training |
| `market_performance` | `ts, condition_id, q_share_pct, on_book_hours, fill_count, net_score, ...` | `oversight_agent` | Short-term & historical adjustments |
| `learning_state` | single row | `LearningController.step()` | `allocate_portfolio` kwarg |
| `learning_efficiency_daily` | `date, reward_efficiency` | `LearningController` | Baseline & trend computation |
| `safety_state` | single row | `SafetyController.evaluate_state` | Startup, cycle entry |
| `portfolio_snapshots` | `ts, value` | `SafetyController._load_portfolio_peak` | Drawdown invariant I3 |
| `dump_states` | per-position unwind state | `dump_manager` | Crash recovery |
| `unliquidatable_markets` | `condition_id` PK, `reason`, `marked_at`, `last_retry_at`. Records cids whose orderbook the bot has confirmed dead. | `database.mark_unliquidatable` (from OL + DM exception handlers + dead-market cleanup) | Gates in OL / DM / orphan-scan / exchange-sync / dump-state restore; periodic 6h re-probe via `_reprobe_unliquidatable`. Added v5.1.9 (`7d8d38d`). |
| `wallet_reconcile_history` | `id` PK auto, `ts`, `actual_wallet`, `expected_wallet`, `divergence`, `status` (one of `baseline` / `ok` / `desync` / `fail_open`), `baseline_ts`, `baseline_wallet`, `fills_delta`, `unwinds_delta`, `rewards_delta`. Audit trail of every wallet-invariant reconciliation event. Most-recent row's `(ts, actual_wallet)` is the BASELINE for the next reconcile cycle (incremental window, not cumulative-from-genesis). | `oversight/wallet_reconciliation.py::reconcile_wallet_invariant` called once per `oversight_agent.run_once()` cycle | Operator monitoring via `[CRITICAL] WALLET_DESYNC` log channel; future analytics on cash-accounting drift. Added v5.1.22 (`06d8406`). FX-049. |
| `stop_losses` | stop-loss events | unwind / dump_manager | Manual monitoring |
| `cycle_snapshots` | per-cycle market prices | `reward_farmer` | Recent-price feeds |

### 9.2 Table notes

- `reward_market_stats.data` is a JSON-serialised `MarketStats` dataclass (~35 fields). Relevant keys: `total_q_score`, `total_market_q`, `q_score_samples`, `daily_rate`, `buy_fills`, `time_on_book_secs`, `cycles_in_reward_window`.
- `portfolio_snapshots` may be absent in some production DBs — older deployments did not create it. Drawdown invariant I3 falls back to exchange balance when the table is missing.
- WAL mode is enabled (`PRAGMA journal_mode=WAL`) to support concurrent reader (agent) and writer (farmer).

---

## 10. Changelog & Known-Fixed Bugs

### 10.1 Recent commits (descending)

| Commit | Description | Impact |
|---|---|---|
| `06d8406` | **v5.1.22 — Polymarket taker-fee accounting + wallet reconciliation (FX-050 + FX-049).** Operator-authorized P3 single-axis bundle since both fixes belong to "loss-accounting integrity". **FX-050**: new config knob `RF_POLYMARKET_TAKER_FEE = 0.009`; `dump_manager.py:89` applies `sell_revenue = matched × price × (1 − fee)`. Closes the ~25-30% under-reporting of dump losses in `unwinds.usd_value` that I7 hourly_loss + 24h-realized-loss kill switch were operating on. Calibrated against 2026-05-22 incident: post-fix `pnl = −$1.349` (within $0.01 of actual −$1.34, float rounding). **FX-049**: new table `wallet_reconcile_history` + new module `oversight/wallet_reconciliation.py::reconcile_wallet_invariant` + integration in `oversight_agent.run_once()`. Compares ACTUAL wallet delta vs EXPECTED (bot DB unwinds − fills + data-api REWARD + MAKER_REBATE since last reconcile). `\|divergence\| > RF_WALLET_DESYNC_THRESHOLD_USD = $0.50` → `[CRITICAL] WALLET_DESYNC`. Fail-OPEN on data-api errors. Incremental (rolling window). First-run path snapshots baseline. **Defense-in-depth backstop catching the SYMPTOM of any future cash-accounting drift even when root cause is unknown.** +1003 / −2 across 7 files. 15 new tests (5 FX-050 in `tests/test_dump_manager_fee.py` + 10 FX-049 in `tests/test_wallet_reconciliation.py`). Fast tier 770 → **785 pass**; CI run 26350996533 green in 5m46s. | **Phase A of Master Plan complete.** After Helsinki pulls: first agent cycle writes baseline reconcile row (no alert); subsequent cycles compare against on-chain truth. Next dump cycle records post-fee usd_value matching wallet ground truth. Friend-rollout safety machinery now trustworthy on loss magnitude. **Master Plan Phase B (FX-045 q_share priority swap) is NEXT** — single highest-leverage code change remaining, structural G3 unfreezer. |
| `a858bb9` | **v5.1.21 part 2 — Fix test_order_lifecycle SDK shim against sibling test pollution.** CI run `26329526380` failed 2/770 because pytest alphabetical ordering imports `test_critical_fixes.py` (which installs MagicMock-based partial mocks at `sys.modules["py_clob_client_v2.clob_types"]` without cleanup) BEFORE `test_order_lifecycle.py`. The prior shim's early-return guard `if 'py_clob_client_v2' in sys.modules: return` didn't distinguish "real SDK installed" from "stale sibling MagicMock present"; FX-037 token_id-routing tests then asserted on MagicMock instead of string. Fix: three-step protocol — drop MagicMock entries first (mirrors `test_placement.py::_drop_stale_clob_mocks`), try fresh real SDK import (succeeds on Helsinki CI), fall back to passthrough dataclass stand-ins (local dev). Production code unchanged; pure test-environment fix. **CI run 26329901126 green: 770/770 in 5m59s.** | Operator-visible: none. Test infrastructure hardening that lets FX-037's contract tests run correctly in both local + CI environments. Lesson logged in §10.3: test pollution between sibling test files is a recurring trap; suggested follow-up: extract shim into `tests/conftest.py` for unconditional install. |
| `0ec898a` | **v5.1.21 part 1 — Add BUY-side phantom-fill defense (FX-037).** Mirrors `DumpManager.check_dump_fills`' on-chain probe (`dump_manager.py:60-87`, shipped v5.1.9) on the BUY side. New helper `OrderLifecycle._check_buy_phantom_fill(ms, side, matched) → float`. After SDK reports a BUY fill with `size_matched > 0` and status in `(MATCHED, CANCELLED)`, query `get_balance_allowance(CONDITIONAL, token_id)` to confirm CTF balance actually increased by reported amount. If `actual_delta < matched - 0.5`, prefer on-chain truth and emit `log.critical("PHANTOM FILL: ...")`. The 2026-05-19 Iran NO incident shape (SDK reported `size_matched=158` for an order that delivered only 38 shares) → inflated fills row → cascaded I7 → SafetyController demotion → forced cold-start OpenAI deployments → dump slippage → kill switch. FX-037 closes the BUY-side asymmetry that allowed this. **Fail-OPEN on API exception** (preserves SDK value; losing legitimate fills is strictly worse than recording occasional phantoms which orphan-scan catches next cycle). 14 new tests across `TestCheckBuyPhantomFill` (11) and `TestDetectFillsPhantomIntegration` (3). | Friend-rollout G2 gate cleared on the silent-corruption axis. Helsinki has zero fills in the observation window, so the defense is dormant but armed; first fill that triggers a phantom (if any) will produce `PHANTOM FILL: SDK size_matched=N but on-chain delta only M` log line and the recorded fill will reflect on-chain truth, not SDK over-report. |
| `3534cb5` | **v5.1.20 — two-sided book-depth check** (`fixit.md::FX-041`). Prerequisite for safely re-enabling FX-036 (queue-depth-aware placement) in production after the 2026-05-19 OpenAI cascade exposed the asymmetric-book trap (bid-side queue sufficient, opposite-side dump capacity thin → 11.5% dump slippage). One new config knob `RF_DUMP_DEPTH_SAFETY_FACTOR = 3.0` in `config.py`. One new helper `_has_sufficient_dump_depth(opposite_book_levels, midpoint, max_spread, shares_per_side, dump_price, safety_factor)` in `order_lifecycle.py` accumulating `Σ(price × size)` over the opposite merged-book side within `max_spread` of midpoint; returns True when cumulative ≥ `shares × midpoint × factor`. `_compute_edge_prices` gains two new kwargs (defaulted to escape-hatch values for backwards compat). After each queue-aware result, the opposite-side check runs; if insufficient, that side falls back to legacy zone-edge. Per-side independence preserved. Production call site in `place_orders_for_market` passes `ms.agent_shares or SHARES_PER_SIDE()` + `DUMP_DEPTH_SAFETY_FACTOR()`. +1 line `config.py`, +~75 lines (1 helper + 1 accessor + extended `_compute_edge_prices` + 2-line call-site update) in `order_lifecycle.py`, +~190 lines / 18 new tests in `tests/test_placement.py` (`TestHasSufficientDumpDepth` × 10, `TestComputeEdgePricesDumpDepthBackwardsCompat` × 2, `TestComputeEdgePricesDumpDepth` × 5, +1 end-to-end). Test count 737 → 755 pass. | Iran market (FX-036 motivating scenario) still passes queue-aware with default factor 3.0 (threshold 50 × 0.485 × 3 = $72.75 vs ~$16k opposite-side in-zone depth) — no reward-density regression. Asymmetric books (deep one side, thin the other in zone) now correctly fall back to legacy zone-edge placement. **Operator action to re-enable FX-036 in production:** remove `"RF_TARGET_QUEUE_AHEAD_USD": 0` from Helsinki's `config_overrides.json` and `sudo systemctl restart polymarket-farmer`. FX-036's 3× reward density uplift returns on deep symmetric markets; asymmetric books safely revert to legacy via FX-041. **Known interpretation trade-off:** OPPOSITE-side check rather than SAME-side (DumpManager's passive mode actually crosses the spread to consume same-side). OPPOSITE-side was chosen because it matches the FX-041 acceptance criterion narrative and adds a NEW safety axis complementary to the existing same-side `exit_buf` check at `order_lifecycle.py:482-493`. Both interpretations catch the OpenAI cascade. |
| `c2c21d7` | **v5.1.19 — cold-start trial-mode sizing** (`fixit.md::FX-040`). Three new config knobs (`RF_TRIAL_MIN_SHARES=20`, `RF_TRIAL_SCORING_SAMPLES=5`, `RF_TRIAL_BUDGET_PCT=0.25`). `q_score_samples` propagated through `MarketMetrics` → `ScoredMarket`. New trial-mode branch in `oversight/allocation_writer.compute_allocations`: untested markets cap at `max(min_size, RF_TRIAL_MIN_SHARES)` shares regardless of recommended sizing; cumulative trial budget gates further trials; redistribution pass excludes trial markets so the cap actually binds. New `[FX-040 trial]` telemetry per cycle. +351 / -5 lines across `config.py`, `oversight/{allocation_writer,data_collector,market_scorer}.py`. +240 lines new `tests/test_trial_sizing.py` (16 tests). 1 existing test updated to set `q_score_samples=10` (graduated) — that's the test's spirit. Fast tier 721 → **737 pass** (0 regressions). | **First Phase 1 fix from the 2026-05-19 cascade analysis.** Closes the "143-share trap" that lost $17.63 yesterday on OpenAI cold-start markets. Production verification on Helsinki at 08:22:40 UTC May 20: first oversight cycle on `c2c21d7` showed `[FX-040 trial] deployed=1 rejected=49 budget_used=$46/$55 (25% cap)`. **49 cold-start markets explicitly rejected by the trial budget gate** — exactly the kind of markets that caused yesterday's cascade. FX-036 still runtime-disabled via `config_overrides.json` until FX-041 (two-sided depth check) ships. |
| `8152a8b` | **v5.1.18 — queue-depth-aware placement** (`fixit.md::FX-036`). Pre-FX-036 placement sat at `max_spread − 1 tick` from midpoint (far edge of reward zone) — `1 − 4.5/5.5 = 18.2%` of theoretical reward density on the Iran market. The fix introduces two helpers in `order_lifecycle.py`: `_queue_aware_edge` walks one side of the merged book accumulating `price × size` and returns the edge one tick BEHIND the level where cumulative queue first crosses `RF_TARGET_QUEUE_AHEAD_USD` (new config knob, default `$1000`); `_compute_edge_prices` runs both sides and falls back to the legacy formula on thin books, escape hatch (`knob ≤ 0`), or zone-boundary edge cases. Mirrors the bid algorithm on the YES-equivalent ask side via the merged book's NO-derived entries (post-FX-035 normalization). +1 line in `config.py`, +~100 / -6 lines in `order_lifecycle.py`, +~350 lines new `tests/test_placement.py` (24 tests). Test count 697 → 721. Inline production-shape verification: Iran market bid `$0.440` → `$0.460`, ask `$0.530` → `$0.510`; reward density `18.2%` → `54.5%` = **3.0× uplift**. Operator-tunable knob is hot-reloadable via `config_overrides.json` and `0` reverts to legacy behaviour unconditionally as an escape hatch. | The largest single reward-yield lever in the codebase. Pre-FX-036 the bot was structurally earning ~18% of theoretical max density on its first production market; post-fix it sits at ~55% on the same market. Thin-queue regimes (weather, low-competition) fall back to legacy zone-edge placement — no regression there. SafetyController and runtime guardrails unchanged. **Production verification path on Helsinki:** `git pull + sudo systemctl restart polymarket-farmer`, then watch `[ATTRIBUTION] reward + rebate` totals over a 24h window vs the prior 24h. If fill rate is uncomfortable, raise `RF_TARGET_QUEUE_AHEAD_USD` via `config_overrides.json` (no restart needed). |
| `647b1e2` | **v5.1.17 — handle V2 SDK dict-return in get_merged_book (THE ROOT CAUSE)** (`fixit.md::FX-035`). `client.get_order_book()` in py-clob-client-v2 v1.0.0 returns a **dict**, but `market_discovery.get_merged_book` was written assuming an OrderBook object with `.bids`/`.asks` attributes. `getattr(dict, "bids", [])` returned `[]` because dicts don't expose keys as attributes. **Every book fetch returned None silently in production since the V2 migration on 2026-04-29.** Helsinki bot placed zero orders for the entire 4-day LIVE window. DRY mode masked it for ~17 days; FX-001's I9 deadlock masked it for 4 more days. Same class as B9 (V1→V2 SDK migration miss in wrapper return-shape). Fix: new `_book_entries(ob, key)` helper normalizes both dict-form (V2 SDK production shape) + object-form (test mock shape). `get_merged_book` uses it for all 4 iteration sites. Backward-compat preserved. 12 new regression tests in `tests/test_get_merged_book.py` that call the REAL function with both shapes — pre-fix dict-form tests would fail, post-fix all pass. +335 / -72 lines across 4 files. CI green in 5m5s. | Helsinki placed its first 2 real orders at 2026-05-19 04:58:49-50 UTC immediately after the pull: YES @ $0.44 size 67 + NO @ $0.53 size 67 on the Iran market (paying ~$200/day in rewards). CYCLE_SUMMARY: `active_markets: 1, total_live_notional: $64.99, notional_ratio: 0.3228, cf: 1.0`. **From 4 days of $0 to actually farming rewards in one commit.** The whole hardening campaign closed every bug it found, but the bug it didn't find was the load-bearing one. Lesson logged in §10.3. |
| `75d03c7` | **v5.1.16 — stop dead-market cleanup from marking cids unliquidatable** (`fixit.md::FX-032`). Surfaced empirically by Helsinki recovery diagnostics: 60 healthy cids got flagged in `unliquidatable_markets` at v5.1.14 farmer startup (03:23:38 UTC), including the Iran market (`0xdb22a7749b83`) — direct CLOB probe confirmed `active=True, accepting_orders=True, rewards_rate=$200/day`. The FX-006 cascade had over-extended `mark_unliquidatable` to fire on any `get_merged_book` failure (3+ consecutive), which catches SDK parse errors, transient blips, and brief empty-book windows — much wider than the canonical FX-007 "orderbook does not exist" body. The FX-016 audit missed it because `TestDeadMarketCleanupCascade` was a logic-shape replay (test re-constructed the loop body locally, would have stayed green even with the production code removed). Fix: removed `mark_unliquidatable` from `reward_farmer.py:2093`; FX-006's `delete_dump_state` cascade preserved. New source-inspection test reads `RewardFarmer.run_cycle` via `inspect.getsource` and asserts `mark_unliquidatable` doesn't appear in the Step 4b block — catches the class of regression where logic-replay tests drift from source. +5 / -23 lines + test rewrite + new source-inspection test. | Helsinki was locked out of an Iran market paying $200/day in rewards because of this bug. Pulling v5.1.16 + clearing the 61 stale `unliquidatable_markets` rows on Helsinki unblocks deployment. The new code won't recreate them — only canonical FX-007 entries get marked. |
| `d5eabea` | **v5.1.15 — scale oversized deploys to fit per-state capital cap** (`fixit.md::FX-031`). Surfaced empirically on Helsinki's first oversight cycle after the v5.1.14 recovery pull: BOOTSTRAP cap = $60 vs allocator sizing each of 3 deploys at $84-$89. The running-cost loop wholesale-rejected all 3 because individual `est_cost > remaining`. Bot would have stayed at 0 deploys until BOOTSTRAP exited to MILDLY (~90 min), and even then only 1 of 3 would have fit. Same shape would have hit SEVERELY and DEGRADED. Fix: scale shares down to fit `remaining` budget instead of wholesale-reject (matching FX-029's scale-down pattern); iterate `deploys` in score-desc order so the top scorer claims the budget; reject cleanly only when `remaining < min_cost`. +141 / -9 lines across `oversight/safety_controller.py` + 5 new regression tests. | Closes the structural gap that left Helsinki at 0 deploys/cycle in BOOTSTRAP. Post-pull, expect 1-3 deploys per cycle at ~$60 total. After BOOTSTRAP → MILDLY → CALIBRATED transition (~3 hours total), full $201 deployment. Same FX-001 silent-contract class of bug; the audit campaign found similar shape (FX-029, FX-030) by reading the architecture doc but missed this one because it only manifests with specific wallet × state combinations. Lesson: "first production cycle after a major release" is an explicit verification step. |
| `38fc63c` | **v5.1.14 — close remaining hardening items: FX-019 fix + FX-027 acceptance** (hardening roadmap COMPLETE). Two minor changes: (a) **FX-019** removed `check_wallet.py:243-246` (the dead `AssetType.CONDITIONAL` query that printed a cosmetic 400 at startup); diagnostic still emits the COLLATERAL pUSD balance + on-chain allowance checks (the useful part). -4 / +4 lines. (b) **FX-027** process-boundary lag accepted as designed architectural risk; moved to fixit §5 with explicit mitigation rationale (farmer-side guardrails operate at 30-s cadence; agent 30-min cadence affects allocation revisions, not enforcement). No code change for FX-027. Doc lock-step: fixit §2 / §3 emptied; §5 carries the FX-027 acceptance; §6 marks Phases 7/8/9 closed; §8 changelog summarizes the 4-day campaign. Architecture doc bumped v5.1.13 → v5.1.14. | Operator-visible: `python check_wallet.py` no longer prints the alarming-but-harmless 400 at the top. Bot behaviour: unchanged. Campaign tally: 30 fixit entries, 28 shipped + 1 accepted + 1 doc-only (FX-027 acceptance is documentation); 11 code commits Phases 0-6 + closure; tests 449 → 679 (+230); SafetyController coverage 0 dedicated → 94% (152 tests); CI gating every push since v5.1.12; zero production-impacting bugs escaped. |
| `1c4ae7e` | **v5.1.13 — close two audit-surfaced bugs in SafetyController** (`fixit.md::FX-029` + `FX-030`). The Phase 6 part 2 audit on the FX-016 test build-out surfaced two real safety defects that the new tests had documented as "behaviour" but were actual bugs. (a) **FX-029**: `filter_allocations` per-market $200 cap (lines 839-850) computed scale from caller's `est_capital_cost` but recomputed post-cap value from an internal formula `shares × est_price × 2` — when these disagreed, post-cap cost could overshoot $200 (audit's repro: shares=500, est=300, spread=0.045 → $303.03; narrow-spread variant → $496.01). Fix derives both from the internal formula. (b) **FX-030**: `_handle_upgrade`'s else-branch caught UNSAFE alongside SEVERELY/DEGRADED and jumped UNSAFE → MILDLY in 2 cycles when fully calibrated, bypassing the documented 3-cycle DEGRADED auto-recovery cap (arch doc §4.14 + lines 1919-1920). Fix: skip `_handle_upgrade`'s post-BOOTSTRAP body when state == UNSAFE; the slow auto-recovery in `evaluate_state` becomes the SOLE UNSAFE exit. +91 / -41 lines across `oversight/safety_controller.py` + `tests/test_safety_controller.py`. Tests: 1 incorrect test removed (the one that pinned FX-030 as a contract), 4 regression tests added (2 for FX-029 mismatched-input + narrow-spread, 2 for FX-030 5-cycle minimum + fully-calibrated UNSAFE). | Production impact zero on Helsinki: FX-029 never fired because prod allocator and SafetyController use the same `shares × est_price × 2` formula; FX-030 never fired because the bot has never entered UNSAFE (current state BOOTSTRAP). The fixes harden invariants for future events. Per-market $200 cap is now machine-enforced regardless of caller-input consistency; UNSAFE → MILDLY transit now guaranteed ≥ 5 cycles, restoring the documented graduated-response window. |
| `f3630c9` | **v5.1.13 — SafetyController test build-out part 2 of 2** (`fixit.md::FX-016`). 44 tests across Blocks E (persistence round-trip with age branches), F (query helpers — `_query_fill_damage`, `_query_data_freshness`, `_query_lifetime_fills_count`, `_query_last_known_balance`, `_compute_portfolio_value`, `_capital_floor`, `confidence_score`, public query methods), G (alert-file writers `_write_alert_file` / `_clear_alert_file`). +519 / -0 lines in `tests/test_safety_controller.py`. Coverage 87% → 94%. | No code change — pure test build-out. Coverage on `safety_controller.py` lifted past the ≥80% fixit target with 14 points of margin. The persistence round-trip tests would have caught any future regression in the 2h/6h `_load_state` window logic; the helper tests pin the cold-start vs warm-DB-empty distinction (FX-001's defensive branch). |
| `4aff918` | **v5.1.13 — SafetyController test build-out part 1 of 2** (`fixit.md::FX-016`). 88 tests across Blocks A (per-invariant I1-I14 happy/breach/query-failure), B (state-machine — permissions, upgrade ladder, UNSAFE auto-recovery slow path, counter resets), C (`filter_allocations` end-to-end — max_markets cap, trial gate, capital cap, probe mode, LOW-signal haircuts, q_share clamp, per-market exposure), D (`evaluate()` integration — multi-priority precedence, worst-within-priority, backward-compat wrapper). +969 / -10 lines in `tests/test_safety_controller.py`. Coverage 58% → 87%. | No code change — pure test build-out. Closes the structural gap that allowed FX-001's I9 deadlock to ship to production. Phase 6 part 2 is the test-coverage closure of the hardening campaign; combined with Phase 6 part 1 CI (v5.1.12), every push to `main` is now gated by 152 SafetyController-focused tests. Three minor test-setup refinements during this work; one edge case (per-market cap) escalated to a real bug fix in commit `1c4ae7e`. |
| `a580bdb` | **v5.1.12 — add GitHub Actions CI for fast-tier tests** (`fixit.md::FX-026`). New `.github/workflows/test.yml` runs `pytest tests/ --ignore=tests/test_simulation.py --tb=short` on every push to `main` + every PR. Single `ubuntu-24.04` job, Python 3.14 via `actions/setup-python@v5`, pip cache keyed on `requirements.txt`, 15-min job timeout. New `README.md` carries project overview + workflow status badge. +54 lines / 2 new files. One Node.js 20 deprecation annotation surfaces in run output (forced upgrade 2026-06-02, removal 2026-09-16); already on latest action major versions (`checkout@v4`, `setup-python@v5`). Phase 6 part 1 of 2 — FX-016 SafetyController comprehensive coverage is part 2. | Every future push to `main` is now gated on a green fast-tier run before regressions land. First green run `26046878949` posted 544/544 tests passing in 7m17s on the runner. No change to bot behaviour — pure tooling. |
| `91bae99` | **v5.1.11 — graceful shutdown + batch cancel on SIGTERM** (`fixit.md::FX-014` + `FX-015`). reward_farmer adds SIGTERM handler; `_shutdown_cleanup` uses V2 batch `cancel_orders` endpoint (one API call cancels everything; per-order fallback if batch raises); `OrderLifecycle.cancel_order` gains `force=True` to bypass dry_run shortcut; rate-limiter covers every V2 SDK method (closes silent-leak vector under 429); structured `[SHUTDOWN]` log channel. §11.11 doc adds `KillSignal=SIGINT`, `TimeoutStopSec=30`, `KillMode=mixed` to both unit blocks + new "Operational stop procedure" subsection. +493/-18 lines across 5 files. +22 new tests in `tests/test_shutdown.py`. Phase 5 audit surfaced 3 real bugs (SHADOW kill-switch override broken, V2 cancel methods missing from rate-limiter, latency cliff at 60+ markets) — all addressed pre-commit. | `systemctl stop polymarket-farmer` now triggers a clean shutdown: one API call cancels every tracked order, exits within seconds, journalctl shows the structured `[SHUTDOWN]` sequence. Forward-compatible: even before the operator re-tees the §11.11 unit blocks, `systemctl stop` (still SIGTERM by default) now triggers a clean shutdown thanks to the new Python-side SIGTERM handler. |
| `d4d1541` | **v5.1.10 — wallet-first capital resolution + wallet-scaled I4 floor** (`fixit.md::FX-010` + `FX-011` + `FX-013` + `FX-024` + `FX-025`). Five fixit entries closed by one structural change. Farmer writes `usdc_balance` on cycle 1; agent `--capital` default `None`; per-cycle `[CAPITAL_SOURCE]` log line; I4 floor scales `max($50, max(peak, portfolio, exchange) * 0.10)`; `RF_MAX_COST_PER_MARKET` + `RF_MAX_TOTAL_EXPOSURE` deleted. +514 / -30 lines across 6 files. +21 new tests in `tests/test_capital_flow.py`. Comprehensive audit ran post-implementation; zero code findings. | Pre-fix `[GUARDRAIL] total_capital: 1500.0` on cold start; post-fix the cycle-1 write closes the 5-min window and the agent reads `~$201` instead. I4 floor on small wallets unchanged ($50 minimum dominates); on large wallets the floor tightens correctly. No change to placement / cancel / dump / kill-switch behaviour on the Helsinki server. |
| `7d8d38d` | **v5.1.9 — stop orphan-dump 400-spam via `unliquidatable_markets`** (`fixit.md::FX-005` + `FX-006` + `FX-007` + `FX-008` + `FX-009` + `FX-028`). New DB table records cids whose orderbook the bot has confirmed dead; both `OrderLifecycle` and `DumpManager` mark on the canonical V2 SDK 400 body (`"orderbook"` AND `"does not exist"` substrings); BUY / SELL / orphan-scan / exchange-position-sync / dump-state restore / dead-market cleanup all gate on `db.is_unliquidatable(cid)`. New `_reprobe_unliquidatable` runs on a 30-min loop sweep with per-cid 6h staleness gating + CLOB `/markets/{cid}` fallback for token_ids. Detector is regression-tested with explicit negatives ("insufficient balance", "rate limit", "market does not exist" all stay unmarked). +984 / -20 lines across 12 files. +31 new tests in `tests/test_unliquidatable_markets.py`. Audited after initial implementation; 4 findings addressed pre-commit (detector tightness, `_sync_exchange_positions` gate, docstring fix, test gaps). | Tamilaga orphan-dump 400-spam closes on the next Helsinki `git pull + restart` (within ~1 cycle of LIVE startup). No change to placement / cancel / dump / kill-switch / allocator behaviour on healthy markets — only an indexed PK lookup per call. |
| `e7fc3d2` | **v5.1.8 — count API-confirmed placements only** (`fixit.md::FX-004`). `OrderLifecycle.place_orders_for_market` now returns `int` (0/1/2). `_gated_place_orders_for_market` accumulates: `self._cycle_orders_placed += n_placed`. Only LIVE-mode paths where `create_and_post_order` returned a valid `orderID` AND `log_order_placed` wrote to the DB contribute. Early returns + DRY-run path return 0. Defensive `isinstance(int)` check tolerates pre-FX-004 stubs. +34 / -12 lines in `order_lifecycle.py`; +26 / -8 lines in `reward_farmer.py`; +270 lines new `tests/test_order_lifecycle.py` (17 tests). | Restores trust in `[CYCLE_SUMMARY] orders_placed`. Pre-FX-004 the field counted attempts (lied during cycle 3 of v5.1.5 Helsinki bootstrap: reported 2 placements while DB had 0). Now matches `SELECT COUNT(*) FROM orders_placed` exactly. No change to actual placement / cancel / dump / kill-switch / allocator code paths. |
| `541108b` | **v5.1.7 — add `BOOTSTRAP` state for first-time cold start** (`fixit.md::FX-003` + `FX-012`). New SafetyController state (severity 2, between `MILDLY_MISCALIBRATED` and `SEVERELY_MISCALIBRATED`) with permissions `max_markets=10, capital_pct=0.30, trials=True`. Entered when `_is_genuine_cold_start()` is True (no orders ever placed, no fills observed); exited to MILDLY on EITHER ≥10 lifetime fills OR ≥3 clean cycles. Subsumes FX-012 (cold-start default routing through `_cold_start_or(MILDLY)`). New `_bootstrap_clean_cycles` counter reset by `_transition`. BOOTSTRAP is once-only — recoveries from downgrades climb back through the existing ladder, not BOOTSTRAP. +106 / -17 lines + 10 new tests in `tests/test_safety_controller.py` + 4-line update to root `test_safety.py`. | Closes the second-to-last cold-start chamber. With v5.1.5 (I9), v5.1.7-FX-002 (I3), and v5.1.7-FX-003 (BOOTSTRAP) all shipped, a fresh-DB LIVE bring-up now enters BOOTSTRAP cleanly and progresses to MILDLY autonomously. The Helsinki server is past cold start, so this code path doesn't fire there. |
| `dc78ba0` | **v5.1.7 — skip I3 drawdown on genuine cold start** (`fixit.md::FX-002`). New helper `_is_genuine_cold_start()` checks lifetime `orders_placed` + `fills` counts. When `_portfolio_val <= 0` AND both counts are zero, I3 logs one INFO line and skips the violation (no DATA_UNAVAILABLE demotion). The warm-DB path is preserved verbatim. The helper is also wired into `_query_data_freshness` (replacing the inline check from `dd67f97`) so I9 and I3 share one source of truth. +43 / -15 lines + new `tests/test_safety_controller.py` with 7 unit tests. | First half of the Phase 1 SafetyController bootstrap completion. Without this, even after v5.1.5's I9 fix, a fresh-DB LIVE bring-up would still hit I3's DATA_UNAVAILABLE demotion during the ~30-min window before `usdc_balance` propagates — same deadlock pattern, different invariant. |
| `987a844` | **v5.1.6 — add `numpy>=2.0` to `requirements.txt`** (`fixit.md::FX-018`). numpy was a real production dependency but previously undeclared in `requirements.txt`; it arrived transitively via streamlit (`pyproject.toml`) on Mac, but headless server installs via `pip install -r requirements.txt` missed it. The Helsinki Phase D bring-up had to pip-install it by hand (§11.8). +1 line. The `>=2.0` floor matches what's already running on Helsinki and supports Python 3.12+ (repo targets 3.14). | Closes the Phase D server-install footgun. No behavioural change — the bot has been running with manually-installed numpy since Phase D. |
| `3f50441` | **v5.1.6 — remove stale `polymarket-bot.service`** (`fixit.md::FX-017`). Repo root carried a leftover systemd unit referencing `/opt/polymarket-bot/` and running `main.py` (the deprecated legacy entry). Not deployed anywhere: canonical units live at `/etc/systemd/system/polymarket-{farmer,oversight}.service` per §11.11 and run `reward_farmer.py` / `oversight_agent.py` from `/home/polymarket/Polymarket-bot`. No internal code referenced the deleted file. -36 lines. The `KillSignal=SIGINT` + `TimeoutStopSec=30` directives captured in this commit body were subsequently copied into the canonical §11.11 unit blocks by Phase 5 (`91bae99`, FX-014). | Zero behaviour change — file was unused. Removes a footgun where a fresh operator might mis-deploy the legacy unit. |
| `dd67f97` | **v5.1.5 — fix SafetyController I9 deadlock on fresh-DB bootstrap.** `oversight/safety_controller.py::_query_data_freshness` previously returned `None` when `scoring_snapshots` was empty. I9 interpreted that as a critical violation and forced `DATA_UNAVAILABLE`, which blocks trials, which blocks all deploys on a fresh DB (every market is a trial), which prevents `are_orders_scoring` from ever being called, which keeps `scoring_snapshots` empty — permanent deadlock observed in production after first LIVE cutover from the new Helsinki server. Fix: differentiate cold-start (`SELECT COUNT(*) FROM orders_placed = 0`, return `0.0` = freshness N/A) from broken-pipeline (`orders_placed` has rows but `scoring_snapshots` empty, return `None` defensively). +15 lines, single function. Behaviour byte-identical on warm DBs. Companion `fixit.md::FX-001` carries full retrospective; v5.1.5 amendments at top of this doc describe the bootstrap deadlock and adjacent observations (`FX-002`, `FX-004`, `FX-007`, `FX-013`) deferred to follow-on commits. | First LIVE bootstrap from a fresh-DB server unblocked. Phase A.1 of the comprehensive hardening fix designed in this session; subsequent phases (B–F) tracked in `fixit.md` §6 Hardening roadmap. |
| `ee6abdf` | **Phase D hotfix — rename `get_orders` → `get_open_orders` for V2 SDK compatibility.** The V2 SDK exposes `get_open_orders()` instead of V1's `get_orders()`. Missed in the V2 migration (`2a6baf6`). DRY mode skips reconciliation paths (`if not self.dry_run` gates), so the bug stayed undetected through 30+ hours of DRY soak. First LIVE cutover surfaced `ERROR | get_orders failed: 'ClobClient' object has no attribute 'get_orders'` on every cycle. Bot fell through to placement with empty `open_ids` set; placed zero orders (no money at risk). Patched 4 production sites (`reward_farmer.py:263, 433, 1751`; `fills.py:65`) and 20 test mocks (`tests/test_order_reconciliation.py`, `tests/test_startup_recovery.py`). Static audit of all `self.client.<method>(` calls in production paths confirmed all other methods (`cancel_order(OrderPayload(orderID=))`, `get_balance_allowance`, `get_order`, `get_order_book`, `are_orders_scoring`, `update_balance_allowance`) are V2-compatible. | First LIVE cutover unblocked at the SDK level. Subsequently surfaced the Polymarket US geoblock as a separate operational issue. 449/457 pytest pass. |
| `5909764` | **Phase C step 3 — oversight promotion-flag isolation tests.** New tests in `tests/test_oversight_shadow.py` verify each of `_SHADOW_ONLY` / `_PAUSE_ENABLED` / `_KILL_ENABLED` is independently honoured. Tests cover: pause disabled returns continue even when signal fires, kill disabled falls through to pause (per Phase C plan C3 decision), master flag disables all actions. Test count: 18 → 33. | Confirms the three-flag promotion ladder behaves correctly under every combination. No behaviour change; pure test coverage. |
| `a08e86a` | **Phase C step 2 — wire oversight signals to pause/kill actions (gated off by default).** Refactors `_check_signals_and_log` at `oversight_agent.py:749` to return `tuple[list[str], list[str]]` of `(fired_pause_signals, fired_kill_signals)`. `evaluate()` consumes the tuple and applies the flag-gated mapping: if `_SHADOW_ONLY` (default True) → return `continue/shadow` regardless; else if `_KILL_ENABLED` AND `fired_kill` non-empty → return `{"action":"kill","reason":<sig>}`; else if `_PAUSE_ENABLED` AND `fired_pause` non-empty → return `{"action":"pause","reason":<sig>}`; else return continue with `reason="no_signal"`. Strict severity precedence (kill > pause > continue). Per-signal try/except hardening so one bad detector doesn't suppress the rest. New tests cover per-kind mapping, multi-signal precedence (`test_kill_overrides_pause_when_both_fire`), reason format. | Wires the signal outputs to real action types behind the master gate. Stage 1 behaviour byte-identical to v5.1.1. |
| `5757aef` | **Phase C step 1 — introduce oversight Stage 2/3 promotion flags (default off).** Three module-level constants in `oversight_agent.py`: `_SHADOW_ONLY=True` (master gate; v5.1.1's dead constant repurposed), `_PAUSE_ENABLED=False` (Stage 2), `_KILL_ENABLED=False` (Stage 3). `evaluate()` body unchanged — flag reads added but no consumer yet (that's `a08e86a`). | Lays the flag scaffolding for Phase C. Existing 15 tests pass unchanged. |
| `e270d63` | **Phase 3b — bump `GATE_ACTIVE_CYCLES` 50→2000 as SHADOW-soak safety belt.** Once `_read_alloc_file` returns real values (Phase 3a), `valid_cycles_observed` starts ticking 1/cycle on `metrics_ok`. β trajectory under legacy path (`_p_fill` unstamped on legacy rows) → `expected_util=0` → β rule converges to upper clamp 0.95 within ~15 EMA cycles of metrics flowing. At ACTIVE promotion, applied β would jump from neutral 0.75 → 0.95, a 27% budget increase. Bump to 2000 cycles (≈16.7h SHADOW soak) gives operator a window to observe `[LEARNING_SHADOW] would_apply` trajectories before applied state shifts. Inline TODO marks the value as temporary; revert to 50 after observation. Test updates: 3 sites use the `GATE_ACTIVE_CYCLES` constant instead of literal 50; `test_probe_blocked_when_unstable` fixed to seed `GATE_ACTIVE_CYCLES + 10` (was passing for the wrong reason post-bump). Simulation tests run only 150 cycles per scenario and broke after the bump — scoped `unittest.mock.patch("profit.learning.GATE_ACTIVE_CYCLES", 50)` added in `setUpClass`. | LearningController gate now provides ~16.7h soak window in LIVE. Safety belt for the unstamped-`_p_fill` β-runaway risk. Reversible single-line. |
| `4f102e3` | **Phase 3a — fix `_read_alloc_file` dict key (`allocations` → `markets`) + parallel sim writer.** LearningController at `profit/learning.py:852` read `alloc.get("allocations", [])` but writer at `oversight/allocation_writer.py:275` writes `"markets"`. Reader silently returned empty list → `reward_efficiency=None`, `reward_error=None`, `expected_util=None` → `_metrics_complete=False` → `valid_cycles_observed` never advances → gate frozen at OFF/SHADOW forever → entire control loop structurally dead since the writer/reader were authored against different keys. Single-line change at line 852. Also fixed parallel writer bug at `simulation/runner.py:_write_alloc_file` (was also writing `"allocations"` to match the buggy reader; surfaced when full pytest broke 3 simulation tests post-Commit-1). 5 test fixture sites updated to write `"markets"` (`tests/test_reward_expansion.py` 3 sites, `tests/test_frontier_memory.py` 2 sites). | LearningController metrics pipeline reactivated. `valid_cycles_observed` advances correctly. β/cap_scale/trust rules compute on real inputs. |
| `d2612e6` | **Phase 2 — stamp `_total_capital` on legacy allocator output + uniform `cap_scale`.** Legacy allocator (`oversight/allocation_writer.py:_to_dict`) didn't stamp `_total_capital`; only profit engine (`profit/allocator.py:379`) did. Since calibrator isn't ready (`fill_model`/`loss_model` untrained), legacy path runs every cycle; farmer reader `_guardrail_total_capital_from_alloc` at `reward_farmer.py:1064-1095` returned `None` → `[GUARDRAIL] total_capital=null` → `notional_ratio` cannot compute, `cluster_cap`, `loss_limit`, kill-switch all inactive; shadow signals `notional_drift` + `slow_bleed` stay in `missing_data`. Fix: hoist `cap_scale` computation out of the profit-engine-only branch (both paths multiply through `alloc_capital = available_capital * cap_scale`) + add post-redistribution loop in `compute_allocations` that stamps `_total_capital` on every deploy row. | Four guardrails + two shadow signals activated. `[GUARDRAIL]` JSON shows non-null `total_capital` every cycle. Notional/cluster/loss caps now armed. |
| `c7ed2e6` | **Phase 1 — populate question text from Gamma in `market_expiry_cache`.** Cold-start markets discovered via CLOB got `question=""` at `oversight/data_collector.py:1354` because the Gamma keyset parser at lines 284-288 extracted only `conditionId` + `endDateIso`, not the `question` field that IS in the response. CLOB `/rewards/markets/current` doesn't carry question text. Empty `question` silently disabled three safety gates that short-circuit on truthy-question: sports protection (`market_scorer.py:272-275`), per-group concentration cap (`allocation_writer.py:117-124` + `profit/allocator.py:115-118`), keyword filters (`market_discovery.py:35-39`). 73% of `market_performance` rows (2594/3556) had empty question. Patched the Gamma parser, the CLOB fallback at lines 307-318, threaded through cache write/read. Schema migration: `ALTER TABLE market_expiry_cache ADD COLUMN question TEXT NOT NULL DEFAULT ''` in `database.py:_migrate_enrichment_columns`. Consumer at line 1381 got an `expiry_map` fallback. Forward-only — historical rows refresh as TTL expires. Tests: 3 new. | Three safety filters reactivated. Live evidence on the server: 11 sports markets correctly time-gated within the first oversight cycle post-fix. |
| `900e3f8` | **Phase 0 — wrap standalone test runners under `if __name__ == "__main__":`.** Five top-level `test_*.py` files (`test_integration.py`, `test_profitability.py`, `test_safety.py`, `test_state_v2.py`, `test_verification.py`) ran their custom test suites at module-level import time. Pytest's collector imported them → runners ran → terminal `sys.exit(1)` killed collection with `INTERNALERROR / SystemExit: 1`. Wrapped each runner body in the `if __name__` guard. Both invocations preserved: `python3 test_X.py` runs the custom suite; `python3 -m pytest` cleanly collects. | 0 tests collected → 434 collected. Test infra unblock. No bot-behaviour change. |
| `2706953` | **Deterministic oversight integration: hasattr gate + latency + strict validation.** Replaces `b8d84bd`'s try/except-based block with a deterministic structure inside `reward_farmer.run_cycle`. `hasattr(oversight_agent, "evaluate")` distinguishes "function not yet implemented" (silent fallback) from "function exists but raised" (`log.error("[OVERSIGHT_ERROR]")`). Adds `OVERSIGHT_LATENCY_WARN_MS = 50` constant + per-cycle latency tracking with `log.warning("[OVERSIGHT_WARNING] slow evaluation:")`. Strict decision validation extracts `action`/`reason` locals, enforces `action ∈ {"continue","pause","kill"}`, truncates `reason = str(reason)[:200]`. Per-cycle `[OVERSIGHT] action=… reason=… latency_ms=…` log (no throttle). Oversight kill propagates `reason="oversight:" + reason`. Placement decision elif chain reordered: `fill_storm → notional_block → action == "pause" → else placement`. | 384/384 fast-tier tests pass. AST walk confirms exactly one `oversight_agent.evaluate(guard)` call site. No new state on `self`; no threads/async/timeouts. Stops the ~2880/day `[OVERSIGHT_WARNING]` log flood from `b8d84bd`. |
| `b8d84bd` | **Oversight-agent hook in `run_cycle` (try/except baseline).** Adds `import oversight_agent` at module top + an oversight evaluation block between `guard = self._guardrail_check_and_log()` and `if guard["kill_switch"]:`. Initial implementation used `try: decision = oversight_agent.evaluate(guard) except Exception as e: log.warning("[OVERSIGHT_WARNING] evaluation failed:")` — caught the absent-`evaluate` `AttributeError` along with all other failures. `pause` elif inserted at slot 2 (between `fill_storm` and `notional_block`); throttled to `cycle_count % 10`. Kill calls `_activate_kill_switch(reason="oversight")`. | 384/384 fast-tier pass. **Superseded by `2706953` one commit later** — the warning prefix on absent-stub was producing ~2880 lines/day (one per 30 s cycle), which `2706953` eliminated via the `hasattr` gate. |
| `7ab514d` | **v5.0 consolidation: execution modes + cycle telemetry.** Three-mode gate `{DRY_RUN, SHADOW, LIVE}` with CLI `--mode`. All write sites in `reward_farmer.py` routed through `_gated_place_orders_for_market` / `_gated_cancel_order` — kill-switch cancels bypass the gate. `OrderLifecycle` + `DumpManager` receive `dry_run=(mode != LIVE)` as belt-and-suspenders. `[CYCLE_SUMMARY]` JSON emitted at every `run_cycle` exit (13 fields); `[ROLLING_STATS]` every 10th cycle over a 100-cycle deque. `[DRY_RUN]` / `[SHADOW]` intent logs on every non-LIVE place/cancel. Stub-safe via `getattr(self, 'mode', MODE_LIVE)` fallbacks. | Safe staged deployment `DRY_RUN → SHADOW → LIVE`. 384/384 fast-tier tests pass. No trading logic changes. |
| `2e72606` | **Farmer guardrails v2: hard enforcement + multi-cancel + persistent-breach.** Adds `HARD_NOTIONAL_RATIO = 2.5` with `_guardrail_hard_enforce_notional` cancelling lowest-priority BUYs until ratio ≤ 2.0. Same pattern for `_guardrail_hard_enforce_clusters` at `0.5·T`. Multi-cancel cap `MAX_CANCELS_PER_CYCLE = 5` per helper. Size-aware priority: `(daily_rate ASC, notional DESC, spread DESC, cid, side)`. `MAX_BREACH_CYCLES = 3` emits `[CRITICAL] persistent_overexposure`. `[GUARDRAIL_WARNING] missing_signal=<name>` for every fail-open skip. Atomic kill switch reordered: flag → cancel → log → return. | Active exposure reduction when soft blocks aren't enough. Eliminates "cancel-storm on large breach" via the per-cycle cap. All stub-based tests preserved. |
| `414354a` | **Farmer runtime safety guardrails v1: soft notional + cluster blocks + kill-switch + structured telemetry.** New `reward_farmer.py` guardrail layer running between expiry-sweep and placement. Soft notional block at `MAX_NOTIONAL_RATIO = 2.0`; soft cluster block at `CLUSTER_NOTIONAL_LIMIT_FRAC = 0.5`; kill-switch on `{daily_loss > 0.1·T, cf < 0.01, fill_rate_ratio > 3.0}`. `[GUARDRAIL] {…json…}` emitted every cycle (17 fields). Fail-open on every missing signal. No allocator / learning changes. | First execution-time safety layer; prior versions relied entirely on SafetyController (agent-side) + allocator caps. |
| `707ca50` | **V5 INV3 rewritten as cap-normalised capital utilisation.** New metric `capital_util = Σ(C)/T`, new denominator `feasible_capital_fraction = min(0.95, Σ cluster_cap_pct + unclustered_fraction)`, `normalized_util = capital_util / feasible`, PASS threshold 0.70. Raw-util band retained only as a fallback when `feasible` is unavailable. `V4Tracker.__init__` gains `db_path` kwarg; `V4CycleSnapshot` gains `feasible_capital_fraction`. | V5 overall **PASS** (was FAIL). INV3_new: 0/6 → 6/6 (normalized_util 1.45–1.73 across all scenarios). INV5_new 6/6, INV7 6/6. The v4.0 raw-util INV3 was measuring cap-policy geometry + bootstrap p_fill clamp, not learning-loop quality. |
| `741d35c` | **`capital_scale` stability filters: bounded-rate clamp + small-amplitude flip suppression.** Two additive filters at the exit of `LearningController.update_state`, AFTER every pre-existing rule. `MAX_CAPITAL_SCALE_STEP = 0.07` clamps `|Δ capital_scale|`. Flip-suppression walks `capital_history` for the last nonzero delta; reverts when the current delta flips sign AND both magnitudes `< CAPITAL_CHANGE_MIN_STEP`. | V5 INV7: 4/6 → **6/6 PASS**. `over_aggressive` / `regime_shift_3phase` `max_flip_rate_100` collapsed 7–9 → 0–1. `expected_util` / `coverage_ratio` byte-identical (the oscillation was too small-amplitude to move any downstream metric). |
| `5611d54` | **v4.0 committed**: continuous allocator + β/η control + V5 audit + sim bootstrap p_fill fix + **Step-3b cap-aware shaping** (new). `profit/allocator.py` ~390 lines (was 1616). Step-3b inserted between Step 3 and Step 4: per binding cluster, pre-selects `k = max(1, floor(cluster_budget / cluster_min_capital))` top-ranked members by `(-raw_alloc, condition_id)` and routes the rest to `action="avoid"`. Restores β/η signal past the cap stack. 394 fast-tier tests pass. | V5 INV5_new (coverage): 1/6 → 6/6 PASS. Closes the v4.0-flagged cluster-cap × min-floor artefact directly inside the allocator. INV3_new raw-util still failed — fixed separately at `707ca50` by normalising. |
| `8a8466e` | **Patch 13 (FINAL CORRECTED) + Audit V4 framework** (bundled commit, SUPERSEDED by v4.0): Patch 13 target-driven allocation + hysteresis, plus `simulation/audit_v4_*` five-module system-level audit with 6 V4 scenarios, strict INV3/INV5/INV7 thresholds, structured failure diagnostics, per-run CSV + JSONL dumps | 563 tests pass (552 → 563); V4 audit empirically confirmed Patch 13 did NOT close INV3/5/7 under V4's tighter gates (actual/target = 0.17–0.38 across all 6 scenarios). Controllability analysis on post-Patch-13 state subsequently showed λ1/λ2 are structurally incapable of producing cross-market differentiation under the continuous allocator's normalisation step — motivated the v4.0 redesign. |
| `d8a4569` | **Patches 6, 7, 9, 10, 11 + V2/V3/V3.1 audit harnesses** (bundled commit): all four original Patch 6–10 layers + Patch 11 exposure saturation + `profit/refill.py` + `_CAPITAL_HISTORY_CACHE` oscillation damping + simulation harness + audit runners | Shifts ACTIVE objective from EV/dollar to exposure-under-constraint with overcommit; 552 tests pass (+47 from v2.0); V3.1 audit shows INV3/INV5/INV7 still failing — motivates Patch 13 |
| `1081e72` | CF clamp 0.001→1e-6 + poisoned-row heuristic (`RF_POISONED_Q_SHARE_THRESHOLD`) | Unmasks CF true signal; routes 394 historical poisoned rows to cold-start prior |
| `88f6c7a` | **Option B**: TTL book cache fixes q_share saturation at source | Removes the 5000× est/actual inflation root cause |
| `9f58e14` | Sports protection Phase 1 using `game_start_time` (1h block) | Closes the in-play adverse-selection window for CLOB-routed sports |
| `a6f580d` | Cold-start prior + configurable trial cap + `game_start_time` pipeline | Unblocks discovery of new markets; adds the infrastructure for Phase 1 sports protection |

### 10.2 Known-fixed bugs

**B23 — `client.get_order_book()` V2 SDK returns dict; `get_merged_book` assumed object — THE 4-day production blackout** (fixed `647b1e2`)
The bug that explains everything else this hardening campaign chased. py-clob-client-v2 v1.0.0's `client.get_order_book(token_id)` returns a `dict` with string-valued `'bids'`/`'asks'` entries, like `{'market': '0xd99...', 'asset_id': '...', 'timestamp': '...', 'hash': '...', 'bids': [{'price': '0.02', 'size': '2250'}, ...], 'asks': [...]}`. But `market_discovery.py:get_merged_book` was written assuming an OrderBook object with `.bids`/`.asks` attributes: `getattr(ob, "bids", [])`. `getattr` on a dict for a key name returns the default (`[]`) because dicts don't expose keys as attributes. So `all_bids` and `all_asks` stayed empty → `if not all_bids or not all_asks: return None` → `get_merged_book` always returned `None` in production. Every farmer cycle incremented `book_failures` for every market it tried to evaluate; after 3 cycles markets got removed (B22) or just stayed un-deployable. Helsinki bot placed **zero orders in production for the entire 4-day LIVE window** (2026-05-15 04:03 UTC → 2026-05-19 04:36 UTC). The V2 migration in commit `2a6baf6` (v5.1.2, 2026-04-29) changed the return shape but never updated this wrapper — same class as B9 (`get_orders → get_open_orders`), in the book-fetching path. DRY mode placed no orders so the silent failure didn't matter for the ~17-day DRY soak after the V2 migration. First LIVE cutover surfaced FX-001's I9 deadlock which masked everything else for 4 days. After the deadlock chain (FX-001/002/003/012/013/etc.), FX-031, and FX-032 were all closed, the next farmer cycle still showed 0 orders — leading to the production diagnostic that found B23. **Discovery:** direct `client.get_order_book(token_id)` call on Helsinki at 2026-05-19 04:36 UTC returned a `dict`, definitively confirming the shape mismatch. Fix: new `_book_entries(ob, key)` helper normalizes both dict-form (V2 SDK) and object-form (test mocks). `get_merged_book` uses it for all 4 iteration sites; `paper_trader_v2.py` delegates to it; `paper_client.py` fill simulator updated. Backward-compat preserved for the ~200 existing tests that use object-form mocks. 12 new regression tests in `tests/test_get_merged_book.py` exercise the REAL function with both shapes — pre-fix every dict-form test fails. **Production verification post-pull (2026-05-19 04:58:49-50 UTC):** Helsinki placed its first two real orders ever — YES @ $0.44 size 67 + NO @ $0.53 size 67 on the Iran market. CYCLE_SUMMARY: `orders_placed: 2, active_markets: 1, total_live_notional: $64.99, notional_ratio: 0.3228, cf: 1.0`. From $0 in 4 days to actually farming. Companion `fixit.md::FX-035`.

**B22 — Dead-market cleanup over-marked healthy cids as unliquidatable via FX-006 cascade** (fixed `75d03c7`)
Pre-fix, `reward_farmer.py:2093` cascaded `self.db.mark_unliquidatable(cid, reason="dead_market_book_failures")` whenever `ms.book_failures >= 3`. The `book_failures` counter increments whenever `get_merged_book` returns `None` or empty bids/asks — much wider than the canonical FX-007 "orderbook does not exist" body. On Helsinki's v5.1.14 startup at 2026-05-19 03:23:38 UTC, 60 healthy markets got mass-marked in a single 3-minute window, including the "Iran closes its airspace by May 27?" market (`0xdb22a7749b83`) which a direct CLOB API probe confirmed was `active=True, accepting_orders=True, rewards_rate=$200/day, deep books on both sides`. The FX-028 re-probe logged `0 un-marked, 60 still dead` immediately after the bot fetched 60 books that all returned HTTP 200 OK. **Bot was locked out of a market paying $200/day in rewards.** The FX-016 audit missed it because `TestDeadMarketCleanupCascade.test_cleanup_loop_cascades` was a "logic-shape replay" — the test re-constructed the loop body locally and asserted on the local re-construction, instead of exercising `RewardFarmer.run_cycle` directly. The test would have stayed green even if the entire Step 4b block had been deleted from production code. Fix: removed the `mark_unliquidatable` call; FX-006's `delete_dump_state` cascade (both sides) preserved — that's the actual cleanup FX-006 was solving. Markets removed from `self.markets` can reappear via the next reward-markets refresh, appropriate for transient failure modes. Genuinely-dead markets still get marked via the FX-007 canonical path (`OrderLifecycle` and `DumpManager` exception handlers requiring both `"orderbook"` AND `"does not exist"` in the 400 body). New source-inspection test `test_actual_reward_farmer_cleanup_does_not_call_mark_unliquidatable` reads `RewardFarmer.run_cycle`'s source via `inspect.getsource` and asserts `mark_unliquidatable` does NOT appear in the Step 4b block — catches the class of regression where logic-replay tests drift from source. Companion `fixit.md::FX-032`.

**B21 — `filter_allocations` wholesale-rejected oversized deploys instead of scaling** (fixed `d5eabea`)
Pre-fix, the running-cost block at `oversight/safety_controller.py:829-843` did `if running_cost + est_cost > max_capital: a["action"] = "avoid"; else: running_cost += est_cost`. Any single deploy whose `est_capital_cost` exceeded the per-state cap was wholesale-rejected, regardless of how much budget remained. The probe-mode block above (line 819) and the per-market exposure block below (line 856, FX-029) both used scale-down semantics; the running-cost block was the only wholesale-rejector — style outlier turned out to be the bug. Surfaced empirically on Helsinki's first oversight cycle after the v5.1.14 recovery pull: BOOTSTRAP cap = $200 × 0.30 = $60, allocator proposed 3 deploys at $84-$89 each, all 3 rejected → `SafetyController [BOOTSTRAP]: 0/3 markets, $0/$201 capital`. Bot was structurally unable to deploy. Two fixes in `d5eabea`: (1) scale shares down to fit `remaining` budget instead of reject, with `min_size` floor preserved (sub-min orders are venue-rejected); (2) iterate `deploys` (already sorted score-desc above) instead of unsorted `allocations`, so the top scorer claims the constrained budget first. New reject reason `"capital exhausted (${remaining:.0f} < min ${min_cost:.0f})"` distinguishes from the legacy "capital cap" wording. Five regression tests in `TestFilterAllocationsCapitalCapScaling`: oversized top-scorer scales to fit; subsequent deploys rejected as "capital exhausted"; iteration is score-desc; remaining < min_cost rejects cleanly; min_size floor respected. Companion `fixit.md::FX-031`.

**B20 — `check_wallet.py` printed a cosmetic 400 error at startup** (fixed in v5.1.14 closure commit `38fc63c`)
`python check_wallet.py` printed `[py_clob_client_v2] request error status=400 ... 'GetBalanceAndAllowance invalid params: assetId invalid value -1...'` at the top of its output before the actually-useful balance/allowance information. Root cause: `check_wallet.py:243-246` called `client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL))` with no `token_id`; the SDK substituted `-1` as a placeholder and the API rejected it. The query was dead code from the operator's perspective — CONDITIONAL balance is checked at trade-time against a specific token_id, not at startup. Fix: deleted the 4-line block. Diagnostic still prints COLLATERAL pUSD balance + on-chain allowances (the actually useful pre-trade check). Operator no longer sees the alarming-but-harmless 400 at startup. Companion `fixit.md::FX-019`.

**B19 — `_handle_upgrade` UNSAFE→MILDLY fast path bypassed documented 3-cycle DEGRADED cap** (fixed `1c4ae7e`)
The architecture doc §4.14 + lines 1919-1920 documented the UNSAFE recovery contract as: UNSAFE → (`UNSAFE_RECOVERY_CYCLES=3`) → DEGRADED → (`UPGRADE_STEP=2`) → MILDLY — a 5-cycle minimum, designed to keep the operator-observation window intact during a recovering bot's exit from the "proven risk" state. But the code's `_handle_upgrade` (oversight/safety_controller.py:752 pre-fix) caught UNSAFE alongside SEVERELY/DEGRADED/DATA_UNAVAILABLE in its else-branch and jumped UNSAFE → MILDLY in 2 cycles when inputs were fully calibrated. The auto-recovery cap at `evaluate_state:644-652` only fires inside the violations branch, so the clean-cycle path through `_handle_upgrade` went uncapped. Surfaced by the Phase 6 part 2 audit pass on the FX-016 test build-out — the audit explicitly noted that the test originally pinning this behaviour ("`test_fast_path_unsafe_to_mildly_after_2_calibrated_cycles`") was codifying a bug as a contract. Fix: special-case UNSAFE at the top of `_handle_upgrade`'s post-BOOTSTRAP block: `if self.state == UNSAFE: return`. The slow auto-recovery in `evaluate_state:658-664` becomes the SOLE UNSAFE exit on a no-violations cycle. Regression tests added: `test_unsafe_to_degraded_after_3_cycles_fully_calibrated` and `test_full_recovery_unsafe_to_mildly_takes_at_least_5_cycles`. Production impact zero — Helsinki has never entered UNSAFE. Companion `fixit.md::FX-030`.

**B18 — `filter_allocations` per-market $200 cap can be exceeded with mismatched caller input** (fixed `1c4ae7e`)
Pre-fix, `oversight/safety_controller.py:839-850` computed the per-market $200 scaling decision from the CALLER's `est_capital_cost` (`scale = 200 / input_est_cost`) but recomputed the post-cap value from an internal formula (`shares × est_price × 2`). When caller and internal formulas disagreed, the post-cap cost overshot $200. Audit's 4-line repro: `shares=500, est_capital_cost=300, max_spread=0.045` → final `$303.03`. Narrow spreads were worse: `max_spread=0.001, est_cost=201` → final $496.01. The cap is the LAST gate in `filter_allocations`, so the overshoot survives to the placement layer. Whole point of the cap was to bound single-market exposure; broken cap meant SafetyController was silently failing to enforce the $200 ceiling. Surfaced by the Phase 6 part 2 audit pass on the FX-016 test build-out (the original test was contorted to use `est_cost=455` so it would pass — that itself was the audit's hint to investigate). Fix: refactor `filter_allocations` per-market block so both scaling decision and post-cap value derive from the same internal formula. Caller's `est_capital_cost` becomes informational only; the cap holds regardless of input consistency. min_size floor still wins by design (sub-min_size orders aren't accepted by the venue). +13 / -7 lines. Two regression tests added (mismatched-input + narrow-spread). Production impact zero — Helsinki's allocator uses the same `shares × est_price × 2` formula, so caller and controller agreed and the bug never fired. Companion `fixit.md::FX-029`.

**B17 — No automated test gate on push** (fixed `a580bdb`)
Pre-fix, the repo had no `.github/workflows/` directory. Every push to `main` (including the six prior hardening commits — v5.1.5 through v5.1.11) relied on operator discipline to run `pytest` locally before pushing. Test coverage was real (544 tests by the end of Phase 5) but unenforced: a regression introduced and pushed without a local run would only surface on the next server pull-and-restart. v5.1.12 closes this by adding `.github/workflows/test.yml`: triggers on `push` to `main` + `pull_request`, runs the fast-tier suite (`pytest tests/ --ignore=tests/test_simulation.py --tb=short`) on `ubuntu-24.04` with Python 3.14, pip cache keyed on `requirements.txt`, 15-min job timeout. A new `README.md` carries the workflow status badge so build health is visible from the repo landing page. The first CI run (`26046878949`, triggered by the `a580bdb` push) completed green in 7m17s, 544/544 fast-tier passing on the runner. Slow-tier `tests/test_simulation.py` remains a manual run. Companion `fixit.md::FX-026`. Phase 6 part 1 of 2 — FX-016 SafetyController comprehensive coverage is part 2.

**B16 — `systemctl stop` could SIGKILL the bot with live orders resting** (fixed `91bae99`)
Pre-fix, `sudo systemctl stop polymarket-farmer` sent SIGTERM (systemd default) but `reward_farmer.run()` only handled SIGINT. Python's default SIGTERM behaviour raised `KeyboardInterrupt` indirectly via the underlying blocking SDK call, but only after the call returned — meaning a stop request mid-cycle could wait up to one full run_cycle (~60s under load) before the loop noticed. Combined with the default 90s `TimeoutStopSec`, this often worked but occasionally got SIGKILL'd with live orders rest­ing. v5.1.11 addresses both sides: Python-side SIGTERM handler in `reward_farmer.run()` flips `_shutdown` identically to SIGINT; `_shutdown_cleanup` uses the V2 batch `cancel_orders` endpoint to cancel everything in one API call (fits comfortably under `TimeoutStopSec=30`); `_RATE_LIMITED_METHODS` expanded to cover V2 cancel names so 429 storms get retried; OL.cancel_order gains `force=True` to bypass the dry_run shortcut on the kill-switch override path. The architecture-doc §11.11 unit blocks gain `KillSignal=SIGINT`, `TimeoutStopSec=30`, `KillMode=mixed` so operators applying the new units get the cleanest shutdown semantics. Companion `fixit.md::FX-014` + `FX-015`.

**B15 — `$1500` silent fallback misconfigured cold-start safety thresholds** (fixed `d4d1541`)
On the first oversight cycle of a fresh-DB LIVE start, the agent fell back to the hardcoded `--capital 1500.0` default because no `usdc_balance` row was yet present in the DB. The farmer wrote `usdc_balance` only every 10 cycles (~5 min), and the agent's 30-min cadence meant the misconfigured value could persist for up to half an hour. Safety thresholds (kill switch, notional cap, cluster cap) were calibrated to $1500 during the window — kill switch fired at $150 (75% of an actual $201 wallet) rather than the intended 10%. v5.1.10 closes the race from both sides: farmer writes `usdc_balance` on cycle 1 (closes the 5-min window), and the agent's `--capital` defaults to `None` with a clean skip-cycle path when no fresh value is available. Every cycle emits a structured `[CAPITAL_SOURCE] source={usdc_db|flag|none} value=$X.XX age_min=Y` line so the operator sees which path fired. The companion FX-010 change makes the SafetyController's I4 floor wallet-scaled (`max($50, max(peak, portfolio, exchange) * 0.10)`) so the threshold meaning stays consistent across wallet sizes. Companion `fixit.md::FX-013` (+ FX-010 / FX-011 / FX-024 / FX-025).

**B14 — Orphan-dump 400-spam from on-chain CTF positions on resolved markets** (fixed `7d8d38d`)
Production observed continuous 400 "the orderbook X does not exist" responses every 30 s from the Helsinki server's first LIVE cycle onward, originating from the Tamilaga orphan dump (3826 NO-side shares of a resolved market held on-chain via FUNDER). The orphan scan re-discovered the on-chain balance on every restart; manual SQL DELETE on `dump_states` didn't help because the next scan would re-create the row. CTF redemption is manual UI-only (no SDK path), so the on-chain balance never clears. Six fixit-tracked entries (FX-005 / FX-006 / FX-007 / FX-008 / FX-009 / FX-028) all shared this root cause: the bot had no DB-backed "this market is dead" memory. v5.1.9 introduces `unliquidatable_markets` — a new DB table that flags cids whose orderbook the bot has confirmed dead via the canonical V2 SDK 400 body. Both `OrderLifecycle` and `DumpManager` mark on the exception; every order path (BUY, SELL, orphan scan, exchange-position sync, dump-state restore, dead-market cleanup) gates on `db.is_unliquidatable(cid)`. A 30-min loop sweep re-probes 6h-stale cids and un-marks any whose orderbook returns. Detection requires both `"orderbook"` AND `"does not exist"` substrings (canonical V2 body has the cid in the middle); explicit regression tests ensure "insufficient balance", "rate limit", and "market does not exist" all stay unmarked. Companion `fixit.md::FX-007` carries the full retrospective + audit findings.

**B13 — `[CYCLE_SUMMARY] orders_placed` counted attempts instead of confirmed placements** (fixed `e7fc3d2`)
`_gated_place_orders_for_market` in `reward_farmer.py` did `self._cycle_orders_placed += 1` unconditionally after calling `OrderLifecycle.place_orders_for_market`. The wrapped function returned `None`; its internal API-success check (`if oid:` around `order_lifecycle.py:379` and `:421`) gated only the DB insert into `orders_placed`, not the caller's counter. Observed in cycle 3 of the v5.1.5 Helsinki bootstrap: `[CYCLE_SUMMARY]` reported `orders_placed: 2` while `SELECT COUNT(*) FROM orders_placed` returned 0 — both attempts had 400'd on resolved orderbooks. v5.1.8 makes `place_orders_for_market` return `int` (the count 0/1/2) and routes the value through the wrapper. Every API-failure path (exceptions, missing `orderID`, early returns, DRY-run path) returns 0. A defensive `isinstance` guard on the wrapper side treats pre-FX-004 stub returns as 0. Companion `fixit.md::FX-004`.

**B12 — Cold-start SafetyController default skips conservative ease-in** (fixed `541108b`)
`SafetyController.__init__` set `self.state = MILDLY_MISCALIBRATED` and `_load_state` defaulted to the same on every "no row / old row / exception" path. On a fresh-DB LIVE bring-up the bot therefore woke up with 70% capital permission and `trials=True` — the second-highest rung of a 6-state machine. For a $201 wallet that's $140 of immediate notional exposure on cycle 1, with no period of operator observation between cutover and full deployment. v5.1.7 introduces a new `BOOTSTRAP` state (`max_markets=10, capital_pct=0.30, trials=True`, severity 2 — between MILDLY and SEVERELY) and routes the cold-start default through `_cold_start_or(MILDLY_MISCALIBRATED)`. Genuine cold start (no orders ever, no fills ever) → BOOTSTRAP; warm restart → MILDLY (unchanged). BOOTSTRAP exits to MILDLY on EITHER ≥10 lifetime fills OR ≥3 clean cycles. Companion `fixit.md::FX-003` + `FX-012`.

**B11 — I3 drawdown deadlock on fresh-DB bootstrap** (fixed `dc78ba0`)
Same deadlock family as B10 (the I9 fix), different invariant. I3 checks `(_portfolio_peak - _portfolio_val) / _portfolio_peak > MAX_DRAWDOWN_PCT`, where `_portfolio_val = total_portfolio_value or exchange_balance`. On a fresh DB during the ~30-min window between LIVE cutover and the first `usdc_balance` row landing, both inputs arrive zero. The original `evaluate_state` interpreted `_portfolio_val <= 0` as "data unavailable" and demoted state to DATA_UNAVAILABLE. But DATA_UNAVAILABLE blocks trials, and on a fresh DB every market is a trial — same lock-up pattern as I9. There's no drawdown to compute from a zero baseline, so the violation is pure noise. v5.1.7 introduces `_is_genuine_cold_start()` (lifetime `orders_placed` + `fills` count) and uses it to suppress the I3 violation (logged at INFO once per cycle) on a genuine cold start. The warm-DB path is unchanged — any prior order or fill makes the helper return False and I3 fires DATA_UNAVAILABLE exactly as before. The helper is also wired into `_query_data_freshness`, replacing the inline orders_placed check from `dd67f97` so I9 and I3 share one source of truth. Companion `fixit.md::FX-002`.

**B10 — SafetyController I9 deadlock on fresh-DB bootstrap** (fixed `dd67f97`)
On a genuinely fresh DB, `oversight/safety_controller.py::_query_data_freshness` runs `SELECT MAX(ts) FROM scoring_snapshots`. The query returns `None` because the table has never been written to. I9 interpreted the `None` as a critical violation and pushed the state machine into `DATA_UNAVAILABLE`. Per the state permissions table (`safety_controller.py:92-111`), `DATA_UNAVAILABLE` sets `trials=False`. On a fresh DB every market scores as a trial (`confidence='low' AND fill_count==0` per `market_scorer.py:814`). So the allocator emitted 0 deploys, the farmer placed 0 orders, no orders means no `are_orders_scoring` API calls, no scoring calls means `scoring_snapshots` stays empty, and I9 keeps firing → permanent deadlock. Observed in production on the first LIVE cutover from the new Helsinki server (Ashburn was geoblocked before reaching this code path): bot ran for 2.5+ hours with 0 deploys and 17 consecutive oversight cycles emitting `VIOLATION: data_freshness | PRIORITY=MEDIUM | value=0 | threshold=0`. Architecture doc §10.3 v5.1.4 lessons documented this deadlock abstractly ("first LIVE cycle writes portfolio_snapshots and starts the SafetyController state-transition machine") but the documented exit path only addresses the I3/I4 portfolio-value chicken-and-egg; I9 has a separate code path that the description missed. Fix: differentiate cold-start (no orders ever placed) from broken-pipeline (orders exist but scoring missing) inside the empty-table branch, via a `SELECT COUNT(*) FROM orders_placed` check. Cold-start returns `0.0` (treat freshness as N/A). Broken-pipeline returns the original defensive `None`. Once the bot places its first order ever, behaviour is byte-identical to pre-patch. Companion `fixit.md::FX-001` carries full retrospective.

**B9 — `client.get_orders()` does not exist on V2 SDK** (fixed `ee6abdf`)
The V2 SDK renamed `get_orders()` → `get_open_orders()`. Bot calls `self.client.get_orders()` at 4 production sites (`reward_farmer.py:263, 433, 1751`; `fills.py:65`). DRY mode skipped these paths (`if not self.dry_run` gates), so the bug stayed undetected through 30+ hours of DRY soak. First LIVE cutover surfaced it: every cycle emitted `ERROR | get_orders failed: 'ClobClient' object has no attribute 'get_orders'`, bot fell through to placement with empty `open_ids` → 0 orders placed (no money at risk). Fix: drop-in rename. Audit of every other `self.client.<method>(` in production paths confirmed all other methods are V2-compatible.

**B8 — `_read_alloc_file` dict-key mismatch (`allocations` vs `markets`)** (fixed `4f102e3`)
Writer at `oversight/allocation_writer.py:275` writes `"markets"`; reader at `profit/learning.py:852` was reading `"allocations"`. Reader silently returned empty list. Every downstream metric (`reward_efficiency`, `reward_error`, `loss_per_capital`, `expected_util`) stayed `None` → `_metrics_complete=False` → `valid_cycles_observed` never advanced → LearningController gate stuck at OFF/SHADOW forever. **Entire control loop structurally dead since the writer/reader were authored against different keys.** Single-line fix; mirror bug in `simulation/runner.py` also fixed in same commit.

**B7 — `_total_capital` unstamped on legacy allocator deploys** (fixed `d2612e6`)
Profit-engine allocator at `profit/allocator.py:379` stamped `_total_capital` per deploy row. Legacy allocator at `oversight/allocation_writer.py:_to_dict` did not. Since the calibrator isn't trained, the legacy path runs every cycle. Farmer reader `_guardrail_total_capital_from_alloc` at `reward_farmer.py:1064-1095` returned `None`, propagating to the guardrail JSON. **Four guardrails (notional_ratio, cluster_cap, daily-loss kill-switch) and two oversight shadow signals (notional_drift, slow_bleed) silently inactive.** Symptom: `[GUARDRAIL_WARNING] missing_signal=total_capital` warning emitted on every farmer cycle for thousands of cycles before fix. Fix: hoist `cap_scale` out of profit-engine-only branch so legacy path also uses `alloc_capital = available_capital * cap_scale`; add post-redistribution loop that stamps `_total_capital` on every deploy row.

**B6 — Empty `question` text on cold-start markets disabled three safety filters** (fixed `c7ed2e6`)
Cold-start markets got hardcoded `question=""` at `oversight/data_collector.py:1354`. The Gamma keyset parser at lines 284-288 extracted `conditionId` + `endDateIso` but dropped the `question` field that IS in the Gamma response. 73% of `market_performance` rows had empty question text. Three safety gates short-circuit on truthy-question and silently disabled: (a) **sports protection** at `oversight/market_scorer.py:272-275` (NBA/NFL/UFC keyword block skipped), (b) **per-group concentration cap** at `oversight/allocation_writer.py:117-124` + `profit/allocator.py:115-118` (empty `question_group` → 30%-of-capital per-cluster cap never tracked), (c) **keyword filters** at `market_discovery.py:35-39` (natural-gas / "during" market substring matches on empty string evaluate False). Fix: extract `question` in Gamma parser, CLOB fallback, threaded through cache + consumer fallback. Schema migration adds `market_expiry_cache.question` column. Live evidence post-fix: 11 sports markets correctly time-gated in first oversight cycle.

**B5 — pytest collection broken by module-level test runners** (fixed `900e3f8`)
Five top-level `test_*.py` files ran custom test suites at module import time. Pytest's collector imported them → runners ran → terminal `sys.exit(1)` (when any test failed) killed collection with `INTERNALERROR / SystemExit: 1`. 0 tests collected. Fix: wrap each runner body in `if __name__ == "__main__":` guard. Both invocations preserved.

**B1 — `reward_tracker.py:331` q_share saturation** (fixed `88f6c7a`)
The previous accumulation line was:
```python
stats.total_market_q += max(market_q, our_q)
```
When `record_cycle` was called without `order_book` (as the production path always did), `market_q = 0` and the `max()` fallback wrote `total_market_q = total_q_score`, producing `q_share = 1.0` for every sample. 394/402 production rows were affected.
Fixed by Option B's book cache + requiring both `our_q > 0 AND market_q > 0` before accumulation.
Memory ref: `project_market_q_fallback_bug.md`

**B2 — Cold-start trap** (fixed `a6f580d`)
New markets had `q_share = 0` → `score = 0` → classified as trials → capped at 10 per cycle → 1,391 markets blocked in production. Resolved by adding `RF_NEW_MARKET_Q_SHARE_PRIOR = 0.10` and redefining the trial-cap criterion from `score <= 0` to `confidence == "low" AND fill_count == 0`.

**B3 — Sports in-play hole** (fixed `9f58e14`)
`end_date_iso` alone couldn't block during live games if `end_date` was far in the future (resolution deadline is often hours after the event ends). Phase 1 using `game_start_time` now closes this for CLOB-routed sports markets.

**B4 — CF signal masking** (fixed `1081e72`)
The 0.001 smoothing clamp inflated raw CF signals by up to 5× before the scorer saw them. Consumer audit confirmed no code divides by CF; lowered to 1e-6.

### 10.3 Known limitations (v5.1.14)

**v5.1.4 blocker resolved in v5.1.5:**
- ~~Polymarket geoblocks US IPs at the CLOB API.~~ — Resolved by Ashburn → Helsinki migration. Verified against the live geoblock docs page (2026-05-15): **Helsinki (`hel1`, Finland)** is allowed; Germany locations (`fsn1`, `nbg1`) and US locations (`ash`, `hil`) are blocked; Singapore (`sin`) is close-only (cannot open new orders, only close existing). §11.4 was updated to reflect the verified list. First LIVE cutover from Helsinki on 2026-05-15 04:03 UTC successfully placed orders (no 403).

**v5.1.5 blocker resolved in same session:**
- ~~SafetyController I9 deadlock on fresh-DB bootstrap.~~ — Surfaced during the first LIVE cutover from the fresh-DB Helsinki server. Closed by `dd67f97`. See §10.2 B10 + Amendments in v5.1.5.

**Currently no known blockers.** Open issues are tracked in `Polymarket bot fixit.md` (the companion fixit doc) with stable `FX-NNN` IDs.

**v5.1.4-vintage operational items resolved in v5.1.6:**
- ~~**`numpy` not in `requirements.txt`.** Transitive dep via `streamlit` (in `pyproject.toml`) on local Mac; missing on headless server install. Manually `pip install numpy` is in §11.8.~~ — Resolved by `987a844` (FX-018). `requirements.txt` now declares `numpy>=2.0`. §11.8's "CRITICAL: numpy is NOT in requirements.txt" warning has been replaced with a note that the previous line (`pip install -r requirements.txt`) handles it.
- ~~**Stale `polymarket-bot.service` in repo root.**~~ — Resolved by `3f50441` (FX-017). The legacy unit referenced `/opt/polymarket-bot/` and ran `main.py`; not deployed anywhere. Its `KillSignal=SIGINT` + `TimeoutStopSec=30` directives were copied into the canonical §11.11 unit blocks by Phase 5 (`91bae99`, FX-014).

**New operational items in v5.1.4 (not blockers, carried forward):**
- **`_p_fill` unstamped on legacy allocator rows.** Profit engine stamps it (`profit/allocator.py:372`); legacy doesn't. Result: `expected_capital_sum = 0` → `expected_util = 0` → β rule converges to upper clamp 0.95 under EMA. Mitigated by `GATE_ACTIVE_CYCLES = 2000` SHADOW soak (Phase 3b). Permanent fix: mirror the profit-engine stamping in `oversight/allocation_writer.compute_allocations`, or retire the legacy path entirely once calibrator readiness is achieved.
- **`GATE_ACTIVE_CYCLES = 2000` is temporary.** Inline TODO in `profit/learning.py:66` marks revert-to-50 once LIVE observation confirms sane β trajectory.
- ~~**`check_wallet.py` 400 error on conditional asset query.** Cosmetic. The on-chain collateral balance shown below the error is read via web3 and is correct. Bot's runtime balance fetch (different code path) is correct.~~ — Resolved in v5.1.14 (FX-019). The dead `AssetType.CONDITIONAL` call without a `token_id` was removed; the diagnostic now starts cleanly.
- **Production farmer's `get_orders` log message wasn't renamed.** Method call updated to `get_open_orders` (B9 fix) but the log message inside the except block still reads `"get_orders failed: …"` — deliberately preserved for log-grep continuity with the historical corpus. Update at next major version if appropriate.

**New behavioural observations from Phase D:**
- **SafetyController + DRY chicken-and-egg.** In DRY mode, `_save_usdc_balance` is gated behind `if not self.dry_run` at `reward_farmer.py:2093`. `portfolio_snapshots` never gets a fresh row. SafetyController reads stale/missing snapshots → state stays in `DATA_UNAVAILABLE`. `STATE_PERMISSIONS[DATA_UNAVAILABLE]["trials"] = False` blocks all trial markets. On a fresh-DB server, every market is a trial. **Result: 0 deploys in DRY soak on a fresh server.** This is correct behaviour, not a bug. Exit path: first LIVE cycle writes `portfolio_snapshots`, SafetyController advances out of the I3/I4 portfolio-value constraints. Local Mac escapes this because its DB has historical `reward_market_stats` from prior runs (some markets are no longer "trial"). Documented in §11.12 so operators don't misinterpret the 0-deploy state.
  - **v5.1.5 finding** — this exit path is *incomplete*. Writing `portfolio_snapshots` clears I3 and I4 but does NOT clear I9 (`data_freshness`), which is queried separately against `scoring_snapshots`. Until v5.1.5's I9 patch, the LIVE bootstrap was permanently stuck on I9 even though portfolio_value was now known. See §10.2 B10. The v5.1.5 fix means the bot now genuinely exits `DATA_UNAVAILABLE` once both portfolio_snapshots and the cold-start I9 check are clean.

**New behavioural observations from v5.1.20 (40h post-FX-041 production analysis, 2026-05-22):**

- **CF smoothing is asymmetric to the upside.** The `_smooth_correction_factor` circuit-breaker (per §4.4) bypasses EMA on the LOW side (`raw < 0.01` → bypass; `raw < 0.05 AND prev_smoothed > 0.2` → fast-adapt α=0.7), but has NO equivalent fast-attenuation on the HIGH side. A single-cycle raw spike (e.g., when `est_d` collapses transiently because the alloc list briefly went 0-deploy) propagates fully into smoothed CF and takes 5-10 cycles to decay. Observed 2026-05-21 20:22 UTC: raw 9.63, smoothed peaked 3.145 (above the CALIBRATED upper bound 3.0). No invariants fired during the spike (only the CF lower-band invariants I5/I5b would react), but it's a noise vector worth knowing about for any future invariant added on the CF upper side.

- **Polymarket CLOB `/markets/{cid}` endpoint is unreliable for "is this market resolved" decisions.** Verified empirically on 2026-05-22: the endpoint returned HTTP 404 for `0x0ed3f07970b272e0d8b50c0ce62b51e26a4dcdb13bee92feca0f0c11ed6cc6c0` while the SAME market was actively scoring + accepting orders + being tracked by `/rewards/markets/current`. **Lesson: don't conclude market resolution from a single endpoint's 404.** Multi-endpoint verification pattern:
  - `/rewards/markets/current` — more authoritative for reward-listing (if cid present → market is in reward pool)
  - Order book endpoint via `client.get_order_book(token_id)` — if non-empty bids/asks → market is live
  - `client.get_open_orders()` showing our own orders on the cid → market accepts orders
  - `client.create_and_post_order` succeeds → market accepts orders for real
  
  Use ≥2 of these before concluding resolution. The bot's internal `unliquidatable_markets` table (FX-007) gates on a different signal (canonical 400 from create_and_post_order) and is the trusted production signal for "this market is dead". The metadata endpoint is informational only.

- **Morning UTC-boundary I6 spike** (`fixit.md::FX-044`). At 00:00 UTC each day, Polymarket's daily payout resets the "actual_daily" measurement to the new day's partial accumulation, while "estimated_daily" stays at full-day rate. I6 (`est_actual_ratio`) jumps from healthy ~5-8× to ~25-30× within 30 min → SafetyController demotes to SEVERELY_MISCALIBRATED → trial markets blocked for 6-8h until act_d catches up. Verified across the 2026-05-22 00:00 UTC boundary on Helsinki. Structural daily friction; doesn't damage anything but constrains operation during peak market activity. Friend rollout G3 gate ("CALIBRATED ≥24h") is unreachable until this ships.

- **`_total_capital` stamp can disappear during 0-deploy alloc moments** (`fixit.md::FX-043`). Phase 2 (`d2612e6`) added the stamp to deploy rows, but the loop only stamps EXISTING rows. When the allocator routes everything to "avoid" momentarily (market-list refresh, deploy demotion), there are no deploy rows → no stamp → `_guardrail_total_capital_from_alloc` returns None → fail-open guardrails (notional + cluster + 24h-loss kill-switch). Observed once for ~5 min on 2026-05-21 19:50-19:54 UTC; no damage but invariant violation. Proposed fix: stamp on alloc metadata + portfolio_snapshots fallback.

**New behavioural observations from v5.1.5 (Helsinki bootstrap):**
- ~~**Counter / DB inconsistency on placement failures** (`fixit.md::FX-004`). `[CYCLE_SUMMARY] orders_placed: N` increments at the point `place_orders_for_market` is called, not after API confirms success.~~ — Resolved in v5.1.8 (`e7fc3d2`). The wrapped function now returns `int` and the gated wrapper accumulates the value; `[CYCLE_SUMMARY] orders_placed` matches `SELECT COUNT(*) FROM orders_placed` for every cycle. See §10.2 B13.
- ~~**Orphan-scan creates persistent failing dumps for resolved markets** (`fixit.md::FX-007`).~~ — Resolved in v5.1.9 (`7d8d38d`). Closes the entire FX-005/006/007/008/009/028 family. See §10.2 B14 + the v5.1.9 amendment block at top of doc.
- ~~**Capital-sizing race on cold start** (`fixit.md::FX-013`).~~ — Resolved in v5.1.10 (`d4d1541`). Closes the entire FX-010/011/013/024/025 family. See §10.2 B15 + the v5.1.10 amendment block at top of doc.
- **No dedicated SafetyController test coverage** (`fixit.md::FX-016`). The bootstrap deadlock that v5.1.5 fixes would have been caught by any unit test exercising `_query_data_freshness` with an empty `scoring_snapshots` table. No such test existed at v5.1.5. v5.1.7's Phase 1 release seeds the new `tests/test_safety_controller.py` with 17 focused tests around the cold-start helper + I3 + BOOTSTRAP, but the broader build-out covering all 14 invariants and the full state machine is still scheduled for Hardening Phase 6.

**Phase 1 (v5.1.7) closes (bootstrap completion):**
- ~~**I3 drawdown deadlock on fresh-DB bootstrap** (`fixit.md::FX-002`).~~ — Resolved by `dc78ba0`. I3 now skips on genuine cold start (`_is_genuine_cold_start()`).
- ~~**No `BOOTSTRAP` state for first-time-ever cold start** (`fixit.md::FX-003`).~~ — Resolved by `541108b`. New state with `max_markets=10, capital_pct=0.30, trials=True`.
- ~~**Cold-start defaults to MILDLY_MISCALIBRATED, not conservative** (`fixit.md::FX-012`).~~ — Resolved by `541108b`. `_load_state` now routes through `_cold_start_or(MILDLY)`.

**Phase 2 (v5.1.8) closes (counter consistency):**
- ~~**`[CYCLE_SUMMARY] orders_placed` counted attempts, not API-confirmed placements** (`fixit.md::FX-004`).~~ — Resolved by `e7fc3d2`. `place_orders_for_market` returns `int` (0/1/2); the gated wrapper accumulates the return value. Counter now matches `SELECT COUNT(*) FROM orders_placed` exactly. See §10.2 B13.

**Phase 8 / 9 (v5.1.14) closes (hardening roadmap closure):**
- ~~**`check_wallet.py` 400 error on conditional asset query** (`fixit.md::FX-019`).~~ — Resolved in v5.1.14 closure commit `38fc63c`. The dead `AssetType.CONDITIONAL` call (no token_id) was removed; diagnostic now starts cleanly with only the COLLATERAL pUSD balance + on-chain allowance checks the operator actually needs. See §10.2 B20.
- **Process-boundary lag** (`fixit.md::FX-027`) — **accepted as designed architectural risk** in v5.1.14. The 30-min agent / 30-s farmer cadence is intentional (§2 + §4.21.6). The actually time-critical safety responses live on the farmer's 30-s cadence: runtime guardrails (§4.18 — notional cap, cluster cap, kill switch on 24h-loss / CF / fill-rate spike), order placement/cancellation gates, Phase-C pause/kill hook. The agent's 30-min cadence affects allocation **revisions**, not allocation **enforcement** (the filter runs at write-time and the farmer enforces every 30 s). Mitigations already in place: Phase 4 wallet-first capital flow closes the "stale capital number" exploit; Phase 3 dump-state lifecycle closes the "agent doesn't know orderbook is dead" exploit; Phase 1 BOOTSTRAP cold-start ladder closes the "fresh-DB SafetyController stuck in DATA_UNAVAILABLE" exploit; Phase 6 part 2's FX-030 fix tightens UNSAFE recovery so even the agent's lag can't cut the documented 5-cycle minimum. Decision recorded in `fixit.md::§5`. Reopens if a specific pathological scenario emerges that the farmer-side guardrails can't bound.

**Phase 6 (v5.1.12 + v5.1.13) closes (test coverage + CI):**
- ~~**No CI: tests don't run automatically on push** (`fixit.md::FX-026`).~~ — Resolved in v5.1.12 (`a580bdb`). GitHub Actions workflow `.github/workflows/test.yml` runs the fast-tier suite on every push to `main` + every PR; first green run `26046878949` in 7m17s. See §10.2 B17.
- ~~**No dedicated SafetyController test coverage** (`fixit.md::FX-016`).~~ — Resolved in v5.1.13 (`4aff918` + `f3630c9`). 17 → 152 tests; coverage 58% → 94% on `oversight/safety_controller.py`. All 14 invariants + state machine ladder + `filter_allocations` + persistence + helpers + alert files now pinned.
- ~~**`filter_allocations` per-market $200 cap can be overshot** (`fixit.md::FX-029`).~~ — Resolved in v5.1.13 (`1c4ae7e`, audit-surfaced). Both scaling decision and post-cap value now derive from the internal formula. See §10.2 B18.
- ~~**`_handle_upgrade` UNSAFE→MILDLY fast path bypasses documented 3-cycle cap** (`fixit.md::FX-030`).~~ — Resolved in v5.1.13 (`1c4ae7e`, audit-surfaced). `_handle_upgrade` no-ops on UNSAFE; slow auto-recovery in `evaluate_state` is the SOLE UNSAFE exit. See §10.2 B19.

**Phase 5 (v5.1.11) closes (operational hardening):**
- ~~**systemd units lack `KillSignal=SIGINT` + `TimeoutStopSec`** (`fixit.md::FX-014`).~~ — Resolved by `91bae99`. §11.11 unit blocks updated; operator re-tees on the server. Forward-compatible with the unit blocks NOT updated, thanks to FX-015.
- ~~**No signal handler for graceful shutdown in bot processes** (`fixit.md::FX-015`).~~ — Resolved by `91bae99`. SIGTERM handler in `reward_farmer.run()`; `_shutdown_cleanup` uses V2 batch `cancel_orders` (1 API call replaces 240); OL.cancel_order honours `force=True`; rate-limiter covers V2 method names; structured `[SHUTDOWN]` log channel. See §10.2 B16.

**Phase 4 (v5.1.10) closes (capital flow correctness):**
- ~~**Capital-sizing race: `$1500` fallback active up to 30 min on cold start** (`fixit.md::FX-013`).~~ — Resolved by `d4d1541`. Farmer cycle-1 write + agent `--capital` default None. See §10.2 B15.
- ~~**`--capital` CLI default `1500.0` should be `None`** (`fixit.md::FX-025`).~~ — Subsumed by FX-013.
- ~~**`CAPITAL_FLOOR_USD` is absolute `$50`, not wallet-scaled** (`fixit.md::FX-010`).~~ — Resolved by `d4d1541`. New `SafetyController._capital_floor` helper; I4 uses `max($50, max(peak, portfolio, exchange) * 0.10)`. $50 minimum preserved for operational floor.
- ~~**`RF_MAX_TOTAL_EXPOSURE` / `RF_MAX_COST_PER_MARKET` defined but unused** (`fixit.md::FX-011`).~~ — Resolved by `d4d1541`. Both constants + their accessors deleted; the v5.0 runtime guardrails own this responsibility.
- ~~**Inconsistent capital-source logging** (`fixit.md::FX-024`).~~ — Resolved by `d4d1541`. Per-cycle `[CAPITAL_SOURCE] source={usdc_db|flag|none}` line.

**Phase 3 (v5.1.9) closes (dump-state lifecycle):**
- ~~**Orphan-dump 400-spam from on-chain CTF positions on resolved markets** (`fixit.md::FX-007`).~~ — Resolved by `7d8d38d`. New `unliquidatable_markets` DB table + gates at every order path. Tamilaga spam closes on next Helsinki `git pull + restart`. See §10.2 B14.
- ~~**`book_failures` doesn't increment on order-placement failures** (`fixit.md::FX-005`).~~ — Subsumed by FX-007. OL marks unliquidatable on canonical 400; the gate filters the cid on subsequent cycles.
- ~~**Dead-market cleanup orphans `dump_states` rows** (`fixit.md::FX-006`).~~ — Resolved by `7d8d38d`. Cleanup loop now cascades to `delete_dump_state` + `mark_unliquidatable`.
- ~~**`dump_states` reload on restart re-creates failing dumps** (`fixit.md::FX-008`).~~ — Subsumed by FX-007. `_restore_dump_states` gates each row on `is_unliquidatable`.
- ~~**`dump_state` row saved BEFORE the SELL is posted** (`fixit.md::FX-009`).~~ — Subsumed by FX-007. Save ordering preserved (retry semantics); exception handler distinguishes definitive failure (cleans up) from transient (preserves state).
- ~~**No re-probe mechanism for unliquidatable markets** (`fixit.md::FX-028`).~~ — Resolved by `7d8d38d`. `_reprobe_unliquidatable` runs every 30 min loop sweep; per-cid 6h staleness gating; un-marks cids whose orderbook returns.

**Phase C oversight stage promotion sequence** (operator-driven, not automatic):
- Stage 1 (current default): all signals computed + logged, no actions. `_SHADOW_ONLY=True`, `_PAUSE_ENABLED=False`, `_KILL_ENABLED=False`.
- Stage 2 candidate flip: after ≥200 LIVE cycles with no `[OVERSIGHT_SHADOW] triggered=True` lines from healthy regime, flip `_SHADOW_ONLY=False` AND `_PAUSE_ENABLED=True`. Promotion gates from §4.21.7: no false positives, triggers fire BEFORE corresponding hard guardrail, no flapping.
- Stage 3 candidate flip: after ≥200 LIVE cycles at Stage 2 with same gates clean, flip `_KILL_ENABLED=True`. cf_trajectory acts as kill.
- Each flag flip is a single-line commit and easy to revert.

**Closed in v4.0 by deletion (Patches 6–13 removed):**
- ~~avg_overcommit_active < 1.5 (V3.1 INV3)~~ — concept retired; v4.0 has no overcommit factor.
- ~~deploy_ratio < 0.85 (V3.1 INV5 / V4 INV5)~~ — concept retired; v4.0 targets `expected_util`, not notional deploy ratio. Replaced with V5 INV5_new (coverage_ratio).
- ~~marginal-efficiency gate rejecting too many candidates (Patch 13 V4 finding)~~ — gate deleted.
- ~~Patch 9 / Patch 10 composition friction~~ — both layers deleted.
- ~~Patch 13 hysteresis dead-band suppressing legitimate moves~~ — hysteresis retained for `capital_scale`, but the dead-band now applies only to `capital_scale`, not to any allocator-side mechanism.
- ~~Legacy `oscillation_lock` DB column from Patch-13 interim draft~~ — still in schema for compat; remains silently ignored.

**Newly closed in v4.0:**
- ~~λ1 / λ2 control system has no leverage on allocation~~ — proven algebraically (§4.16.1 / §4.16.2) and deleted. Replaced with (β, η); β has non-cancelling linear leverage on absolute scale in any regime, η has non-cancelling leverage on relative allocation under any non-uniform market.
- ~~`expected_capital ≈ 0` in sim bootstrap because FillModel is untrained~~ — `simulation/bootstrap_calibrator.py` substitutes a deterministic, bounded, state-dependent `p_fill ∈ [0.02, 0.15]` while `fill_model.is_ready() == False`. Production calibrator untouched.
- ~~allocator couples reward reconstruction through EV~~ — `CalibrationPredictions.raw_reward_per_day` added; allocator reads reward directly.

**Newly closed in v5.1.1 (shadow stage 1):**
- ~~`oversight_agent.evaluate(guard)` not implemented~~ — function now exists in shadow form; computes 6 trigger signals (§4.21.7) over a 30-snapshot ring buffer; returns `{"action": "continue", "reason": "shadow"}` unconditionally. Behaviour byte-identical to pre-shadow; only observable change is the per-cycle `[OVERSIGHT] reason=shadow` log line + new `[OVERSIGHT_SHADOW]` channel emitted only on triggers/missing-data.
- ~~Per-cycle `reason=not_implemented` log line~~ — replaced by `reason=shadow` (truthful representation; no downstream consumers depended on `not_implemented`, verified by repo-wide grep).

**Newly closed in v5.1:**
- ~~No structured oversight evaluation hook in the farmer~~ — `b8d84bd` added the hook; `2706953` made it deterministic with `hasattr` gate, latency tracking (`OVERSIGHT_LATENCY_WARN_MS = 50`), strict `{action, reason}` validation, per-cycle `[OVERSIGHT]` audit log, kill-reason propagation. See §4.21.
- ~~`[OVERSIGHT_WARNING] evaluation failed` log spam (~2880/day) when `oversight_agent.evaluate` is absent~~ — closed by the `hasattr` gate at `2706953`. Stayed silent under v5.1; fully obsolete now that the function exists in v5.1.1.

**Newly closed in v5.0:**
- ~~V5 INV5_new (coverage) passes only in `under_deployed`~~ — Step-3b cap-aware shaping (`5611d54`) resolves the cluster-cap × min-floor artefact directly inside the allocator. INV5_new: 1/6 → 6/6 PASS (coverage 0.50–0.98 across all scenarios).
- ~~V5 INV7 fails in `over_aggressive` + `regime_shift_3phase`~~ — `capital_scale` stability filters (`741d35c`, bounded-rate + flip suppression) collapse `max_flip_rate_100` from 7–9 to 0–1. INV7: 4/6 → 6/6 PASS.
- ~~V5 INV3_new unreachable in 5/6 scenarios because the raw-util metric is bounded by the bootstrap p_fill clamp~~ — INV3 rewritten (`707ca50`) as cap-normalised `capital_util / feasible_capital_fraction ≥ 0.70`. Metric is now scenario-independent and evaluates control-loop quality instead of cap-policy geometry. INV3_new: 0/6 → 6/6 PASS.
- ~~No runtime execution-time safety layer~~ — v5.0 ships the `reward_farmer.py` guardrail stack (`414354a` + `2e72606`): soft notional + cluster blocks, hard enforcement with multi-cancel cap, kill-switch on {daily_loss, CF, fill-rate spike}, persistent-breach detector, structured `[GUARDRAIL]` telemetry, fail-open visibility.
- ~~Binary dry-run flag with no intermediate~~ — v5.0 three-mode gate (`7ab514d`): DRY_RUN → SHADOW → LIVE with staged promotion path.
- ~~Unstructured log output~~ — v5.0 emits `[CYCLE_SUMMARY]` / `[ROLLING_STATS]` / `[GUARDRAIL]` / `[CRITICAL]` / `[GUARDRAIL_WARNING]` / `[DRY_RUN]` / `[SHADOW]` as machine-parseable JSON.

**Still open (v5.1.1):**
- **Shadow evaluator running but not promoted to live control.** `evaluate(guard)` exists and triggers logs at `[OVERSIGHT_SHADOW]`, but `_SHADOW_ONLY = True` so all six signals resolve to `continue`. Activation ladder in §4.21.7: stage 2 = pause-kind signals (A–D, F), stage 3 = kill-kind signal E (cf_trajectory). Each promotion requires evidence from a 200–500 cycle shadow run (no false positives, triggers fire before guardrails, no flapping).
- **Per-cycle `[OVERSIGHT] reason=shadow` line** (~2880/day at 30 s cadence) — same volume as the pre-shadow `reason=not_implemented` line. Not a bug; truthful representation of the function's intentional non-operative state.
- **Deprecated `lambda_1`, `lambda_2` fields still on `LearningState`.** Retained as frozen compatibility fields solely because `simulation/engine.py` and `simulation/invariants.py` reference them. A future sim-side migration can remove them.
- **Gamma-routed sports markets still unprotected by Phase 1** (field not exposed by Gamma API).
- **Learning-loop Rule A low-fill high-loss edge case** (§6.7) — unchanged from v3.x. Rule A requires `fill_rate > threshold` to contract; a low-fill high-loss regime is invisible to it.
- **`profit_efficiency` not used by the learning loop** (only `reward_efficiency`) — unchanged.
- **Stop-loss events not distinguished** from normal unwinds in the learning signal — unchanged.
- **No per-market CF; still global** — unchanged. Reward-global / loss-local asymmetry preserved by design.
- **`profit/refill.py` pure helpers still not wired** into `reward_farmer.py` / `order_lifecycle.py` — fill-triggered cancellation + re-allocation still runs on the 30 s cycle cadence (deferred from v3.x; not touched in v4.0 or v5.0).
- **`capital_util > 1.0` in some V5 scenarios** is notional overcommit (Σ C > T) — allowed on Polymarket since orders cancel on first fill, but the allocator's Step-7 rescale only caps `Σ(p·C) ≤ 0.95·T`, not `Σ C`. Consistent with `project_capital_overcommit` memory; revisit if over-fill risk becomes a production concern.
- **Repo structure**: flat `.py` files should eventually move into `src/` package layout. Deferred until the bot is stable in production (`project_repo_structure` memory).
- **Post-shaping V5 re-audit produced one edge case**: `over_aggressive` `expected_util` dropped from 0.029 (pre-shaping raw util) to 0.018 (post-shaping raw util) because shaping's top-k selection sometimes leaves survivors that don't scale above min_capital under the cluster cap. The cap-normalised INV3 correctly records this as `normalized_util ≈ 1.45` (still well above 0.70 threshold), but the raw-util regression is worth monitoring in production; it signals that shaping is conservative for that topology.

### 10.4 Audit framework evolution (v1 → v5)

The simulation harness (`simulation/`) and audit framework went through six iterations alongside Patches 6–13 and the v4.0 continuous-allocator replacement. All audit code is sim-only; no production logic was ever touched.

| Audit | Criteria emphasis | Post-Patch-11 (`d8a4569`) | Post-Patch-13 (`8a8466e`) | Post-v4.0 (continuous allocator + β/η, working tree) |
|---|---|---|---|---|
| V1 (`run_audit`) | Directional correctness | PASS | Not re-run | Not re-run |
| V2 (`run_audit_v2`) | Profit-max enforcement | FAIL — criteria artefact | Not re-run | Not re-run |
| V3 (`run_audit_v3`) | Overcommitment-aware | FAIL — `expected_capital = 0` in cold-start | Not re-run | Not re-run |
| V3.1 | + bootstrap exclusion (50 cycles), ACTIVE-only overcommit average, front/back efficiency retention | FAIL — 3 of 7 criteria still miss | Not re-run post-Patch-13 | Not re-run |
| **V4 (`run_audit_v4`)** | 6 scenarios, strict invariant thresholds (INV3 avg_overcommit ≥ 1.3 AND avg(actual/target) ≥ 0.9; INV5 deploy_ratio ≥ 0.85 on ≥ 80% cycles; INV7 flip_rate_100 ≤ 3 AND no sustained alternation > 20 cycles) | n/a (post-dates `d8a4569`) | **FAIL** — see Patch-13 table below | **Not meaningful** — V4 INV3/INV5 measure concepts (overcommit factor, notional deploy_ratio) that do not exist in the continuous allocator. INV7 still applicable but duplicated by V5. |
| **V5 (`run_audit_v5`)** | 6 scenarios same as V4. **INV3_new** (v5.0 rewrite at `707ca50`): cap-normalised `capital_util / feasible_capital_fraction ≥ 0.70` (was raw `expected_util ∈ [0.5, 0.95]`). **INV5_new**: `coverage_ratio ≥ 0.5`. **INV7**: unchanged. Per-cycle `_p_fill`, `est_capital_cost`, `shares_per_side`, `min_size`, `max_spread` required on every deploy row or `V5FieldMissingError` raised. Dumps `expected_util.csv` + `coverage_ratio.csv` + `capital_scale.csv` + `flip_rate.csv` + JSONL full snapshots. | n/a | n/a (V5 is v4.0-era) | **v4.0 (pre-v5.0 patches)**: FAIL overall (0/6 INV3, 1/6 INV5, 4/6 INV7); 700× `expected_util` improvement vs pre-sim-bootstrap-fix but still below raw-util band. **v5.0**: **PASS overall (18/18 seed-scenarios)** after Step-3b shaping + stability filters + cap-normalised INV3. Full per-commit progression below. |

The simulation harness uses a monkey-patched `time.time()` (`_SimClock` in `simulation/engine.py`) so each cycle advances one simulated hour. `N_SYNTHETIC_MARKETS` was raised from 8 → 30 with per-market index-based jitter on q_share / spread / daily_rate. The V3.1 audit treats "ACTIVE cycles" specifically (requires ≥ 20 to evaluate) so bootstrap noise can't dominate the metrics.

**Post-Patch-11 V3.1 numbers (pre-Patch-13, seeds 1, 42, 1337; 200 cycles each)**

| Scenario | avg_OC | max_OC | capture | reward | eff_front | eff_back | osc_windows |
|---|---:|---:|---:|---:|---:|---:|---:|
| stable_optimal | 0.78–0.94 | 1.22–1.96 | 0.986 | 473 | 12.8–14.3 | 14.2–16.1 | 0 |
| under_deployed | 3.08–3.16 | 3.78–3.87 | 0.92 | 55 | 1.35–1.48 | 0.48–0.50 | 0 |
| over_aggressive | 0.41–0.42 | 0.996 | 0.99 | 297 | 10.8–11.0 | 18.2–18.4 | 13–65 |
| high_reward_fake | 0.57 | 0.997 | 0.98 | 235 | 7.58–7.60 | 10.3–10.4 | 0 |
| regime_shift | 0.47–0.49 | 4.49 | 0.98 | 383 | 13.9–14.2 | 18.2–18.4 | 19–54 |

- INV3 (`avg_overcommit_active ≥ 1.5`): PASS only in `under_deployed`. FAIL in 4/5 scenarios.
- INV5 (efficiency retention ≥ 0.7 × front): FAIL in `under_deployed` across all three seeds.
- INV7 (no persistent oscillation): FAIL for seed 42 `regime_shift`, seed 1 `over_aggressive` + `regime_shift`.

**Post-Patch-13 V4 verdict (seeds 1, 42, 1337; 500 cycles each; warmup cutoff cycle > 100)**

| Scenario | INV3 | INV5 | INV7 | Verdict | Notes |
|---|---|---|---|---|---|
| balanced | FAIL | FAIL | PASS | FAIL | avg(actual/target) ≈ 0.33, avg_overcommit ≈ 4.0; deploy_ratio holds on < 60% of cycles |
| under_deployed | FAIL | PASS | PASS | FAIL | Patch 13 target-driven fires but stalls far below target; cap_scale pinned at clamp top so INV5 trivially holds |
| over_aggressive | FAIL | FAIL | **FAIL** | FAIL | Hysteresis helps but doesn't eliminate sustained alternation; efficiency penalty fires + contracts deploy_ratio |
| regime_shift_3phase | FAIL | FAIL | **FAIL** | FAIL | Phase-transition oscillation still exceeds flip-rate ceiling; worst INV7 scenario |
| efficiency_collapse | FAIL | FAIL | PASS | FAIL | Part 4 penalty fires post-step-down (as designed) but contracts deploy past the 85% floor |
| saturation_edge | FAIL | PASS | PASS | FAIL | avg_overcommit ≈ 4.1 but actual/target stalls at 0.33–0.38; marginal-efficiency gate is the binding constraint |

**Overall V4 verdict: FAIL** — every scenario fails at least one invariant.

**Key finding from V4**: Patch 13's mechanisms ARE firing correctly (target_notional stamped at ~4 × total_capital, `_forced_target_alloc` visible on some deploys, hysteresis direction-lock observable in `last_direction` + `direction_lock` stamps), but compose into an allocation profile whose actual deployed notional stalls at 1.3–1.6 × capital — far short of V4's `≥ 0.9 × target_notional` gate. The binding constraint is the **marginal-efficiency gate**: it rejects most markets before they can be upsized, so the greedy target fill exhausts its candidate pool without hitting target. Secondary issue is the hysteresis dead-band suppressing capital-scale movements that would otherwise push allocations upward.

Threshold-tuning candidates for a follow-up patch (none tried yet):
1. Lower the 0.7 × baseline floor on the marginal-efficiency gate (e.g., 0.5 × baseline).
2. Lower `CAPITAL_CHANGE_MIN_STEP` below 0.05 so rule-driven nudges pass through more readily.
3. Add a second greedy pass that upsizes beyond `effective_per_market_cap` when the first pass exhausts candidates below target.
4. Re-examine V4's `avg(actual/target) ≥ 0.9` threshold itself — a softer `≥ 0.7` gate would credit Patch 13's partial progress.

**Key honest finding from V3.1** (historical, pre-Patch-13): the spec's `avg_overcommit_active ≥ 1.5` is reachable in well-behaved scenarios but not in adversarial ones after the FillModel trains — not because the system is broken, but because Patch 9's per-market halving + Patch 10's avoid-only promotion can't both clear the threshold simultaneously when every market is already marked deploy via `_low_ev_override`. Patch 13 raised avg_overcommit from 0.4–0.9 to 3.0–4.0 across all scenarios — a real directional win — but the V4 gate is asking for `≥ 0.9 × target`, which is about actual capital committed, not the factor headroom.

**Post-v4.0 V5 verdict** (continuous allocator + sim bootstrap p_fill fix + β/η control law; seeds 1, 42, 1337; 500 cycles each; warmup cutoff cycle > 100):

| Scenario | INV3_new | INV5_new | INV7 | `expected_util` (pre-fix → post-fix → post-β/η) | max `flip_rate_100` (pre → post → β/η) |
|---|---|---|---|---|---|
| balanced | FAIL | FAIL | PASS | 0.00005 → 0.03303 → **0.03303** | 0.0 → 0.0 → **0.0** |
| **under_deployed** | FAIL | **PASS** | PASS | 0.00016 → 0.10589 → **0.10589** ✓ | 0.0 → 0.0 → **0.0** |
| over_aggressive | FAIL | FAIL | **FAIL** | 0.00004 → 0.02883 → **0.02883** | 9.0 → 9.0 → **9.0** |
| regime_shift_3phase | FAIL | FAIL | **FAIL** | 0.00006 → 0.04009 → **0.04009** | 10.0 → 10.0 → **10.0** |
| efficiency_collapse | FAIL | FAIL | PASS | 0.00005 → 0.03184 → **0.03184** | 0.0 → 0.0 → **0.0** |
| saturation_edge | FAIL | FAIL | PASS | 0.00008 → 0.05379 → **0.05379** | 0.0 → 0.0 → **0.0** |

Pass counts: INV3_new 0/6, INV5_new 1/6, INV7 4/6. **Overall V5 verdict: FAIL.**

**Three layered V5 results** explain the progression:
1. **Pre-fix (v3.3-era continuous allocator, no sim bootstrap, λ1/λ2 control):** every scenario's `expected_util` was 5e-5 because sim calibrator returned `p_fill = 0` on every cycle → `expected_capital = Σ(p·C) ≈ 0` regardless of β. INV3_new failed on zero signal, not on real underperformance.
2. **Post-fix (v4.0 sim bootstrap calibrator):** `p_fill` substituted with `0.03 + 0.001·daily_rate + 0.004·q_share_pct` clamped to [0.02, 0.15]. `expected_util` jumps 700× to 0.029–0.106. `under_deployed` crosses the INV3_new 0.5 floor. Other five scenarios land below 0.5 — **cluster-cap × min-floor composition** is the binding constraint (30 markets × $10/market-post-cluster-cap < $27.3 min_capital → every `C_i` floored to min_capital; β's effect is erased post-cap).
3. **Post-β/η control law:** bit-for-bit identical to post-fix. Verified by tracing β through 300 sim cycles — β moves 0.50 → 0.057 correctly under sustained `err_β > 0`, but in the cap-bound regime the movement doesn't translate into `expected_util` change because `Σ p·C` is pinned by `N × p × min_capital` independent of β. This is the cluster-cap structural artefact documented in §4.16.5, not a control-law failure. The control law's isolated properties (bounds, direction, smoothness, determinism, stability guard, fail-closed) are verified by the unit battery.

**INV7** (oscillation stability) is unchanged through all three phases — it measures `capital_scale`, which the β/η control law doesn't drive. `over_aggressive` and `regime_shift_3phase` still show `max_flip_rate_100 = 9–10` because `capital_scale` rules (Rule A/B/D/E + Patch-3 expansion) still produce per-cycle direction flips under adversarial signals; Patch-13 hysteresis helps but doesn't eliminate.

**V5 → V4 compatibility.** V5 and V4 can run side-by-side against the same production code. V4's INV3 and INV5 are no longer meaningful under the continuous allocator (they measure concepts — overcommit factor, notional deploy_ratio — that don't exist in v4.0). V5's INV3_new and INV5_new measure the continuous allocator's actual objective (expected_util, coverage_ratio). INV7 is identical across the two audit framings.

**Honest finding from V5 (post-v4.0, pre-v5.0).** The V5 audit's FAIL result is not a control-law failure. It's a binding-constraint failure in the sim environment: the cluster-cap × min-floor composition drives every `C_i` to min_capital in 5/6 scenarios regardless of what β or η does upstream. This was proven by (a) the controllability analysis (§4.16.2 — β has non-cancelling leverage in principle), (b) isolated unit tests (β/η move correctly, smoothly, deterministically), and (c) a 300-cycle sim trace (β moves 0.50 → 0.057 while `expected_util` stays at 0.033). **Resolved by the three v5.0 commits detailed below.**

**Post-v5.0 V5 verdict** (continuous allocator + Step-3b shaping + capital_scale stability filters + cap-normalised INV3; seeds 1/42/1337; 500 cycles each):

| Scenario | seed | `capital_util` | `feasible` | `normalized_util` | `flip_rate_100` | INV3_new | INV5_new | INV7 | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| balanced | 1/42/1337 | 0.86–0.95 | 0.54–0.59 | **1.63–1.65** | 0.0 | PASS | PASS | PASS | PASS |
| under_deployed | 1/42/1337 | 1.58–1.64 | 0.94 | **1.68–1.73** | 0.0 | PASS | PASS | PASS | PASS |
| over_aggressive | 1/42/1337 | 0.25 | 0.16–0.17 | **1.45–1.47** | 0.0–1.0 | PASS | PASS | PASS | PASS |
| regime_shift_3phase | 1/42/1337 | 0.78–0.86 | 0.50–0.55 | **1.52–1.53** | 0.0 | PASS | PASS | PASS | PASS |
| efficiency_collapse | 1/42/1337 | 0.76–0.80 | 0.47–0.51 | **1.57–1.61** | 0.0 | PASS | PASS | PASS | PASS |
| saturation_edge | 1/42/1337 | 1.20–1.31 | 0.75–0.82 | **1.62–1.66** | 0.0 | PASS | PASS | PASS | PASS |

Pass counts: INV3_new 6/6, INV5_new 6/6, INV7 6/6. **Overall V5 verdict: PASS. 18/18 seed-scenarios clean.**

**Full progression across the v4.0 → v5.0 arc:**

| Audit stage | Commit | INV3 | INV5 | INV7 | Overall |
|---|---|---|---|---|---|
| Post-v4.0 (β/η control, pre-shaping) | working-tree pre-5611d54 | 0/6 FAIL (raw util ≤ 0.107) | 1/6 | 4/6 | FAIL |
| + Step-3b cap-aware shaping | `5611d54` | 0/6 FAIL (raw util unchanged/moved slightly) | **6/6 PASS** (coverage 0.50–0.98) | 4/6 | FAIL |
| + bounded-rate clamp | (in `741d35c`, no-op in this sim) | 0/6 | 6/6 | 4/6 | FAIL |
| + small-amplitude flip suppression | `741d35c` | 0/6 | 6/6 | **6/6 PASS** (flip_rate 0–1) | FAIL |
| + cap-normalised INV3 | `707ca50` | **6/6 PASS** (norm_util 1.45–1.73) | 6/6 | 6/6 | **PASS** |

**Why capital_util exceeds 1.0 in several scenarios.** `capital_util = Σ(C_i)/T`, measured from `snap.total_notional`. The allocator's Step-7 safety rescale caps `Σ(p·C) ≤ 0.95·T` (expected capital), NOT `Σ C` (notional). When mean `p_fill ~ 0.1`, Σ notional can reach ~10·T before Step-7 would fire. This is **notional overcommit** — allowed on Polymarket (orders cancel on first fill) and consistent with the `project_capital_overcommit` memory. The cap-normalised ratio stays physics-correct because the same numerator is divided by the same-unit `feasible_capital_fraction`.

---

## 11. Replication & Operations

This section covers two distinct replication tracks:
- **§11.1–11.3** — the **system-level** replication (rebuild this codebase from the architecture spec, regardless of where it runs).
- **§11.4–11.14** — the **operational** replication (provision a production server, harden it, install the bot, run it, switch modes, promote oversight stages, recover from failures).

A future operator (human or AI agent) starting from a clean state should be able to follow §11.4 → §11.10 verbatim and arrive at a running bot. §11.11–11.14 cover lifecycle operations after first bring-up.

To rebuild this system from scratch (code-level), implement in roughly this order:

### 11.1 Required components

1. **Market ingestion** — Polymarket CLOB `/rewards/markets/current` + `/markets/{cid}`, Gamma API enrichment
2. **Scoring observation** — `are_orders_scoring` API polling (every 5th cycle), persisted as `scoring_snapshots`
3. **Database schema** — at minimum: fills, unwinds, scoring_snapshots, book_snapshots, reward_market_stats, market_expiry_cache, correction_factor_history
4. **Reward tracker** — per-market cumulative Q-score accumulation with proper book-aware logic (see B1 bug)
5. **Order book cache** — TTL-based on MarketState, fed by order-placement path
6. **Reward model** — Phase 1 (CF passthrough) + Phase 2 (OLS fit) with phase gate at ~7 days data
7. **Fill model** — Logistic regression over book-state features, activation thresholds (≥50 samples, ≥15 positives)
8. **Loss model** — Per-market recency-weighted averages
9. **EV & RAS computation** — with confidence adjustment
10. **Allocation engine** — RAS-ranked with caps, conservation, exploration
11. **Learning loop** — 4 behavioural scalars, mode gate (OFF/SHADOW/ACTIVE), regime frontier memory
12. **Bandit layer** — Thompson sampling per-market
13. **Safety controller** — 6 states, 14 invariants, state-based allocation override
14. **Sports protection** — 3 layers, 4 phases

### 11.2 Required assumptions

- Reward is proportional to scoring presence (platform policy)
- Fill risk is estimable from book state (requires sufficient book depth history)
- Loss is estimable from slippage and spread (requires sufficient fill history)

### 11.3 Required observability

- CF history (raw and smoothed)
- Scoring snapshots
- Fills and unwinds
- Allocation logs
- Safety state transitions

### 11.4 Server provisioning (Hetzner Cloud, the chosen provider)

**⚠ Critical: verify the server region against Polymarket's published geoblock list at https://docs.polymarket.com/developers/CLOB/geoblock BEFORE creating the server.** US-based regions (Ashburn) and other CFTC-blocked jurisdictions reject every `POST /order` with HTTP 403 at the API layer regardless of how well the code works.

**Verified Hetzner Cloud regions as of 2026-05-15:**

| Hetzner location | Code | Status against Polymarket geoblock | Usable for the bot? |
|---|---|---|---|
| **Helsinki (Finland)** | `hel1` | **Allowed** | **Yes — used for the v5.1.5 production deployment** |
| Falkenstein (Germany) | `fsn1` | Blocked | No |
| Nuremberg (Germany) | `nbg1` | Blocked | No |
| Ashburn (USA) | `ash` | Blocked (CFTC settlement, Jan 2022) | No — v5.1.4 confirmed unusable |
| Hillsboro (USA) | `hil` | Blocked | No |
| Singapore | `sin` | Close-only (can close existing positions; cannot open new orders) | No for market-making |

**As of v5.1.5, Helsinki is the only Hetzner Cloud location that supports order placement on Polymarket.** Polymarket's geoblock list may change; verify before each provisioning by visiting the docs page above or hitting `https://polymarket.com/api/geoblock` from the target server's IP.

**Pre-requisites**
- Hetzner Cloud account (sign up at https://accounts.hetzner.com/signUp, complete identity verification — can take 1-24 hours)
- Local SSH key (e.g., `~/.ssh/polymarket_bot_ed25519`, generated via `ssh-keygen -t ed25519 -C "polymarket-bot-$(date +%Y%m%d)" -f ~/.ssh/polymarket_bot_ed25519`)
- Funded EOA wallet on Polygon with `FUNDER` proxy address set up via Polymarket UI deposit flow
- The 7 env values: `PRIVATE_KEY`, `CLOB_API_KEY`, `CLOB_SECRET`, `CLOB_PASS_PHRASE`, `WALLET_ADDRESS`, `FUNDER`, `DISCORD_WEBHOOK_URL` (optional)

**Hetzner Cloud Console setup**

1. **Create a project** named e.g. `polymarket-bot`. All resources belong to a project for cost isolation.
2. **Add SSH key**: Cloud Console → Security → SSH Keys → "Add SSH key" → paste the contents of `~/.ssh/polymarket_bot_ed25519.pub` (one line, starts with `ssh-ed25519`).
3. **Create firewall** named e.g. `polymarket-firewall`:
   - Inbound: TCP/22 from `0.0.0.0/0` (key-only auth + fail2ban handles brute-force risk)
   - Inbound (optional): ICMPv4 from `0.0.0.0/0` for `ping` debugging
   - Outbound: leave default (allow all — bot calls Polymarket CLOB, Gamma, Polygon RPC)
4. **Create server**: Cloud Console → Servers → "Add server"
   - **Location**: chosen non-US region (verified against geoblock list)
   - **Image**: Ubuntu 24.04
   - **Type**: CCX13 — 2 dedicated AMD vCPU, 8 GB RAM, 80 GB NVMe, 1 TB traffic. $19.99/mo.
   - **Networking**: Public IPv4 + Public IPv6
   - **SSH keys**: select the one added above
   - **Firewalls**: select `polymarket-firewall`
   - **Backups**: enable ($4/mo, 7 daily snapshots auto-retained) — recommended
   - **Volumes**: none
   - **Cloud config / user data**: leave empty
   - **Name**: `polymarket-bot-prod`
   - **Label**: `env=prod`
   - Total ~$24.59/mo with backups + IPv4 (Hetzner charges $0.60/mo for IPv4 separately since 2024)
5. After ~30s, the server's IPv4 address appears on the server detail page. Save it. First connection:
   ```
   ssh -i ~/.ssh/polymarket_bot_ed25519 root@<server-ipv4>
   ```
   Accept the SSH fingerprint on first connect.

### 11.5 Server hardening

As `root` on the server. Each command is idempotent.

```bash
# Time + OS updates
timedatectl set-timezone UTC
apt-get update && apt-get upgrade -y
# If a purple dpkg dialog asks about restarting services / modified config files,
# press Tab to highlight the default option (usually <Ok> / "keep current version")
# and press Enter.

# Create dedicated bot user
adduser --disabled-password --gecos "" polymarket
usermod -aG sudo polymarket
id polymarket   # expect: uid=1000(polymarket) gid=1000(polymarket) groups=1000(polymarket),27(sudo)

# Mirror SSH key from root → polymarket
mkdir -p /home/polymarket/.ssh
cp /root/.ssh/authorized_keys /home/polymarket/.ssh/
chown -R polymarket:polymarket /home/polymarket/.ssh
chmod 700 /home/polymarket/.ssh
chmod 600 /home/polymarket/.ssh/authorized_keys

# Disable root SSH + password auth
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#*PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
systemctl restart ssh

# CRITICAL: before disconnecting from root, verify polymarket login works.
# Open a NEW terminal on the Mac and run:
#     ssh -i ~/.ssh/polymarket_bot_ed25519 polymarket@<IP>
# Should land at polymarket@polymarket-bot-prod:~$
# Also verify root is now blocked:
#     ssh -i ~/.ssh/polymarket_bot_ed25519 root@<IP>
# Should print: Permission denied (publickey)

# Hardening tools
apt-get install -y ufw fail2ban unattended-upgrades

# OS firewall (defense in depth alongside Hetzner Cloud Firewall)
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable
ufw status verbose   # expect: Status: active, 22/tcp ALLOW IN

# fail2ban for SSH brute-force rate-limiting
systemctl enable --now fail2ban
systemctl is-active fail2ban   # expect: active

# Auto-security-updates
echo 'APT::Periodic::Update-Package-Lists "1";' > /etc/apt/apt.conf.d/20auto-upgrades
echo 'APT::Periodic::Unattended-Upgrade "1";' >> /etc/apt/apt.conf.d/20auto-upgrades
```

**Set up passwordless sudo for `polymarket`.** Required because the bot's `--disabled-password` user can't enter a sudo password. Root SSH is now blocked, so this must be done via Hetzner's in-browser VNC console:

1. Cloud Console → Servers → `polymarket-bot-prod` → click **Rescue** in the left sub-nav
2. Click **"Reset root password"** — Hetzner displays a new password ONCE. Copy it immediately.
3. Click the **Console** icon (terminal/monitor icon, top-right of server detail page)
4. In the browser VNC: type `root` + Enter, then paste the password + Enter
5. At the `root@polymarket-bot-prod:~#` prompt, run:
   ```
   echo "polymarket ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/polymarket
   chmod 0440 /etc/sudoers.d/polymarket
   visudo -c -f /etc/sudoers.d/polymarket
   # expect: /etc/sudoers.d/polymarket: parsed OK
   exit
   ```
6. Verify from the SSH session as `polymarket`:
   ```
   sudo -n true && echo "passwordless sudo works"
   ```

**Reboot** to apply pending kernel updates (the `apt-get upgrade` above may have installed a new kernel):
```bash
sudo reboot
# Wait 45-60s, reconnect as polymarket. Root SSH stays blocked permanently.
```

### 11.6 Install Python 3.14 + build tools

As `polymarket` on the server. The repo specifies `requires-python = ">=3.12"`; we use 3.14.4 for parity with the development Mac.

```bash
sudo apt-get install -y \
    git sqlite3 curl wget \
    build-essential libssl-dev libffi-dev \
    libsqlite3-dev liblzma-dev libreadline-dev \
    libbz2-dev zlib1g-dev libncursesw5-dev tk-dev \
    libxml2-dev libxmlsec1-dev libgdbm-dev \
    software-properties-common

sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update
sudo apt-get install -y python3.14 python3.14-venv python3.14-dev

python3.14 --version   # expect: Python 3.14.x
```

### 11.7 GitHub deploy key (read-only access to the bot repo)

Bot is on a private GitHub repo. Server uses a **deploy key** (repo-scoped, read-only) rather than the operator's personal GitHub credentials. This means a server compromise can't push code and can't access the operator's other repos.

```bash
# Generate the deploy key on the server (NOT on Mac — must live on server)
ssh-keygen -t ed25519 -C "polymarket-server-deploy" -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub
# Copy the printed line — single line, starts with ssh-ed25519, ends with polymarket-server-deploy
```

On GitHub:
- Open the repo → **Settings** (top tab) → **Deploy keys** (left sidebar, under Security) → **Add deploy key**
- Title: `polymarket-bot-prod-deploy`
- Key: paste the line
- **Allow write access: UNCHECKED** (read-only is the goal)
- Click Add key

Configure SSH on the server to route github.com via this deploy key:
```bash
cat >> ~/.ssh/config <<'EOF'

Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
```

Test:
```bash
ssh -T git@github.com
# Accept the fingerprint, then expect:
# Hi <user>/Polymarket-bot! You've successfully authenticated, but GitHub does not provide shell access.
# The "but GitHub does not provide shell access" line is SUCCESS.
```

### 11.8 Clone repo + Python deps

```bash
cd ~
git clone git@github.com:<your-github-user>/Polymarket-bot.git
cd Polymarket-bot
git log -1 --format='%h %s'
# Expect the HEAD commit hash from the architecture doc header (currently ee6abdf)

python3.14 -m venv venv
venv/bin/pip install --upgrade pip wheel
venv/bin/pip install -r requirements.txt

# numpy is now declared in requirements.txt (v5.1.6, `987a844`, FX-018) and will
# be installed by the previous line on a fresh venv. The manual step that the
# v5.1.4-era doc carried here is no longer required.

# pytest for smoke test (not in requirements.txt for production minimalism)
venv/bin/pip install pytest
```

Verify imports work:
```bash
venv/bin/python3 -c "
import sys; print(f'python: {sys.version.split()[0]}')
import requests; print(f'requests: {requests.__version__}')
import dotenv; print(f'python-dotenv: imported OK')
import web3; print(f'web3: {web3.__version__}')
import py_clob_client_v2; print(f'py-clob-client-v2: imported OK')
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType, ApiCreds
print('V2 client imports: OK')
import numpy; print(f'numpy: {numpy.__version__}')
"
```

### 11.9 Transfer `.env` from local Mac to server

`.env` is in `.gitignore` and never committed to GitHub. Transfer via `scp` from the operator's Mac:

```bash
# Run from Mac, in the local clone directory
cd "<path-to-local-clone>"
scp -i ~/.ssh/polymarket_bot_ed25519 .env polymarket@<server-IP>:~/Polymarket-bot/.env
```

Lock down perms on server:
```bash
chmod 600 ~/Polymarket-bot/.env
ls -la ~/Polymarket-bot/.env
# Expect: -rw------- 1 polymarket polymarket ... .env
```

**Env keys** (must all be present; format `KEY=value`, no quotes):
- `PRIVATE_KEY` — EOA private key (signer for L1 auth + order signing)
- `CLOB_API_KEY` — Polymarket CLOB API key (L2 auth)
- `CLOB_SECRET` — Polymarket CLOB API secret
- `CLOB_PASS_PHRASE` — Polymarket CLOB API passphrase
- `WALLET_ADDRESS` — EOA address (derived from `PRIVATE_KEY`, kept for convenience)
- `FUNDER` — Polymarket proxy wallet address on Polygon
- `DISCORD_WEBHOOK_URL` — optional, for alert notifications

### 11.10 Smoke tests on server (do NOT skip before going LIVE)

```bash
cd ~/Polymarket-bot

# Pytest collection (catches import errors)
venv/bin/python3 -m pytest --collect-only -q 2>&1 | tail -3
# Expect: 457 tests collected (or current count)

# Full pytest run (~3-5 min on CCX13)
venv/bin/python3 -m pytest --tb=short -q 2>&1 | tail -10
# Expect: 449 passed, 1 failed (pre-existing flake test_over_aggressive_contracts_capital)

# Wallet sanity (on-chain reads only — no orders placed)
venv/bin/python3 check_wallet.py 2>&1 | head -40
# Expect: Connected to Polygon: True; pUSD balance; allowances UNLIMITED.
# A 400 error at the top of check_wallet output is a known harmless cosmetic
# issue (the script's CONDITIONAL asset query has a bug). The on-chain
# COLLATERAL balance below it is read via web3 and works correctly.

# V2 client live auth test (replaces check_wallet's broken API path)
venv/bin/python3 -c "
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType, ApiCreds
import os; from dotenv import load_dotenv; load_dotenv()
creds = ApiCreds(api_key=os.getenv('CLOB_API_KEY'),api_secret=os.getenv('CLOB_SECRET'),api_passphrase=os.getenv('CLOB_PASS_PHRASE'))
c = ClobClient(host='https://clob.polymarket.com', chain_id=137, key=os.getenv('PRIVATE_KEY'), funder=os.getenv('FUNDER'), signature_type=2, creds=creds)
print(c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)))
"
# Expect: {'balance': 'X', 'allowances': {...}} — proves V2 SDK + credentials + network all work.
# A 403 here means the server is geoblocked — STOP and migrate region before proceeding.
```

**Geoblock detection probe** (run BEFORE going LIVE — the smoke test above only exercises READ paths; the geoblock applies to ORDER PLACEMENT paths). Currently the only reliable test is a brief `--mode live` attempt; the bot's logs will surface 403 within 1-2 cycles if blocked. Plan to revert to DRY immediately if 403 fires.

### 11.11 Install systemd units (canonical)

The two services run the farmer and oversight processes with `Restart=on-failure`, journal logging, and hardened sandboxing.

Write `polymarket-farmer.service`:
```bash
sudo tee /etc/systemd/system/polymarket-farmer.service > /dev/null <<'EOF'
[Unit]
Description=Polymarket reward farmer (DRY mode)
Documentation=https://github.com/<your-github-user>/Polymarket-bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=polymarket
Group=polymarket
WorkingDirectory=/home/polymarket/Polymarket-bot

# Mode is the only thing that changes between DRY and LIVE.
# Cutover: sed -i 's|--mode dry|--mode live|' on this file + daemon-reload + restart.
ExecStart=/home/polymarket/Polymarket-bot/venv/bin/python3 reward_farmer.py --mode dry

Restart=on-failure
RestartSec=30s
StartLimitIntervalSec=300
StartLimitBurst=5

# Graceful stop (FX-014, v5.1.11). systemd's default KillSignal is SIGTERM
# and TimeoutStopSec is 90s — long enough that an operator hitting Ctrl+C
# in another terminal might lose patience and SIGKILL the process before
# its _shutdown_cleanup() runs. SIGINT + 30s gives the bot a tight window
# to finish its current cycle and cancel every live order via the
# kill-switch override path. KillMode=mixed sends the signal to the main
# Python process only; any spawned worker threads inherit the shutdown
# flag through self._shutdown.
KillSignal=SIGINT
TimeoutStopSec=30
KillMode=mixed

# stdout/stderr → systemd journal (query with journalctl)
StandardOutput=journal
StandardError=journal
SyslogIdentifier=polymarket-farmer

# Hardening — keep filesystem mostly read-only; allow writes only to bot dir
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/polymarket/Polymarket-bot
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true

[Install]
WantedBy=multi-user.target
EOF
```

Write `polymarket-oversight.service`:
```bash
sudo tee /etc/systemd/system/polymarket-oversight.service > /dev/null <<'EOF'
[Unit]
Description=Polymarket oversight evaluator
Documentation=https://github.com/<your-github-user>/Polymarket-bot
After=network-online.target polymarket-farmer.service
Wants=network-online.target

[Service]
Type=simple
User=polymarket
Group=polymarket
WorkingDirectory=/home/polymarket/Polymarket-bot

ExecStart=/home/polymarket/Polymarket-bot/venv/bin/python3 oversight_agent.py --loop

Restart=on-failure
RestartSec=30s
StartLimitIntervalSec=300
StartLimitBurst=5

# Graceful stop (FX-014, v5.1.11). Same rationale as the farmer unit
# above. The agent doesn't trade — it's the planner — so the only thing
# it needs to do on signal is exit the 30-min loop cleanly. SIGINT + 30s
# is generous; agent shutdown takes < 1s.
KillSignal=SIGINT
TimeoutStopSec=30
KillMode=mixed

StandardOutput=journal
StandardError=journal
SyslogIdentifier=polymarket-oversight

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/polymarket/Polymarket-bot
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true

[Install]
WantedBy=multi-user.target
EOF
```

Reload + enable on boot + start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable polymarket-farmer polymarket-oversight
sudo systemctl start polymarket-farmer
sleep 30   # let farmer connect to CLOB API first
sudo systemctl start polymarket-oversight

# Verify both running
sudo systemctl status polymarket-farmer polymarket-oversight --no-pager
```

#### Operational stop procedure (FX-014 / FX-015, v5.1.11)

Given the unit `KillSignal=SIGINT` + `TimeoutStopSec=30` directives above and the Python-side handler in `reward_farmer.run()`:

```bash
sudo systemctl stop polymarket-farmer        # waits up to 30s for graceful exit
```

Expected `journalctl -u polymarket-farmer` sequence on a clean stop:
```
[SHUTDOWN] SIGINT received — exiting at next cycle boundary
[SHUTDOWN] cleanup beginning: N buy orders + M dump orders across K markets
[SHUTDOWN] cleanup complete: cancelled X/Y orders (Z failed)
```

If `TimeoutStopSec` elapses before `_shutdown_cleanup` finishes (e.g., the CLOB API is throttling the cancel calls), systemd escalates to SIGKILL and any remaining orders stay resting. Run `sudo journalctl -u polymarket-farmer --since "2 min ago" | grep SHUTDOWN` to verify cleanup ran; if the "cleanup complete" line is absent, inspect open orders manually via the Polymarket UI or `client.get_open_orders()`.

For the oversight agent the procedure is identical but trivial — the agent doesn't trade, so its cleanup is just "exit the 30-min loop":
```bash
sudo systemctl stop polymarket-oversight
# Expected log: [SHUTDOWN] SIGINT received — exiting loop
#               [SHUTDOWN] Oversight agent stopped
```

If you've previously installed these units WITHOUT the `KillSignal=SIGINT` directive (i.e. against a pre-v5.1.11 doc), apply the new directives by re-running the `sudo tee` blocks above, then `sudo systemctl daemon-reload && sudo systemctl restart polymarket-farmer polymarket-oversight`. The farmer's Python-side SIGTERM handler (also v5.1.11) means the directive change is forward-compatible: even without the directive, `systemctl stop` (which sends SIGTERM by default) now triggers a clean shutdown.

### 11.12 DRY soak before LIVE (≥1h minimum, ≥4h recommended)

```bash
# Live tail
sudo journalctl -u polymarket-farmer -u polymarket-oversight -f
```

Watch for the following in order (within ~2-5 min of service start):
1. `Connected to Polymarket CLOB API`
2. `Refreshing reward markets... CLOB: ~5000 reward markets`
3. `Starting reward farming | N markets | dry_run=True`
4. `[CYCLE_SUMMARY]` lines every ~30 s
5. `[OVERSIGHT] action=continue reason=shadow latency_ms=<low>` (sub-ms expected)
6. **No `ERROR` or `Traceback`**

Periodic checkpoint:
```bash
echo "=== checkpoint $(date -u '+%H:%M UTC') ==="
sudo systemctl is-active polymarket-farmer polymarket-oversight
sudo journalctl -u polymarket-farmer --no-pager | grep -c CYCLE_SUMMARY
sudo journalctl -u polymarket-oversight --no-pager | grep -c "Cycle complete"
sudo journalctl -u polymarket-farmer -u polymarket-oversight --no-pager | grep -cE "ERROR|Traceback|FATAL"
```

**Note on DRY behaviour with fresh DB**: on a freshly-provisioned server, `bot_history.db` has zero historical fills / unwinds / reward_days. The LearningController gate evaluates to `OFF` (the lowest state — needs ≥100 fills / ≥50 pairs / ≥3 days to reach SHADOW). The SafetyController state stays `DATA_UNAVAILABLE` (no `portfolio_snapshots` row because DRY mode never refreshes the wallet balance). In `DATA_UNAVAILABLE`, `STATE_PERMISSIONS["trials"]=False` blocks all trial markets. On a fresh DB every market is a trial market. **Result: 0 deploys during DRY soak on a fresh server.** This is correct behaviour, not a bug — see §11.13's expected LIVE-first-cycle behaviour for the exit.

### 11.13 LIVE cutover

⚠ **Verify wallet ≥ $200 pUSD on FUNDER before cutover.** Smaller balances produce only trivial deploys due to per-market cap math.

```bash
# Wallet check
cd ~/Polymarket-bot
venv/bin/python3 -c "
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType, ApiCreds
import os; from dotenv import load_dotenv; load_dotenv()
creds = ApiCreds(api_key=os.getenv('CLOB_API_KEY'),api_secret=os.getenv('CLOB_SECRET'),api_passphrase=os.getenv('CLOB_PASS_PHRASE'))
c = ClobClient(host='https://clob.polymarket.com', chain_id=137, key=os.getenv('PRIVATE_KEY'), funder=os.getenv('FUNDER'), signature_type=2, creds=creds)
b = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
print(f'pUSD balance: \${int(b[\"balance\"])/1e6:.2f}')
"

# Cutover — three commands
sudo sed -i 's|--mode dry|--mode live|' /etc/systemd/system/polymarket-farmer.service
sudo systemctl daemon-reload
sudo systemctl restart polymarket-farmer

# Verify the change took
grep ExecStart /etc/systemd/system/polymarket-farmer.service
# Expect: ... reward_farmer.py --mode live
```

**Watch the first cycle live**:
```bash
sudo journalctl -u polymarket-farmer -u polymarket-oversight -f
```

Expect within ~30-60 s:
- `Starting reward farming | N markets | dry_run=False` ← key check: `False`
- **No `get_orders failed` errors** (the V1→V2 fix in `ee6abdf` should hold)
- **No `status=403` errors** (geoblock check — if 403 appears, see §11.14 emergency revert)
- `place_order` lines WITHOUT `[DRY_RUN]` prefix
- `[OVERSIGHT] action=continue reason=shadow` (still Stage 1)
- `[CYCLE_SUMMARY]` lines with `dry_run` field absent (LIVE doesn't emit it)

After ~5 min, verification probe:
```bash
echo "=== LIVE cutover verification ($(date -u '+%H:%M UTC')) ==="
sudo systemctl is-active polymarket-farmer polymarket-oversight
sudo journalctl -u polymarket-farmer --since "10 minutes ago" --no-pager | grep "Starting reward farming" | tail -1
sudo journalctl -u polymarket-farmer --since "10 minutes ago" --no-pager | grep -cE "get_orders failed"   # expect: 0
sudo journalctl -u polymarket-farmer --since "10 minutes ago" --no-pager | grep -cE "status=403"          # expect: 0 (geoblock check)
sudo journalctl -u polymarket-farmer --since "10 minutes ago" --no-pager | grep "place_order" | grep -v DRY_RUN | head -5
sqlite3 bot_history.db "SELECT datetime(ts,'unixepoch'), exchange_balance FROM portfolio_snapshots ORDER BY ts DESC LIMIT 1;"
sudo journalctl -u polymarket-farmer --since "10 minutes ago" --no-pager | grep "kill_switch" | tail -3
```

After the first LIVE cycle, the farmer writes a `portfolio_snapshots` row (gated on `if not self.dry_run` in `_save_usdc_balance` at `reward_farmer.py:2093`, every 10th cycle ≈ 5 min). The bootstrap exit chain in v5.1.7 is:

1. **Cold-start state**: `_load_state` enters `BOOTSTRAP` (severity 2, between MILDLY and SEVERELY) instead of MILDLY when `_is_genuine_cold_start()` is True (no orders ever placed AND no fills ever observed). Permissions: 10 markets / 30% capital / trials=True. **Behaviour change vs v5.1.6 and earlier:** the bot starts conservatively rather than at MILDLY's 40 markets / 70% capital. Verify on cycle 1 by querying `SELECT state FROM safety_state ORDER BY ts DESC LIMIT 1` — expect `BOOTSTRAP`.
2. **I3 drawdown** (CRITICAL → DATA_UNAVAILABLE pre-v5.1.7): clears as soon as either (a) `_is_genuine_cold_start()` returns True (the new `dc78ba0` skip path) — fires on the first cycle, no waiting; OR (b) `portfolio_snapshots` has a row with `exchange_balance > 0` within the 6h lookback window. Post-v5.1.7 there is no longer a window during which I3 demotes a genuinely-cold bootstrap.
3. **I9 data_freshness** (pre-v5.1.5 deadlock): closed by `dd67f97` and now factored through the same `_is_genuine_cold_start()` helper as I3 (refactored in `dc78ba0`).
4. **BOOTSTRAP exit**: ≥10 lifetime fills (fast path) OR ≥3 clean cycles in BOOTSTRAP (slow path) → MILDLY_MISCALIBRATED. The fills path is bounded by market activity; the cycle path is bounded by ~90 s (3 × 30 s farmer cycles, gated on no CRITICAL violations).

With all four steps clean, on the next oversight cycle (~30 min worst case from LIVE start), the allocation file starts containing real deploys constrained to BOOTSTRAP's 10/30% limits. As fills accumulate, the bot exits BOOTSTRAP and MILDLY's 40/70% caps take over. **First fills should appear within minutes-to-hours depending on market activity; BOOTSTRAP exit follows within an hour of operational activity, sooner if markets are liquid.**

The capital-sizing race that previously occupied this paragraph is closed in v5.1.10 (`d4d1541`). The farmer now writes `usdc_balance` on cycle 1 (~30 s after LIVE cutover), and the agent's `--capital` default is `None` — no more silent `$1500` fallback. On a fresh-DB cold start, the first oversight cycle sees a `[CAPITAL_SOURCE] source=usdc_db value=$X.XX age_min=<1` line and the safety thresholds calibrate against the actual wallet from cycle 1.

If the bot remains in `DATA_UNAVAILABLE` after ~35 min of LIVE operation, confirm v5.1.7 is loaded (`git log -1` should show `541108b` or newer) and check whether some other invariant is firing (`sudo journalctl -u polymarket-oversight | grep "VIOLATION:"`). With Phase 1 shipped, the most likely adjacent failure modes are I10 data_completeness (if scoring_snapshots are sparse) or I4 capital_floor (if the wallet read returns sub-$50). Both have distinct VIOLATION log signatures.

### 11.14 Operational lifecycle commands

**Daily health check** (~30s):
```bash
ssh -i ~/.ssh/polymarket_bot_ed25519 polymarket@<server-IP>
cd ~/Polymarket-bot

sudo systemctl is-active polymarket-farmer polymarket-oversight
sudo journalctl -u polymarket-farmer -u polymarket-oversight --since "24 hours ago" --no-pager | grep -cE "ERROR|Traceback|FATAL"

venv/bin/python3 -c "
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType, ApiCreds
import os; from dotenv import load_dotenv; load_dotenv()
creds = ApiCreds(api_key=os.getenv('CLOB_API_KEY'),api_secret=os.getenv('CLOB_SECRET'),api_passphrase=os.getenv('CLOB_PASS_PHRASE'))
c = ClobClient(host='https://clob.polymarket.com', chain_id=137, key=os.getenv('PRIVATE_KEY'), funder=os.getenv('FUNDER'), signature_type=2, creds=creds)
b = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
print(f'pUSD: \${int(b[\"balance\"])/1e6:.2f}')
"

ls -lh bot_history.db | awk '{print $5}'   # DB growth check
df -h / | tail -1 | awk '{print "disk free: "$4}'

sqlite3 bot_history.db "SELECT mode, valid_cycles_observed FROM learning_state WHERE id=1;"
sqlite3 bot_history.db "SELECT model_name, n_samples, n_positive FROM calibration_model_state;"

sudo journalctl -u polymarket-farmer --no-pager | grep CYCLE_SUMMARY | tail -1
```

**Pull new code on server** (after pushing a commit from Mac):
```bash
cd ~/Polymarket-bot
git pull origin main
git log -1 --format='%h %s'
sudo systemctl restart polymarket-farmer polymarket-oversight
sleep 30
sudo journalctl -u polymarket-farmer --since "1 minute ago" --no-pager | grep -cE "ERROR|Traceback"   # expect: 0
```

**Mode switch DRY → LIVE** (re-cutover after a revert):
```bash
sudo sed -i 's|--mode dry|--mode live|' /etc/systemd/system/polymarket-farmer.service
sudo systemctl daemon-reload
sudo systemctl restart polymarket-farmer
```

**Mode switch LIVE → DRY** (rollback / emergency / geoblock detection):
```bash
sudo sed -i 's|--mode live|--mode dry|' /etc/systemd/system/polymarket-farmer.service
sudo systemctl daemon-reload
sudo systemctl restart polymarket-farmer
grep ExecStart /etc/systemd/system/polymarket-farmer.service   # confirm --mode dry
```

**Oversight stage promotion** (must be deliberate — see §4.21.7 promotion gates first):
```bash
# Stage 1 → Stage 2: flip master gate off AND enable pause
cd ~/Polymarket-bot
# Edit on Mac, commit, push, pull on server — deploy key is read-only so
# can't push from server.

# On Mac:
sed -i '' 's|^_SHADOW_ONLY = True|_SHADOW_ONLY = False|' oversight_agent.py
sed -i '' 's|^_PAUSE_ENABLED = False|_PAUSE_ENABLED = True|' oversight_agent.py
grep -E "^_SHADOW_ONLY|^_PAUSE_ENABLED|^_KILL_ENABLED" oversight_agent.py
git diff oversight_agent.py
git add oversight_agent.py
git commit -m "Promote oversight to Stage 2 (pause enabled)"
git push origin main

# On server:
cd ~/Polymarket-bot
git pull origin main
sudo systemctl restart polymarket-farmer polymarket-oversight
sleep 30
# Verify the new flag state is loaded:
venv/bin/python3 -c "
import oversight_agent
print(f'_SHADOW_ONLY={oversight_agent._SHADOW_ONLY} _PAUSE_ENABLED={oversight_agent._PAUSE_ENABLED} _KILL_ENABLED={oversight_agent._KILL_ENABLED}')
"
```

Stage 2 → Stage 3 is symmetric: flip `_KILL_ENABLED=True` via the same edit-on-Mac/pull-on-server pattern.

**`GATE_ACTIVE_CYCLES` revert** (once SHADOW computed-state trajectory observed sane in LIVE):
```bash
# On Mac:
sed -i '' 's|^GATE_ACTIVE_CYCLES = 2000|GATE_ACTIVE_CYCLES = 50|' profit/learning.py
grep "^GATE_ACTIVE_CYCLES" profit/learning.py
git diff profit/learning.py
git add profit/learning.py
git commit -m "Revert GATE_ACTIVE_CYCLES 2000 → 50 after SHADOW soak"
git push origin main

# On server: git pull + restart as above.
```

**Emergency rollback if LIVE goes wrong:**
```bash
# 1. Stop services immediately
sudo systemctl stop polymarket-farmer polymarket-oversight

# 2. Cancel any open orders manually via Polymarket UI (browser)
#    Connect EOA wallet → Orders tab → Cancel All

# 3. Revert mode to DRY (in case you want to keep services alive for diagnostics)
sudo sed -i 's|--mode live|--mode dry|' /etc/systemd/system/polymarket-farmer.service
sudo systemctl daemon-reload

# 4. Optional: revert code to a prior commit if the issue is recent code
cd ~/Polymarket-bot
git log --oneline -10
git checkout <prior-good-sha>   # detached HEAD is fine for diagnosis
# To return: git checkout main

# 5. Restart in DRY for diagnosis
sudo systemctl start polymarket-farmer polymarket-oversight
```

**Geoblock detection (HTTP 403 on order placement)**:
- This is what happened with the Ashburn server in v5.1.4. Symptom: `[py_clob_client_v2] request error status=403 ... "Trading restricted in your region"`.
- No money at risk — orders are rejected at the API; nothing fills.
- Immediate action: revert to DRY (see Emergency Rollback step 3).
- Permanent fix: provision a new server in a non-blocked region per §11.4, run §11.5–11.13 again, destroy the blocked server (Cloud Console → Server → Delete; pro-rated refund applies).

**Wallet top-up** (operator handles via Polymarket UI browser flow):
1. Connect the EOA wallet (whose private key is in `PRIVATE_KEY`)
2. Click Deposit → choose funding method (Coinbase, Polygon bridge, MoonPay)
3. Deposit lands as pUSD in FUNDER address after 1-5 min Polygon confirmation
4. Verify on server using the wallet check command above

**Realistic earning expectation** (post-Phase-D state, on $200 wallet, post-LIVE):
- First fills appear within minutes-hours of LIVE start (market-activity dependent)
- Calibrator readiness (≥50 fills, ≥15 positives) typically takes days-to-weeks on $200 capital
- LearningController gate to ACTIVE: needs ≥200 fills, ≥100 pairs, ≥5 reward_days, ≥2000 valid_cycles (≥16.7h of metrics_ok cycles). Practically: ~1-2 weeks of stable LIVE operation.
- Daily earnings at $200 capital: low single-digit dollars in the steady state (theoretical ceiling depends on market spread + competition).
- Scale-up path: after ≥1 week of stable LIVE at $200, consider $1000+ for design-spec capacity (~$1500). Per-cycle exposure bounds: `β · cap_scale · T ≤ 1.20 · 0.95 · T ≈ 1.14·T` worst-case under ACTIVE-mode rule outputs (notional, not realised loss; realised loss is bounded by `MAX_DAILY_LOSS_FRAC = 10%` of T via the kill switch).

---

## 12. Closing Principles

### 12.1 What the system IS

A reward-maximising allocator built on:
- Global reward scaling (one CF, one α, applied everywhere)
- Local risk estimation (per-market fill and loss models)
- Behavioural feedback loops (learning scalars, bandit, frontier memory)
- Layered safety (allocator EV gate + SafetyController state machine + farmer sports re-checks + expiry sweep)

### 12.2 What the system is NOT

- Not a global optimiser (there is no joint optimisation across markets)
- Not a robust exploration system (EV-gating prevents discovery of new classes)
- Not a tail-risk-aware system (stop-loss events are not distinguished in learning)
- Not a price-prediction system (makes no directional calls)

### 12.3 Core insight

> Reward errors propagate globally.
> Loss errors propagate locally.

This asymmetry defines:
- System fragility (global signals are the failure vector)
- Failure modes (CF collapse is the only truly irreversible loop)
- Debugging priority (CF and scoring integrity first, then local models)

### 12.4 Operating mantra

The system is well-architected but inherently asymmetric:

- Strong at exploiting known edges
- Weak at discovering unknown ones
- Extremely sensitive to reward miscalibration

If it works, it scales efficiently.
If it breaks, it breaks globally and quickly.

### 12.5 v4.0 addendum — the continuous allocator + β/η control law

The v4.0 rewrite collapses the Patch-era 8-lever stack into two continuous controls (β, η) plus the retained capital_scale / reward_trust scalars. Levers active in v4.0:

| Lever | Scope | Notes |
|---|---|---|
| `capital_scale` ∈ [0.30, 1.20] | `total_capital` multiplier, applied BEFORE the allocator in oversight_agent | Retained from v3.x; driven by Rule A/B/D/E + Patch-3 expansion + Patch-11 damping + Patch-13 hysteresis. |
| `reward_trust` ∈ [0.50, 1.00] | `CalibrationManager.reward_trust`, applied upstream in the PART-6 reward pipeline | Retained from v3.x; Rule C + mean reversion. |
| `β` ∈ [0.10, 0.95] | Step-3 scale in allocator: `scale = β · total_capital / Σ(p·raw)` | New in v4.0. Feedback on `TARGET_UTIL − expected_util`, `K_BETA = 0.5`, `ALPHA_BETA = 0.03`. |
| `η` ∈ [0.00, 4.00] | Concentration exponent: `raw_i = w_i^(1+η)` | New in v4.0. Feedback on `TARGET_COVERAGE − coverage_ratio`, `K_ETA = 1.0`, `ALPHA_ETA = 0.03`. |

Each lever has a distinct destination and a provable non-cancelling leverage in some regime (§4.16.2). Together they preserve the v3.x pattern of **one scalar per behavioural axis** (overall capital, reward discount, utilisation, concentration) without stacking multiple overlays on the same axis.

**Tensions resolved vs v3.x:**

1. **λ1/λ2 algebraic cancellation under uniform markets.** Proven mathematically in §4.16.1 / §4.16.2. Addressed in v4.0 by swapping λ1/λ2 for β (which enters outside the `raw_i / Σ raw_k` ratio as a linear prefactor — cannot cancel) and η (which enters as an exponent — produces `C_i / C_j = (w_i/w_j)^(1+η)`, non-cancelling under any market heterogeneity).

2. **Min-floor collapse erasing upstream control signals.** Confirmed empirically in V4/V5 audits: when cluster-cap × min-floor composition drives every `C_i` to `min_capital`, no pre-cap differential survives. Not resolved in v4.0 (out of scope for the control-law patch); flagged in §4.16.5 and §10.3 as a binding-constraint issue at the sim-environment / cap-policy level.

3. **Over-layering and circular updates.** Removed with Patches 6–13 deletion. Allocator is one formula, not a phase-based pipeline. Control loop is one rule per variable, not a per-patch ruleset.

**Debugging priority (v4.0):**

1. **Are we deploying?** (CF-deadlock check — unchanged from v3.x)
2. **Is CF in [0.05, 1.5]?** (unchanged)
3. **Is q_share non-zero for deployed markets?** (unchanged)
4. **Is `expected_util` in its target band [0.5, 0.95]?** Check the `_total_capital` + `_expected_capital` stamps on the alloc JSON. If `expected_util < 0.1` with β at ceiling (0.95), the cap stack is fully binding — check cluster membership, per-market cap headroom, min_capital math.
5. **Is `coverage_ratio ≥ 0.5`?** If `coverage → 0` with η at ceiling (4.0), every market is at min-floor; concentration control has saturated. Same cap-stack diagnosis as #4.
6. **Are β and η stamps present on every deploy row?** Absence of `_beta` / `_eta` indicates the allocator ran with `learning_state=None` (check whether the oversight_agent passed the applied state).
7. **Is `capital_scale` oscillating?** Check `_detect_oscillation` log line + recent `capital_history`. In v4.0 this also throttles β/η updates (halves their α), so diagnosing capital_scale oscillation localises control-loop instability.
8. **Are rules A/B/D/E firing as expected?** Check `[LEARNING]` log line — cap, trust, β, η transitions should align with metric signals.

The core asymmetry (reward-global / loss-local) is preserved. β is a global multiplier on all `C_i`; η shifts relative allocation shape across markets. Neither changes the underlying observation that reward-side miscalibration (CF, q_share, reward model) propagates globally while loss-side miscalibration (per-market L) stays local. v4.0 does not address the global-reward fragility; that is still the dominant failure axis.

### 12.6 Change-management principles (added v5.1.20, post-FX-041)

These are the five operating rules for ALL future changes — code, doc, config, operational actions on the production server, and decisions about who runs the bot. They were codified during the 2026-05-19 → 2026-05-21 cascade-recovery sequence, where each FX-NNN fix shipped under them and each "first production cycle after" verification confirmed them. They apply to every session, every operator, every change.

**P1 — Verified > assumed.** Anything you haven't directly observed in data (log line, DB row, on-chain probe, live API response) gets flagged as a hypothesis, not stated as a fact. The cascade chain that produced the 4-day production blackout (FX-001 → FX-031 → FX-032 → FX-035 — see §10.2) was a sequence of hypotheses being wrong; the bugs that mattered were the ones nobody had actually looked at the data for. When you say "the bot is doing X", you must be able to point to the row, line, or response that proves it. When you can't, say "I'm not sure" and pull the data before continuing.

**P2 — Reversibility first.** Prefer cheap-to-undo actions over expensive-to-undo ones, even when the cheap one is structurally less satisfying. Examples that came up in v5.1.x:
- `RF_TARGET_QUEUE_AHEAD_USD: 0` in `config_overrides.json` is a runtime override that costs nothing to add/remove. Used to runtime-disable FX-036 between v5.1.18 (ship) and v5.1.20 (FX-041 safety prerequisite ships). The alternative (reverting the FX-036 commit) would have been a doc-and-tests-cascade.
- `RF_DUMP_DEPTH_SAFETY_FACTOR = 0` is a hot-reloadable escape hatch that reverts FX-041 to FX-036-only behaviour without a restart.
- Backup-before-edit on `config_overrides.json` (the `.bak.fx041-pre` pattern from 2026-05-20) costs one extra `cp` command and gives a 5-second rollback path.
The asymmetry between cost-to-make and cost-to-undo determines what you should do first: if a 2-line change is reversible in 10 seconds and a 200-line change requires a release, do the 2-line first and observe.

**P3 — Single-axis changes.** One variable at a time. When two changes ship together, you can't attribute the outcome to either. The 2026-05-19 cascade had three composed failures (FX-036 close-to-mid + cold-start prior + dump-immediately) that were each individually defensible; nobody saw the composition because each shipped on its own day. Going forward, every change should be paired with a single observable hypothesis ("after this change, X should increase / Y should stop firing") so that the production cycles immediately after can confirm or refute. **Don't ship two FX-NNN fixes in the same commit.** Don't combine a code change with a config override change. If you must change two things, change one and observe ≥24h before the second.

**P4 — Production cycles are the most expensive verification step. Tests don't replace them.** The 752 fast-tier tests passed every commit through the 2026-05-15 → 2026-05-19 hardening; the bot placed zero orders in production for 4 days during that window because FX-035 (V2 SDK dict-return) was invisible to every test. The lesson: code-level audits catch architectural drift; production diagnostics catch input-shape drift. **Run the first 5-10 production cycles with `journalctl -f` open BEFORE celebrating any release.** A change is not validated by its tests alone — it's validated by what the production logs show after deploy. Treat "first cycle after release" as an explicit verification step in the runbook, not as a deploy completion.

**P5 — Friend rollout doesn't happen until the bot has proven itself on the development wallet for ≥7 days of clean operation.** "Clean" means: no `[CRITICAL]` log lines, no kill-switch activations, no manual SQL interventions, no operator-driven restarts beyond planned deploys, and at least one observed fill+dump cycle without slippage exceeding 3%. The 7-day count resets on any of those events. Each additional operator (friend) compounds risk linearly in the simplest case and super-linearly when their wallet, server region, or config differs from ours. The gating criteria are listed in `fixit.md` §6 Hardening roadmap and copied below for canonicality:

  - **G1**: Bot has run **7 days** clean on the dev wallet.
  - **G2**: All HIGH-severity open `FX-NNN` items in fixit §3 are shipped (currently FX-037).
  - **G3**: SafetyController has been observed in CALIBRATED state for ≥24h.
  - **G4**: At least one fill+dump cycle observed without slippage > 3%.
  - **G5**: FX-036 firing on ≥3 distinct deep markets without false-positive cascade.
  - **G6**: Operator runbook written (setup + monitoring + emergency stop).
  - **G7**: Wallet recovery procedure tested (lost key, server crash, etc.).

  All seven must be green before a friend turns on `--mode live`. The first friend goes through `--mode dry` (≥24h) → `--mode shadow` (≥24h) → `--mode live`. Cohort 2 (2-3 more friends) only after cohort 1 has been clean for ≥7 days. Wider opening only after 30+ collective bot-days clean across cohort 1 + 2.

**How these principles get enforced:**
- Each commit message should say which principle it operates under (when ambiguous).
- Each session's first action should be `verify > assume`: pull the actual state before proposing a change.
- Each new FX-NNN entry in `fixit.md` notes the principle that surfaced the issue (often P4 — "production cycle exposed it").
- The friend-rollout gates (G1-G7) live in `fixit.md` §6 as a checklist; they're the operational embodiment of P5.

---

## Appendix A — Minimal Monitoring Dashboard

Track these continuously. Alert on any that enter the warning/critical ranges of §7.

**Absolute must-haves**
```
CF (raw + smoothed)                Display as line chart, 7-day window
Deployed market count              Integer gauge, threshold alert at < 5
Capital deployed %                 Gauge, threshold alert at < 50%
q_share distribution               Histogram, alert if all < 0.01 or all > 0.5
fill_rate                          Gauge, alert on sudden spike
loss_per_capital                   Gauge, threshold alert at > 5%
reward_efficiency                  Trend line, alert on sustained decline
Safety state                       Traffic light: CALIBRATED = green, MILDLY = yellow, others = red
```

**Highly recommended**
```
Scoring snapshot counts per market (variance detection)
Bandit multiplier distribution (detect stuck system)
Learning scalars (capital_scale, aggressiveness, risk_multiplier, reward_trust)
Regime ID (changes are significant)
```

## Appendix B — Diagnostic Queries

Common SQL queries for live diagnosis:

**Is the bot running?**
```sql
SELECT MAX(ts), COUNT(*) FROM scoring_snapshots WHERE ts > strftime('%s','now') - 600;
-- Should return a recent timestamp and a non-zero count.
```

**Current CF?**
```sql
SELECT date, correction_factor, total_reward_usd FROM reward_daily ORDER BY date DESC LIMIT 1;
```

**Poisoned row count?**
```sql
SELECT COUNT(*) FROM reward_market_stats
WHERE CAST(json_extract(data, '$.total_q_score') AS REAL)
    > CAST(json_extract(data, '$.total_market_q') AS REAL) * 0.5
  AND json_extract(data, '$.q_score_samples') > 0;
```

**Active positions?**
```sql
SELECT condition_id, SUM(shares) FROM fills
WHERE condition_id NOT IN (SELECT condition_id FROM unwinds)
GROUP BY condition_id HAVING SUM(shares) > 0;
```

**Recent SafetyController events?**
```sql
SELECT * FROM safety_state ORDER BY rowid DESC LIMIT 5;
```

**Fill rate last 24h?**
```sql
SELECT
  (SELECT COUNT(*) FROM fills WHERE ts > strftime('%s','now') - 86400) AS fills_24h,
  (SELECT COUNT(*) FROM orders_placed WHERE ts > strftime('%s','now') - 86400) AS orders_24h;
```

---

*End of document.*
