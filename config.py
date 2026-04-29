"""
Configuration for the Polymarket market-making bot.

All tunable parameters live here. Sensitive credentials are loaded
from environment variables via a .env file.
"""

import json
import os
import threading
from dotenv import load_dotenv

load_dotenv()

# ── Dry Run Mode ──────────────────────────────────────────────────────────────
DRY_RUN: bool = False  # Set to False when ready to trade with real money

# ── API Credentials ───────────────────────────────────────────────────────────
PRIVATE_KEY: str | None = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS: str | None = os.getenv("WALLET_ADDRESS")
CLOB_API_KEY: str | None = os.getenv("CLOB_API_KEY")
CLOB_SECRET: str | None = os.getenv("CLOB_SECRET")
CLOB_PASS_PHRASE: str | None = os.getenv("CLOB_PASS_PHRASE")
FUNDER: str | None = os.getenv("FUNDER")
SIGNATURE_TYPE: int = 2  # 2 = POLY_GNOSIS_SAFE for Polymarket proxy wallet

# Builder code (bytes32, public) for builder-fee attribution. Pasted from
# https://polymarket.com/settings?tab=builder. The V2 SDK auto-injects
# this on every order via BuilderConfig (see ClobClient construction
# sites). Set to "" to disable builder attribution.
BUILDER_CODE: str = "0x3669771781fd877ef7e97e494c46157aaeb863e6c60c417441d6e0c17d66ff6f"

HOST: str = "https://clob.polymarket.com"
CHAIN_ID: int = 137  # Polygon mainnet

# ── External APIs ─────────────────────────────────────────────────────────────
GAMMA_API: str = "https://gamma-api.polymarket.com"

# ── Market Selection ──────────────────────────────────────────────────────────
MAX_MARKETS: int = 5          # Trade up to 5 markets at once
MIN_SCORE_THRESHOLD: int = 60 # Minimum score (out of 100) to trade a market
HYSTERESIS_SCORE_MARGIN: int = 10  # New market must outscore weakest by this much to swap in
MARKET_REFRESH_SECS: int = 1800  # Re-score and refresh markets every 30 min

# ── Scoring Weights (must sum to 100) ─────────────────────────────────────────
# Optimised for reward EFFICIENCY, not raw pool size.
# Reward efficiency (estimated $/day we capture per $ deployed) is the
# primary driver.  Competition and fill safety prevent us from entering
# crowded or toxic markets.  Liquidity ensures we can unwind.
# Price balance REMOVED — skewed markets often have less competition and
# are more capital-efficient for reward capture.
# Expiry is NOT scored — it is only a hygiene filter (≥ 12 hours).
WEIGHT_REWARD_EFFICIENCY: int = 30  # Estimated reward per $ of capital deployed
WEIGHT_COMPETITION: int = 15        # Capture rate (our share of pool)
WEIGHT_FILL_SAFETY: int = 25        # Lower volume = fewer fills = less adverse selection ← KEY
WEIGHT_UNWIND_ABILITY: int = 10     # Bid depth — can we get out?
WEIGHT_DAILY_RATE: int = 10         # Raw pool size (tiebreaker, not primary)
WEIGHT_SPREAD: int = 10             # Wider reward window = easier to stay inside

# ── Hygiene Filter Thresholds ─────────────────────────────────────────────────
MIN_DAYS_TO_EXPIRY: float = 0.5   # Skip markets expiring within 12 hours
MIN_YES_PRICE: float = 0.05      # Skip if Yes price below 5c
MAX_YES_PRICE: float = 0.95      # Skip if Yes price above 95c
MIN_DAILY_RATE: float = 5.0      # Skip if daily reward rate below $5
MIN_LIQUIDITY: int = 1000        # Skip if liquidity below $1000
MIN_SPREAD_ALLOWED: float = 0.01 # Skip if max spread below 1c
MAX_VOLUME_TO_REWARD_RATIO: float = 5000.0  # Skip extremely fill-heavy markets (soft filter; scoring handles the rest)

