"""Order lifecycle: placement, fill detection, priority batch, guards.

Extracted from reward_farmer.py. All order-related logic in one module.
"""

import logging
import time

from ab.cohort import cohort as ab_cohort
from config import cfg
from fast_vol_guard import check_fast_vol_timeout
from models import OrderSlot, MarketState
from market_discovery import get_merged_book

log = logging.getLogger("reward_farmer")

# Config accessors
def SHARES_PER_SIDE(): return cfg("RF_SHARES_PER_SIDE")
def PLACEMENT_TICKS_INSIDE(): return cfg("RF_PLACEMENT_TICKS_INSIDE")
def BATCH_SIZE(): return cfg("RF_BATCH_SIZE")
def TARGET_QUEUE_AHEAD_USD(): return cfg("RF_TARGET_QUEUE_AHEAD_USD")
def AB_C1_TARGET_QUEUE_AHEAD_USD(): return cfg("RF_AB_C1_TARGET_QUEUE_AHEAD_USD")
def AB_C1_SECOND_BEST_COURT_ENABLED(): return cfg("RF_AB_C1_SECOND_BEST_COURT_ENABLED")
def AB_COHORT_COUNT(): return cfg("RF_AB_COHORT_COUNT")
def DUMP_DEPTH_SAFETY_FACTOR(): return cfg("RF_DUMP_DEPTH_SAFETY_FACTOR")


def _effective_target_queue_usd(cid: str) -> float:
    """Cohort-aware queue-ahead target for `place_orders_for_market`.

    Trader cohorts (C1/C2) use RF_AB_C1_TARGET_QUEUE_AHEAD_USD. C0 uses the
    baseline RF_TARGET_QUEUE_AHEAD_USD. A non-positive trader config falls back
    to baseline so a typo/disabled value never zeroes queue-aware placement.
    """
    if cfg("RF_AB_EXPERIMENT_ENABLED"):
        try:
            if ab_cohort(cid, AB_COHORT_COUNT()) != 0:
                trader_target = float(AB_C1_TARGET_QUEUE_AHEAD_USD() or 0.0)
                if trader_target > 0:
                    return trader_target
        except Exception:
            pass
    return TARGET_QUEUE_AHEAD_USD()


def _second_best_court_enabled(cid: str) -> bool:
    """Trader-cohort rule: do not be the best quote; join behind the current best.

    Enabled for any non-baseline cohort (C1/C2) when the A/B experiment is on
    and the second-best flag is enabled. Fail-open: any error returns False.
    """
    if not cfg("RF_AB_EXPERIMENT_ENABLED"):
        return False
    try:
        if AB_C1_SECOND_BEST_COURT_ENABLED() and ab_cohort(cid, AB_COHORT_COUNT()) != 0:
            return True
    except Exception:
        pass
    return False


def _second_best_adjust(
    side: str,
    edge: float,
    book_levels,
    midpoint: float,
    max_spread: float,
    tick: float,
    decimals: int,
) -> float | None:
    """Move an aggressive C1 quote behind the current best level.

    For bids: if `edge` is higher (better) than the best existing bid, move it
    to `best_bid - tick`. For asks: if `edge` is lower (better) than the best
    existing ask, move it to `best_ask + tick`.

    If stepping one tick behind the best would push us outside the reward zone
    (the best quote is already at the zone edge), we instead join the best
    price rather than improve it. We never post a strictly better quote than
    the current best.

    Returns None when there is no existing level on that side (C1 refuses to be
    the first quote — "happy being second best").
    """
    if not book_levels:
        return None
    try:
        best_price = float(book_levels[0]["price"])
    except (KeyError, ValueError, TypeError):
        return None
    if side == "bid":
        if edge > best_price:
            edge = round(best_price - tick, decimals)
            if abs(edge - midpoint) >= max_spread:
                edge = best_price
    else:  # ask
        if edge < best_price:
            edge = round(best_price + tick, decimals)
            if abs(edge - midpoint) >= max_spread:
                edge = best_price
    return edge

# FX-054 constants
#
# On Polygon the SDK reports `size_matched > 0` BEFORE the CTF transfer
# block confirms. The phantom-check's on-chain balance probe runs
# immediately, so during fast bursts it can read stale 0-balance and
# zero a legitimate fill. The lag tolerance lets the phantom check
# stay fail-OPEN for the first window after `slot.placed_at` —
# beyond that the FX-037 phantom defence resumes.
FILL_BALANCE_LAG_TOLERANCE_SEC = 60.0

# Drift-sweep dedup bucket. The post-detect catch-up sweep keys its
# `fill_event_id` on `(cid, side, int(now / DRIFT_DEDUP_BUCKET_SEC))`
# so repeated drift detections in the same bucket collapse to one row.
# 5 min × 30 s farmer cycle = ~10 consecutive cycles share the same key
# — wide enough to cover any single fill that lingers across cycles
# without losing distinct fills that arrive in different buckets.
DRIFT_DEDUP_BUCKET_SEC = 300.0


def _queue_aware_edge(
    side: str,
    book_levels,
    midpoint: float,
    max_spread: float,
    tick: float,
    target_queue_usd: float,
    decimals: int,
):
    """FX-036: walk one side of the merged book accumulating $ queue ahead.

    For ``side="bid"``, ``book_levels`` is ``merged["bids"]`` (sorted highest
    price → lowest, i.e., closest to mid → furthest). For ``side="ask"``, it
    is ``merged["asks"]`` (lowest → highest). At each level we add
    ``price × size`` to the running total; when the total first reaches
    ``target_queue_usd``, we sit one tick BEHIND that level (deeper from
    mid by one tick) so the accumulated queue shields us from fills while
    we earn the higher reward density of the inner zone.

    Returns ``None`` to signal "fall back to the legacy zone-edge formula"
    when:

    - ``target_queue_usd <= 0`` (operator escape hatch)
    - the book is empty
    - we walk to the zone boundary without crossing the threshold (thin
      book — current behaviour is appropriate)
    - the one-tick step behind the chosen level would itself fall outside
      the reward zone (defensive — never place outside the zone)

    The merged book is YES-equivalent on both sides (real YES bids + NO-
    derived asks on the bid side; real YES asks + NO-derived bids on the
    ask side — see ``market_discovery.get_merged_book``). Both contribute
    to ``cum_queue`` because they're arbitrage-linked competitors for the
    same liquidity.
    """
    if target_queue_usd <= 0 or not book_levels:
        return None
    cum_queue = 0.0
    for level in book_levels:
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (KeyError, ValueError, TypeError):
            continue
        d = abs(price - midpoint)
        if d >= max_spread:
            return None  # walked past the zone before crossing threshold
        cum_queue += price * size
        if cum_queue >= target_queue_usd:
            edge = price - tick if side == "bid" else price + tick
            if abs(edge - midpoint) >= max_spread:
                return None  # one-tick step would exit the zone
            return round(edge, decimals)
    return None


def _has_sufficient_dump_depth(
    opposite_book_levels,
    midpoint: float,
    max_spread: float,
    shares_per_side: int,
    dump_price: float,
    safety_factor: float,
) -> bool:
    """FX-041: gate queue-aware placement on enough OPPOSITE-side $-depth
    in the reward zone to host a passive dump if our order gets filled.

    For a ``"bid"`` placement (YES BID, lives at ``merged["bids"]``) the
    opposite side is ``merged["asks"]``. For an ``"ask"`` placement (NO
    BID, lives at ``merged["asks"]`` in YES-equivalent terms) the opposite
    side is ``merged["bids"]``. Threshold is
    ``shares_per_side × dump_price × safety_factor`` USD.

    The existing same-side ``yes_exit_depth`` / ``no_exit_depth`` check at
    the placement call site (within ``RF_DUMP_EXIT_DEPTH_BUFFER`` of edge,
    in shares) already guards same-side near-edge depth; FX-041 adds a
    complementary axis: asymmetric books (one side deep, the other thin)
    look safe to FX-036's bid-side queue check but expose us to high
    passive-dump slippage post-fill — exactly the 2026-05-19 OpenAI
    cascade. The opposite-side check catches the asymmetry.

    Returns ``True`` when disabled (``safety_factor <= 0`` or threshold
    ≤ 0) — operator escape hatch reverts to FX-036-only behaviour.
    """
    if safety_factor <= 0:
        return True
    required = shares_per_side * dump_price * safety_factor
    if required <= 0:
        return True
    cum = 0.0
    for level in opposite_book_levels:
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (KeyError, ValueError, TypeError):
            continue
        if abs(price - midpoint) > max_spread:
            continue  # outside reward zone — doesn't count toward in-zone dump depth
        cum += price * size
        if cum >= required:
            return True
    return False


