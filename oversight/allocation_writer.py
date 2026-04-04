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
EST_PRICE_PER_SHARE = 0.30  # rough average for capital estimation


def compute_allocations(
    scored_markets: list[ScoredMarket],
    total_capital: float = 1500.0,
    max_per_market: float = MAX_PER_MARKET,
) -> list[dict]:
    """Apply capital constraints and compute final allocations.

    Algorithm:
    1. Take "deploy" markets sorted by score (already sorted from scorer)
    2. Each gets base_shares (DEFAULT_SHARES or their recommended_shares)
    3. Walk down list, accumulating capital
    4. When budget exhausted, remaining markets demoted to "avoid"

    Returns list of allocation dicts ready for JSON serialization.
    """
    allocations = []
    remaining_capital = total_capital

    for sm in scored_markets:
        if sm.action == "avoid":
            allocations.append(_to_dict(sm, shares=0))
            continue

        shares = sm.recommended_shares if sm.recommended_shares > 0 else DEFAULT_SHARES

        # Estimate capital for this market (both sides)
        est_cost = shares * EST_PRICE_PER_SHARE * 2
        est_cost = min(est_cost, max_per_market)

        if est_cost > remaining_capital:
            # Budget exhausted — demote
            allocations.append(_to_dict(sm, shares=0, action_override="avoid",
                                        reason_override="Capital budget exhausted"))
            continue

        remaining_capital -= est_cost
        allocations.append(_to_dict(sm, shares=shares))

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
