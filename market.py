import requests
import json
import logging
from datetime import datetime, timezone
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from config import (
    HOST, CHAIN_ID, PRIVATE_KEY,
    CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE,
    MAX_MARKETS, MAX_ORDER_SIZE, MIN_DAYS_TO_EXPIRY,
    MIN_YES_PRICE, MAX_YES_PRICE, MIN_DAILY_RATE,
    MIN_LIQUIDITY, MIN_SPREAD_ALLOWED,
    WEIGHT_DAILY_RATE, WEIGHT_COMPETITION,
    WEIGHT_PRICE_BAL, WEIGHT_EXPIRY,
    WEIGHT_SPREAD, WEIGHT_LIQUIDITY
)

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


# ── Client ────────────────────────────────────────────────────────────────────
def get_client():
    creds = ApiCreds(
        api_key=CLOB_API_KEY,
        api_secret=CLOB_SECRET,
        api_passphrase=CLOB_PASS_PHRASE,
    )
    return ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, creds=creds)


# ── Fetching ──────────────────────────────────────────────────────────────────
def fetch_all_rewards_markets():
    url       = f"{GAMMA_API}/markets"
    page_size = 100
    offset    = 0
    max_offset = 50  # Only scan top 50 markets by volume
    all_rewards = []

    log.info("Paginating through all Gamma API markets...")

    while True:
        params = {
            "active":          "true",
            "closed":          "false",
            "archived":        "false",
            "enableOrderBook": "true",
            "limit":           page_size,
            "offset":          offset,
            "order":           "volume24hr",
            "ascending":       "false"
        }
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            page = response.json()
        except Exception as e:
            log.error(f"Error fetching page at offset {offset}: {e}")
            break

        if not page:
            break

        for market in page:
            clob_rewards = market.get("clobRewards", [])
            if isinstance(clob_rewards, list) and len(clob_rewards) > 0:
                daily_rate = float(clob_rewards[0].get("rewardsDailyRate") or 0)
                if daily_rate > 0:
                    all_rewards.append(market)

        log.info(
            f"  Offset {offset}: fetched {len(page)} markets | "
            f"cumulative rewards markets: {len(all_rewards)}"
        )

        if len(page) < page_size:
            break

        offset += page_size

        if offset >= max_offset:
            log.info(f"Reached max offset {max_offset} — stopping pagination")
            break

    log.info(f"Total rewards markets found: {len(all_rewards)}")
    return all_rewards


# ── Parsers ───────────────────────────────────────────────────────────────────
def parse_yes_price(market):
    try:
        prices = json.loads(market.get("outcomePrices", "[]"))
        if prices:
            return float(prices[0])
    except Exception:
        pass
    return None


