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
import collections
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

        from oversight.data_collector import _connect_db
        conn = _connect_db(db_path)
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
    capital: float | None = None,
    dry_run: bool = False,
) -> dict:
    """One full cycle: collect → score → allocate → safety check → write.

    Returns summary dict for logging/testing.

    Capital resolution (FX-013 / FX-025): the agent prefers a fresh
    ``usdc_balance`` row written by the farmer (`<30 min` old). If that
    is missing or stale and ``capital`` is None, the cycle is skipped
    with a ``[CAPITAL_SOURCE] source=none`` log line. If ``capital`` is
    an explicit operator override, the cycle proceeds with that value.
    No silent ``$1500`` fallback any more.
    """
    from oversight.data_collector import collect_all, compute_available_capital
    from oversight.market_scorer import rank_markets
    from oversight.allocation_writer import (
        compute_allocations, write_allocations, generate_summary,
    )
    from oversight.safety_controller import SafetyController, UNSAFE

    # Initialize safety controller (loads persisted state from DB)
    safety = SafetyController(db_path=db_path)

    # Step 1: Compute available capital
    # FX-013 / FX-024: prefer fresh `usdc_balance` row written by the farmer
    # (cycle 1 + every 10 cycles thereafter, ~5 min). When stale or absent,
    # fall through to the operator's `--capital` override; if neither is
    # available, skip the cycle rather than silently using a hardcoded
    # `$1500`. Every decision emits a structured `[CAPITAL_SOURCE]` line so
    # the operator sees which path was used.
    from database import get_db
    exchange_bal = None
    capital_age_min = None
    try:
        _db = get_db()
        _bal, _ts = _db.load_usdc_balance()
        if _bal is not None:
            import time as _time
            age_min = (_time.time() - _ts) / 60
            if age_min < 30:  # only trust balance data < 30 min old
                exchange_bal = _bal
                capital_age_min = age_min
    except Exception as e:
        log.warning(f"Could not read exchange balance: {e}")

    # Decide the source + emit structured log
    if exchange_bal is not None:
        log.info(
            f"[CAPITAL_SOURCE] source=usdc_db value=${exchange_bal:.2f} "
            f"age_min={capital_age_min:.0f}"
        )
    elif capital is not None:
        log.warning(
            f"[CAPITAL_SOURCE] source=flag value=${capital:.2f} age_min=- "
            f"(operator override; no fresh usdc_balance row)"
        )
    else:
        log.warning(
            "[CAPITAL_SOURCE] source=none value=- age_min=- "
            "(no fresh usdc_balance row AND --capital not provided — "
            "skipping cycle; pass --capital=X to override)"
        )
        return {"status": "no_capital", "markets": 0}

    available_capital = compute_available_capital(
        db_path, total_capital=capital, exchange_balance=exchange_bal,
    )

    # Step 2: Collect metrics + correction factor
    log.info(f"Collecting metrics (lookback={hours:.0f}h, db={db_path})...")
    collect_result = collect_all(db_path=db_path, hours=hours)

    # collect_all returns 5 values: (metrics, cf, rate_delta, completeness, actual_daily_total)
    if len(collect_result) >= 5:
        metrics, correction_factor, clob_rate_delta, data_completeness, actual_daily_payout = collect_result[:5]
    elif len(collect_result) == 4:
        metrics, correction_factor, clob_rate_delta, data_completeness = collect_result
        actual_daily_payout = 0.0
    else:
        # Backward compat if old collect_all returns 2
        metrics, correction_factor = collect_result[0], collect_result[1]
        clob_rate_delta, data_completeness, actual_daily_payout = 0.0, 1.0, 0.0

    if not metrics:
        log.warning("No market data collected — skipping allocation")
        return {"status": "no_data", "markets": 0}

    # Step 2b: Safety evaluation — check system state BEFORE scoring
    # GAP 1 FIX: Use DEPLOYED markets only for est/actual ratio.
    # collect_all() already computes this internally. We replicate the
    # logic here for the safety controller's independent check.
    from oversight.data_collector import _load_deployed_cids
    deployed_cids, _probe_cids = _load_deployed_cids(db_path)
    if deployed_cids:
        estimated_daily_total = sum(
            m.daily_rate * min(m.q_share_pct, 0.5)
            for m in metrics
            if m.q_share_pct > 0 and m.condition_id in deployed_cids
        )
    else:
        # No deployment data — use on-book markets as proxy
        estimated_daily_total = sum(
            m.daily_rate * min(m.q_share_pct, 0.5)
            for m in metrics
            if m.q_share_pct > 0 and m.on_book_hours > 0
        )
    # actual_daily_payout already available from collect_all (5th return value)
    fill_damage_24h = safety.query_24h_fill_damage()
    fill_damage_7d = safety.query_7d_fill_damage()
    num_scoring = safety.count_scoring_markets()

    # Compute raw CF for safety evaluation (before smoothing)
    raw_cf = 0.0
    if actual_daily_payout > 0 and estimated_daily_total > 0:
        actual_per_day = actual_daily_payout / max(hours / 24, 0.1)
        raw_cf = actual_per_day / estimated_daily_total

    # Compute total portfolio value for safety layer drawdown tracking
    _total_portfolio = safety._compute_portfolio_value(exchange_bal or 0.0)

    safety_state = safety.evaluate_state(
        correction_factor_raw=raw_cf,
        estimated_daily_total=estimated_daily_total,
        actual_daily_payout=actual_daily_payout,
        reward_payout_24h=actual_daily_payout,
        num_scoring_markets=num_scoring,
        fill_damage_24h=fill_damage_24h,
        fill_damage_7d=fill_damage_7d,
        clob_rate_delta_pct=clob_rate_delta,
        data_completeness=data_completeness,
        exchange_balance=exchange_bal or 0.0,
        total_portfolio_value=_total_portfolio,
    )
    log.info(
        f"Safety state: {safety_state} | CF_raw={raw_cf:.4f} | "
        f"est_daily=${estimated_daily_total:.2f} | actual=${actual_daily_payout:.2f} | "
        f"fill_damage_24h=${fill_damage_24h:.2f} | fill_damage_7d=${fill_damage_7d:.2f} | "
        f"scoring_markets={num_scoring} | rate_delta={clob_rate_delta:+.1%} | "
        f"data_completeness={data_completeness:.0%}"
    )

    # Step 2c: Calibration layer — retrain models and produce EV estimates
    from calibration import CalibrationManager
    calibrator = CalibrationManager(db_path=db_path)
    try:
        cal_metrics = calibrator.retrain(correction_factor=correction_factor)
        log.info(
            f"Calibration: ready={calibrator.is_ready()} | "
            f"fill={cal_metrics.get('fill_model', {}).get('status', '?')} | "
            f"loss={cal_metrics.get('loss_model', {}).get('status', '?')} | "
            f"hazard={cal_metrics.get('hazard_model', {}).get('status', '?')} | "
            f"reward={cal_metrics.get('reward_model', {}).get('status', '?')}"
        )
    except Exception as e:
        log.warning(f"Calibration retrain failed: {e}")
        calibrator = None

    # Step 3: Score (with correction factor from actual payouts + calibration)
    log.info(f"Scoring {len(metrics)} markets (reward correction={correction_factor:.4f})...")
    scored = rank_markets(
        metrics, hours=hours, correction_factor=correction_factor,
        db_path=db_path, calibrator=calibrator,
    )

    # Step 3b: Learning loop — real-time behavior correction.
    # Runs ALWAYS (safe by design: OFF/SHADOW publish neutral state). The
    # applied_state is the ONLY thing that can influence downstream
    # decisions — OFF/SHADOW modes are guaranteed to produce all-1.0
    # scalars so behavior is identical to the prior system until the
    # gate promotes to ACTIVE.
    from profit.learning import LearningController
    try:
        learn_ctrl = LearningController(db_path=db_path)
        learn_step = learn_ctrl.step()
        learning_state = learn_step.applied_state
        if calibrator is not None:
            # Propagate reward_trust into the calibrator's PART 6 reward
            # pipeline. 1.0 in OFF/SHADOW → no effect.
            calibrator.reward_trust = learning_state.reward_trust
    except Exception as e:
        log.warning(f"Learning loop failed (proceeding neutral): {e}")
        learning_state = None

    # Step 4: Allocate — profit engine when calibrator ready, else legacy.
    # capital_scale is applied uniformly across both paths so the allocator
    # sees a single total_capital input. Neutral (1.0) when learning_state
    # is None or in OFF/SHADOW; non-neutral only after the LearningController
    # gate promotes to ACTIVE.
    cap_scale = 1.0
    if learning_state is not None:
        cap_scale = float(getattr(learning_state, "capital_scale", 1.0))
    alloc_capital = available_capital * cap_scale

    if calibrator is not None and calibrator.is_ready():
        from profit import allocate_portfolio
        log.info(
            f"Profit engine allocation "
            f"(${available_capital:.0f} × cap_scale {cap_scale:.2f} = "
            f"${alloc_capital:.0f})..."
        )
        allocations = allocate_portfolio(
            scored_markets=scored,
            total_capital=alloc_capital,
            calibrator=calibrator,
            db_path=db_path,
            learning_state=learning_state,
        )
    else:
        log.info(
            f"Legacy allocation "
            f"(${available_capital:.0f} × cap_scale {cap_scale:.2f} = "
            f"${alloc_capital:.0f})..."
        )
        allocations = compute_allocations(scored, total_capital=alloc_capital)

    # Step 4b: SAFETY GATE — filter allocations based on system state
    allocations = safety.filter_allocations(allocations, available_capital)

    # Persist deployed CIDs to DB so collect_all can read them next cycle
    # without depending on the JSON allocation file.
    from oversight.data_collector import persist_deployed_cids
    _deploy_cids = {a["condition_id"] for a in allocations if a["action"] == "deploy"}
    _probe_cids_out = {
        a["condition_id"] for a in allocations
        if a["action"] == "deploy" and str(a.get("reason", "")).startswith("PROBE:")
    }
    persist_deployed_cids(db_path, _deploy_cids, _probe_cids_out)

    raw_deployed = sum(
        a.get("shares_per_side", 0)
        * max(0.10, (1.0 - 2 * a.get("max_spread", 0.045)) / 2)
        * 2
        for a in allocations if a["action"] == "deploy"
    )
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

    # Step 6b: Phase 4 — daily bandit posterior update + per-market
    # reward attribution. Both consume the last 24h of PnL + payout data
    # that was just written by step 6 / the bot's live DB writes.
    # Failures here must not crash the cycle (fail-closed: no bandit
    # update simply means posteriors stay where they were).
    try:
        from profit.bandit import Bandit
        Bandit(db_path).update()
    except Exception as e:
        log.warning(f"Bandit update failed: {e}")

    try:
        from calibration.attribution import compute_attribution
        compute_attribution(db_path)
    except Exception as e:
        log.warning(f"Reward attribution failed: {e}")

    # Step 7: Summary
    summary = generate_summary(allocations)
    print(summary)
    print(f"\n[Safety: {safety_state}]")

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
        "safety_state": safety_state,
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
        # FX-015: structured `[SHUTDOWN]` log channel + name the signal so
        # the operator can tell SIGINT (Ctrl+C) from SIGTERM (systemctl)
        # in journalctl. The agent doesn't trade, so there's nothing to
        # cancel — flipping ``shutdown`` is enough to exit the main loop
        # at the next iteration.
        nonlocal shutdown
        name = (
            "SIGINT" if signum == signal.SIGINT
            else "SIGTERM" if signum == signal.SIGTERM
            else f"signal {signum}"
        )
        shutdown = True
        log.info(f"[SHUTDOWN] {name} received — exiting loop")

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

    log.info("[SHUTDOWN] Oversight agent stopped")


