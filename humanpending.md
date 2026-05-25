# Human-Gated Decisions — Phase B execution

Created 2026-05-25 during Phase 0 ground-truth + B1 deployment.
All items here block specific downstream work; Claude continues non-dependent
threads in parallel until each is resolved.

---

## D1 — Strategic path choice [BLOCKS B5 and below]

**MAJOR REVISION 2026-05-25 post-Polymarket-API discovery.** Phase 0 found
`GET /rewards/user/percentages` exposes the user's real-time q_share per market.
Live probe returned `0.0845` for our deployed market vs Priority 1's `0.42`
estimate (5× over) and Priority 2's `0.000452` (190× under). This replaces
the entire FX-045/FX-046 estimation problem with a direct API call.

Four viable paths:

- **Path A (revised) — API-based q_share + continue Master Plan.** Ship
  Option E (new): integrate `/rewards/user/percentages` as Priority 0 q_share
  source. Eliminates I6 ratio mismatch, unfreezes SafetyController, then
  diversify (Phase C) → size opt (Phase D) → friend rollout. ~2-3 weeks.
  Higher confidence than original Path A because the q_share fix is now
  rooted in production data, not estimation.

- **Path B — Strip and simplify.** Disable SafetyController, deploy on top
  20 markets by daily_rate at $10 each, drop calibration models, measure
  empirically. ~1 week. Higher ceiling potential but higher risk; would discard
  4000 LOC of safety scaffolding.

- **Path C — Acknowledge scale mismatch.** $226 is below the wallet size the
  architecture was designed for. Either scale wallet to $1000-1500 first or
  accept the bot is a slow rule-based earner. No code change required.

- **Path D (hybrid) — Ship Option E only, then observe before further changes.**
  Single-axis (P3): just integrate the API q_share, observe 24-48h, then
  decide whether to continue Path A or pivot.

**Recommendation under R2:** Path D first. Ship Option E (~3-4h code + 24h
observation), then re-evaluate. It's a single-axis fix with the highest
confidence we've had on any q_share change, and it makes every downstream
measurement (I6, SafetyController state, expected_util, CF) trustworthy
before we commit to bigger architectural moves.

**Status:** Open. Operator decision required. Recommendation: Path D.

## D2 — Trial budget hot-reload knob [BLOCKS market diversification]

Phase 0 revealed `RF_TRIAL_BUDGET_PCT = 0.25` is the new binding constraint
keeping the bot on 1 market (post-restart MILDLY transition unlocked trials,
but trial budget ate the headroom).

Options:
- Hold at 0.25 (current; 1 market)
- Bump to 0.50 (~2-3 trials; modest discovery)
- Bump to 0.75 (~3-4 trials; faster discovery + reward aggregation above $1/day)
- Bump to 1.0 (~4-5 trials; full wallet on trials, no headroom for graduated markets)

Trade-off: higher → more reward potential, more cold-start exposure (the
exact failure mode FX-040 was designed to limit after the 2026-05-19 OpenAI
cascade lost $17.63).

**Status:** Open. Operator decision required. Hot-reloadable, fully reversible.

## D3 — Helsinki observation window post-B1 [BLOCKS B2-shaping decisions]

B1 deployed FX-037 + FX-050 + FX-049 at 04:43 UTC 2026-05-25. The bot will:
- Write first non-baseline `wallet_reconcile_history` row at next oversight
  cycle (~30 min, so ~05:13 UTC)
- Apply FX-050 taker-fee accounting to the next dump event (whenever a fill
  triggers one — last 3 fills averaged ~1 every 8h)
- Run FX-037 BUY-side phantom-fill defense on every BUY detect

**No operator action needed.** Just need to wait ~30-60 min for the first
non-trivial reconcile cycle before drawing conclusions about wallet drift.

Logged here because it sets the timing of B2 ship/validate.

---

## D4 — SimpleAllocator architectural rebuild [BLOCKS Path B-prime]

**Verdict from Phase 0 + 7-agent investigation:** the existing 19,782 LOC system has
~10,000 LOC of dormant infrastructure (SafetyController, LearningController, all 6
calibration models, β/η allocator, Bandit, shadow evaluator) producing zero useful
signal at $226 wallet. The I/O layer (~5,000 LOC) is the strong, keepable part.

**Replacement design (committed pending auth):**

```python
# simple_oversight.py — ~300 LOC replacement for oversight_agent.py
class SimpleAllocator:
    # Q-share estimation (3-tier):
    # Priority 0: /rewards/user/percentages API (real, percent-units, ÷100 = fraction)
    # Priority 1: cumulative reward_market_stats ratio (1.7× under-truth, close)
    # Priority 2: cold-start prior 0.001 (0.1%) for unseen markets

    # Allocation logic:
    # 1. Fetch /rewards/markets/current (all reward-eligible)
    # 2. Filter daily_rate ≥ MIN_DAILY_RATE (20)
    # 3. For each, estimate q_share × daily_rate = expected_reward
    # 4. Filter expected_reward ≥ MIN_PER_MARKET (0.20)
    # 5. Rank by expected_reward
    # 6. Allocate up to MAX_DEPLOYED (20) markets within wallet × DEPLOY_RATIO (0.95)
    # 7. Per-market exposure cap MAX_PER_MARKET_USD ($30 at $226)

    # Minimal safety (replaces SafetyController):
    # - Kill switch on 24h realized loss > 10% of wallet
    # - Kill switch on drawdown > 15% from peak
    # - Per-market cap (above)
    # No state machine, no calibration, no β/η
```

**Integration:**
- New entry point `simple_oversight.py --loop`
- Writes same `market_allocations.json` schema (farmer untouched)
- Feature-flagged: `RF_USE_SIMPLE_ALLOCATOR: bool = False`
- Deploy via systemd ExecStart swap: oversight_agent.py → simple_oversight.py
- 24-48h side-by-side observation before retiring old

**Tests (per R6):** 12-15 contract tests covering each filter, priority tier,
kill switch, output schema match.

**Estimated effort:** ~6h code + 24h observation + retirement of dormant infra.

**Open question:** authorization to ship?

**Recommendation:** authorize ONLY AFTER D5 (wallet top-up) decision.
- If wallet scaled to $1k+: SimpleAllocator is necessary and high-impact
- If wallet stays at $226: SimpleAllocator is technically right but yields modest gain ($1-4/day cap unchanged)

**Status:** Open. Operator decision required.

## D5 — Wallet scale-up [HIGHEST leverage, no code]

Verified at $226: theoretical ceiling $1-4/day. Verified competitive median q_share
0.074%. Verified $1 daily threshold. Verified only 2 markets out of 5,235 qualify
for $1/day at our q_share.

**Math:**
- $226: ceiling $1-4/day = $365-1,460/year (162-650% ROI annualized)
- $1,500: ceiling $8-35/day = $2,900-12,800/year (190-850% ROI annualized)
- $10,000: ceiling $50-230/day = $18k-84k/year (180-840% ROI annualized)

**Trade-off:** more capital risked. Kill switch at 10% daily loss caps catastrophic
downside. Across 30 days of operation (post-FX-040, FX-041, FX-049), max single-day
realized loss is $17.63 (2026-05-19 cascade) = 8% of $226. At $1,500 same percentage
= $120 loss.

**Recommendation:** scale wallet to $1,000-$1,500 in next 24-48h to unlock the
SimpleAllocator's full value. Without scale-up, the bot is structurally capped at
$1-4/day regardless of architecture.

**Status:** Open. Operator decision required.

## Resolved
(none yet)
