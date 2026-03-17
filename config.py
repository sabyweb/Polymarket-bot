import os
from dotenv import load_dotenv

load_dotenv()
# ── Dry Run Mode ──────────────────────────────────────────────────────────────
DRY_RUN = True   # Set to False when ready to trade with real money


# ── API Credentials ───────────────────────────────────────────────────────────
PRIVATE_KEY    = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
CLOB_API_KEY   = os.getenv("CLOB_API_KEY")
CLOB_SECRET    = os.getenv("CLOB_SECRET")
CLOB_PASS_PHRASE = os.getenv("CLOB_PASS_PHRASE")

HOST     = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

# ── Market Selection ──────────────────────────────────────────────────────────
MAX_MARKETS           = 3     # Maximum number of markets to trade at once
MIN_SCORE_THRESHOLD   = 60    # Minimum score (out of 100) to trade a market
MARKET_REFRESH_SECS   = 1800  # Re-score and refresh markets every 1 hour

# ── Scoring Weights (must sum to 100) ─────────────────────────────────────────
WEIGHT_DAILY_RATE  = 25
WEIGHT_COMPETITION = 25
WEIGHT_PRICE_BAL   = 20
WEIGHT_EXPIRY      = 15
WEIGHT_SPREAD      = 10
WEIGHT_LIQUIDITY   = 5

# ── Hygiene Filter Thresholds ─────────────────────────────────────────────────
MIN_DAYS_TO_EXPIRY  = 7      # Skip markets expiring within 7 days
MIN_YES_PRICE       = 0.05   # Skip if Yes price below 5¢
MAX_YES_PRICE       = 0.95   # Skip if Yes price above 95¢
MIN_DAILY_RATE      = 1.0    # Skip if daily reward rate below $1
MIN_LIQUIDITY       = 1000   # Skip if liquidity below $1000
MIN_SPREAD_ALLOWED  = 0.01   # Skip if max spread below 1¢

# ── Order Management ──────────────────────────────────────────────────────────
ORDER_SIZE          = 100     # USDC per order
MAX_ORDER_SIZE      = 100      # Alias used by market.py hygiene check
ORDER_REFRESH_SECS  = 30     # Cancel and replace orders every 30 seconds

# How far inside the max spread to place orders (as a fraction)
# 0.5 means place orders halfway between midpoint and max spread boundary
# e.g. if max spread is ±4¢, we place orders ±2¢ from midpoint
SPREAD_DEPTH        = 0.5

# ── Order Zone Thresholds ─────────────────────────────────────────────────────
DANGER_ZONE_CENTS   = 0.005  # Cancel immediately if order within 0.5¢ of best price
DEAD_ZONE_BUFFER    = 0.001  # Extra buffer beyond max spread before declaring dead zone

# ── Position Limits ───────────────────────────────────────────────────────────
MAX_POSITION_USD    = 100    # Stop quoting a side if position exceeds $100
RESUME_POSITION_USD = 75     # Resume quoting when position falls back below $75

# ── Alert Thresholds ──────────────────────────────────────────────────────────
MAX_ORDER_FAILURES  = 3      # Alert after this many consecutive order failures