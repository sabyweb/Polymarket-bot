"""FX-049: Wallet-invariant reconciliation — defense-in-depth backstop.

PURPOSE
-------
Compare the BOT'S BELIEF about wallet movement (derived from `fills` +
`unwinds` + Polymarket REWARD/MAKER_REBATE payouts) against ON-CHAIN
TRUTH (live pUSD balance + Polymarket activity feed). Divergence above a
threshold fires `[CRITICAL] WALLET_DESYNC` for operator visibility.

DESIGN
------
- Runs once per agent cycle (~30 min), not per farmer cycle (30 s) —
  fetching activity from data-api has rate-limit cost.
- INCREMENTAL: compares the window since the last reconcile row, not
  all-time. Cumulative drift is captured but doesn't compound across
  alerts (each cycle resets the baseline regardless of outcome).
- FAIL-OPEN on data fetch errors: writes a 'fail_open' status row, no
  alert. Strictly safer than fail-closed (which could kill the bot on a
  transient network blip).
- FIRST RUN: snapshots current wallet as baseline, writes 'baseline'
  status row, no alert. Subsequent calls do the comparison.

CONTRACTS (R6)
--------------
C1: First call with empty history → writes 'baseline' row, returns
    "no_baseline" status. No comparison performed.

C2: Subsequent call with `|actual_delta − expected_delta| ≤ threshold`
    → writes 'ok' row, returns "ok". No alert.

C3: Subsequent call with `|actual_delta − expected_delta| > threshold`
    → writes 'desync' row AND emits log.critical("WALLET_DESYNC: ...")
    with all signals. Returns "desync".

C4: Any exception during data fetch → writes 'fail_open' row, returns
    "fail_open". No CRITICAL emitted. log.warning for operator.

C5: After ANY call (ok / desync / fail_open / baseline), the new row's
    `(ts, actual_wallet)` becomes the BASELINE for the next call.

C6: Reward/rebate fetch failures degrade gracefully — the reconciler
    proceeds assuming rewards_delta=0, which CAN over-attribute
    divergence to the bot DB (false positive) but never under-attribute.
    The fail-open path covers truly broken fetches.

SUPERSEDES
----------
This is the SYMPTOM catch for FX-050 (Polymarket taker fee). Once FX-050
ships, the fee no longer contributes to divergence. But future unknown
unknowns (silent fill misses, phantom unwinds, external wallet activity)
still trip the reconciler. Permanent invariant.
"""

import logging
import time
from typing import Any

import requests

log = logging.getLogger("oversight.wallet_reconciliation")


def _fetch_rewards_rebates_since(funder: str, since_ts: float,
                                 timeout: float = 15.0) -> tuple[float, bool]:
    """Sum REWARD + MAKER_REBATE usdcSize from data-api since `since_ts`.

    Returns ``(total_usd, fetch_ok)``. On any exception or non-200,
    returns ``(0.0, False)`` — caller treats this as fail-open.

    Polymarket pays REWARDs once daily ~00:20 UTC (threshold-gated $1).
    MAKER_REBATEs accumulate continuously. Both arrive in the wallet as
    cash inflows so both must be in `expected_wallet_delta`.
    """
    if not funder:
        return 0.0, False
    total = 0.0
    try:
        for ptype in ("REWARD", "MAKER_REBATE"):
            offset = 0
            while True:
                r = requests.get(
                    "https://data-api.polymarket.com/activity",
                    params={"user": funder, "type": ptype, "limit": 500,
                            "offset": offset},
                    timeout=timeout,
                )
                if r.status_code != 200:
                    return 0.0, False
                data = r.json()
                if not data:
                    break
                page_in_window = False
                for item in data:
                    ts = float(item.get("timestamp", 0))
                    if ts <= since_ts:
                        continue
                    page_in_window = True
                    amt = float(item.get("usdcSize", 0) or item.get("amount", 0))
                    if amt > 0:
                        total += amt
                if len(data) < 500:
                    break
                offset += 500
                if not page_in_window:
                    # All items on this page are older than the window;
                    # next pages will be even older. Stop early.
                    break
        return total, True
    except Exception as e:
        log.debug(f"_fetch_rewards_rebates_since error: {e}")
        return 0.0, False


