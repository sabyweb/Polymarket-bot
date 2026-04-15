"""Correlation-aware capital allocation.

Detects markets with correlated fill risk by analyzing co-fill patterns.
Markets filled within the same 5-minute window share adverse selection risk.

When ≥2 co-fills in 24h → same cluster → capped at 30% of deployable capital.
Clusters exceeding MAX_CLUSTER_SIZE are dissolved (chain-link explosion guard).
"""

import logging
import time

from oversight.data_collector import _connect_db

log = logging.getLogger("profit.correlation")

# Co-fill detection parameters
COFILL_WINDOW_SECS = 300      # 5-minute window (sliding, not bucket-aligned)
COFILL_MIN_COUNT = 2          # Fix 2: lowered from 3 to catch real correlations
COFILL_LOOKBACK_SECS = 86400  # 24h lookback

# Fix 4: Chain-link explosion guard
MAX_CLUSTER_SIZE = 10

# Default cluster cap
DEFAULT_MAX_CLUSTER_PCT = 0.30

# FIX 9: Stricter cap applied to oversized clusters instead of dissolving them
OVERSIZED_CLUSTER_PCT = 0.15


def build_fill_clusters(db_path: str) -> tuple[dict[str, int], set[int]]:
    """Build clusters of correlated markets from co-fill patterns.

    Two markets are in the same cluster if a pair of their fills landed within
    COFILL_WINDOW_SECS of each other at least COFILL_MIN_COUNT times in the
    last COFILL_LOOKBACK_SECS. The window is a true sliding window measured
    with |t1 - t2| <= COFILL_WINDOW_SECS — NOT bucket-aligned.

    Oversized clusters (> MAX_CLUSTER_SIZE) are preserved but flagged in the
    returned oversized_cluster_ids set so callers can apply a stricter cap.
    They are NEVER dropped (doing so would treat the most dangerous chain-link
    clusters as uncorrelated).

    Returns: (clusters, oversized_cluster_ids)
      - clusters: {market_id: cluster_id}, only multi-market clusters.
      - oversized_cluster_ids: set of cluster_ids with size > MAX_CLUSTER_SIZE.
    """
    cutoff = time.time() - COFILL_LOOKBACK_SECS

    try:
        db = _connect_db(db_path)
        rows = db.execute(
            "SELECT condition_id, ts FROM fills "
            "WHERE ts > ? AND condition_id != '__FILL_STORM__' "
            "ORDER BY ts",
            (cutoff,),
        ).fetchall()
        db.close()
    except Exception as e:
        log.warning(f"Cluster build failed (DB): {e}")
        return {}, set()

    if not rows:
        return {}, set()

    all_cids: set[str] = {cid for cid, _ in rows}

    # FIX 7: Sliding-window co-fill detection.
    # Rows are ORDER BY ts. For each fill i, scan forward while
    # |ts_j - ts_i| <= window and count distinct pair interactions.
    pair_counts: dict[tuple[str, str], int] = {}
    n = len(rows)
    for i in range(n):
        cid_i, ts_i = rows[i]
        for j in range(i + 1, n):
            cid_j, ts_j = rows[j]
            if ts_j - ts_i > COFILL_WINDOW_SECS:
                break
            if cid_i == cid_j:
                continue
            pair = (cid_i, cid_j) if cid_i < cid_j else (cid_j, cid_i)
            pair_counts[pair] = pair_counts.get(pair, 0) + 1

    edges: list[tuple[str, str]] = [
        (a, b) for (a, b), count in pair_counts.items()
        if count >= COFILL_MIN_COUNT
    ]
    if not edges:
        return {}, set()

    # Union-Find
    parent: dict[str, str] = {cid: cid for cid in all_cids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str):
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra > rb:
                ra, rb = rb, ra
            parent[rb] = ra

    for a, b in edges:
        union(a, b)

    clusters_by_root: dict[str, list[str]] = {}
    for cid in sorted(all_cids):
        root = find(cid)
        clusters_by_root.setdefault(root, []).append(cid)

    # FIX 9: Oversized clusters are NOT dissolved. They get a stricter cap
    # applied downstream so the most dangerous chain-link correlations are
    # restrained, not ignored.
    result: dict[str, int] = {}
    oversized_ids: set[int] = set()
    next_id = 0
    for root, members in sorted(clusters_by_root.items()):
        if len(members) < 2:
            continue
        for cid in members:
            result[cid] = next_id
        if len(members) > MAX_CLUSTER_SIZE:
            oversized_ids.add(next_id)
            log.warning(
                f"Cluster {next_id}: {len(members)} markets > "
                f"MAX_CLUSTER_SIZE={MAX_CLUSTER_SIZE} — applying stricter cap"
            )
        next_id += 1

    cluster_sizes: dict[int, int] = {}
    for cluster_id in result.values():
        cluster_sizes[cluster_id] = cluster_sizes.get(cluster_id, 0) + 1
    if cluster_sizes:
        log.info(
            f"Fill clusters: {len(cluster_sizes)} groups, "
            f"largest={max(cluster_sizes.values())}, "
            f"{len(edges)} pairs, {len(oversized_ids)} oversized"
        )

    return result, oversized_ids


