#!/usr/bin/env python3
"""simple_oversight.py — Path B-prime planner entry point.

Replaces oversight_agent.py. Uses SimpleAllocator end-to-end:
  - Live wallet balance from Polymarket CLOB
  - Real q_share via /rewards/user/percentages (current positions)
  - Cumulative-DB / cold-start fallbacks for new candidates
  - Minimal kill switch (24h loss > 10% wallet, drawdown > 15% from peak)
  - Writes market_allocations.json in the exact schema the farmer reads

CLI compatible with oversight_agent.py:
  python simple_oversight.py --loop             # 30-min cycle loop (systemd)
  python simple_oversight.py --once             # one cycle for testing
  python simple_oversight.py --once --output X  # write to alternate file

Rollback: systemd ExecStart back to oversight_agent.py --loop.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import sys
import time
from typing import Optional

from dotenv import load_dotenv

from simple_allocator import SimpleAllocator
import alerts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("simple_oversight")

DEFAULT_DB_PATH = "bot_history.db"
DEFAULT_OUTPUT = "market_allocations.json"
LOOP_INTERVAL_SEC = 1800  # 30 min — matches oversight_agent cadence

_shutdown = False


def _signal_handler(signum, _frame):
    """SIGINT/SIGTERM → exit on next cycle boundary (FX-014/015 pattern)."""
    global _shutdown
    sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    log.info(f"[SHUTDOWN] {sig_name} received — exiting loop")
    _shutdown = True


# ── Wallet probes ──

def get_live_wallet_usd(funder: str, signer_key: str, api_creds) -> float:
    """Fetch current wallet pUSD balance via V2 CLOB client.

    Returns balance in USD. Raises on auth failure — caller may catch.
    """
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

    c = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=signer_key,
        funder=funder,
        signature_type=2,
        creds=api_creds,
    )
    b = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    return int(b["balance"]) / 1e6


def get_wallet_peak_usd(
    db_path: str,
    current_wallet: float,
    current_portfolio: float | None = None,
) -> float:
    """Max(historical total_value peak, current portfolio). FX-095."""
    current = current_portfolio if current_portfolio is not None else current_wallet
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT MAX(total_value) FROM portfolio_snapshots"
        ).fetchone()
        conn.close()
        peak = float(row[0]) if row and row[0] is not None else 0.0
        return max(peak, current)
    except Exception as e:
        log.debug(f"peak lookup fallback (using current): {e}")
        return current


def _load_positions_and_mids(db_path: str) -> tuple[dict, dict[str, float]]:
    """Load open positions + latest book_snapshot midpoints for FX-095 marks."""
    positions: dict = {}
    mids: dict[str, float] = {}
    try:
        conn = sqlite3.connect(db_path)
        for row in conn.execute(
            "SELECT condition_id, yes_shares, yes_avg_price, "
            "no_shares, no_avg_price FROM positions "
            "WHERE yes_shares > 0 OR no_shares > 0"
        ):
            positions[row[0]] = {
                "yes_shares": float(row[1] or 0),
                "yes_avg_price": float(row[2] or 0),
                "no_shares": float(row[3] or 0),
                "no_avg_price": float(row[4] or 0),
            }
        for row in conn.execute(
            "SELECT condition_id, midpoint FROM book_snapshots bs "
            "WHERE ts = (SELECT MAX(ts) FROM book_snapshots bs2 "
            "WHERE bs2.condition_id = bs.condition_id) "
            "AND midpoint > 0 AND midpoint < 1"
        ):
            mids[row[0]] = float(row[1])
        conn.close()
    except Exception as e:
        log.debug(f"portfolio mark load failed (fail-open): {e}")
    return positions, mids


# ── FIX-3 (RC-5): authoritative on-chain portfolio value for the kill input ──
# The DB `positions` table can lag or miss an on-chain fill (a fill recorded late,
# or never if a protective kill halts the farmer before it syncs). When that happens
# the drawdown/loss kill reads cash-only and FALSE-trips (the 2026-06-13 deadlock) —
# or misses a held-to-resolution loss and UNDER-fires (2026-06-12). When
# RF_KILL_PORTFOLIO_SOURCE == "onchain" the kill's portfolio value is sourced from the
# data-api instead. Off by default (reversible single-axis change).

_LAST_ONCHAIN_INV: Optional[tuple[float, float]] = None  # (ts, inventory_value_usd)


def _onchain_inventory_value() -> Optional[float]:
    """Authoritative marked inventory value from the data-api: sum of size*curPrice
    over /positions. `curPrice` is the exchange mark, so no midpoint convention is
    needed. Returns None on any failure (caller applies the fail-safe)."""
    try:
        import requests
        from config import FUNDER

        if not FUNDER:
            log.warning("[FIX-3] FUNDER unset — cannot source on-chain portfolio")
            return None
        resp = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": FUNDER, "sizeThreshold": "0.1"},
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning(f"[FIX-3] data-api /positions returned {resp.status_code}")
            return None
        data = resp.json()
        if not isinstance(data, list):
            log.warning("[FIX-3] data-api /positions unexpected format")
            return None
        return sum(
            float(p.get("size", 0) or 0) * float(p.get("curPrice", 0) or 0)
            for p in data
        )
    except Exception as e:
        log.warning(f"[FIX-3] on-chain inventory fetch failed: {e}")
        return None


def _portfolio_value_for_kill(wallet: float, db_portfolio: float) -> float:
    """FIX-3: the portfolio value fed to the kill switch.

    With RF_KILL_PORTFOLIO_SOURCE == "onchain", return cash + the authoritative
    on-chain marked inventory. Fail-safe — a missing data-api reading must never
    silently disable NOR falsely fire the kill:
      * good reading           -> cash + on-chain inventory (cached);
      * miss + fresh cache      -> cash + cached inventory (WARN);
      * miss + no fresh cache   -> the DB-based value (WARN; no worse than legacy).
    With "db" (default) it returns the DB-based value unchanged.
    """
    from config import cfg

    global _LAST_ONCHAIN_INV
    source = str(cfg("RF_KILL_PORTFOLIO_SOURCE") or "db").lower()
    if source != "onchain":
        return db_portfolio

    inv = _onchain_inventory_value()
    if inv is not None:
        _LAST_ONCHAIN_INV = (time.time(), inv)
        return wallet + inv

    if _LAST_ONCHAIN_INV is not None:
        age = time.time() - _LAST_ONCHAIN_INV[0]
        try:
            max_stale = float(cfg("RF_KILL_ONCHAIN_MAX_STALE_SECS"))
        except (TypeError, ValueError):
            max_stale = 3600.0
        if age <= max_stale:
            log.warning(
                f"[FIX-3] data-api unavailable; reusing on-chain inventory from "
                f"{age:.0f}s ago for the kill metric (cash=${wallet:.2f})"
            )
            return wallet + _LAST_ONCHAIN_INV[1]

    log.warning(
        "[FIX-3] data-api unavailable and no fresh cached inventory; falling back "
        f"to DB-based portfolio ${db_portfolio:.2f} for the kill metric"
    )
    return db_portfolio


def get_realized_loss_24h(db_path: str) -> float:
    """Sum of |pnl| for pnl<0 unwinds in last 24h. Returns positive USD."""
    try:
        conn = sqlite3.connect(db_path)
        cutoff = time.time() - 86400
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN pnl < 0 THEN -pnl ELSE 0 END), 0) "
            "FROM unwinds WHERE ts > ?",
            (cutoff,),
        ).fetchone()
        conn.close()
        return float(row[0]) if row else 0.0
    except Exception as e:
        log.debug(f"realized loss lookup fallback (using 0): {e}")
        return 0.0


def get_wallet_24h_ago(db_path: str) -> Optional[float]:
    """Wallet balance ~24h ago from portfolio_snapshots. None if no data."""
    try:
        conn = sqlite3.connect(db_path)
        target = time.time() - 86400
        row = conn.execute(
            "SELECT exchange_balance FROM portfolio_snapshots "
            "WHERE ts BETWEEN ? AND ? ORDER BY ABS(ts - ?) LIMIT 1",
            (target - 3600, target + 3600, target),
        ).fetchone()
        conn.close()
        return float(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def write_portfolio_snapshot(
    db_path: str,
    balance: float,
    total_value: float | None = None,
    locked_capital: float = 0.0,
) -> None:
    """Persist portfolio snapshot for peak/24h/drawdown lookups (FX-078/FX-095)."""
    tv = total_value if total_value is not None else balance
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ts               REAL NOT NULL,
                total_value      REAL NOT NULL,
                exchange_balance REAL NOT NULL DEFAULT 0,
                locked_capital   REAL NOT NULL DEFAULT 0,
                peak_value       REAL NOT NULL DEFAULT 0
            )"""
        )
        prev_peak_row = conn.execute(
            "SELECT MAX(total_value) FROM portfolio_snapshots"
        ).fetchone()
        prev_peak = float(prev_peak_row[0]) if prev_peak_row and prev_peak_row[0] else 0.0
        peak_value = max(prev_peak, tv)
        conn.execute(
            "INSERT INTO portfolio_snapshots "
            "(ts, total_value, exchange_balance, locked_capital, peak_value) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), tv, balance, locked_capital, peak_value),
        )
        conn.commit()
    except Exception as e:
        # Truthful (FX-054 class): a silent failure here disables a kill-switch
        # input, so surface it at WARNING — never debug.
        log.warning(f"[PORTFOLIO_SNAPSHOT] write failed: {e}")
    finally:
        # FX-078: always close — the pre-fix code put conn.close() AFTER the
        # throwing execute, so every failed write leaked a connection (and the
        # held read/write state contributed to the un-checkpointed WAL growth).
        if conn is not None:
            conn.close()