def main():
    parser = argparse.ArgumentParser(
        description="Oversight Agent — intelligent capital allocation"
    )
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--dry-run", action="store_true", help="Analyze only, don't write")
    parser.add_argument("--interval", type=int, default=1800, help="Loop interval in seconds (default: 30min)")
    parser.add_argument("--hours", type=float, default=24, help="Lookback window in hours")
    parser.add_argument(
        "--capital", type=float, default=None,
        help="Total capital override. By default (None) the agent reads the "
             "live USDC balance the farmer writes to bot_history.db; pass an "
             "explicit value only to override that, e.g. for dry-run sims.",
    )
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


# ══════════════════════════════════════════════════════════════════════
# Oversight evaluation hook (v5.1 — SHADOW mode)
# ══════════════════════════════════════════════════════════════════════
# Called once per farmer cycle from reward_farmer.run_cycle.
# Spec: see Polymarket bot architecture v5.1.md §4.21.
# Invariants honoured: synchronous, no DB, no HTTP, no threads, no raise.
#
# Promotion ladder per architecture §4.21.7:
#   Stage 1 — SHADOW: _SHADOW_ONLY=True (master gate). All triggered
#             conditions are logged via [OVERSIGHT_SHADOW] but evaluate()
#             returns {"action": "continue", "reason": "shadow"} regardless
#             of fired signals. Safe default state.
#   Stage 2 — PAUSE active: _SHADOW_ONLY=False, _PAUSE_ENABLED=True.
#             would_pause signals translate to real pause returns; the
#             farmer skips placements for that cycle.
#   Stage 3 — KILL active: _SHADOW_ONLY=False, _KILL_ENABLED=True.
#             would_kill signals translate to real kill returns; the
#             farmer activates the kill switch (cancel-all + halt).
#
# Each stage flip is operator-driven after the per-stage soak gate
# (no false positives in healthy regime, triggers fire before hard
# guardrails, no flapping). When _KILL_ENABLED=False but a would_kill
# signal fires AND _PAUSE_ENABLED=True, the action falls through to pause
# — preserves safety intent without escalating to terminal.
#
# Wiring of these flags into evaluate() lands in a follow-up commit;
# this commit introduces them as additive constants with zero behaviour
# change.

