"""
Core bot orchestration for the Polymarket market-making bot.

Connects to the CLOB API, selects markets, delegates order management
to OrderManager instances, and handles the main trading loop.
"""

import signal
import time
import logging
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

from config import (
    HOST, CHAIN_ID, PRIVATE_KEY,
    CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE,
    MAX_MARKETS, MIN_SCORE_THRESHOLD,
    MARKET_REFRESH_SECS, ORDER_REFRESH_SECS,
    ORDER_SIZE, FUNDER, SIGNATURE_TYPE,
    HYSTERESIS_SCORE_MARGIN, HEARTBEAT_TIMEOUT_SECS,
    REWARD_LOG_INTERVAL_SECS,
)
from market import get_rewards_markets
from state import PositionStore as PositionTracker
from orders import OrderManager, BalanceGate
from rate_limiter import RateLimitedClient
from arbitrage import ArbitrageScanner
from alerts import (
    setup_logger, alert_bot_restart, alert_no_markets,
    alert_api_failure, alert_positions, alert_heartbeat_failure,
    log_cycle_start, log_market_refresh,
)

log = logging.getLogger(__name__)


class MarketMakerBot:
    """Main market-making bot.

    Orchestrates market selection, order management, position tracking,
    and alerting.
    """

    def __init__(self) -> None:
        self.client: ClobClient | None = None
        self.position_tracker: PositionTracker = PositionTracker()
        self.balance_gate: BalanceGate | None = None
        self.order_managers: dict[str, OrderManager] = {}
        self.active_markets: list[dict] = []
        self.cycle_count: int = 0
        self.last_market_refresh: float = 0
        self._shutdown_requested: bool = False
        self._last_successful_cycle: float = time.time()
        self._heartbeat_alerted: bool = False
        self._last_reconcile: float = 0  # Periodic exchange reconciliation
        self._arb_scanner: ArbitrageScanner | None = None
        self._last_arb_scan: float = 0
        self._last_reward_log: float = 0

    # ── Client Setup ─────────────────────────────────────────────────────────
    def connect(self) -> bool:
        """Establish connection to Polymarket CLOB API.

        Also performs a balance/allowance pre-flight check so that
        misconfigured wallets fail fast instead of during live trading.

        Returns:
            True if connection succeeded, False otherwise.
        """
        try:
            creds = ApiCreds(
                api_key=CLOB_API_KEY,
                api_secret=CLOB_SECRET,
                api_passphrase=CLOB_PASS_PHRASE,
            )
            raw_client = ClobClient(
                HOST,
                key=PRIVATE_KEY,
                chain_id=CHAIN_ID,
                creds=creds,
                signature_type=SIGNATURE_TYPE,
                funder=FUNDER,
            )
            self.client = RateLimitedClient(raw_client)
            self.balance_gate = BalanceGate(self.client)
            self._arb_scanner = ArbitrageScanner(self.client)
            log.info("Connected to Polymarket CLOB API (rate-limited)")

            # Set COLLATERAL (USDC) allowance at startup.
            # CONDITIONAL (ERC1155) allowances require a token_id and are
            # set per-token in OrderManager.ensure_sell_allowance().
            try:
                self.client.update_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                )
                bal = self.client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                )
                log.info(f"Allowance set — COLLATERAL (USDC): {bal}")
            except Exception as e:
                log.warning(f"Could not set COLLATERAL allowance: {e}")

            return True
        except Exception as e:
            alert_api_failure(str(e))
            return False

    # ── Market Management ────────────────────────────────────────────────────
    def refresh_markets(self) -> None:
        """Fetch, score, and select markets to trade.

        Called every MARKET_REFRESH_SECS and on bot startup.  Uses
        hysteresis to avoid thrashing: a current market is only replaced
        if a new candidate outscores it by HYSTERESIS_SCORE_MARGIN points.
        """
        log.info("Refreshing market list...")

        # Fetch more candidates than needed so we can compare
        new_markets = get_rewards_markets(limit=MAX_MARKETS * 3)
        candidates = [m for m in new_markets if m["score"] >= MIN_SCORE_THRESHOLD]

        if not candidates:
            alert_no_markets()
            if self.active_markets:
                log.info("Keeping existing markets until eligible ones are found.")
            return

        current_ids = {m["condition_id"] for m in self.active_markets}
        current_scores = {
            m["condition_id"]: m["score"] for m in self.active_markets
        }

        # Build the new active set with hysteresis:
        # 1. Keep current markets that still pass threshold
        # 2. Only swap in a new market if it beats a current one by margin
        kept = [m for m in candidates if m["condition_id"] in current_ids]
        new_only = [m for m in candidates if m["condition_id"] not in current_ids]

        # Start with kept markets, then fill remaining slots from new candidates
        result = list(kept)
        remaining_slots = MAX_MARKETS - len(result)

        if remaining_slots > 0 and new_only:
            # If we have fewer markets than MAX, just add the best new ones
            if len(kept) < len(current_ids):
                # Some current markets dropped below threshold — fill freely
                result.extend(new_only[:remaining_slots])
            else:
                # All current markets still valid — only swap if new one
                # significantly outscores the weakest current market
                if result:
                    weakest_score = min(m["score"] for m in result)
                else:
                    weakest_score = 0

                for candidate in new_only[:remaining_slots]:
                    if candidate["score"] > weakest_score + HYSTERESIS_SCORE_MARGIN:
                        result.append(candidate)
                    else:
                        log.debug(
                            f"Skipping {candidate['question'][:30]} "
                            f"(score={candidate['score']:.1f}, needs "
                            f">{weakest_score + HYSTERESIS_SCORE_MARGIN:.1f})"
                        )

        # Cap at MAX_MARKETS, keeping highest scores
        result.sort(key=lambda x: x["score"], reverse=True)
        result = result[:MAX_MARKETS]

        new_result_ids = {m["condition_id"] for m in result}
        to_remove = current_ids - new_result_ids
        to_add = new_result_ids - current_ids

        for condition_id in to_remove:
            self._remove_market(condition_id)

        for market in result:
            if market["condition_id"] in to_add:
                self._add_market(market)

        # Update score for existing markets (rankings may have shifted)
        for market in result:
            cid = market["condition_id"]
            if cid in current_ids and cid in current_scores:
                old_score = current_scores[cid]
                new_score = market["score"]
                if abs(old_score - new_score) > 1.0:
                    log.info(
                        f"Score changed: {market['question'][:40]} | "
                        f"{old_score:.1f} → {new_score:.1f}"
                    )

        self.active_markets = result
        self.last_market_refresh = time.time()

        log_market_refresh(self.active_markets)

        # Create unwind managers for orphaned positions using token data
        # from the candidates we just fetched (avoids Gamma API lookup).
        self._ensure_unwind_managers(new_markets)

        # Send Discord notification when markets change
        if to_add or to_remove:
            from alerts import _send_discord
            changes = []
            for cid in to_remove:
                q = "Unknown"
                for m in self.active_markets:
                    if m["condition_id"] == cid:
                        q = m["question"]
                        break
                changes.append(f"➖ {q[:50]}")
            for m in result:
                if m["condition_id"] in to_add:
                    changes.append(
                        f"➕ {m['question'][:50]} (score={m['score']:.1f})"
                    )
            _send_discord(
                "**Market Rotation**",
                {
                    "title": "Active Markets Changed",
                    "description": "\n".join(changes),
                    "color": 0x9B59B6,
                    "fields": [
                        {
                            "name": f"#{i+1} {m['question'][:45]}",
                            "value": f"Score: {m['score']:.1f} | ${m['daily_rate']:.0f}/day",
                            "inline": False,
                        }
                        for i, m in enumerate(result)
                    ],
                },
            )

    def _add_market(self, market: dict) -> None:
        """Add a market to the active trading set.

        If an unwind-only manager already exists (e.g. from startup
        orphaned-position recovery), preserve its tracked unwind orders
        by transferring them to the new full manager.

        Args:
            market: Parsed market dict from get_rewards_markets().
        """
        condition_id = market["condition_id"]
        question = market["question"]

        # Preserve unwind orders from any existing manager
        existing_unwinds: dict = {}
        if condition_id in self.order_managers:
            existing_unwinds = self.order_managers[condition_id].unwind_orders.copy()

        self.position_tracker.register_market(condition_id, question)
        self.order_managers[condition_id] = OrderManager(
            self.client, market, self.position_tracker,
            balance_gate=self.balance_gate,
        )

        if existing_unwinds:
            self.order_managers[condition_id].unwind_orders = existing_unwinds
            log.info(
                f"Transferred {len(existing_unwinds)} unwind order(s) "
                f"to new manager for {question[:40]}"
            )

        log.info(f"Added market: {question[:50]} (score={market['score']})")

    def _remove_market(self, condition_id: str) -> None:
        """Remove a market from the active trading set.

        Cancels active BUY orders but preserves the OrderManager and
        position tracking if there are pending unwind orders or open
        positions, so that unwind fills continue to be detected and
        Discord position reports remain accurate.

        Args:
            condition_id: The market's condition ID.
        """
        question = "Unknown"
        for m in self.active_markets:
            if m["condition_id"] == condition_id:
                question = m["question"]
                break

        if condition_id in self.order_managers:
            manager = self.order_managers[condition_id]
            # Cancel active BUY orders (stop quoting) but keep unwinds
            manager.cancel_all(reason="market removed")

            if manager.has_open_obligations():
                log.info(
                    f"Market removed from active set but keeping "
                    f"{len(manager.unwind_orders)} unwind order(s) "
                    f"tracked: {question[:50]}"
                )
            else:
                del self.order_managers[condition_id]

        # Only remove position tracking if position is flat (ignore dust < $0.05)
        pos = self.position_tracker.positions.get(condition_id)
        if pos and (pos.get("yes", 0) > 0.05 or pos.get("no", 0) > 0.05):
            log.info(
                f"Keeping position tracking for {question[:50]} "
                f"(YES=${pos['yes']:.2f} NO=${pos['no']:.2f})"
            )
        else:
            self.position_tracker.remove_market(condition_id)

        log.info(f"Removed market: {question[:50]}")

    # ── Main Loop ────────────────────────────────────────────────────────────
    def run(self) -> None:
        """Main bot loop.

        - Every MARKET_REFRESH_SECS: refresh market list.
        - Every ORDER_REFRESH_SECS: run order cycle on all active markets.
        """
        # Install signal handler so first Ctrl+C triggers clean shutdown
        def _handle_signal(signum, frame):
            if not self._shutdown_requested:
                self._shutdown_requested = True
                log.info(f"Received SIGINT — shutting down gracefully...")

        signal.signal(signal.SIGINT, _handle_signal)

        log.info("Market Making Bot Starting...")
        log.info(f"    Max markets:      {MAX_MARKETS}")
        log.info(f"    Score threshold:  {MIN_SCORE_THRESHOLD}/100")
        log.info(f"    Order size:       ${ORDER_SIZE}")
        log.info(f"    Order refresh:    every {ORDER_REFRESH_SECS}s")
        log.info(f"    Market refresh:   every {MARKET_REFRESH_SECS}s")

        # Cancel any orphaned orders from previous sessions
        self._cancel_orphaned_orders()

        # Fix legacy USD values (NO-side was computed with wrong formula)
        self.position_tracker.recalculate_usd()

        # Verify positions from disk against actual exchange balances
        self._verify_positions_on_startup()

        # Initial market fetch
        self.refresh_markets()

        while not self._shutdown_requested:
            try:
                self.cycle_count += 1
                log_cycle_start(self.cycle_count)

                # ── Heartbeat check ─────────────────────────────────────
                since_last = time.time() - self._last_successful_cycle
                if since_last > HEARTBEAT_TIMEOUT_SECS and not self._heartbeat_alerted:
                    alert_heartbeat_failure(since_last)
                    self._heartbeat_alerted = True

                # ── Market refresh check ──────────────────────────────────
                time_since_refresh = time.time() - self.last_market_refresh
                if time_since_refresh >= MARKET_REFRESH_SECS:
                    self.refresh_markets()

                # ── Run order cycle on each active market ─────────────────
                if not self.active_markets:
                    log.info("No active markets — waiting for next refresh...")
                    time.sleep(ORDER_REFRESH_SECS)
                    continue

                for market in self.active_markets:
                    if self._shutdown_requested:
                        break
                    condition_id = market["condition_id"]
                    if condition_id in self.order_managers:
                        try:
                            self.order_managers[condition_id].run_cycle()
                        except Exception as e:
                            log.error(
                                f"Cycle error for "
                                f"{market['question'][:40]}: {e}"
                            )

                # ── Check unwind-only managers (removed markets) ────────
                active_cids = {m["condition_id"] for m in self.active_markets}
                for cid in list(self.order_managers.keys()):
                    if cid not in active_cids:
                        manager = self.order_managers[cid]
                        if manager.has_open_obligations():
                            try:
                                manager.refresh_cached_book()
                                manager.detect_fills()
                                manager.reconcile_unwinds()
                            except Exception as e:
                                log.error(
                                    f"Unwind check error for removed market: {e}"
                                )
                        else:
                            # No more unwinds — clean up manager and position
                            del self.order_managers[cid]
                            pos = self.position_tracker.positions.get(cid)
                            if pos and pos.get("yes", 0) <= 0.05 and pos.get("no", 0) <= 0.05:
                                self.position_tracker.remove_market(cid)

                # Mark cycle as successful (for heartbeat monitoring)
                self._last_successful_cycle = time.time()
                self._heartbeat_alerted = False

                # ── Print position summary every 10 cycles ────────────────
                if self.cycle_count % 10 == 0:
                    self.position_tracker.print_summary()
                    alert_positions(self.position_tracker.positions)

                # ── Periodic exchange reconciliation (every 5 min) ────────
                # Catches state drift: missed fills, failed cancels,
                # external trades, etc. The startup verification runs once;
                # this runs continuously to keep state honest.
                if time.time() - self._last_reconcile >= 300:
                    self._reconcile_with_exchange()
                    self._last_reconcile = time.time()

                # ── Arbitrage detection scan (every 5 min) ──────────────
                # Scan for YES+NO complement mispricing (detection only).
                # Spread capture is now inline in reconcile_unwinds().
                if (self._arb_scanner
                        and time.time() - self._last_arb_scan >= 300):
                    try:
                        self._arb_scanner.scan_complement_arb(
                            self.active_markets
                        )
                    except Exception as e:
                        log.debug(f"Arb scan error: {e}")
                    self._last_arb_scan = time.time()

                # ── Reward earnings log (every hour) ────────────────────
                if time.time() - self._last_reward_log >= REWARD_LOG_INTERVAL_SECS:
                    try:
                        self._log_reward_earnings()
                    except Exception as e:
                        log.debug(f"Reward log error: {e}")
                    self._last_reward_log = time.time()

                # ── Wait for next cycle (interruptible) ───────────────────
                if not self._shutdown_requested:
                    log.info(f"Sleeping {ORDER_REFRESH_SECS}s until next cycle...")
                    # Sleep in 1s intervals so shutdown is responsive
                    for _ in range(ORDER_REFRESH_SECS):
                        if self._shutdown_requested:
                            break
                        time.sleep(1)

            except Exception as e:
                alert_bot_restart(str(e))
                log.info("Restarting in 30s...")
                for _ in range(30):
                    if self._shutdown_requested:
                        break
                    time.sleep(1)

        self._shutdown()

    # ── Startup Position Verification ─────────────────────────────────────────
    def _verify_positions_on_startup(self) -> None:
        """Cross-check positions.json against actual exchange token balances.

        If the tracker says we hold tokens but the exchange says we don't
        (e.g. user manually closed positions), reset the stale entries.
        This prevents phantom unwind attempts on bot restart.
        """
        positions = self.position_tracker.get_all_positions()
        if not positions:
            return

        log.info(f"Verifying {len(positions)} position(s) against exchange...")

        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

        # We need token IDs to check balances — fetch from rewards/market data
        # For positions loaded from disk, we may not have token_ids yet.
        # Use the Gamma API to look them up.
        stale_cids = []

        for cid, pos in positions.items():
            has_position = False
            for side_key in ("yes", "no"):
                shares = pos.get(f"{side_key}_shares", 0.0)
                if shares >= 1.0:
                    has_position = True
                    break

            if not has_position:
                continue

            # Try to get token IDs for this market
            try:
                token_ids = self._get_token_ids_for_condition(cid)
                if not token_ids:
                    log.warning(
                        f"Could not find token IDs for {pos.get('question', cid[:16])} "
                        f"— skipping verification"
                    )
                    continue

                for side_key, token_idx in [("yes", 0), ("no", 1)]:
                    tracked_shares = pos.get(f"{side_key}_shares", 0.0)
                    if tracked_shares < 1.0:
                        continue

                    try:
                        bal = self.client.get_balance_allowance(
                            BalanceAllowanceParams(
                                asset_type=AssetType.CONDITIONAL,
                                token_id=token_ids[token_idx],
                            )
                        )
                        actual = float(bal.get("balance", 0)) / 1e6
                    except Exception as e:
                        log.warning(f"Balance check failed for {side_key.upper()}: {e}")
                        continue

                    if actual < 1.0 and tracked_shares >= 1.0:
                        log.warning(
                            f"STALE POSITION | {pos.get('question', cid[:16])[:40]} | "
                            f"{side_key.upper()} tracker={tracked_shares:.2f} "
                            f"actual={actual:.2f} → resetting"
                        )
                        self.position_tracker.reset_side(cid, side_key)
                    elif abs(actual - tracked_shares) > 1.0:
                        log.warning(
                            f"POSITION MISMATCH | {pos.get('question', cid[:16])[:40]} | "
                            f"{side_key.upper()} tracker={tracked_shares:.2f} "
                            f"actual={actual:.2f} → correcting"
                        )
                        self.position_tracker.set_shares(cid, side_key, actual)
                    else:
                        log.info(
                            f"Position verified | {pos.get('question', cid[:16])[:40]} | "
                            f"{side_key.upper()} {tracked_shares:.2f} shares ✓"
                        )
            except Exception as e:
                log.warning(f"Could not verify position {cid[:16]}: {e}")

        log.info("Position verification complete.")

    def _reconcile_with_exchange(self) -> None:
        """Periodic reconciliation: compare tracked positions vs exchange.

        Runs every 5 minutes during normal operation (not just at startup).
        Catches mid-session drift: missed fills, failed cancels, external
        trades, or any other source of state divergence.

        Unlike _verify_positions_on_startup (which runs once), this is
        lightweight — it only checks markets that have an active manager
        with token_ids already available.
        """
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

        corrections = 0
        for cid, manager in self.order_managers.items():
            token_ids = manager.market.get("token_ids", [])
            if len(token_ids) < 2:
                continue

            for side_key, token_idx in [("yes", 0), ("no", 1)]:
                tracked = self.position_tracker.get_shares(cid, side_key)

                try:
                    bal = self.client.get_balance_allowance(
                        BalanceAllowanceParams(
                            asset_type=AssetType.CONDITIONAL,
                            token_id=token_ids[token_idx],
                        )
                    )
                    actual = float(bal.get("balance", 0)) / 1e6
                except Exception:
                    continue  # Skip on API error — don't block

                # Only correct significant mismatches (> 1 share)
                if abs(actual - tracked) <= 1.0:
                    continue

                question = manager.market.get("question", cid[:16])[:40]

                if actual < 1.0 and tracked >= 1.0:
                    log.warning(
                        f"RECONCILE | {question} | {side_key.upper()} "
                        f"tracker={tracked:.2f} actual={actual:.2f} -> resetting"
                    )
                    self.position_tracker.reset_side(cid, side_key)
                    corrections += 1
                elif actual >= 1.0 and tracked < 1.0:
                    # Discovered tokens we don't know about
                    yes_price = manager.market.get("yes_price") or 0.50
                    log.warning(
                        f"RECONCILE | {question} | {side_key.upper()} "
                        f"tracker={tracked:.2f} actual={actual:.2f} -> recording"
                    )
                    self.position_tracker.record_fill(
                        cid, side_key, actual, yes_price,
                        question=manager.market.get("question", ""),
                    )
                    corrections += 1
                else:
                    log.warning(
                        f"RECONCILE | {question} | {side_key.upper()} "
                        f"tracker={tracked:.2f} actual={actual:.2f} -> correcting"
                    )
                    self.position_tracker.set_shares(cid, side_key, actual)
                    corrections += 1

        if corrections > 0:
            log.info(f"Reconciliation complete: {corrections} correction(s)")
        else:
            log.debug("Reconciliation complete: no drift detected")

        # Create unwind-only OrderManagers for positions that aren't in the
        # active market set.  Without this, positions from previous sessions
        # that aren't re-selected sit in the wallet with no sell orders.
        self._create_unwind_managers_for_orphaned_positions()

    def _create_unwind_managers_for_orphaned_positions(self) -> None:
        """Create OrderManagers for positions loaded from disk that have no manager.

        After a restart, positions.json may contain markets that aren't in the
        active set.  Without a manager, reconcile_unwinds() never runs and
        SELL orders are never placed.
        """
        positions = self.position_tracker.get_all_positions()
        if not positions:
            return

        for cid, pos in positions.items():
            # Skip if already managed
            if cid in self.order_managers:
                continue

            # Check if there's a meaningful position
            yes_shares = pos.get("yes_shares", 0.0)
            no_shares = pos.get("no_shares", 0.0)
            if yes_shares < 1.0 and no_shares < 1.0:
                continue

            # Look up market data to build a minimal market dict
            token_ids = self._get_token_ids_for_condition(cid)
            if not token_ids:
                log.warning(
                    f"Cannot create unwind manager for "
                    f"{pos.get('question', cid[:16])[:40]} — no token IDs"
                )
                continue

            # Build a minimal market dict for the OrderManager
            question = pos.get("question", f"unknown-{cid[:12]}")
            yes_price = None
            if yes_shares > 0 and pos.get("yes_avg_price", 0) > 0:
                yes_price = pos["yes_avg_price"]
            elif no_shares > 0 and pos.get("no_avg_price", 0) > 0:
                yes_price = 1 - pos["no_avg_price"]

            minimal_market = {
                "condition_id": cid,
                "question": question,
                "token_ids": token_ids,
                "yes_price": yes_price or 0.50,
                "daily_rate": 0,
                "min_size": 1.0,
                "max_spread": 0.10,
                "tick_size": 0.01,
            }

            self.order_managers[cid] = OrderManager(
                self.client, minimal_market, self.position_tracker,
                balance_gate=self.balance_gate,
            )
            log.info(
                f"Created unwind-only manager for orphaned position: "
                f"{question[:40]} | YES={yes_shares:.1f} NO={no_shares:.1f}"
            )

    def _get_token_ids_for_condition(self, condition_id: str) -> list[str] | None:
        """Look up YES/NO token IDs for a condition_id via the Gamma API.

        Returns:
            List of [yes_token_id, no_token_id] or None on failure.
        """
        try:
            import requests
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"condition_id": condition_id},
                timeout=10,
            )
            resp.raise_for_status()
            markets = resp.json()
            if markets and len(markets) > 0:
                market = markets[0]
                tokens = market.get("tokens", [])
                if len(tokens) >= 2:
                    yes_token = next(
                        (t["token_id"] for t in tokens
                         if t.get("outcome", "").lower() == "yes"),
                        tokens[0]["token_id"],
                    )
                    no_token = next(
                        (t["token_id"] for t in tokens
                         if t.get("outcome", "").lower() == "no"),
                        tokens[1]["token_id"],
                    )
                    return [yes_token, no_token]
            return None
        except Exception as e:
            log.warning(f"Gamma API lookup failed for {condition_id[:16]}: {e}")
            return None

    def _ensure_unwind_managers(self, fetched_markets: list[dict]) -> None:
        """Create unwind managers for positions that have no manager.

        Uses token data from the rewards markets we already fetched,
        avoiding a separate Gamma API lookup (which can fail for some
        condition_ids).  Called after each refresh_markets().
        """
        positions = self.position_tracker.get_all_positions()
        if not positions:
            return

        # Build a lookup from condition_id to market data
        market_lookup = {m["condition_id"]: m for m in fetched_markets}

        for cid, pos in positions.items():
            if cid in self.order_managers:
                continue

            yes_shares = pos.get("yes_shares", 0.0)
            no_shares = pos.get("no_shares", 0.0)
            if yes_shares < 1.0 and no_shares < 1.0:
                continue

            # Try to find this market in the fetched data
            market_data = market_lookup.get(cid)
            if market_data and market_data.get("token_ids"):
                self.order_managers[cid] = OrderManager(
                    self.client, market_data, self.position_tracker,
                    balance_gate=self.balance_gate,
                )

                log.info(
                    f"Created unwind manager from rewards data: "
                    f"{pos.get('question', cid[:16])[:40]} | "
                    f"YES={yes_shares:.1f} NO={no_shares:.1f}"
                )
                continue

            # Fallback: try Gamma API
            token_ids = self._get_token_ids_for_condition(cid)
            if not token_ids:
                log.warning(
                    f"Cannot create unwind manager for "
                    f"{pos.get('question', cid[:16])[:40]} — no token IDs"
                )
                continue

            question = pos.get("question", f"unknown-{cid[:12]}")
            yes_price = None
            if yes_shares > 0 and pos.get("yes_avg_price", 0) > 0:
                yes_price = pos["yes_avg_price"]
            elif no_shares > 0 and pos.get("no_avg_price", 0) > 0:
                yes_price = 1 - pos["no_avg_price"]

            minimal_market = {
                "condition_id": cid,
                "question": question,
                "token_ids": token_ids,
                "yes_price": yes_price or 0.50,
                "daily_rate": 0,
                "min_size": 1.0,
                "max_spread": 0.10,
                "tick_size": 0.01,
            }
            self.order_managers[cid] = OrderManager(
                self.client, minimal_market, self.position_tracker,
                balance_gate=self.balance_gate,
            )
            log.info(
                f"Created unwind manager from Gamma API: "
                f"{question[:40]} | YES={yes_shares:.1f} NO={no_shares:.1f}"
            )

    # ── Orphaned Order Cleanup ────────────────────────────────────────────────
    def _cancel_orphaned_orders(self) -> None:
        """Cancel ALL orphaned orders from previous sessions.

        Both BUY and SELL orders are cancelled. reconcile_unwinds will
        place fresh sells based on actual inventory and current VWAP —
        no need to inherit stale orders from a previous session.
        """
        try:
            open_orders = self.client.get_orders()
            if not open_orders:
                log.info("No orphaned orders found — starting clean.")
                return

            log.warning(
                f"Found {len(open_orders)} orphaned order(s) from "
                f"previous session — cancelling ALL..."
            )
            for order in open_orders:
                side_label = order.get("side", "?")
                price = order.get("price", "?")
                try:
                    self.client.cancel(order["id"])
                    log.info(
                        f"Cancelled orphaned order {order['id'][:16]}... "
                        f"({side_label} @ {price})"
                    )
                except Exception as e:
                    log.error(
                        f"Failed to cancel orphaned order "
                        f"{order['id'][:16]}...: {e}"
                    )

            log.info(
                "Orphaned order cleanup complete — "
                "reconcile_unwinds will place fresh sells from inventory."
            )
        except Exception as e:
            log.error(f"Could not fetch open orders for cleanup: {e}")
            log.warning("Old orders may still be live — check Polymarket UI")

    # ── Reward Earnings Tracking ─────────────────────────────────────────────
    def _log_reward_earnings(self) -> None:
        """Query and log estimated reward earnings for visibility.

        Uses the CLOB API rewards endpoint if available, otherwise
        estimates from market daily rates and order presence time.
        Logs per-market breakdown and total for profitability tracking.
        """
        try:
            import requests

            # Try the CLOB rewards earned endpoint
            headers = {
                "POLY_API_KEY": CLOB_API_KEY,
                "POLY_SECRET": CLOB_SECRET,
                "POLY_PASSPHRASE": CLOB_PASS_PHRASE,
            }
            resp = requests.get(
                f"{HOST}/rewards/earned",
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    total = float(data.get("total_earned", 0))
                    log.info(
                        f"REWARD EARNINGS | total=${total:.2f} | "
                        f"source=CLOB API"
                    )
                    # Send to Discord if significant
                    if total > 0:
                        from alerts import _send_discord
                        _send_discord(
                            "Reward Earnings",
                            {
                                "title": "Liquidity Reward Earnings",
                                "description": f"Total earned: ${total:.2f}",
                                "color": 0x2ECC71,
                            },
                        )
                    return
                elif isinstance(data, list):
                    total = sum(float(item.get("earned", 0)) for item in data)
                    log.info(
                        f"REWARD EARNINGS | total=${total:.2f} | "
                        f"markets={len(data)} | source=CLOB API"
                    )
                    return

            # API returned non-200 — fall back to estimation
            log.debug(
                f"Rewards API returned {resp.status_code} — "
                f"using estimation fallback"
            )

        except Exception as e:
            log.debug(f"Rewards API query failed: {e} — using estimation")

        # ── Fallback: estimate from daily rates and active time ────────
        # Each market has a daily_rate. Our share depends on how much of
        # the time we have qualifying orders on the book and our order
        # size relative to total eligible volume. This is a rough estimate.
        if not self.active_markets:
            return

        total_daily_rate = sum(
            m.get("daily_rate", 0) for m in self.active_markets
        )
        # Rough estimate: we capture ~5-15% of each market's rewards
        # (one of many makers, but we're on both sides continuously)
        est_pct = 0.10
        est_daily = total_daily_rate * est_pct
        est_hourly = est_daily / 24

        market_lines = []
        for m in self.active_markets:
            rate = m.get("daily_rate", 0)
            est = rate * est_pct
            market_lines.append(
                f"  {m['question'][:40]} | "
                f"pool=${rate:.0f}/day | est_share=${est:.2f}/day"
            )

        log.info(
            f"REWARD ESTIMATE | "
            f"total_pool=${total_daily_rate:.0f}/day | "
            f"est_share=${est_daily:.2f}/day (${est_hourly:.2f}/hr) | "
            f"markets={len(self.active_markets)}"
        )
        for line in market_lines:
            log.info(f"  REWARD {line}")

    # ── Shutdown ─────────────────────────────────────────────────────────────
    def _shutdown(self) -> None:
        """Clean shutdown — cancel all orders (including unwinds) before exiting."""
        log.info("Shutting down bot...")
        for condition_id, manager in self.order_managers.items():
            manager.cancel_all(reason="bot shutdown", include_unwinds=True)
        self.position_tracker.print_summary()
        log.info("All orders cancelled. Bot stopped cleanly.")