# ── Order Management ──────────────────────────────────────────────────────────
ORDER_SIZE: int = 150        # Reward farming: smaller fills = less trapped capital
MAX_ORDER_BUDGET: int = 750  # Hard cap — allows sports markets with high min_size
CHEAP_TOKEN_THRESHOLD: float = 0.25  # Tokens under 25c get reduced order size
CHEAP_TOKEN_SCALE: float = 0.50     # Scale order size to 50% for cheap tokens
MAX_ORDER_SIZE: int = MAX_ORDER_BUDGET  # Alias used by market.py hygiene check
ORDER_REFRESH_SECS: int = 30 # Cancel and replace orders every 30 seconds

# ── Orderbook Safety ─────────────────────────────────────────────────────────
# Maximum spread between best bid and best ask before we consider the book
# too sparse to trust. Markets with wider spreads are skipped for that cycle.
MAX_ORDERBOOK_SPREAD: float = 0.10  # 10c — wider than this is too sparse


# ── Pricing ─────────────────────────────────────────────────────────────────
# Co-best strategy: join at best bid/ask. Queue priority shields us.
MIN_PRICE_DRIFT_TICKS: int = 2  # Only cancel+replace when price drifts by 2+ ticks
                                 # Keeps orders alive through small oscillations → more reward time

# ── Inventory Skew ──────────────────────────────────────────────────────────
# When holding inventory on one side, skew quotes to unwind naturally:
# tighten the ask (sell faster) and widen the bid (buy less).
INVENTORY_SKEW_ENABLED: bool = True
INVENTORY_SKEW_TICKS: int = 2    # Ticks to shift quotes per skew step
INVENTORY_SKEW_THRESHOLD: float = 50.0  # Start skewing above $50 inventory

# ── Post-Fill Cooldown ───────────────────────────────────────────────────────
# After a BUY fills, widen quotes on that side to avoid adverse selection.
# Fast fills usually mean informed flow is moving through our price.
POST_FILL_COOLDOWN_SECS: int = 90      # Widen for 90s after fill (~3 cycles)
POST_FILL_WIDEN_TICKS: int = 3         # Extra ticks to push bid away on filled side

# ── Order Zone Thresholds ─────────────────────────────────────────────────────
DANGER_ZONE_CENTS: float = 0.01   # Cancel if order within 1c of best price (wider = fewer instant fills)
DEAD_ZONE_BUFFER: float = 0.001   # Buffer beyond max spread for dead zone

# ── Position Limits ───────────────────────────────────────────────────────────
MAX_POSITION_USD: int = 200     # Stop quoting early — trapped capital earns no rewards
RESUME_POSITION_USD: int = 125  # Resume quoting when position falls below $125

# ── Alert Thresholds ──────────────────────────────────────────────────────────
MAX_ORDER_FAILURES: int = 3  # Alert after this many consecutive order failures

# ── Unwind Settings ─────────────────────────────────────────────────────────
# Position-based: reconcile_unwinds() checks total position vs covered
# unwind orders each cycle. No retry queue needed.
MIN_UNWIND_SHARES: float = 1.0   # Ignore positions below this many shares

# ── Sell Price Decay ────────────────────────────────────────────────────────
# Sell orders start at acquisition cost (VWAP). To avoid holding depreciating
# inventory forever, the sell price decays by 1 tick per interval.
# With 300s interval: 1c drop every 5 min = 12c/hour normal.
UNWIND_DECAY_INTERVAL_SECS: int = 300   # Lower sell by 1 tick every 5 min
UNWIND_DECAY_TICKS: int = 1             # Ticks to drop per interval
# Graduated acceleration: instead of a single 5x panic multiplier,
# use tiered acceleration that increases with loss severity.
# Tier 1: 5-10% loss → 2x decay (2c/5min = 24c/hr)
# Tier 2: 10-15% loss → 3x decay (3c/5min = 36c/hr)
# Tier 3: >15% loss → 4x decay (4c/5min = 48c/hr) — still no panic
UNWIND_ACCEL_TIERS: list = [
    (0.03, 2),   # 3% loss → 2x (trigger earlier for reward farming)
    (0.06, 3),   # 6% loss → 3x
    (0.10, 4),   # 10% loss → 4x (max — no 5x panic)
]
MIN_SELL_PRICE: float = 0.01            # Never sell below 1 cent