def compute_cluster_exposure(
    allocations: list[dict],
    clusters: dict[str, int],
) -> dict[int, float]:
    """Compute total allocated capital per cluster."""
    exposure: dict[int, float] = {}
    for a in allocations:
        if a.get("action") != "deploy":
            continue
        cid = a["condition_id"]
        cluster_id = clusters.get(cid)
        if cluster_id is None:
            continue
        cost = a.get("est_capital_cost", 0)
        exposure[cluster_id] = exposure.get(cluster_id, 0) + cost
    return exposure


def apply_cluster_caps(
    allocations: list[dict],
    clusters: dict[str, int],
    max_cluster_pct: float,
    total_capital: float,
    oversized_cluster_ids: set[int] | None = None,
) -> list[dict]:
    """Scale down allocations in over-allocated clusters.

    Marks capped markets with _cluster_capped=True for redistribution.

    FIX 9: Clusters in `oversized_cluster_ids` use OVERSIZED_CLUSTER_PCT
    (tighter) instead of max_cluster_pct.
    """
    if not clusters:
        return allocations

    oversized = oversized_cluster_ids or set()

    # Per-cluster cap ($) — looked up by cluster_id
    def cluster_cap(cid_int: int) -> float:
        pct = OVERSIZED_CLUSTER_PCT if cid_int in oversized else max_cluster_pct
        return total_capital * pct

    exposure = compute_cluster_exposure(allocations, clusters)

    over_clusters: dict[int, float] = {}
    for cluster_id, total in exposure.items():
        if total > cluster_cap(cluster_id):
            over_clusters[cluster_id] = total

    if not over_clusters:
        return allocations

    for a in allocations:
        if a.get("action") != "deploy":
            continue
        cid = a["condition_id"]
        cluster_id = clusters.get(cid)
        if cluster_id is None or cluster_id not in over_clusters:
            continue

        current_total = over_clusters[cluster_id]
        scale = cluster_cap(cluster_id) / current_total

        old_shares = a["shares_per_side"]
        new_shares = max(int(a.get("min_size", 50)), int(old_shares * scale))
        a["shares_per_side"] = new_shares
        a["_cluster_capped"] = True  # Fix 3: mark for redistribution

        spread = a.get("max_spread", 0.045)
        s = spread if spread > 0 else 0.045
        cpb = 2 * max(0.05, (1.0 - 2 * s) / 2)
        a["est_capital_cost"] = round(new_shares * cpb, 2)

    for cluster_id, total in over_clusters.items():
        cap_dollars = cluster_cap(cluster_id)
        tag = " (oversized)" if cluster_id in oversized else ""
        members = [a["condition_id"] for a in allocations
                    if clusters.get(a["condition_id"]) == cluster_id
                    and a.get("action") == "deploy"]
        log.info(
            f"Cluster {cluster_id}{tag} capped: ${total:.0f} → "
            f"${cap_dollars:.0f} ({len(members)} markets)"
        )

    return allocations