_SHADOW_ONLY = True       # Master gate — when True, evaluate returns continue/shadow
_PAUSE_ENABLED = False    # Stage 2: would_pause signals → pause action
_KILL_ENABLED = False     # Stage 3: would_kill signals → kill action

# Ring buffer length: 30 snapshots ≈ 15 min at 30 s cadence. Bounds the
# longest detector window (10-cycle CF trajectory) with headroom.
_HISTORY_LEN = 30
_GUARD_HISTORY: collections.deque = collections.deque(maxlen=_HISTORY_LEN)

# Signal A — notional drift (avg ratio over last N cycles)
_NOTIONAL_DRIFT_WINDOW = 5
_NOTIONAL_DRIFT_THRESHOLD = 1.8

# Signal B — cluster breadth (≥ M blocked clusters for N consecutive cycles)
_CLUSTER_BREADTH_WINDOW = 3
_CLUSTER_BREADTH_MIN_BLOCKED = 2

# Signal C — CF soft-zone persistence (cf in band for N consecutive cycles)
_CF_SOFT_LO = 0.01
_CF_SOFT_HI = 0.03
_CF_SOFT_WINDOW = 5

# Signal D — cancel pressure (avg cancels > K × avg places, sustained N cycles)
# Per design decision: kill-switch cancels are NOT filtered out. Oversight
# reads what the system did, not why; if a kill happens and the process
# continues, an extra pause signal is benign.
_CANCEL_PRESSURE_WINDOW = 6
_CANCEL_PRESSURE_RATIO = 2.0
_CANCEL_PRESSURE_MIN_PLACES = 1   # avoid div-by-zero / single-cycle false positives

