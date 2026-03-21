"""
Logging setup and alert functions for the Polymarket bot.

Provides structured logging to both console (INFO) and file (DEBUG),
plus specialised alert helpers that write to a separate alerts.log for
quick incident review.
"""

import logging
import os
import requests
from datetime import datetime
from config import DISCORD_WEBHOOK_URL

# ── Log File Setup ───────────────────────────────────────────────────────────
LOG_DIR: str = "logs"
LOG_FILE: str = os.path.join(LOG_DIR, "bot.log")

os.makedirs(LOG_DIR, exist_ok=True)

log = logging.getLogger(__name__)


def setup_logger() -> logging.Logger:
    """Configure the root logger with console and file handlers.

    Console handler outputs INFO and above.
    File handler captures everything from DEBUG upwards.

    Returns:
        The configured root logger.
    """
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)

    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger.addHandler(console)
    logger.addHandler(file_handler)

    return logger


# ── Position Alerts ──────────────────────────────────────────────────────────
def alert_position_limit(
    market_question: str, side: str, position_usd: float
) -> None:
    """Log a warning when a position limit is reached.

    Args:
        market_question: Human-readable market name.
        side: "YES" or "NO".
        position_usd: Current position value in USD.
    """
    msg = (
        f"POSITION LIMIT HIT | "
        f"Market: {market_question[:40]} | "
        f"Side: {side} | "
        f"Position: ${position_usd:.2f} | "
        f"Action: Stopped quoting {side} side"
    )
    log.warning(msg)
    _write_alert("POSITION_LIMIT", msg)


def alert_market_swapped(
    old_market: str, new_market: str, reason: str
) -> None:
    """Log when one active market is replaced by another.

    Args:
        old_market: Name of the removed market.
        new_market: Name of the newly added market.
        reason: Why the swap happened.
    """
    msg = (
        f"MARKET SWAPPED | "
        f"Removed: {old_market[:40]} | "
        f"Added: {new_market[:40]} | "
        f"Reason: {reason}"
    )
    log.warning(msg)
    _write_alert("MARKET_SWAP", msg)


def alert_market_removed(market_question: str, reason: str) -> None:
    """Log when a market is removed from the active set.

    Args:
        market_question: Human-readable market name.
        reason: Why the market was removed.
    """
    msg = (
        f"MARKET REMOVED | "
        f"Market: {market_question[:40]} | "
        f"Reason: {reason}"
    )
    log.warning(msg)
    _write_alert("MARKET_REMOVED", msg)


def alert_order_failure(
    market_question: str, side: str, error: str, consecutive_count: int
) -> None:
    """Log an order placement failure.

    Args:
        market_question: Human-readable market name.
        side: "YES" or "NO".
        error: Error message from the exchange.
        consecutive_count: How many failures in a row on this side.
    """
    msg = (
        f"ORDER FAILURE #{consecutive_count} | "
        f"Market: {market_question[:40]} | "
        f"Side: {side} | "
        f"Error: {error}"
    )
    log.error(msg)
    _write_alert("ORDER_FAILURE", msg)


def alert_api_failure(error: str) -> None:
    """Log a CLOB API connection failure.

    Args:
        error: Error message.
    """
    msg = f"API CONNECTION FAILURE | Error: {error}"
    log.error(msg)
    _write_alert("API_FAILURE", msg)


def alert_bot_restart(reason: str) -> None:
    """Log that the bot is restarting after an unexpected error.

    Args:
        reason: Description of what went wrong.
    """
    msg = f"BOT RESTARTING | Reason: {reason}"
    log.warning(msg)
    _write_alert("BOT_RESTART", msg)


def alert_no_markets() -> None:
    """Log that no markets passed the score threshold."""
    msg = "NO ELIGIBLE MARKETS | No markets passed the score threshold."
    log.warning(msg)
    _write_alert("NO_MARKETS", msg)