# ── Reward-Offset Unwind Budget ───────────────────────────────────────────
# Decay is bounded: never lose more than this fraction of rewards earned
# while holding the position. E.g., 0.50 = allow up to 50% of earned rewards
# as unwind loss. Ensures every position is NET profitable after rewards.
REWARD_LOSS_BUDGET_PCT: float = 0.50

# ── Position Age Acceleration ────────────────────────────────────────────
# Capital trapped in a position for >24h earns zero rewards and blocks
# deployment elsewhere. Accelerate decay based on age regardless of loss.
UNWIND_AGE_ACCEL_HOURS: float = 24.0   # Start age-based acceleration after 24h
UNWIND_AGE_ACCEL_TICKS: int = 2        # Extra ticks per interval after age threshold
UNWIND_AGE_MAX_HOURS: float = 48.0     # After 48h: add 4 extra ticks (hard push)
UNWIND_AGE_MAX_TICKS: int = 4          # Extra ticks after max age

# ── Stop-Loss ───────────────────────────────────────────────────────────────
# If unrealized loss on a position exceeds this threshold, immediately
# sell at the current market bid to prevent further damage.
STOP_LOSS_PCT: float = 0.15        # 15% unrealized loss → dump at market (reward-first: cut early)
MIN_STOP_LOSS_USD: float = 40.0    # AND absolute loss must exceed $40 (tighter for smaller positions)
STOP_LOSS_MIN_PRICE: float = 0.20  # Skip stop-loss on tokens under 20c (let decay handle)

# ── Market Depth Filter ─────────────────────────────────────────────────────
# Avoid markets where the order book is too thin to unwind. If top-5 bid
# levels have less than this in total dollar volume, the bot can accumulate
# inventory it cannot sell.
MIN_BID_DEPTH_USD: float = 500.0   # Minimum $500 on the bid side to accept

# ── M1: EMA Fair Value ────────────────────────────────────────────────────
# Track exponential moving average of midpoint across cycles.
# Used as stable midpoint reference for skew and zone checks instead of
# raw (best_bid + best_ask)/2, which is noisy.
EMA_HALF_LIFE_CYCLES: int = 20     # Half-life in cycles (~10 min at 30s cycles)
                                    # alpha = 2 / (N + 1) ≈ 0.095

# ── M2: Dynamic Spread (Volatility-Based) ────────────────────────────────
# In calm markets, co-best is fine. In volatile markets, joining at best
# bid/ask = instant adverse selection.  Widen spread proportional to
# recent volatility.
VOL_WINDOW_CYCLES: int = 30        # Look-back for vol measurement (~15 min)
VOL_SPREAD_MULTIPLIER: float = 2.0 # Spread widen = vol × this multiplier
VOL_WIDEN_MAX_TICKS: int = 3       # Cap: never widen more than 3 ticks from co-best

# ── M4: Dynamic Order Sizing ────────────────────────────────────────────
# Size orders based on market conditions instead of a flat ORDER_SIZE.
# High-reward, stable, deep markets get larger orders.
# Volatile, thin, low-reward markets get smaller orders.
# The result is clamped to [DYNAMIC_SIZE_MIN, DYNAMIC_SIZE_MAX].
DYNAMIC_SIZING_ENABLED: bool = True
DYNAMIC_SIZE_MIN: int = 100          # Floor — must meet min_size for reward eligibility
DYNAMIC_SIZE_MAX: int = 250          # Cap per-order budget — limits fill damage
# Weights for the sizing score (0-1 each, then combined):
# reward_eff: higher reward per $ deployed → size up
# stability: low volatility → size up
# depth: deep bid book → size up (can unwind)
# spread: wide max_spread → size up (easier to stay in window)
SIZE_WEIGHT_REWARD: float = 0.35
SIZE_WEIGHT_STABILITY: float = 0.30
SIZE_WEIGHT_DEPTH: float = 0.20
SIZE_WEIGHT_SPREAD: float = 0.15

# ── M5: Book Imbalance Guard ─────────────────────────────────────────────
# If one side of the order book is much thinner than the other, informed
# traders are about to push through our quote on the thin side.
# Widen our quote on the thin side to avoid adverse selection.
BOOK_IMBALANCE_THRESHOLD: float = 3.0  # Ratio (e.g. 3:1) to trigger guard
BOOK_IMBALANCE_WIDEN_TICKS: int = 1    # Extra ticks on thin side

