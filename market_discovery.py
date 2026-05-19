"""Market discovery: CLOB + Gamma fetch, order book verification, merged book.

Pure functions with no dependency on RewardFarmer class.
"""

import json
import logging
import time
import concurrent.futures
import requests
from datetime import datetime, timezone, timedelta

from config import cfg

log = logging.getLogger("reward_farmer")


# ── Config accessors ────────────────────────────────────────────────
def MIN_DAILY_RATE(): return cfg("RF_MIN_DAILY_RATE")


def verify_order_books(markets: list[dict]) -> list[dict]:
    """Verify each candidate market has real order book depth.

    Replaces unreliable liquidity values with actual on-book USD depth.
    Filters out markets resolving within 12 hours and one-sided books.
    Uses thread pool for parallel book fetches (~5x faster than sequential).
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=12)

    # Phase 1: Quick local filters (no API calls)
    candidates = []
    for m in markets:
        q_lower = (m.get("question") or "").lower()
        if " during " in q_lower:
            continue
        if "natural gas" in q_lower or "(ng)" in q_lower:
            continue
        end_date = m.get("end_date_iso")
        if end_date:
            try:
                dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                if dt <= cutoff:
                    continue
            except Exception:
                pass
        candidates.append(m)

    log.info(f"  Pre-filter: {len(candidates)}/{len(markets)} passed (expiry/keyword)")

    # Phase 2: Parallel order book depth checks
    def _check_book(m: dict) -> dict | None:
        yes_tid = m["token_ids"][0]
        try:
            resp = requests.get(
                "https://clob.polymarket.com/book",
                params={"token_id": yes_tid},
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            book = resp.json()
        except Exception:
            return None

        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            return None

        bid_depth = sum(float(b["price"]) * float(b["size"]) for b in bids[:5])
        ask_depth = sum(float(a["price"]) * float(a["size"]) for a in asks[:5])
        m["liquidity"] = bid_depth + ask_depth
        return m

    verified = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_check_book, m): m for m in candidates}
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                if result is not None:
                    verified.append(result)
            except Exception:
                pass

    log.info(f"  Verified: {len(verified)}/{len(candidates)} passed order book check")
    return verified


def fetch_all_reward_markets() -> list[dict]:
    """Fetch ALL reward markets from CLOB endpoint + Gamma details.

    Has two V2-compat fallbacks:
      1. CLOB rewards: if /rewards/markets/current returns 5xx or empty
         (post-2026-04-28 PG-timeout outage), fall back to /sampling-markets
         via authenticated V2 SDK. Translated into V1-flat shape via
         market._v2_sampling_to_v1_flat.
      2. Gamma pagination: uses /markets/keyset (post-2026-04-10
         deprecation of `offset` parameter) via market._gamma_paginated_keyset.
    """
    log.info("  Fetching CLOB rewards (authoritative source)...")
    clob_markets = []
    cursor = ""
    for _ in range(20):
        params = {"limit": 500}
        if cursor:
            params["next_cursor"] = cursor
        try:
            resp = requests.get(
                "https://clob.polymarket.com/rewards/markets/current",
                params=params, timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"  CLOB rewards fetch failed: {e}")
            break
        items = data.get("data", [])
        clob_markets.extend(items)
        cursor = data.get("next_cursor", "")
        if not cursor or not items or cursor == "LTE=":
            break
    log.info(f"  CLOB: {len(clob_markets)} reward markets")

    # V2 fallback: when /rewards/markets/current is unhealthy or empty,
    # fetch /sampling-markets via the V2 SDK and translate to V1 shape.
    if not clob_markets:
        try:
            from market import _v2_sampling_to_v1_flat, get_client
            client = get_client()
            sampling_items: list[dict] = []
            sm_cursor = "MA=="
            for _ in range(20):
                resp = client.get_sampling_markets(next_cursor=sm_cursor)
                items = resp.get("data", []) if isinstance(resp, dict) else []
                sampling_items.extend(items)
                sm_cursor = (
                    resp.get("next_cursor", "")
                    if isinstance(resp, dict) else ""
                )
                if not sm_cursor or sm_cursor == "LTE=":
                    break
            for raw in sampling_items:
                flat = _v2_sampling_to_v1_flat(raw)
                if flat is None:
                    continue
                if not flat.get("accepting_orders", True):
                    continue
                clob_markets.append(flat)
            log.warning(
                f"  [V2_FALLBACK] /rewards/markets/current empty/5xx — "
                f"used /sampling-markets, got {len(clob_markets)} markets "
                f"(may miss championship-style top-tier reward markets)"
            )
        except Exception as e:
            log.warning(f"  V2 /sampling-markets fallback failed: {e}")

    log.info("  Fetching Gamma market details (keyset pagination)...")
    from market import _gamma_paginated_keyset
    gamma_all = _gamma_paginated_keyset(
        extra_params={"closed": "false", "limit": 100},
        max_pages=100,
        page_size=100,
    )
    log.info(f"  Gamma: {len(gamma_all)} markets")

    gamma_by_cid = {m.get("conditionId", ""): m for m in gamma_all}

    merged = []
    for c in clob_markets:
        cid = c["condition_id"]
        rate = float(c.get("total_daily_rate") or 0)
        if rate < MIN_DAILY_RATE():
            continue
        min_size = float(c.get("rewards_min_size") or 50)
        ms_cents = float(c.get("rewards_max_spread") or 4.5)

        g = gamma_by_cid.get(cid)
        if g:
            try:
                token_ids = json.loads(g.get("clobTokenIds") or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if len(token_ids) < 2:
                continue
            yes_price = None
            try:
                prices = json.loads(g.get("outcomePrices") or "[]")
                yes_price = float(prices[0]) if prices else None
            except Exception as e:
                log.debug(f"  Price parse error: {g.get('question','')[:30]}: {e}")
            liq = float(g.get("liquidityNum") or 0)
            vol = float(g.get("volume24hrClob") or 0)
            question = g.get("question", "")
            tick = float(g.get("orderPriceMinTickSize") or 0.01)
            end_date_iso = g.get("endDateIso") or g.get("end_date_iso")
            # Gamma API does not expose game_start_time; only CLOB does.
            game_start_time = ""
        else:
            if rate < MIN_DAILY_RATE():
                continue
            try:
                mkt_resp = requests.get(
                    f"https://clob.polymarket.com/markets/{cid}",
                    timeout=10,
                )
                if mkt_resp.status_code != 200:
                    continue
                mkt = mkt_resp.json()
                tokens_data = mkt.get("tokens", [])
                if len(tokens_data) < 2:
                    continue
                token_ids = [tokens_data[0]["token_id"], tokens_data[1]["token_id"]]
                yes_price = float(tokens_data[0].get("price", 0.5))
                question = mkt.get("question", "")
                tick = float(mkt.get("minimum_tick_size") or 0.01)
                end_date_iso = mkt.get("end_date_iso")
                # CLOB exposes game_start_time on ~73% of markets (all sports).
                # This is the actual event/kickoff time, distinct from
                # end_date_iso (market resolution deadline).
                game_start_time = mkt.get("game_start_time", "") or ""
                liq = 999999.0
                vol = 0.0
            except Exception as e:
                log.debug(f"  CLOB market fetch failed {cid[:16]}: {e}")
                continue

        merged.append({
            "condition_id": cid,
            "question": question,
            "token_ids": token_ids,
            "yes_price": yes_price,
            "daily_rate": rate,
            "min_size": min_size,
            "max_spread": ms_cents / 100.0,
            "tick_size": tick,
            "liquidity": liq,
            "volume_24h": vol,
            "end_date_iso": end_date_iso,
            "game_start_time": game_start_time,
        })

    log.info(f"  Merged: {len(merged)} candidates with rate >= ${MIN_DAILY_RATE()}/day")

    log.info(f"  Verifying order books for {len(merged)} candidates...")
    merged = verify_order_books(merged)

    merged.sort(key=lambda x: x["liquidity"])
    log.info(f"  Final: {len(merged)} verified markets")
    return merged


def _book_entries(ob, key: str) -> list[tuple[float, float]]:
    """Extract (price, size) tuples from an orderbook bids/asks list.

    fixit.md::FX-035 — py-clob-client-v2 v1.0.0 returns `get_order_book`
    as a **dict** with string-valued `'bids'`/`'asks'` entries like
    ``[{'price': '0.02', 'size': '2250'}, ...]``. Pre-FX-035 the code used
    `getattr(ob, "bids", [])` and `float(b.price)`, which assumed an
    object-with-attributes shape (the format test mocks produce). On the
    real V2 SDK return, `getattr(dict, "bids")` returns the default (`[]`),
    so get_merged_book always returned None on production — invisible to
    unit tests but catastrophic in LIVE mode (Helsinki bot couldn't fetch
    a single book for 4 days). This normalizer handles both forms so the
    function works under SDK + mocks alike.
    """
    if ob is None:
        return []
    # V2 SDK dict-form: {'bids': [{'price': str, 'size': str}, ...]}
    if isinstance(ob, dict):
        raw = ob.get(key, []) or []
        out = []
        for e in raw:
            if isinstance(e, dict):
                p = float(e.get("price", 0))
                s = float(e.get("size", 0))
            else:
                p = float(getattr(e, "price", 0))
                s = float(getattr(e, "size", 0))
            out.append((p, s))
        return out
    # Object-form (test mocks, possibly future SDK changes):
    raw = getattr(ob, key, []) or []
    out = []
    for e in raw:
        if isinstance(e, dict):
            p = float(e.get("price", 0))
            s = float(e.get("size", 0))
        else:
            p = float(getattr(e, "price", 0))
            s = float(getattr(e, "size", 0))
        out.append((p, s))
    return out


def get_merged_book(client, yes_tid: str, no_tid: str) -> dict | None:
    """Fetch YES + NO order books and merge into YES-equivalent view.

    Handles both V2 SDK dict-return and test-mock object-return shapes
    via _book_entries (fixit.md::FX-035).
    """
    try:
        ob_yes = client.get_order_book(yes_tid)
        if not ob_yes:
            return None

        all_bids: list[tuple[float, float]] = []
        all_asks: list[tuple[float, float]] = []

        for p, s in _book_entries(ob_yes, "bids"):
            all_bids.append((p, s))
        for p, s in _book_entries(ob_yes, "asks"):
            all_asks.append((p, s))

        ob_no = client.get_order_book(no_tid)
        if ob_no:
            # NO-side asks → YES-side bids at (1 - price)
            for p, s in _book_entries(ob_no, "asks"):
                derived = round(1.0 - p, 4)
                if derived > 0:
                    all_bids.append((derived, s))
            # NO-side bids → YES-side asks at (1 - price)
            for p, s in _book_entries(ob_no, "bids"):
                derived = round(1.0 - p, 4)
                if derived < 1:
                    all_asks.append((derived, s))

        all_bids.sort(key=lambda x: x[0], reverse=True)
        all_asks.sort(key=lambda x: x[0])

        if not all_bids or not all_asks:
            return None

        return {
            "bids": [{"price": p, "size": s} for p, s in all_bids],
            "asks": [{"price": p, "size": s} for p, s in all_asks],
        }
    except Exception as e:
        log.debug(f"Merged book fetch error: {e}")
        return None
