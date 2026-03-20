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
MAX_MARKETS: int = 3          # Maximum number of markets to trade at once
MIN_SCORE_THRESHOLD: int = 60 # Minimum score (out of 100) to trade a market
MARKET_REFRESH_SECS: int = 1800  # Re-score and refresh markets every 30 min

# ── Scoring Weights (must sum to 100) ─────────────────────────────────────────
WEIGHT_DAILY_RATE: int = 25
WEIGHT_COMPETITION: int = 25
WEIGHT_PRICE_BAL: int = 20
WEIGHT_EXPIRY: int = 15
WEIGHT_SPREAD: int = 10
WEIGHT_LIQUIDITY: int = 5

# ── Hygiene Filter Thresholds ─────────────────────────────────────────────────
MIN_DAYS_TO_EXPIRY: int = 7       # Skip markets expiring within 7 days
MIN_YES_PRICE: float = 0.05      # Skip if Yes price below 5c
MAX_YES_PRICE: float = 0.95      # Skip if Yes price above 95c
MIN_DAILY_RATE: float = 1.0      # Skip if daily reward rate below $1
MIN_LIQUIDITY: int = 1000        # Skip if liquidity below $1000
MIN_SPREAD_ALLOWED: float = 0.01 # Skip if max spread below 1c

# ── Order Management ──────────────────────────────────────────────────────────
ORDER_SIZE: int = 100        # Preferred USDC per order (target budget)
MAX_ORDER_BUDGET: int = 500  # Hard cap — never spend more than this per order
MAX_ORDER_SIZE: int = MAX_ORDER_BUDGET  # Alias used by market.py hygiene check
ORDER_REFRESH_SECS: int = 30 # Cancel and replace orders every 30 seconds

# ── Orderbook Safety ─────────────────────────────────────────────────────────
# Maximum spread between best bid and best ask before we consider the book
# too sparse to trust. Markets with wider spreads are skipped for that cycle.
MAX_ORDERBOOK_SPREAD: float = 0.10  # 10c — wider than this is too sparse

# Minimum dollar value of existing orders that must sit in front of ours.
# We walk the orderbook and place our bid below $1000 of other bids,
# and our ask above $1000 of other asks.
MIN_LIQUIDITY_BUFFER: float = 1000.0  # $1000 of liquidity buffer

# ── Order Zone Thresholds ─────────────────────────────────────────────────────
DANGER_ZONE_CENTS: float = 0.005  # Cancel if order within 0.5c of best price
DEAD_ZONE_BUFFER: float = 0.001   # Buffer beyond max spread for dead zone

# ── Position Limits ───────────────────────────────────────────────────────────
MAX_POSITION_USD: int = 100     # Stop quoting a side if position exceeds $100
RESUME_POSITION_USD: int = 75   # Resume quoting when position falls below $75

# ── Alert Thresholds ──────────────────────────────────────────────────────────
MAX_ORDER_FAILURES: int = 3  # Alert after this many consecutive order failures

# ── Discord Notifications ────────────────────────────────────────────────────
# Create a webhook in your Discord server:
#   Server Settings -> Integrations -> Webhooks -> New Webhook
# Paste the URL into your .env file as DISCORD_WEBHOOK_URL
DISCORD_WEBHOOK_URL: str | None = os.getenv("DISCORD_WEBHOOK_URL")