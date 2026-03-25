"""
Configuration for the Polymarket market-making bot.

All tunable parameters live here. Sensitive credentials are loaded
from environment variables via a .env file.
"""

import os
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

HOST: str = "https://clob.polymarket.com"
CHAIN_ID: int = 137  # Polygon mainnet

# ── External APIs ─────────────────────────────────────────────────────────────
GAMMA_API: str = "https://gamma-api.polymarket.com"

# ── Market Selection ──────────────────────────────────────────────────────────
MAX_MARKETS: int = 3          # Focus on fewer, higher-quality markets
MIN_SCORE_THRESHOLD: int = 60 # Minimum score (out of 100) to trade a market
HYSTERESIS_SCORE_MARGIN: int = 10  # New market must outscore weakest by this much to swap in
MARKET_REFRESH_SECS: int = 1800  # Re-score and refresh markets every 30 min

# ── Scoring Weights (must sum to 100) ─────────────────────────────────────────
# Heavily weight reward rate and competition (reward density).
# Price balance demoted — balanced markets are most competitive/least rewarding.
# Expiry is NOT scored — it is only a hygiene filter (≥ 12 hours).
WEIGHT_DAILY_RATE: int = 35
WEIGHT_COMPETITION: int = 35
WEIGHT_PRICE_BAL: int = 10
WEIGHT_SPREAD: int = 10
WEIGHT_LIQUIDITY: int = 10

# ── Hygiene Filter Thresholds ─────────────────────────────────────────────────
MIN_DAYS_TO_EXPIRY: float = 0.5   # Skip markets expiring within 12 hours
MIN_YES_PRICE: float = 0.05      # Skip if Yes price below 5c
MAX_YES_PRICE: float = 0.95      # Skip if Yes price above 95c
MIN_DAILY_RATE: float = 5.0      # Skip if daily reward rate below $5
MIN_LIQUIDITY: int = 1000        # Skip if liquidity below $1000
MIN_SPREAD_ALLOWED: float = 0.01 # Skip if max spread below 1c

# ── Order Management ──────────────────────────────────────────────────────────
ORDER_SIZE: int = 250        # Reduced from $500 — limits adverse selection damage
MAX_ORDER_BUDGET: int = 750  # Hard cap — allows sports markets with high min_size
CHEAP_TOKEN_THRESHOLD: float = 0.25  # Tokens under 25c get reduced order size
CHEAP_TOKEN_SCALE: float = 0.50     # Scale order size to 50% for cheap tokens
MAX_ORDER_SIZE: int = MAX_ORDER_BUDGET  # Alias used by market.py hygiene check
ORDER_REFRESH_SECS: int = 30 # Cancel and replace orders every 30 seconds

# ── Orderbook Safety ─────────────────────────────────────────────────────────
# Maximum spread between best bid and best ask before we consider the book
# too sparse to trust. Markets with wider spreads are skipped for that cycle.
MAX_ORDERBOOK_SPREAD: float = 0.10  # 10c — wider than this is too sparse

# Minimum dollar value of existing orders that must sit in front of ours.
# Higher buffer = orders placed further back = less adverse selection.
MIN_LIQUIDITY_BUFFER: float = 2000.0  # $2000 of liquidity buffer (was $1000)

# ── Order Zone Thresholds ─────────────────────────────────────────────────────
DANGER_ZONE_CENTS: float = 0.005  # Cancel if order within 0.5c of best price
DEAD_ZONE_BUFFER: float = 0.001   # Buffer beyond max spread for dead zone

# ── Position Limits ───────────────────────────────────────────────────────────
MAX_POSITION_USD: int = 400     # Stop quoting a side if position exceeds $400
RESUME_POSITION_USD: int = 300  # Resume quoting when position falls below $300

# ── Alert Thresholds ──────────────────────────────────────────────────────────
MAX_ORDER_FAILURES: int = 3  # Alert after this many consecutive order failures

# ── Unwind Settings ─────────────────────────────────────────────────────────
# Position-based: reconcile_unwinds() checks total position vs covered
# unwind orders each cycle. No retry queue needed.
MIN_UNWIND_SHARES: float = 1.0   # Ignore positions below this many shares

# ── Sell Price Decay ────────────────────────────────────────────────────────
# Sell orders start at acquisition cost (VWAP). To avoid holding depreciating
# inventory forever, the sell price decays by 1 tick per interval.
# With 300s interval: 1c drop every 5 min = 12c/hour normal, 60c/hour accelerated.
UNWIND_DECAY_INTERVAL_SECS: int = 300   # Lower sell by 1 tick every 5 min (was 10)
UNWIND_DECAY_TICKS: int = 1             # Ticks to drop per interval
UNWIND_ACCEL_LOSS_PCT: float = 0.08     # Accelerate decay when loss exceeds 8% (was 10%)
UNWIND_ACCEL_MULTIPLIER: int = 5        # 5x decay speed when accelerated (was 3x)
MIN_SELL_PRICE: float = 0.01            # Never sell below 1 cent

# ── Stop-Loss ───────────────────────────────────────────────────────────────
# If unrealized loss on a position exceeds this threshold, immediately
# sell at the current market bid to prevent further damage.
STOP_LOSS_PCT: float = 0.20        # 20% unrealized loss → dump at market
MIN_STOP_LOSS_USD: float = 50.0    # AND absolute loss must exceed $50
STOP_LOSS_MIN_PRICE: float = 0.20  # Skip stop-loss on tokens under 20c (let decay handle)

# ── Heartbeat ────────────────────────────────────────────────────────────────
# Alert if no successful cycle completes within this many seconds.
# Catches silent failures (empty API responses, 0 eligible markets, etc.)
HEARTBEAT_TIMEOUT_SECS: int = 300  # 5 minutes

# ── Discord Notifications ────────────────────────────────────────────────────
# Create a webhook in your Discord server:
#   Server Settings -> Integrations -> Webhooks -> New Webhook
# Paste the URL into your .env file as DISCORD_WEBHOOK_URL
DISCORD_WEBHOOK_URL: str | None = os.getenv("DISCORD_WEBHOOK_URL")


# ── Credential Validation ───────────────────────────────────────────────────
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