def parse_days_remaining(market):
    try:
        end_str = market.get("endDateIso") or market.get("endDate")
        if not end_str:
            return None
        if len(end_str) <= 10:
            end_str += "T23:59:59Z"
        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        return (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400
    except Exception:
        return None


def parse_clob_rewards(market):
    defaults = {
        "daily_rate": 0.0,
        "min_size":   5.0,
        "max_spread": 0.03,
    }
    try:
        clob_rewards = market.get("clobRewards", [])
        if not clob_rewards:
            return defaults
        r = clob_rewards[0]
        defaults["daily_rate"] = float(r.get("rewardsDailyRate") or 0)
        defaults["min_size"]   = float(r.get("rewardsMinSize")   or 5)
        defaults["max_spread"] = float(r.get("rewardsMaxSpread") or 0.03)
    except Exception:
        pass
    return defaults


def parse_token_ids(market):
    try:
        raw = market.get("clobTokenIds", "[]")
        ids = json.loads(raw)
        return ids if isinstance(ids, list) else []
    except Exception:
        return []


def parse_liquidity(market):
    try:
        return float(market.get("liquidityNum") or 0)
    except Exception:
        return 0.0


def parse_volume_24h(market):
    try:
        return float(market.get("volume24hrClob") or 0)
    except Exception:
        return 0.0


# ── Hygiene Checks ────────────────────────────────────────────────────────────
def hygiene_check(market, rewards):
    # 1. Must have token IDs for both outcomes
    token_ids = parse_token_ids(market)
    if len(token_ids) < 2:
        return False, "Missing token IDs"

    # 2. Must have meaningful daily reward rate
    if rewards["daily_rate"] < MIN_DAILY_RATE:
        return False, f"Daily rate too low (${rewards['daily_rate']:.2f})"

    # 3. Must not expire too soon
    days_left = parse_days_remaining(market)
    if days_left is not None and days_left < MIN_DAYS_TO_EXPIRY:
        return False, f"Expiring too soon ({days_left:.1f} days)"

    # 4. Price must not be too skewed
    yes_price = parse_yes_price(market)
    if yes_price is not None:
        if yes_price < MIN_YES_PRICE or yes_price > MAX_YES_PRICE:
            return False, f"Price too skewed (Yes={yes_price:.3f})"

    # 5. Min shares cost must be within our budget
    yes_price    = parse_yes_price(market) or 0.50
    min_cost_usd = rewards["min_size"] * yes_price
    if min_cost_usd > MAX_ORDER_SIZE:
        return False, (
            f"Min cost ${min_cost_usd:.2f} "
            f"({rewards['min_size']} shares @ {yes_price:.2f}) "
            f"exceeds budget ${MAX_ORDER_SIZE}"
        )

    # 6. Max spread must be meaningful
    if rewards["max_spread"] < MIN_SPREAD_ALLOWED:
        return False, f"Max spread too tight ({rewards['max_spread']})"

    # 7. Must have some liquidity
    if parse_liquidity(market) < MIN_LIQUIDITY:
        return False, "Liquidity too low"

    return True, "OK"


# ── Scoring ───────────────────────────────────────────────────────────────────
def score_market(market, rewards):
    score = 0.0

    # 1. Daily reward rate
    rate_score = min(rewards["daily_rate"] / 500.0, 1.0) * WEIGHT_DAILY_RATE
    score += rate_score

    # 2. Competition
    try:
        liquidity = parse_liquidity(market)
        volume    = parse_volume_24h(market)
        if volume > 0:
            comp_ratio = liquidity / volume
        else:
            comp_ratio = 10.0
        comp_score = max(0, 1.0 - min(comp_ratio / 10.0, 1.0)) * WEIGHT_COMPETITION
        score += comp_score
    except Exception:
        pass

    # 3. Price balance
    yes_price = parse_yes_price(market)
    if yes_price is not None:
        distance      = abs(yes_price - 0.50)
        balance_score = max(0, 1.0 - (distance / 0.45) ** 2) * WEIGHT_PRICE_BAL
        score += balance_score

    # 4. Days remaining
    days_left = parse_days_remaining(market)
    if days_left is not None:
        expiry_score = min(days_left / 60.0, 1.0) * WEIGHT_EXPIRY
        score += expiry_score

    # 5. Spread room
    spread_score = min(rewards["max_spread"] / 0.05, 1.0) * WEIGHT_SPREAD
    score += spread_score

    # 6. Liquidity
    liq       = parse_liquidity(market)
    liq_score = min(liq / 5_000_000.0, 1.0) * WEIGHT_LIQUIDITY
    score += liq_score

    return round(score, 2)


# ── Main Function ─────────────────────────────────────────────────────────────
def get_rewards_markets(limit=MAX_MARKETS):
    raw_markets = fetch_all_rewards_markets()

    passed   = []
    rejected = []

    for market in raw_markets:
        rewards    = parse_clob_rewards(market)
        ok, reason = hygiene_check(market, rewards)

        if ok:
            passed.append({
                "condition_id": market.get("conditionId"),
                "question":     market.get("question"),
                "slug":         market.get("slug"),
                "token_ids":    parse_token_ids(market),
                "yes_price":    parse_yes_price(market),
                "daily_rate":   rewards["daily_rate"],
                "min_size":     rewards["min_size"],
                "max_spread":   rewards["max_spread"],
                "tick_size":    float(market.get("orderPriceMinTickSize") or 0.01),
                "days_left":    parse_days_remaining(market),
                "liquidity":    parse_liquidity(market),
                "volume_24h":   parse_volume_24h(market),
                "score":        score_market(market, rewards),
            })
        else:
            rejected.append((market.get("question", "?")[:50], reason))

    passed.sort(key=lambda x: x["score"], reverse=True)

    log.info(f"Passed hygiene: {len(passed)} | Rejected: {len(rejected)}")
    for q, r in rejected[:5]:
        log.debug(f"  Rejected: {q} — {r}")

    return passed[:limit]


# ── Standalone Test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )
    markets = get_rewards_markets()

    if not markets:
        print("\nNo suitable markets found.")
    else:
        print(f"\n{'='*60}")
        print(f"TOP {len(markets)} MARKETS")
        print(f"{'='*60}\n")
        for i, m in enumerate(markets, 1):
            days = f"{m['days_left']:.1f}" if m['days_left'] else "Unknown"
            print(f"#{i}  {m['question']}")
            print(f"    Score:       {m['score']}/100")
            print(f"    Yes Price:   {m['yes_price']}")
            print(f"    Daily Rate:  ${m['daily_rate']:.2f}/day")
            print(f"    Min Size:    {m['min_size']} shares")
            print(f"    Max Spread:  {m['max_spread']*100:.1f}c")
            print(f"    Tick Size:   {m['tick_size']}")
            print(f"    Days Left:   {days}")
            print(f"    Liquidity:   ${m['liquidity']:,.0f}")
            print()
