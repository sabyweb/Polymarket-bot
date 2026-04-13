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


def _phase0_daily_attribution(
    db_path: str,
    metrics: list,
    correction_factor: float,
) -> None:
    """Phase 0: Write daily reward payout + per-market portfolio context.

    Uses exact totals from Data API (REWARD + MAKER_REBATE) and records
    what the portfolio looked like that day. No circular attribution —
    the model learns the mapping from portfolio context → total payout.
    """
    import sqlite3
    from datetime import datetime, timezone

    try:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Fetch today's actual payouts (both streams)
        from oversight.data_collector import fetch_reward_correction_factor
        total_combined = fetch_reward_correction_factor(hours=24)

        # Split REWARD vs MAKER_REBATE for the record
        import requests as rq
        funder = os.getenv("FUNDER", "") or os.getenv("WALLET_ADDRESS", "")
        cutoff_ts = time.time() - 24 * 3600
        reward_total = 0.0
        rebate_total = 0.0
        for ptype, accumulator_name in [("REWARD", "reward_total"), ("MAKER_REBATE", "rebate_total")]:
            off = 0
            while True:
                resp = rq.get(
                    "https://data-api.polymarket.com/activity",
                    params={"user": funder, "type": ptype, "limit": 500, "offset": off},
                    timeout=15,
                )
                if resp.status_code != 200:
                    break
                data = resp.json()
                if not data:
                    break
                for item in data:
                    ts = float(item.get("timestamp", 0))
                    if ts < cutoff_ts:
                        continue
                    amount = float(item.get("usdcSize", 0) or item.get("amount", 0))
                    if amount > 0:
                        if ptype == "REWARD":
                            reward_total += amount
                        else:
                            rebate_total += amount
                if len(data) < 500:
                    break
                off += 500
                time.sleep(0.2)

        # Estimated daily total from Q-score model
        est_total = sum(m.daily_rate * m.q_share_pct for m in metrics if m.q_share_pct > 0)
        num_active = sum(1 for m in metrics if m.on_book_hours > 0)

        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute(
            "INSERT OR REPLACE INTO reward_daily "
            "(date, total_reward_usd, total_rebate_usd, total_combined_usd, "
            "num_markets_active, est_daily_total, correction_factor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date_str, reward_total, rebate_total, reward_total + rebate_total,
             num_active, est_total, correction_factor),
        )

        # Per-market portfolio context
        # Use scoring_snapshots from today if available for scoring_seconds
        scoring_by_cid = {}
        try:
            today_start = time.time() - 24 * 3600
            rows = conn.execute(
                "SELECT condition_id, SUM(scoring) as scoring_cycles, COUNT(*) as total_cycles "
                "FROM scoring_snapshots WHERE ts >= ? GROUP BY condition_id",
                (today_start,),
            ).fetchall()
            for r in rows:
                cid = r[0]
                # Each snapshot is ~2.5 min apart (every 5 cycles × 30s)
                scoring_by_cid[cid] = r[1] * 150  # scoring_cycles × 150 seconds
        except Exception:
            pass  # table may not exist yet

        market_rows = []
        for m in metrics:
            if m.on_book_hours <= 0:
                continue
            market_rows.append((
                date_str, m.condition_id,
                scoring_by_cid.get(m.condition_id, 0),
                0, 0,  # avg_bid_size, avg_ask_size (enriched later from book_snapshots)
                0, 0,  # avg_spread, avg_midpoint (enriched later)
                m.daily_rate, m.max_spread,
                m.fill_count_recent,
            ))
        if market_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO reward_daily_markets "
                "(date, condition_id, scoring_seconds, avg_bid_size, avg_ask_size, "
                "avg_spread, avg_midpoint, daily_rate, max_spread_cfg, fill_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                market_rows,
            )

        conn.commit()
        conn.close()
        log.info(
            f"Phase0 attribution: ${reward_total:.2f} reward + ${rebate_total:.2f} rebate "
            f"= ${reward_total + rebate_total:.2f} | {num_active} markets | {date_str}"
        )
    except Exception as e:
        log.warning(f"Phase0 daily attribution failed: {e}")


def _phase0_prune(db_path: str) -> None:
    """Phase 0: Prune old book/scoring snapshots (keep 7 days)."""
    try:
        from database import BotDatabase
        db = BotDatabase(db_path)
        db.prune_phase0_data(retention_days=7)
    except Exception as e:
        log.debug(f"Phase0 prune failed: {e}")


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

    # Step 1: Compute available capital
    # Prefer actual USDC balance from exchange (written by bot every ~5 min)
    # over the hardcoded --capital flag. This makes the agent balance-aware.
    from database import get_db
    exchange_bal = None
    try:
        _db = get_db()
        _bal, _ts = _db.load_usdc_balance()
        if _bal is not None:
            import time as _time
            age_min = (_time.time() - _ts) / 60
            if age_min < 30:  # only trust balance data < 30 min old
                exchange_bal = _bal
                log.info(
                    f"Exchange USDC balance: ${_bal:.2f} (age={age_min:.0f}m)"
                )
            else:
                log.warning(
                    f"Exchange balance stale ({age_min:.0f}m old) — "
                    f"falling back to --capital=${capital:.0f}"
                )
    except Exception as e:
        log.warning(f"Could not read exchange balance: {e}")

    available_capital = compute_available_capital(
        db_path, total_capital=capital, exchange_balance=exchange_bal,
    )

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
    raw_deployed = sum(
        a.get("shares_per_side", 0)
        * max(0.10, (1.0 - 2 * a.get("max_spread", 0.045)) / 2)
        * 2
        for a in allocations if a["action"] == "deploy"
    )
    # Cap at available capital — the allocator doesn't enforce budget
    # (bot stops on exchange balance error), but the written total should
    # reflect reality, not the sum of all deploy markets.
    total_deployed = min(raw_deployed, available_capital)

    # Step 5: Write performance snapshots (adaptive tracking)
    _write_performance_snapshots(
        db_path, scored, metrics, correction_factor, hours
    )

    # Step 5b: Closed-loop feedback — compare allocation vs what bot actually placed
    from oversight.data_collector import query_placement_feedback
    feedback = query_placement_feedback(db_path)
    deploy_cids = {a["condition_id"] for a in allocations if a["action"] == "deploy"}
    placed_count = 0
    skip_reasons: dict[str, int] = {}
    for cid in deploy_cids:
        fb = feedback.get(cid, {})
        yes_placed = fb.get("yes", {}).get("status") == "placed"
        no_placed = fb.get("no", {}).get("status") == "placed"
        if yes_placed or no_placed:
            placed_count += 1
        else:
            # Both sides skipped or no feedback yet
            for side in ["yes", "no"]:
                reason = fb.get(side, {}).get("reason", "no_feedback")
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
    if deploy_cids:
        log.info(
            f"Feedback: allocated {len(deploy_cids)}, bot placed {placed_count}, "
            f"skipped {len(deploy_cids) - placed_count} ({dict(skip_reasons)})"
        )

    # Step 6: Phase 0 — daily reward attribution + pruning
    _phase0_daily_attribution(db_path, metrics, correction_factor)
    _phase0_prune(db_path)

    # Step 7: Summary
    summary = generate_summary(allocations)
    print(summary)

    # Step 8: Write allocations (unless dry-run)
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
    parser.add_argument("--interval", type=int, default=1800, help="Loop interval in seconds (default: 30min)")
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
