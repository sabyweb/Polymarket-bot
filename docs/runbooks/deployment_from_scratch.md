# Deployment from Scratch — Provisioning + Hardening + Install + Lifecycle

Procedural runbook for provisioning a fresh server, hardening Ubuntu,
installing Python 3.14 + the bot's deps, transferring .env, smoke
tests, systemd unit installation, DRY soak, LIVE cutover, and lifecycle
commands (systemd, mode switching, stage promotion, emergency rollback).

Moved here on 2026-05-28 from `Polymarket bot architecture v5.1.md`
§11.4-§11.14 to keep the architecture doc focused on the system spec
(what the bot does, why it does it) rather than procedural steps
(how to deploy it). The §11.1-§11.3 "rebuild spec" stays in the
architecture doc.

**For day-to-day operations (mode/stage promotion, monitoring during
G1 7-day run, etc.):** see `docs/runbooks/9_of_10_p5_p7_operator_runbook.md`.

**For ground rules + immutable contract:** see `ground_rules.md`.

**For open issues + ship history:** see `Polymarket bot fixit.md` +
`CHANGELOG.md`.

---

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

