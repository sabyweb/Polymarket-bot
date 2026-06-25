"""24h CLOB volume cache for arbitrary condition_ids.

Polymarket's Gamma API does not expose a reliable per-condition_id endpoint
for 24h CLOB volume.  The slug endpoint (`/markets?slug=...`) returns
`volume` (all-time) but not `volume24hrClob`.  The active market list endpoint
(`/markets?active=true&limit=100&offset=...`) *does* include `volume24hrClob`
for nearly every market.

So the lookup strategy is:
  1. Build a condition_id -> volume_24h map by paginating the active list
     until all requested cids are found (or the list is exhausted).
  2. Fall back to the slug endpoint for any cid that was not in the list.
  3. If 24h volume is missing, conservatively use the all-time `volumeNum`
     value: a market whose all-time volume is below the cap must also have
     24h volume below the cap, so it is safe to include.

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

# Conservative 24h volume proxy.  If the explicit 24h field is present we use
# it.  Otherwise we fall back to all-time volume: a market with all-time volume
# below the cap is guaranteed to have 24h volume below the cap, so including it
# is safe.  Markets with all-time volume above the cap are excluded, which is
# the conservative choice when 24h data is unavailable.
def _extract_volume(market: dict) -> float:
    for key in ("volume24hrClob", "volumeNum", "volume"):
        val = market.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return 0.0


def lookup(
    cids: list[str],
    db_path: str,
    ttl: float = 21600.0,
    max_workers: int = 10,
    _now: Callable[[], float] = time.time,
    _http: Callable = requests.get,
) -> dict[str, float]:
    """Return {condition_id: volume_24h_usd} for every cid in `cids`.

    Uses the cache when fresh; fetches missing/stale entries from Gamma.
    Volume is 0.0 for any cid that cannot be resolved.
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

    # 2. Bulk resolve from the active-market list, which includes
    #    volume24hrClob for the vast majority of markets.
    target = set(to_fetch)
    list_map = _fetch_volumes_from_list(target, _http)
    unresolved = target - set(list_map.keys())

    # 3. Fallback for cids not present in the active list: resolve slug via
    #    CLOB, then fetch the individual market record by slug.
    slug_map: dict[str, str | None] = {}
    if unresolved:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            future_to_cid = {ex.submit(_fetch_slug, cid, _http): cid for cid in unresolved}
            for future in as_completed(future_to_cid):
                cid = future_to_cid[future]
                try:
                    slug_map[cid] = future.result()
                except Exception as e:
                    log.debug(f"slug fetch failed for {cid}: {e}")
                    slug_map[cid] = None

    vol_map: dict[str, float | None] = {}
    slugs_to_resolve = {cid: slug for cid, slug in slug_map.items() if slug}
    if slugs_to_resolve:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            future_to_cid = {
                ex.submit(_fetch_volume_by_slug, slug, _http): cid
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
            fresh_vol: float | None = None
            source = "gamma"
            if cid in list_map:
                fresh_vol = list_map[cid]
                source = "gamma-list"
            elif cid in vol_map:
                fresh_vol = vol_map[cid]
                source = "gamma-slug"

            if fresh_vol is not None:
                final_vol = fresh_vol
                final_fetched = now
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
                (cid, slug_map.get(cid), final_vol, final_fetched, source),
            )
        conn.commit()
        conn.close()
        log.info(
            f"volume_cache: {len(out)} total, {len(to_fetch)} refreshed/looked up, "
            f"{len(list_map)} from list, {sum(1 for v in vol_map.values() if v is not None)} slug hits"
        )
    except Exception as e:
        log.warning(f"volume_cache write failed: {e}")
        # Fall back to whatever we had in memory.
        for cid in to_fetch:
            if cid not in out:
                out[cid] = old_rows.get(cid, (0.0, 0.0))[0]

    return out


def _fetch_volumes_from_list(
    target: set[str], _http: Callable
) -> dict[str, float]:
    """Paginate Gamma active markets until all target cids are resolved."""
    resolved: dict[str, float] = {}
    offset = 0
    limit = 100
    missing = set(target)
    pages = 0
    while missing:
        pages += 1
        url = f"{GAMMA_HOST}/markets"
        params = {"active": "true", "limit": limit, "offset": offset}
        try:
            r = _http(url, params=params, timeout=30)
        except Exception as e:
            log.debug(f"Gamma list page offset={offset} request failed: {e}")
            break
        if getattr(r, "status_code", 0) != 200:
            log.debug(f"Gamma list page offset={offset} status={getattr(r, "status_code", 0)}")
            break
        data = r.json()
        if not isinstance(data, list):
            break
        if not data:
            break
        for m in data:
            cid = m.get("conditionId")
            if cid in missing:
                resolved[cid] = _extract_volume(m)
                missing.discard(cid)
                if not missing:
                    break
        offset += limit
        # Safety cap: stop after a very large number of pages.  10k active
        # markets is far above current Polymarket size.
        if pages >= 500:
            break
    log.debug(f"Gamma list resolved {len(resolved)}/{len(target)} cids in {pages} page(s)")
    return resolved


def _fetch_slug(cid: str, _http: Callable) -> str | None:
    url = f"{CLOB_HOST}/markets/{cid}"
    r = _http(url, timeout=15)
    if getattr(r, "status_code", 0) != 200:
        log.debug(f"CLOB markets/{cid} status={getattr(r, "status_code", 0)}")
        return None
    data = r.json()
    if not isinstance(data, dict):
        return None
    return data.get("market_slug") or data.get("slug")


def _fetch_volume_by_slug(slug: str, _http: Callable) -> float | None:
    url = f"{GAMMA_HOST}/markets"
    r = _http(url, params={"slug": slug}, timeout=15)
    if getattr(r, "status_code", 0) != 200:
        log.debug(f"Gamma markets?slug={slug} status={getattr(r, "status_code", 0)}")
        return None
    data = r.json()
    m = None
    if isinstance(data, list) and data:
        m = data[0]
    elif isinstance(data, dict):
        m = data
    if not isinstance(m, dict):
        return None
    return _extract_volume(m)
