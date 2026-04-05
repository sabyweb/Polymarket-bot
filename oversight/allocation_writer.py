"""Module 3: Allocation Writer — applies capital constraints and writes JSON.

Takes scored markets from market_scorer, allocates capital within budget,
and writes market_allocations.json atomically (safe for concurrent reads).
"""

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from .market_scorer import ScoredMarket

log = logging.getLogger("oversight.allocator")

# Capital constraints
MAX_PER_MARKET = 200.0
DEFAULT_SHARES = 50
MIN_SHARES = 20


def _est_market_cost(shares: int, max_spread: float) -> float:
    """Estimate capital needed for a market (both sides)."""
    spread = max_spread if max_spread > 0 else 0.045
    est_price_per_share = max(0.10, (1.0 - 2 * spread) / 2)
    return shares * est_price_per_share * 2


def compute_allocations(
    scored_markets: list[ScoredMarket],
    total_capital: float = 1500.0,
    max_per_market: float = MAX_PER_MARKET,
    max_capital_pct: float = 0.15,
) -> list[dict]:
    """Apply capital constraints and compute final allocations.

    Two-pass algorithm:
    1. **Base pass** — each deploy market gets its recommended shares (from scorer).
       Markets are funded in score order until budget runs out.
    2. **Redistribution pass** — if surplus capital remains, redistribute it
       proportionally to top-scoring deployed markets (by score weight).
       This ensures capital isn't left idle when we have profitable markets
       that could absorb more. Each market is capped at max_per_market and
       max_capital_pct of total to prevent over-concentration.

    Args:
        scored_markets: Pre-sorted by score descending from rank_markets.
        total_capital: Total deployable capital.
        max_per_market: Hard cap on capital per market.
        max_capital_pct: Max fraction of total capital per market (default 15%).

    Returns list of allocation dicts ready for JSON serialization.
    """
    per_market_cap = min(max_per_market, total_capital * max_capital_pct)
    allocations = []
    remaining_capital = total_capital

    # ── Pass 1: Base allocation ──
    for sm in scored_markets:
        if sm.action == "avoid":
            allocations.append(_to_dict(sm, shares=0))
            continue

        shares = sm.recommended_shares if sm.recommended_shares > 0 else DEFAULT_SHARES
        spread = getattr(sm, "max_spread", 0.045)
        est_cost = min(_est_market_cost(shares, spread), per_market_cap)

        if est_cost > remaining_capital:
            allocations.append(_to_dict(sm, shares=0, action_override="avoid",
                                        reason_override="Capital budget exhausted"))
            continue

        remaining_capital -= est_cost
        allocations.append(_to_dict(sm, shares=shares))

    # ── Pass 2: Redistribute surplus capital ──
    # If >10% of budget is undeployed and we have deployed markets, spread
    # the surplus across top markets proportional to their score.
    deployed_indices = [
        i for i, a in enumerate(allocations)
        if a["action"] == "deploy" and a["score"] > 0
    ]
    surplus_threshold = total_capital * 0.10

    if remaining_capital > surplus_threshold and deployed_indices:
        # Weight by score (min 0.01 to avoid division by zero)
        scores = [max(allocations[i]["score"], 0.01) for i in deployed_indices]
        total_score = sum(scores)

        redistrib_count = 0
        for idx, s in zip(deployed_indices, scores):
            a = allocations[idx]
            spread = a.get("max_spread", 0.045)
            est_price = max(0.10, (1.0 - 2 * spread) / 2)
            current_cost = _est_market_cost(a["shares_per_side"], spread)

            # How much more can this market absorb?
            headroom = per_market_cap - current_cost
            if headroom <= 0:
                continue

            # Score-proportional share of the surplus
            share_of_surplus = remaining_capital * (s / total_score)
            extra_capital = min(share_of_surplus, headroom)
            if extra_capital < est_price * 2:
                continue  # not enough for even 1 extra share per side

            extra_shares = int(extra_capital / (est_price * 2))
            if extra_shares < 1:
                continue

            new_shares = a["shares_per_side"] + extra_shares
            new_cost = _est_market_cost(new_shares, spread)
            actual_extra = _est_market_cost(extra_shares, spread)

            allocations[idx]["shares_per_side"] = new_shares
            allocations[idx]["reason"] += f" (+{extra_shares}sh redistrib)"
            remaining_capital -= actual_extra
            redistrib_count += 1

        if redistrib_count > 0:
            log.info(
                f"Redistribution: boosted {redistrib_count} markets, "
                f"${total_capital - remaining_capital:.0f} now deployed"
            )

    deployed = [a for a in allocations if a["action"] == "deploy"]
    avoided = [a for a in allocations if a["action"] == "avoid"]
    total_deployed = total_capital - remaining_capital

    log.info(
        f"Allocation: {len(deployed)} deploy, {len(avoided)} avoid | "
        f"${total_deployed:.0f} of ${total_capital:.0f} deployed"
    )

    return allocations


def _to_dict(
    sm: ScoredMarket,
    shares: int,
    action_override: str = "",
    reason_override: str = "",
) -> dict:
    """Convert ScoredMarket to allocation dict."""
    return {
        "condition_id": sm.condition_id,
        "question": sm.question,
        "action": action_override or sm.action,
        "shares_per_side": shares,
        "score": round(sm.score, 6),
        "reason": reason_override or sm.reason,
        "confidence": sm.confidence,
        "actual_reward_total": round(sm.actual_reward_total, 4),
        "fill_damage": round(sm.fill_damage, 2),
        "fill_count": sm.fill_count,
        "daily_rate": sm.daily_rate,
        "min_size": sm.min_size,
        "max_spread": sm.max_spread,
    }


def write_allocations(
    allocations: list[dict],
    total_capital_deployed: float,
    output_path: str = "market_allocations.json",
) -> None:
    """Write allocations to JSON file atomically.

    Uses write-to-temp + os.replace() for POSIX-atomic writes.
    The farmer bot never sees a partial file.
    """
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": "1.0",
        "total_capital_deployed": round(total_capital_deployed, 2),
        "num_deploy": sum(1 for a in allocations if a["action"] == "deploy"),
        "num_avoid": sum(1 for a in allocations if a["action"] == "avoid"),
        "markets": allocations,
    }

    # Atomic write
    dir_name = os.path.dirname(output_path) or "."
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".json.tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, output_path)
        log.info(f"Wrote allocations to {output_path}")
    except Exception as e:
        log.error(f"Failed to write allocations: {e}")
        # Clean up temp file if it exists
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def generate_summary(allocations: list[dict]) -> str:
    """Generate human-readable summary for logging."""
    deploy = [a for a in allocations if a["action"] == "deploy"]
    avoid = [a for a in allocations if a["action"] == "avoid"]

    lines = []
    lines.append(f"=== OVERSIGHT AGENT ALLOCATION ===")
    lines.append(f"Deploy: {len(deploy)} markets | Avoid: {len(avoid)} markets")
    lines.append("")

    if deploy:
        lines.append("TOP DEPLOY:")
        for a in deploy[:10]:
            lines.append(
                f"  {a['question'][:45]:<45s} | ${a['daily_rate']:>4.0f}/d | "
                f"score={a['score']:.4f} | {a['shares_per_side']}sh | "
                f"fills={a['fill_count']} | {a['confidence']}"
            )

    if avoid:
        lines.append("\nTOP AVOID:")
        for a in avoid[:5]:
            lines.append(
                f"  {a['question'][:45]:<45s} | {a['reason'][:40]}"
            )

    return "\n".join(lines)