def _compute_edge_prices(
    merged: dict,
    midpoint: float,
    max_spread: float,
    tick: float,
    decimals: int,
    ticks_inside: int,
    target_queue_usd: float,
    shares_per_side: int = 0,
    dump_depth_safety_factor: float = 0.0,
    second_best_court: bool = False,
) -> tuple[float, float]:
    """Return ``(edge_bid, edge_ask)`` for placement.

    FX-036 (fixit.md::FX-036, arch doc §4.23): queue-depth-aware placement.
    The legacy formula sits at ``max_spread - tick·ticks_inside`` from
    midpoint — the far edge of the reward zone, which earns the LOWEST
    reward density inside the zone (Polymarket's reward weight is
    ``1 - d/max_spread``). On the 5.5¢ Iran market that was ~9% of the
    theoretical maximum density per share-minute. Queue-aware placement
    sits as close to mid as the operator-chosen ``target_queue_usd`` of
    queue-ahead permits, capturing multiples more reward density while
    still being shielded from fills by the queue we sit behind.

    FX-041 (fixit.md::FX-041): each queue-aware result is additionally
    gated on the OPPOSITE merged-book side carrying enough $-weighted
    depth in the reward zone to absorb a passive dump if filled. Catches
    asymmetric books that FX-036's bid-side check alone misses. Defaults
    ``shares_per_side=0, dump_depth_safety_factor=0.0`` keep the helper
    backwards-compatible — callers that don't pass these args get
    pre-FX-041 behaviour.

    C1 second-best-court rule: when `second_best_court=True`, a C1 order is
    never placed at the best available level. If the queue-aware price would
    improve on the current best, it is pushed one tick behind that best level;
    if the book is empty on that side, C1 refuses to be the first quote and
    that side falls back to the legacy edge.

    Falls back to the legacy zone-edge formula when either the queue-aware
    walk or the dump-depth check fails on a side (thin book, escape hatch,
    zone-boundary edge case, or insufficient opposite-side depth). Final
    values are clamped to ``[0.01, 0.99]`` for safety.
    """
    legacy_bid = round(midpoint - max_spread + tick * ticks_inside, decimals)
    legacy_ask = round(midpoint + max_spread - tick * ticks_inside, decimals)

    qa_bid = _queue_aware_edge(
        "bid", merged.get("bids", []),
        midpoint, max_spread, tick, target_queue_usd, decimals,
    )
    qa_ask = _queue_aware_edge(
        "ask", merged.get("asks", []),
        midpoint, max_spread, tick, target_queue_usd, decimals,
    )

    # FX-041: two-sided dump-depth check. If either queue-aware result
    # would place close to mid but the opposite side is too thin to host
    # a passive dump, revert that side to legacy zone-edge placement.
    if qa_bid is not None and not _has_sufficient_dump_depth(
        merged.get("asks", []), midpoint, max_spread,
        shares_per_side, midpoint, dump_depth_safety_factor,
    ):
        qa_bid = None
    if qa_ask is not None and not _has_sufficient_dump_depth(
        merged.get("bids", []), midpoint, max_spread,
        shares_per_side, midpoint, dump_depth_safety_factor,
    ):
        qa_ask = None

    edge_bid = qa_bid if qa_bid is not None else legacy_bid
    edge_ask = qa_ask if qa_ask is not None else legacy_ask

    # C1 second-best-court rule: never post a quote better than the current
    # best. If the chosen edge would improve on the best level, push it one
    # tick behind. If the book is empty on that side we keep the chosen edge
    # (no one else is quoting, so "second best" is impossible).
    if second_best_court:
        adj_bid = _second_best_adjust(
            "bid", edge_bid, merged.get("bids", []),
            midpoint, max_spread, tick, decimals,
        )
        if adj_bid is not None:
            edge_bid = adj_bid
        adj_ask = _second_best_adjust(
            "ask", edge_ask, merged.get("asks", []),
            midpoint, max_spread, tick, decimals,
        )
        if adj_ask is not None:
            edge_ask = adj_ask

    edge_bid = max(0.01, edge_bid)
    edge_ask = min(0.99, edge_ask)
    return edge_bid, edge_ask


