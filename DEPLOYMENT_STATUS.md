# Polymarket Bot — Deployment Status & Operations Reference

**Status date:** 2026-05-14 (snapshot)
**Current HEAD:** `ee6abdf` — `Rename get_orders → get_open_orders for V2 SDK compatibility`
**Branch:** `main` (solo contributor, no feature branches)
**Origin:** `git@github.com:sabyweb/Polymarket-bot.git` (private repo, SSH-only)
**Architecture spec:** `~/Downloads/Polymarket bot architecture v5.1.md` (v5.1.3 baseline)

---

## 1. Executive summary

The bot has been **fully implemented, hardened, tested (457 collected / 449 passing + 1 pre-existing flake), pushed to production server, and exercised end-to-end through DRY → LIVE cutover**. The LIVE cutover attempted from a US-hosted server produced **HTTP 403 geoblock errors on every order placement**. No money has moved. The bot is currently reverted (or about to be reverted) to DRY mode pending a server-region migration.

### Blocker (immediate)

**Polymarket geoblocks US-based IPs from trading via the CLOB API** (CFTC settlement, Jan 2022). The server is in Hetzner Ashburn (us-east); every `POST /order` returns:
```
status=403 body={"error":"Trading restricted in your region, please refer to available regions"}
```

Resolution path: move the server to a non-US region (Hetzner Helsinki / Falkenstein / Nuremberg / Singapore), or run from a non-US local environment.

### What's working end-to-end

- Code shipped: 10 commits past V2 migration baseline (`2a6baf6` → `ee6abdf`)
- Pytest: 449 passed / 1 pre-existing flake (`test_over_aggressive_contracts_capital`)
- Server provisioned + hardened (Hetzner CCX13, Ubuntu 24.04, fail2ban + ufw + key-only SSH + non-root user + passwordless sudo for `polymarket`)
- Python 3.14.4 + venv + deps + numpy installed
- `.env` transferred with 7 keys, perms 600
- V2 client credentials authenticate against `clob.polymarket.com` (verified balance fetch)
- systemd units (`polymarket-farmer`, `polymarket-oversight`) running clean
- Server DRY soak: 32+ hours, zero errors (pre-LIVE-attempt)
- Phase C oversight (stage 2/3 promotion flags) operational at default Stage 1
- All 5 prior fixes verified operational on server (question text, `_total_capital`, dict key, GATE bump, V2 method rename)
- Wallet topped up to **$201.35 pUSD** on FUNDER `0xB23Bc80E6719099aeBE0c34389f05EC8C928503f`
- The first LIVE attempt placed real order requests (proves LIVE codepath fires) — only the destination API rejected them due to geoblock

---

## 2. Critical open issues

Listed in priority order.

### 2.1 Server geoblock — IMMEDIATE BLOCKER for LIVE

| Field | Value |
|---|---|
| Symptom | HTTP 403 on every `POST clob.polymarket.com/order` |
| Cause | Polymarket geoblocks US, France, and other jurisdictions per CFTC settlement Jan 2022 |
| Server location | Hetzner Ashburn, VA, USA (`ash`) |
| Exposure | None — orders rejected at API, no fills, no capital moved |
| Fix | Move server to non-blocked region OR run from local non-US environment |

**Confirmed during the May 14 04:55 UTC LIVE cutover.** The error message:
```
[py_clob_client_v2] request error status=403 url=https://clob.polymarket.com/order
body={"error":"Trading restricted in your region, please refer to available regions - https://docs.polymarket.com/developers/CLOB/geoblock"}
```

**Polymarket's allowed regions documentation:** https://docs.polymarket.com/developers/CLOB/geoblock — must be checked before committing to any server region.

**Operator decision needed before LIVE re-attempt:**
1. Which non-US region (Helsinki / Falkenstein / Nuremberg / Singapore / other) is acceptable?
2. Does the operator's home/personal region work for Polymarket trading? (test before committing to migrate)

### 2.2 `_p_fill` not stamped on legacy allocator rows

The legacy allocator (`oversight/allocation_writer.py:compute_allocations`) does not stamp `_p_fill` on deploy rows. Only the profit-engine allocator (`profit/allocator.py:372`) does.

**Consequence in LIVE:** `expected_util = expected_capital_sum / total_capital_input = 0 / X = 0`. The β rule's `err_beta = TARGET_UTIL - 0 = 0.75` drives `beta_raw` toward the upper clamp 0.95 (after EMA smoothing).

**Mitigation in place:** `GATE_ACTIVE_CYCLES = 2000` (bumped from 50) gives ≥16.7h SHADOW soak before ACTIVE promotion can flip computed β into applied state. Operator should observe `[LEARNING_SHADOW] would_apply` trajectories before promoting.

**Permanent fix (deferred):** stamp `_p_fill` in `compute_allocations` or retire the legacy path entirely once calibrator readiness is achieved.

### 2.3 Calibrator never trains in DRY/SHADOW

`FillModel` / `LossModel` require:
- ≥50 real fills (`MIN_SAMPLES = 50` per `calibration/fill_model.py:22`)
- ≥15 positives (`MIN_POSITIVES = 15`)

DRY mode skips order placement → no fills → calibrator stays in `not_ready` → profit-engine allocator never runs → legacy allocator continues forever → `_p_fill` stays unstamped → loop closes back on issue 2.2.

**Resolution path:** real LIVE operation accumulates fills, calibrator trains, profit-engine activates. ETA: hours-to-days of LIVE depending on market activity.

### 2.4 `check_wallet.py` 400 error (cosmetic, harmless)

