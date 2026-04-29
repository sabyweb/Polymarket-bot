"""
Market discovery, filtering, and scoring for the Polymarket bot.

Fetches active reward-bearing markets from the Gamma API, applies hygiene
filters (expiry, price range, liquidity, spread), scores them, and returns
the top candidates for the bot to trade.
"""

import requests
import json
import logging
from datetime import datetime, timezone
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, BuilderConfig
import config
# Immutable credentials and constants — safe as direct imports
from config import (
    HOST, CHAIN_ID, PRIVATE_KEY,
    CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE,
    FUNDER, SIGNATURE_TYPE, BUILDER_CODE, GAMMA_API,
    # Hygiene filters (not overridden at runtime)
    MIN_DAYS_TO_EXPIRY, MIN_YES_PRICE, MAX_YES_PRICE,
    MIN_DAILY_RATE, MIN_LIQUIDITY, MIN_SPREAD_ALLOWED,
)

# Cache for CLOB rewards params (refreshed each market refresh cycle)
_clob_rewards_cache: dict[str, dict] = {}

log = logging.getLogger(__name__)


# ── Client ───────────────────────────────────────────────────────────────────
def get_client() -> ClobClient:
    """Create and return an authenticated ClobClient.

    Returns:
        Configured ClobClient instance.
    """
    creds = ApiCreds(
        api_key=CLOB_API_KEY,
        api_secret=CLOB_SECRET,
        api_passphrase=CLOB_PASS_PHRASE,
    )
    return ClobClient(
        HOST,
        key=PRIVATE_KEY,
        chain_id=CHAIN_ID,
        creds=creds,
        signature_type=SIGNATURE_TYPE,
        funder=FUNDER,
        builder_config=BuilderConfig(builder_code=BUILDER_CODE) if BUILDER_CODE else None,
    )