# ── M6: Merge Arbitrage Execution ────────────────────────────────────────
# When YES_ask + NO_ask < $1, buy both sides and merge for guaranteed
# profit.  Conservative sizing — arb is opportunistic, not primary.
ARB_ENABLED: bool = True
ARB_MIN_PROFIT_PCT: float = 0.005  # 0.5% minimum profit to execute
ARB_MAX_PAIRS: int = 50            # Max pairs per arb trade
ARB_MAX_BUDGET_USD: float = 100.0  # Max capital per arb trade
ARB_COOLDOWN_SECS: int = 120       # Per-market cooldown between arb attempts

# ── Reward Tracking ─────────────────────────────────────────────────────────
# Log earned rewards periodically for profitability visibility.
REWARD_LOG_INTERVAL_SECS: int = 3600   # Query and log rewards every hour

# ── Heartbeat ────────────────────────────────────────────────────────────────
# Alert if no successful cycle completes within this many seconds.
# Catches silent failures (empty API responses, 0 eligible markets, etc.)
HEARTBEAT_TIMEOUT_SECS: int = 300  # 5 minutes

# ── Discord Notifications ────────────────────────────────────────────────────
# Create a webhook in your Discord server:
#   Server Settings -> Integrations -> Webhooks -> New Webhook
# Paste the URL into your .env file as DISCORD_WEBHOOK_URL
DISCORD_WEBHOOK_URL: str | None = os.getenv("DISCORD_WEBHOOK_URL")


# ── Reward Farmer Parameters ───────────────��────────────────────────────────
# All parameters for reward_farmer.py. Hot-reloadable via config_overrides.json.
RF_SHARES_PER_SIDE: int = 50               # Default order size per side
RF_PLACEMENT_TICKS_INSIDE: int = 1          # Ticks inside from midpoint edge
RF_MIN_DAILY_RATE: float = 10.0             # Minimum reward rate ($/day) to consider market
RF_MAX_LIQUIDITY: int = 5000                # Skip markets with on-book depth above this
RF_MAX_COST_PER_MARKET: float = 50.0        # Max initial position cost per market
RF_MAX_MARKETS: int = 60                    # Maximum markets in portfolio (exchange is the capital gate)
RF_MAX_TRIAL_MARKETS: int = 50              # Max trial (confidence=low, zero fills) markets per cycle, sorted by daily_rate
RF_NEW_MARKET_Q_SHARE_PRIOR: float = 0.10   # Prior q_share for cold-start markets (0 on_book, 0 samples); escapes cold-start trap
RF_POISONED_Q_SHARE_THRESHOLD: float = 0.5  # Cumulative q_share above this triggers fallthrough to prior (defends against legacy max(market_q, our_q) saturation; see memory: project_market_q_fallback_bug.md)
RF_MAX_TOTAL_EXPOSURE: float = 1500.0       # Total USD at risk limit
RF_CYCLE_SECS: int = 30                     # Main loop cycle frequency
RF_BATCH_SIZE: int = 10                     # Markets processed per cycle (was 5, increased for faster coverage)
RF_MARKET_REFRESH_SECS: int = 1800          # Background market refresh interval
RF_DUMP_AGGRESSIVE_MINS: float = 5.0        # Duration of aggressive dump decay phase
RF_DUMP_PASSIVE_REPRICE_MINS: float = 5.0   # Reprice interval in passive dump mode
RF_DUMP_ABANDON_MINS: float = 30.0          # Hard timeout: give up on dump
RF_DUMP_EXIT_DEPTH_BUFFER: float = 0.02     # Max price buffer for exit depth check
RF_UNKNOWN_RETRY_THRESHOLD: int = 2         # Retries before clearing UNKNOWN status order (was 5, reduced)
RF_DUMP_MAX_FAILURES: int = 3               # Dump failures before blocking placement
RF_MAX_BOOK_SPREAD: float = 0.15            # Skip if merged book spread exceeds this
RF_ALLOCATION_TTL_HOURS: float = 2.0        # Max age of oversight agent allocations
RF_FILL_BREAKER_WINDOW: int = 180            # Fill-rate breaker window (seconds)
RF_FILL_BREAKER_THRESHOLD: int = 3           # Total fills (both sides) to trigger block
RF_FILL_BREAKER_SIDE_THRESHOLD: int = 2      # Same-side fills to trigger block
RF_SPORTS_BLOCK_HOURS: float = 4.0           # Block sports markets expiring within this many hours
RF_GAME_BLOCK_HOURS: float = 1.0             # Block sports markets within N hours of game_start_time (pre-kickoff + in-play); 0 disables
RF_BOOK_CACHE_TTL: int = 180                 # Max age (seconds) for MarketState.cached_book used by Q-score sampling in record_cycle; 0 disables
RF_ORDER_STALE_CHECK_SECS: int = 300         # Force-check orders still in open_ids after this many seconds

