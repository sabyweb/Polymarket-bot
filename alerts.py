import logging
import os
from datetime import datetime

# ── Log File Setup ────────────────────────────────────────────────────────────
LOG_DIR  = "logs"
LOG_FILE = os.path.join(LOG_DIR, "bot.log")

os.makedirs(LOG_DIR, exist_ok=True)

log = logging.getLogger(__name__)


def setup_logger():
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
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


def alert_position_limit(market_question, side, position_usd):
    msg = (
        f"POSITION LIMIT HIT | "
        f"Market: {market_question[:40]} | "
        f"Side: {side} | "
        f"Position: ${position_usd:.2f} | "
        f"Action: Stopped quoting {side} side"
    )
    log.warning(msg)
    _write_alert("POSITION_LIMIT", msg)


def alert_market_swapped(old_market, new_market, reason):
    msg = (
        f"MARKET SWAPPED | "
        f"Removed: {old_market[:40]} | "
        f"Added: {new_market[:40]} | "
        f"Reason: {reason}"
    )
    log.warning(msg)
    _write_alert("MARKET_SWAP", msg)


def alert_market_removed(market_question, reason):
    msg = (
        f"MARKET REMOVED | "
        f"Market: {market_question[:40]} | "
        f"Reason: {reason}"
    )
    log.warning(msg)
    _write_alert("MARKET_REMOVED", msg)


def alert_order_failure(market_question, side, error, consecutive_count):
    msg = (
        f"ORDER FAILURE #{consecutive_count} | "
        f"Market: {market_question[:40]} | "
        f"Side: {side} | "
        f"Error: {error}"
    )
    log.error(msg)
    _write_alert("ORDER_FAILURE", msg)


def alert_api_failure(error):
    msg = f"API CONNECTION FAILURE | Error: {error}"
    log.error(msg)
    _write_alert("API_FAILURE", msg)


def alert_bot_restart(reason):
    msg = f"BOT RESTARTING | Reason: {reason}"
    log.warning(msg)
    _write_alert("BOT_RESTART", msg)


def alert_no_markets():
    msg = "NO ELIGIBLE MARKETS | No markets passed the score threshold."
    log.warning(msg)
    _write_alert("NO_MARKETS", msg)


def alert_danger_zone(market_question, side, order_price, best_price):
    gap = abs(order_price - best_price)
    msg = (
        f"DANGER ZONE | "
        f"Market: {market_question[:40]} | "
        f"Side: {side} | "
        f"Order: {order_price:.4f} | "
        f"Best: {best_price:.4f} | "
        f"Gap: {gap*100:.2f}c"
    )
    log.warning(msg)
    _write_alert("DANGER_ZONE", msg)


def log_cycle_start(cycle_number):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info("-" * 50)
    log.info(f"CYCLE #{cycle_number} | {now}")
    log.info("-" * 50)


def log_order_placed(side, price, size, market_question, order_id):
    log.info(
        f"ORDER PLACED | {side} | price={price:.4f} | "
        f"size={size} | market={market_question[:40]} | id={order_id}"
    )


def log_order_cancelled(order_id, reason):
    log.info(f"ORDER CANCELLED | id={order_id} | reason={reason}")


def log_position_update(market_question, yes_pos, no_pos):
    log.info(
        f"POSITION | {market_question[:40]} | "
        f"YES=${yes_pos:.2f} | NO=${no_pos:.2f}"
    )


def log_market_refresh(markets):
    log.info(f"MARKET REFRESH | {len(markets)} markets selected:")
    for i, m in enumerate(markets, 1):
        log.info(
            f"  #{i} {m['question'][:50]} | "
            f"score={m['score']} | rate=${m['daily_rate']:.0f}/day"
        )


def _write_alert(alert_type, message):
    alerts_file = os.path.join(LOG_DIR, "alerts.log")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(alerts_file, "a") as f:
        f.write(f"\n[{timestamp}] [{alert_type}]\n{message}\n")