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

                        # Verify exchange balance actually decreased before recording unwind
                        phantom = False
                        if actual_matched > 0:
                            try:
                                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
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
                                log.warning(f"Dump fill verification failed: {e} — proceeding with record_unwind")

                        if phantom:
                            # Don't record unwind — clear state for fresh retry
                            if ms.dump_orders[side]:
                                self.db.delete_active_order(ms.dump_orders[side])
                            ms.dump_orders[side] = None
                            ms.dump_state[side] = None
                            self.db.delete_dump_state(ms.cid, side)
                            continue

                        sell_revenue = actual_matched * actual_price if actual_price > 0 else 0

                        from price import to_clob
                        avg_p = self.positions.get_avg_price(ms.cid, side)
                        vwap_cost = actual_matched * to_clob(avg_p, side) if avg_p > 0 else 0

                        log.info(
                            f"DUMP CONFIRMED {side.upper()} {actual_matched:.0f}sh @ {actual_price:.4f} | "
                            f"rev=${sell_revenue:.2f} cost=${vwap_cost:.2f} pnl=${sell_revenue - vwap_cost:+.2f} | "
                            f"{ms.question[:30]}"
                        )

                        self.positions.record_unwind(ms.cid, side, actual_matched)
                        self.db.log_unwind(
                            condition_id=ms.cid, question=ms.question,
                            side=side, shares=actual_matched,
                            sell_price=actual_price, usd_value=sell_revenue,
                            vwap_cost=vwap_cost,
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
        """Merge YES + NO positions for $1 each. Falls back to dual dump."""
        if self.dry_run:
            log.info(f"[DRY] MERGE {amount:.0f} pairs | {ms.question[:30]}")
            self.positions.record_unwind(ms.cid, "yes", amount)
            self.positions.record_unwind(ms.cid, "no", amount)
            return

        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            for tid in [ms.yes_tid, ms.no_tid]:
                self.client.update_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
                )

            # Snapshot YES balance before merge so we can verify it actually happened
            pre_bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=ms.yes_tid)
            )
            pre_yes = float(pre_bal.get("balance", 0)) / 1e6

            result = self.client.merge_positions(ms.cid, amount)

            # Verify merge actually reduced the exchange balance
            post_bal = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=ms.yes_tid)
            )
            post_yes = float(post_bal.get("balance", 0)) / 1e6

            if post_yes >= pre_yes - 0.5:
                log.critical(
                    f"PHANTOM MERGE: API returned but exchange YES balance unchanged "
                    f"(pre={pre_yes:.0f} post={post_yes:.0f}, expected -{amount:.0f}) | "
                    f"{ms.question[:30]}"
                )
                raise RuntimeError("Merge balance verification failed")

            log.info(f"MERGE {amount:.0f} pairs | {ms.question[:30]}")
            self.positions.record_unwind(ms.cid, "yes", amount)
            self.positions.record_unwind(ms.cid, "no", amount)
            self.db.log_unwind(
                condition_id=ms.cid, question=ms.question,
                side="merge", shares=amount,
                sell_price=1.0, usd_value=amount,
            )
        except Exception as e:
            log.warning(f"Merge failed ({e}) — falling back to dual dump | {ms.question[:30]}")
            for side in ["yes", "no"]:
                shares = self.positions.get_shares(ms.cid, side)
                if shares >= 1:
                    self.dump_position(ms, side, shares)

    def dump_position(self, ms: MarketState, side: str, shares: float):
        """Smart dump: SELL near fill price, decay over time.

        T+0 to T+5m: aggressive decay (fill_price - N ticks per minute)
        T+5m to T+30m: passive mode (reprice to merged book every 5m)
        T+30m: abandon
        """
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import SELL

        tid = ms.yes_tid if side == "yes" else ms.no_tid
        tick = ms.tick_size

        if self.dry_run:
            log.info(f"[DRY] DUMP {side.upper()} {shares:.0f}sh | {ms.question[:30]}")
            self.positions.record_unwind(ms.cid, side, shares)
            return

        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
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
            log.error(f"Dump {side} FAILED: {e} | {ms.question[:30]}")
            ms.dump_failures += 1
