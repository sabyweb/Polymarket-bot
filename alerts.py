"""
Logging setup and alert functions for the Polymarket bot.

Provides structured logging to both console (INFO) and file (DEBUG),
plus specialised alert helpers that write to a separate alerts.log for
quick incident review.
"""

import logging
import logging.handlers
import os
import requests
from datetime import datetime
from config import (
    DISCORD_WEBHOOK_URL,
    DISCORD_CRITICAL_WEBHOOK_URL,
    DISCORD_CRITICAL_MENTION,
)

# ── Log File Setup ───────────────────────────────────────────────────────────
LOG_DIR: str = "logs"
LOG_FILE: str = os.path.join(LOG_DIR, "bot.log")

os.makedirs(LOG_DIR, exist_ok=True)

log = logging.getLogger(__name__)


def setup_logger() -> logging.Logger:
    """Configure the root logger with console and rotating file handlers.

    Console handler outputs INFO and above.
    File handler captures everything from DEBUG upwards with automatic
    rotation: 10 MB per file, 5 backups kept (50 MB total max).

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

    # Rotating file handler: 10 MB per file, keep 5 backups (50 MB total)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger.addHandler(console)
    logger.addHandler(file_handler)

    # Silence noisy HTTP/2 frame-level debug logging from httpx/httpcore/h2.
    # These generate thousands of lines per cycle (header encoding, TLS
    # handshakes, frame decoding) and bloat the log file to 100+ MB.
    for noisy in ("httpx", "httpcore", "h2", "hpack", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

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
        est = m.get("est_daily_reward", 0)
        cap = m.get("capture_pct", 0)
        log.info(
            f"  #{i} {m['question'][:50]} | "
            f"score={m['score']} | rate=${m['daily_rate']:.0f}/day | "
            f"est=${est:.1f}/day ({cap:.0f}% of pool)"
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


def _send_critical(content: str, embed: dict | None = None) -> None:
    """Send a CRITICAL alert to the dedicated low-volume Discord channel with an
    @mention so it pierces a muted routine channel.

    Routes to DISCORD_CRITICAL_WEBHOOK_URL; falls back to DISCORD_WEBHOOK_URL if
    the critical webhook isn't configured — a kill/crash page is never silently
    dropped. Fail-safe: never raises (notifications must not crash the bot).
    """
    url = DISCORD_CRITICAL_WEBHOOK_URL or DISCORD_WEBHOOK_URL
    if not url:
        return
    mention = f"{DISCORD_CRITICAL_MENTION} " if DISCORD_CRITICAL_MENTION else ""
    try:
        payload: dict = {
            "content": f"{mention}{content}",
            # Webhooks suppress @here/@everyone/role/user pings unless explicitly
            # allowed — this is what makes the mention actually notify.
            "allowed_mentions": {"parse": ["everyone", "roles", "users"]},
        }
        if embed:
            payload["embeds"] = [embed]
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        log.debug(f"Discord critical notification failed (non-critical): {e}")


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
        if yes_val > 0.05 or no_val > 0.05:  # Ignore dust < $0.05
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


def alert_merge_needed(
    market_question: str, yes_shares: float, no_shares: float,
    mergeable: float, freed_usd: float,
) -> None:
    """Send urgent Discord alert when YES+NO positions need manual merge.

    Args:
        market_question: Human-readable market name.
        yes_shares: YES shares held.
        no_shares: NO shares held.
        mergeable: Number of pairs that can be merged.
        freed_usd: USDC that would be freed by merging.
    """
    embed = {
        "title": "MERGE NEEDED — Capital Locked",
        "description": (
            f"Holding **both** YES and NO on the same market.\n"
            f"Merge via Polymarket UI to free **${freed_usd:.0f} USDC**.\n\n"
            f"**Market:** {market_question}\n"
            f"**YES:** {yes_shares:.1f} shares\n"
            f"**NO:** {no_shares:.1f} shares\n"
            f"**Mergeable pairs:** {mergeable:.1f}\n\n"
            f"Go to [Polymarket Portfolio](https://polymarket.com/portfolio) → "
            f"find this market → click **Merge Positions**"
        ),
        "color": 0xFF0000,  # Red — urgent
        "timestamp": datetime.utcnow().isoformat(),
    }
    _send_critical("**ACTION REQUIRED** — merge positions to free capital", embed)

    msg = (
        f"MERGE NEEDED | {market_question[:40]} | "
        f"YES={yes_shares:.1f} NO={no_shares:.1f} | "
        f"mergeable={mergeable:.1f} | would_free=${freed_usd:.0f}"
    )
    log.warning(msg)
    _write_alert("MERGE", msg)


def alert_wallet_desync(
    divergence: float,
    threshold_usd: float,
    actual_wallet: float,
    expected_wallet: float,
) -> None:
    """Page the operator on a [CRITICAL] WALLET_DESYNC breach (FX-074).

    The FX-049 reconciler is OBSERVATIONAL — it never halts or gates
    allocation. This helper turns its [CRITICAL] log into a real external
    alert (Discord) so a desync pages, not just logs. Reuses the existing
    _send_discord channel; never raises (Discord send is best-effort).

    Args:
        divergence: actual_wallet − expected_wallet (signed USD).
        threshold_usd: the breached tolerance.
        actual_wallet: live on-chain pUSD balance this cycle.
        expected_wallet: bot-DB-derived expected balance this cycle.
    """
    embed = {
        "title": "WALLET DESYNC — On-chain vs Bot-DB Divergence",
        "description": (
            f"Reconciler divergence **${divergence:+.4f}** exceeds threshold "
            f"**${threshold_usd:.4f}**.\n\n"
            f"**Actual wallet:** ${actual_wallet:.4f}\n"
            f"**Expected wallet:** ${expected_wallet:.4f}\n\n"
            f"Observational alert — trading is NOT halted. Investigate: "
            f"missed fill, phantom unwind, external wallet activity, or "
            f"Polymarket reporting lag."
        ),
        "color": 0xFF0000,  # Red — urgent
        "timestamp": datetime.utcnow().isoformat(),
    }
    _send_discord(
        f"**WALLET DESYNC** — divergence ${divergence:+.4f} "
        f"(threshold ${threshold_usd:.4f})",
        embed,
    )

    msg = (
        f"WALLET_DESYNC | divergence=${divergence:+.4f} "
        f"threshold=${threshold_usd:.4f} | "
        f"actual=${actual_wallet:.4f} expected=${expected_wallet:.4f}"
    )
    log.critical(msg)
    _write_alert("WALLET_DESYNC", msg)


def alert_heartbeat_failure(last_success_secs_ago: float, process: str = "bot") -> None:
    """Send a Discord alert when a process has not cycled recently.

    This catches silent failures — e.g. the process is running but all API
    calls return empty data, no markets pass hygiene, or it is fully hung.

    Args:
        last_success_secs_ago: Seconds since the process's last heartbeat/cycle.
        process: Which process is stale (e.g. "farmer", "oversight"). Defaults
            to "bot" so the legacy single-process caller is unchanged (FX-083).
    """
    minutes = last_success_secs_ago / 60
    embed = {
        "title": f"Heartbeat Missing — {process}",
        "description": (
            f"No cycle from **{process}** in **{minutes:.0f} minutes**.\n\n"
            f"The process may be hung, all markets may be failing hygiene, "
            f"or API calls may be timing out. Existing positions are NOT being "
            f"monitored while it is down."
        ),
        "color": 0xFF6600,  # Orange — warning
        "timestamp": datetime.utcnow().isoformat(),
    }
    _send_critical(f"**HEARTBEAT ALERT** — {process} may be stalled", embed)

    msg = f"HEARTBEAT MISSING | {process} | no cycle in {minutes:.0f} minutes"
    log.warning(msg)
    _write_alert("HEARTBEAT", msg)


# FX-083: cross-process heartbeat staleness paging. Each production process
# (farmer ~30s, oversight ~30min) writes its own heartbeat to the DB every
# cycle and checks its PEER's heartbeat via this helper. A hung/dead peer is
# paged to Discord (deduped). Per-process module state — each process imports
# its own copy, tracking only the peer(s) it watches, so keys never collide.
_HB_LAST_ALERT: dict[str, float] = {}


def maybe_alert_stale_heartbeat(
    peer_name: str,
    last_hb_ts: "float | None",
    now: float,
    stale_secs: float,
    repage_secs: float = 1800.0,
) -> bool:
    """Page (deduped) when a peer process heartbeat is stale.

    `last_hb_ts is None` (never written / read error) or `<= 0` is treated as
    UNKNOWN, not stale → no page. This is the fail-open contract: a fresh
    deploy where the peer hasn't cycled yet must never false-page.

    Returns True iff a page was emitted this call. Re-pages at most once per
    `repage_secs` while the peer stays stale; clears the dedup state the moment
    the peer is healthy again, so the next stall episode pages promptly.
    """
    if last_hb_ts is None or last_hb_ts <= 0:
        return False
    if not stale_secs or stale_secs <= 0:
        return False  # disabled
    age = now - last_hb_ts
    if age < stale_secs:
        _HB_LAST_ALERT.pop(peer_name, None)  # healthy → reset dedup
        return False
    last = _HB_LAST_ALERT.get(peer_name)
    if last is not None and (now - last) < repage_secs:
        return False  # already paged recently — suppress spam
    _HB_LAST_ALERT[peer_name] = now
    alert_heartbeat_failure(age, process=peer_name)
    return True


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
    _send_critical("**BOT CRASHED** — all orders cancelled", embed)


def alert_kill_switch(reason: str, cancelled_orders: int = 0) -> None:
    """Page Discord when a farmer kill switch trips (trading halts until restart).

    FX-092: a kill leaves THIS process ALIVE-but-idle, so the stale-heartbeat
    alert (maybe_alert_stale_heartbeat) never fires for it — this page is the
    ONLY thing that surfaces a kill to the operator. Called once per kill
    episode from reward_farmer._activate_kill_switch. Fail-safe: _send_discord
    no-ops without a webhook and never raises.
    """
    embed = {
        "title": "KILL SWITCH ACTIVATED",
        "description": (
            f"Farmer halted — idle until restarted.\n```{str(reason)[:400]}```"
        ),
        "color": 0xFF0000,  # red
        "fields": [
            {"name": "Cancelled orders", "value": str(cancelled_orders), "inline": True},
            {"name": "Action", "value": "Investigate, then restart polymarket-farmer", "inline": False},
        ],
        "timestamp": datetime.utcnow().isoformat(),
    }
    _send_critical("**KILL SWITCH** — farmer halted (idle until restart)", embed)
    _write_alert("KILL_SWITCH", f"{reason} | cancelled {cancelled_orders} orders")


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