# ── Per-cycle orchestration ──

def run_once(
    allocator: SimpleAllocator,
    db_path: str,
    output_path: str,
    signer_key: str,
    api_creds,
) -> dict:
    """Execute one oversight cycle. Returns summary dict.

    Steps:
      1. Fetch live wallet balance
      2. Persist to portfolio_snapshots for history
      3. Look up peak + 24h-ago + 24h realized loss
      4. Call SimpleAllocator.compute() — discover + estimate + allocate
      5. Write market_allocations.json atomically
      6. Emit [SIMPLE_ALLOC] telemetry line

    Failures in steps 1-3 fail-fast (no allocation cycle); failures in step 4-6 are
    logged but the bot continues on the previous alloc file (farmer is resilient
    to stale alloc — has RF_ALLOCATION_TTL_HOURS guard).
    """
    # FX-063: Hot-reload config_overrides.json if it changed, so the
    # SimpleAllocator's cfg()-driven knobs (e.g.
    # RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC) take effect without restarting the
    # oversight process. The farmer and bot already reload every cycle
    # (reward_farmer.py / bot.py); oversight was the only long-running
    # entrypoint that never did, so allocator knobs froze at process start.
    # Cheap mtime-guarded no-op when the file is unchanged; fail-open by design
    # (check_and_reload swallows its own errors and returns 0).
    from config import BotConfig
    BotConfig.instance().check_and_reload()

    # FX-083: liveness heartbeat + farmer-peer staleness paging. Written at
    # cycle top (mode-independent), fully fail-open. A hung/dead farmer is
    # otherwise invisible to humans — its only heartbeat sender was legacy
    # bot.py. Pages at RF_FARMER_HEARTBEAT_STALE_SECS (~5min = ~10 missed
    # 30s farmer cycles).
    try:
        from database import get_db
        from config import cfg as _cfg
        _hb_db = get_db()
        _hb_db.record_heartbeat("oversight")
        if alerts.maybe_alert_stale_heartbeat(
            "farmer",
            _hb_db.get_heartbeat("farmer"),
            time.time(),
            _cfg("RF_FARMER_HEARTBEAT_STALE_SECS"),
            _cfg("RF_HEARTBEAT_REPAGE_SECS"),
        ):
            log.warning("[HEARTBEAT] farmer peer heartbeat STALE — Discord paged")
    except Exception as e:
        log.debug(f"[HEARTBEAT] pass skipped (fail-open): {e}")

    # 1. Wallet probe
    try:
        wallet = get_live_wallet_usd(allocator.funder, signer_key, api_creds)
    except Exception as e:
        log.error(f"[CAPITAL_SOURCE] source=none reason=clob_error err={e}")
        return {"status": "no_capital", "error": str(e)}

    log.info(f"[CAPITAL_SOURCE] source=live_api value=${wallet:.2f}")

    # 2. FX-095 portfolio value (cash + marked inventory)
    positions, mids = _load_positions_and_mids(db_path)
    from portfolio_value import compute_portfolio_value
    portfolio = compute_portfolio_value(wallet, positions, mids)
    # FIX-3 (RC-5): optionally source the inventory half from the authoritative
    # data-api so the kill can't FALSE-trip on a DB-missed fill (the 06-13 deadlock)
    # or UNDER-fire on a held-to-resolution loss (06-12). No-op unless the flag
    # RF_KILL_PORTFOLIO_SOURCE == "onchain"; fail-safe on a data-api miss.
    portfolio = _portfolio_value_for_kill(wallet, portfolio)
    locked = max(0.0, portfolio - wallet)

    # 3. Snapshot for history (so future cycles have peak/24h data)
    write_portfolio_snapshot(db_path, wallet, total_value=portfolio, locked_capital=locked)

    # 4. History reads
    peak = get_wallet_peak_usd(db_path, wallet, current_portfolio=portfolio)
    realized_loss = get_realized_loss_24h(db_path)
    wallet_24h_ago = get_wallet_24h_ago(db_path)

    # 3a. FX-055: re-wire FX-049 wallet-invariant reconciliation that was
    # dropped in the 2026-05-25 simple_oversight swap. Mirrors the
    # oversight_agent.run_once() integration. Compares bot-DB cash flows
    # (fills + unwinds + data-api rewards) against on-chain wallet delta;
    # emits [CRITICAL] WALLET_DESYNC if drift > RF_WALLET_DESYNC_THRESHOLD_USD.
    # Fail-open: any exception is logged + cycle continues (the reconciler
    # is observational, never gates allocation).
    try:
        from oversight.wallet_reconciliation import reconcile_wallet_invariant
        from database import get_db
        from config import cfg
        _db = get_db()
        reconcile_wallet_invariant(
            _db,
            actual_wallet_now=float(wallet),
            funder=allocator.funder,
            threshold_usd=float(cfg("RF_WALLET_DESYNC_THRESHOLD_USD")),
        )
    except Exception as e:
        log.warning(f"[WALLET_RECONCILE] reconciliation pass failed (fail-open): {e}")

    # 3b. FX-051 / Ground Rule 3: tick the per-market ROI tracker and let the
    # DecisionPolicy update cooldowns. The policy returns a set of
    # condition_ids that the allocator must exclude this cycle. Fail-open:
    # any exception logs and the allocator gets an empty exclusion set
    # (i.e., behaves exactly as pre-FX-051).
    excluded_cids: set[str] = set()
    size_reduction_cids: set[str] = set()
    global_tighten: bool = False
    global_reward_low: bool = False
    q_share_distrust_cids: set[str] = set()
    tracker = None
    policy = None
    try:
        from market_roi_tracker import MarketROITracker
        from decision_policy import DecisionPolicy
        tracker = MarketROITracker(
            db_path=db_path,
            funder=allocator.funder,
            api_key=allocator.api_key,
            api_secret=allocator.api_secret,
            api_passphrase=allocator.api_passphrase,
            wallet_address=allocator.wallet_address,
            # Re-use the allocator's HTTP + clock injections so tests that
            # stub the allocator's network also stub the tracker's. In
            # production both default to requests.get / time.time.
            _http=getattr(allocator, "_http", None),
            _now=getattr(allocator, "_now", None),
        )
        tracker.tick()
        tracker.prune_old_snapshots()
        policy = DecisionPolicy(db_path=db_path, tracker=tracker)
        # P4 of 9/10 plan: evaluate() returns richer dict — extract the new
        # behavior-change outputs (size_reduction_cids per-market, global_tighten
        # globally) alongside the cooldown set.
        # P10/P11 additions: global_reward_low + q_share_distrust_cids.
        eval_out = policy.evaluate()
        excluded_cids = policy.get_excluded_cids()
        size_reduction_cids = eval_out.get("size_reduction_cids", set())
        global_tighten = eval_out.get("global_tighten", False)
        global_reward_low = eval_out.get("global_reward_low", False)
        q_share_distrust_cids = eval_out.get("q_share_distrust_cids", set())
    except Exception as e:
        log.warning(f"[LEARN] tracker/policy pass failed (fail-open): {e}")

    # P11 of 9/10 plan: q_share divergence detection requires comparing
    # API q_share vs cumulative DB ratio. Both sources live on the
    # allocator (fetch_current_q_shares + load_cumulative_ratios). We pull
    # them here and feed each cid pair to policy.record_qshare_divergence,
    # which inserts a row in q_share_recalibration_events on breach.
    # Next cycle's policy.evaluate will pick up the new event via
    # _detect_qshare_divergence and add the cid to q_share_distrust_cids.
    #
    # Fail-quiet: any failure in this block skips detection this cycle;
    # next cycle retries. Wrapped in its own try/except so a fetch error
    # doesn't pollute the main allocation path.
    if policy is not None:
        try:
            api_shares = allocator.fetch_current_q_shares()
            cumul = allocator.load_cumulative_ratios()
            breaches = 0
            for cid, api_q in api_shares.items():
                if cid in cumul:
                    if policy.record_qshare_divergence(cid, api_q, cumul[cid]):
                        breaches += 1
            if breaches > 0:
                log.warning(
                    f"[LEARN_DIVERGENCE] cycle_summary: {breaches} cids "
                    f"with q_share divergence > 2× recorded"
                )
        except Exception as e:
            log.debug(f"[LEARN_DIVERGENCE] detection pass skipped: {e}")

    # 4. Allocate
    # P4 / P10 / P11: pass the new behavior-change inputs.
    # - size_reduction_cids: halve shares for these (fill_rate too high)
    # - global_tighten: raise floors + halve sizing (loss > rewards)
    # - global_reward_low: lower floors (under-deployment)
    # - q_share_distrust_cids: apply 0.5× to non-API q_share for these
    result = allocator.compute(
        wallet_usd=wallet,
        wallet_peak_usd=peak,
        wallet_24h_ago_usd=wallet_24h_ago,
        realized_loss_24h=realized_loss,
        excluded_cids=excluded_cids,
        size_reduction_cids=size_reduction_cids,
        global_tighten=global_tighten,
        global_reward_low=global_reward_low,
        q_share_distrust_cids=q_share_distrust_cids,
        portfolio_value_usd=portfolio,
        portfolio_peak_usd=peak,
    )

    # 4a. FX-051: record per-market est_capital_cost rows so future ROI ticks
    # can compute time-weighted capital_committed_avg. Fail-quiet — the
    # tracker is observational; if this write fails the next cycle's ROI
    # numbers are slightly stale but the bot keeps running.
    if tracker is not None:
        try:
            tracker.snapshot_capital(result)
        except Exception as e:
            log.debug(f"[LEARN] snapshot_capital skipped (fail-quiet): {e}")

    # 5. Write alloc file
    allocator.write_allocation_json(result, output_path=output_path)

    # 5b. A3: candidate-features survivorship log — isolated candidate_features.db, written AFTER the
    # alloc file (so a logging failure can never block the critical output) and fail-quiet (mirrors
    # snapshot_capital). Non-behavioral; a no-op unless RF_CANDIDATE_FEATURE_LOG_ENABLED made
    # compute() populate result.candidate_features.
    try:
        import candidate_features_log
        candidate_features_log.log_result(result)
    except Exception as e:
        log.debug(f"[A3] candidate-features log skipped (fail-quiet): {e}")

    # 5a. FX-085: capital-efficiency metric (Ground Rule 1 scorecard — GROSS
    # rewards per $ of committed capital). Previously UNMEASURED (the eval's
    # capital-efficiency gap). Computed from the ROI tracker's 24h global
    # summary, logged each cycle ([LEARN_CAPEFF]) and stamped into the
    # [SIMPLE_ALLOC] line. Observational only: an auto-correction trigger on
    # this metric (Ground Rule 3 #2) needs live reward data to calibrate and is
    # intentionally NOT wired to allocation yet. Fail-quiet.
    capital_efficiency = None
    daily_roi = None
    if tracker is not None:
        try:
            from config import cfg as _cfg_ce
            gsum = tracker.get_global_summary("24h")
            capital_efficiency = gsum.get("capital_efficiency")
            daily_roi = gsum.get("daily_roi")
            tcap = float(gsum.get("total_capital", 0.0) or 0.0)
            ce_line = {
                "window": "24h",
                "capital_efficiency": (
                    round(capital_efficiency, 6) if capital_efficiency is not None else None
                ),
                "daily_roi": round(daily_roi, 6) if daily_roi is not None else None,
                "total_reward": round(float(gsum.get("total_reward", 0.0) or 0.0), 4),
                "total_capital": round(tcap, 2),
            }
            log.info(f"[LEARN_CAPEFF] {ce_line}")
            target = _cfg_ce("RF_CAPITAL_EFFICIENCY_TARGET_24H")
            if (
                target and target > 0 and tcap >= 1.0
                and capital_efficiency is not None
                and capital_efficiency < target
            ):
                log.warning(
                    f"[LEARN_WARN] capital_efficiency {capital_efficiency:.5f} "
                    f"< target {target:.5f} (24h, capital ${tcap:.2f}) — "
                    f"reward yield per $ below floor"
                )
        except Exception as e:
            log.debug(f"[LEARN_CAPEFF] skipped (fail-quiet): {e}")

    # 6. Telemetry
    summary = {
        "wallet": round(wallet, 2),
        "peak": round(peak, 2),
        "realized_loss_24h": round(realized_loss, 2),
        "num_deploy": len(result.deploys),
        "num_avoid": len(result.avoids),
        "capital_deployed": result.capital_deployed,
        "expected_total_reward": result.expected_total_reward,
        "capital_efficiency": (
            round(capital_efficiency, 6) if capital_efficiency is not None else None
        ),
        "daily_roi": round(daily_roi, 6) if daily_roi is not None else None,
        "kill_switch": result.kill_switch,
        "kill_reason": result.kill_reason,
        "sources": result.sources_used,
    }
    if result.kill_switch:
        log.error(f"[KILL_SWITCH] {result.kill_reason}")
    log.info(f"[SIMPLE_ALLOC] {summary}")
    return summary