# Signal E — CF trajectory collapse (would_kill)
_CF_TRAJECTORY_WINDOW = 10
_CF_TRAJECTORY_DROP_PCT = 0.50

# Signal F — slow bleed (daily_loss > frac·T sustained N cycles)
_SLOW_BLEED_WINDOW = 6
_SLOW_BLEED_LOSS_FRAC = 0.05


def _snapshot(guard: dict) -> dict:
    """Reduce a guard dict to the minimal fields the detectors need.
    Keeps the ring buffer small and avoids retaining cluster/live dicts."""
    live_by_cid = guard.get("live_by_cid") or {}
    blocked = guard.get("blocked_clusters") or set()
    return {
        "ts": time.time(),
        "notional_ratio": guard.get("notional_ratio"),
        "cf": guard.get("cf"),
        "daily_loss": guard.get("daily_loss"),
        "total_capital": guard.get("total_capital"),
        "blocked_count": len(blocked),
        "deployed_count": sum(1 for v in live_by_cid.values() if (v or 0) > 0),
        "orders_placed": int(guard.get("orders_placed_prev_cycle", 0) or 0),
        "orders_cancelled": int(guard.get("orders_cancelled_prev_cycle", 0) or 0),
    }


def _signal_notional_drift() -> tuple:
    """A — avg(notional_ratio) over last N cycles ≥ threshold."""
    if len(_GUARD_HISTORY) < _NOTIONAL_DRIFT_WINDOW:
        return False, None, "insufficient_history"
    window = list(_GUARD_HISTORY)[-_NOTIONAL_DRIFT_WINDOW:]
    vals = [s["notional_ratio"] for s in window if s["notional_ratio"] is not None]
    if len(vals) < _NOTIONAL_DRIFT_WINDOW:
        return False, None, "missing_data"
    avg = sum(vals) / len(vals)
    return avg >= _NOTIONAL_DRIFT_THRESHOLD, avg, "ok"


