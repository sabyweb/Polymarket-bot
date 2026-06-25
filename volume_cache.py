"""24h CLOB volume cache for arbitrary condition_ids.

Polymarket's Gamma API does not expose a bulk endpoint keyed by condition_id.
The practical path is:

    CLOB /markets/{condition_id}  ->  market_slug
    Gamma /markets?slug={slug}    ->  volume24hrClob

This module caches the result in `bot_history.db.volume_24h_cache` so the
allocator only pays the network cost once per TTL (default 6h).

Design constraints:
  - Fail-open: any per-cid error returns volume=0 and logs at debug level.
  - Stale cache: if a refresh fails, keep the old value and old fetched_at so
    the next cycle retries.
  - Missing cid: cache volume=0 with fetched_at=now so we don't hammer the
    API every cycle; it will be retried after TTL.
  - Threaded: CLOB slug fetch and Gamma volume fetch are parallelised with a
    bounded pool to stay polite and reasonably fast.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

import requests

log = logging.getLogger("volume_cache")

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"


def lookup(
    cids: list[str],
    db_path: str,
    ttl: float = 21600.0,
    max_workers: int = 10,
    _now: Callable[[], float] = time.time,
    _http: Callable = requests.get,
) -> dict[str, float]:
    """Return {condition_id: volume_24h_usd} for every cid in `cids`.

    Uses the cache when fresh; fetches missing/stale entries via CLOB slug +
    Gamma volume. Volume is 0.0 for any cid that cannot be resolved.
    """
    now = _now()
    out: dict[str, float] = {}
    old_rows: dict[str, tuple[float, float]] = {}
    to_fetch: list[str] = []

    # 1. Read cache
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        for cid in set(cids):
            row = cur.execute(
                "SELECT volume_24h, fetched_at FROM volume_24h_cache WHERE condition_id=?",
                (cid,),
            ).fetchone()
            if row:
                vol, fetched_at = row
                old_rows[cid] = (float(vol), float(fetched_at))
                if now - fetched_at <= ttl:
                    out[cid] = float(vol)
                    continue
            to_fetch.append(cid)
        conn.close()
    except Exception as e:
        log.warning(f"volume_cache read failed: {e}")
        to_fetch = list(set(cids))
        out = {}

    if not to_fetch:
        return out

    # 2. Fetch slugs in parallel
    slug_map: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_cid = {ex.submit(_fetch_slug, cid, _http): cid for cid in to_fetch}
        for future in as_completed(future_to_cid):
            cid = future_to_cid[future]
            try:
                slug_map[cid] = future.result()
            except Exception as e:
                log.debug(f"slug fetch failed for {cid}: {e}")
                slug_map[cid] = None

    # 3. Fetch volumes in parallel (only for cids where we got a slug)
    vol_map: dict[str, float | None] = {}
    slugs_to_resolve = {cid: slug for cid, slug in slug_map.items() if slug}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_cid = {
            ex.submit(_fetch_volume, slug, _http): cid
            for cid, slug in slugs_to_resolve.items()
        }
        for future in as_completed(future_to_cid):
            cid = future_to_cid[future]
            try:
                vol_map[cid] = future.result()
            except Exception as e:
                log.debug(f"volume fetch failed for {cid}: {e}")
                vol_map[cid] = None

    # 4. Write back to cache
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        for cid in to_fetch:
            slug = slug_map.get(cid)
            fresh_vol = vol_map.get(cid)

            if fresh_vol is not None:
                # Successful refresh
                final_vol = fresh_vol
                final_fetched = now
                source = "gamma"
            elif cid in old_rows:
                # Refresh failed but we have a stale value: keep it, do not bump
                # fetched_at so the next cycle retries.
                final_vol = old_rows[cid][0]
                final_fetched = old_rows[cid][1]
                source = "gamma-stale"
            else:
                # No prior value: cache 0 and retry after TTL.
                final_vol = 0.0
                final_fetched = now
                source = "gamma-failed"

            out[cid] = final_vol
            cur.execute(
                """INSERT INTO volume_24h_cache
                   (condition_id, slug, volume_24h, fetched_at, source)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(condition_id) DO UPDATE SET
                     slug=excluded.slug,
                     volume_24h=excluded.volume_24h,
                     fetched_at=excluded.fetched_at,
                     source=excluded.source""",
                (cid, slug, final_vol, final_fetched, source),
            )
        conn.commit()
        conn.close()
        log.info(
            f"volume_cache: {len(out)} total, {len(to_fetch)} refreshed/looked up, "
            f"{sum(1 for v in vol_map.values() if v is not None)} fresh Gamma hits"
        )
    except Exception as e:
        log.warning(f"volume_cache write failed: {e}")
        # Fall back to whatever we had in memory.
        for cid in to_fetch:
            if cid not in out:
                out[cid] = old_rows.get(cid, (0.0, 0.0))[0]

    return out


def _fetch_slug(cid: str, _http: Callable) -> str | None:
    url = f"{CLOB_HOST}/markets/{cid}"
    r = _http(url, timeout=15)
    if getattr(r, "status_code", 0) != 200:
        log.debug(f"CLOB markets/{cid} status={getattr(r, 'status_code', 0)}")
        return None
    data = r.json()
    if not isinstance(data, dict):
        return None
    return data.get("market_slug") or data.get("slug")


def _fetch_volume(slug: str, _http: Callable) -> float | None:
    url = f"{GAMMA_HOST}/markets"
    r = _http(url, params={"slug": slug}, timeout=15)
    if getattr(r, "status_code", 0) != 200:
        log.debug(f"Gamma markets?slug={slug} status={getattr(r, 'status_code', 0)}")
        return None
    data = r.json()
    m = None
    if isinstance(data, list) and data:
        m = data[0]
    elif isinstance(data, dict):
        m = data
    if not isinstance(m, dict):
        return None
    try:
        return float(m.get("volume24hrClob") or 0.0)
    except (TypeError, ValueError):
        return None