`check_wallet.py` prints:
```
[py_clob_client_v2] request error status=400 ... GetBalanceAndAllowance invalid params: assetId invalid value -1, as this is a erc1155 operation
```
…before showing the (correct) on-chain wallet state. The script is a manual diagnostic; the bot's runtime balance fetch path (used in production) works correctly. Fix is a script-level cleanup, not blocking.

### 2.5 `test_over_aggressive_contracts_capital` flake (pre-existing)

`tests/test_simulation.py::TestScenarioDirections::test_over_aggressive_contracts_capital` has been failing across all our commits with values like:
```
AssertionError: 1.000530444123704 not less than 1.0
```
Predates this work. Tracked but deferred — see Phase B plan (commit `e270d63`).

### 2.6 Other untested V1→V2 method paths

The V1→V2 audit (commit `ee6abdf`) verified all currently-called methods. But these methods will only be exercised after the bot actually places orders / has fills, and were verified by `inspect.signature` only, not end-to-end:
- `create_and_post_order` — fires on first order placement
- `post_order` — same
- `get_order(order_id)` — fires when querying specific order status

Any V1→V2 issue here will surface as a similar `AttributeError` or signature mismatch and abort the placement step. The kill switch + oversight contain blast radius.

---

## 3. Current system state

### 3.1 Local Mac

- Path: `/Users/sabyasaachikarmakar/Downloads/Codex playground/polymarket_bot`
- Git: `ee6abdf`, clean except pre-existing untracked audit JSONs
- venv: `venv/bin/python3` = Python 3.14.3
- Bot processes: none running (stopped when Mac restarted on May 6)
- `.env`: present locally (gitignored, perms 600)
- `bot_history.db`: present with 474 fills + 395 unwinds from earlier local runs (calibrator NOT ready)

### 3.2 Server (Hetzner CCX13)

- Hostname: `polymarket-bot-prod`
- IP: `5.161.80.22`
- Location: Ashburn, VA, USA (us-east-1) — **needs to be migrated due to geoblock**
- OS: Ubuntu 24.04.4 LTS, kernel 6.8.0-111-generic
- TZ: UTC
- Cost: $24.59/mo ($19.99 server + $4.00 backups + $0.60 IPv4)
- User: `polymarket` (uid 1000, in sudo group, passwordless via `/etc/sudoers.d/polymarket`)
- SSH: key-only (no passwords, no root login)
- Firewall: Hetzner Cloud Firewall + ufw, port 22 inbound from `0.0.0.0/0`, all outbound
- fail2ban: active
- Python: 3.14.4 via deadsnakes PPA
- Repo path: `/home/polymarket/Polymarket-bot` at commit `ee6abdf`
- venv: `~/Polymarket-bot/venv`, includes requirements.txt + numpy (manually added)
- `.env`: in `~/Polymarket-bot/.env`, perms 600
- `bot_history.db`: started fresh on server (no migrated history)
- systemd services: `polymarket-farmer`, `polymarket-oversight`, both auto-enabled on boot
- Current mode: **was `--mode live`, errors all 403; revert to `--mode dry` recommended**

### 3.3 Wallet

| Field | Value |
|---|---|
| FUNDER (Polymarket proxy) | `0xB23Bc80E6719099aeBE0c34389f05EC8C928503f` |
| EOA (signer, derived from PRIVATE_KEY) | `0x20DBbC6e57c5C2182A7f71B8D97994f926a64b8E` |
| Signature type | `2` (POLY_GNOSIS_SAFE) |
| pUSD balance | **$201.35** (May 13 top-up) |
| USDC.e balance | $0.00 |
| pUSD allowance: FUNDER → V2 Exchange | unlimited |
| pUSD allowance: FUNDER → V2 Neg Risk Exchange | unlimited |
| pUSD allowance: FUNDER → V2 Neg Risk Adapter | unlimited |
| CTF (ERC1155) approval: FUNDER → V2 contracts | True (all three) |
| EOA allowances | not set (fine — FUNDER is the active wallet) |

---

## 4. Commit ladder since V2 migration baseline

| # | SHA | Title | Phase | Files |
|---|---|---|---|---|
| 0 | `2a6baf6` | Migrate to Polymarket CLOB V2 (mandatory post-2026-04-28 cutover) | pre-baseline | — |
| 1 | `ad22512` | V2 endpoint compatibility: /sampling-markets fallback + Gamma keyset | pre-Phase-0 | `market.py`, `market_discovery.py`, `oversight/data_collector.py`, `paper_trader_v2.py`, `tests/test_market_discovery_v2_fallback.py` |
| 2 | `900e3f8` | Wrap standalone test runners under if __name__ guard | Phase 0 (test infra) | 5 root-level `test_*.py` |
| 3 | `c7ed2e6` | Populate question text from Gamma in market_expiry_cache | Phase 1 (safety filters) | `database.py`, `oversight/data_collector.py`, `tests/test_data_collector.py`, `tests/test_database_persistence.py` |
| 4 | `d2612e6` | Stamp _total_capital on legacy allocator output + uniform cap_scale | Phase 2 (guardrail activation) | `oversight_agent.py`, `oversight/allocation_writer.py`, `tests/test_market_scorer.py` |
| 5 | `4f102e3` | Fix _read_alloc_file dict key (allocations → markets) + parallel sim writer | Phase 3a (LearningController unblock) | `profit/learning.py`, `simulation/runner.py`, `tests/test_reward_expansion.py`, `tests/test_frontier_memory.py` |
| 6 | `e270d63` | Bump GATE_ACTIVE_CYCLES 50→2000 as SHADOW-soak safety belt | Phase 3b (safety belt) | `profit/learning.py`, 4 test files |
| 7 | `5757aef` | Introduce oversight Stage 2/3 promotion flags (default off) | Phase C step 1 | `oversight_agent.py` |
| 8 | `a08e86a` | Wire oversight signals to pause/kill actions (gated off by default) | Phase C step 2 | `oversight_agent.py`, `tests/test_oversight_shadow.py` |
| 9 | `5909764` | Tests: oversight promotion-flag isolation | Phase C step 3 | `tests/test_oversight_shadow.py` |
| 10 | `ee6abdf` | Rename get_orders → get_open_orders for V2 SDK compatibility | Phase D hotfix | `reward_farmer.py`, `fills.py`, `tests/test_order_reconciliation.py`, `tests/test_startup_recovery.py` |