# ── Loop driver ──

def run_loop(
    allocator: SimpleAllocator,
    db_path: str,
    output_path: str,
    signer_key: str,
    api_creds,
    interval: int = LOOP_INTERVAL_SEC,
) -> None:
    """Long-running cycle loop. Signal-safe shutdown on SIGINT/SIGTERM."""
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    log.info(
        f"Starting simple_oversight loop | interval={interval}s | "
        f"output={output_path} | db={db_path}"
    )

    while not _shutdown:
        t0 = time.time()
        try:
            run_once(allocator, db_path, output_path, signer_key, api_creds)
        except Exception as e:
            log.exception(f"Cycle failed: {e}")

        elapsed = time.time() - t0
        sleep_remaining = max(1.0, interval - elapsed)
        # 1-sec granular sleep so SIGTERM aborts within 1 sec
        deadline = time.time() + sleep_remaining
        while not _shutdown and time.time() < deadline:
            time.sleep(1)

    log.info("[SHUTDOWN] simple_oversight loop exited cleanly")


# ── CLI ──

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Simple oversight — Path B-prime planner (replaces oversight_agent.py)"
    )
    parser.add_argument("--loop", action="store_true",
                        help="Run in long-running loop mode (every 30 min)")
    parser.add_argument("--once", action="store_true",
                        help="Run a single cycle and exit (testing)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="Path to write market_allocations.json")
    parser.add_argument("--db", default=DEFAULT_DB_PATH,
                        help="Path to bot_history.db")
    parser.add_argument("--interval", type=int, default=LOOP_INTERVAL_SEC,
                        help="Loop interval seconds (default 1800)")
    args = parser.parse_args()

    if not args.loop and not args.once:
        parser.print_help()
        return 1

    load_dotenv()

    # Build api_creds once (passed to wallet fetcher; allocator stores its own copy)
    from py_clob_client_v2.clob_types import ApiCreds
    api_creds = ApiCreds(
        api_key=os.getenv("CLOB_API_KEY"),
        api_secret=os.getenv("CLOB_SECRET"),
        api_passphrase=os.getenv("CLOB_PASS_PHRASE"),
    )
    signer_key = os.getenv("PRIVATE_KEY")

    allocator = SimpleAllocator(
        db_path=args.db,
        wallet_address=os.getenv("WALLET_ADDRESS"),
        funder=os.getenv("FUNDER"),
        api_key=os.getenv("CLOB_API_KEY"),
        api_secret=os.getenv("CLOB_SECRET"),
        api_passphrase=os.getenv("CLOB_PASS_PHRASE"),
    )

    if args.once:
        result = run_once(allocator, args.db, args.output, signer_key, api_creds)
        return 0 if result.get("status") != "no_capital" else 2

    run_loop(allocator, args.db, args.output, signer_key, api_creds, args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
