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
COFILL_WINDOW_SECS = 300      # 5-minute window
COFILL_MIN_COUNT = 2          # Fix 2: lowered from 3 to catch real correlations
COFILL_LOOKBACK_SECS = 86400  # 24h lookback

# Fix 4: Chain-link explosion guard
MAX_CLUSTER_SIZE = 10

# Default cluster cap
DEFAULT_MAX_CLUSTER_PCT = 0.30


def build_fill_clusters(db_path: str) -> dict[str, int]:
    """Build clusters of correlated markets from co-fill patterns.

    Two markets are in the same cluster if they received fills within
    the same 5-minute window ≥ COFILL_MIN_COUNT times in the last 24h.

    Clusters exceeding MAX_CLUSTER_SIZE are dissolved (markets treated
    as unclustered) to prevent chain-link explosion.

    Returns: {market_id: cluster_id}. Only markets in multi-market
    clusters are included. Singleton markets are omitted.
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
        return {}

    if not rows:
        return {}

    # Step 1: Bucket fills into 5-min windows
    buckets: dict[int, set[str]] = {}
    all_cids: set[str] = set()
    for cid, ts in rows:
        bucket_key = int(ts // COFILL_WINDOW_SECS)
        buckets.setdefault(bucket_key, set()).add(cid)
        all_cids.add(cid)

    # Step 2: Count co-fill occurrences between pairs
    pair_counts: dict[tuple[str, str], int] = {}
    for bucket_cids in buckets.values():
        if len(bucket_cids) < 2:
            continue
        cids_sorted = sorted(bucket_cids)
        for i in range(len(cids_sorted)):
            for j in range(i + 1, len(cids_sorted)):
                pair = (cids_sorted[i], cids_sorted[j])
                pair_counts[pair] = pair_counts.get(pair, 0) + 1

    # Step 3: Build edges for pairs with ≥ COFILL_MIN_COUNT co-fills
    edges: list[tuple[str, str]] = []
    for (a, b), count in pair_counts.items():
        if count >= COFILL_MIN_COUNT:
            edges.append((a, b))

    if not edges:
        return {}

    # Step 4: Union-Find
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

    # Step 5: Collect clusters
    clusters_by_root: dict[str, list[str]] = {}
    for cid in sorted(all_cids):
        root = find(cid)
        clusters_by_root.setdefault(root, []).append(cid)

    # Step 6: Assign cluster IDs, enforcing size guard (Fix 4)
    result: dict[str, int] = {}
    next_id = 0
    oversized_count = 0

    for root, members in sorted(clusters_by_root.items()):
        if len(members) < 2:
            continue  # skip singletons — not correlated
        if len(members) > MAX_CLUSTER_SIZE:
            # Fix 4: dissolve oversized clusters
            oversized_count += 1
            log.warning(
                f"Cluster dissolved: {len(members)} markets > MAX_CLUSTER_SIZE={MAX_CLUSTER_SIZE} "
                f"(root={root[:12]})"
            )
            continue
        for cid in members:
            result[cid] = next_id
        next_id += 1

    # Log stats
    cluster_sizes: dict[int, int] = {}
    for cluster_id in result.values():
        cluster_sizes[cluster_id] = cluster_sizes.get(cluster_id, 0) + 1
    multi_clusters = {k: v for k, v in cluster_sizes.items() if v > 1}
    if multi_clusters or oversized_count:
        log.info(
            f"Fill clusters: {len(multi_clusters)} groups, "
            f"largest={max(multi_clusters.values()) if multi_clusters else 0}, "
            f"{len(edges)} pairs, {oversized_count} dissolved"
        )

    return result


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
) -> list[dict]:
    """Scale down allocations in over-allocated clusters.

    Marks capped markets with _cluster_capped=True for redistribution.
    """
    if not clusters:
        return allocations

    max_cluster_capital = total_capital * max_cluster_pct
    exposure = compute_cluster_exposure(allocations, clusters)

    over_clusters: dict[int, float] = {}
    for cluster_id, total in exposure.items():
        if total > max_cluster_capital:
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
        scale = max_cluster_capital / current_total

        old_shares = a["shares_per_side"]
        new_shares = max(int(a.get("min_size", 50)), int(old_shares * scale))
        a["shares_per_side"] = new_shares
        a["_cluster_capped"] = True  # Fix 3: mark for redistribution

        spread = a.get("max_spread", 0.045)
        s = spread if spread > 0 else 0.045
        cpb = 2 * max(0.05, (1.0 - 2 * s) / 2)
        a["est_capital_cost"] = round(new_shares * cpb, 2)

    for cluster_id, total in over_clusters.items():
        members = [a["condition_id"] for a in allocations
                    if clusters.get(a["condition_id"]) == cluster_id
                    and a.get("action") == "deploy"]
        log.info(
            f"Cluster {cluster_id} capped: ${total:.0f} → "
            f"${max_cluster_capital:.0f} ({len(members)} markets)"
        )

    return allocations