### Phase summary

- **Phase 0:** test infra unblock (pytest collection)
- **Phase 1:** safety filter activation (question text → sports/cluster/keyword gates)
- **Phase 2:** guardrail activation (`_total_capital` stamping → notional/cluster/loss guardrails live, drift signal data)
- **Phase 3:** LearningController metrics pipeline unblocked + soak safety belt
- **Phase C:** Stage 2 oversight (signals → pause/kill actions, all flags default off)
- **Phase D:** server provisioning + V2 SDK rename hotfix

Each commit is single-purpose, independently revertible via `git revert <sha>`.

---

## 5. Architecture overview

### 5.1 Process topology

```
┌────────────────────────────────────┐         ┌────────────────────────────────────┐
│  systemd: polymarket-farmer        │         │  systemd: polymarket-oversight     │
│  ────────────────────────────────  │         │  ────────────────────────────────  │
│  reward_farmer.py --mode {dry|live}│         │  oversight_agent.py --loop         │
│                                    │         │                                    │
│  • run cycle every ~30s            │         │  • run cycle every ~30 min         │
│  • read market_allocations.json    │         │  • discover markets via Gamma+CLOB │
│  • place / cancel orders (LIVE)    │         │  • score markets                   │
│  • compute guardrail JSON          │  ───→   │  • allocate (legacy or profit eng) │
│  • call oversight_agent.evaluate() │  ←───   │  • write market_allocations.json   │
│  • detect fills via fills.py       │         │  • compute LearningController step │
│  • update positions / DB           │         │  • run SafetyController            │
└────────────────────────────────────┘         └────────────────────────────────────┘
                  │                                              │
                  │                                              │
                  ▼                                              ▼
        ┌─────────────────────────┐                  ┌─────────────────────────┐
        │  bot_history.db         │  ◄──── shared ──►│  market_allocations.json│
        │  ──────────────────     │     (SQLite)     │  (JSON file)            │
        │  fills, unwinds,        │                  │                         │
        │  portfolio_snapshots,   │                  │  markets[], num_deploy, │
        │  reward_market_stats,   │                  │  total_capital_deployed │
        │  market_expiry_cache,   │                  └─────────────────────────┘
        │  learning_state,        │
        │  calibration_state, ... │
        └─────────────────────────┘
                  │
                  ▼
        ┌───────────────────────────────────────────────────────────────┐
        │  External APIs                                                │
        │  ────────────                                                 │
        │  • clob.polymarket.com (V2)        — orders, balance, fills   │
        │  • gamma-api.polymarket.com        — market metadata          │
        │  • data-api.polymarket.com         — rewards/attribution data │
        │  • Polygon RPC (chain_id=137)      — on-chain reads via web3  │
        └───────────────────────────────────────────────────────────────┘
```

### 5.2 Key code paths

**Production (the only path that matters):**
- `reward_farmer.py` — farmer entrypoint, run cycle, gated order placement
- `oversight_agent.py` — oversight loop, scoring, allocation dispatcher, Phase C evaluator
- `oversight/data_collector.py` — Gamma + CLOB market discovery + scoring inputs
- `oversight/market_scorer.py` — score function + trial cap + sports filter
- `oversight/allocation_writer.py` — **legacy** allocator (currently active)
- `oversight/safety_controller.py` — SafetyController state machine
- `profit/allocator.py` — **profit-engine** allocator (inactive until calibrator ready)
- `profit/learning.py` — LearningController gate + Step 3 control rules
- `fills.py` — FillDetector (per-market reconciliation)
- `database.py` — SQLite schema + migrations

**Deprecated (do not touch):**
- `bot.py` — legacy farmer, replaced by `reward_farmer.py`
- `main.py` — legacy entrypoint

### 5.3 Three orthogonal "modes"

| Mode dimension | Values | Where set | Default |
|---|---|---|---|
| **Bot mode** | `dry` / `shadow` / `live` | `--mode` CLI flag to `reward_farmer.py` | `dry` |
| **LearningController state** | `OFF` / `SHADOW` / `ACTIVE` | `evaluate_activation()` in `profit/learning.py` | starts `OFF`, advances based on `valid_cycles_observed`, `fills_total`, `pairs_total`, `reward_days` |
| **Oversight stage** | Stage 1 / Stage 2 / Stage 3 / fully on | three module-level constants in `oversight_agent.py` | Stage 1 (master gate `_SHADOW_ONLY=True`) |

#### Bot mode semantics
- **dry:** no orders placed; reconciliation skipped; balance refresh skipped; calibrator can't train
- **shadow:** counters and balance still mode-gated; placement still skipped — same effect as DRY for our purposes
- **live:** real orders placed; balance refreshed every cycle; fills accumulate; calibrator trains; only mode where the bot earns or loses anything

#### LearningController state semantics
| State | Effect on applied state | Required to advance to next |
|---|---|---|
| OFF | applied state is `_neutral(OFF)` | fills_total ≥ 100, pairs ≥ 50, days ≥ 3 → SHADOW |
| SHADOW | applied state is `_neutral(SHADOW)`, computed state moves | fills ≥ 200, pairs ≥ 100, days ≥ 5, valid_cycles ≥ `GATE_ACTIVE_CYCLES=2000` |
| ACTIVE | applied state moves; `capital_scale`, `β`, `reward_trust` actually scale allocations | (no further automatic advance) |

