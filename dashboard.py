"""
Streamlit monitoring dashboard for the Polymarket reward farming bot.

Reads bot_history.db (read-only) + market_allocations.json + the Polymarket
data-api to display live state, market-selection diagnostics, P&L, positions,
historical trends, and system health.

Tabs:
  Overview          live KPIs + safety/kill banner + heartbeats
  Market Selection  the core diagnostic: reward vs adverse-fill damage per market
  Market Perf       latest allocator scoring + per-market drill-down
  P&L               realized P&L, daily/recent fills & unwinds
  Positions         exchange positions (data-api) + bot-tracked
  History           reward trend, correction factor, hourly/learning curves
  System Health     freshness, safety history, wallet reconciliation

All queries are read-only and defensive: a missing/empty table yields an empty
frame rather than an error, so the same file runs on a fresh DB or on prod.

Launch:  streamlit run dashboard.py --server.port 8501
Prod:    served from Helsinki against the live bot_history.db (read-only).
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

# Kill / drawdown thresholds (mirror the safety stack, display-only)
DRAWDOWN_KILL_PCT = 0.15
REALIZED_LOSS_KILL_PCT = 0.10
UNREALIZED_LOSS_KILL_PCT = 0.20


# ---------------------------------------------------------------------------
# Database helpers (defensive: empty frame / None on any error)
# ---------------------------------------------------------------------------
def query_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run a read-only query and return a DataFrame (empty on any error)."""
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return pd.DataFrame()
    conn.row_factory = sqlite3.Row
    try:
        return pd.read_sql_query(sql, conn, params=params)
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def query_scalar(sql: str, params: tuple = ()):
    """Run a read-only query and return a single scalar (None on any error)."""
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        conn.close()


def query_kv(sql: str, params: tuple = ()) -> dict:
    """Run a query returning key-value pairs as a dict (empty on any error)."""
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(sql, params).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------
def _ts_ago(unix_ts) -> str:
    if not unix_ts:
        return "never"
    try:
        delta = time.time() - float(unix_ts)
    except (TypeError, ValueError):
        return "unknown"
    if delta < 0:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{delta / 3600:.1f}h ago"
    return f"{delta / 86400:.1f}d ago"


