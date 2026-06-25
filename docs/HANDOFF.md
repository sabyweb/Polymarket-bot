# Polymarket Reward-Farming Bot — System Handoff & Status

**Purpose:** one document to hand anyone for a complete, correct understanding of the system.
**As of:** 2026-06-02 · **GitHub:** `github.com/sabyweb/Polymarket-bot` (branch `main`) · **main HEAD:** `29abf42`
**Local dev:** `/Users/sabyasaachikarmakar/code/polymarket_bot` · **Production (Helsinki):**
`/home/polymarket/Polymarket-bot` @ `29abf42` (the repo deploys **1:1** — repo-relative path = Helsinki path).
**Live state:** `--mode live`, 5-market bounded canary, wallet **~$1,131 (flat)**, **net ≈ −$53 since the
FX-090 deploy**, gross capital-efficiency ~2.4%/day, **net-positive UNPROVEN**.

> **One-line state:** the live canary exposed that the allocator was selecting near-resolution / news
> markets that the farmer refused (→ 0 orders) or that adversely filled (→ loss). This session shipped
> four fixes (FX-090 time-to-event filter, FX-091 capeff denominator, FX-092 kill→Discord, FX-093
> volatility filter) + a deeper-queue config. The bot now farms safely on longer-dated, low-volatility
> markets; gross reward yield is good, but **whether it is net-profitable is still unproven** and only
> resolves over a multi-day soak. The remaining binding constraint is adverse fills on news markets.

---

## 1. What it is & the objective

Two file-coupled processes that **farm Polymarket CLOB liquidity rewards** by resting `min_size` limit
orders inside many markets' reward zones while avoiding capital loss from adverse fills. Immutable
objective (`ground_rules.md`): **maximize daily reward earnings, capital-efficiently, while remaining
net-profitable.** Three ground rules: (1) **max-farm** breadth at ~`min_size` (aggregate sub-$1 accruals;
Polymarket pays a $1/day/user threshold ~00:20 UTC); (2) **exploit capital overcommit** (total notional
3–8× wallet by design — one fill auto-cancels the rest); (3) a **self-learning loop** with 6 mandatory
auto-correction triggers.

## 2. Architecture (two processes, file-coupled)

- **Farmer** — `reward_farmer.py`, ~30 s cycles. *Executes:* discovers markets, consumes
  `market_allocations.json`, places/cancels orders, detects fills, dumps inventory, enforces all
  execution-time guardrails + kill switches.
- **Oversight planner** — `simple_oversight.py`, ~30 min cycles. *Plans:* probes the wallet, snapshots it,
  scores/filters/allocates via `SimpleAllocator`, runs the learning loop (`MarketROITracker` +
  `DecisionPolicy`), writes `market_allocations.json` (incl. a `kill_switch` flag).
- Coupled only by **one JSON file** (`market_allocations.json`, 2 h TTL) + **one SQLite DB** (`bot_history.db`).
- ⚠ **Critical caveat:** the architecture doc's deep §4 prose still describes a **legacy** stack
  (`oversight_agent.py` + `SafetyController` + β/η allocator) — rollback-only. **Trust the
  "Current Production State" table (v6.7) at the top of the architecture doc over the §4 bodies.**

## 3. Reading order (onboard in this order)

1. `README.md` — 1-screen overview
2. `ground_rules.md` — the immutable contract
3. **this file** (`docs/HANDOFF.md`)
4. `Polymarket bot architecture v5.1.md` — reference; **trust the Current-Production-State table over §4**
5. `docs/STATUS_2026-06-02.md` — **latest session record** (root cause + FX-090→093 + the bleed + monitoring regime)
6. `docs/runbooks/live_canary_operator.md` — **how to run/monitor the live bot** (alerts, kill response, tuning knobs)
7. `Polymarket bot fixit.md` — open/closed issue tracker (FX-NNN), v1.37
8. `CHANGELOG.md` — version history

---

## 4. File manifest (path → role)

All paths are **repo-relative** and deploy **1:1** to Helsinki at `/home/polymarket/Polymarket-bot/`.

### 4a. LIVE code path — core logic (read these to understand the system)

