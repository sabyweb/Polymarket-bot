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


def run_once(
    db_path: str = "bot_history.db",
    output_path: str = "market_allocations.json",
    hours: float = 24,
    dry_run: bool = False,
) -> dict:
    """One full cycle: collect → score → allocate → write.

    Returns summary dict for logging/testing.
    """
    from oversight.data_collector import collect_all
    from oversight.market_scorer import rank_markets
    from oversight.allocation_writer import (
        compute_allocations, write_allocations, generate_summary,
        TOTAL_CAPITAL_LIMIT,
    )

    # Step 1: Collect
    log.info(f"Collecting metrics (lookback={hours:.0f}h, db={db_path})...")
    metrics = collect_all(db_path=db_path, hours=hours)

    if not metrics:
        log.warning("No market data collected — skipping allocation")
        return {"status": "no_data", "markets": 0}

    # Step 2: Score
    log.info(f"Scoring {len(metrics)} markets...")
    scored = rank_markets(metrics, hours=hours)

    # Step 3: Allocate
    log.info("Computing allocations...")
    allocations = compute_allocations(scored)
    total_deployed = TOTAL_CAPITAL_LIMIT - sum(
        0 for a in allocations if a["action"] == "avoid"
    )

    # Step 4: Summary
    summary = generate_summary(allocations)
    print(summary)

    # Step 5: Write (unless dry-run)
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
    parser.add_argument("--db", default="bot_history.db", help="Path to bot_history.db")
    parser.add_argument("--output", default="market_allocations.json", help="Output path")
    args = parser.parse_args()

    if args.loop:
        run_loop(
            interval_secs=args.interval,
            db_path=args.db,
            output_path=args.output,
            hours=args.hours,
            dry_run=args.dry_run,
        )
    else:
        result = run_once(
            db_path=args.db,
            output_path=args.output,
            hours=args.hours,
            dry_run=args.dry_run,
        )
        log.info(f"Done: {result}")


if __name__ == "__main__":
    main()