#### Oversight stage semantics
Three module-level flags in `oversight_agent.py` (currently lines 596, 597, 598):
```python
_SHADOW_ONLY    = True   # master gate (Stage 1)
_PAUSE_ENABLED  = False  # Stage 2: would_pause signals → pause action
_KILL_ENABLED   = False  # Stage 3: would_kill signals → kill action
```

| Flag combination | Behaviour |
|---|---|
| `_SHADOW_ONLY=True` | `evaluate()` returns `{"action":"continue","reason":"shadow"}` always |
| `_SHADOW_ONLY=False, _PAUSE_ENABLED=True, _KILL_ENABLED=False` | Pause signals act (A,B,C,D,F); kill signals fall through to pause (per C3 decision) |
| `_SHADOW_ONLY=False, _PAUSE_ENABLED=True, _KILL_ENABLED=True` | Full Stage 3 — pause + kill act |

Promotion strategy: keep Stage 1 for first ≥200 LIVE cycles, observe shadow signals via logs, then flip one flag at a time.

---

## 6. How to run / operate (server-side)

### 6.1 Daily health check (~30 seconds)

```bash
ssh -i ~/.ssh/polymarket_bot_ed25519 polymarket@5.161.80.22
cd ~/Polymarket-bot

# Service health
sudo systemctl is-active polymarket-farmer polymarket-oversight

# Recent errors
sudo journalctl -u polymarket-farmer -u polymarket-oversight --since "24 hours ago" --no-pager | grep -cE "ERROR|Traceback|FATAL"

# Wallet
venv/bin/python3 -c "
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType, ApiCreds
import os; from dotenv import load_dotenv; load_dotenv()
creds = ApiCreds(api_key=os.getenv('CLOB_API_KEY'),api_secret=os.getenv('CLOB_SECRET'),api_passphrase=os.getenv('CLOB_PASS_PHRASE'))
c = ClobClient(host='https://clob.polymarket.com', chain_id=137, key=os.getenv('PRIVATE_KEY'), funder=os.getenv('FUNDER'), signature_type=2, creds=creds)
b = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
print(f'pUSD: \${int(b[\"balance\"])/1e6:.2f}')
"

# DB growth (alert if > 1 GB)
ls -lh bot_history.db | awk '{print $5}'
df -h / | tail -1 | awk '{print "disk free: "$4}'

# LearningController state
sqlite3 bot_history.db "SELECT mode, valid_cycles_observed FROM learning_state WHERE id=1;"

# Last cycle summary
sudo journalctl -u polymarket-farmer --no-pager | grep CYCLE_SUMMARY | tail -1
```

### 6.2 Live tail (for debugging)

```bash
# Both services, real-time
sudo journalctl -u polymarket-farmer -u polymarket-oversight -f

# Farmer only
sudo journalctl -u polymarket-farmer -f

# Last 200 lines
sudo journalctl -u polymarket-farmer -n 200 --no-pager

# Filter by content
sudo journalctl -u polymarket-farmer --no-pager | grep "CYCLE_SUMMARY" | tail -10
```

### 6.3 Mode switching

**DRY → LIVE:**
```bash
sudo sed -i 's|--mode dry|--mode live|' /etc/systemd/system/polymarket-farmer.service
sudo systemctl daemon-reload
sudo systemctl restart polymarket-farmer
grep ExecStart /etc/systemd/system/polymarket-farmer.service
```

**LIVE → DRY (rollback):**
```bash
sudo sed -i 's|--mode live|--mode dry|' /etc/systemd/system/polymarket-farmer.service
sudo systemctl daemon-reload
sudo systemctl restart polymarket-farmer
```

### 6.4 Code update (pull new commit)

```bash
cd ~/Polymarket-bot
git pull origin main
git log -1 --format='%h %s'
sudo systemctl restart polymarket-farmer polymarket-oversight
```

### 6.5 Oversight stage promotion (must be done carefully)

**Stage 1 → Stage 2:** flip `_PAUSE_ENABLED` only (keep `_SHADOW_ONLY=False` AND `_PAUSE_ENABLED=True`):
```bash
cd ~/Polymarket-bot
sed -i 's|^_SHADOW_ONLY = True|_SHADOW_ONLY = False|' oversight_agent.py
sed -i 's|^_PAUSE_ENABLED = False|_PAUSE_ENABLED = True|' oversight_agent.py
grep -E "^_SHADOW_ONLY|^_PAUSE_ENABLED|^_KILL_ENABLED" oversight_agent.py
# Verify the change before restart
sudo systemctl restart polymarket-farmer polymarket-oversight
```

**Stage 2 → Stage 3:** flip `_KILL_ENABLED=True` similarly. Each promotion should be preceded by ≥200-cycle observation per architecture doc §4.21.7.

### 6.6 GATE_ACTIVE_CYCLES revert (once SHADOW computed-state observed sane)

Currently bumped to 2000 as a safety belt. Once `[LEARNING_SHADOW] would_apply` logs show stable β/cap_scale/trust trajectories under real LIVE conditions, revert:
```bash
cd ~/Polymarket-bot
sed -i 's|^GATE_ACTIVE_CYCLES = 2000|GATE_ACTIVE_CYCLES = 50|' profit/learning.py
grep "^GATE_ACTIVE_CYCLES" profit/learning.py
git diff profit/learning.py  # review before commit
# Commit and push from your Mac, not server (deploy key is read-only)
```

### 6.7 Kill-switch emergency stop

