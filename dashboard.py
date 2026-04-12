"""
Streamlit monitoring dashboard for the Polymarket reward farming bot.

Reads bot_history.db (read-only) and market_allocations.json to display
market performance, P&L, positions, and system health.

Launch: streamlit run dashboard.py --server.port 8501
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_DIR, "bot_history.db")
ALLOC_PATH = os.path.join(_DIR, "market_allocations.json")
REFRESH_SECS = 60

load_dotenv(os.path.join(_DIR, ".env"))
FUNDER = os.getenv("FUNDER", "")
DATA_API = "https://data-api.polymarket.com"


# ---------------------------------------------------------------------------
# Database helper
# ---------------------------------------------------------------------------
def query_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run a read-only query and return a DataFrame."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


def query_scalar(sql: str, params: tuple = ()):
    """Run a read-only query and return a single scalar value."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def query_kv(sql: str, params: tuple = ()) -> dict:
    """Run a query returning key-value pairs as a dict."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    try:
        rows = conn.execute(sql, params).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------
def _ts_ago(unix_ts) -> str:
    """Convert Unix timestamp to 'X min ago' string."""
    if not unix_ts:
        return "never"
    try:
        delta = time.time() - float(unix_ts)
    except (TypeError, ValueError):
        return "unknown"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{delta / 3600:.1f}h ago"
    return f"{delta / 86400:.1f}d ago"


def _ts_str(unix_ts) -> str:
    """Convert Unix timestamp to readable datetime string."""
    if not unix_ts:
        return ""
    try:
        return datetime.fromtimestamp(float(unix_ts), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (TypeError, ValueError):
        return ""


def _usd(val) -> str:
    """Format a number as USD."""
    if val is None:
        return "$0.00"
    return f"${val:,.2f}" if val >= 0 else f"-${abs(val):,.2f}"


def _pct(val) -> str:
    """Format a number as percentage."""
    if val is None:
        return "0%"
    return f"{val * 100:.1f}%"


def _trunc(text: str, n: int = 55) -> str:
    """Truncate a string."""
    if not text:
        return ""
    return text if len(text) <= n else text[: n - 1] + "..."


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------
def get_latest_performance(action_filter: str = "all") -> pd.DataFrame:
    base = """
        SELECT condition_id, question, net_score, corrected_daily, fill_cost,
               dump_revenue, q_share_pct, fill_count, shares_recommended, action,
               on_book_hours, estimated_daily, correction_factor
        FROM market_performance
        WHERE ts = (SELECT MAX(ts) FROM market_performance)
    """
    if action_filter == "deploy":
        base += " AND action = 'deploy'"
    elif action_filter == "avoid":
        base += " AND action = 'avoid'"
    base += " ORDER BY net_score DESC"
    return query_df(base)


def get_market_score_history(condition_id: str, days: int = 7) -> pd.DataFrame:
    cutoff = time.time() - days * 86400
    return query_df(
        """SELECT ts, net_score, corrected_daily, fill_cost, dump_revenue,
                  q_share_pct, shares_recommended, action
           FROM market_performance
           WHERE condition_id = ? AND ts > ?
           ORDER BY ts""",
        (condition_id, cutoff),
    )


def get_market_fills(condition_id: str, limit: int = 50) -> pd.DataFrame:
    return query_df(
        """SELECT ts, side, shares, price, usd_value, slippage, fill_type
           FROM fills WHERE condition_id = ? ORDER BY ts DESC LIMIT ?""",
        (condition_id, limit),
    )


def get_market_unwinds(condition_id: str, limit: int = 50) -> pd.DataFrame:
    return query_df(
        """SELECT ts, side, shares, sell_price, usd_value, pnl,
                  hold_duration_secs, unwind_type
           FROM unwinds WHERE condition_id = ? ORDER BY ts DESC LIMIT ?""",
        (condition_id, limit),
    )


def get_market_feedback(condition_id: str) -> pd.DataFrame:
    return query_df(
        "SELECT side, status, reason, ts FROM placement_feedback WHERE condition_id = ?",
        (condition_id,),
    )


def get_pnl_summary() -> dict:
    realized = query_scalar("SELECT COALESCE(SUM(pnl), 0) FROM unwinds") or 0
    stop_loss = query_scalar("SELECT COALESCE(SUM(loss_usd), 0) FROM stop_losses") or 0
    num_fills = query_scalar("SELECT COUNT(*) FROM fills") or 0
    num_unwinds = query_scalar("SELECT COUNT(*) FROM unwinds") or 0
    num_stops = query_scalar("SELECT COUNT(*) FROM stop_losses") or 0
    return {
        "realized_pnl": realized,
        "stop_loss_total": stop_loss,
        "net": realized - stop_loss,
        "num_fills": num_fills,
        "num_unwinds": num_unwinds,
        "num_stops": num_stops,
    }


def get_daily_pnl() -> pd.DataFrame:
    return query_df(
        """SELECT date(ts, 'unixepoch') as day,
                  SUM(CASE WHEN pnl >= 0 THEN pnl ELSE 0 END) as gains,
                  SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) as losses,
                  SUM(pnl) as net_pnl,
                  COUNT(*) as unwind_count
           FROM unwinds GROUP BY day ORDER BY day"""
    )


def get_daily_fills() -> pd.DataFrame:
    return query_df(
        """SELECT date(ts, 'unixepoch') as day,
                  COUNT(*) as fill_count,
                  SUM(usd_value) as total_usd,
                  AVG(slippage) as avg_slippage
           FROM fills GROUP BY day ORDER BY day"""
    )


def get_recent_fills(limit: int = 20) -> pd.DataFrame:
    return query_df(
        """SELECT ts, question, side, shares, price, usd_value, slippage
           FROM fills ORDER BY ts DESC LIMIT ?""",
        (limit,),
    )


def get_recent_unwinds(limit: int = 20) -> pd.DataFrame:
    return query_df(
        """SELECT ts, question, side, shares, sell_price, usd_value, pnl,
                  hold_duration_secs
           FROM unwinds ORDER BY ts DESC LIMIT ?""",
        (limit,),
    )


def get_positions() -> pd.DataFrame:
    return query_df(
        """SELECT condition_id, question, yes_shares, yes_avg_price,
                  no_shares, no_avg_price, yes_halted, no_halted, updated_at
           FROM positions"""
    )


def get_exchange_positions() -> pd.DataFrame:
    """Fetch real positions from Polymarket Data API."""
    if not FUNDER:
        return pd.DataFrame()
    try:
        resp = requests.get(
            f"{DATA_API}/positions",
            params={"user": FUNDER, "sizeThreshold": "0.1"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return pd.DataFrame()
        return pd.DataFrame(data)
    except Exception:
        return pd.DataFrame()


def get_system_health() -> dict:
    kv = query_kv(
        "SELECT key, value FROM reward_tracker_state WHERE key IN "
        "('usdc_balance', 'usdc_balance_at', 'bot_start')"
    )
    last_order = query_scalar("SELECT MAX(ts) FROM orders_placed")
    last_fill = query_scalar("SELECT MAX(ts) FROM fills")
    last_agent = query_scalar("SELECT MAX(ts) FROM market_performance")
    active_orders = query_scalar("SELECT COUNT(*) FROM active_orders") or 0
    active_dumps = query_scalar("SELECT COUNT(*) FROM dump_states") or 0
    return {
        "usdc_balance": float(kv.get("usdc_balance", 0)),
        "usdc_balance_at": kv.get("usdc_balance_at"),
        "bot_start": kv.get("bot_start"),
        "last_order": last_order,
        "last_fill": last_fill,
        "last_agent": last_agent,
        "active_orders": active_orders,
        "active_dumps": active_dumps,
    }


def get_correction_history() -> pd.DataFrame:
    return query_df(
        "SELECT ts, raw, smoothed FROM correction_factor_history ORDER BY ts"
    )


def load_allocations() -> dict | None:
    try:
        with open(ALLOC_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Polymarket Reward Farmer", layout="wide")

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
st.sidebar.title("Reward Farmer")
auto_refresh = st.sidebar.toggle("Auto-refresh (60s)", value=True)
days_range = st.sidebar.selectbox("Time range (days)", [1, 3, 7, 14, 30], index=2)
action_filter = st.sidebar.radio(
    "Market filter", ["Deploy only", "Avoid only", "All"], index=0
)
filter_map = {"Deploy only": "deploy", "Avoid only": "avoid", "All": "all"}

# DB file info
if os.path.exists(DB_PATH):
    db_size = os.path.getsize(DB_PATH) / (1024 * 1024)
    db_mtime = os.path.getmtime(DB_PATH)
    st.sidebar.caption(f"DB: {db_size:.1f} MB, updated {_ts_ago(db_mtime)}")
else:
    st.sidebar.error("bot_history.db not found")

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_perf, tab_pnl, tab_pos, tab_health = st.tabs(
    ["Market Performance", "P&L", "Positions", "System Health"]
)

# ===== TAB 1: MARKET PERFORMANCE =====
with tab_perf:
    perf = get_latest_performance(filter_map[action_filter])

    if perf.empty:
        st.warning("No market performance data found.")
    else:
        # Summary metrics
        deployed = perf[perf["action"] == "deploy"] if action_filter == "All" else perf
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Markets shown", len(perf))
        c2.metric(
            "Avg net score",
            f"{perf['net_score'].mean():.4f}" if len(perf) else "0",
        )
        c3.metric(
            "Total est daily reward",
            _usd(perf["corrected_daily"].sum()),
        )
        c4.metric(
            "Correction factor",
            f"{perf['correction_factor'].iloc[0]:.2f}" if len(perf) else "?",
        )

        # Main table
        display = perf.copy()
        display["question"] = display["question"].apply(lambda q: _trunc(q))
        display["q_share_pct"] = display["q_share_pct"].apply(_pct)
        display = display[
            [
                "question",
                "action",
                "net_score",
                "corrected_daily",
                "fill_cost",
                "dump_revenue",
                "q_share_pct",
                "fill_count",
                "shares_recommended",
            ]
        ]
        display.columns = [
            "Market",
            "Action",
            "Score",
            "Est $/day",
            "Fill cost",
            "Dump rev",
            "Q-share",
            "Fills",
            "Shares",
        ]

        def _color_neg_pos(v):
            if isinstance(v, (int, float)):
                if v > 0:
                    return "color: green"
                if v < 0:
                    return "color: red"
            return ""

        st.dataframe(
            display.style.map(
                _color_neg_pos,
                subset=["Score", "Est $/day", "Fill cost", "Dump rev"],
            ),
            use_container_width=True,
            height=450,
        )

        # Drill-down
        st.subheader("Market drill-down")
        market_options = perf[["condition_id", "question"]].copy()
        market_options["label"] = market_options["question"].apply(
            lambda q: _trunc(q, 80)
        )
        selected_label = st.selectbox(
            "Select market",
            market_options["label"].tolist(),
            index=0,
        )
        selected_row = market_options[market_options["label"] == selected_label].iloc[0]
        cid = selected_row["condition_id"]

        # Score history chart
        with st.expander("Score history", expanded=True):
            hist = get_market_score_history(cid, days_range)
            if hist.empty:
                st.info("No history for this market in the selected range.")
            else:
                hist["time"] = pd.to_datetime(hist["ts"], unit="s", utc=True)
                chart_data = hist.set_index("time")[
                    ["net_score", "corrected_daily", "fill_cost", "dump_revenue"]
                ]
                st.line_chart(chart_data)

        # Fill history
        with st.expander("Fill history"):
            fills = get_market_fills(cid)
            if fills.empty:
                st.info("No fills for this market.")
            else:
                fills["time"] = fills["ts"].apply(_ts_str)
                st.dataframe(
                    fills[
                        ["time", "side", "shares", "price", "usd_value", "slippage", "fill_type"]
                    ],
                    use_container_width=True,
                )

        # Unwind history
        with st.expander("Unwind history"):
            unwinds = get_market_unwinds(cid)
            if unwinds.empty:
                st.info("No unwinds for this market.")
            else:
                unwinds["time"] = unwinds["ts"].apply(_ts_str)
                unwinds["hold_hrs"] = (unwinds["hold_duration_secs"] / 3600).round(1)
                st.dataframe(
                    unwinds[
                        ["time", "side", "shares", "sell_price", "pnl", "hold_hrs", "unwind_type"]
                    ],
                    use_container_width=True,
                )

        # Placement feedback
        with st.expander("Placement feedback"):
            fb = get_market_feedback(cid)
            if fb.empty:
                st.info("No placement feedback.")
            else:
                fb["time"] = fb["ts"].apply(_ts_str)
                st.dataframe(fb[["time", "side", "status", "reason"]], use_container_width=True)


# ===== TAB 2: P&L =====
with tab_pnl:
    pnl = get_pnl_summary()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Realized P&L (unwinds)", _usd(pnl["realized_pnl"]))
    c2.metric("Stop-loss damage", _usd(-pnl["stop_loss_total"]))
    c3.metric("Net (unwinds + stops)", _usd(pnl["net"]))
    c4.metric(
        "Fills / Unwinds / Stops",
        f"{pnl['num_fills']} / {pnl['num_unwinds']} / {pnl['num_stops']}",
    )

    # Daily P&L chart
    st.subheader("Daily unwind P&L")
    daily = get_daily_pnl()
    if daily.empty:
        st.info("No unwind data yet.")
    else:
        daily = daily.set_index("day")
        st.bar_chart(daily[["gains", "losses", "net_pnl"]])

    # Daily fills chart
    st.subheader("Daily fill activity")
    daily_f = get_daily_fills()
    if not daily_f.empty:
        daily_f = daily_f.set_index("day")
        st.bar_chart(daily_f[["fill_count"]])

    # Recent transactions
    col_l, col_r = st.columns(2)
    with col_l:
        st.subheader("Recent fills")
        rf = get_recent_fills()
        if not rf.empty:
            rf["time"] = rf["ts"].apply(_ts_str)
            rf["question"] = rf["question"].apply(lambda q: _trunc(q, 40))
            st.dataframe(
                rf[["time", "question", "side", "shares", "price", "usd_value", "slippage"]],
                use_container_width=True,
                height=350,
            )

    with col_r:
        st.subheader("Recent unwinds")
        ru = get_recent_unwinds()
        if not ru.empty:
            ru["time"] = ru["ts"].apply(_ts_str)
            ru["question"] = ru["question"].apply(lambda q: _trunc(q, 40))
            ru["hold_hrs"] = (ru["hold_duration_secs"] / 3600).round(1)
            st.dataframe(
                ru[["time", "question", "side", "shares", "sell_price", "pnl", "hold_hrs"]],
                use_container_width=True,
                height=350,
            )


# ===== TAB 3: POSITIONS =====
with tab_pos:
    # Exchange positions (source of truth)
    ex_pos = get_exchange_positions()

    if ex_pos.empty:
        st.warning("Could not fetch exchange positions (check FUNDER in .env).")
    else:
        total_value = ex_pos["currentValue"].sum()
        total_pnl = ex_pos["cashPnl"].sum()
        total_cost = ex_pos["initialValue"].sum()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Positions", len(ex_pos))
        c2.metric("Total cost", _usd(total_cost))
        c3.metric("Current value", _usd(total_value))
        c4.metric("Unrealized P&L", _usd(total_pnl))

        display_ex = ex_pos.copy()
        display_ex["market"] = display_ex["title"].apply(lambda t: _trunc(t, 55))
        display_ex["side"] = display_ex["outcome"]
        display_ex["shares"] = display_ex["size"].round(2)
        display_ex["avg"] = display_ex["avgPrice"].round(4)
        display_ex["now"] = display_ex["curPrice"].round(4)
        display_ex["value"] = display_ex["currentValue"].round(2)
        display_ex["pnl"] = display_ex["cashPnl"].round(2)
        display_ex["pnl_pct"] = display_ex["percentPnl"].round(1)
        display_ex["expires"] = display_ex["endDate"]
        display_ex = display_ex[
            ["market", "side", "shares", "avg", "now", "value", "pnl", "pnl_pct", "expires"]
        ]
        display_ex.columns = [
            "Market", "Side", "Shares", "Avg", "Now", "Value $", "P&L $", "P&L %", "Expires",
        ]
        st.dataframe(
            display_ex.style.map(
                lambda v: "color: green" if isinstance(v, (int, float)) and v > 0 else (
                    "color: red" if isinstance(v, (int, float)) and v < 0 else ""
                ),
                subset=["P&L $", "P&L %"],
            ),
            use_container_width=True,
        )

    # Bot-tracked positions (may be stale)
    with st.expander("Bot-tracked positions (internal DB — may be stale)"):
        pos = get_positions()
        if pos.empty:
            st.info("No bot-tracked positions.")
        else:
            pos["yes_usd"] = (pos["yes_shares"] * pos["yes_avg_price"]).round(2)
            pos["no_usd"] = (pos["no_shares"] * (1 - pos["no_avg_price"])).round(2)
            pos["total_usd"] = pos["yes_usd"] + pos["no_usd"]

            display_pos = pos.copy()
            display_pos["question"] = display_pos["question"].apply(lambda q: _trunc(q, 50))
            display_pos["updated"] = display_pos["updated_at"].apply(_ts_ago)
            display_pos = display_pos[
                [
                    "question", "yes_shares", "yes_avg_price", "yes_usd",
                    "no_shares", "no_avg_price", "no_usd", "total_usd", "updated",
                ]
            ]
            display_pos.columns = [
                "Market", "YES sh", "YES avg", "YES $",
                "NO sh", "NO avg", "NO $", "Total $", "Updated",
            ]
            st.dataframe(display_pos, use_container_width=True)


# ===== TAB 4: SYSTEM HEALTH =====
with tab_health:
    health = get_system_health()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("USDC balance", _usd(health["usdc_balance"]))
    c2.metric("Last order", _ts_ago(health["last_order"]))
    c3.metric("Last fill", _ts_ago(health["last_fill"]))
    c4.metric("Last agent run", _ts_ago(health["last_agent"]))

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Active orders", health["active_orders"])
    c6.metric("Active dumps", health["active_dumps"])
    c7.metric("Bot started", _ts_str(health["bot_start"]))
    c8.metric("Balance updated", _ts_ago(health["usdc_balance_at"]))

    # Correction factor trend
    st.subheader("Correction factor trend")
    cf = get_correction_history()
    if cf.empty:
        st.info("No correction factor history.")
    else:
        cf["time"] = pd.to_datetime(cf["ts"], unit="s", utc=True)
        chart = cf.set_index("time")[["raw", "smoothed"]]
        st.line_chart(chart)

    # Current allocations summary
    alloc = load_allocations()
    if alloc:
        st.subheader("Current agent allocations")
        ac1, ac2, ac3 = st.columns(3)
        ac1.metric("Deploy", alloc.get("num_deploy", "?"))
        ac2.metric("Avoid", alloc.get("num_avoid", "?"))
        ac3.metric("Generated", alloc.get("generated_at", "?")[:19])


# ---------------------------------------------------------------------------
# Auto-refresh
# ---------------------------------------------------------------------------
if auto_refresh:
    time.sleep(REFRESH_SECS)
    st.rerun()