| Path | Role |
|---|---|
| `reward_farmer.py` | Farmer process: cycle loop, placement, fill detection, dump trigger, all guardrails/kills (incl. FX-092 kill→Discord) |
| `simple_oversight.py` | Oversight planner: wallet probe, snapshot, learning loop, allocate, write alloc JSON |
| `simple_allocator.py` | `SimpleAllocator` (OverCommit): scoring, EV gate, **FX-090** time-to-event filter, **FX-093** recent-volatility filter, `check_kill_switch` |
| `market_roi_tracker.py` | Per-market rolling ROI/reward/loss/capital; reward from data-api `/activity` (FX-088); **FX-091** capeff denominator |
| `decision_policy.py` | Learning policy: cooldowns + the 5 behavior-change flags the allocator consumes |
| `order_lifecycle.py` | Order place/replace/TTL/reconcile, queue-aware placement (FX-036), per-side gating, fill detection |
| `dump_manager.py` | Inventory unwind: decay dump, FX-071 bounded-loss floor, FX-089 execution-price booking |
| `database.py` | `BotDatabase` (thread-local WAL SQLite), ~31 tables, FX-080 rollback, `record_heartbeat`, `book_snapshots` |
| `config.py` | All `RF_*` knobs + `BotConfig` hot-reload of `config_overrides.json` |

### 4b. LIVE code path — shared / support