# ── Sports Keywords ──────────────────────────────────────────────────────────
# Unified list used by agent (market_scorer), bot (order_lifecycle), and
# pre-cycle sweep (reward_farmer). Define once, import everywhere.
# Sports markets near expiry have extreme adverse selection risk from
# informed bettors watching the event.
SPORTS_KEYWORDS: tuple = (
    " vs ", " vs. ",
    # Soccer
    "premier league", "serie a", "la liga", "bundesliga",
    "champions league", "europa league", "mls ",
    # US Sports — word-boundary padded to avoid substring matches
    # ("nfl" matched "conflict", "ipl" matched "diplomatic", "odi" matched "Coding")
    " nba", " nfl", " mlb", " nhl", " wnba",
    # Combat
    " ufc", "boxing",
    # Tennis
    " atp", " wta", "grand slam", "wimbledon", "us open tennis",
    "french open", "australian open",
    # Racing
    "grand prix", "formula 1", " f1 ", "nascar", "indycar",
    # Cricket — word-boundary padded to avoid substring matches
    " ipl", "cricket", " t20", " odi ",
    # Golf
    "masters", " pga", "ryder cup",
    # College
    "march madness", "ncaa",
    # Esports
    "esports", "league of legends", "dota",
)

# ── Credential Validation ───────────────────────────────────��───────────────
def validate_credentials() -> None:
    """Fail fast if any required credential is missing or empty.

    Called at startup before any API connections are attempted.
    Raises SystemExit with a clear message telling the user exactly
    what is missing.
    """
    required = {
        "PRIVATE_KEY": PRIVATE_KEY,
        "CLOB_API_KEY": CLOB_API_KEY,
        "CLOB_SECRET": CLOB_SECRET,
        "CLOB_PASS_PHRASE": CLOB_PASS_PHRASE,
    }
    missing = [name for name, val in required.items() if not val]
    if missing:
        raise SystemExit(
            f"FATAL: Missing required credentials in .env: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in all fields."
        )


# ── O2: Hot-Reloadable Config ──────────────────────────────────────────────
# BotConfig reads config_overrides.json to override any parameter at runtime.
# Usage: cfg("ORDER_SIZE") returns the live value (overridden or default).
# To change: edit config_overrides.json — the bot picks it up next cycle.
# Never reloadable: credentials, host, chain_id, signature_type.

OVERRIDES_FILE: str = os.path.join(os.path.dirname(__file__), "config_overrides.json")

# Parameters that are NEVER hot-reloadable (security-sensitive or structural)
_IMMUTABLE: frozenset = frozenset({
    "PRIVATE_KEY", "WALLET_ADDRESS", "CLOB_API_KEY", "CLOB_SECRET",
    "CLOB_PASS_PHRASE", "FUNDER", "SIGNATURE_TYPE", "HOST", "CHAIN_ID",
    "BUILDER_CODE", "GAMMA_API", "DISCORD_WEBHOOK_URL",
})