class OrderLifecycle:
    """Manages order placement, fill detection, and priority batching."""

    def __init__(self, client, db, positions, rewards, markets, dry_run=False):
        """
        Args:
            client: RateLimitedClient (CLOB API)
            db: BotDatabase instance
            positions: PositionStore instance
            rewards: RewardTracker instance
            markets: dict[str, MarketState] — shared reference with RewardFarmer
            dry_run: if True, no real orders placed
        """
        self.client = client
        self.db = db
        self.positions = positions
        self.rewards = rewards
        self.markets = markets  # shared reference — mutations visible to caller
        self.dry_run = dry_run
        self.capital_ceiling: float | None = None  # lowest cost that hit "insufficient balance"
        self.cycle_count = 0
        self._batch_idx = 0

    def cancel_order(self, order_id: str, reason: str = "", force: bool = False) -> bool:
        """Cancel an order on the exchange. Returns True on success.

        V2 SDK: cancel_order takes an OrderPayload, not a bare string.

        ``force=True`` bypasses the dry_run short-circuit and fires a real
        API cancel even in DRY/SHADOW. Used by the farmer's kill-switch
        override path (`_gated_cancel_order` propagates the flag) and by
        `_shutdown_cleanup` so any operator-poked real orders get
        cancelled before exit regardless of mode. Phase 5 audit caught
        this — previously the DRY return-True hard-shortcut defeated the
        advertised kill-switch override.
        """
        if self.dry_run and not force:
            return True
        try:
            from py_clob_client_v2.clob_types import OrderPayload
            self.client.cancel_order(OrderPayload(orderID=order_id))
            log.debug(f"Cancelled {order_id[:16]} ({reason})")
            return True
        except Exception as e:
            log.warning(f"Cancel FAILED {order_id[:16]} ({reason}): {e}")
            return False

    def capture_pre_cycle_dumps(self) -> None:
        """FX-072: snapshot outstanding dumps at the TOP of run_cycle.

        Called by reward_farmer.run_cycle BEFORE check_dump_fills. For each
        (cid, side) with a resting dump, record (shares, dump_order_id) so the
        end-of-cycle drift sweep can recover a real concurrent BUY that the
        phantom check zeroed because a dump drained on-chain without its
        unwind being recorded the same cycle (the 2026-05-25 burst shape).

        This is FREE — pure in-memory reads of dump_state / dump_orders, NO
        RPC. It also resets fx072_unwound_this_cycle for the new cycle so
        gate 3 starts clean; check_dump_fills sets it True when it records an
        unwind. Both fields are cleared by the drift sweep at end-of-cycle.
        """
        for ms in self.markets.values():
            for side in ("yes", "no"):
                ms.fx072_unwound_this_cycle[side] = False
                if ms.dump_orders[side] and ms.dump_state[side]:
                    ms.fx072_pre_cycle_dump[side] = (
                        float(ms.dump_state[side].get("shares", 0)),
                        ms.dump_orders[side],
                    )
                else:
                    ms.fx072_pre_cycle_dump[side] = None

    def _reconcile_balance_drift(self, ms: MarketState, side: str, open_ids: set | None = None) -> bool:
        """FX-054 drift catch-up sweep.

        Compares on-chain CTF balance against the position-store's tracked
        shares. If the bot's tracked count is short by ≥ 1 share, records
        a synthetic catch-up fill so the ``fills`` table stays consistent
        with reality, even when the primary detect-fills path lost a fill
        (silent DB error, swallowed phantom, cycle-skip during network
        timeout, fill arriving while we were processing dump-side fills).

        Idempotency: ``fill_event_id`` is bucketed on
        ``(cid, side, int(now / DRIFT_DEDUP_BUCKET_SEC))`` so multiple
        drift detections inside a 5-min bucket collapse to one row via
        the partial unique index. A fresh fill in the next bucket gets
        a new key and a new row.

        Returns True if a catch-up row was written, False otherwise (no
        drift, RPC failure, or duplicate within bucket).
        """
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            tid = ms.yes_tid if side == "yes" else ms.no_tid
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
            )
            on_chain = float(bal.get("balance", 0)) / 1e6
        except Exception as e:
            log.debug(
                f"[RECONCILE_DRIFT] cid={ms.cid[:12]} side={side} "
                f"step=balance_probe_failed err={type(e).__name__}: {e}"
            )
            return False

        # FX-072: dump-masked-fill add-back. When a dump SELL drained on-chain
        # this cycle but its unwind was NOT recorded (check_dump_fills deferred
        # it or mis-fired its phantom check because a concurrent BUY replenished
        # the balance), `tracked` overstates holdings by the drained amount.
        # The real concurrent BUY then gets zeroed by the phantom check and the
        # raw (on_chain - tracked) drift here can't see it either. Add the
        # drained dump shares back to on_chain so the drift surfaces — but ONLY
        # under 3 gates so we never fabricate a catch-up:
        #   GATE 1 CAPTURED  : a dump was outstanding at cycle top (cap != None)
        #   GATE 2 DRAINED   : that dump order left the book (oid not in open_ids)
        #   GATE 3 NOT-UNWOUND: check_dump_fills did NOT record the unwind this
        #                       cycle (else tracked was correctly reduced).
        cap = getattr(ms, "fx072_pre_cycle_dump", {}).get(side) if hasattr(ms, "fx072_pre_cycle_dump") else None
        if (cap is not None and open_ids is not None
                and cap[1] not in open_ids
                and not getattr(ms, "fx072_unwound_this_cycle", {}).get(side, False)):
            _drained = float(cap[0])
            if _drained > 0:
                log.warning(f"[RECONCILE_DRIFT] cid={ms.cid[:12]} side={side} step=fx072_dump_addback drained={_drained:.2f} on_chain={on_chain:.2f}->{on_chain+_drained:.2f} (dump drained but unwind not recorded this cycle)")
                on_chain = on_chain + _drained

        tracked = self.positions.get_shares(ms.cid, side)
        drift = on_chain - tracked
        if drift < 1.0:
            return False

        # Drift detected — the bot has at least 1 share more on-chain than
        # in its position store. Almost certainly a fill the primary path
        # missed; catch it up.
        now = time.time()
        bucket = int(now / DRIFT_DEDUP_BUCKET_SEC)
        event_id = f"drift:{ms.cid}:{side}:{bucket}"
        log.warning(
            f"[RECONCILE_DRIFT] cid={ms.cid[:12]} side={side} "
            f"on_chain={on_chain:.2f} tracked={tracked:.2f} drift={drift:.2f} "
            f"step=catching_up event_id={event_id[:48]}"
        )
        # Use the current slot's price if available, otherwise the market's
        # midpoint as a best-effort proxy. The catch-up fill records what
        # the bot just discovered; price accuracy on the catch-up row is
        # secondary to the row existing at all.
        slot = ms.orders.get(side)
        if slot and slot.price > 0:
            fill_price = slot.price
        elif ms.midpoint > 0:
            fill_price = ms.midpoint
        else:
            fill_price = 0.5  # last-resort placeholder
        # Synthesize a slot for handle_fill's signature requirements.
        # placed_at = now so the order_age log enrichment doesn't underflow.
        synthetic_slot = slot if slot else OrderSlot(
            order_id="", price=fill_price, shares=int(drift), placed_at=now,
        )
        # FX-054: slot.order_id is typically None here — the primary path
        # cleared it after deciding phantom_zeroed (the case this sweep
        # rescues). Coalesce to '' so we don't pass None into the
        # ``fills.order_id`` TEXT NOT NULL column (INSERT OR IGNORE
        # would silently drop the row on constraint violation).
        oid = (slot.order_id if slot and slot.order_id else "")
        self.handle_fill(
            ms, side, synthetic_slot,
            actual_shares=drift, actual_price=fill_price,
            fill_type="FULL",
            order_id=oid,
            fill_event_id=event_id,
        )
        return True

    def detect_fills(self, open_ids: set):
        """Step 3: Detect BUY order fills from exchange state.

        FX-054: at the end of the per-market loop, for any market that
        had a fill processed THIS cycle (or whose slot was JUST cleared
        by the loop), runs a drift-catchup sweep. This is the third line
        of defence against missed fills — orthogonal to the primary
        SDK-status path (root cause B: phantom-check zero) and the
        UNKNOWN-retry path (root cause from network timeouts during
        bursts). Together with the idempotent ``log_fill`` writes
        (root cause A: silent DB failures), invariant
        ``fills_count >= on_chain_BUY_count`` holds across all three
        attack surfaces.
        """
        # Track markets where an order disappeared this cycle so the
        # end-of-cycle drift sweep knows what to probe. Subtracted from
        # this is `primary_handled` — pairs where the primary path
        # already called handle_fill (positions store has been updated;
        # drift sweep would double-count). The drift sweep therefore
        # only fires on (cid, side) pairs whose order disappeared
        # without a primary handle_fill — i.e., phantom_zeroed branch
        # AND UNKNOWN-with-no-surplus branch — exactly the gap classes
        # FX-054 is designed to close.
        cids_processed: set[tuple[str, str]] = set()
        primary_handled: set[tuple[str, str]] = set()
        for cid, ms in list(self.markets.items()):
            for side in ["yes", "no"]:
                slot = ms.orders[side]
                if not slot.order_id:
                    continue

                if self.dry_run:
                    slot.order_id = None
                    continue

                if slot.order_id not in open_ids:
                    # FX-054: any time the order disappears from the
                    # exchange's open-list, mark this (cid, side) for the
                    # end-of-cycle drift sweep — regardless of which
                    # detection branch we end up in. This catches the
                    # case where the SDK path silently dropped the fill
                    # (root causes A or B) or returned UNKNOWN without
                    # the unknown_count threshold tripping yet.
                    cids_processed.add((cid, side))
                    # FX-054 instrumentation: trace every fill-detection branch
                    # so we can diagnose why 8 of 9 fills went missing from the
                    # DB on 2026-05-25.
                    log.info(
                        f"[FILL_DETECT_TRACE] cid={cid[:12]} side={side} "
                        f"order_id={slot.order_id[:16]} step=missing_from_open_ids"
                    )
                    try:
                        status = self.client.get_order(slot.order_id)
                        order_status = status.get("status", "UNKNOWN")
                        matched = float(status.get("size_matched", 0))
                        log.info(
                            f"[FILL_DETECT_TRACE] cid={cid[:12]} side={side} "
                            f"order_id={slot.order_id[:16]} step=sdk_resp "
                            f"status={order_status} matched={matched:.2f}"
                        )
                    except Exception as e:
                        # FX-054: this exception path is the prime suspect for
                        # the 8 missing fills — get_order timeouts during burst
                        # silently route every fill into UNKNOWN with matched=0.
                        log.warning(
                            f"[FILL_DETECT_TRACE] cid={cid[:12]} side={side} "
                            f"order_id={slot.order_id[:16]} step=sdk_exception "
                            f"err={type(e).__name__}: {e}"
                        )
                        order_status = "UNKNOWN"
                        matched = 0

                    if matched > 0 and order_status in ("MATCHED", "CANCELLED"):
                        # FX-037: BUY-side phantom-fill defense.
                        # On 2026-05-19 the V2 SDK reported size_matched=158 NO
                        # shares for an order that delivered only 38 on-chain;
                        # the inflated fills row cascaded I7 → SafetyController
                        # → kill switch (realized loss $19.55). Mirror
                        # DumpManager.check_dump_fills' on-chain probe (see
                        # dump_manager.py:60-87) on the BUY side. Symmetric
                        # defense — fail-open on probe error preserves SDK
                        # value so legitimate fills aren't lost to network
                        # blips. FX-054: now passes ``slot`` so the phantom
                        # check can apply balance-lag tolerance for orders
                        # placed within the last 60s (avoids zeroing legit
                        # fills before the CTF transfer confirms).
                        pre_phantom_matched = matched
                        matched = self._check_buy_phantom_fill(ms, side, matched, slot=slot)
                        if matched != pre_phantom_matched:
                            log.info(
                                f"[FILL_DETECT_TRACE] cid={cid[:12]} side={side} "
                                f"step=phantom_adjusted pre={pre_phantom_matched:.2f} "
                                f"post={matched:.2f}"
                            )
                    if matched > 0 and order_status in ("MATCHED", "CANCELLED"):
                        fill_type = "FULL" if matched >= slot.shares - 0.5 else "PARTIAL"
                        # API returns token-specific CLOB price; convert to
                        # YES-equiv so all internal pricing is consistent.
                        # For YES orders CLOB==YES-equiv; for NO orders
                        # CLOB price is 1-YES-equiv, so to_yes_equiv flips it.
                        raw_api_price = float(status.get("price", 0))
                        if raw_api_price > 0:
                            from price import to_yes_equiv
                            actual_price = to_yes_equiv(raw_api_price, side)
                        else:
                            actual_price = slot.price  # already YES-equiv
                        if fill_type == "PARTIAL":
                            log.info(
                                f"PARTIAL fill {side.upper()} {matched:.0f}/{slot.shares:.0f}sh "
                                f"(order {order_status}) | {ms.question[:30]}"
                            )
                        # FX-054: dedup key derives from order_id + matched
                        # quantity so re-detection of the same logical fill
                        # is collapsed by the DB partial unique index, but a
                        # subsequent partial-fill increment on the same order
                        # writes a distinct row.
                        event_id = f"sdk:{slot.order_id}:{int(matched)}"
                        self.handle_fill(
                            ms, side, slot,
                            actual_shares=matched, actual_price=actual_price,
                            fill_type=fill_type,
                            order_id=slot.order_id,
                            fill_event_id=event_id,
                        )
                        # FX-054: primary path took it — drift sweep skipped
                        # for this (cid, side) to avoid double-counting against
                        # the now-updated positions store.
                        primary_handled.add((cid, side))
                        log.info(
                            f"[FILL_DETECT_TRACE] cid={cid[:12]} side={side} "
                            f"step=fill_recorded shares={matched:.2f} "
                            f"fill_type={fill_type} actual_price={actual_price:.4f}"
                        )
                        ms.unknown_count[side] = 0
                    elif matched <= 0 and order_status in ("MATCHED", "CANCELLED"):
                        # FX-037: phantom check zeroed the fill (full phantom).
                        # SDK said size_matched > 0 but on-chain delta was 0.
                        # Treat as no-fill — clear the slot below, do not record.
                        log.warning(
                            f"[FILL_DETECT_TRACE] cid={cid[:12]} side={side} "
                            f"step=phantom_zeroed status={order_status} "
                            f"(SDK said matched > 0, on-chain delta = 0; no DB write)"
                        )
                        ms.unknown_count[side] = 0
                    elif order_status == "UNKNOWN":
                        ms.unknown_count[side] = ms.unknown_count.get(side, 0) + 1
                        log.info(
                            f"[FILL_DETECT_TRACE] cid={cid[:12]} side={side} "
                            f"step=unknown_status count={ms.unknown_count[side]}/"
                            f"{cfg('RF_UNKNOWN_RETRY_THRESHOLD')}"
                        )
                        if ms.unknown_count[side] >= cfg("RF_UNKNOWN_RETRY_THRESHOLD"):
                            log.warning(f"BUY order stuck UNKNOWN {cfg('RF_UNKNOWN_RETRY_THRESHOLD')}x, clearing | {ms.question[:30]}")
                            # Reconcile: check exchange balance to detect silent fills
                            if self._reconcile_after_unknown(ms, side, slot):
                                # FX-054: surplus found → handle_fill called →
                                # mark primary-handled so drift sweep skips it.
                                primary_handled.add((cid, side))
                            self.db.delete_active_order(slot.order_id)
                            slot.order_id = None
                            ms.unknown_count[side] = 0
                        else:
                            log.warning(f"Order {slot.order_id[:16]} UNKNOWN ({ms.unknown_count[side]}/{cfg('RF_UNKNOWN_RETRY_THRESHOLD')})")
                        continue
                    else:
                        ms.unknown_count[side] = 0
                    if slot.order_id:
                        self.db.delete_active_order(slot.order_id)
                    slot.order_id = None

                else:
                    # Order IS in open_ids (exchange says it's live).
                    # Periodically force-check for partial fills that the
                    # exchange hasn't removed from open_ids yet. Without this,
                    # a partially filled order sits forever — shares accumulate
                    # untracked and the slot stays occupied at a stale price.
                    self._check_stale_order(ms, side, slot)

        # FX-054 drift catch-up sweep. Targeted at the (cid, side) pairs
        # whose order disappeared this cycle BUT didn't go through the
        # primary handle_fill path — i.e., phantom_zeroed and
        # UNKNOWN-with-no-surplus. For each, compare on-chain CTF
        # balance against tracked shares; if on-chain leads tracked by
        # ≥ 1 share, the bot missed a fill and we write a catch-up row
        # with a 5-minute-bucketed event_id (partial unique index
        # collapses repeated detections).
        #
        # Bounded API cost: 1 RPC per missed-detection observation per
        # cycle, never per market. In production at <1 fill/day target
        # rate this is typically 0 RPC/cycle on healthy operation; only
        # fires when the primary path silently failed.
        for cid, side in cids_processed - primary_handled:
            ms = self.markets.get(cid)
            if ms is None:
                continue
            try:
                # FX-072: pass open_ids so the drift sweep's dump-mask
                # add-back can apply gate 2 (dump order left the book).
                self._reconcile_balance_drift(ms, side, open_ids=open_ids)
            except Exception as e:
                log.warning(
                    f"[RECONCILE_DRIFT] cid={cid[:12]} side={side} "
                    f"step=sweep_exception err={type(e).__name__}: {e}"
                )

        # FX-072: clear the per-cycle dump capture so a stale snapshot can't
        # leak an add-back into a later cycle. capture_pre_cycle_dumps()
        # re-arms it at the top of the next run_cycle.
        for ms in self.markets.values():
            if hasattr(ms, "fx072_pre_cycle_dump"):
                ms.fx072_pre_cycle_dump = {"yes": None, "no": None}

    def handle_fill(self, ms: MarketState, side: str, slot: OrderSlot,
                    actual_shares: float = 0, actual_price: float = 0.0,
                    fill_type: str = "FULL",
                    order_id: str = "",
                    fill_event_id: str = ""):
        """Process a detected fill: record, then merge or dump.

        FX-039: ``fill_type`` is threaded through from the caller (detect_fills
        and _check_stale_order both compute it) so the ``fills`` DB row carries
        the correct PARTIAL/FULL label. Defaults to "FULL" for the
        _reconcile_after_unknown caller which has no SDK-reported matched
        size to distinguish.

        FX-054: ``order_id`` + ``fill_event_id`` flow through to
        ``db.log_fill`` for idempotent persistence. Callers should supply
        a stable ``fill_event_id`` per logical fill so retries (network
        timeout retry, drift-sweep catch-up) collapse to one row via
        the partial unique index. Missing event_id (legacy callers /
        tests) keeps append-only semantics.

        Now also checks the DB write result and emits an honest
        ``[FILL_WRITE]`` log line: ``succeeded`` only on actual insert,
        ``duplicate`` on idempotent collision, ``FAILED`` on error. The
        old behaviour logged ``succeeded`` unconditionally — masking the
        DB exception path that swallowed errors at debug level.
        """
        from alerts import alert_fill
        from dump_manager import DumpManager

        filled_shares = actual_shares if actual_shares > 0 else slot.shares
        fill_price = actual_price if actual_price > 0 else slot.price
        cid = ms.cid

        log.info(
            f"FILL {side.upper()} {filled_shares:.0f}sh @ {fill_price:.4f} | "
            f"{ms.question[:35]}"
        )

        # FX-065: guard the positions update with the fills idempotency key.
        # The fills-table write (log_fill below) is already idempotent via
        # INSERT OR IGNORE on fill_event_id, but PositionStore.record_fill was
        # NOT — so a re-handled fill (network retry, SDK-detect then
        # stale-check on a grown partial, drift-sweep overlap) double-counted
        # shares and corrupted VWAP, which then fed the dump cost-basis
        # (FX-066) and the kill-switch loss math. Only the positions mutation
        # is guarded; the [FILL_WRITE] instrumentation + dump re-attempt below
        # run unchanged (dump_position is balance-clamped, so re-attempting a
        # dump on an already-recorded fill is a no-op / harmless).
        if fill_event_id and self.db.fill_event_exists(fill_event_id):
            log.info(
                f"[FILL_WRITE] cid={cid[:12]} side={side} shares={filled_shares:.2f} "
                f"step=positions_skip_duplicate event_id={fill_event_id[:32]} (FX-065)"
            )
        else:
            self.positions.record_fill(cid, side, filled_shares, fill_price, question=ms.question)

        from price import to_clob
        clob_cost = to_clob(fill_price, side)
        # Compute enrichment data for Phase 0 learning
        _order_age = time.time() - slot.placed_at if slot.placed_at > 0 else 0
        _pos_usd = 0.0
        try:
            _yes_sh = self.positions.get_shares(cid, "yes")
            _no_sh = self.positions.get_shares(cid, "no")
            _yes_cost = _yes_sh * self.positions.get_avg_price(cid, "yes")
            _no_cost = _no_sh * (1 - self.positions.get_avg_price(cid, "no")) if _no_sh > 0 else 0
            _pos_usd = _yes_cost + _no_cost
        except Exception:
            pass
        # FX-054 instrumentation + idempotency check.
        # [FILL_WRITE] attempting must always be followed by EXACTLY ONE of
        # [FILL_WRITE] succeeded / duplicate / FAILED. The smoking gun for
        # the 2026-05-25 incident would have been many `attempting` rows
        # followed by FAILED — invisible in the pre-FX-054 code because
        # log_fill swallowed exceptions at debug level + always logged
        # `succeeded`.
        log.info(
            f"[FILL_WRITE] cid={cid[:12]} side={side} shares={filled_shares:.2f} "
            f"price={fill_price:.4f} step=attempting event_id={fill_event_id[:32]}"
        )
        if ms.midpoint > 0:
            mkt = ms.midpoint if side == "yes" else (1.0 - ms.midpoint)
            slip = clob_cost - mkt
        else:
            slip = 0.0
        inserted = self.db.log_fill(
            condition_id=cid, question=ms.question,
            side=side, fill_type=fill_type,
            shares=filled_shares, price=fill_price,
            clob_cost=clob_cost, usd_value=filled_shares * clob_cost,
            midpoint=ms.midpoint,
            slippage=slip,
            order_age_secs=_order_age,
            position_usd_after=_pos_usd,
            reward_rate_hr=ms.daily_rate / 24.0 if ms.daily_rate > 0 else 0,
            order_id=order_id,
            fill_event_id=fill_event_id,
        )
        if inserted:
            log.info(
                f"[FILL_WRITE] cid={cid[:12]} side={side} shares={filled_shares:.2f} "
                f"step=succeeded"
            )
            # Phase 5d: pre-emptive cooldown on adverse slippage
            try:
                thresh = float(cfg("RF_PREEMPTIVE_SLIPPAGE_USD") or 0.0)
                if thresh > 0 and slip > thresh:
                    from decision_policy import DecisionPolicy
                    from market_roi_tracker import MarketROITracker
                    pol = DecisionPolicy(
                        db_path=self.db._db_path,  # noqa: SLF001
                        tracker=MarketROITracker(
                            db_path=self.db._db_path, funder="",  # noqa: SLF001
                        ),
                    )
                    pol.preemptive_cooldown(
                        cid, f"slippage={slip:.4f}>{thresh:.4f}",
                    )
            except Exception:
                pass
        elif fill_event_id:
            # FX-054: distinguish idempotent collision (safe, expected on
            # retry) from genuine DB error. We can tell by re-querying for
            # the event id; presence ⇒ duplicate, absence ⇒ error.
            try:
                row = self.db._get_conn().execute(  # noqa: SLF001
                    "SELECT 1 FROM fills WHERE fill_event_id = ? LIMIT 1",
                    (fill_event_id,),
                ).fetchone()
            except Exception:
                row = None
            if row:
                log.info(
                    f"[FILL_WRITE] cid={cid[:12]} side={side} shares={filled_shares:.2f} "
                    f"step=duplicate event_id={fill_event_id[:32]}"
                )
            else:
                log.error(
                    f"[FILL_WRITE] cid={cid[:12]} side={side} shares={filled_shares:.2f} "
                    f"step=FAILED event_id={fill_event_id[:32]} "
                    f"(log_fill returned False AND row absent — DB write actually failed)"
                )
        else:
            # No event_id supplied (legacy caller). Can't disambiguate
            # collision vs error; surface as FAILED because the legacy
            # append-only contract treats False as "did not insert".
            log.error(
                f"[FILL_WRITE] cid={cid[:12]} side={side} shares={filled_shares:.2f} "
                f"step=FAILED (no event_id; log_fill returned False)"
            )

        # FX-039 follow-up: alerts.py's PARTIAL branch formats remaining_shares
        # unconditionally and crashes on None. Pre-FX-039 the hardcoded
        # fill_type='FULL' masked this latent bug. Pass the remainder explicitly
        # so partial-fill alerts work.
        remaining_shares = max(0.0, slot.shares - filled_shares)
        alert_fill(
            fill_type=fill_type, side=side.upper(),
            price=clob_cost, filled_shares=filled_shares,
            filled_usd=filled_shares * clob_cost,
            market_question=ms.question,
            remaining_shares=remaining_shares,
        )

        ms.last_fill_price[side] = fill_price
        _fill_ts = time.time()
        ms.fill_times[side].append(_fill_ts)
        # FX-069: also record into the kill-switch history (separate from the
        # 180s can_place breaker buffer) and prune it to the 6h kill baseline
        # so the fill-rate spike kill can detect slow bleed. can_place's 180s
        # prune intentionally does NOT touch this list.
        ms.kill_fill_times.append(_fill_ts)
        _kill_hist_window = cfg("RF_KILL_FILL_HISTORY_SECS")
        ms.kill_fill_times = [
            t for t in ms.kill_fill_times if _fill_ts - t < _kill_hist_window
        ]

        yes_shares = self.positions.get_shares(cid, "yes")
        no_shares = self.positions.get_shares(cid, "no")
        merge_qty = min(yes_shares, no_shares)
        if merge_qty >= 1.0:
            # Use the dump_manager reference from the farmer
            self._dump_mgr.try_merge(ms, merge_qty)
            return

        self._dump_mgr.dump_position(ms, side, filled_shares)

    def set_dump_manager(self, dump_mgr):
        """Set reference to DumpManager (avoids circular import at init)."""
        self._dump_mgr = dump_mgr

    def place_orders_for_market(self, ms: MarketState) -> int:
        """Fetch book + place edge orders for one market.

        Returns the number of API-confirmed placements this call produced
        (0, 1, or 2). Only LIVE-mode placements that received a valid
        ``orderID`` from ``client.create_and_post_order`` AND wrote a row
        to the ``orders_placed`` DB table contribute to this count. Early
        returns (no book, wide spread, resolution proximity, sports block,
        has-both shortcut, unliquidatable-gate, fast-vol timeout) and
        DRY-run-mode placements return 0 — those do not write to
        ``orders_placed``.

        Drives FX-004 (telemetry / DB consistency): the caller accumulates
        this value into ``_cycle_orders_placed`` so ``[CYCLE_SUMMARY]
        orders_placed`` matches ``SELECT COUNT(*) FROM orders_placed`` for
        the cycle window.

        FX-005 / FX-007: gates on ``db.is_unliquidatable(cid)`` before
        fetching the book; the orderbook for a marked cid is known dead
        and any further BUY would just 400 again.
        """
        from py_clob_client_v2.clob_types import OrderArgs
        from py_clob_client_v2.order_builder.constants import BUY

        placed_count = 0

        # FX-005 / FX-007 gate: skip BUY placement on cids whose orderbook
        # the bot has already confirmed dead. Re-enabled only by the
        # periodic re-probe (FX-028) in reward_farmer.
        if self.db.is_unliquidatable(ms.cid):
            return placed_count

        now = time.time()

        # FX-098: enforce an active fast-volatility timeout immediately,
        # before the has-both shortcut can skip the book fetch.
        if ms.fast_vol_timeout_until > now:
            for side in ("yes", "no"):
                slot = ms.orders[side]
                if slot.order_id:
                    if self.cancel_order(slot.order_id, reason="fast_vol_timeout"):
                        self.db.delete_active_order(slot.order_id)
                        slot.order_id = None
            self.db.write_placement_feedback(ms.cid, "yes", "skipped", "fast_vol_timeout")
            self.db.write_placement_feedback(ms.cid, "no", "skipped", "fast_vol_timeout")
            return placed_count

        # Skip book fetch if both sides already have orders — saves 2 API calls.
        # Still fetch if a book refresh is due (every RF_RESTING_BOOK_MAX_AGE_SECS)
        # so repricing, liquidity, and fast-vol guards stay active.
        has_both = ms.orders["yes"].order_id and ms.orders["no"].order_id
        book_age = now - ms.last_book_fetch
        if has_both and book_age < cfg("RF_RESTING_BOOK_MAX_AGE_SECS"):
            return placed_count

        merged = get_merged_book(self.client, ms.yes_tid, ms.no_tid)
        if not merged or not merged["bids"] or not merged["asks"]:
            ms.book_failures += 1
            return placed_count

        ms.book_failures = 0  # reset on success
        best_bid = float(merged["bids"][0]["price"])
        best_ask = float(merged["asks"][0]["price"])
        midpoint = (best_bid + best_ask) / 2
        ms.midpoint = midpoint
        # Cache the book so record_cycle (reward_farmer.py) can feed it to
        # estimate_market_q without refetching. TTL enforced at read site
        # via RF_BOOK_CACHE_TTL.
        ms.cached_book = merged
        ms.last_book_fetch = time.time()

        # ── Phase 0: Log book snapshot (zero extra API calls) ──
        try:
            self.db.log_book_snapshot(
                condition_id=ms.cid, merged=merged,
                best_bid=best_bid, best_ask=best_ask, midpoint=midpoint,
                our_bid=ms.orders["yes"].price if ms.orders["yes"].order_id else 0,
                our_ask=ms.orders["no"].price if ms.orders["no"].order_id else 0,
                daily_rate=ms.daily_rate, max_spread=ms.max_spread,
                agent_shares=ms.agent_shares,
            )
        except Exception:
            pass  # never break production

        # FX-098: fast-volatility timeout guard. Check midpoint range over the
        # last 30s/60s using the snapshots we just logged. If triggered, cancel
        # resting orders and block placement for RF_FAST_VOL_TIMEOUT_SECS.
        if check_fast_vol_timeout(ms, self.db, now=now):
            log.info(
                f"SKIP fast-vol timeout | until={ms.fast_vol_timeout_until:.0f} "
                f"| {ms.question[:30]}"
            )
            self.db.write_placement_feedback(
                ms.cid, "yes", "skipped", "fast_vol_timeout",
            )
            self.db.write_placement_feedback(
                ms.cid, "no", "skipped", "fast_vol_timeout",
            )
            for side in ("yes", "no"):
                slot = ms.orders[side]
                if slot.order_id:
                    if self.cancel_order(slot.order_id, reason="fast_vol_timeout"):
                        self.db.delete_active_order(slot.order_id)
                        slot.order_id = None
            return placed_count

        # Phase 5b: farmer-side FX-093 volatility mirror (~30s cadence)
        vol_cap = cfg("RF_ALLOC_MAX_RECENT_VOLATILITY")
        if vol_cap and vol_cap > 0:
            try:
                window_h = cfg("RF_ALLOC_VOLATILITY_WINDOW_HOURS")
                min_samples = cfg("RF_ALLOC_VOLATILITY_MIN_SAMPLES")
                cutoff = time.time() - float(window_h) * 3600.0
                row = self.db._get_conn().execute(
                    "SELECT MAX(midpoint), MIN(midpoint), COUNT(*) "
                    "FROM book_snapshots WHERE condition_id = ? AND ts >= ?",
                    (ms.cid, cutoff),
                ).fetchone()
                if row and row[2] and int(row[2]) >= int(min_samples):
                    recent_vol = float(row[0]) - float(row[1])
                    if recent_vol > float(vol_cap):
                        log.info(
                            f"SKIP volatility guard | range={recent_vol:.3f} "
                            f"| {ms.question[:30]}"
                        )
                        self.db.write_placement_feedback(
                            ms.cid, "yes", "skipped", "volatility_guard",
                        )
                        self.db.write_placement_feedback(
                            ms.cid, "no", "skipped", "volatility_guard",
                        )
                        for side in ("yes", "no"):
                            slot = ms.orders[side]
                            if slot.order_id:
                                if self.cancel_order(slot.order_id, reason="volatility_guard"):
                                    self.db.delete_active_order(slot.order_id)
                                    slot.order_id = None
                        return placed_count
            except Exception:
                pass  # fail-open

        if best_ask - best_bid > cfg("RF_MAX_BOOK_SPREAD"):
            self.db.write_placement_feedback(ms.cid, "yes", "skipped", "wide_spread")
            self.db.write_placement_feedback(ms.cid, "no", "skipped", "wide_spread")
            return placed_count

        # ── Resolution proximity guard (real-time) ──
        # The agent detects this every ~30min, but markets can move fast.
        # Block placement if midpoint suggests the market is near resolution.
        # This closes the gap between agent cycles and prevents placing on
        # markets that moved to 0.95 after the last agent run.
        if midpoint > 0.90 or midpoint < 0.10:
            log.info(
                f"SKIP resolution proximity | mid={midpoint:.3f} | {ms.question[:30]}"
            )
            self.db.write_placement_feedback(ms.cid, "yes", "skipped", "resolution_proximity")
            self.db.write_placement_feedback(ms.cid, "no", "skipped", "resolution_proximity")
            # Also cancel any existing orders — don't stay exposed
            for side in ("yes", "no"):
                slot = ms.orders[side]
                if slot.order_id:
                    if self.cancel_order(slot.order_id, reason="resolution_proximity"):
                        self.db.delete_active_order(slot.order_id)
                        slot.order_id = None
            return placed_count

        # ── Live sports guard (Layer 2) ──
        # Sports markets near expiry have extreme adverse selection risk.
        # Block if: sports + (< 4h to expiry OR missing end_date).
        # This is defense-in-depth — the agent (Layer 1) should have
        # already avoided these, but if one slips through, block here.
        if ms.question:
            from config import SPORTS_KEYWORDS, RF_SPORTS_BLOCK_HOURS
            q_lower = ms.question.lower()
            _is_sports_q = any(kw in q_lower for kw in SPORTS_KEYWORDS)

            if _is_sports_q:
                _block_sports = False
                _block_reason = ""

                if not ms.end_date_iso:
                    # No end_date on a sports market = no proof it's safe
                    _block_sports = True
                    _block_reason = "sports_no_expiry"
                    log.info(
                        f"BLOCK sports (no expiry date) | {ms.question[:40]}"
                    )
                else:
                    try:
                        from datetime import datetime, timezone
                        dt = datetime.fromisoformat(ms.end_date_iso.replace("Z", "+00:00"))
                        hours_to_expiry = (dt - datetime.now(timezone.utc)).total_seconds() / 3600
                        if hours_to_expiry <= 0:
                            _block_sports = True
                            _block_reason = "sports_expired"
                            log.info(
                                f"BLOCK sports (already expired) | {ms.question[:40]}"
                            )
                        elif hours_to_expiry <= RF_SPORTS_BLOCK_HOURS:
                            _block_sports = True
                            _block_reason = "live_sports"
                            log.info(
                                f"BLOCK live sports | expires in {hours_to_expiry:.1f}h "
                                f"(< {RF_SPORTS_BLOCK_HOURS}h) | {ms.question[:40]}"
                            )
                    except Exception:
                        # Can't parse date on sports market — block to be safe
                        _block_sports = True
                        _block_reason = "sports_bad_date"
                        log.info(
                            f"BLOCK sports (unparseable date) | {ms.question[:40]}"
                        )

                if _block_sports:
                    self.db.write_placement_feedback(ms.cid, "yes", "skipped", _block_reason)
                    self.db.write_placement_feedback(ms.cid, "no", "skipped", _block_reason)
                    for side in ("yes", "no"):
                        slot = ms.orders[side]
                        if slot.order_id:
                            if self.cancel_order(slot.order_id, reason=_block_reason):
                                self.db.delete_active_order(slot.order_id)
                                slot.order_id = None
                    return placed_count

        tick = ms.tick_size
        decimals = max(2, len(str(tick).rstrip('0').split('.')[-1]))
        edge_bid, edge_ask = _compute_edge_prices(
            merged=merged,
            midpoint=midpoint,
            max_spread=ms.max_spread,
            tick=tick,
            decimals=decimals,
            ticks_inside=PLACEMENT_TICKS_INSIDE(),
            target_queue_usd=_effective_target_queue_usd(ms.cid),
            shares_per_side=ms.agent_shares if ms.agent_shares > 0 else SHARES_PER_SIDE(),
            dump_depth_safety_factor=DUMP_DEPTH_SAFETY_FACTOR(),
            second_best_court=_second_best_court_enabled(ms.cid),
        )

        # Reprice stale orders outside reward window
        for side, edge_price in [("yes", edge_bid), ("no", edge_ask)]:
            slot = ms.orders[side]
            if not slot.order_id:
                continue
            order_dist = abs(slot.price - midpoint)
            if order_dist >= ms.max_spread:
                if self.cancel_order(slot.order_id, reason="outside_reward_window"):
                    log.info(
                        f"REPRICE {side.upper()} | old={slot.price:.3f} dist={order_dist:.3f} >= spread={ms.max_spread:.3f} | "
                        f"new={edge_price:.3f} | {ms.question[:30]}"
                    )
                    self.db.delete_active_order(slot.order_id)
                    slot.order_id = None

        # Exit liquidity check
        exit_buf = cfg("RF_DUMP_EXIT_DEPTH_BUFFER")
        yes_exit_depth = sum(
            float(b["size"]) for b in merged["bids"]
            if float(b["price"]) >= edge_bid - exit_buf
        )
        no_exit_depth = sum(
            float(a["size"]) for a in merged["asks"]
            if float(a["price"]) <= edge_ask + exit_buf
        )
        effective_shares = ms.agent_shares if ms.agent_shares > 0 else SHARES_PER_SIDE()
        can_exit_yes = yes_exit_depth >= effective_shares
        can_exit_no = no_exit_depth >= effective_shares

        if not can_exit_yes and not ms.orders["yes"].order_id:
            self.db.write_placement_feedback(ms.cid, "yes", "skipped", "exit_liquidity")
        if not can_exit_no and not ms.orders["no"].order_id:
            self.db.write_placement_feedback(ms.cid, "no", "skipped", "exit_liquidity")

        # Sizing
        shares_target = ms.agent_shares if ms.agent_shares > 0 else SHARES_PER_SIDE()
        yes_shares = max(ms.min_size, shares_target)
        no_clob = round(1.0 - edge_ask, decimals)
        no_clob = max(0.01, no_clob)
        no_shares = max(ms.min_size, shares_target)

        # Place YES bid
        if can_exit_yes:
            can, reason = self.can_place(ms.cid, "yes", yes_shares * edge_bid)
            if can:
                if self.dry_run:
                    log.info(f"[DRY] BID YES @ {edge_bid:.3f} ({yes_shares:.0f}sh) | {ms.question[:30]}")
                    ms.orders["yes"] = OrderSlot(order_id="dry_yes", price=edge_bid, shares=yes_shares, placed_at=time.time())
                    self.db.write_placement_feedback(ms.cid, "yes", "placed", "")
                else:
                    try:
                        args = OrderArgs(token_id=ms.yes_tid, price=edge_bid, size=float(yes_shares), side=BUY)
                        resp = self.client.create_and_post_order(args)
                        oid = resp.get("orderID") if isinstance(resp, dict) else None
                        if oid:
                            ms.orders["yes"] = OrderSlot(order_id=oid, price=edge_bid, shares=yes_shares, placed_at=time.time())
                            self.db.log_order_placed(condition_id=ms.cid, side="yes", price=edge_bid, size=float(yes_shares), order_id=oid)
                            self.db.save_active_order(oid, ms.cid, "yes", "buy", edge_bid, yes_shares)
                            self.db.write_placement_feedback(ms.cid, "yes", "placed", "")
                            placed_count += 1
                            if self.cycle_count <= 3:
                                log.info(f"BID YES @ {edge_bid:.3f} ({yes_shares:.0f}sh) | {ms.question[:30]}")
                        else:
                            self.db.write_placement_feedback(ms.cid, "yes", "failed", "no_order_id")
                            log.warning(f"YES order got no orderID | {ms.question[:25]}")
                    except Exception as e:
                        err_str = str(e).lower()
                        # FX-005 / FX-007: definitive dead-orderbook signal.
                        # Canonical V2 SDK body: "the orderbook {cid} does
                        # not exist". Require BOTH substrings; tight enough
                        # to skip "insufficient balance" / "rate limit" /
                        # "market does not exist", loose enough to handle
                        # the cid in the middle.
                        orderbook_dead = (
                            "orderbook" in err_str and "does not exist" in err_str
                        )
                        # Closed/resolved markets may return "invalid token id".
                        invalid_token = "invalid token id" in err_str
                        if orderbook_dead or invalid_token:
                            reason = "buy_yes_invalid_token" if invalid_token else "buy_yes_orderbook_gone"
                            log.warning(
                                f"Marking {ms.cid[:16]} unliquidatable: "
                                f"{'invalid token' if invalid_token else 'orderbook gone'} "
                                f"(YES BUY) | {ms.question[:30]}"
                            )
                            self.db.mark_unliquidatable(ms.cid, reason=reason)
                            self.db.write_placement_feedback(ms.cid, "yes", "failed", "invalid_token" if invalid_token else "orderbook_gone")
                            # Don't try NO either — same orderbook is dead.
                            return placed_count
                        if "insufficient" in err_str or "balance" in err_str or "not enough" in err_str:
                            failed_cost = yes_shares * edge_bid
                            prev = self.capital_ceiling
                            self.capital_ceiling = min(prev if prev is not None else float('inf'), failed_cost)
                            log.warning(
                                f"Insufficient balance (YES, ${failed_cost:.1f}) — "
                                f"ceiling=${self.capital_ceiling:.1f} | {ms.question[:30]}"
                            )
                            self.db.write_placement_feedback(ms.cid, "yes", "failed", "capital_exhausted")
                            # Don't return — let NO side try if it's cheaper
                        else:
                            self.db.write_placement_feedback(ms.cid, "yes", "failed", "order_error")
                            log.debug(f"YES order failed {ms.question[:25]}: {e}")
            else:
                if reason not in ("already_has_order", "dump_pending"):
                    self.db.write_placement_feedback(ms.cid, "yes", "skipped", reason)

        # Place NO ask
        if can_exit_no:
            can, reason = self.can_place(ms.cid, "no", no_shares * no_clob)
            if can:
                if self.dry_run:
                    log.info(f"[DRY] ASK NO @ {edge_ask:.3f} (clob={no_clob:.3f}, {no_shares:.0f}sh) | {ms.question[:30]}")
                    ms.orders["no"] = OrderSlot(order_id="dry_no", price=edge_ask, shares=no_shares, placed_at=time.time())
                    self.db.write_placement_feedback(ms.cid, "no", "placed", "")
                else:
                    try:
                        args = OrderArgs(token_id=ms.no_tid, price=no_clob, size=float(no_shares), side=BUY)
                        resp = self.client.create_and_post_order(args)
                        oid = resp.get("orderID") if isinstance(resp, dict) else None
                        if oid:
                            ms.orders["no"] = OrderSlot(order_id=oid, price=edge_ask, shares=no_shares, placed_at=time.time())
                            self.db.log_order_placed(condition_id=ms.cid, side="no", price=edge_ask, size=float(no_shares), order_id=oid)
                            self.db.save_active_order(oid, ms.cid, "no", "buy", edge_ask, no_shares)
                            self.db.write_placement_feedback(ms.cid, "no", "placed", "")
                            placed_count += 1
                            if self.cycle_count <= 3:
                                log.info(f"ASK NO @ {edge_ask:.3f} (clob={no_clob:.3f}, {no_shares:.0f}sh) | {ms.question[:30]}")
                        else:
                            self.db.write_placement_feedback(ms.cid, "no", "failed", "no_order_id")
                            log.warning(f"NO order got no orderID | {ms.question[:25]}")
                    except Exception as e:
                        err_str = str(e).lower()
                        # FX-005 / FX-007: definitive dead-orderbook signal.
                        # Canonical V2 SDK body: "the orderbook {cid} does
                        # not exist". Require BOTH substrings; tight enough
                        # to skip "insufficient balance" / "rate limit" /
                        # "market does not exist", loose enough to handle
                        # the cid in the middle.
                        orderbook_dead = (
                            "orderbook" in err_str and "does not exist" in err_str
                        )
                        # Closed/resolved markets may return "invalid token id".
                        invalid_token = "invalid token id" in err_str
                        if orderbook_dead or invalid_token:
                            reason = "buy_no_invalid_token" if invalid_token else "buy_no_orderbook_gone"
                            log.warning(
                                f"Marking {ms.cid[:16]} unliquidatable: "
                                f"{'invalid token' if invalid_token else 'orderbook gone'} "
                                f"(NO BUY) | {ms.question[:30]}"
                            )
                            self.db.mark_unliquidatable(ms.cid, reason=reason)
                            self.db.write_placement_feedback(ms.cid, "no", "failed", "invalid_token" if invalid_token else "orderbook_gone")
                            return placed_count
                        if "insufficient" in err_str or "balance" in err_str or "not enough" in err_str:
                            failed_cost = no_shares * no_clob
                            prev = self.capital_ceiling
                            self.capital_ceiling = min(prev if prev is not None else float('inf'), failed_cost)
                            log.warning(
                                f"Insufficient balance (NO, ${failed_cost:.1f}) — "
                                f"ceiling=${self.capital_ceiling:.1f} | {ms.question[:30]}"
                            )
                            self.db.write_placement_feedback(ms.cid, "no", "failed", "capital_exhausted")
                        else:
                            self.db.write_placement_feedback(ms.cid, "no", "failed", "order_error")
                            log.debug(f"NO order failed {ms.question[:25]}: {e}")
            else:
                if reason not in ("already_has_order", "dump_pending"):
                    self.db.write_placement_feedback(ms.cid, "no", "skipped", reason)

        return placed_count

    def get_priority_batch(self, market_list: list) -> list:
        """Priority-based batch: empty slots first (highest daily_rate), then rotation."""
        needs_orders = []
        has_orders = []

        for ms in market_list:
            empty_slots = sum(
                1 for s in ["yes", "no"]
                if not ms.orders[s].order_id and not ms.dump_orders[s]
            )
            if empty_slots > 0:
                needs_orders.append(ms)
            else:
                has_orders.append(ms)

        needs_orders.sort(key=lambda x: x.daily_rate, reverse=True)
        batch = needs_orders[:BATCH_SIZE()]

        if len(batch) < BATCH_SIZE() and has_orders:
            remaining = BATCH_SIZE() - len(batch)
            start = self._batch_idx % max(len(has_orders), 1)
            for i in range(remaining):
                batch.append(has_orders[(start + i) % len(has_orders)])
            self._batch_idx = (start + remaining) % max(len(has_orders), 1)

        return batch

    def can_place(self, cid: str, side: str, est_cost: float) -> tuple[bool, str]:
        """All guards before placing an order. Returns (can_place, reason)."""
        ms = self.markets.get(cid)
        if not ms:
            return False, "no_market"
        if not ms.agent_approved:
            return False, "not_agent_approved"
        if self.capital_ceiling is not None and est_cost >= self.capital_ceiling:
            return False, "capital_exhausted"
        if ms.orders[side].order_id:
            return False, "already_has_order"
        if ms.dump_orders[side]:
            return False, "dump_pending"
        if self.positions.get_shares(cid, side) > 1:
            return False, "inventory"
        if not self.positions.can_quote(cid, side):
            return False, "halted"
        if ms.dump_failures >= cfg("RF_DUMP_MAX_FAILURES"):
            return False, "dump_failures"
        # Fill-rate breaker: block placement if fills are clustering.
        # Per-side check catches directional cascades (same side hit repeatedly).
        # Total check catches broad activity across both sides.
        now = time.time()
        fill_window = cfg("RF_FILL_BREAKER_WINDOW")
        for s in ("yes", "no"):
            ms.fill_times[s] = [t for t in ms.fill_times[s] if now - t < fill_window]
        side_threshold = cfg("RF_FILL_BREAKER_SIDE_THRESHOLD")
        for s in ("yes", "no"):
            if len(ms.fill_times[s]) >= side_threshold:
                return False, "fill_rate_breaker"
        recent_fills = len(ms.fill_times["yes"]) + len(ms.fill_times["no"])
        if recent_fills >= cfg("RF_FILL_BREAKER_THRESHOLD"):
            return False, "fill_rate_breaker"
        return True, ""

    def _check_buy_phantom_fill(
        self, ms: MarketState, side: str, matched: float,
        slot: OrderSlot | None = None,
    ) -> float:
        """FX-037: BUY-side phantom-fill defense.

        After the SDK reports a BUY fill, verify the on-chain CTF balance
        actually increased by the expected amount. The V2 SDK has been
        observed (2026-05-19, Iran NO) to over-report ``size_matched`` for
        orders that only partially delivered on-chain.

        FX-054: balance-lag tolerance. On Polygon, CTF transfers confirm
        ~2–5s after the SDK reports a match (sometimes longer when the bot's
        RPC endpoint serves stale state). The pre-FX-054 phantom check
        zeroed legitimate fills during this window — strongly suspected as
        one root cause of the 2026-05-25 8-of-9-lost-fills incident.
        When ``slot`` is provided and the order is younger than
        ``FILL_BALANCE_LAG_TOLERANCE_SEC`` from ``placed_at``, treat
        ``on_chain_delta=0`` as "balance hasn't confirmed yet" and fail
        OPEN (return SDK matched). Past the tolerance window, the FX-037
        defence resumes — a true phantom won't update the balance even
        after 60s.

        Returns the corrected fill quantity (≤ ``matched``). On API failure
        we fail OPEN with a warning — preserving the SDK value avoids
        losing legitimate fills during transient network issues.

        Symmetric with ``DumpManager.check_dump_fills`` lines 60-87
        (SELL-side phantom defense shipped in v5.1.9 / FX-007). The slot
        parameter is keyword-defaulted to ``None`` so pre-FX-054 callers
        (e.g., the stale-order path before refactor) keep working.
        """
        if matched <= 0:
            return matched
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            tid = ms.yes_tid if side == "yes" else ms.no_tid
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
            )
            on_chain = float(bal.get("balance", 0)) / 1e6
            pre_fill_tracked = self.positions.get_shares(ms.cid, side)
            actual_delta = max(0.0, on_chain - pre_fill_tracked)
            if actual_delta < matched - 0.5:
                # FX-054: balance-lag window check before declaring phantom.
                elapsed = (
                    time.time() - slot.placed_at
                    if slot is not None and slot.placed_at > 0 else 1e9
                )
                if actual_delta == 0 and elapsed < FILL_BALANCE_LAG_TOLERANCE_SEC:
                    log.warning(
                        f"[FILL_DETECT_TRACE] cid={ms.cid[:12]} side={side} "
                        f"step=phantom_lag_tolerated matched={matched:.0f} "
                        f"on_chain_delta=0 elapsed={elapsed:.0f}s<"
                        f"{FILL_BALANCE_LAG_TOLERANCE_SEC:.0f}s "
                        f"(CTF transfer likely still confirming, trusting SDK)"
                    )
                    return matched
                log.critical(
                    f"PHANTOM FILL: SDK size_matched={matched:.0f}sh but on-chain "
                    f"delta only {actual_delta:.0f}sh (pre_tracked={pre_fill_tracked:.0f}, "
                    f"post_on_chain={on_chain:.0f}) | {side.upper()} | {ms.question[:30]}"
                )
                return actual_delta
            return matched
        except Exception as e:
            log.warning(
                f"BUY phantom check failed (fail-open, using SDK matched={matched:.0f}sh): "
                f"{e} | {side.upper()} | {ms.question[:30]}"
            )
            return matched

    def _reconcile_after_unknown(self, ms: MarketState, side: str, slot: OrderSlot) -> bool:
        """Check exchange balance when clearing an UNKNOWN order.

        If the order silently filled, the exchange will have more shares than
        we're tracking. Detect this and record the fill so position tracking
        stays in sync.

        FX-054: returns True iff handle_fill was called (surplus >= 1.0 sh).
        Caller uses this to mark (cid, side) as primary-handled so the
        end-of-cycle drift sweep skips the redundant catch-up. False
        means the bot either found no surplus OR the RPC failed.
        """
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            tid = ms.yes_tid if side == "yes" else ms.no_tid
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
            )
            actual = float(bal.get("balance", 0)) / 1e6
            tracked = self.positions.get_shares(ms.cid, side)
            surplus = actual - tracked
            if surplus >= 1.0:
                log.warning(
                    f"UNKNOWN reconcile: exchange has {actual:.0f} but tracking {tracked:.0f} "
                    f"({surplus:.0f} surplus) — recording as fill | {ms.question[:30]}"
                )
                # FX-054: stamp a stable event_id keyed on
                # (cid, side, order_id) so retries of the same UNKNOWN
                # path collapse to one row via the partial unique index.
                # order_id may still be non-empty here even though the
                # SDK was unreachable — slot.order_id stays set until
                # the caller clears it after this method returns.
                ev = f"reconcile_unknown:{slot.order_id or ms.cid}:{side}"
                self.handle_fill(
                    ms, side, slot,
                    actual_shares=surplus, actual_price=slot.price,
                    order_id=slot.order_id, fill_event_id=ev,
                )
                return True
            else:
                log.info(f"UNKNOWN reconcile: no surplus (exchange={actual:.0f} tracked={tracked:.0f}) | {ms.question[:30]}")
        except Exception as e:
            log.warning(f"UNKNOWN reconcile balance check failed {side} {ms.question[:25]}: {e}")
        return False

    def _check_stale_order(self, ms: MarketState, side: str, slot: OrderSlot):
        """Force-check an order still in open_ids for partial fills.

        Called when the exchange reports an order as live, but it's been
        alive longer than RF_ORDER_STALE_CHECK_SECS. Catches partial fills
        that the exchange hasn't removed from open_ids.

        If partially filled: record fill, cancel remainder, clear slot.
        If clean: update last_stale_check so we don't re-check every cycle.
        """
        stale_secs = cfg("RF_ORDER_STALE_CHECK_SECS")
        check_ref = max(slot.placed_at, slot.last_stale_check)
        if time.time() - check_ref < stale_secs:
            return  # Not stale yet

        try:
            status = self.client.get_order(slot.order_id)
            matched = float(status.get("size_matched", 0))
            order_status = status.get("status", "")
        except Exception as e:
            log.debug(f"Stale order check failed {slot.order_id[:16]}: {e}")
            slot.last_stale_check = time.time()  # Backoff — retry after next interval
            return

        slot.last_stale_check = time.time()

        if matched > 0:
            # Partial fill on a "live" order — the exchange still has the
            # remainder open, but we have untracked shares. Cancel the
            # remainder, record the fill, clear the slot for fresh placement.
            fill_type = "PARTIAL" if matched < slot.shares - 0.5 else "FULL"
            raw_api_price = float(status.get("price", 0))
            if raw_api_price > 0:
                from price import to_yes_equiv
                actual_price = to_yes_equiv(raw_api_price, side)
            else:
                actual_price = slot.price

            age_min = (time.time() - slot.placed_at) / 60.0
            log.info(
                f"STALE CHECK: {fill_type} fill {side.upper()} "
                f"{matched:.0f}/{slot.shares:.0f}sh after {age_min:.0f}m | "
                f"{ms.question[:30]}"
            )

            # Cancel the remaining order first
            self.cancel_order(slot.order_id, reason="stale_partial_fill")
            self.db.delete_active_order(slot.order_id)

            # Record the fill
            # FX-054: stale-check path uses an event_id keyed on the order
            # and the matched quantity, identical shape to the SDK-detect
            # path. If a fill is observed by BOTH paths (rare; could happen
            # if a partial fill grows between cycles) only the first
            # write succeeds — the second collapses to `step=duplicate`.
            ev = f"stale:{slot.order_id}:{int(matched)}"
            self.handle_fill(
                ms, side, slot,
                actual_shares=matched, actual_price=actual_price,
                fill_type=fill_type,
                order_id=slot.order_id, fill_event_id=ev,
            )
            ms.unknown_count[side] = 0
            slot.order_id = None

    def total_exposure(self) -> float:
        """Sum of all open position USD values."""
        total = 0.0
        for cid in self.markets:
            for side in ["yes", "no"]:
                total += self.positions.get_position(cid, side)
        return total