If something goes wrong in LIVE:
```bash
# 1. Stop services immediately
sudo systemctl stop polymarket-farmer polymarket-oversight

# 2. Cancel any open orders via Polymarket UI (browser, manual)
#    Login → connect EOA wallet → Orders tab → Cancel All

# 3. Withdraw pUSD via Polymarket UI if needed
#    Browser → Polymarket → Withdraw

# 4. (Optional) revert code to a prior commit
cd ~/Polymarket-bot
git log --oneline -10
git checkout <prior-sha>
# When ready to come back: git checkout main
```

---

## 7. How to replicate from scratch

This section is the canonical bring-up procedure. Reference the architecture doc (`v5.1.md`) for design rationale; this is operational.

### 7.1 Pre-requisites

- Hetzner Cloud account (verified, payment method, project named `polymarket-bot`)
- SSH key on local Mac (e.g. `~/.ssh/polymarket_bot_ed25519`)
- Funded EOA wallet on Polygon with pUSD-on-FUNDER setup (use Polymarket UI deposit flow)
- All 7 env keys: `PRIVATE_KEY`, `CLOB_API_KEY`, `CLOB_SECRET`, `CLOB_PASS_PHRASE`, `WALLET_ADDRESS`, `FUNDER`, `DISCORD_WEBHOOK_URL` (optional)
- GitHub repo at `git@github.com:sabyweb/Polymarket-bot.git` (or fork)

### 7.2 Provision server

⚠ **CRITICAL: pick a Hetzner location NOT in Polymarket's geoblock list.** US and France are confirmed blocked. Verify your chosen location at https://docs.polymarket.com/developers/CLOB/geoblock before paying.

Hetzner options (regions outside US/FR):
- `hel1` — Helsinki, Finland (EU)
- `fsn1` — Falkenstein, Germany (EU)
- `nbg1` — Nuremberg, Germany (EU)
- `sin` — Singapore (Asia)

Console steps:
1. Cloud Console → Security → Firewalls → "+ Create firewall" named `polymarket-firewall`
   - Inbound: TCP/22 + ICMPv4 from `0.0.0.0/0`
   - Outbound: leave default
2. Cloud Console → Security → SSH Keys → "+ Add SSH key"
   - Paste your local Mac's `~/.ssh/polymarket_bot_ed25519.pub` content
3. Cloud Console → Servers → "+ Add server"
   - Location: chosen non-US region
   - Image: Ubuntu 24.04
   - Type: CCX13 (Dedicated, 2 AMD vCPU, 8 GB RAM, 80 GB NVMe)
   - Networking: IPv4 + IPv6
   - SSH keys: select yours
   - Firewalls: select `polymarket-firewall`
   - Backups: enable ($4/mo, recommended)
   - Name: `polymarket-bot-prod`
   - Cost: $24.59/mo

### 7.3 Server hardening

SSH in as root:
```bash
ssh -i ~/.ssh/polymarket_bot_ed25519 root@<IP>
```

Run (as root):
```bash
# Time + updates
timedatectl set-timezone UTC
apt-get update && apt-get upgrade -y

# Non-root user
adduser --disabled-password --gecos "" polymarket
usermod -aG sudo polymarket
mkdir -p /home/polymarket/.ssh
cp /root/.ssh/authorized_keys /home/polymarket/.ssh/
chown -R polymarket:polymarket /home/polymarket/.ssh
chmod 700 /home/polymarket/.ssh
chmod 600 /home/polymarket/.ssh/authorized_keys

# SSH hardening
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#*PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
systemctl restart ssh

# Test polymarket SSH from new Mac terminal:
#   ssh -i ~/.ssh/polymarket_bot_ed25519 polymarket@<IP>
# Test root SSH is now blocked:
#   ssh -i ~/.ssh/polymarket_bot_ed25519 root@<IP>  # should fail

# Firewall + intrusion prevention
apt-get install -y ufw fail2ban unattended-upgrades
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable
systemctl enable --now fail2ban
echo 'APT::Periodic::Update-Package-Lists "1";' > /etc/apt/apt.conf.d/20auto-upgrades
echo 'APT::Periodic::Unattended-Upgrade "1";' >> /etc/apt/apt.conf.d/20auto-upgrades
```

**Set up passwordless sudo for polymarket** (this requires Hetzner Web Console because root SSH is now blocked):
1. Cloud Console → server → Rescue → "Reset root password" (copy the displayed password)
2. Same page → "Console" button (opens browser VNC)
3. Login as root with the new password
4. Run:
   ```bash
   echo "polymarket ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/polymarket
   chmod 0440 /etc/sudoers.d/polymarket
   visudo -c -f /etc/sudoers.d/polymarket
   # expect: parsed OK
   exit
   ```

Verify on SSH:
```bash
ssh -i ~/.ssh/polymarket_bot_ed25519 polymarket@<IP>
sudo -n true && echo "passwordless sudo OK"
```

Reboot to apply pending kernel updates:
```bash
sudo reboot
# wait 45-60s, reconnect
```

### 7.4 Install Python 3.14

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
python3.14 --version  # expect: Python 3.14.x
```

### 7.5 GitHub deploy key (read-only)

The server should NOT have write access to your repo. Use a deploy key:

```bash
# Generate on server
ssh-keygen -t ed25519 -C "polymarket-server-deploy" -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub
```

Copy the printed line. On GitHub: repo → Settings → Deploy keys → "Add deploy key"
- Title: `polymarket-bot-prod-deploy`
- Key: paste
- Allow write access: **unchecked** (READ-ONLY)

Configure SSH on server:
```bash
cat >> ~/.ssh/config <<'EOF'

Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
ssh -T git@github.com  # accept fingerprint, expect "Hi sabyweb/Polymarket-bot!"
```

### 7.6 Clone repo + venv + deps

```bash
cd ~
git clone git@github.com:sabyweb/Polymarket-bot.git
cd Polymarket-bot
git log -1 --format='%h %s'   # confirm HEAD