| Path | Role |
|---|---|
| `models.py` | `OrderSlot`, `MarketState` dataclasses |
| `alerts.py` | Discord + file alerts: `alert_kill_switch` (FX-092), heartbeat (FX-083), wallet desync (FX-074) |
| `market_discovery.py` | `fetch_all_reward_markets`, `get_merged_book` |
| `price.py` | CLOB price math (`to_clob`, `to_yes_equiv`) |
| `state.py` | `PositionStore` — held-position bookkeeping |
| `rate_limiter.py` | `RateLimitedClient` — wraps py-clob-client-v2 |
| `reward_tracker.py` | Hourly reward **logging** (telemetry only; NOT the authoritative reward source — that's data-api via FX-088) |
| `oversight/wallet_reconciliation.py` | FX-049/055 cash-invariant reconciler (data-api rewards); pages on desync (FX-074) |

### 4c. ⚠ Live-used helpers that live *inside* otherwise-legacy modules (don't delete these)

| Path | What's live |
|---|---|
| `oversight/data_collector.py` | only `_connect_db` is used by the live farmer (rest is legacy) |
| `profit/correlation.py` | `build_fill_clusters` — used by the farmer's cluster-notional guardrail |
| `oversight_agent.py` | **legacy planner**, BUT the farmer imports it for an *optional, no-op* `evaluate()` shadow hook. The live planner is `simple_oversight.py`. |

### 4d. Docs / the "share set"

| Path | Role |
|---|---|
| `README.md` | Overview |
| `ground_rules.md` | Immutable contract (v1.1) |
| `docs/HANDOFF.md` | **this file** |
| `Polymarket bot architecture v5.1.md` | Design + ops reference (content is **v6.7**; filename is legacy) |
| `Polymarket bot fixit.md` | FX-NNN issue tracker (**v1.37**) |
| `docs/STATUS_2026-06-02.md` | **Latest session record** (root cause, FX-090→093, the fill-rate-kill incident, monitoring regime) |
| `docs/STATUS_2026-05-31.md` | Prior session record |
| `docs/STATUS_2026-05-29.md` | Earlier session record |
| `docs/runbooks/live_canary_operator.md` | **Live-bot operator runbook** — alert meanings, kill response, tuning knobs, "normal-but-alarming" behaviors (§10) |
| `docs/runbooks/9_of_10_p5_p7_operator_runbook.md` | Staged bring-up + G-C/G-E gate scripts |
| `docs/runbooks/deployment_from_scratch.md` | Provision a fresh server |
| `docs/runbooks/stage_c_pull_2026-05-31.md` | The dry→live cutover procedure (already executed) |
| `CHANGELOG.md` | Version history |

### 4e. Tests + config

| Path | Role |
|---|---|
| `tests/` | **59 test files** (the real suite; **1118 pass + 2 skip**). Run: `pytest tests/ --ignore=tests/test_simulation.py` |
| `requirements.txt` | Python deps (Python 3.14; `py-clob-client-v2==1.0.0`) |
| `.env.example` | Template for the secrets file (see 4f) |
| `.gitignore` | Excludes `.env`, `*.db`, `logs/`, `market_allocations.json`, `config_overrides.json`, `.claude/`, `venv/`, diagnostics |
| `.github/workflows/test.yml` | CI test gate |

### 4f. NOT in the repo — server-only / generated (live on Helsinki)

| Path (on Helsinki) | What it is |
|---|---|
| `/etc/systemd/system/polymarket-farmer.service` | Farmer unit — `ExecStart … reward_farmer.py --mode live` |
| `/etc/systemd/system/polymarket-oversight.service` | Oversight unit — `ExecStart … simple_oversight.py --loop` |
| `…/config_overrides.json` | Hot-reloadable knobs. **Currently:** `{RF_TRIAL_BUDGET_PCT:0.75, RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC:0.01, RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS:5, RF_TARGET_QUEUE_AHEAD_USD:4000}` |
| `…/bot_history.db` | Live SQLite state/history (WAL) — source of truth for fills/unwinds/ROI/cooldowns/book_snapshots |
| `…/market_allocations.json` | The farmer↔oversight coupling file (regenerated each oversight cycle) |
| `…/.env` | **Secrets — never share.** `CLOB_API_KEY`, `CLOB_SECRET`, `CLOB_PASS_PHRASE`, `PRIVATE_KEY`, `WALLET_ADDRESS`, `FUNDER`, `DISCORD_WEBHOOK_URL` |
| `…/logs/` | Runtime logs (also via `journalctl`) |

### 4g. Legacy / ignore for understanding the live system

Old entry points & stack: `bot.py`, `main.py`, `oversight_agent.py` (see 4c caveat),
`oversight/{safety_controller,market_scorer,allocation_writer}.py`, `profit/*`
(allocator/learning/bandit/regime/sizing/efficiency/rebalance/refill), `calibration/*` (dormant models),
`simulation/*` (sim harness), `paper_client.py`, `paper_trader*.py`, `simulate.py`, `backtest.py`,
`arbitrage.py`, `unwind.py`, `order_manager.py`, `market.py`, `orders.py`, `fills.py`, `pricing.py`,
`placement.py`. Diagnostics: `dashboard.py` (Streamlit), `check_wallet.py`, `set_allowances.py`,
`revoke_allowances.py`. **The 7 root-level `test_*.py` are legacy — the real suite is `tests/`.**
`humanpending.md` is ad-hoc notes (not load-bearing).

---

## 5. Operations

- **SSH (private key required):** `ssh -i ~/.ssh/polymarket_bot_ed25519 polymarket@46.62.209.203`
  (Hetzner Helsinki `hel1` — the only region that clears Polymarket's geoblock).
- **Halt (graceful — cancels resting orders):** `sudo systemctl stop polymarket-farmer`
- **Mode switch / deploy / kill-response / tuning knobs / "normal-but-alarming" behaviors:**
  see **`docs/runbooks/live_canary_operator.md`**.
- **Watch:** `journalctl -u polymarket-farmer -u polymarket-oversight -f` (key lines: `[CYCLE_SUMMARY]`,
  `[GUARDRAIL]`, `[OVERCOMMIT_ALLOC]` (incl. `vol_excluded`/`timing_excluded`), `[SIMPLE_ALLOC]`, `[LEARN_CAPEFF]`).
- **Authoritative reward/P&L (on-chain, public, no auth):**
  `https://data-api.polymarket.com/activity?user=0xB23Bc80E6719099aeBE0c34389f05EC8C928503f&type=REWARD`
  (+ `MAKER_REBATE`, `/positions`). Rewards settle as a daily aggregate ~00:20 UTC. Trust this over
  SDK-derived numbers (see FX-088/089).
- **Safety stack (all armed):** realized-loss kill (10% wallet/24h), cash-drawdown kill (15% from peak),
  held-inventory unrealized-loss kill (FX-084, 20%), fill-rate spike kill, rapid-growth kill (FX-058/087),
  oversight-silence farmer backstop (FX-082), **kill→Discord page (FX-092)**, heartbeat→Discord stall alert
  (FX-083), wallet-desync paging (FX-049/055/074).

### Dashboard v2

A React + FastAPI dashboard now runs on the box (localhost-only, same SSH-tunnel security model as the
old Streamlit dashboard):

- **Service:** `polymarket-dashboard-v2.service`
- **Local port:** `8502`
- **Tunnel:** `ssh -i ~/.ssh/polymarket_bot_ed25519 -L 8502:127.0.0.1:8502 -N polymarket@46.62.209.203`
- **Open:** http://localhost:8502
- **Pages:** Command Center, A/B Experiment Lab, P&L, Positions, Markets, Health, Config.
- **Source:** `api/` (FastAPI) and `frontend/` (React). Build output is served from `frontend/dist/`.
- **Actions:** read-only by default; safe operator actions are behind confirmation modals (not yet wired in Phase 1).

## 6. What shipped recently (this session, the path to here)

- **FX-090** (`b8a0a95`): allocator **adverse-selection / time-to-event filter** — excludes markets within
  `RF_ALLOC_MIN_HOURS_TO_RESOLUTION` (48 h) / `RF_ALLOC_MIN_HOURS_TO_GAME_START` (12 h), enriching from
  cached CLOB `/markets/{cid}`. Fixed the "farming nothing" state (`0/5`→`5/5 on-book`).
- **FX-091** (`3891bc6`): **capital-efficiency denominator** fix — `total_capital` was $78 k garbage; now
  the time-averaged per-cycle committed capital from `capital_committed_snapshots`. capeff truthful.
- **FX-092** (`92ec34c`): **kill → Discord page** — a kill leaves the process alive-but-idle, so the
  heartbeat alert never fired (the 02:43 UTC fill-rate kill went unnoticed). Now pages once per episode.
- **FX-093** (`ed38e35`): allocator **proactive recent-volatility exclusion** — excludes candidates whose
  `book_snapshots` midpoint range > `RF_ALLOC_MAX_RECENT_VOLATILITY` (0.10) over 6 h. Catches news markets.
- **Config:** `RF_TARGET_QUEUE_AHEAD_USD: 1000→4000` (deeper queue, round-1 adverse-fill cut).
- **Docs reconciled:** architecture v6.7, fixit v1.37, this HANDOFF, `docs/STATUS_2026-06-02.md` (new),
  `docs/runbooks/live_canary_operator.md` (new). Full suite **1118 pass**.

## 7. Honest status & what remains

- **Done + proven:** correct reward/loss accounting (FX-088/089); the bot farms safely on longer-dated,
  low-volatility markets (FX-090 + FX-093 + deeper queue); all safety limbs armed + a kill now pages you;
  metrics truthful (FX-091); docs current. Tests 1118 pass.
- **NOT done / unproven (the objective):** **net profitability** (rewards > losses). Gross capital-efficiency
  is good (~2.4%/day) but net is negative (~−$53 since the FX-090 deploy) because longer-dated **news**
  markets (ships-transit, IPO, elections) still adversely fill. FX-093 attacks exactly this; **whether it
  stops the bleed is being verified** (verdict at the ~00:20 UTC daily settlement).
- **Monitoring regime (operator-directed):** continuous periodic supervisory checks; **change code/logic
  ONLY if something is BROKEN during the live run** (kill / crash / real desync / 0-farming / runaway loss) —
  net-negative-but-stable is "let it soak," not broken; any fix is plan-first (scenarios → design → test →
  deploy). The next lever if FX-093 is insufficient is a faster/pre-emptive cooldown (an operator decision).
- **Gates still open:** G-C (a real fill+dump handled correctly under load — partially met) and G-E (7 days
  clean, rewards > losses) — both require sustained live operation.

**Honest mission rating: ~6/10.** The foundation is correct, the bot is live and farming safely, monitoring
is solid — but profitable reward-farming is not yet demonstrated. **Handoff-readiness: ~6/10** (operable,
safe, documented; the objective is unproven and time-gated).
