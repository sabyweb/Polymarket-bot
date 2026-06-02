# Polymarket Reward-Farming Bot — System Handoff & Status

**Purpose:** one document to hand anyone for a complete, correct understanding of the system.
**As of:** 2026-06-02 · **Repo:** `github.com/sabyweb/Polymarket-bot` · **main HEAD:** `92ec34c`
**Production (Helsinki):** code at `92ec34c`, **`--mode live`, 5-market bounded canary** + config
`RF_TARGET_QUEUE_AHEAD_USD:4000`, wallet **~$1,159.76 (flat)**, **net ~−$25 since the FX-090 deploy**;
profitability **unproven** (the adverse-fill fix is in test). Live-canary ops:
`docs/runbooks/live_canary_operator.md`; latest session: `docs/STATUS_2026-06-02.md`.

> **One-line state:** the live analysis found the real blocker — the allocator selected near-resolution /
> news markets the farmer refused (→ 0 orders) or that adversely filled (→ loss). **FX-090** fixed the
> selection (farming resumed, `0/5`→`5/5 on-book`), **FX-091** made the capital-efficiency scorecard
> truthful (~1.4%/day gross — good), **FX-092** closed a monitoring gap (kills now page Discord). Gross
> yield is there; **net is still negative (~−$25) from adverse fills on longer-dated news markets** —
> round 1 of that fix (deeper queue) is live but unproven. **Net-positive is not yet achieved.**

---

## 1. What it is & the objective

A two-process bot that **farms Polymarket CLOB liquidity rewards** by resting `min_size` limit orders
inside many markets' reward zones, while avoiding capital loss from adverse fills. The single objective
(`ground_rules.md`, immutable): **maximize daily reward earnings, capital-efficiently, while remaining
profitable.** Three ground rules:

1. **Max-farm** — be on as many reward-eligible markets as possible at ~`min_size` each; aggregate
   sub-$1 accruals (Polymarket pays a $1/day/user threshold ~00:20 UTC).
2. **Exploit capital overcommit** — total limit-order notional may exceed the wallet (3–8× by design);
   when one order fills the exchange auto-cancels the rest. Per-market cap ≈ wallet/target-count.
3. **Self-learning loop** with 6 mandatory auto-correction triggers (cool persistent losers, widen
   queue on high fill-rate, expand on low reward, tighten on global loss, recalibrate q_share, kill on
   realized loss).

## 2. Architecture (two processes, file-coupled)

Two independent OS processes, **no shared memory** — coupled only by one JSON file + one SQLite DB:

- **Farmer** — `reward_farmer.py`, ~30s cycles. *Executes*: discovers markets, consumes
  `market_allocations.json`, places/cancels limit orders, detects fills, dumps inventory, enforces all
  execution-time guardrails + kill switches.
- **Oversight planner** — `simple_oversight.py`, ~30min cycles. *Plans*: probes the wallet, snapshots
  it, scores/filters/allocates via `SimpleAllocator`, runs the learning loop
  (`MarketROITracker` + `DecisionPolicy`), writes `market_allocations.json` (incl. a `kill_switch` flag).

**Critical caveat:** the architecture doc's deep §4 prose still describes a **legacy** stack
(`oversight_agent.py` + `SafetyController` + β/η allocator). That is rollback-only. **Trust the
"Current Production State" table** at the top of the architecture doc over the §4 bodies. The live
planner is `simple_oversight.py`, NOT `oversight_agent.py`.

## 3. Reading order (onboard in this order)

1. `README.md` — 1-screen overview
2. `ground_rules.md` — the immutable contract (what the system must do)
3. **this file** (`docs/HANDOFF.md`)
4. `Polymarket bot architecture v5.1.md` — reference; **trust the Current-Production-State table over §4**
5. `Polymarket bot fixit.md` — open/closed issue tracker (FX-NNN), now v1.35
6. `docs/STATUS_2026-05-31.md` — latest session record (Addenda 1–5)
7. `docs/runbooks/*` — operational procedures
8. `CHANGELOG.md` — version history (v6.1→v6.6 summarized; detail in fixit)

---

## 4. File manifest (path → role)

All paths are **repo-relative**. The repo is deployed 1:1 to Helsinki at
`/home/polymarket/Polymarket-bot/`. GitHub: `github.com/sabyweb/Polymarket-bot` (branch `main`).

### 4a. LIVE code path — core logic (read these to understand the system)

| Path | Role |
|---|---|
| `reward_farmer.py` | Farmer process: cycle loop, placement, fill detection, dump trigger, all guardrails/kills |
| `simple_oversight.py` | Oversight planner: wallet probe, snapshot, learning loop, allocate, write alloc JSON |
| `simple_allocator.py` | `SimpleAllocator` (OverCommit): market scoring, EV gate, cost-to-score sizing, `check_kill_switch` |
| `market_roi_tracker.py` | Per-market rolling ROI/reward/loss/capital; **reward sourced from data-api `/activity` (FX-088)** |
| `decision_policy.py` | Learning policy: cooldowns + the behavior-change flags the allocator consumes |
| `order_lifecycle.py` | Order place/replace/TTL/reconcile, queue-aware placement (FX-036), 3-line fill detection |
| `dump_manager.py` | Inventory unwind: decay dump, FX-071 bounded-loss floor, **FX-089 execution-price booking** |
| `database.py` | `BotDatabase` (thread-local WAL SQLite), ~31 tables, FX-080 rollback; `record_heartbeat` (FX-083) |
| `config.py` | All `RF_*` knobs + `BotConfig` hot-reload of `config_overrides.json` |