def _signal_cluster_breadth() -> tuple:
    """B — blocked_count ≥ min on every one of the last N cycles."""
    if len(_GUARD_HISTORY) < _CLUSTER_BREADTH_WINDOW:
        return False, None, "insufficient_history"
    window = list(_GUARD_HISTORY)[-_CLUSTER_BREADTH_WINDOW:]
    counts = [s["blocked_count"] for s in window]
    triggered = all(c >= _CLUSTER_BREADTH_MIN_BLOCKED for c in counts)
    return triggered, max(counts), "ok"


def _signal_cf_soft_zone() -> tuple:
    """C — cf in [_CF_SOFT_LO, _CF_SOFT_HI] on every one of last N cycles."""
    if len(_GUARD_HISTORY) < _CF_SOFT_WINDOW:
        return False, None, "insufficient_history"
    window = list(_GUARD_HISTORY)[-_CF_SOFT_WINDOW:]
    cfs = [s["cf"] for s in window]
    if any(cf is None for cf in cfs):
        return False, None, "missing_data"
    triggered = all(_CF_SOFT_LO <= cf <= _CF_SOFT_HI for cf in cfs)
    return triggered, sum(cfs) / len(cfs), "ok"


def _signal_cancel_pressure() -> tuple:
    """D — avg(cancels) ≥ ratio × avg(places) over last N cycles."""
    if len(_GUARD_HISTORY) < _CANCEL_PRESSURE_WINDOW:
        return False, None, "insufficient_history"
    window = list(_GUARD_HISTORY)[-_CANCEL_PRESSURE_WINDOW:]
    avg_places = sum(s["orders_placed"] for s in window) / len(window)
    avg_cancels = sum(s["orders_cancelled"] for s in window) / len(window)
    if avg_places < _CANCEL_PRESSURE_MIN_PLACES:
        return False, None, "insufficient_activity"
    ratio = avg_cancels / avg_places
    return ratio >= _CANCEL_PRESSURE_RATIO, ratio, "ok"


def _signal_cf_trajectory() -> tuple:
    """E — cf has dropped ≥ pct from cycle-now-minus-N to cycle-now,
    AND deployed_count is declining over the same window."""
    if len(_GUARD_HISTORY) < _CF_TRAJECTORY_WINDOW:
        return False, None, "insufficient_history"
    window = list(_GUARD_HISTORY)[-_CF_TRAJECTORY_WINDOW:]
    cf_first = window[0]["cf"]
    cf_last = window[-1]["cf"]
    if cf_first is None or cf_last is None or cf_first <= 0:
        return False, None, "missing_data"
    drop = (cf_first - cf_last) / cf_first
    deployed_first = window[0]["deployed_count"]
    deployed_last = window[-1]["deployed_count"]
    declining = deployed_last < deployed_first
    triggered = drop >= _CF_TRAJECTORY_DROP_PCT and declining
    return triggered, drop, "ok"


def _signal_slow_bleed() -> tuple:
    """F — daily_loss > frac·total_capital on every one of last N cycles."""
    if len(_GUARD_HISTORY) < _SLOW_BLEED_WINDOW:
        return False, None, "insufficient_history"
    window = list(_GUARD_HISTORY)[-_SLOW_BLEED_WINDOW:]
    fracs = []
    for s in window:
        loss, cap = s["daily_loss"], s["total_capital"]
        if loss is None or cap is None or cap <= 0:
            return False, None, "missing_data"
        fracs.append(loss / cap)
    triggered = all(f > _SLOW_BLEED_LOSS_FRAC for f in fracs)
    return triggered, max(fracs), "ok"


_SIGNALS = (
    ("notional_drift",   _signal_notional_drift,   "would_pause"),
    ("cluster_breadth",  _signal_cluster_breadth,  "would_pause"),
    ("cf_soft_zone",     _signal_cf_soft_zone,     "would_pause"),
    ("cancel_pressure",  _signal_cancel_pressure,  "would_pause"),
    ("slow_bleed",       _signal_slow_bleed,       "would_pause"),
    ("cf_trajectory",    _signal_cf_trajectory,    "would_kill"),
)