class BotConfig:
    """Hot-reloadable configuration singleton.

    On each reload():
      1. Reads config_overrides.json if it exists.
      2. For each key not in _IMMUTABLE, overrides the live value.
      3. Logs every change with old → new values.

    All module-level constants remain as the compile-time defaults.
    cfg("PARAM") returns the live (possibly overridden) value.
    """

    _instance: "BotConfig | None" = None
    _init_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._overrides: dict = {}
        self._file_mtime: float = 0
        # Snapshot of all module-level constants as defaults
        import config as _self_module
        self._defaults: dict = {
            k: v for k, v in vars(_self_module).items()
            if k.isupper() and not k.startswith("_") and k not in _IMMUTABLE
        }
        # Initial load
        self._try_load()

    @classmethod
    def instance(cls) -> "BotConfig":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def get(self, name: str) -> object:
        """Return the live value for a config parameter.

        Checks overrides first, then falls back to the module-level default.
        """
        with self._lock:
            if name in self._overrides:
                return self._overrides[name]
        # Fall back to module-level constant
        return self._defaults.get(name)

    def reload(self) -> int:
        """Reload overrides from config_overrides.json.

        Returns the number of parameters that changed.
        """
        return self._try_load()

    def check_and_reload(self) -> int:
        """Reload only if the overrides file has been modified.

        Designed to be called every cycle — cheap no-op if file unchanged.
        Returns number of changes (0 if no reload needed).
        """
        try:
            if not os.path.exists(OVERRIDES_FILE):
                if self._overrides:
                    # File was deleted — revert to defaults
                    with self._lock:
                        old = self._overrides.copy()
                        self._overrides = {}
                        self._file_mtime = 0
                    if old:
                        import logging
                        logging.getLogger(__name__).info(
                            f"CONFIG | Overrides file removed — "
                            f"reverted {len(old)} param(s) to defaults"
                        )
                    return len(old)
                return 0

            mtime = os.path.getmtime(OVERRIDES_FILE)
            if mtime <= self._file_mtime:
                return 0
            return self._try_load()
        except Exception:
            return 0

    def _try_load(self) -> int:
        """Load overrides from JSON file. Returns number of changes."""
        import logging
        log = logging.getLogger(__name__)
        if not os.path.exists(OVERRIDES_FILE):
            return 0

        try:
            with open(OVERRIDES_FILE, "r") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                log.warning("CONFIG | config_overrides.json is not a dict — ignoring")
                return 0

            changes = 0
            new_overrides = {}
            for key, val in raw.items():
                if key in _IMMUTABLE:
                    log.warning(f"CONFIG | Ignoring immutable param: {key}")
                    continue
                if key not in self._defaults:
                    log.warning(f"CONFIG | Unknown param in overrides: {key}")
                    continue

                # Type-check: ensure override matches the default's type
                default_val = self._defaults[key]
                if default_val is not None:
                    expected_type = type(default_val)
                    if not isinstance(val, expected_type):
                        # Allow int → float promotion
                        if expected_type is float and isinstance(val, int):
                            val = float(val)
                        else:
                            log.warning(
                                f"CONFIG | Type mismatch for {key}: "
                                f"expected {expected_type.__name__}, "
                                f"got {type(val).__name__} — skipping"
                            )
                            continue

                new_overrides[key] = val

            with self._lock:
                old = self._overrides
                self._overrides = new_overrides
                self._file_mtime = os.path.getmtime(OVERRIDES_FILE)

                # Log changes
                for key, val in new_overrides.items():
                    old_val = old.get(key, self._defaults.get(key))
                    if old_val != val:
                        log.info(f"CONFIG RELOAD | {key}: {old_val} → {val}")
                        changes += 1
                # Log reverted params
                for key in old:
                    if key not in new_overrides:
                        log.info(
                            f"CONFIG RELOAD | {key}: {old[key]} → "
                            f"{self._defaults.get(key)} (reverted to default)"
                        )
                        changes += 1

            if changes:
                log.info(f"CONFIG | Reloaded {changes} param(s) from {OVERRIDES_FILE}")
            return changes

        except json.JSONDecodeError as e:
            log.warning(f"CONFIG | Invalid JSON in overrides file: {e}")
            return 0
        except Exception as e:
            log.warning(f"CONFIG | Could not load overrides: {e}")
            return 0


def cfg(name: str) -> object:
    """Get the live value of a config parameter (hot-reloadable).

    Usage:
        from config import cfg
        size = cfg("ORDER_SIZE")  # Returns overridden value if set

    Falls back to the module-level constant if no override is active.
    """
    return BotConfig.instance().get(name)