def _ts_str(unix_ts) -> str:
    if not unix_ts:
        return ""
    try:
        return datetime.fromtimestamp(float(unix_ts), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (TypeError, ValueError):
        return ""


def _usd(val) -> str:
    if val is None:
        return "$0.00"
    try:
        val = float(val)
    except (TypeError, ValueError):
        return "$0.00"
    return f"${val:,.2f}" if val >= 0 else f"-${abs(val):,.2f}"


def _pct(val) -> str:
    if val is None:
        return "0%"
    try:
        return f"{float(val) * 100:.1f}%"
    except (TypeError, ValueError):
        return "0%"


def _trunc(text, n: int = 55) -> str:
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "..."


def _freshness(unix_ts, warn_min=15, stale_min=60) -> str:
    """Return an emoji-prefixed freshness label for a unix timestamp."""
    if not unix_ts:
        return "⚪ never"
    try:
        age_min = (time.time() - float(unix_ts)) / 60
    except (TypeError, ValueError):
        return "⚪ unknown"
    dot = "🟢" if age_min <= warn_min else ("🟡" if age_min <= stale_min else "🔴")
    return f"{dot} {_ts_ago(unix_ts)}"


# ---------------------------------------------------------------------------
# Data-api (authoritative rewards / positions)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=120, show_spinner=False)
def get_live_rewards(days: int = 14) -> pd.DataFrame:
    """Authoritative reward + maker-rebate activity from the data-api."""
    if not FUNDER:
        return pd.DataFrame()
    rows = []
    for typ in ("REWARD", "MAKER_REBATE"):
        try:
            resp = requests.get(
                f"{DATA_API}/activity",
                params={"user": FUNDER, "type": typ, "limit": 1000},
                timeout=10,
            )
            resp.raise_for_status()
            for r in resp.json() or []:
                ts = r.get("timestamp") or r.get("time") or 0
                usd = r.get("usdcSize") or r.get("size") or r.get("amount") or 0
                rows.append({"type": typ, "ts": float(ts), "usd": float(usd)})
        except Exception:
            continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    cutoff = time.time() - days * 86400
    df = df[df["ts"] >= cutoff]
    df["day"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    return df


@st.cache_data(ttl=120, show_spinner=False)
def get_exchange_positions() -> pd.DataFrame:
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
        return pd.DataFrame(data) if data else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Query functions — DB
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


def get_system_health() -> dict:
    kv = query_kv(
        "SELECT key, value FROM reward_tracker_state WHERE key IN "
        "('usdc_balance', 'usdc_balance_at', 'bot_start')"
    )
    last_order = query_scalar("SELECT MAX(ts) FROM orders_placed")
    last_fill = query_scalar("SELECT MAX(ts) FROM fills")
    last_agent = query_scalar("SELECT MAX(ts) FROM market_performance")
    last_cycle = query_scalar("SELECT MAX(ts) FROM cycle_snapshots")
    active_orders = query_scalar("SELECT COUNT(*) FROM active_orders") or 0
    active_dumps = query_scalar("SELECT COUNT(*) FROM dump_states") or 0
    try:
        usdc = float(kv.get("usdc_balance", 0) or 0)
    except (TypeError, ValueError):
        usdc = 0.0
    return {
        "usdc_balance": usdc,
        "usdc_balance_at": kv.get("usdc_balance_at"),
        "bot_start": kv.get("bot_start"),
        "last_order": last_order,
        "last_fill": last_fill,
        "last_agent": last_agent,
        "last_cycle": last_cycle,
        "active_orders": active_orders,
        "active_dumps": active_dumps,
    }


def get_heartbeats() -> dict:
    """All heartbeat:* keys from reward_tracker_state -> {name: unix_ts}."""
    kv = query_kv("SELECT key, value FROM reward_tracker_state WHERE key LIKE 'heartbeat:%'")
    out = {}
    for k, v in kv.items():
        name = k.split(":", 1)[1] if ":" in k else k
        try:
            out[name] = float(v)
        except (TypeError, ValueError):
            out[name] = None
    return out


def get_safety_latest() -> dict | None:
    df = query_df("SELECT ts, state, reason, consecutive_good FROM safety_state ORDER BY ts DESC LIMIT 1")
    return df.iloc[0].to_dict() if not df.empty else None


def get_safety_history(days: int = 7) -> pd.DataFrame:
    cutoff = time.time() - days * 86400
    return query_df(
        "SELECT ts, state, reason, consecutive_good FROM safety_state "
        "WHERE ts > ? ORDER BY ts DESC LIMIT 200",
        (cutoff,),
    )


def get_correction_history(days: int = 30) -> pd.DataFrame:
    cutoff = time.time() - days * 86400
    return query_df(
        "SELECT ts, raw, smoothed, estimated_daily, actual_daily, deployed_count "
        "FROM correction_factor_history WHERE ts > ? ORDER BY ts",
        (cutoff,),
    )


def get_reward_daily() -> pd.DataFrame:
    return query_df(
        "SELECT date, total_reward_usd, total_rebate_usd, total_combined_usd, "
        "num_markets_active, est_daily_total, correction_factor "
        "FROM reward_daily ORDER BY date"
    )


def get_hourly(days: int = 7) -> pd.DataFrame:
    cutoff = time.time() - days * 86400
    return query_df(
        "SELECT ts, hour_label, num_markets, realized_pnl, unrealized_pnl, "
        "total_position_usd, est_reward_usd, est_reward_rate_hr, num_fills, "
        "num_unwinds, num_stop_losses FROM hourly_snapshots "
        "WHERE ts > ? ORDER BY ts",
        (cutoff,),
    )


def get_learning_efficiency() -> pd.DataFrame:
    return query_df(
        "SELECT date, reward_efficiency FROM learning_efficiency_daily ORDER BY date"
    )


def get_wallet_reconcile(limit: int = 50) -> pd.DataFrame:
    return query_df(
        "SELECT ts, actual_wallet, expected_wallet, divergence, status, "
        "fills_delta, unwinds_delta, rewards_delta FROM wallet_reconcile_history "
        "ORDER BY ts DESC LIMIT ?",
        (limit,),
    )


def get_selection_log(days: int = 7) -> pd.DataFrame:
    cutoff = time.time() - days * 86400
    return query_df(
        "SELECT ts, question, action, score, daily_rate, reason, volume_24h, liquidity "
        "FROM market_selection_log WHERE ts > ? ORDER BY ts DESC LIMIT 300",
        (cutoff,),
    )


def get_market_roi() -> pd.DataFrame:
    return query_df(
        "SELECT condition_id, window, reward_earned, fill_loss, capital_committed_avg, "
        "roi, fill_count, fill_rate_per_hour, samples, last_updated "
        "FROM market_roi ORDER BY roi ASC"
    )


def get_market_stats() -> pd.DataFrame:
    """Flatten reward_market_stats JSON into a per-market diagnostic frame."""
    raw = query_df("SELECT condition_id, data, updated_at FROM reward_market_stats")
    if raw.empty:
        return pd.DataFrame()
    recs = []
    for _, row in raw.iterrows():
        try:
            d = json.loads(row["data"])
        except Exception:
            continue
        reward = float(d.get("actual_reward_usd") or 0)
        est_reward = float(d.get("est_reward_usd") or 0)
        unwind_loss = float(d.get("unwind_loss_usd") or 0)
        stop_loss = float(d.get("stop_loss_usd") or 0)
        spread_cap = float(d.get("spread_capture_usd") or 0)
        fill_damage = unwind_loss + stop_loss
        adverse = int(d.get("adverse_fills") or 0)
        favourable = int(d.get("favourable_fills") or 0)
        total_fills = adverse + favourable
        avg_inv = float(d.get("avg_inventory_usd") or 0)
        net = reward + spread_cap - fill_damage
        recs.append(
            {
                "condition_id": row["condition_id"],
                "question": d.get("question", ""),
                "daily_rate": float(d.get("daily_rate") or 0),
                "reward": reward,
                "est_reward": est_reward,
                "spread_capture": spread_cap,
                "fill_damage": fill_damage,
                "unwind_loss": unwind_loss,
                "stop_loss": stop_loss,
                "net": net,
                "adverse_fills": adverse,
                "favourable_fills": favourable,
                "adverse_ratio": (adverse / total_fills) if total_fills else 0.0,
                "buy_fills": int(d.get("buy_fills") or 0),
                "sell_fills": int(d.get("sell_fills") or 0),
                "peak_inventory": float(d.get("peak_inventory_usd") or 0),
                "avg_inventory": avg_inv,
                "cooldown_cycles": int(d.get("cooldown_cycles") or 0),
                "time_on_book_hrs": float(d.get("time_on_book_secs") or 0) / 3600.0,
                "roi": (net / avg_inv) if avg_inv else 0.0,
                "updated_at": row["updated_at"],
            }
        )
    return pd.DataFrame(recs)


def load_allocations() -> dict | None:
    try:
        with open(ALLOC_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def compute_portfolio() -> dict:
    """Best-effort live portfolio snapshot from data-api + DB."""
    health = get_system_health()
    ex = get_exchange_positions()
    inventory_value = float(ex["currentValue"].sum()) if not ex.empty and "currentValue" in ex else 0.0
    unrealized = float(ex["cashPnl"].sum()) if not ex.empty and "cashPnl" in ex else 0.0
    cash = health["usdc_balance"]
    total = cash + inventory_value
    return {
        "cash": cash,
        "inventory_value": inventory_value,
        "unrealized": unrealized,
        "total": total,
        "num_positions": 0 if ex.empty else len(ex),
    }


# ---------------------------------------------------------------------------
# Page config + sidebar
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Polymarket Reward Farmer", layout="wide")

st.sidebar.title("Reward Farmer")
auto_refresh = st.sidebar.toggle("Auto-refresh (60s)", value=True)
days_range = st.sidebar.selectbox("Time range (days)", [1, 3, 7, 14, 30], index=2)
action_filter = st.sidebar.radio("Market filter", ["Deploy only", "Avoid only", "All"], index=0)
filter_map = {"Deploy only": "deploy", "Avoid only": "avoid", "All": "all"}

if os.path.exists(DB_PATH):
    db_size = os.path.getsize(DB_PATH) / (1024 * 1024)
    db_mtime = os.path.getmtime(DB_PATH)
    st.sidebar.caption(f"DB: {db_size:.1f} MB · updated {_ts_ago(db_mtime)}")
else:
    st.sidebar.error("bot_history.db not found")
if not FUNDER:
    st.sidebar.warning("FUNDER not set in .env — live data-api panels disabled.")

# ---------------------------------------------------------------------------
# Top banner: safety / kill state
# ---------------------------------------------------------------------------
_safety = get_safety_latest()
if _safety:
    _state = str(_safety.get("state", "")).upper()
    _reason = _safety.get("reason", "")
    _when = _ts_ago(_safety.get("ts"))
    if _state in ("OK", "HEALTHY", "NORMAL", "RUNNING"):
        st.success(f"Safety: {_state} · {_when}")
    elif _state in ("DATA_UNAVAILABLE", "DEGRADED", "WARN", "WARNING", "PAUSED"):
        st.warning(f"Safety: {_state} — {_reason} · {_when}")
    else:
        st.error(f"⛔ Safety: {_state} — {_reason} · {_when}")

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
(
    tab_overview,
    tab_selection,
    tab_perf,
    tab_pnl,
    tab_pos,
    tab_history,
    tab_health,
) = st.tabs(
    [
        "Overview",
        "Market Selection",
        "Market Perf",
        "P&L",
        "Positions",
        "History",
        "System Health",
    ]
)


# ===== TAB: OVERVIEW =====
with tab_overview:
    port = compute_portfolio()
    pnl = get_pnl_summary()
    rewards = get_live_rewards(days_range)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    reward_today = (
        float(rewards[rewards["day"] == today]["usd"].sum()) if not rewards.empty else 0.0
    )
    reward_window = float(rewards["usd"].sum()) if not rewards.empty else 0.0

    # Drawdown vs peak total value (from hourly snapshots as a proxy)
    hourly = get_hourly(30)
    peak_total = None
    if not hourly.empty:
        eq = hourly["total_position_usd"].fillna(0) + hourly["realized_pnl"].fillna(0)
        peak_total = float(eq.max()) if len(eq) else None
    drawdown = None
    if peak_total and peak_total > 0:
        drawdown = (peak_total - port["total"]) / peak_total

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total value (cash + inventory)", _usd(port["total"]))
    c2.metric("Cash (USDC)", _usd(port["cash"]))
    c3.metric("Inventory value", _usd(port["inventory_value"]))
    c4.metric(
        "Drawdown vs peak",
        _pct(drawdown) if drawdown is not None else "n/a",
        delta=f"kill at {int(DRAWDOWN_KILL_PCT*100)}%",
        delta_color="off",
    )

    c5, c6, c7, c8 = st.columns(4)
    c5.metric(f"Rewards today ({today[5:]})", _usd(reward_today))
    c6.metric(f"Rewards {days_range}d", _usd(reward_window))
    c7.metric("Realized P&L (all)", _usd(pnl["realized_pnl"]))
    c8.metric("Unrealized P&L", _usd(port["unrealized"]))

    # Heartbeats + freshness
    st.subheader("Liveness")
    hb = get_heartbeats()
    health = get_system_health()
    hcols = st.columns(4)
    hcols[0].metric("Oversight heartbeat", "", help="From reward_tracker_state")
    hcols[0].write(_freshness(hb.get("oversight")))
    hcols[1].metric("Farmer heartbeat", "")
    hcols[1].write(_freshness(hb.get("farmer")))
    hcols[2].metric("Last fill", "")
    hcols[2].write(_freshness(health["last_fill"], warn_min=120, stale_min=720))
    hcols[3].metric("Last cycle", "")
    hcols[3].write(_freshness(health["last_cycle"]))

    # Allocation snapshot
    alloc = load_allocations()
    if alloc:
        st.subheader("Current plan (market_allocations.json)")
        ac1, ac2, ac3, ac4 = st.columns(4)
        ac1.metric("Deploy", alloc.get("num_deploy", "?"))
        ac2.metric("Avoid", alloc.get("num_avoid", "?"))
        ac3.metric("Capital deployed", _usd(alloc.get("total_capital_deployed")))
        gen = str(alloc.get("generated_at", ""))[:19]
        ac4.metric("Plan generated", gen)
        try:
            gen_ts = datetime.fromisoformat(
                str(alloc.get("generated_at")).replace("Z", "+00:00")
            ).timestamp()
            if time.time() - gen_ts > 2 * 3600:
                st.warning("Plan file is older than its 2h TTL — planner may be stalled.")
        except Exception:
            pass

    # Recent activity preview
    st.subheader("Recent fills")
    rf = get_recent_fills(8)
    if rf.empty:
        st.info("No fills recorded.")
    else:
        rf["time"] = rf["ts"].apply(_ts_ago)
        rf["question"] = rf["question"].apply(lambda q: _trunc(q, 50))
        st.dataframe(
            rf[["time", "question", "side", "shares", "price", "usd_value", "slippage"]],
            use_container_width=True,
            hide_index=True,
        )


# ===== TAB: MARKET SELECTION (core diagnostic) =====
with tab_selection:
    st.caption(
        "The core unsolved problem: the allocator ranks raw daily_rate × q_share and "
        "over-weights markets that adversely fill us. This view surfaces where reward "
        "capture is being eaten by fill damage."
    )
    stats = get_market_stats()

    if stats.empty:
        st.warning(
            "No reward_market_stats rows yet. On the live (Helsinki) DB this populates "
            "as markets accumulate cycles."
        )
    else:
        active = stats[(stats["reward"] != 0) | (stats["fill_damage"] != 0) | (stats["adverse_fills"] > 0)]
        total_reward = float(stats["reward"].sum())
        total_spread = float(stats["spread_capture"].sum())
        total_damage = float(stats["fill_damage"].sum())
        total_net = float(stats["net"].sum())

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Reward captured", _usd(total_reward))
        c2.metric("Spread captured", _usd(total_spread))
        c3.metric("Fill damage (unwind+stop)", _usd(-total_damage))
        c4.metric("Net (reward+spread−damage)", _usd(total_net))

        st.markdown("#### Worst markets — net loss after fill damage")
        worst = active.sort_values("net").head(25).copy()
        worst["question"] = worst["question"].apply(lambda q: _trunc(q, 45))
        show = worst[
            [
                "question", "net", "reward", "fill_damage", "adverse_fills",
                "favourable_fills", "adverse_ratio", "daily_rate", "peak_inventory",
                "cooldown_cycles",
            ]
        ].copy()
        show["adverse_ratio"] = show["adverse_ratio"].apply(_pct)
        show.columns = [
            "Market", "Net $", "Reward $", "Damage $", "Adv fills", "Fav fills",
            "Adv %", "Daily rate", "Peak inv $", "Cooldowns",
        ]

        def _np(v):
            if isinstance(v, (int, float)):
                return "color: green" if v > 0 else ("color: red" if v < 0 else "")
            return ""

        st.dataframe(
            show.style.map(_np, subset=["Net $", "Reward $", "Damage $"]),
            use_container_width=True,
            hide_index=True,
            height=430,
        )

        cL, cR = st.columns(2)
        with cL:
            st.markdown("#### Daily rate vs net outcome")
            st.caption("If high-daily-rate markets cluster below zero, the ranker is the problem.")
            scat = active[["daily_rate", "net"]].copy()
            if not scat.empty:
                st.scatter_chart(scat, x="daily_rate", y="net")
        with cR:
            st.markdown("#### Adverse-fill ratio vs net")
            scat2 = active[["adverse_ratio", "net"]].copy()
            if not scat2.empty:
                st.scatter_chart(scat2, x="adverse_ratio", y="net")

        st.markdown("#### Best markets — net positive")
        best = active.sort_values("net", ascending=False).head(15).copy()
        best["question"] = best["question"].apply(lambda q: _trunc(q, 45))
        bshow = best[["question", "net", "reward", "fill_damage", "adverse_ratio", "daily_rate"]].copy()
        bshow["adverse_ratio"] = bshow["adverse_ratio"].apply(_pct)
        bshow.columns = ["Market", "Net $", "Reward $", "Damage $", "Adv %", "Daily rate"]
        st.dataframe(bshow, use_container_width=True, hide_index=True)

    # Selection log
    with st.expander("Allocator selection log (selected / kept / removed)"):
        sel = get_selection_log(days_range)
        if sel.empty:
            st.info("No selection log entries in range.")
        else:
            sel["time"] = sel["ts"].apply(_ts_str)
            sel["question"] = sel["question"].apply(lambda q: _trunc(q, 50))
            st.dataframe(
                sel[["time", "action", "question", "score", "daily_rate", "volume_24h", "liquidity", "reason"]],
                use_container_width=True,
                hide_index=True,
            )

    # ROI table
    with st.expander("Per-market ROI windows (market_roi)"):
        roi = get_market_roi()
        if roi.empty:
            st.info("No market_roi rows yet.")
        else:
            roi["updated"] = roi["last_updated"].apply(_ts_ago)
            st.dataframe(roi, use_container_width=True, hide_index=True)


# ===== TAB: MARKET PERFORMANCE =====
with tab_perf:
    perf = get_latest_performance(filter_map[action_filter])
    if perf.empty:
        st.warning("No market performance data found.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Markets shown", len(perf))
        c2.metric("Avg net score", f"{perf['net_score'].mean():.4f}" if len(perf) else "0")
        c3.metric("Total est daily reward", _usd(perf["corrected_daily"].sum()))
        c4.metric(
            "Correction factor",
            f"{perf['correction_factor'].iloc[0]:.2f}" if len(perf) else "?",
        )

        display = perf.copy()
        display["question"] = display["question"].apply(lambda q: _trunc(q))
        display["q_share_pct"] = display["q_share_pct"].apply(_pct)
        display = display[
            ["question", "action", "net_score", "corrected_daily", "fill_cost",
             "dump_revenue", "q_share_pct", "fill_count", "shares_recommended"]
        ]
        display.columns = [
            "Market", "Action", "Score", "Est $/day", "Fill cost", "Dump rev",
            "Q-share", "Fills", "Shares",
        ]

        def _color_neg_pos(v):
            if isinstance(v, (int, float)):
                return "color: green" if v > 0 else ("color: red" if v < 0 else "")
            return ""

        st.dataframe(
            display.style.map(
                _color_neg_pos, subset=["Score", "Est $/day", "Fill cost", "Dump rev"]
            ),
            use_container_width=True,
            height=450,
            hide_index=True,
        )

        st.subheader("Market drill-down")
        market_options = perf[["condition_id", "question"]].copy()
        market_options["label"] = market_options["question"].apply(lambda q: _trunc(q, 80))
        selected_label = st.selectbox("Select market", market_options["label"].tolist(), index=0)
        selected_row = market_options[market_options["label"] == selected_label].iloc[0]
        cid = selected_row["condition_id"]

        with st.expander("Score history", expanded=True):
            hist = get_market_score_history(cid, days_range)
            if hist.empty:
                st.info("No history for this market in the selected range.")
            else:
                hist["time"] = pd.to_datetime(hist["ts"], unit="s", utc=True)
                st.line_chart(
                    hist.set_index("time")[
                        ["net_score", "corrected_daily", "fill_cost", "dump_revenue"]
                    ]
                )

        with st.expander("Fill history"):
            fills = get_market_fills(cid)
            if fills.empty:
                st.info("No fills for this market.")
            else:
                fills["time"] = fills["ts"].apply(_ts_str)
                st.dataframe(
                    fills[["time", "side", "shares", "price", "usd_value", "slippage", "fill_type"]],
                    use_container_width=True,
                    hide_index=True,
                )

        with st.expander("Unwind history"):
            unwinds = get_market_unwinds(cid)
            if unwinds.empty:
                st.info("No unwinds for this market.")
            else:
                unwinds["time"] = unwinds["ts"].apply(_ts_str)
                unwinds["hold_hrs"] = (unwinds["hold_duration_secs"] / 3600).round(1)
                st.dataframe(
                    unwinds[["time", "side", "shares", "sell_price", "pnl", "hold_hrs", "unwind_type"]],
                    use_container_width=True,
                    hide_index=True,
                )

        with st.expander("Placement feedback"):
            fb = get_market_feedback(cid)
            if fb.empty:
                st.info("No placement feedback.")
            else:
                fb["time"] = fb["ts"].apply(_ts_str)
                st.dataframe(fb[["time", "side", "status", "reason"]], use_container_width=True, hide_index=True)


# ===== TAB: P&L =====
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

    st.subheader("Daily unwind P&L")
    daily = get_daily_pnl()
    if daily.empty:
        st.info("No unwind data yet.")
    else:
        st.bar_chart(daily.set_index("day")[["gains", "losses", "net_pnl"]])

    st.subheader("Daily fill activity")
    daily_f = get_daily_fills()
    if not daily_f.empty:
        st.bar_chart(daily_f.set_index("day")[["fill_count"]])

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
                hide_index=True,
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
                hide_index=True,
            )


# ===== TAB: POSITIONS =====
with tab_pos:
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

        d = ex_pos.copy()
        d["market"] = d["title"].apply(lambda t: _trunc(t, 55))
        d["side"] = d["outcome"]
        d["shares"] = d["size"].round(2)
        d["avg"] = d["avgPrice"].round(4)
        d["now"] = d["curPrice"].round(4)
        d["value"] = d["currentValue"].round(2)
        d["pnl"] = d["cashPnl"].round(2)
        d["pnl_pct"] = d["percentPnl"].round(1)
        d["expires"] = d["endDate"]
        d = d[["market", "side", "shares", "avg", "now", "value", "pnl", "pnl_pct", "expires"]]
        d.columns = ["Market", "Side", "Shares", "Avg", "Now", "Value $", "P&L $", "P&L %", "Expires"]
        st.dataframe(
            d.style.map(
                lambda v: "color: green" if isinstance(v, (int, float)) and v > 0 else (
                    "color: red" if isinstance(v, (int, float)) and v < 0 else ""
                ),
                subset=["P&L $", "P&L %"],
            ),
            use_container_width=True,
            hide_index=True,
        )

    with st.expander("Bot-tracked positions (internal DB — may be stale)"):
        pos = get_positions()
        if pos.empty:
            st.info("No bot-tracked positions.")
        else:
            pos["yes_usd"] = (pos["yes_shares"] * pos["yes_avg_price"]).round(2)
            pos["no_usd"] = (pos["no_shares"] * (1 - pos["no_avg_price"])).round(2)
            pos["total_usd"] = pos["yes_usd"] + pos["no_usd"]
            dp = pos.copy()
            dp["question"] = dp["question"].apply(lambda q: _trunc(q, 50))
            dp["updated"] = dp["updated_at"].apply(_ts_ago)
            dp = dp[["question", "yes_shares", "yes_avg_price", "yes_usd",
                     "no_shares", "no_avg_price", "no_usd", "total_usd", "updated"]]
            dp.columns = ["Market", "YES sh", "YES avg", "YES $", "NO sh", "NO avg", "NO $", "Total $", "Updated"]
            st.dataframe(dp, use_container_width=True, hide_index=True)


# ===== TAB: HISTORY =====
with tab_history:
    st.subheader("Daily rewards (authoritative, data-api)")
    rewards = get_live_rewards(max(days_range, 14))
    if rewards.empty:
        st.info("No data-api reward activity (or FUNDER unset).")
    else:
        by_day = rewards.groupby(["day", "type"])["usd"].sum().unstack(fill_value=0)
        st.bar_chart(by_day)

    st.subheader("Reward vs estimate (reward_daily)")
    rd = get_reward_daily()
    if rd.empty:
        st.info("No reward_daily rows.")
    else:
        rd_idx = rd.set_index("date")
        st.bar_chart(rd_idx[["total_reward_usd", "total_rebate_usd"]])
        st.line_chart(rd_idx[["correction_factor"]])

    st.subheader("Correction factor: estimate vs actual")
    cf = get_correction_history(max(days_range, 30))
    if cf.empty:
        st.info("No correction factor history.")
    else:
        cf["time"] = pd.to_datetime(cf["ts"], unit="s", utc=True)
        st.line_chart(cf.set_index("time")[["raw", "smoothed"]])
        if cf[["estimated_daily", "actual_daily"]].abs().sum().sum() > 0:
            st.line_chart(cf.set_index("time")[["estimated_daily", "actual_daily"]])

    st.subheader("Hourly equity & reward rate")
    hourly = get_hourly(days_range)
    if hourly.empty:
        st.info("No hourly snapshots in range.")
    else:
        hourly["time"] = pd.to_datetime(hourly["ts"], unit="s", utc=True)
        hidx = hourly.set_index("time")
        st.line_chart(hidx[["total_position_usd", "realized_pnl", "unrealized_pnl"]])
        st.line_chart(hidx[["est_reward_rate_hr"]])

    le = get_learning_efficiency()
    if not le.empty and le["reward_efficiency"].abs().sum() > 0:
        st.subheader("Learning efficiency")
        st.line_chart(le.set_index("date")[["reward_efficiency"]])


# ===== TAB: SYSTEM HEALTH =====
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

    st.subheader("Heartbeats")
    hb = get_heartbeats()
    if not hb:
        st.info("No heartbeat keys found.")
    else:
        hcols = st.columns(max(len(hb), 1))
        for col, (name, ts) in zip(hcols, hb.items()):
            col.metric(name, _ts_ago(ts))
            col.write(_freshness(ts))

    st.subheader("Safety state history")
    sh = get_safety_history(days_range)
    if sh.empty:
        st.info("No safety state history in range.")
    else:
        sh["time"] = sh["ts"].apply(_ts_str)
        st.dataframe(
            sh[["time", "state", "consecutive_good", "reason"]],
            use_container_width=True,
            hide_index=True,
            height=260,
        )

    st.subheader("Wallet reconciliation")
    wr = get_wallet_reconcile()
    if wr.empty:
        st.info("No wallet reconciliation history.")
    else:
        wr["time"] = wr["ts"].apply(_ts_str)
        latest = wr.iloc[0]
        rc1, rc2, rc3 = st.columns(3)
        rc1.metric("Actual wallet", _usd(latest["actual_wallet"]))
        rc2.metric("Expected wallet", _usd(latest["expected_wallet"]))
        rc3.metric("Divergence", _usd(latest["divergence"]))
        st.dataframe(
            wr[["time", "status", "actual_wallet", "expected_wallet", "divergence",
                "fills_delta", "unwinds_delta", "rewards_delta"]],
            use_container_width=True,
            hide_index=True,
            height=260,
        )

    st.subheader("Correction factor trend")
    cf = get_correction_history(days_range)
    if cf.empty:
        st.info("No correction factor history.")
    else:
        cf["time"] = pd.to_datetime(cf["ts"], unit="s", utc=True)
        st.line_chart(cf.set_index("time")[["raw", "smoothed"]])

    alloc = load_allocations()
    if alloc:
        st.subheader("Current agent allocations")
        ac1, ac2, ac3 = st.columns(3)
        ac1.metric("Deploy", alloc.get("num_deploy", "?"))
        ac2.metric("Avoid", alloc.get("num_avoid", "?"))
        ac3.metric("Generated", str(alloc.get("generated_at", "?"))[:19])


# ---------------------------------------------------------------------------
# Auto-refresh
# ---------------------------------------------------------------------------
if auto_refresh:
    time.sleep(REFRESH_SECS)
    st.rerun()