def _fmt(v) -> str:
    if v is None:
        return "None"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def _check_signals_and_log(guard: dict) -> tuple[list[str], list[str]]:
    """Run all six detectors. Emit one [OVERSIGHT_SHADOW] line per
    triggered signal + one summary line if any fired.

    Returns (fired_pause, fired_kill): names of signals that triggered,
    partitioned by their declared kind in _SIGNALS. evaluate() consumes
    these lists to map promotion-flag state into pause/kill actions.

    Per-signal try/except hardening: a detector that raises only loses
    its own cycle (logged at [OVERSIGHT_SHADOW_ERROR]) — the remaining
    five detectors still run.
    """
    fired_pause: list[str] = []
    fired_kill: list[str] = []
    for name, fn, kind in _SIGNALS:
        try:
            triggered, value, status = fn()
        except Exception as e:
            log.error(
                "[OVERSIGHT_SHADOW_ERROR] signal=%s failed: %s", name, e,
            )
            continue
        if triggered:
            log.warning(
                "[OVERSIGHT_SHADOW] signal=%s value=%s window_status=%s "
                "kind=%s triggered=True",
                name, _fmt(value), status, kind,
            )
            (fired_kill if kind == "would_kill" else fired_pause).append(name)
        elif status not in ("ok", "insufficient_history"):
            # Missing data / insufficient activity — log once so we can
            # count gap-frequency in the validation log review.
            log.info(
                "[OVERSIGHT_SHADOW] signal=%s status=%s triggered=False",
                name, status,
            )
    if fired_pause or fired_kill:
        log.warning(
            "[OVERSIGHT_SHADOW] would_pause=%s would_kill=%s "
            "pause_reasons=%s kill_reasons=%s",
            bool(fired_pause), bool(fired_kill),
            ",".join(fired_pause) or "-",
            ",".join(fired_kill) or "-",
        )
    return fired_pause, fired_kill


def evaluate(guard: dict) -> dict:
    """Oversight evaluation hook. See architecture doc §4.21.

    Returns one of:
      {"action": "continue", "reason": "shadow"}     — master gate on
      {"action": "continue", "reason": "no_signal"}  — master off, no triggers
      {"action": "pause",    "reason": "<sig,sig>"}  — Stage 2, pause fired
      {"action": "kill",     "reason": "<sig>"}      — Stage 3, kill fired

    Promotion ladder (per the comment block above and architecture
    §4.21.7):
      Stage 1: _SHADOW_ONLY=True (default). Master gate forces continue
               regardless of fired signals.
      Stage 2: _SHADOW_ONLY=False + _PAUSE_ENABLED=True. would_pause
               signals translate to pause; the farmer skips placements
               for that cycle.
      Stage 3: + _KILL_ENABLED=True. would_kill signals translate to
               kill; the farmer activates the kill switch (cancel-all
               + halt, manual restart to recover).

    Multi-signal precedence: strict severity. If any would_kill signal
    fires AND _KILL_ENABLED, return kill (the kill_reasons line). Else
    if any signal fires (kill or pause) AND _PAUSE_ENABLED, return pause
    (would_kill falls through to pause when _KILL_ENABLED=False —
    preserves safety intent without escalating to terminal). Else
    continue/no_signal.
    """
    fired_pause: list[str] = []
    fired_kill: list[str] = []
    try:
        _GUARD_HISTORY.append(_snapshot(guard))
        fired_pause, fired_kill = _check_signals_and_log(guard)
    except Exception as e:
        # Defence in depth — the caller in reward_farmer.run_cycle
        # already wraps this in try/except, but we never want a shadow
        # bug to surface as [OVERSIGHT_ERROR] noise on the operator's
        # dashboard. fired_* default to empty so the action mapping
        # below cleanly falls through to the safe continue branch.
        log.error("[OVERSIGHT_SHADOW_ERROR] internal: %s", e)

    # Master gate: Stage 1 behaviour. Returns continue regardless of
    # fired signals. Operator flips _SHADOW_ONLY to False to enter the
    # Stage 2/3 promotion ladder.
    if _SHADOW_ONLY:
        return {"action": "continue", "reason": "shadow"}

    # Stage 3: would_kill signals are real kill returns when enabled.
    if fired_kill and _KILL_ENABLED:
        reason = ",".join(fired_kill)[:200]
        return {"action": "kill", "reason": reason}

    # Stage 2: would_pause signals are real pause returns when enabled.
    # would_kill signals fall through to pause here when _KILL_ENABLED
    # is False (Phase C decision: preserve safety intent).
    if (fired_pause or fired_kill) and _PAUSE_ENABLED:
        names = fired_kill + fired_pause  # kill first, more visible in logs
        reason = ",".join(names)[:200]
        return {"action": "pause", "reason": reason}

    # No actionable signal under current flag state.
    return {"action": "continue", "reason": "no_signal"}