### 4b. LIVE code path — shared / support

| Path | Role |
|---|---|
| `models.py` | `OrderSlot`, `MarketState` dataclasses |
| `alerts.py` | Discord + file alerts; `alert_heartbeat_failure` + `maybe_alert_stale_heartbeat` (FX-083) |
| `market_discovery.py` | `fetch_all_reward_markets`, `get_merged_book` (used by farmer + dump_manager) |
| `price.py` | CLOB price math (`to_clob`, `to_yes_equiv`) |
| `state.py` | `PositionStore` — held-position bookkeeping |
| `rate_limiter.py` | `RateLimitedClient` — wraps the py-clob-client-v2 CLOB client |
| `reward_tracker.py` | `RewardTracker` — hourly reward **logging** (telemetry only; NOT the authoritative reward source — that's `market_roi_tracker` via FX-088) |
| `oversight/wallet_reconciliation.py` | FX-049/055 cash-invariant reconciler (data-api `/activity` rewards); pages on desync |

### 4c. ⚠ Live-used helpers that live *inside* otherwise-legacy modules (don't delete these)

| Path | What's live |
|---|---|
| `oversight/data_collector.py` | only `_connect_db` is used by the live farmer (rest is legacy) |
| `profit/correlation.py` | `build_fill_clusters` — used by the farmer's cluster-notional guardrail |
| `oversight_agent.py` | **legacy planner**, BUT the farmer imports it for an *optional, no-op* `evaluate()` shadow hook (returns "continue" if absent). The live planner is `simple_oversight.py`. |

### 4d. Docs / the "share set"

| Path | Role |
|---|---|
| `README.md` | Overview |
| `ground_rules.md` | Immutable contract (v1.1) |
| `Polymarket bot architecture v5.1.md` | Design + ops reference (content is **v6.6**; filename is legacy) |
| `Polymarket bot fixit.md` | FX-NNN issue tracker (v1.35) |
| `docs/HANDOFF.md` | **this file** |
| `docs/STATUS_2026-05-31.md` | Latest session record (Addenda 1–5) |
| `docs/STATUS_2026-05-29.md` | Prior session record |
| `docs/runbooks/deployment_from_scratch.md` | Provision a fresh server (note: §11 has some v5.x-era staleness) |
| `docs/runbooks/9_of_10_p5_p7_operator_runbook.md` | Staged bring-up + G-C/G-E gate scripts |
| `docs/runbooks/stage_c_pull_2026-05-31.md` | The dry→live cutover procedure (already executed) |
| `CHANGELOG.md` | Version history (v6.1→v6.6 summarized at top) |

### 4e. Tests + config

| Path | Role |
|---|---|
| `tests/` | **59 test files** (the real suite; 1116 pass + 1 known pre-existing legacy failure). Run: `pytest tests/ --ignore=tests/test_simulation.py` (fast tier) |
| `requirements.txt` | Python deps (Python 3.14; `py-clob-client-v2==1.0.0`) |
| `.env.example` | Template for the secrets file (see 4g) |
| `.gitignore` | Excludes `.env`, `*.db`, `logs/`, `market_allocations.json`, `positions.json`, `.claude/`, `venv/`, diagnostic scripts |

### 4f. NOT in the repo — server-only / generated (live on Helsinki)

| Path (on Helsinki) | What it is |
|---|---|
| `/etc/systemd/system/polymarket-farmer.service` | Farmer unit — `ExecStart … reward_farmer.py --mode live` |
| `/etc/systemd/system/polymarket-oversight.service` | Oversight unit — `ExecStart … simple_oversight.py --loop` |
| `/home/polymarket/Polymarket-bot/config_overrides.json` | Hot-reloadable knob overrides. **Currently:** `{RF_TRIAL_BUDGET_PCT: 0.75, RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC: 0.01, RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS: 5}` |
| `/home/polymarket/Polymarket-bot/bot_history.db` | Live SQLite state/history (WAL) — the source of truth for fills/unwinds/ROI/cooldowns |
| `/home/polymarket/Polymarket-bot/market_allocations.json` | The farmer↔oversight coupling file (regenerated each oversight cycle) |
| `/home/polymarket/Polymarket-bot/.env` | **Secrets — never share.** Vars: `CLOB_API_KEY`, `CLOB_SECRET`, `CLOB_PASS_PHRASE`, `PRIVATE_KEY`, `WALLET_ADDRESS`, `FUNDER`, `DISCORD_WEBHOOK_URL` |
| `/home/polymarket/Polymarket-bot/logs/` | Runtime logs (also via `journalctl`) |

### 4g. Legacy / ignore for understanding the live system

Entry points & old stack: `bot.py`, `main.py`, `oversight_agent.py` (see 4c caveat),
`oversight/{safety_controller,data_collector,market_scorer,allocation_writer}.py` (only the helpers in
4c are live), `profit/allocator.py`, `calibration/*` (dormant fill/hazard/loss/reward models),
`paper_client.py`, `paper_trader*.py`, `simulate.py`, `backtest.py`, `arbitrage.py`, `unwind.py`,
`order_manager.py`, `market.py`, `orders.py`, `fills.py`, `pricing.py`, `placement.py`.
Utility/diagnostic (gitignored or one-off): `dashboard.py` (Streamlit), `check_wallet.py`,
`generate_keys.py`, `set_allowances.py`, `revoke_allowances.py`, `diagnose_*.py`,
`analyze_distributions.py`. **Top-level `test_*.py` (7 files) are legacy — the real suite is `tests/`.**

---

## 5. Operations

- **SSH (private key required):** `ssh -i ~/.ssh/polymarket_bot_ed25519 polymarket@46.62.209.203`
  (Hetzner Helsinki `hel1` — the only Hetzner region that clears Polymarket's geoblock).
- **Halt (graceful — cancels resting orders):** `sudo systemctl stop polymarket-farmer`
- **Mode switch:** `sudo sed -i 's/--mode live/--mode dry/' /etc/systemd/system/polymarket-farmer.service && sudo systemctl daemon-reload && sudo systemctl restart polymarket-farmer`
- **Watch:** `journalctl -u polymarket-farmer -u polymarket-oversight -f` (key lines: `[CYCLE_SUMMARY]`,
  `[GUARDRAIL]`, `[SIMPLE_ALLOC]`, `[LEARN_CAPEFF]`).
- **Wallet:** FUNDER proxy `0xB23Bc80E6719099aeBE0c34389f05EC8C928503f`, ~$1184.53.
- **Authoritative reward/P&L source:** `https://data-api.polymarket.com/activity?user=<funder>&type=REWARD`
  (and `MAKER_REBATE`) — public, no auth, on-chain credits. Rewards are a **daily aggregate** paid ~00:20 UTC.
- **Safety stack (all armed):** realized-loss kill (10% wallet/24h), cash-drawdown kill (15% from peak),
  **held-inventory unrealized-loss kill (FX-084, 20%)**, fill-rate spike kill, rapid-growth kill
  (FX-058, with FX-087 cold-start fix), oversight-silence farmer backstop (FX-082),
  **heartbeat→Discord stall alert (FX-083)**, wallet-desync paging (FX-049/055).

## 6. What shipped recently (the path to here)

- **9/10 plan (FX-051→061):** the OverCommit allocator + the 6-trigger self-learning loop.
- **Pre-cutover hardening (FX-063→077, FX-078/080/081/082):** fill/dump/accounting/safety fixes +
  oversight persistence cluster + farmer drawdown backstop.
- **This session (FX-083→089):** heartbeat alert, unrealized-loss kill, `capital_efficiency` metric,
  cold-start cfg (closes FX-064), rapid-growth cold-start fix, and the two **accounting fixes the live
  canary exposed**: **FX-088** reward sourcing → data-api (`reward_earned` $0 → real $10.12; un-blinds
  `capital_efficiency` + the learning ROI) and **FX-089** dump unwinds booked at the marketable
  execution price (recorded −$85 vs on-chain −$22).
- **Canary saga:** first live canary (cap-3) ran 5.4h, lost ~$17 real (not the −$85 the broken
  accounting reported), earned $10.12 rewards, validated the learning loop firing for the first time,
  then halted → fixes shipped → **re-launched live (cap-5) on corrected accounting** (the loop had
  already cooled the prior losers).

## 7. Honest status & what remains

- **Done + proven:** correct reward/loss accounting (FX-088 verified in prod); all safety limbs built +
  armed; the bot is live, resting, self-cooling losers, wallet flat, 0 kills/desyncs.
- **NOT done / unproven (the objective):** **net profitability** (rewards > losses) over a multi-day
  soak. The only data point so far (the cap-3 canary) was net-negative. The corrected learning loop
  *can* now self-select net-positive markets (it has correct signals for the first time), but that is
  unverified.
- **Next verdict:** the first true reward-day P&L with correct accounting lands at the **~00:20 UTC
  daily settlement**. If the corrected loop doesn't converge net-positive, the next lever is an explicit
  adverse-selection (volatility/news/near-resolution) market filter + queue-depth placement.
- **Gates still open:** G-C (a real fill+dump handled correctly under load) and G-E (7 days clean,
  rewards > losses) — both require sustained live operation.

**Honest mission rating: ~5.5/10** — the foundation is now correct and the bot is live and measurable,
but profitable reward-farming is not yet demonstrated.
