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


def get_wallet_peak_usd(db_path: str, current_wallet: float) -> float:
    """Max(historical_max from portfolio_snapshots, current_wallet).

    The historical peak is used for drawdown computation in the kill switch.
    Falls back to current_wallet on any DB issue (no kill on first cycle).
    """
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT MAX(exchange_balance) FROM portfolio_snapshots"
        ).fetchone()
        conn.close()
        peak = float(row[0]) if row and row[0] is not None else 0.0
        return max(peak, current_wallet)
    except Exception as e:
        log.debug(f"peak lookup fallback (using current): {e}")
        return current_wallet


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


def write_portfolio_snapshot(db_path: str, balance: float) -> None:
    """Persist current wallet to portfolio_snapshots for future peak/24h lookups."""
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO portfolio_snapshots (ts, exchange_balance) VALUES (?, ?)",
            (time.time(), balance),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"portfolio_snapshot insert skipped: {e}")


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
    # 1. Wallet probe
    try:
        wallet = get_live_wallet_usd(allocator.funder, signer_key, api_creds)
    except Exception as e:
        log.error(f"[CAPITAL_SOURCE] source=none reason=clob_error err={e}")
        return {"status": "no_capital", "error": str(e)}

    log.info(f"[CAPITAL_SOURCE] source=live_api value=${wallet:.2f}")

    # 2. Snapshot for history (so future cycles have peak/24h data)
    write_portfolio_snapshot(db_path, wallet)

    # 3. History reads
    peak = get_wallet_peak_usd(db_path, wallet)
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
    tracker = None
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
        policy.evaluate()
        excluded_cids = policy.get_excluded_cids()
    except Exception as e:
        log.warning(f"[LEARN] tracker/policy pass failed (fail-open): {e}")

    # 4. Allocate
    result = allocator.compute(
        wallet_usd=wallet,
        wallet_peak_usd=peak,
        wallet_24h_ago_usd=wallet_24h_ago,
        realized_loss_24h=realized_loss,
        excluded_cids=excluded_cids,
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

    # 6. Telemetry
    summary = {
        "wallet": round(wallet, 2),
        "peak": round(peak, 2),
        "realized_loss_24h": round(realized_loss, 2),
        "num_deploy": len(result.deploys),
        "num_avoid": len(result.avoids),
        "capital_deployed": result.capital_deployed,
        "expected_total_reward": result.expected_total_reward,
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
