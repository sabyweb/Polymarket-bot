"""Dump manager: smart dump decay, merge, dump fill detection.

Extracted from reward_farmer.py. All dump/unwind logic in one module.
"""

import logging
import time

from config import cfg
from models import MarketState
from market_discovery import get_merged_book

log = logging.getLogger("reward_farmer")


class DumpManager:
    """Manages dump lifecycle: decay schedule, merge, fill detection."""

    def __init__(self, client, db, positions, cancel_fn, dry_run=False):
        """
        Args:
            client: RateLimitedClient (CLOB API)
            db: BotDatabase instance
            positions: PositionStore instance
            cancel_fn: callable(order_id, reason) -> bool
            dry_run: if True, no real orders placed
        """
        self.client = client
        self.db = db
        self.positions = positions
        self.cancel_order = cancel_fn
        self.dry_run = dry_run

    def check_dump_fills(self, markets: dict, open_ids: set):
        """Step 2: Check if dump SELL orders filled on exchange."""
        for cid, ms in list(markets.items()):
            for side in ["yes", "no"]:
                dump_oid = ms.dump_orders[side]
                if not dump_oid:
                    continue

                if self.dry_run:
                    ms.dump_orders[side] = None
                    ms.dump_state[side] = None
                    continue

                if dump_oid not in open_ids:
                    try:
                        status = self.client.get_order(dump_oid)
                        dump_status = status.get("status", "UNKNOWN")
                    except Exception as e:
                        log.debug(f"Dump order status check failed {dump_oid[:16]}: {e}")
                        dump_status = "UNKNOWN"

                    if dump_status == "MATCHED":
                        actual_price = float(status.get("price", 0))
                        actual_matched = float(status.get("size_matched", 0))

                        if actual_matched < 1.0:
                            log.warning(
                                f"DUMP MATCHED with zero size_matched — clearing without "
                                f"recording | {ms.question[:30]}"
                            )
                            if ms.dump_orders[side]:
                                self.db.delete_active_order(ms.dump_orders[side])
                            ms.dump_orders[side] = None
                            ms.dump_state[side] = None
                            ms.unverified_count[side] = 0
                            self.db.delete_dump_state(ms.cid, side)
                            continue

                        # Verify exchange balance actually decreased before recording unwind
                        phantom = False
                        unverified = False
                        if actual_matched >= 1.0:
                            try:
                                from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
                                tid = ms.yes_tid if side == "yes" else ms.no_tid
                                bal = self.client.get_balance_allowance(
                                    BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
                                )
                                on_exchange = float(bal.get("balance", 0)) / 1e6
                                tracked = self.positions.get_shares(ms.cid, side)
                                if on_exchange >= tracked - 0.5:
                                    log.critical(
                                        f"PHANTOM FILL: status=MATCHED size_matched={actual_matched:.0f}sh "
                                        f"but exchange still holds {on_exchange:.0f} (tracked={tracked:.0f}) | "
                                        f"{ms.question[:30]}"
                                    )
                                    phantom = True
                            except Exception as e:
                                if cfg("RF_DUMP_VERIFY_FAILSAFE_ENABLED"):
                                    log.warning(
                                        f"[DUMP_VERIFY_UNVERIFIED] cid={ms.cid[:12]} side={side} "
                                        f"order={dump_oid[:16]}: verification RPC failed ({e}) — "
                                        f"NOT recording unwind; will retry next cycle"
                                    )
                                    unverified = True
                                else:
                                    log.warning(f"Dump fill verification failed: {e} — proceeding with record_unwind")

                        if unverified:
                            # Leave state intact for retry; count consecutive failures.
                            ms.unverified_count[side] = ms.unverified_count.get(side, 0) + 1
                            max_unv = cfg("RF_DUMP_VERIFY_MAX_UNVERIFIED_CYCLES")
                            if ms.unverified_count[side] >= max_unv:
                                from alerts import alert_dump_verify_stuck
                                alert_dump_verify_stuck(
                                    cid=ms.cid,
                                    question=ms.question,
                                    side=side,
                                    order_id=dump_oid,
                                    cycles=ms.unverified_count[side],
                                )
                                # Cap the counter so we don't page every cycle.
                                ms.unverified_count[side] = max_unv
                            continue
                        else:
                            ms.unverified_count[side] = 0

                        if phantom:
                            # Don't record unwind — clear state for fresh retry
                            if ms.dump_orders[side]:
                                self.db.delete_active_order(ms.dump_orders[side])
                            ms.dump_orders[side] = None
                            ms.dump_state[side] = None
                            self.db.delete_dump_state(ms.cid, side)
                            continue

                        # FX-050: Polymarket charges a taker fee (~0.88-0.9%)
                        # on orders that cross the spread. DumpManager's passive
                        # mode (dump_manager.py:308-327) sets the dump SELL
                        # price to the best opposite-side bid, crossing the
                        # spread → we are the taker → fee applies. The SDK's
                        # `price` field reports the book match price, not the
                        # cash actually settled to the wallet. Without this
                        # correction, recorded usd_value is the gross revenue
                        # and pnl is under-magnitude vs reality. Empirical
                        # calibration: 2026-05-22 dump on 0x0ed3f07970 →
                        # bot recorded pnl=−$1.00, wallet actual −$1.34
                        # (gap = 0.88% taker fee on $39 gross).
                        # FX-089: status["price"] is the order's LIMIT, not the
                        # execution price. A marketable dump SELL (the aggressive/
                        # passive dump deliberately sets a low limit to force a fill)
                        # executes at the BID via price improvement, NOT its limit, so
                        # deriving proceeds from the limit massively over-states the
                        # loss. Verified on-chain: a dump booked at limit $0.01
                        # actually executed at ~$0.24 → recorded −$54 vs real −$8 (the
                        # source of the WALLET_DESYNC alarms + inflated realized-loss).
                        # Re-price to the marketable execution (best bid for the sold
                        # side) when it beats the limit — a sell never realizes LESS
                        # than its own limit, and a marketable sell realizes ~the bid.
                        # Fail-open to the limit (the prior loss-over-stating behavior)
                        # if the book is unavailable; the FX-049/055 wallet reconciler
                        # backstops any residual drift.
                        try:
                            _mb = get_merged_book(self.client, ms.yes_tid, ms.no_tid)
                            if _mb and _mb.get("bids") and _mb.get("asks"):
                                if side == "yes":
                                    _bid = float(_mb["bids"][0]["price"])
                                else:
                                    _bid = round(1.0 - float(_mb["asks"][0]["price"]), 4)
                                if _bid > actual_price:
                                    log.info(
                                        f"[FX089] dump exec re-priced {side.upper()} "
                                        f"limit={actual_price:.4f} -> bid={_bid:.4f} "
                                        f"(marketable sell fills at the bid) | {ms.question[:30]}"
                                    )
                                    actual_price = _bid
                        except Exception as e:
                            log.debug(f"[FX089] exec-price book fetch failed, using limit: {e}")

                        gross_revenue = actual_matched * actual_price if actual_price > 0 else 0
                        _taker_fee = cfg("RF_POLYMARKET_TAKER_FEE")
                        sell_revenue = gross_revenue * (1.0 - _taker_fee)

                        from price import to_clob
                        avg_p = self.positions.get_avg_price(ms.cid, side)
                        if avg_p > 0:
                            vwap_cost = actual_matched * to_clob(avg_p, side)
                        else:
                            # FX-066 Tier 1 (safety floor): cost basis unknown —
                            # the position was registered from on-chain balance via
                            # set_shares (orphan / startup recovery) with NO price, so
                            # get_avg_price returns 0. Pre-fix vwap_cost=0 → pnl =
                            # usd_value − 0 = +sell_revenue, i.e. a real loss recorded
                            # as PROFIT (pnl>0) → excluded from the kill switch's
                            # SUM(pnl WHERE pnl<0) → the loss is invisible to the kill.
                            # We cannot know the true buy price here (that is FX-066
                            # Tier 2 — reconstruct avg_price at orphan registration —
                            # and FX-074 — wallet reconciler), but we MUST never record
                            # an unknown-cost dump as a profit. Floor vwap_cost to the
                            # gross (pre-fee) proceeds so pnl = sell_revenue − gross =
                            # −fee ≤ 0: visible to the kill as a (small) loss, never a
                            # phantom profit that could mask real losses in aggregate.
                            vwap_cost = gross_revenue
                            log.warning(
                                f"[UNWIND_COST] cid={ms.cid[:12]} side={side} "
                                f"cost_basis_unknown avg_price=0 — flooring vwap_cost to "
                                f"gross ${gross_revenue:.2f} so pnl<=0 (FX-066 Tier 1; "
                                f"true magnitude needs Tier 2 / FX-074)"
                            )

                        log.info(
                            f"DUMP CONFIRMED {side.upper()} {actual_matched:.0f}sh @ {actual_price:.4f} | "
                            f"gross=${gross_revenue:.2f} fee={_taker_fee*100:.2f}% net=${sell_revenue:.2f} "
                            f"cost=${vwap_cost:.2f} pnl=${sell_revenue - vwap_cost:+.2f} | "
                            f"{ms.question[:30]}"
                        )

                        self.positions.record_unwind(ms.cid, side, actual_matched)
                        # FX-072 gate 3: mark that THIS cycle recorded an
                        # unwind for (cid, side) so the end-of-cycle drift
                        # sweep does NOT also add the drained dump shares back
                        # (tracked was correctly reduced here — adding back
                        # would fabricate a phantom catch-up). hasattr guard
                        # keeps old-namespace test stubs / MarketState mocks
                        # without the field working.
                        if hasattr(ms, "fx072_unwound_this_cycle"):
                            ms.fx072_unwound_this_cycle[side] = True
                        # FX-067: key the unwind by the dump order id so a
                        # restart between this write and the dump-state clear
                        # below can't double-log the loss; check the truthful
                        # return so a silently-dropped loss row is visible (it
                        # is the sole input to the 24h-loss kill).
                        _uw_ok = self.db.log_unwind(
                            condition_id=ms.cid, question=ms.question,
                            side=side, shares=actual_matched,
                            sell_price=actual_price, usd_value=sell_revenue,
                            vwap_cost=vwap_cost,
                            unwind_event_id=f"unwind:{ms.cid}:{side}:{dump_oid}",
                        )
                        if not _uw_ok:
                            log.warning(
                                f"[UNWIND_WRITE] cid={ms.cid[:12]} side={side} "
                                f"pnl=${sell_revenue - vwap_cost:+.2f} step=not_inserted "
                                f"(duplicate or DB error — loss may be missing from kill math)"
                            )
                        from alerts import alert_unwind
                        alert_unwind(
                            side=side.upper(), price=actual_price,
                            size=actual_matched, usd_value=sell_revenue,
                            market_question=ms.question,
                        )

                        if ms.dump_orders[side]:
                            self.db.delete_active_order(ms.dump_orders[side])
                        ms.dump_orders[side] = None
                        ms.dump_state[side] = None
                        ms.dump_failures = 0
                        self.db.delete_dump_state(ms.cid, side)
                    elif dump_status == "UNKNOWN":
                        ms.unknown_count[side] = ms.unknown_count.get(side, 0) + 1
                        if ms.unknown_count[side] >= cfg("RF_UNKNOWN_RETRY_THRESHOLD"):
                            log.warning(f"Dump order stuck UNKNOWN {cfg('RF_UNKNOWN_RETRY_THRESHOLD')}x — clearing order, will retry | {ms.question[:30]}")
                            if ms.dump_orders[side]:
                                self.db.delete_active_order(ms.dump_orders[side])
                            ms.dump_orders[side] = None
                            ms.unknown_count[side] = 0
                            # Preserve dump_state so reprice_active_dumps
                            # detects (dump_state exists, dump_orders=None)
                            # and re-initiates the dump via dump_position()
                    else:
                        log.warning(f"Dump order {dump_status} — will retry | {ms.question[:30]}")
                        if ms.dump_orders[side]:
                            self.db.delete_active_order(ms.dump_orders[side])
                        ms.dump_orders[side] = None

    def reprice_active_dumps(self, markets: dict, open_ids: set):
        """Step 2.5: Reprice active dumps on their decay schedule."""
        for cid, ms in list(markets.items()):
            for side in ["yes", "no"]:
                if not ms.dump_state[side]:
                    continue
                dump_oid = ms.dump_orders[side]
                if dump_oid and dump_oid in open_ids:
                    elapsed_min = (time.time() - ms.dump_state[side]["started_at"]) / 60.0
                    last_reprice_min = ms.dump_state[side].get("last_reprice_min", 0)
                    if int(elapsed_min) > int(last_reprice_min):
                        ms.dump_state[side]["last_reprice_min"] = elapsed_min
                        shares = ms.dump_state[side]["shares"]
                        self.dump_position(ms, side, shares)
                elif not dump_oid and ms.dump_state[side]:
                    shares = ms.dump_state[side]["shares"]
                    self.dump_position(ms, side, shares)

        # Safety sweep: catch positions with shares but no dump/buy state
        for cid, ms in list(markets.items()):
            for side in ["yes", "no"]:
                if ms.dump_state[side] or ms.dump_orders[side]:
                    continue
                if ms.orders[side].order_id:
                    continue
                shares = self.positions.get_shares(cid, side)
                if shares >= 1.0:
                    log.warning(
                        f"LOST POSITION detected: {side.upper()} {shares:.0f}sh "
                        f"with no dump or buy order — re-initiating dump | {ms.question[:30]}"
                    )
                    self.dump_position(ms, side, shares)

    def try_merge(self, ms: MarketState, amount: float):
        """Merge YES + NO positions for ~$1/pair via CTF relayer (FX-094).

        On failure: alert + hold hedged pair — do NOT auto dual-dump (lossy).
        """
        if self.dry_run:
            log.info(f"[DRY] MERGE {amount:.0f} pairs | {ms.question[:30]}")
            self.positions.record_unwind(ms.cid, "yes", amount)
            self.positions.record_unwind(ms.cid, "no", amount)
            return

        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        from ctf_merge import try_merge_positions
        from alerts import alert_merge_needed

        raw_client = getattr(self.client, "_client", self.client)

        def _yes_balance() -> float:
            for tid in [ms.yes_tid, ms.no_tid]:
                self.client.update_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
                )
            bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL, token_id=ms.yes_tid
                )
            )
            return float(bal.get("balance", 0)) / 1e6

        ok, reason = try_merge_positions(
            raw_client,
            condition_id=ms.cid,
            amount=amount,
            yes_tid=ms.yes_tid,
            verify_balance_fn=_yes_balance,
        )
        if not ok:
            yes_sh = self.positions.get_shares(ms.cid, "yes")
            no_sh = self.positions.get_shares(ms.cid, "no")
            mergeable = min(yes_sh, no_sh, amount)
            log.warning(
                f"Merge failed ({reason}) — holding hedged pair, NOT dual-dumping | "
                f"{ms.question[:30]}"
            )
            try:
                alert_merge_needed(
                    ms.question, yes_sh, no_sh, mergeable, mergeable,
                )
            except Exception:
                pass
            return

        log.info(f"MERGE {amount:.0f} pairs | {ms.question[:30]}")
        # Realized-loss-kill accounting (RF_KILL_ACCT_MERGE_COST_ENABLED): a complete
        # set (1 YES + 1 NO) redeems to $1 with no taker fee, so usd_value=amount; the
        # cost is amount*(yes_clob + no_clob). pnl<0 iff the pair cost >$1 (an adverse
        # merge) — exactly the loss the kill must see. Capture the per-leg cost basis
        # BEFORE record_unwind, which zeroes avg_price at 0 shares (state.py). OFF =>
        # vwap_cost=0 => pnl=+amount, byte-identical to the pre-fix behaviour.
        _merge_acct = bool(cfg("RF_KILL_ACCT_MERGE_COST_ENABLED"))
        if _merge_acct:
            _yes_avg = self.positions.get_avg_price(ms.cid, "yes")
            _no_avg = self.positions.get_avg_price(ms.cid, "no")
        self.positions.record_unwind(ms.cid, "yes", amount)
        self.positions.record_unwind(ms.cid, "no", amount)
        if _merge_acct:
            if 0 < _yes_avg <= 1 and 0 < _no_avg <= 1:
                from price import to_clob
                _merge_cost = amount * (to_clob(_yes_avg, "yes") + to_clob(_no_avg, "no"))
            else:
                # Unknown/corrupt basis (e.g. an orphan/startup hedge registered via
                # set_shares with avg_price=0): never book a merge as a profit. Floor
                # vwap_cost to usd_value so pnl=0 (no phantom profit). True magnitude
                # for unknown-basis legs is FX-066 Tier-2 reconstruction (separate axis).
                _merge_cost = float(amount)
                log.warning(
                    f"[UNWIND_COST] cid={ms.cid[:12]} side=merge cost_basis_unknown "
                    f"yes_avg={_yes_avg} no_avg={_no_avg} — floored vwap_cost to "
                    f"usd ${amount:.2f} so pnl<=0 (FX-066 Tier-2 territory)"
                )
        else:
            _merge_cost = 0.0  # legacy: pnl = usd_value - 0 = +amount (byte-identical)
        _mg_ok = self.db.log_unwind(
            condition_id=ms.cid, question=ms.question,
            side="merge", shares=amount,
            sell_price=1.0, usd_value=amount, vwap_cost=_merge_cost,
        )
        if not _mg_ok:
            log.warning(
                f"[UNWIND_WRITE] cid={ms.cid[:12]} side=merge amount={amount:.0f} "
                f"step=not_inserted (DB error — merge unwind row missing)"
            )

    def dump_position(self, ms: MarketState, side: str, shares: float):
        """Smart dump: SELL near fill price, decay over time.

        T+0 to T+5m: aggressive decay (fill_price - N ticks per minute)
        T+5m to T+30m: passive mode (reprice to merged book every 5m)
        T+30m: abandon

        FX-007: skips silently when the cid is in the unliquidatable_markets
        table. The bot has already confirmed the orderbook is gone; retrying
        produces only 400 spam.
        """
        from py_clob_client_v2.clob_types import OrderArgs
        from py_clob_client_v2.order_builder.constants import SELL

        # FX-007 gate: if this cid has been confirmed dead at the orderbook
        # level, abandon any in-flight dump_state and return without an API
        # call. The periodic re-probe (FX-028) is the only path that
        # un-marks; un-marking re-enables this method on subsequent calls.
        if self.db.is_unliquidatable(ms.cid):
            if ms.dump_state[side]:
                ms.dump_state[side] = None
                self.db.delete_dump_state(ms.cid, side)
            return

        tid = ms.yes_tid if side == "yes" else ms.no_tid
        tick = ms.tick_size

        if self.dry_run:
            log.info(f"[DRY] DUMP {side.upper()} {shares:.0f}sh | {ms.question[:30]}")
            self.positions.record_unwind(ms.cid, side, shares)
            return

        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            try:
                bal = self.client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
                )
                actual_balance = float(bal.get("balance", 0)) / 1e6
                dump_shares = min(shares, actual_balance)
                if dump_shares < 1.0:
                    log.warning(f"Skip dump {side}: want {shares:.0f} but only {actual_balance:.0f} on exchange | {ms.question[:30]}")
                    ms.dump_failures += 1
                    return
            except Exception as e:
                log.debug(f"Balance check failed for dump {side}: {e}")
                dump_shares = shares

            # Initialize dump state if first attempt
            if ms.dump_state[side] is None:
                from price import to_clob
                fill_price_yes_equiv = 0
                if ms.last_fill_price.get(side, 0) > 0:
                    fill_price_yes_equiv = ms.last_fill_price[side]
                elif ms.orders[side].price > 0:
                    fill_price_yes_equiv = ms.orders[side].price

                fill_price_clob = to_clob(fill_price_yes_equiv, side) if fill_price_yes_equiv > 0 else 0
                ms.dump_state[side] = {
                    "fill_price": fill_price_clob,
                    "started_at": time.time(),
                    "shares": dump_shares,
                    "tid": tid,
                }
                self.db.save_dump_state(ms.cid, side, ms.dump_state[side])

            state = ms.dump_state[side]
            elapsed_min = (time.time() - state["started_at"]) / 60.0

            # Compute decay price
            if elapsed_min >= cfg("RF_DUMP_ABANDON_MINS"):
                log.warning(f"DUMP ABANDONED {side.upper()} after {elapsed_min:.0f}m | {ms.question[:30]}")
                ms.dump_state[side] = None
                self.db.delete_dump_state(ms.cid, side)
                if ms.dump_orders[side]:
                    oid = ms.dump_orders[side]
                    if self.cancel_order(oid, reason="dump_30m_timeout"):
                        self.db.delete_active_order(oid)
                        ms.dump_orders[side] = None
                    else:
                        log.warning(f"Orphaned dump order {oid[:16]} — cancel failed on abandon, force-cleaning DB")
                        self.db.delete_active_order(oid)
                        ms.dump_orders[side] = None
                return

            elif elapsed_min >= cfg("RF_DUMP_AGGRESSIVE_MINS"):
                passive_interval = cfg("RF_DUMP_PASSIVE_REPRICE_MINS")
                last_passive = state.get("last_passive_reprice", cfg("RF_DUMP_AGGRESSIVE_MINS"))
                if elapsed_min - last_passive < passive_interval:
                    return
                state["last_passive_reprice"] = elapsed_min

                merged = get_merged_book(self.client, ms.yes_tid, ms.no_tid)
                if not merged or not merged["bids"] or not merged["asks"]:
                    ms.dump_failures += 1
                    return

                if side == "yes":
                    sell_price = float(merged["bids"][0]["price"])
                else:
                    best_yes_ask = float(merged["asks"][0]["price"])
                    sell_price = round(1.0 - best_yes_ask, 4)

                sell_price = max(0.01, sell_price)
                log.info(f"DUMP PASSIVE {side.upper()} @ {sell_price:.4f} ({elapsed_min:.0f}m) | {ms.question[:30]}")
            else:
                decay_ticks = 1 + int(elapsed_min)
                sell_price = round(state["fill_price"] - decay_ticks * tick, 4)
                sell_price = max(0.01, sell_price)

            # FX-071: dump-time slippage floor. Both pricing branches above
            # converge here. The aggressive-decay branch walks sell_price below
            # the fill price with no floor, so an extreme/illiquid market gets an
            # unbounded-loss forced SELL (the 2026-05-25 13.3% class). Floor the
            # SELL so a single dump never crystallizes more than
            # RF_DUMP_MAX_SLIPPAGE_FRAC below the cost basis (state["fill_price"],
            # CLOB terms). The order then rests at the bounded-loss price; if the
            # book never reaches it, RF_DUMP_ABANDON_MINS holds the position
            # rather than dumping into a loss bigger than the reward (Rule 3).
            # Only applies when the cost basis is known (fill_price>0; orphan /
            # startup positions with avg_price=0 are handled by FX-066 Tier 1 +
            # FX-074 paging). Disabled when frac<=0 or >=1.
            _dump_max_slip = cfg("RF_DUMP_MAX_SLIPPAGE_FRAC")
            _cost_basis = state.get("fill_price", 0) or 0
            if 0 < _dump_max_slip < 1.0 and _cost_basis > 0:
                _slip_floor = round(_cost_basis * (1.0 - _dump_max_slip), 4)
                if sell_price < _slip_floor:
                    log.info(
                        f"DUMP SLIPPAGE FLOOR {side.upper()}: {sell_price:.4f} -> "
                        f"{_slip_floor:.4f} (cost {_cost_basis:.4f}, cap "
                        f"{_dump_max_slip*100:.0f}%) — bounded-loss rest, "
                        f"abandon-timer holds if unfilled | {ms.question[:30]}"
                    )
                    sell_price = _slip_floor

            # Cancel existing dump order if any (repricing)
            if ms.dump_orders[side]:
                old_oid = ms.dump_orders[side]
                if not self.cancel_order(old_oid, reason="dump_reprice"):
                    return  # Don't post new order if old one is still live
                self.db.delete_active_order(old_oid)
                ms.dump_orders[side] = None

            args = OrderArgs(token_id=tid, price=sell_price, size=float(dump_shares), side=SELL)
            resp = self.client.create_and_post_order(args)
            oid = resp.get("orderID") if isinstance(resp, dict) else None

            if oid:
                ms.dump_orders[side] = oid
                ms.dump_failures = 0
                state["dump_order_id"] = oid
                self.db.save_dump_state(ms.cid, side, state)
                self.db.save_active_order(oid, ms.cid, side, "dump_sell", sell_price, dump_shares)
                if elapsed_min < 0.1:
                    log.info(
                        f"DUMP POSTED {side.upper()} {dump_shares:.0f}sh @ {sell_price:.4f} "
                        f"(fill was {state['fill_price']:.4f}) | {ms.question[:30]}"
                    )
            else:
                log.warning(f"Dump {side} no order ID | {ms.question[:30]}")
                ms.dump_failures += 1

        except Exception as e:
            # FX-007 + FX-009: definitive "orderbook does not exist" → mark
            # unliquidatable + clean the dump_state we just saved (the row
            # was saved on first-attempt init before the post; on a dead
            # orderbook there's nothing to retry on later cycles). Other
            # exceptions (transient API, balance, network) leave the state
            # alone for the next cycle to retry.
            err_str = str(e).lower()
            # Canonical V2 SDK 400 body: "the orderbook {cid} does not exist".
            # The cid sits between "orderbook" and "does not exist", so we
            # require BOTH substrings — strict enough that "insufficient
            # balance" / "rate limit" / "market does not exist" don't
            # match, loose enough to catch the canonical form regardless
            # of the cid in the middle.
            orderbook_dead = (
                "orderbook" in err_str and "does not exist" in err_str
            )
            if orderbook_dead:
                log.warning(
                    f"Marking {ms.cid[:16]} unliquidatable: orderbook gone "
                    f"({side.upper()} dump) | {ms.question[:30]}"
                )
                self.db.mark_unliquidatable(ms.cid, reason=f"dump_{side}_orderbook_gone")
                if ms.dump_state[side]:
                    ms.dump_state[side] = None
                self.db.delete_dump_state(ms.cid, side)
                ms.dump_failures += 1
            else:
                log.error(f"Dump {side} FAILED: {e} | {ms.question[:30]}")
                ms.dump_failures += 1