def alert_danger_zone(
    market_question: str, side: str, order_price: float, best_price: float
) -> None:
    """Log when an order is dangerously close to the midpoint.

    Args:
        market_question: Human-readable market name.
        side: "YES" or "NO".
        order_price: Price of our order.
        best_price: Current best price / midpoint.
    """
    gap = abs(order_price - best_price)
    msg = (
        f"DANGER ZONE | "
        f"Market: {market_question[:40]} | "
        f"Side: {side} | "
        f"Order: {order_price:.4f} | "
        f"Best: {best_price:.4f} | "
        f"Gap: {gap * 100:.2f}c"
    )
    log.warning(msg)
    _write_alert("DANGER_ZONE", msg)


# ── Informational Logging ───────────────────────────────────────────────────
def log_cycle_start(cycle_number: int) -> None:
    """Log the beginning of a new order cycle.

    Args:
        cycle_number: Sequential cycle counter.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info("-" * 50)
    log.info(f"CYCLE #{cycle_number} | {now}")
    log.info("-" * 50)


def log_order_placed(
    side: str, price: float, size: float, market_question: str, order_id: str
) -> None:
    """Log a successful order placement.

    Args:
        side: "YES" or "NO".
        price: Order price.
        size: Order size in shares.
        market_question: Human-readable market name.
        order_id: Exchange order identifier.
    """
    log.info(
        f"ORDER PLACED | {side} | price={price:.4f} | "
        f"size={size} | market={market_question[:40]} | id={order_id}"
    )


def log_order_cancelled(order_id: str, reason: str) -> None:
    """Log an order cancellation.

    Args:
        order_id: Exchange order identifier.
        reason: Why the order was cancelled.
    """
    log.info(f"ORDER CANCELLED | id={order_id} | reason={reason}")


def log_position_update(
    market_question: str, yes_pos: float, no_pos: float
) -> None:
    """Log a position change after a fill.

    Args:
        market_question: Human-readable market name.
        yes_pos: Current Yes position in USD.
        no_pos: Current No position in USD.
    """
    log.info(
        f"POSITION | {market_question[:40]} | "
        f"YES=${yes_pos:.2f} | NO=${no_pos:.2f}"
    )


def log_market_refresh(markets: list[dict]) -> None:
    """Log the result of a market refresh.

    Args:
        markets: List of selected market dicts.
    """
    log.info(f"MARKET REFRESH | {len(markets)} markets selected:")
    for i, m in enumerate(markets, 1):
        log.info(
            f"  #{i} {m['question'][:50]} | "
            f"score={m['score']} | rate=${m['daily_rate']:.0f}/day"
        )


# ── Discord Notifications ────────────────────────────────────────────────────
def _send_discord(content: str, embed: dict | None = None) -> None:
    """Send a message to the configured Discord webhook.

    Silently fails if DISCORD_WEBHOOK_URL is not set or the request
    errors — Discord notifications should never crash the bot.

    Args:
        content: Plain text message body.
        embed: Optional Discord embed dict for rich formatting.
    """
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        payload: dict = {"content": content}
        if embed:
            payload["embeds"] = [embed]
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        log.debug(f"Discord notification failed (non-critical): {e}")


def alert_fill(
    fill_type: str,
    side: str,
    price: float,
    filled_shares: float,
    filled_usd: float,
    market_question: str,
    remaining_shares: float | None = None,
) -> None:
    """Send a Discord notification when an order is filled.

    Also logs locally and writes to alerts.log.

    Args:
        fill_type: "FULL" or "PARTIAL".
        side: "YES" or "NO".
        price: Fill price.
        filled_shares: Number of shares filled.
        filled_usd: Dollar value of the fill.
        market_question: Human-readable market name.
        remaining_shares: Shares still open (for partial fills).
    """
    # Build the message
    if fill_type == "PARTIAL":
        title = "Partial Fill Detected"
        description = (
            f"**{filled_shares:.2f} shares** filled on the "
            f"**{side}** side\n"
            f"{remaining_shares:.2f} shares still open"
        )
        color = 0xFFA500  # Orange
    else:
        title = "Order Fully Filled"
        description = f"**{side}** order completely filled"
        color = 0xFF4444  # Red — demands attention

    embed = {
        "title": title,
        "description": description,
        "color": color,
        "fields": [
            {"name": "Market", "value": market_question[:80], "inline": False},
            {"name": "Side", "value": side, "inline": True},
            {"name": "Price", "value": f"${price:.4f}", "inline": True},
            {"name": "Value", "value": f"${filled_usd:.2f}", "inline": True},
        ],
        "timestamp": datetime.utcnow().isoformat(),
    }

    _send_discord(f"**{title}** — {side} ${filled_usd:.2f}", embed)

    msg = (
        f"FILL ({fill_type}) | {side} | "
        f"price={price:.4f} | shares={filled_shares:.2f} | "
        f"value=${filled_usd:.2f} | "
        f"market={market_question[:40]}"
    )
    _write_alert("FILL", msg)


def alert_unwind(
    side: str,
    price: float,
    size: float,
    usd_value: float,
    market_question: str,
) -> None:
    """Send a Discord notification when inventory is successfully unwound.

    Args:
        side: "YES" or "NO".
        price: The price at which inventory was sold.
        size: Number of shares sold.
        usd_value: Dollar value of the unwind.
        market_question: Human-readable market name.
    """
    embed = {
        "title": "Inventory Unwound",
        "description": f"**{side}** position sold at acquisition price",
        "color": 0x2ECC71,  # Green — position closed
        "fields": [
            {"name": "Market", "value": market_question[:80], "inline": False},
            {"name": "Side", "value": side, "inline": True},
            {"name": "Price", "value": f"${price:.4f}", "inline": True},
            {"name": "Value", "value": f"${usd_value:.2f}", "inline": True},
            {"name": "Shares", "value": f"{size:.2f}", "inline": True},
        ],
        "timestamp": datetime.utcnow().isoformat(),
    }
    _send_discord(f"**Inventory Unwound** — {side} ${usd_value:.2f}", embed)

    msg = (
        f"UNWIND | {side} | price={price:.4f} | "
        f"shares={size:.2f} | value=${usd_value:.2f} | "
        f"market={market_question[:40]}"
    )
    _write_alert("UNWIND", msg)


def alert_positions(positions: dict) -> None:
    """Send a position summary to Discord.

    Args:
        positions: Dict from PositionTracker.positions keyed by condition_id.
    """
    fields = []
    for cid, pos in positions.items():
        yes_val = pos.get("yes", 0.0)
        no_val = pos.get("no", 0.0)
        if yes_val > 0 or no_val > 0:
            fields.append({
                "name": pos.get("question", cid)[:50],
                "value": f"YES: ${yes_val:.2f} | NO: ${no_val:.2f}",
                "inline": False,
            })

    if not fields:
        fields = [{
            "name": "No open positions",
            "value": "All inventory unwound",
            "inline": False,
        }]

    embed = {
        "title": "Current Positions",
        "color": 0x3498DB,  # Blue — informational
        "fields": fields,
        "timestamp": datetime.utcnow().isoformat(),
    }
    _send_discord("**Position Update**", embed)


def alert_bot_crash(error: str) -> None:
    """Send a Discord notification when the bot crashes.

    Args:
        error: The exception message.
    """
    embed = {
        "title": "Bot Crashed!",
        "description": f"```{error[:500]}```",
        "color": 0xFF0000,  # Bright red
        "fields": [
            {"name": "Action", "value": "All orders have been cancelled.", "inline": False},
        ],
        "timestamp": datetime.utcnow().isoformat(),
    }
    _send_discord("**BOT CRASHED** — all orders cancelled", embed)


# ── Alert File Writer ────────────────────────────────────────────────────────
def _write_alert(alert_type: str, message: str) -> None:
    """Append an alert to the dedicated alerts log file.

    Args:
        alert_type: Short category tag (e.g. "ORDER_FAILURE").
        message: Full alert message.
    """
    alerts_file = os.path.join(LOG_DIR, "alerts.log")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(alerts_file, "a") as f:
        f.write(f"\n[{timestamp}] [{alert_type}]\n{message}\n")
