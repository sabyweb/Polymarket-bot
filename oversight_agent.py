#!/usr/bin/env python3
"""Oversight Agent — intelligent capital allocation for reward farming.

Runs alongside the farmer bot. Analyzes actual reward earnings vs fill costs
per market, scores and ranks markets, and writes allocation recommendations
to market_allocations.json which the farmer bot reads at each refresh.

Usage:
    python oversight_agent.py                  # one-shot analysis + write
    python oversight_agent.py --loop           # continuous hourly
    python oversight_agent.py --dry-run        # analyze + print, no write
    python oversight_agent.py --interval 1800  # custom interval (seconds)
    python oversight_agent.py --hours 12       # lookback window (hours)
"""

import argparse
import logging
import os
import signal
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("oversight")


def _write_performance_snapshots(
    db_path: str,
    scored: list,
    metrics: list,
    correction_factor: float,
    hours: float,
) -> None:
    """Write per-market performance snapshots for adaptive tracking.

    This builds the historical dataset that enables the agent to learn
    which markets are actually profitable over time, not just estimated to be.
    """
    from database import BotDatabase
    try:
        db = BotDatabase(db_path)
        now = time.time()

        # Build metrics lookup by condition_id
        metrics_by_cid = {m.condition_id: m for m in metrics}

        snapshots = []
        for sm in scored:
            m = metrics_by_cid.get(sm.condition_id)
            if not m:
                continue
            estimated_daily = m.daily_rate * m.q_share_pct
            snapshots.append({
                "ts": now,
                "condition_id": sm.condition_id,
                "question": sm.question[:100],
                "window_hours": hours,
                "estimated_daily": estimated_daily,
                "correction_factor": correction_factor,
                "corrected_daily": estimated_daily * correction_factor,
                "fill_cost": m.fill_cost_recent,
                "dump_revenue": m.dump_revenue_recent,
                "net_score": sm.score,
                "action": sm.action,
                "q_share_pct": m.q_share_pct,
                "on_book_hours": m.on_book_hours,
                "fill_count": m.fill_count_recent,
                "shares_recommended": sm.recommended_shares,
            })

        db.save_performance_batch(snapshots)
        log.info(f"Saved {len(snapshots)} performance snapshots to DB")

        # Log performance summary if we have history
        summary = db.get_performance_summary(days=7)
        if summary and summary.get("total_snapshots", 0) > 1:
            log.info(
                f"7-day performance: {summary.get('unique_markets', 0)} markets tracked, "
                f"avg_correction={summary.get('avg_correction', 1.0):.2f}, "
                f"avg_score={summary.get('avg_score', 0):.4f}"
            )

    except Exception as e:
        log.warning(f"Performance snapshot write failed: {e}")


def run_once(
    db_path: str = "bot_history.db",
    output_path: str = "market_allocations.json",
    hours: float = 24,
    capital: float = 1500.0,
    dry_run: bool = False,
) -> dict:
    """One full cycle: collect → score → allocate → write.

    Returns summary dict for logging/testing.
    """
    from oversight.data_collector import collect_all, compute_available_capital
    from oversight.market_scorer import rank_markets
    from oversight.allocation_writer import (
        compute_allocations, write_allocations, generate_summary,
    )

    # Step 1: Compute available capital (total minus locked positions)
    available_capital = compute_available_capital(db_path, total_capital=capital)

    # Step 2: Collect metrics + correction factor
    log.info(f"Collecting metrics (lookback={hours:.0f}h, db={db_path})...")
    metrics, correction_factor = collect_all(db_path=db_path, hours=hours)

    if not metrics:
        log.warning("No market data collected — skipping allocation")
        return {"status": "no_data", "markets": 0}

    # Step 3: Score (with correction factor from actual payouts)
    log.info(f"Scoring {len(metrics)} markets (reward correction={correction_factor:.2f})...")
    scored = rank_markets(metrics, hours=hours, correction_factor=correction_factor, db_path=db_path)

    # Step 4: Allocate with ACTUAL available capital
    log.info(f"Computing allocations (${available_capital:.0f} available)...")
    allocations = compute_allocations(scored, total_capital=available_capital)
    total_deployed = sum(
        a.get("shares_per_side", 0) * 0.30 * 2
        for a in allocations if a["action"] == "deploy"
    )

    # Step 5: Write performance snapshots (adaptive tracking)
    _write_performance_snapshots(
        db_path, scored, metrics, correction_factor, hours
    )

    # Step 6: Summary
    summary = generate_summary(allocations)
    print(summary)

    # Step 7: Write allocations (unless dry-run)
    if dry_run:
        log.info("[DRY RUN] Would write allocations — skipping")
    else:
        write_allocations(allocations, total_deployed, output_path)

    deploy_count = sum(1 for a in allocations if a["action"] == "deploy")
    avoid_count = sum(1 for a in allocations if a["action"] == "avoid")

    return {
        "status": "ok",
        "markets_total": len(metrics),
        "markets_deploy": deploy_count,
        "markets_avoid": avoid_count,
        "correction_factor": correction_factor,
        "dry_run": dry_run,
    }


def run_loop(
    interval_secs: int = 3600,
    **kwargs,
) -> None:
    """Continuous loop. Runs run_once every interval_secs.

    Handles SIGINT/SIGTERM for clean shutdown. Never crashes the loop.
    """
    shutdown = False

    def _sig(signum, frame):
        nonlocal shutdown
        shutdown = True
        log.info("Shutdown requested...")

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    log.info(f"Oversight agent starting (interval={interval_secs}s)")

    while not shutdown:
        try:
            result = run_once(**kwargs)
            log.info(f"Cycle complete: {result}")
        except Exception as e:
            log.error(f"Cycle failed: {e}")

        # Sleep in 1s intervals for responsive shutdown
        for _ in range(interval_secs):
            if shutdown:
                break
            time.sleep(1)

    log.info("Oversight agent stopped")


def main():
    parser = argparse.ArgumentParser(
        description="Oversight Agent — intelligent capital allocation"
    )
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--dry-run", action="store_true", help="Analyze only, don't write")
    parser.add_argument("--interval", type=int, default=3600, help="Loop interval in seconds")
    parser.add_argument("--hours", type=float, default=24, help="Lookback window in hours")
    parser.add_argument("--capital", type=float, default=1500.0, help="Total capital available")
    parser.add_argument("--db", default="bot_history.db", help="Path to bot_history.db")
    parser.add_argument("--output", default="market_allocations.json", help="Output path")
    args = parser.parse_args()

    if args.loop:
        run_loop(
            interval_secs=args.interval,
            db_path=args.db,
            output_path=args.output,
            hours=args.hours,
            capital=args.capital,
            dry_run=args.dry_run,
        )
    else:
        result = run_once(
            db_path=args.db,
            output_path=args.output,
            hours=args.hours,
            capital=args.capital,
            dry_run=args.dry_run,
        )
        log.info(f"Done: {result}")


if __name__ == "__main__":
    main()