def reconcile_wallet_invariant(
    db,
    actual_wallet_now: float,
    funder: str,
    threshold_usd: float,
    *,
    _fetch_rewards_fn=_fetch_rewards_rebates_since,
    _now_fn=time.time,
) -> dict[str, Any]:
    """Run one reconciliation cycle. See module docstring for contracts.

    Args:
      db: BotDatabase instance (provides
        ``load_latest_wallet_reconcile``, ``sum_fills_usd_since``,
        ``sum_unwinds_usd_since``, ``insert_wallet_reconcile``).
      actual_wallet_now: live on-chain pUSD balance from
        ``get_balance_allowance(COLLATERAL)``.
      funder: wallet address used to filter data-api activity (FUNDER
        env var).
      threshold_usd: |divergence| ≤ this → 'ok'; > this → 'desync'.
      _fetch_rewards_fn / _now_fn: dependency-inject for testability.

    Returns dict with keys: status, divergence, actual_delta,
    expected_delta, baseline_ts, baseline_wallet, fills_delta,
    unwinds_delta, rewards_delta, rewards_fetch_ok.
    """
    now = _now_fn()
    prior = db.load_latest_wallet_reconcile()

    # C1: First run — snapshot baseline, no comparison.
    if prior is None:
        log.info(
            f"[WALLET_RECONCILE] baseline established: actual=${actual_wallet_now:.4f}"
        )
        db.insert_wallet_reconcile(
            actual_wallet=actual_wallet_now,
            expected_wallet=actual_wallet_now,
            divergence=0.0,
            status="baseline",
            baseline_ts=now,
            baseline_wallet=actual_wallet_now,
        )
        return {
            "status": "no_baseline",
            "divergence": 0.0,
            "actual_delta": 0.0,
            "expected_delta": 0.0,
            "baseline_ts": now,
            "baseline_wallet": actual_wallet_now,
            "fills_delta": 0.0,
            "unwinds_delta": 0.0,
            "rewards_delta": 0.0,
            "rewards_fetch_ok": True,
        }

    baseline_ts = float(prior["ts"])
    baseline_wallet = float(prior["actual_wallet"])

    # Window: from prior reconcile timestamp to now.
    actual_delta = actual_wallet_now - baseline_wallet

    # Sum bot-DB cash flows in window. usd_value of fills = cash OUT
    # (BUY paid); usd_value of unwinds = cash IN (SELL received, fee-
    # adjusted post-FX-050).
    fills_delta = db.sum_fills_usd_since(baseline_ts)
    unwinds_delta = db.sum_unwinds_usd_since(baseline_ts)

    # Fetch rewards + rebates from Polymarket data-api.
    rewards_delta, rewards_fetch_ok = _fetch_rewards_fn(funder, baseline_ts)

    # If reward fetch failed, we can still detect SIZABLE divergences
    # but small ones might be attributable to rewards we couldn't see.
    # Be conservative — emit fail_open status, no CRITICAL.
    if not rewards_fetch_ok:
        # Compute divergence anyway so the row records what we knew
        expected_delta = unwinds_delta - fills_delta + 0.0  # rewards unknown
        divergence = actual_delta - expected_delta
        log.warning(
            f"[WALLET_RECONCILE] fail_open — could not fetch rewards from data-api. "
            f"Recording row with rewards_delta=0; divergence might be false-positive. "
            f"actual_delta=${actual_delta:.4f} expected_delta=${expected_delta:.4f} "
            f"divergence=${divergence:+.4f}"
        )
        db.insert_wallet_reconcile(
            actual_wallet=actual_wallet_now,
            expected_wallet=baseline_wallet + expected_delta,
            divergence=divergence,
            status="fail_open",
            baseline_ts=now,  # new baseline for next cycle
            baseline_wallet=actual_wallet_now,
            fills_delta=fills_delta,
            unwinds_delta=unwinds_delta,
            rewards_delta=0.0,
        )
        return {
            "status": "fail_open",
            "divergence": divergence,
            "actual_delta": actual_delta,
            "expected_delta": expected_delta,
            "baseline_ts": baseline_ts,
            "baseline_wallet": baseline_wallet,
            "fills_delta": fills_delta,
            "unwinds_delta": unwinds_delta,
            "rewards_delta": 0.0,
            "rewards_fetch_ok": False,
        }

    expected_delta = unwinds_delta - fills_delta + rewards_delta
    expected_wallet = baseline_wallet + expected_delta
    divergence = actual_wallet_now - expected_wallet

    if abs(divergence) > threshold_usd:
        status = "desync"
        log.critical(
            f"[CRITICAL] WALLET_DESYNC divergence=${divergence:+.4f} "
            f"threshold=${threshold_usd:.4f} | "
            f"actual_wallet=${actual_wallet_now:.4f} "
            f"expected_wallet=${expected_wallet:.4f} | "
            f"baseline_wallet=${baseline_wallet:.4f} "
            f"(at {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(baseline_ts))}) | "
            f"fills_delta=${fills_delta:.4f} (out) "
            f"unwinds_delta=${unwinds_delta:.4f} (in) "
            f"rewards_delta=${rewards_delta:.4f} (in)"
        )
        # FX-074: page the operator via the existing Discord alert channel.
        # OBSERVATIONAL ONLY — this does NOT halt or gate allocation; it just
        # turns the [CRITICAL] log above into a real external alert. Fail-open:
        # an alert-send failure must never crash oversight or block trading.
        try:
            from alerts import alert_wallet_desync
            alert_wallet_desync(
                divergence=divergence,
                threshold_usd=threshold_usd,
                actual_wallet=actual_wallet_now,
                expected_wallet=expected_wallet,
            )
        except Exception as e:
            log.warning(f"[WALLET_RECONCILE] desync alert send failed (fail-open): {e}")
    else:
        status = "ok"
        log.info(
            f"[WALLET_RECONCILE] ok divergence=${divergence:+.4f} "
            f"(threshold=${threshold_usd:.4f}) | "
            f"actual=${actual_wallet_now:.4f} expected=${expected_wallet:.4f} | "
            f"fills=${fills_delta:.4f} unwinds=${unwinds_delta:.4f} "
            f"rewards=${rewards_delta:.4f}"
        )

    db.insert_wallet_reconcile(
        actual_wallet=actual_wallet_now,
        expected_wallet=expected_wallet,
        divergence=divergence,
        status=status,
        baseline_ts=now,  # the NEW baseline is now
        baseline_wallet=actual_wallet_now,
        fills_delta=fills_delta,
        unwinds_delta=unwinds_delta,
        rewards_delta=rewards_delta,
    )
    return {
        "status": status,
        "divergence": divergence,
        "actual_delta": actual_delta,
        "expected_delta": expected_delta,
        "baseline_ts": baseline_ts,
        "baseline_wallet": baseline_wallet,
        "fills_delta": fills_delta,
        "unwinds_delta": unwinds_delta,
        "rewards_delta": rewards_delta,
        "rewards_fetch_ok": True,
    }