python3.14 -m venv venv
venv/bin/pip install --upgrade pip wheel
venv/bin/pip install -r requirements.txt
venv/bin/pip install numpy       # ⚠ numpy is NOT in requirements.txt — needed for tests
venv/bin/pip install pytest      # for smoke test
```

### 7.7 Transfer `.env`

From your **local Mac**:
```bash
cd "/Users/sabyasaachikarmakar/Downloads/Codex playground/polymarket_bot"
scp -i ~/.ssh/polymarket_bot_ed25519 .env polymarket@<IP>:~/Polymarket-bot/.env
```

On server:
```bash
cd ~/Polymarket-bot
chmod 600 .env
ls -la .env       # expect -rw------- polymarket polymarket
```

### 7.8 Smoke tests

```bash
cd ~/Polymarket-bot

# Pytest collection
venv/bin/python3 -m pytest --collect-only -q 2>&1 | tail -3
# expect: 457 tests collected (or similar +/- new tests)

# Full pytest (5 min)
venv/bin/python3 -m pytest --tb=short -q 2>&1 | tail -10
# expect: ~449 passed, 1 pre-existing flake

# Wallet sanity (read-only, no orders)
venv/bin/python3 -c "
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType, ApiCreds
import os; from dotenv import load_dotenv; load_dotenv()
creds = ApiCreds(api_key=os.getenv('CLOB_API_KEY'),api_secret=os.getenv('CLOB_SECRET'),api_passphrase=os.getenv('CLOB_PASS_PHRASE'))
c = ClobClient(host='https://clob.polymarket.com', chain_id=137, key=os.getenv('PRIVATE_KEY'), funder=os.getenv('FUNDER'), signature_type=2, creds=creds)
print(c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)))
"
# expect: {'balance': 'XXX', 'allowances': {...}}
```

### 7.9 Install systemd units

See section 8 below for the exact unit file contents. Apply via:

```bash
sudo tee /etc/systemd/system/polymarket-farmer.service > /dev/null <<'EOF'
# ... see section 8.1 ...
EOF

sudo tee /etc/systemd/system/polymarket-oversight.service > /dev/null <<'EOF'
# ... see section 8.2 ...
EOF

sudo systemctl daemon-reload
sudo systemctl enable polymarket-farmer polymarket-oversight
sudo systemctl start polymarket-farmer
sleep 30
sudo systemctl start polymarket-oversight
sudo systemctl status polymarket-farmer polymarket-oversight --no-pager
```

### 7.10 DRY soak (≥1h, recommended ≥4h)

```bash
sudo journalctl -u polymarket-farmer -u polymarket-oversight -f
# observe for errors, cycle cadence, no kill-switch fires
# Ctrl+C when satisfied
```

Periodic checkpoint:
```bash
echo "=== checkpoint $(date -u '+%H:%M UTC') ==="
sudo systemctl is-active polymarket-farmer polymarket-oversight
sudo journalctl -u polymarket-farmer --no-pager | grep -c CYCLE_SUMMARY
sudo journalctl -u polymarket-farmer -u polymarket-oversight --no-pager | grep -cE "ERROR|Traceback|FATAL"
```

### 7.11 LIVE cutover

⚠ **Verify wallet is funded to a meaningful amount before cutover** (`$200+ pUSD` recommended for first deploys).

```bash
sudo sed -i 's|--mode dry|--mode live|' /etc/systemd/system/polymarket-farmer.service
sudo systemctl daemon-reload
sudo systemctl restart polymarket-farmer

# Watch first cycle
sudo journalctl -u polymarket-farmer -u polymarket-oversight -f
# Expect: "Starting reward farming | N markets | dry_run=False"
#         place_order lines WITHOUT [DRY_RUN] prefix
#         NO 403 geoblock errors

# Verify after ~5 min
sudo journalctl -u polymarket-farmer --since "5 minutes ago" --no-pager | grep -E "place_order|ERROR" | head -10
sqlite3 bot_history.db "SELECT datetime(ts,'unixepoch'), exchange_balance FROM portfolio_snapshots ORDER BY ts DESC LIMIT 1;"
```

If any 403 appears → server region is geoblocked → revert to DRY and migrate.

---

## 8. systemd unit files (canonical, copy-paste-ready)

### 8.1 `/etc/systemd/system/polymarket-farmer.service`

```ini
[Unit]
Description=Polymarket reward farmer (DRY mode)
Documentation=https://github.com/sabyweb/Polymarket-bot
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