# ── Fetching ─────────────────────────────────────────────────────────────────
def fetch_all_rewards_markets() -> list[dict]:
    """Paginate through Gamma API and return all markets with active rewards.

    Returns:
        List of raw market dicts that have a positive daily reward rate.
    """
    url = f"{GAMMA_API}/markets"
    page_size = 100
    offset = 0
    max_offset = 50  # Only scan top 50 markets by volume
    all_rewards: list[dict] = []

    log.info("Paginating through all Gamma API markets...")

    while True:
        params = {
            "active": "true",
            "closed": "false",
            "archived": "false",
            "enableOrderBook": "true",
            "limit": page_size,
            "offset": offset,
            "order": "volume24hr",
            "ascending": "false",
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


def fetch_clob_rewards_params() -> dict[str, dict]:
    """Fetch real rewards parameters (min_size, max_spread) from the CLOB API.

    The Gamma API does NOT return rewardsMinSize or rewardsMaxSpread.
    The authoritative source is the CLOB API endpoint:
        GET https://clob.polymarket.com/rewards/markets/current

    Returns:
        Dict keyed by condition_id (hex string) with values:
            {"min_size": float, "max_spread": float}
        max_spread is converted from cents to price units (4.5 → 0.045).
    """
    global _clob_rewards_cache
    url = f"{HOST}/rewards/markets/current"
    result: dict[str, dict] = {}
    cursor = None

    try:
        while True:
            params = {}
            if cursor:
                params["next_cursor"] = cursor

            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()

            items = data.get("data", []) if isinstance(data, dict) else data
            if not isinstance(items, list):
                break

            for m in items:
                cid = m.get("condition_id", "")
                min_size = float(m.get("rewards_min_size") or 5)
                # max_spread comes as cents (e.g. 4.5 = 4.5 cents)
                max_spread_cents = float(m.get("rewards_max_spread") or 3.0)
                max_spread = max_spread_cents / 100.0
                result[cid] = {"min_size": min_size, "max_spread": max_spread}

            cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not cursor or len(items) == 0:
                break

        log.info(f"Fetched CLOB rewards params for {len(result)} markets")
        _clob_rewards_cache = result
        return result

    except Exception as e:
        log.warning(f"Could not fetch CLOB rewards params: {e}")
        if result:
            _clob_rewards_cache = result
        return _clob_rewards_cache


# ── Parsers ──────────────────────────────────────────────────────────────────
def parse_yes_price(market: dict) -> float | None:
    """Extract the Yes outcome price from a raw market dict.

    Args:
        market: Raw market dict from the Gamma API.

    Returns:
        Yes price as a float, or None if unavailable.
    """
    try:
        prices = json.loads(market.get("outcomePrices", "[]"))
        if prices:
            return float(prices[0])
    except Exception:
        pass
    return None


def parse_days_remaining(market: dict) -> float | None:
    """Calculate days until market expiry.

    Args:
        market: Raw market dict from the Gamma API.

    Returns:
        Days remaining as a float, or None if no end date.
    """
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


def parse_clob_rewards(market: dict) -> dict[str, float]:
    """Extract CLOB reward parameters from a raw market dict.

    Args:
        market: Raw market dict from the Gamma API.

    Returns:
        Dict with keys: daily_rate, min_size, max_spread.
    """
    defaults: dict[str, float] = {
        "daily_rate": 0.0,
        "min_size": 5.0,
        "max_spread": 0.03,
    }
    try:
        clob_rewards = market.get("clobRewards", [])
        if not clob_rewards:
            return defaults
        r = clob_rewards[0]
        defaults["daily_rate"] = float(r.get("rewardsDailyRate") or 0)
        defaults["min_size"] = float(r.get("rewardsMinSize") or 5)
        defaults["max_spread"] = float(r.get("rewardsMaxSpread") or 0.03)
    except Exception:
        pass
    return defaults


def parse_token_ids(market: dict) -> list[str]:
    """Extract CLOB token IDs for both outcomes.

    Args:
        market: Raw market dict from the Gamma API.

    Returns:
        List of token ID strings (ideally two: Yes and No).
    """
    try:
        raw = market.get("clobTokenIds", "[]")
        ids = json.loads(raw)
        return ids if isinstance(ids, list) else []
    except Exception:
        return []


def parse_liquidity(market: dict) -> float:
    """Extract the market's total liquidity in USD.

    Args:
        market: Raw market dict from the Gamma API.

    Returns:
        Liquidity value in USD.
    """
    try:
        return float(market.get("liquidityNum") or 0)
    except Exception:
        return 0.0


def parse_volume_24h(market: dict) -> float:
    """Extract 24-hour CLOB volume.

    Args:
        market: Raw market dict from the Gamma API.

    Returns:
        24h volume in USD.
    """
    try:
        return float(market.get("volume24hrClob") or 0)
    except Exception:
        return 0.0


# ── Hygiene Checks ───────────────────────────────────────────────────────────
def hygiene_check(market: dict, rewards: dict) -> tuple[bool, str]:
    """Apply all pass/fail filters to a market.

    Args:
        market: Raw market dict from the Gamma API.
        rewards: Parsed reward parameters from parse_clob_rewards().

    Returns:
        (True, "OK") if the market passes, or (False, reason) if rejected.
    """
    # 1. Must have token IDs for both outcomes
    token_ids = parse_token_ids(market)
    if len(token_ids) < 2:
        return False, "Missing token IDs"

    # 2. Must have meaningful daily reward rate
    if rewards["daily_rate"] < MIN_DAILY_RATE:
        return False, f"Daily rate too low (${rewards['daily_rate']:.2f})"

    # 3. Must not expire too soon (MIN_DAYS_TO_EXPIRY = 0.5 = 12 hours)
    days_left = parse_days_remaining(market)
    if days_left is not None and days_left < MIN_DAYS_TO_EXPIRY:
        hours_left = days_left * 24
        return False, f"Expiring too soon ({hours_left:.1f}h < {MIN_DAYS_TO_EXPIRY * 24:.0f}h)"

    # 4. Price must not be too skewed
    yes_price = parse_yes_price(market)
    if yes_price is not None:
        if yes_price < MIN_YES_PRICE or yes_price > MAX_YES_PRICE:
            return False, f"Price too skewed (Yes={yes_price:.3f})"

    # 5. Min shares cost must be within our budget (check BOTH sides)
    #    Both sides must individually fit within budget since we
    #    need to quote both to earn rewards.
    yes_price = parse_yes_price(market) or 0.50
    no_price = 1 - yes_price
    yes_cost = rewards["min_size"] * yes_price
    no_cost = rewards["min_size"] * no_price
    max_cost_usd = max(yes_cost, no_cost)
    if max_cost_usd > config.MAX_ORDER_SIZE:
        return False, (
            f"Order cost YES=${yes_cost:.2f}/NO=${no_cost:.2f} "
            f"({rewards['min_size']} shares) "
            f"exceeds budget ${config.MAX_ORDER_SIZE}"
        )

    # 6. Max spread must be meaningful
    if rewards["max_spread"] < MIN_SPREAD_ALLOWED:
        return False, f"Max spread too tight ({rewards['max_spread']})"

    # 7. Must have some liquidity
    if parse_liquidity(market) < MIN_LIQUIDITY:
        return False, "Liquidity too low"

    # 8. Must have meaningful 24h volume (proxy for book depth)
    # Markets with zero volume are impossible to unwind
    vol_24h = parse_volume_24h(market)
    if vol_24h < 500:
        return False, f"24h volume too low (${vol_24h:.0f})"

    # 9. Volume-to-reward ratio filter (reward farming: avoid fill-heavy markets)
    # High volume relative to reward rate = orders get picked off constantly
    # Illinois ($7500/day, low volume) → good. Crude Oil ($500/day, huge volume) → bad.
    if rewards["daily_rate"] > 0:
        vol_reward_ratio = vol_24h / rewards["daily_rate"]
        if vol_reward_ratio > config.MAX_VOLUME_TO_REWARD_RATIO:
            return False, (
                f"Too fill-heavy (vol/reward={vol_reward_ratio:.0f}x, "
                f"max={config.MAX_VOLUME_TO_REWARD_RATIO:.0f}x)"
            )

    return True, "OK"


# ── Rank-Based Percentile Scoring ────────────────────────────────────────────
def _rank_percentiles(values: list[float], higher_is_better: bool = True) -> list[float]:
    """Convert raw values to percentile scores (0.0 to 1.0).

    Best market on a component gets 1.0, worst gets 0.0, rest are
    linearly interpolated.  Ties receive the same (averaged) score.

    Args:
        values: Raw metric values, one per market.
        higher_is_better: If True, highest value = rank 1.

    Returns:
        List of percentile scores in the same order as input.
    """
    n = len(values)
    if n <= 1:
        return [1.0] * n

    indexed = sorted(enumerate(values), key=lambda x: x[1], reverse=higher_is_better)
    scores = [0.0] * n
    i = 0
    while i < n:
        j = i + 1
        while j < n and abs(indexed[j][1] - indexed[i][1]) < 1e-9:
            j += 1
        avg_rank = sum(range(i, j)) / (j - i)
        percentile = 1.0 - (avg_rank / (n - 1))
        for k in range(i, j):
            scores[indexed[k][0]] = percentile
        i = j
    return scores


def score_markets_ranked(markets: list[dict]) -> list[dict]:
    """Score markets using rank-based percentile scoring.

    Optimised for reward EFFICIENCY — estimated reward capture per dollar
    of capital deployed — rather than raw pool size.

    Components (must sum to 100):
      - Reward efficiency (25): estimated $/day we capture per $ deployed
      - Competition / capture rate (25): our share of the pool
      - Fill safety (15): lower volume = fewer fills = less adverse selection
      - Unwind ability (15): bid-side liquidity for exit
      - Daily rate (10): raw pool size (tiebreaker)
      - Spread width (10): wider reward window = easier to stay inside

    Expiry is NOT scored — it is only a hygiene filter.

    Args:
        markets: List of parsed market dicts (must have daily_rate,
                 liquidity, yes_price, max_spread, volume_24h fields).

    Returns:
        Same list with "score" and "score_breakdown" keys added,
        sorted by score descending.
    """
    n = len(markets)
    if n == 0:
        return markets

    our_capital = config.ORDER_SIZE * 2  # Both sides

    # ── Raw component values ──────────────────────────────────────────

    # 1. Reward efficiency: estimated $/day we capture per $ deployed
    efficiencies = []
    for m in markets:
        liq = m.get("liquidity", 0)
        rate = m["daily_rate"]
        # Estimate our share: our_capital / (our_capital + total_eligible)
        capture_pct = our_capital / (our_capital + liq) if liq > 0 else 0.5
        est_daily = rate * capture_pct
        # Capital required: min_size × max(yes_price, no_price) × 2 sides
        yes_p = m.get("yes_price") or 0.50
        max_side = max(yes_p, 1 - yes_p)
        capital_req = max(m.get("min_size", 1) * max_side * 2, our_capital)
        efficiency = est_daily / capital_req if capital_req > 0 else 0
        efficiencies.append(efficiency)
        m["est_daily_reward"] = round(est_daily, 2)
        m["capture_pct"] = round(capture_pct * 100, 1)

    # 2. Competition: capture rate (higher = less competition)
    capture_rates = []
    for m in markets:
        liq = m.get("liquidity", 0)
        capture = our_capital / (our_capital + liq) if liq > 0 else 0.5
        capture_rates.append(capture)

    # 3. Fill safety: inverse of volume — lower volume = safer
    # Use 1 / (1 + vol/10000) so it's bounded [0, 1]
    fill_safeties = []
    for m in markets:
        vol = m.get("volume_24h", 0)
        fill_safeties.append(1.0 / (1.0 + vol / 10000.0))

    # 4. Unwind ability: raw liquidity (higher = easier to exit)
    liqs = [m.get("liquidity", 0) for m in markets]

    # 5. Daily rate: raw pool size
    rates = [m["daily_rate"] for m in markets]

    # 6. Spread width: wider = easier to stay in reward window
    spreads = [m["max_spread"] for m in markets]

    # ── Percentile ranks ──────────────────────────────────────────────
    pct_efficiency = _rank_percentiles(efficiencies, higher_is_better=True)
    pct_capture = _rank_percentiles(capture_rates, higher_is_better=True)
    pct_fill_safety = _rank_percentiles(fill_safeties, higher_is_better=True)
    pct_unwind = _rank_percentiles(liqs, higher_is_better=True)
    pct_rate = _rank_percentiles(rates, higher_is_better=True)
    pct_spread = _rank_percentiles(spreads, higher_is_better=True)

    for i, m in enumerate(markets):
        breakdown = {
            "efficiency": round(pct_efficiency[i] * config.WEIGHT_REWARD_EFFICIENCY, 2),
            "competition": round(pct_capture[i] * config.WEIGHT_COMPETITION, 2),
            "fill_safety": round(pct_fill_safety[i] * config.WEIGHT_FILL_SAFETY, 2),
            "unwind": round(pct_unwind[i] * config.WEIGHT_UNWIND_ABILITY, 2),
            "daily_rate": round(pct_rate[i] * config.WEIGHT_DAILY_RATE, 2),
            "spread": round(pct_spread[i] * config.WEIGHT_SPREAD, 2),
        }
        m["score"] = round(sum(breakdown.values()), 2)
        m["score_breakdown"] = breakdown
        m["reward_efficiency"] = round(efficiencies[i], 4)

    markets.sort(key=lambda x: x["score"], reverse=True)

    # Log score breakdowns for visibility
    for i, m in enumerate(markets[:10], 1):
        bd = m["score_breakdown"]
        log.info(
            f"  #{i} {m['question'][:45]} | "
            f"score={m['score']:.1f} | "
            f"eff={bd['efficiency']:.0f}/{config.WEIGHT_REWARD_EFFICIENCY} "
            f"comp={bd['competition']:.0f}/{config.WEIGHT_COMPETITION} "
            f"fill={bd['fill_safety']:.0f}/{config.WEIGHT_FILL_SAFETY} "
            f"unwind={bd['unwind']:.0f}/{config.WEIGHT_UNWIND_ABILITY} "
            f"rate={bd['daily_rate']:.0f}/{config.WEIGHT_DAILY_RATE} "
            f"spread={bd['spread']:.0f}/{config.WEIGHT_SPREAD} "
            f"| est=${m.get('est_daily_reward', 0):.1f}/day "
            f"({m.get('capture_pct', 0):.0f}% of pool)"
        )

    return markets


# ── Main Function ────────────────────────────────────────────────────────────
def get_rewards_markets(limit: int | None = None) -> list[dict]:
    """Fetch, filter, score, and return the top markets for trading.

    Uses rank-based percentile scoring: all eligible markets are ranked
    against each other on each component. Expiry is only a hygiene
    filter (≥ 12 hours), not a scoring factor.

    Args:
        limit: Maximum number of markets to return.

    Returns:
        List of market dicts, sorted by score descending.
    """
    raw_markets = fetch_all_rewards_markets()

    # Enrich with real rewards params from CLOB API first
    # (Gamma API doesn't return min_size or max_spread reliably)
    clob_rewards = fetch_clob_rewards_params()

    passed: list[dict] = []
    rejected: list[tuple[str, str]] = []

    for market in raw_markets:
        rewards = parse_clob_rewards(market)

        # Enrich with CLOB params before hygiene check
        cid = market.get("conditionId", "")
        if cid in clob_rewards:
            rewards["min_size"] = clob_rewards[cid]["min_size"]
            rewards["max_spread"] = clob_rewards[cid]["max_spread"]

        ok, reason = hygiene_check(market, rewards)
        if not ok:
            rejected.append((market.get("question", "?")[:50], reason))
            continue

        # Post-enrichment validation
        if rewards["max_spread"] < MIN_SPREAD_ALLOWED:
            rejected.append((
                market.get("question", "?")[:50],
                f"max_spread={rewards['max_spread']:.4f} < {MIN_SPREAD_ALLOWED}",
            ))
            continue
        yes_price = parse_yes_price(market) or 0.50
        max_side_cost = rewards["min_size"] * max(yes_price, 1 - yes_price)
        if max_side_cost > config.MAX_ORDER_SIZE:
            rejected.append((
                market.get("question", "?")[:50],
                f"max_side_cost=${max_side_cost:.2f} > ${config.MAX_ORDER_SIZE}",
            ))
            continue

        passed.append({
            "condition_id": cid,
            "question": market.get("question"),
            "slug": market.get("slug"),
            "token_ids": parse_token_ids(market),
            "yes_price": parse_yes_price(market),
            "daily_rate": rewards["daily_rate"],
            "min_size": rewards["min_size"],
            "max_spread": rewards["max_spread"],
            "tick_size": float(market.get("orderPriceMinTickSize") or 0.01),
            "days_left": parse_days_remaining(market),
            "liquidity": parse_liquidity(market),
            "volume_24h": parse_volume_24h(market),
        })

    log.info(f"Passed hygiene: {len(passed)} | Rejected: {len(rejected)}")
    for q, r in rejected[:5]:
        log.debug(f"  Rejected: {q} — {r}")

    # Score all eligible markets using rank-based percentile scoring
    scored = score_markets_ranked(passed)

    effective_limit = limit if limit is not None else config.MAX_MARKETS
    return scored[:int(effective_limit)]
