import time
import logging
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

from config import (
    HOST, CHAIN_ID, PRIVATE_KEY,
    CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE,
    MAX_MARKETS, MIN_SCORE_THRESHOLD,
    MARKET_REFRESH_SECS, ORDER_REFRESH_SECS,
    ORDER_SIZE, FUNDER, SIGNATURE_TYPE
)
from market import get_rewards_markets
from position import PositionTracker
from orders import OrderManager
from alerts import (
    setup_logger, alert_bot_restart, alert_no_markets,
    alert_api_failure, log_cycle_start, log_market_refresh
)

log = logging.getLogger(__name__)


class MarketMakerBot:
    """
    Main market making bot.
    Orchestrates market selection, order management,
    position tracking and alerting.
    """

    def __init__(self):
        self.client              = None
        self.position_tracker    = PositionTracker()
        self.order_managers      = {}
        self.active_markets      = []
        self.cycle_count         = 0
        self.last_market_refresh = 0

    # ── Client Setup ──────────────────────────────────────────────────────────
    def connect(self):
        """Establish connection to Polymarket CLOB API."""
        try:
            creds = ApiCreds(
                api_key=CLOB_API_KEY,
                api_secret=CLOB_SECRET,
                api_passphrase=CLOB_PASS_PHRASE,
            )
            self.client = ClobClient(
                HOST,
                key=PRIVATE_KEY,
                chain_id=CHAIN_ID,
                creds=creds,
                signature_type=SIGNATURE_TYPE,
                funder=FUNDER
            )
            log.info("✅  Connected to Polymarket CLOB API")
            return True
        except Exception as e:
            alert_api_failure(str(e))
            return False

    # ── Market Management ─────────────────────────────────────────────────────
    def refresh_markets(self):
        """
        Fetch, score and select markets to trade.
        Called every 30 minutes and on bot startup.
        """
        log.info("Refreshing market list...")

        new_markets = get_rewards_markets(limit=MAX_MARKETS)
        eligible    = [m for m in new_markets if m["score"] >= MIN_SCORE_THRESHOLD]

        if not eligible:
            alert_no_markets()
            if self.active_markets:
                log.info("Keeping existing markets until eligible ones are found.")
            return

        current_ids = {m["condition_id"] for m in self.active_markets}
        new_ids     = {m["condition_id"] for m in eligible}

        to_remove = current_ids - new_ids
        to_add    = new_ids - current_ids

        for condition_id in to_remove:
            self._remove_market(condition_id)

        for market in eligible:
            if market["condition_id"] in to_add:
                self._add_market(market)

        self.active_markets      = eligible
        self.last_market_refresh = time.time()

        log_market_refresh(self.active_markets)

    def _add_market(self, market):
        """Add a market to the active trading set."""
        condition_id = market["condition_id"]
        question     = market["question"]

        self.position_tracker.register_market(condition_id, question)
        self.order_managers[condition_id] = OrderManager(
            self.client, market, self.position_tracker
        )
        log.info(f"Added market: {question[:50]} (score={market['score']})")

    def _remove_market(self, condition_id):
        """Remove a market — cancel all its orders and clean up."""
        question = "Unknown"
        for m in self.active_markets:
            if m["condition_id"] == condition_id:
                question = m["question"]
                break

        if condition_id in self.order_managers:
            self.order_managers[condition_id].cancel_all(reason="market removed")
            del self.order_managers[condition_id]

        self.position_tracker.remove_market(condition_id)
        log.info(f"Removed market: {question[:50]}")

    # ── Main Loop ─────────────────────────────────────────────────────────────
    def run(self):
        """
        Main bot loop:
        - Every 30 mins: refresh market list
        - Every 30s:     run order cycle on all active markets
        """
        log.info("Market Making Bot Starting...")
        log.info(f"    Max markets:      {MAX_MARKETS}")
        log.info(f"    Score threshold:  {MIN_SCORE_THRESHOLD}/100")
        log.info(f"    Order size:       ${ORDER_SIZE}")
        log.info(f"    Order refresh:    every {ORDER_REFRESH_SECS}s")
        log.info(f"    Market refresh:   every {MARKET_REFRESH_SECS}s")

        # Initial market fetch
        self.refresh_markets()

        while True:
            try:
                self.cycle_count += 1
                log_cycle_start(self.cycle_count)

                # ── Market refresh check ───────────────────────────────────
                time_since_refresh = time.time() - self.last_market_refresh
                if time_since_refresh >= MARKET_REFRESH_SECS:
                    self.refresh_markets()

                # ── Run order cycle on each active market ──────────────────
                if not self.active_markets:
                    log.info("No active markets — waiting for next refresh...")
                    time.sleep(ORDER_REFRESH_SECS)
                    continue

                for market in self.active_markets:
                    condition_id = market["condition_id"]
                    if condition_id in self.order_managers:
                        try:
                            self.order_managers[condition_id].run_cycle()
                        except Exception as e:
                            log.error(
                                f"Cycle error for "
                                f"{market['question'][:40]}: {e}"
                            )

                # ── Print position summary every 10 cycles ─────────────────
                if self.cycle_count % 10 == 0:
                    self.position_tracker.print_summary()

                # ── Wait for next cycle ────────────────────────────────────
                log.info(f"Sleeping {ORDER_REFRESH_SECS}s until next cycle...")
                time.sleep(ORDER_REFRESH_SECS)

            except KeyboardInterrupt:
                self._shutdown()
                break

            except Exception as e:
                alert_bot_restart(str(e))
                log.info("Restarting in 30s...")
                time.sleep(30)

    # ── Shutdown ──────────────────────────────────────────────────────────────
    def _shutdown(self):
        """Clean shutdown — cancel all orders before exiting."""
        log.info("Shutting down bot...")
        for condition_id, manager in self.order_managers.items():
            manager.cancel_all(reason="bot shutdown")
        self.position_tracker.print_summary()
        log.info("All orders cancelled. Bot stopped cleanly.")