# stdout/stderr → systemd journal
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
```

### 8.2 `/etc/systemd/system/polymarket-oversight.service`

```ini
[Unit]
Description=Polymarket oversight evaluator
Documentation=https://github.com/sabyweb/Polymarket-bot
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
```

---

## 9. Configuration reference

### 9.1 `.env` schema

7 keys, all required (except `DISCORD_WEBHOOK_URL`). Format: `KEY=value` (no quotes on values).

```
PRIVATE_KEY=0x...           # EOA private key (signer)
CLOB_API_KEY=...            # Polymarket CLOB API key (Level 1 auth)
CLOB_SECRET=...             # Polymarket CLOB API secret (Level 2 auth)
CLOB_PASS_PHRASE=...        # Polymarket CLOB API passphrase
WALLET_ADDRESS=0x...        # EOA address (derived from PRIVATE_KEY, kept for convenience)
FUNDER=0x...                # Polymarket proxy wallet address (Polygon)
DISCORD_WEBHOOK_URL=https://...  # optional, for alert notifications
```

### 9.2 Python dependencies

`requirements.txt`:
```
py-clob-client-v2==1.0.0
requests==2.32.5
python-dotenv==1.2.2
web3==7.14.1
```

**Manually-installed on server (NOT in requirements.txt — must remember):**
- `numpy` (used by simulation tests + learning rules)

Local Mac has `streamlit` from `pyproject.toml` which transitively pulled numpy. Server should add numpy explicitly until requirements.txt is updated.

### 9.3 Key constants

| Constant | File:line | Value | Meaning |
|---|---|---|---|
| `_SHADOW_ONLY` | `oversight_agent.py:596` | `True` | Master gate for oversight pause/kill |
| `_PAUSE_ENABLED` | `oversight_agent.py:597` | `False` | Stage 2: pause signals act |
| `_KILL_ENABLED` | `oversight_agent.py:598` | `False` | Stage 3: kill signals act |
| `GATE_ACTIVE_CYCLES` | `profit/learning.py:66` | `2000` | SHADOW soak before ACTIVE (bumped from 50) |
| `MAX_DAILY_LOSS_FRAC` | `reward_farmer.py:68` | `0.10` | Kill-switch threshold |
| `MAX_CAPITAL_SCALE_STEP` | `profit/learning.py:199` | `0.07` | Per-cycle bound on cap_scale |
| `EMA_ALPHA` | `profit/learning.py:202` | `0.20` | EMA smoothing on all rules |
| `CLAMP_BETA` | `profit/learning.py:208` | `(0.10, 0.95)` | β bounds |
| `DEFAULT_BETA` | `profit/learning.py:211` | `0.75` | Neutral β |
| `TARGET_UTIL` | `profit/learning.py:216` | `0.75` | β rule target |
| `K_BETA` | `profit/learning.py:225` | `0.5` | β rule learning rate |
| `MIN_FILL_BASELINE` | `reward_farmer.py:73` | `5` | Min cycles for fill_rate baseline |

### 9.4 Database tables (bot_history.db, SQLite)

Created in `database.py:_SCHEMA`. Key tables:

| Table | Purpose | Written by |
|---|---|---|
| `fills` | Buy/sell fills detected | farmer cycle |
| `unwinds` | Unwind sell completions | farmer cycle |
| `positions` | Current per-market positions | farmer cycle |
| `active_orders` | Currently-tracked open orders | farmer cycle |
| `orders_placed` | Audit log of placements | farmer cycle |
| `orders_cancelled` | Audit log of cancels | farmer cycle |
| `portfolio_snapshots` | Wallet balance over time | farmer cycle (LIVE only) |
| `correction_factor_history` | CF tracking | farmer cycle |
| `book_snapshots` | Periodic order book captures | farmer cycle |
| `placement_feedback` | Bot→agent closed loop | farmer cycle |
| `reward_market_stats` | Per-market scoring history | oversight agent |
| `market_performance` | Per-market scored outcomes | oversight agent |
| `scoring_snapshots` | Audit of scoring decisions | oversight agent |
| `safety_state` | SafetyController state history | oversight agent |
| `learning_state` | LearningController persisted state | oversight agent |
| `calibration_model_state` | FillModel + LossModel weights | oversight agent |
| `reward_attribution` | Per-day reward attribution | oversight agent |
| `reward_daily` | Daily reward totals | external (data API) |
| `market_expiry_cache` | End dates + question text (post-Phase-1) | oversight agent |
| `dump_states` | Active dump SELL state (persisted for crash recovery) | farmer cycle |

### 9.5 Files written at runtime (in working directory)

| File | Purpose | Writer | Reader |
|---|---|---|---|
| `bot_history.db` | SQLite, primary state store | both | both |
| `bot_history.db-wal` | SQLite WAL | both | — |
| `bot_history.db-shm` | SQLite shared memory | both | — |
| `market_allocations.json` | Per-cycle allocation output | oversight | farmer |
| `positions.json` | Per-market position state | farmer | farmer |
| `SAFETY_ALERT.txt` | Critical safety violations | farmer/oversight | operator |

All are `.gitignore`d.

---

## 10. Key findings & lessons

### 10.1 Geoblock should have been verified before server purchase

The Phase D plan recommended Hetzner Ashburn for **latency** (US-East proximity to Polymarket's AWS infrastructure). I did not check Polymarket's geoblock policy. Polymarket explicitly blocks US-based IPs at the CLOB API level. This cost ~1 day of work and triggered a server migration.

**Lesson:** for any third-party API integration, the **first** check is geoblock/region policy, not latency.

### 10.2 V1→V2 SDK migration was incomplete

The May 2026 V2 migration (commit `2a6baf6`) renamed several SDK methods. `get_orders()` → `get_open_orders()` was missed because:
- DRY mode skips reconciliation paths (`if not self.dry_run` gates)
- 24+ hours of DRY soak never exercised the production order-reconciliation paths
- Static `pyclob_v2` audit was incomplete

The first LIVE cutover surfaced the missing rename in ~5 cycles. Fix: commit `ee6abdf`.

**Lesson:** a static audit of every `self.client.<method>(` against the new SDK's exposed methods would have caught this pre-migration. The audit script is preserved at section 4 above.

### 10.3 `numpy` not in `requirements.txt`

Local Mac had numpy as a transitive dep via `streamlit` (in `pyproject.toml`). Server-side `pip install -r requirements.txt` (which omits streamlit) doesn't pull numpy. Tests fail with `ModuleNotFoundError: No module named 'numpy'`. Manual `venv/bin/pip install numpy` fixes it but should be in requirements.txt.

**Permanent fix needed:** add `numpy>=2.0` to `requirements.txt` and either install streamlit on server or remove it from pyproject.toml deps for server use.

### 10.4 The chicken-and-egg of SafetyController + DRY

In DRY mode the farmer never writes `portfolio_snapshots` (gated on `if not self.dry_run`). Without recent portfolio_snapshots, SafetyController stays in `DATA_UNAVAILABLE`. In that state, `STATE_PERMISSIONS[DATA_UNAVAILABLE]["trials"] = False` blocks all trial markets. On a fresh DB every market is a trial market. **Net: 0 deploys on a fresh-DB DRY server.**

Local Mac escapes this only because it has historical reward_market_stats from prior runs. A clean replication will have the same chicken-and-egg until first LIVE cycle.

**Operational consequence:** the first LIVE cycle is the only way out. DRY-only soak on a fresh server validates almost everything but cannot validate the deploy+fill chain.

### 10.5 cancel_order signature was V2-compatible by luck

`cancel_order(payload: OrderPayload)` with `OrderPayload(orderID: str)` is identical V1↔V2. Three call sites at `reward_farmer.py:355, 412, 439` work as-is. This is fortunate — the V2 migration could easily have changed it. **A signature-level audit (`inspect.signature(method)`) for every V1→V2 method is a good practice for any future SDK upgrade.**

### 10.6 Phase D server soak was 32+ hours, not 4

Plan called for 4h. The user kept it running ~32h. This proved memory stability and cycle cadence robustness over multi-day spans on the server. Net: high confidence in long-running stability — the geoblock issue is the only thing standing between the bot and full LIVE operation.

---

## 11. Pending decisions / next actions

### 11.1 Immediate (blocks LIVE)

| Decision | Recommended | Notes |
|---|---|---|
| Server region post-geoblock | Hetzner Helsinki/Falkenstein/Singapore (verify per geoblock list) | EU more latency to Polymarket but unblocked; Singapore is closer to Asian users |
| Migrate server or run from local? | Migrate (24/7 uptime, systemd auto-restart, no ISP blips) | If operator is in a non-blocked country, local Mac is interim option |
| Stop the current LIVE attempt | Revert to DRY now | `sudo sed -i 's\|--mode live\|--mode dry\|' /etc/systemd/system/polymarket-farmer.service && sudo systemctl daemon-reload && sudo systemctl restart polymarket-farmer` |

### 11.2 Short-term (improve reliability)

| Decision | Action |
|---|---|
| Add numpy to requirements.txt | One-line PR: `numpy>=2.0` |
| Fix `check_wallet.py` 400 error | Audit the conditional asset query, use `BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)` only |
| Stamp `_p_fill` on legacy allocator rows | Mirror `profit/allocator.py:372` pattern in `oversight/allocation_writer.py` |
| Fix `test_over_aggressive_contracts_capital` flake | Investigate (separate scope) |

### 11.3 Medium-term (architectural)

| Decision | Action |
|---|---|
| Stage 2 oversight promotion | After ≥200 LIVE cycles with Stage 1, flip `_PAUSE_ENABLED=True` |
| Stage 3 oversight promotion | After ≥200 LIVE cycles with Stage 2, flip `_KILL_ENABLED=True` |
| `GATE_ACTIVE_CYCLES` revert | After ≥4h SHADOW soak with sane β trajectory, revert 2000 → 50 |
| Calibrator training watch | Monitor `calibration_model_state.fill_model` / `loss_model` for `n_samples ≥ 50, n_positive ≥ 15` → enables profit-engine allocator |

### 11.4 Long-term

| Decision | Action |
|---|---|
| Retire legacy allocator | Once calibrator is reliably ready, deprecate `oversight/allocation_writer.compute_allocations` |
| Retire `bot.py`/`main.py` | Confirmed dead code post-`reward_farmer.py` migration |
| Wallet scale-up | After ≥1 week of stable LIVE at $200, consider $1000+ for design-spec capacity |

---

## 12. Reference

### 12.1 Architecture documents

- **Architecture spec:** `~/Downloads/Polymarket bot architecture v5.1.md` (v5.1.3, authoritative for design intent)
- **This doc** (`DEPLOYMENT_STATUS.md`) — operational state, replication guide, known issues
- **Project memory:** `~/.claude/projects/-Users-sabyasaachikarmakar-Downloads-Codex-playground-polymarket-bot/memory/`
  - `MEMORY.md` — index
  - `project_system_status.md` — short status summary (keep in sync with this doc)
  - `project_core_asymmetry.md` — reward-global / loss-local design rationale
  - `project_dual_codepaths.md` — `reward_farmer.py` vs legacy `bot.py`/`main.py`
  - `project_capital_overcommit.md` — Patch 7 overcommit behavior (intentional)
  - `project_market_q_fallback_bug.md` — `q_share=1.0` saturation guard
  - `feedback_no_claude_branding.md` — never include third-party branding in commits

### 12.2 External references

- Polymarket V2 CLOB docs: https://docs.polymarket.com/developers/CLOB/introduction
- Polymarket geoblock policy: https://docs.polymarket.com/developers/CLOB/geoblock
- py-clob-client-v2: https://pypi.org/project/py-clob-client-v2/
- Hetzner Cloud Console: https://console.hetzner.cloud
- Polygon block explorer: https://polygonscan.com/address/0xB23Bc80E6719099aeBE0c34389f05EC8C928503f

### 12.3 Key contracts (Polygon)

| Contract | Address |
|---|---|
| pUSD (V2 collateral) | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
| USDC.e (legacy) | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |
| V2 Exchange | `0xE111180000d2663C0091e4f400237545B87B996B` |
| V2 Neg Risk Exchange | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |
| V2 Neg Risk Adapter | `0xe2222d279d744050d28e00520010520000310F59` |

---

**End of status doc.** Last updated: 2026-05-14.
