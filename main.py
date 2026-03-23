"""
Entry point for the Polymarket market-making bot.

Handles top-level exception logging, SIGTERM signals (cloud platforms),
and ensures all open orders are cancelled on crash so that stale quotes
never remain on the exchange.
"""

import signal
import sys
import logging
from alerts import setup_logger, alert_bot_crash
from bot import MarketMakerBot

log = logging.getLogger(__name__)

# Global reference so signal handlers can access the bot
_bot: MarketMakerBot | None = None


def _signal_handler(signum: int, frame) -> None:
    """Handle SIGTERM/SIGINT — cancel all orders and exit cleanly."""
    sig_name = signal.Signals(signum).name
    log.info(f"Received {sig_name} — shutting down gracefully...")
    if _bot:
        _bot._shutdown()
    sys.exit(0)


def main() -> None:
    """Initialise logging, connect, and run the bot."""
    global _bot
    setup_logger()

    # Fail fast if credentials are missing
    from config import validate_credentials
    validate_credentials()

    # Register signal handlers for graceful cloud shutdown
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    _bot = MarketMakerBot()

    if not _bot.connect():
        log.error(
            "Failed to connect to Polymarket. "
            "Check your credentials in .env"
        )
        return

    try:
        _bot.run()
    except Exception as e:
        log.exception(f"Bot crashed unexpectedly: {e}")
        _bot._shutdown()
        alert_bot_crash(str(e))
        raise


if __name__ == "__main__":
    main()
