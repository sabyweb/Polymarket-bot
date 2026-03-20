"""
Entry point for the Polymarket market-making bot.

Handles top-level exception logging and ensures all open orders are
cancelled on crash so that stale quotes never remain on the exchange.
"""

import logging
from alerts import setup_logger, alert_bot_crash
from bot import MarketMakerBot

log = logging.getLogger(__name__)


def main() -> None:
    """Initialise logging, connect, and run the bot."""
    setup_logger()

    bot = MarketMakerBot()

    if not bot.connect():
        log.error(
            "Failed to connect to Polymarket. "
            "Check your credentials in .env"
        )
        return

    try:
        bot.run()
    except Exception as e:
        log.exception(f"Bot crashed unexpectedly: {e}")
        # Cancel all open orders so stale quotes don't linger
        bot._shutdown()
        # Notify Discord
        alert_bot_crash(str(e))
        raise


if __name__ == "__main__":
    main()
