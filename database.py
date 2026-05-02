"""
SQLite-based history and analytics database for the Polymarket bot.

Stores every fill, unwind, order placement, cancellation, and cycle
snapshot in WAL-mode SQLite. The existing JSON positions file remains
the source of truth for current state; this database is append-only
history for analytics, P&L tracking, and backtesting.

Usage:
    from database import get_db
    db = get_db()
    db.log_fill(...)
"""

import logging
import os
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "bot_history.db")

_SCHEMA = """
-- Filled BUY orders
CREATE TABLE IF NOT EXISTS fills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    condition_id TEXT   NOT NULL,
    question    TEXT    NOT NULL DEFAULT '',
    side        TEXT    NOT NULL,       -- 'yes' or 'no'
    fill_type   TEXT    NOT NULL,       -- 'FULL' or 'PARTIAL'
    shares      REAL    NOT NULL,
    price       REAL    NOT NULL,       -- YES-equivalent price
    clob_cost   REAL    NOT NULL,       -- actual CLOB cost per share
    usd_value   REAL    NOT NULL,       -- shares * clob_cost
    midpoint    REAL    NOT NULL DEFAULT 0,  -- M3: EMA midpoint at fill time
    slippage    REAL    NOT NULL DEFAULT 0   -- M3: clob_cost - midpoint (>0 = adverse)
);

-- Filled SELL (unwind) orders
CREATE TABLE IF NOT EXISTS unwinds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    condition_id TEXT   NOT NULL,
    question    TEXT    NOT NULL DEFAULT '',
    side        TEXT    NOT NULL,
    shares      REAL    NOT NULL,
    sell_price  REAL    NOT NULL,       -- CLOB sell price
    usd_value   REAL    NOT NULL,       -- shares * sell_price
    vwap_cost   REAL    NOT NULL DEFAULT 0,  -- VWAP-based cost for P&L
    pnl         REAL    NOT NULL DEFAULT 0   -- usd_value - vwap_cost
);

-- Order placements (BUY)
CREATE TABLE IF NOT EXISTS orders_placed (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    condition_id TEXT   NOT NULL,
    side        TEXT    NOT NULL,
    price       REAL    NOT NULL,
    size        REAL    NOT NULL,
    order_id    TEXT    NOT NULL DEFAULT '',
    order_type  TEXT    NOT NULL DEFAULT 'BUY'  -- 'BUY' or 'SELL'
);

-- Order cancellations
CREATE TABLE IF NOT EXISTS orders_cancelled (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    order_id    TEXT    NOT NULL,
    reason      TEXT    NOT NULL DEFAULT ''
);

-- Cycle snapshots (one row per manager per cycle)
CREATE TABLE IF NOT EXISTS cycle_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    cycle_num   INTEGER NOT NULL,
    condition_id TEXT   NOT NULL,
    best_bid    REAL,
    best_ask    REAL,
    our_bid     REAL,
    our_ask     REAL,
    yes_position_usd REAL DEFAULT 0,
    no_position_usd  REAL DEFAULT 0,
    active_orders    INTEGER DEFAULT 0,
    unwind_orders    INTEGER DEFAULT 0
);

-- Merges (YES+NO -> USDC)
CREATE TABLE IF NOT EXISTS merges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    condition_id TEXT   NOT NULL,
    shares      REAL    NOT NULL,
    freed_usd   REAL    NOT NULL
);

-- Stop-loss events
CREATE TABLE IF NOT EXISTS stop_losses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    condition_id TEXT   NOT NULL,
    side        TEXT    NOT NULL,
    shares      REAL    NOT NULL,
    cost_price  REAL    NOT NULL,
    sell_price  REAL    NOT NULL,
    loss_usd    REAL    NOT NULL
);

-- Daily P&L summary (generated once per day)
CREATE TABLE IF NOT EXISTS daily_pnl (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL UNIQUE,  -- YYYY-MM-DD
    total_bought_usd    REAL DEFAULT 0,
    total_sold_usd      REAL DEFAULT 0,
    total_merged_usd    REAL DEFAULT 0,
    realized_pnl        REAL DEFAULT 0,
    num_fills           INTEGER DEFAULT 0,
    num_unwinds         INTEGER DEFAULT 0,
    num_merges          INTEGER DEFAULT 0,
    num_stop_losses     INTEGER DEFAULT 0
);

-- Reward estimate vs. actual comparison (hourly snapshots)
CREATE TABLE IF NOT EXISTS reward_comparisons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    condition_id    TEXT    NOT NULL DEFAULT '',  -- empty = aggregate
    q_score_est     REAL    NOT NULL DEFAULT 0,
    legacy_est      REAL    NOT NULL DEFAULT 0,
    actual_earned   REAL    NOT NULL DEFAULT 0,
    q_share_pct     REAL    NOT NULL DEFAULT 0,  -- our Q / total market Q
    our_q_score     REAL    NOT NULL DEFAULT 0,
    market_q_score  REAL    NOT NULL DEFAULT 0
);

-- A3: Live position state (replaces positions.json)
CREATE TABLE IF NOT EXISTS positions (
    condition_id TEXT    PRIMARY KEY,
    question     TEXT    NOT NULL DEFAULT '',
    yes_shares   REAL    NOT NULL DEFAULT 0,
    yes_avg_price REAL   NOT NULL DEFAULT 0,
    yes_halted   INTEGER NOT NULL DEFAULT 0,
    no_shares    REAL    NOT NULL DEFAULT 0,
    no_avg_price REAL    NOT NULL DEFAULT 0,
    no_halted    INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL    NOT NULL DEFAULT 0
);

-- A3: Reward tracker scalar state (replaces reward_history.json top-level keys)
CREATE TABLE IF NOT EXISTS reward_tracker_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- A3: Reward tracker per-market stats (replaces reward_history.json markets)
CREATE TABLE IF NOT EXISTS reward_market_stats (
    condition_id TEXT PRIMARY KEY,
    data         TEXT NOT NULL,
    updated_at   REAL NOT NULL DEFAULT 0
);

-- Hourly P&L + reward snapshot (one row per hour, aggregated across all markets)
CREATE TABLE IF NOT EXISTS hourly_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    hour_label      TEXT    NOT NULL,       -- 'YYYY-MM-DD HH:00'
    num_markets     INTEGER NOT NULL DEFAULT 0,
    total_bought_usd    REAL NOT NULL DEFAULT 0,
    total_sold_usd      REAL NOT NULL DEFAULT 0,
    realized_pnl        REAL NOT NULL DEFAULT 0,
    unrealized_pnl      REAL NOT NULL DEFAULT 0,
    total_position_usd  REAL NOT NULL DEFAULT 0,
    est_reward_usd      REAL NOT NULL DEFAULT 0,  -- est rewards earned THIS hour
    est_reward_rate_hr  REAL NOT NULL DEFAULT 0,   -- current $/hr rate
    num_fills           INTEGER NOT NULL DEFAULT 0,
    num_unwinds         INTEGER NOT NULL DEFAULT 0,
    num_stop_losses     INTEGER NOT NULL DEFAULT 0,
    num_danger_cancels  INTEGER NOT NULL DEFAULT 0,
    avg_uptime_pct      REAL NOT NULL DEFAULT 0,
    config_json         TEXT NOT NULL DEFAULT '{}'  -- snapshot of key config params
);

-- Market selection decisions (why market was chosen or rejected)
CREATE TABLE IF NOT EXISTS market_selection_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    condition_id    TEXT    NOT NULL,
    question        TEXT    NOT NULL DEFAULT '',
    action          TEXT    NOT NULL,       -- 'selected', 'rejected', 'kept', 'removed'
    score           REAL    NOT NULL DEFAULT 0,
    daily_rate      REAL    NOT NULL DEFAULT 0,
    reason          TEXT    NOT NULL DEFAULT '',  -- rejection reason or score breakdown
    volume_24h      REAL    NOT NULL DEFAULT 0,
    liquidity       REAL    NOT NULL DEFAULT 0
);

-- Market performance snapshots (adaptive agent tracking)
CREATE TABLE IF NOT EXISTS market_performance (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    condition_id    TEXT    NOT NULL,
    question        TEXT    NOT NULL DEFAULT '',
    window_hours    REAL    NOT NULL DEFAULT 24,
    estimated_daily REAL    NOT NULL DEFAULT 0,
    correction_factor REAL  NOT NULL DEFAULT 1.0,
    corrected_daily REAL    NOT NULL DEFAULT 0,
    fill_cost       REAL    NOT NULL DEFAULT 0,
    dump_revenue    REAL    NOT NULL DEFAULT 0,
    net_score       REAL    NOT NULL DEFAULT 0,
    action          TEXT    NOT NULL DEFAULT 'deploy',
    q_share_pct     REAL    NOT NULL DEFAULT 0,
    on_book_hours   REAL    NOT NULL DEFAULT 0,
    fill_count      INTEGER NOT NULL DEFAULT 0,
    shares_recommended INTEGER NOT NULL DEFAULT 50
);
CREATE INDEX IF NOT EXISTS idx_mp_cid ON market_performance(condition_id);
CREATE INDEX IF NOT EXISTS idx_mp_ts ON market_performance(ts);

-- Market expiry cache (avoid 671 CLOB calls per agent cycle)
CREATE TABLE IF NOT EXISTS market_expiry_cache (
    condition_id TEXT PRIMARY KEY,
    end_date_iso TEXT NOT NULL,
    fetched_at   REAL NOT NULL
);

-- Placement feedback (bot → agent closed loop)
CREATE TABLE IF NOT EXISTS placement_feedback (
    ts              REAL    NOT NULL,
    condition_id    TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    status          TEXT    NOT NULL,
    reason          TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (condition_id, side)
);

-- Active dump state (persisted for crash recovery)
CREATE TABLE IF NOT EXISTS dump_states (
    condition_id TEXT NOT NULL,
    side         TEXT NOT NULL,
    fill_price   REAL NOT NULL,
    started_at   REAL NOT NULL,
    shares       REAL NOT NULL,
    tid          TEXT NOT NULL,
    dump_order_id TEXT DEFAULT '',
    last_passive_reprice REAL DEFAULT 0,
    PRIMARY KEY (condition_id, side)
);

-- Active orders placed by the bot (persisted for crash recovery)
CREATE TABLE IF NOT EXISTS active_orders (
    order_id     TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL,
    side         TEXT NOT NULL,
    order_type   TEXT NOT NULL DEFAULT 'buy',
    price        REAL NOT NULL,
    shares       REAL NOT NULL,
    placed_at    REAL NOT NULL
);

-- Phase 0: Order book snapshots (one row per market per book fetch)
CREATE TABLE IF NOT EXISTS book_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    condition_id    TEXT    NOT NULL,
    best_bid        REAL    NOT NULL,
    best_ask        REAL    NOT NULL,
    midpoint        REAL    NOT NULL,
    spread          REAL    NOT NULL,
    bid_depth_5c    REAL    NOT NULL DEFAULT 0,
    ask_depth_5c    REAL    NOT NULL DEFAULT 0,
    bid_depth_10c   REAL    NOT NULL DEFAULT 0,
    ask_depth_10c   REAL    NOT NULL DEFAULT 0,
    total_bid_depth REAL    NOT NULL DEFAULT 0,
    total_ask_depth REAL    NOT NULL DEFAULT 0,
    num_bid_levels  INTEGER NOT NULL DEFAULT 0,
    num_ask_levels  INTEGER NOT NULL DEFAULT 0,
    our_bid_price   REAL    NOT NULL DEFAULT 0,
    our_ask_price   REAL    NOT NULL DEFAULT 0,
    our_bid_depth_ahead REAL NOT NULL DEFAULT 0,
    our_ask_depth_ahead REAL NOT NULL DEFAULT 0,
    daily_rate      REAL    NOT NULL DEFAULT 0,
    max_spread      REAL    NOT NULL DEFAULT 0,
    agent_shares    REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bs_cid_ts ON book_snapshots(condition_id, ts);
CREATE INDEX IF NOT EXISTS idx_bs_ts ON book_snapshots(ts);

-- Phase 0: Per-order scoring snapshots (from are_orders_scoring API)
CREATE TABLE IF NOT EXISTS scoring_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    order_id        TEXT    NOT NULL,
    condition_id    TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    scoring         INTEGER NOT NULL,
    price           REAL    NOT NULL DEFAULT 0,
    shares          REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ss_cid_ts ON scoring_snapshots(condition_id, ts);
CREATE INDEX IF NOT EXISTS idx_ss_ts ON scoring_snapshots(ts);

-- Phase 0: Daily reward payout (exact totals from Data API)
CREATE TABLE IF NOT EXISTS reward_daily (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    date                TEXT    NOT NULL,
    total_reward_usd    REAL    NOT NULL DEFAULT 0,
    total_rebate_usd    REAL    NOT NULL DEFAULT 0,
    total_combined_usd  REAL    NOT NULL DEFAULT 0,
    num_markets_active  INTEGER NOT NULL DEFAULT 0,
    est_daily_total     REAL    NOT NULL DEFAULT 0,
    correction_factor   REAL    NOT NULL DEFAULT 0,
    UNIQUE(date)
);

-- Phase 0: Per-market context for each reward day
CREATE TABLE IF NOT EXISTS reward_daily_markets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    date                TEXT    NOT NULL,
    condition_id        TEXT    NOT NULL,
    scoring_seconds     REAL    NOT NULL DEFAULT 0,
    avg_bid_size        REAL    NOT NULL DEFAULT 0,
    avg_ask_size        REAL    NOT NULL DEFAULT 0,
    avg_spread          REAL    NOT NULL DEFAULT 0,
    avg_midpoint        REAL    NOT NULL DEFAULT 0,
    daily_rate          REAL    NOT NULL DEFAULT 0,
    max_spread_cfg      REAL    NOT NULL DEFAULT 0,
    fill_count          INTEGER NOT NULL DEFAULT 0,
    UNIQUE(date, condition_id)
);
CREATE INDEX IF NOT EXISTS idx_rdm_date ON reward_daily_markets(date);

CREATE INDEX IF NOT EXISTS idx_fills_cid ON fills(condition_id);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS idx_unwinds_cid ON unwinds(condition_id);
CREATE INDEX IF NOT EXISTS idx_unwinds_ts ON unwinds(ts);
CREATE INDEX IF NOT EXISTS idx_cycle_ts ON cycle_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_cycle_cid_ts ON cycle_snapshots(condition_id, ts);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_pnl(date);
CREATE INDEX IF NOT EXISTS idx_hourly_ts ON hourly_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_msl_ts ON market_selection_log(ts);

CREATE TABLE IF NOT EXISTS calibration_model_state (
    model_name    TEXT PRIMARY KEY,
    weights_json  TEXT NOT NULL,
    trained_at    REAL NOT NULL,
    n_samples     INTEGER NOT NULL,
    n_positive    INTEGER NOT NULL DEFAULT 0,
    metrics_json  TEXT NOT NULL DEFAULT '{}',
    feature_names TEXT NOT NULL DEFAULT '[]'
);
"""


class BotDatabase:
    """Thread-safe SQLite database for bot history and analytics."""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local connection (SQLite connections are not thread-safe)."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self) -> None:
        """Create tables if they don't exist, and migrate existing tables."""
        try:
            conn = self._get_conn()
            conn.executescript(_SCHEMA)
            # Migrate: add M3 fill quality columns if missing
            self._migrate_fill_quality(conn)
            # Migrate: add enrichment columns for iteration data
            self._migrate_enrichment_columns(conn)
            conn.commit()
            log.info(f"Bot history database ready: {self._db_path}")
        except Exception as e:
            log.error(f"Failed to initialize database: {e}")

    def _migrate_fill_quality(self, conn: sqlite3.Connection) -> None:
        """Add midpoint and slippage columns to fills table if missing."""
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(fills)")}
            if "midpoint" not in cols:
                conn.execute("ALTER TABLE fills ADD COLUMN midpoint REAL NOT NULL DEFAULT 0")
                log.info("Migrated fills table: added 'midpoint' column")
            if "slippage" not in cols:
                conn.execute("ALTER TABLE fills ADD COLUMN slippage REAL NOT NULL DEFAULT 0")
                log.info("Migrated fills table: added 'slippage' column")
        except Exception as e:
            log.warning(f"Fill quality migration check: {e}")

    def _migrate_enrichment_columns(self, conn: sqlite3.Connection) -> None:
        """Add iteration-critical context columns to existing tables."""
        migrations = [
            # fills: order age, position after, reward rate at fill time
            ("fills", "order_age_secs", "REAL NOT NULL DEFAULT 0"),
            ("fills", "position_usd_after", "REAL NOT NULL DEFAULT 0"),
            ("fills", "reward_rate_hr", "REAL NOT NULL DEFAULT 0"),
            # unwinds: hold duration, fill type, reward earned during hold
            ("unwinds", "hold_duration_secs", "REAL NOT NULL DEFAULT 0"),
            ("unwinds", "unwind_type", "TEXT NOT NULL DEFAULT ''"),
            ("unwinds", "reward_earned_est", "REAL NOT NULL DEFAULT 0"),
            # orders_cancelled: market context
            ("orders_cancelled", "condition_id", "TEXT NOT NULL DEFAULT ''"),
            ("orders_cancelled", "side", "TEXT NOT NULL DEFAULT ''"),
            ("orders_cancelled", "price", "REAL NOT NULL DEFAULT 0"),
            ("orders_cancelled", "age_secs", "REAL NOT NULL DEFAULT 0"),
            # market_expiry_cache: game start time for sports markets (CLOB only)
            ("market_expiry_cache", "game_start_time", "TEXT NOT NULL DEFAULT ''"),
            # market_expiry_cache: question text from Gamma keyset (enables sports
            # protection, per-group cap, keyword filters that gate on m.question)
            ("market_expiry_cache", "question", "TEXT NOT NULL DEFAULT ''"),
        ]
        for table, col, typedef in migrations:
            try:
                existing = {row[1] for row in conn.execute(
                    f"PRAGMA table_info({table})"
                )}
                if col not in existing:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col} {typedef}"
                    )
                    log.info(f"Migrated {table}: added '{col}'")
            except Exception as e:
                log.warning(f"Migration {table}.{col}: {e}")

    # ── Logging Methods ───────────────────────────────────────────────────────

    def log_fill(
        self, condition_id: str, question: str, side: str,
        fill_type: str, shares: float, price: float,
        clob_cost: float, usd_value: float,
        midpoint: float = 0.0, slippage: float = 0.0,
        order_age_secs: float = 0.0, position_usd_after: float = 0.0,
        reward_rate_hr: float = 0.0,
    ) -> None:
        """Record a BUY fill with context for iteration analysis."""
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO fills (ts, condition_id, question, side, "
                "fill_type, shares, price, clob_cost, usd_value, "
                "midpoint, slippage, order_age_secs, position_usd_after, "
                "reward_rate_hr) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), condition_id, question, side,
                 fill_type, shares, price, clob_cost, usd_value,
                 midpoint, slippage, order_age_secs, position_usd_after,
                 reward_rate_hr),
            )
            conn.commit()
        except Exception as e:
            log.debug(f"DB log_fill error: {e}")

    def log_unwind(
        self, condition_id: str, question: str, side: str,
        shares: float, sell_price: float, usd_value: float,
        vwap_cost: float = 0.0,
        hold_duration_secs: float = 0.0, unwind_type: str = "",
        reward_earned_est: float = 0.0,
    ) -> None:
        """Record a SELL (unwind) fill with hold context."""
        pnl = usd_value - vwap_cost
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO unwinds (ts, condition_id, question, side, "
                "shares, sell_price, usd_value, vwap_cost, pnl, "
                "hold_duration_secs, unwind_type, reward_earned_est) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), condition_id, question, side,
                 shares, sell_price, usd_value, vwap_cost, pnl,
                 hold_duration_secs, unwind_type, reward_earned_est),
            )
            conn.commit()
        except Exception as e:
            log.debug(f"DB log_unwind error: {e}")

    def log_order_placed(
        self, condition_id: str, side: str, price: float,
        size: float, order_id: str = "", order_type: str = "BUY",
    ) -> None:
        """Record an order placement."""
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO orders_placed (ts, condition_id, side, price, "
                "size, order_id, order_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), condition_id, side, price, size,
                 order_id, order_type),
            )
            conn.commit()
        except Exception as e:
            log.debug(f"DB log_order_placed error: {e}")

    def log_order_cancelled(
        self, order_id: str, reason: str = "",
        condition_id: str = "", side: str = "",
        price: float = 0.0, age_secs: float = 0.0,
    ) -> None:
        """Record an order cancellation with market context."""
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO orders_cancelled (ts, order_id, reason, "
                "condition_id, side, price, age_secs) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), order_id, reason,
                 condition_id, side, price, age_secs),
            )
            conn.commit()
        except Exception as e:
            log.debug(f"DB log_order_cancelled error: {e}")

    def log_cycle_snapshot(
        self, cycle_num: int, condition_id: str,
        best_bid: float = 0, best_ask: float = 0,
        our_bid: float = 0, our_ask: float = 0,
        yes_position_usd: float = 0, no_position_usd: float = 0,
        active_orders: int = 0, unwind_orders: int = 0,
    ) -> None:
        """Record a cycle snapshot for one market."""
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO cycle_snapshots (ts, cycle_num, condition_id, "
                "best_bid, best_ask, our_bid, our_ask, "
                "yes_position_usd, no_position_usd, "
                "active_orders, unwind_orders) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), cycle_num, condition_id,
                 best_bid, best_ask, our_bid, our_ask,
                 yes_position_usd, no_position_usd,
                 active_orders, unwind_orders),
            )
            conn.commit()
        except Exception as e:
            log.debug(f"DB log_cycle_snapshot error: {e}")

    def log_merge(
        self, condition_id: str, shares: float, freed_usd: float,
    ) -> None:
        """Record a YES+NO merge."""
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO merges (ts, condition_id, shares, freed_usd) "
                "VALUES (?, ?, ?, ?)",
                (time.time(), condition_id, shares, freed_usd),
            )
            conn.commit()
        except Exception as e:
            log.debug(f"DB log_merge error: {e}")

    def log_stop_loss(
        self, condition_id: str, side: str, shares: float,
        cost_price: float, sell_price: float, loss_usd: float,
    ) -> None:
        """Record a stop-loss event."""
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO stop_losses (ts, condition_id, side, shares, "
                "cost_price, sell_price, loss_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), condition_id, side, shares,
                 cost_price, sell_price, loss_usd),
            )
            conn.commit()
        except Exception as e:
            log.debug(f"DB log_stop_loss error: {e}")

    def log_hourly_snapshot(
        self, hour_label: str, num_markets: int,
        total_bought_usd: float, total_sold_usd: float,
        realized_pnl: float, unrealized_pnl: float,
        total_position_usd: float, est_reward_usd: float,
        est_reward_rate_hr: float, num_fills: int, num_unwinds: int,
        num_stop_losses: int, num_danger_cancels: int,
        avg_uptime_pct: float, config_json: str = "{}",
    ) -> None:
        """Record an hourly P&L + reward snapshot for iteration analysis."""
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO hourly_snapshots (ts, hour_label, num_markets, "
                "total_bought_usd, total_sold_usd, realized_pnl, "
                "unrealized_pnl, total_position_usd, est_reward_usd, "
                "est_reward_rate_hr, num_fills, num_unwinds, "
                "num_stop_losses, num_danger_cancels, avg_uptime_pct, "
                "config_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), hour_label, num_markets,
                 total_bought_usd, total_sold_usd, realized_pnl,
                 unrealized_pnl, total_position_usd, est_reward_usd,
                 est_reward_rate_hr, num_fills, num_unwinds,
                 num_stop_losses, num_danger_cancels, avg_uptime_pct,
                 config_json),
            )
            conn.commit()
        except Exception as e:
            log.debug(f"DB log_hourly_snapshot error: {e}")

    def log_market_selection(
        self, condition_id: str, question: str, action: str,
        score: float = 0, daily_rate: float = 0,
        reason: str = "", volume_24h: float = 0,
        liquidity: float = 0,
    ) -> None:
        """Record a market selection decision for iteration analysis."""
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO market_selection_log (ts, condition_id, question, "
                "action, score, daily_rate, reason, volume_24h, liquidity) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), condition_id, question, action,
                 score, daily_rate, reason, volume_24h, liquidity),
            )
            conn.commit()
        except Exception as e:
            log.debug(f"DB log_market_selection error: {e}")

    def log_reward_comparison(
        self, condition_id: str = "",
        q_score_est: float = 0, legacy_est: float = 0,
        actual_earned: float = 0, q_share_pct: float = 0,
        our_q_score: float = 0, market_q_score: float = 0,
    ) -> None:
        """Record a reward estimate vs actual comparison snapshot."""
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO reward_comparisons (ts, condition_id, "
                "q_score_est, legacy_est, actual_earned, q_share_pct, "
                "our_q_score, market_q_score) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), condition_id, q_score_est, legacy_est,
                 actual_earned, q_share_pct, our_q_score, market_q_score),
            )
            conn.commit()
        except Exception as e:
            log.debug(f"DB log_reward_comparison error: {e}")

    def get_reward_accuracy_history(self, days: int = 7) -> list[dict]:
        """Get reward estimate vs actual history for variance analysis."""
        try:
            conn = self._get_conn()
            cutoff = time.time() - days * 86400
            rows = conn.execute(
                "SELECT * FROM reward_comparisons "
                "WHERE ts > ? AND condition_id = '' "
                "ORDER BY ts DESC",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            log.debug(f"DB get_reward_accuracy_history error: {e}")
            return []

    # ── Query Methods ─────────────────────────────────────────────────────────

    def get_daily_pnl(self, date: str = "") -> dict:
        """Get P&L summary for a specific date (default: today).

        Returns dict with keys: total_bought_usd, total_sold_usd,
        realized_pnl, num_fills, num_unwinds, etc.
        """
        if not date:
            date = time.strftime("%Y-%m-%d")
        try:
            conn = self._get_conn()
            # Calculate from raw events
            start_ts = time.mktime(time.strptime(date, "%Y-%m-%d"))
            end_ts = start_ts + 86400

            row = conn.execute(
                "SELECT COALESCE(SUM(usd_value), 0) as total, "
                "COUNT(*) as cnt FROM fills WHERE ts >= ? AND ts < ?",
                (start_ts, end_ts),
            ).fetchone()
            total_bought = row["total"]
            num_fills = row["cnt"]

            row = conn.execute(
                "SELECT COALESCE(SUM(usd_value), 0) as total, "
                "COALESCE(SUM(pnl), 0) as pnl, "
                "COUNT(*) as cnt FROM unwinds WHERE ts >= ? AND ts < ?",
                (start_ts, end_ts),
            ).fetchone()
            total_sold = row["total"]
            realized_pnl = row["pnl"]
            num_unwinds = row["cnt"]

            row = conn.execute(
                "SELECT COALESCE(SUM(freed_usd), 0) as total, "
                "COUNT(*) as cnt FROM merges WHERE ts >= ? AND ts < ?",
                (start_ts, end_ts),
            ).fetchone()
            total_merged = row["total"]
            num_merges = row["cnt"]

            row = conn.execute(
                "SELECT COUNT(*) as cnt, "
                "COALESCE(SUM(loss_usd), 0) as total "
                "FROM stop_losses WHERE ts >= ? AND ts < ?",
                (start_ts, end_ts),
            ).fetchone()
            num_stop_losses = row["cnt"]
            stop_loss_total = row["total"]

            return {
                "date": date,
                "total_bought_usd": round(total_bought, 2),
                "total_sold_usd": round(total_sold, 2),
                "total_merged_usd": round(total_merged, 2),
                "realized_pnl": round(realized_pnl, 2),
                "stop_loss_usd": round(stop_loss_total, 2),
                "num_fills": num_fills,
                "num_unwinds": num_unwinds,
                "num_merges": num_merges,
                "num_stop_losses": num_stop_losses,
            }
        except Exception as e:
            log.debug(f"DB get_daily_pnl error: {e}")
            return {}

    def get_position_history(
        self, condition_id: str, limit: int = 100
    ) -> list[dict]:
        """Get recent fills and unwinds for a market."""
        try:
            conn = self._get_conn()
            fills = conn.execute(
                "SELECT ts, 'BUY' as action, side, shares, clob_cost as price, "
                "usd_value FROM fills WHERE condition_id = ? "
                "ORDER BY ts DESC LIMIT ?",
                (condition_id, limit),
            ).fetchall()
            unwinds = conn.execute(
                "SELECT ts, 'SELL' as action, side, shares, sell_price as price, "
                "usd_value FROM unwinds WHERE condition_id = ? "
                "ORDER BY ts DESC LIMIT ?",
                (condition_id, limit),
            ).fetchall()
            combined = [dict(r) for r in fills] + [dict(r) for r in unwinds]
            combined.sort(key=lambda x: x["ts"], reverse=True)
            return combined[:limit]
        except Exception as e:
            log.debug(f"DB get_position_history error: {e}")
            return []

    def get_total_pnl(self) -> float:
        """Get all-time realized P&L from unwinds."""
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) as total FROM unwinds"
            ).fetchone()
            return round(row["total"], 2)
        except Exception as e:
            log.debug(f"DB get_total_pnl error: {e}")
            return 0.0

    # ── A3: Position Persistence (replaces positions.json) ──────────────────

    def save_position(
        self, condition_id: str, question: str,
        yes_shares: float, yes_avg_price: float, yes_halted: bool,
        no_shares: float, no_avg_price: float, no_halted: bool,
    ) -> None:
        """UPSERT a single market's position state."""
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO positions "
                "(condition_id, question, yes_shares, yes_avg_price, yes_halted, "
                "no_shares, no_avg_price, no_halted, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (condition_id, question, yes_shares, yes_avg_price,
                 int(yes_halted), no_shares, no_avg_price, int(no_halted),
                 time.time()),
            )
            conn.commit()
        except Exception as e:
            log.debug(f"DB save_position error: {e}")

    def save_all_positions(self, positions: dict) -> None:
        """Batch UPSERT all positions in a single transaction.

        Args:
            positions: Dict of {condition_id: {question, yes_shares, ...}}
                matching the format from PositionStore._save().
        """
        try:
            conn = self._get_conn()
            now = time.time()
            conn.execute("BEGIN")
            # Clear old positions not in current set
            if positions:
                placeholders = ",".join("?" * len(positions))
                conn.execute(
                    f"DELETE FROM positions WHERE condition_id NOT IN ({placeholders})",
                    list(positions.keys()),
                )
            else:
                conn.execute("DELETE FROM positions")
            for cid, pos in positions.items():
                conn.execute(
                    "INSERT OR REPLACE INTO positions "
                    "(condition_id, question, yes_shares, yes_avg_price, yes_halted, "
                    "no_shares, no_avg_price, no_halted, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (cid, pos.get("question", ""),
                     pos.get("yes_shares", 0), pos.get("yes_avg_price", 0),
                     int(pos.get("yes_halted", False)),
                     pos.get("no_shares", 0), pos.get("no_avg_price", 0),
                     int(pos.get("no_halted", False)),
                     now),
                )
            conn.commit()
        except Exception as e:
            log.debug(f"DB save_all_positions error: {e}")
            try:
                conn.rollback()
            except Exception:
                pass

    def load_all_positions(self) -> dict:
        """Load all positions from SQLite.

        Returns:
            Dict matching PositionStore's expected format, or empty dict.
        """
        try:
            conn = self._get_conn()
            rows = conn.execute("SELECT * FROM positions").fetchall()
            result = {}
            for r in rows:
                result[r["condition_id"]] = {
                    "question": r["question"],
                    "yes_shares": r["yes_shares"],
                    "yes_avg_price": r["yes_avg_price"],
                    "yes_halted": bool(r["yes_halted"]),
                    "no_shares": r["no_shares"],
                    "no_avg_price": r["no_avg_price"],
                    "no_halted": bool(r["no_halted"]),
                }
            return result
        except Exception as e:
            log.debug(f"DB load_all_positions error: {e}")
            return {}

    def delete_position(self, condition_id: str) -> None:
        """Remove a market's position."""
        try:
            conn = self._get_conn()
            conn.execute(
                "DELETE FROM positions WHERE condition_id = ?", (condition_id,)
            )
            conn.commit()
        except Exception as e:
            log.debug(f"DB delete_position error: {e}")

    # ── A3: Reward Tracker Persistence (replaces reward_history.json) ─────

    def save_reward_state(self, bot_start: float, last_hourly: float,
                          last_daily: float) -> None:
        """Save scalar reward tracker state."""
        try:
            conn = self._get_conn()
            for key, val in [("bot_start", bot_start),
                             ("last_hourly_log", last_hourly),
                             ("last_daily_report", last_daily)]:
                conn.execute(
                    "INSERT OR REPLACE INTO reward_tracker_state (key, value) "
                    "VALUES (?, ?)", (key, str(val)),
                )
            conn.commit()
        except Exception as e:
            log.debug(f"DB save_reward_state error: {e}")

    def load_reward_state(self) -> dict:
        """Load scalar reward tracker state."""
        try:
            conn = self._get_conn()
            rows = conn.execute("SELECT key, value FROM reward_tracker_state").fetchall()
            return {r["key"]: float(r["value"]) for r in rows}
        except Exception as e:
            log.debug(f"DB load_reward_state error: {e}")
            return {}

    def save_usdc_balance(self, balance: float) -> None:
        """Save USDC balance from exchange (for oversight agent to read)."""
        try:
            conn = self._get_conn()
            now = time.time()
            conn.execute(
                "INSERT OR REPLACE INTO reward_tracker_state (key, value) "
                "VALUES (?, ?)", ("usdc_balance", str(balance)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO reward_tracker_state (key, value) "
                "VALUES (?, ?)", ("usdc_balance_at", str(now)),
            )
            conn.commit()
        except Exception as e:
            log.debug(f"DB save_usdc_balance error: {e}")

    def load_usdc_balance(self) -> tuple[float | None, float]:
        """Load USDC balance written by bot. Returns (balance, timestamp)."""
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT value FROM reward_tracker_state WHERE key = 'usdc_balance'"
            ).fetchone()
            ts_row = conn.execute(
                "SELECT value FROM reward_tracker_state WHERE key = 'usdc_balance_at'"
            ).fetchone()
            if row:
                return float(row["value"]), float(ts_row["value"]) if ts_row else 0.0
            return None, 0.0
        except Exception as e:
            log.debug(f"DB load_usdc_balance error: {e}")
            return None, 0.0

    def save_all_reward_stats(self, markets: dict) -> None:
        """Batch UPSERT all market stats as JSON blobs.

        Args:
            markets: Dict of {condition_id: MarketStats dataclass}.
        """
        import json as _json
        from dataclasses import asdict
        try:
            conn = self._get_conn()
            now = time.time()
            conn.execute("BEGIN")
            if markets:
                placeholders = ",".join("?" * len(markets))
                conn.execute(
                    f"DELETE FROM reward_market_stats "
                    f"WHERE condition_id NOT IN ({placeholders})",
                    list(markets.keys()),
                )
            else:
                conn.execute("DELETE FROM reward_market_stats")
            for cid, stats in markets.items():
                d = asdict(stats) if hasattr(stats, "__dataclass_fields__") else stats
                conn.execute(
                    "INSERT OR REPLACE INTO reward_market_stats "
                    "(condition_id, data, updated_at) VALUES (?, ?, ?)",
                    (cid, _json.dumps(d), now),
                )
            conn.commit()
        except Exception as e:
            log.debug(f"DB save_all_reward_stats error: {e}")
            try:
                conn.rollback()
            except Exception:
                pass

    def load_all_reward_stats(self) -> dict:
        """Load all reward market stats from SQLite."""
        import json as _json
        try:
            conn = self._get_conn()
            rows = conn.execute("SELECT condition_id, data FROM reward_market_stats").fetchall()
            return {r["condition_id"]: _json.loads(r["data"]) for r in rows}
        except Exception as e:
            log.debug(f"DB load_all_reward_stats error: {e}")
            return {}

    # ── A3: Backtest query methods ─────────────────────────────────────────

    def get_cycle_snapshots(
        self, condition_id: str = "", start_ts: float = 0,
        end_ts: float = 0,
    ) -> list[dict]:
        """Get cycle snapshots for backtesting."""
        try:
            conn = self._get_conn()
            if not end_ts:
                end_ts = time.time()
            if condition_id:
                rows = conn.execute(
                    "SELECT * FROM cycle_snapshots "
                    "WHERE condition_id = ? AND ts >= ? AND ts <= ? "
                    "ORDER BY ts", (condition_id, start_ts, end_ts),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM cycle_snapshots "
                    "WHERE ts >= ? AND ts <= ? ORDER BY ts",
                    (start_ts, end_ts),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            log.debug(f"DB get_cycle_snapshots error: {e}")
            return []

    def get_fills_in_range(
        self, condition_id: str = "", start_ts: float = 0,
        end_ts: float = 0,
    ) -> list[dict]:
        """Get fills for backtesting."""
        try:
            conn = self._get_conn()
            if not end_ts:
                end_ts = time.time()
            if condition_id:
                rows = conn.execute(
                    "SELECT * FROM fills WHERE condition_id = ? "
                    "AND ts >= ? AND ts <= ? ORDER BY ts",
                    (condition_id, start_ts, end_ts),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM fills WHERE ts >= ? AND ts <= ? ORDER BY ts",
                    (start_ts, end_ts),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            log.debug(f"DB get_fills_in_range error: {e}")
            return []

    def get_available_backtest_markets(
        self, start_ts: float = 0, end_ts: float = 0, min_cycles: int = 100,
    ) -> list[dict]:
        """List markets with enough cycle data for backtesting."""
        try:
            conn = self._get_conn()
            if not end_ts:
                end_ts = time.time()
            rows = conn.execute(
                "SELECT condition_id, COUNT(*) as cycles, "
                "MIN(ts) as first_ts, MAX(ts) as last_ts "
                "FROM cycle_snapshots "
                "WHERE ts >= ? AND ts <= ? "
                "GROUP BY condition_id HAVING cycles >= ? "
                "ORDER BY cycles DESC",
                (start_ts, end_ts, min_cycles),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            log.debug(f"DB get_available_backtest_markets error: {e}")
            return []

    def get_fill_quality(self, days: int = 7) -> dict:
        """Get fill quality stats for the given time period (M3).

        Returns dict with:
          - total_fills: number of fills with midpoint data
          - avg_slippage: average slippage per fill (>0 = adverse)
          - adverse_pct: percentage of fills that were adverse
          - total_adverse_usd: total USD lost to adverse selection
          - per_market: dict of condition_id → {fills, avg_slippage, adverse_pct}
        """
        try:
            conn = self._get_conn()
            cutoff = time.time() - days * 86400

            # Aggregate stats (only rows with midpoint data)
            row = conn.execute(
                "SELECT COUNT(*) as cnt, "
                "AVG(slippage) as avg_slip, "
                "SUM(CASE WHEN slippage > 0 THEN 1 ELSE 0 END) as adverse_cnt, "
                "SUM(CASE WHEN slippage > 0 THEN slippage * shares ELSE 0 END) as adverse_usd "
                "FROM fills WHERE ts > ? AND midpoint > 0",
                (cutoff,),
            ).fetchone()

            total_fills = row["cnt"] or 0
            result = {
                "total_fills": total_fills,
                "avg_slippage": round(row["avg_slip"] or 0, 6),
                "adverse_pct": round(
                    (row["adverse_cnt"] or 0) / total_fills * 100, 1
                ) if total_fills > 0 else 0,
                "total_adverse_usd": round(row["adverse_usd"] or 0, 2),
            }

            # Per-market breakdown
            rows = conn.execute(
                "SELECT condition_id, question, "
                "COUNT(*) as cnt, "
                "AVG(slippage) as avg_slip, "
                "SUM(CASE WHEN slippage > 0 THEN 1 ELSE 0 END) as adverse_cnt "
                "FROM fills WHERE ts > ? AND midpoint > 0 "
                "GROUP BY condition_id "
                "ORDER BY avg_slip DESC",
                (cutoff,),
            ).fetchall()
            result["per_market"] = [
                {
                    "condition_id": r["condition_id"],
                    "question": r["question"],
                    "fills": r["cnt"],
                    "avg_slippage": round(r["avg_slip"] or 0, 6),
                    "adverse_pct": round(
                        (r["adverse_cnt"] or 0) / r["cnt"] * 100, 1
                    ) if r["cnt"] > 0 else 0,
                }
                for r in rows
            ]
            return result

        except Exception as e:
            log.debug(f"DB get_fill_quality error: {e}")
            return {"total_fills": 0, "avg_slippage": 0, "adverse_pct": 0,
                    "total_adverse_usd": 0, "per_market": []}

    # ── Placement Feedback (bot → agent closed loop) ──────────────────

    def write_placement_feedback(self, condition_id: str, side: str,
                                  status: str, reason: str = "") -> None:
        """Write placement outcome for agent feedback. Upserts per (cid, side)."""
        try:
            self._get_conn().execute(
                """INSERT OR REPLACE INTO placement_feedback
                   (ts, condition_id, side, status, reason)
                   VALUES (?, ?, ?, ?, ?)""",
                (time.time(), condition_id, side, status, reason),
            )
            self._get_conn().commit()
        except Exception as e:
            log.debug(f"write_placement_feedback error: {e}")

    def query_all_placement_feedback(self) -> dict[str, dict]:
        """Read all placement feedback. Returns {condition_id: {"yes": {status, reason, ts}, "no": ...}}."""
        try:
            rows = self._get_conn().execute("SELECT * FROM placement_feedback").fetchall()
            result: dict[str, dict] = {}
            for r in rows:
                cid = r["condition_id"]
                if cid not in result:
                    result[cid] = {}
                result[cid][r["side"]] = {
                    "status": r["status"],
                    "reason": r["reason"],
                    "ts": r["ts"],
                }
            return result
        except Exception as e:
            log.debug(f"query_all_placement_feedback error: {e}")
            return {}

    # ── Market Performance Tracking (adaptive agent) ──────────────────

    def save_performance_snapshot(self, snapshot: dict) -> None:
        """Write a single market performance snapshot."""
        try:
            self._get_conn().execute(
                """INSERT INTO market_performance
                   (ts, condition_id, question, window_hours, estimated_daily,
                    correction_factor, corrected_daily, fill_cost, dump_revenue,
                    net_score, action, q_share_pct, on_book_hours, fill_count,
                    shares_recommended)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (snapshot["ts"], snapshot["condition_id"], snapshot.get("question", ""),
                 snapshot.get("window_hours", 24), snapshot.get("estimated_daily", 0),
                 snapshot.get("correction_factor", 1.0), snapshot.get("corrected_daily", 0),
                 snapshot.get("fill_cost", 0), snapshot.get("dump_revenue", 0),
                 snapshot.get("net_score", 0), snapshot.get("action", "deploy"),
                 snapshot.get("q_share_pct", 0), snapshot.get("on_book_hours", 0),
                 snapshot.get("fill_count", 0), snapshot.get("shares_recommended", 50)),
            )
            self._get_conn().commit()
        except Exception as e:
            log.debug(f"save_performance_snapshot error: {e}")

    def save_performance_batch(self, snapshots: list[dict]) -> None:
        """Write multiple performance snapshots in one transaction."""
        if not snapshots:
            return
        try:
            conn = self._get_conn()
            conn.executemany(
                """INSERT INTO market_performance
                   (ts, condition_id, question, window_hours, estimated_daily,
                    correction_factor, corrected_daily, fill_cost, dump_revenue,
                    net_score, action, q_share_pct, on_book_hours, fill_count,
                    shares_recommended)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(s["ts"], s["condition_id"], s.get("question", ""),
                  s.get("window_hours", 24), s.get("estimated_daily", 0),
                  s.get("correction_factor", 1.0), s.get("corrected_daily", 0),
                  s.get("fill_cost", 0), s.get("dump_revenue", 0),
                  s.get("net_score", 0), s.get("action", "deploy"),
                  s.get("q_share_pct", 0), s.get("on_book_hours", 0),
                  s.get("fill_count", 0), s.get("shares_recommended", 50))
                 for s in snapshots],
            )
            conn.commit()
            log.debug(f"Saved {len(snapshots)} performance snapshots")
        except Exception as e:
            log.debug(f"save_performance_batch error: {e}")

    def get_market_performance_history(
        self, condition_id: str, days: int = 7
    ) -> list[dict]:
        """Get performance history for a specific market over N days."""
        cutoff = time.time() - days * 86400
        try:
            rows = self._get_conn().execute(
                """SELECT * FROM market_performance
                   WHERE condition_id = ? AND ts > ?
                   ORDER BY ts DESC""",
                (condition_id, cutoff),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            log.debug(f"get_market_performance_history error: {e}")
            return []

    def get_performance_summary(self, days: int = 7) -> dict:
        """Get aggregate performance summary across all markets."""
        cutoff = time.time() - days * 86400
        try:
            row = self._get_conn().execute(
                """SELECT
                    COUNT(DISTINCT condition_id) as unique_markets,
                    COUNT(*) as total_snapshots,
                    AVG(correction_factor) as avg_correction,
                    SUM(CASE WHEN action='deploy' THEN 1 ELSE 0 END) as deploy_decisions,
                    SUM(CASE WHEN action='avoid' THEN 1 ELSE 0 END) as avoid_decisions,
                    AVG(net_score) as avg_score,
                    SUM(fill_cost) as total_fill_cost,
                    SUM(dump_revenue) as total_dump_revenue
                   FROM market_performance WHERE ts > ?""",
                (cutoff,),
            ).fetchone()
            return dict(row) if row else {}
        except Exception as e:
            log.debug(f"get_performance_summary error: {e}")
            return {}

    # ── Dump State Persistence (crash recovery) ───────────────────────

    def save_dump_state(self, condition_id: str, side: str, state: dict) -> None:
        """Persist a dump state for crash recovery."""
        try:
            self._get_conn().execute(
                """INSERT OR REPLACE INTO dump_states
                   (condition_id, side, fill_price, started_at, shares, tid,
                    dump_order_id, last_passive_reprice)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (condition_id, side, state["fill_price"], state["started_at"],
                 state["shares"], state["tid"],
                 state.get("dump_order_id", ""),
                 state.get("last_passive_reprice", 0)),
            )
            self._get_conn().commit()
        except Exception as e:
            log.debug(f"save_dump_state error: {e}")

    def load_all_dump_states(self) -> dict[str, dict]:
        """Load all saved dump states. Returns {(cid, side): state_dict}."""
        try:
            rows = self._get_conn().execute("SELECT * FROM dump_states").fetchall()
            result = {}
            for r in rows:
                key = (r["condition_id"], r["side"])
                result[key] = {
                    "fill_price": r["fill_price"],
                    "started_at": r["started_at"],
                    "shares": r["shares"],
                    "tid": r["tid"],
                    "dump_order_id": r["dump_order_id"],
                    "last_passive_reprice": r["last_passive_reprice"],
                }
            return result
        except Exception as e:
            log.debug(f"load_all_dump_states error: {e}")
            return {}

    def delete_dump_state(self, condition_id: str, side: str) -> None:
        """Remove a dump state after dump completes or is abandoned."""
        try:
            self._get_conn().execute(
                "DELETE FROM dump_states WHERE condition_id = ? AND side = ?",
                (condition_id, side),
            )
            self._get_conn().commit()
        except Exception as e:
            log.debug(f"delete_dump_state error: {e}")

    # ── Active Order Persistence (crash recovery) ────────────────────

    def save_active_order(self, order_id: str, condition_id: str, side: str,
                          order_type: str, price: float, shares: float) -> None:
        """Persist an active order for crash recovery."""
        try:
            self._get_conn().execute(
                """INSERT OR REPLACE INTO active_orders
                   (order_id, condition_id, side, order_type, price, shares, placed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (order_id, condition_id, side, order_type, price, shares, time.time()),
            )
            self._get_conn().commit()
        except Exception as e:
            log.debug(f"save_active_order error: {e}")

    def load_active_orders(self) -> list[dict]:
        """Load all saved active orders."""
        try:
            rows = self._get_conn().execute("SELECT * FROM active_orders").fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            log.debug(f"load_active_orders error: {e}")
            return []

    def delete_active_order(self, order_id: str) -> None:
        """Remove an active order after it fills or is cancelled."""
        try:
            self._get_conn().execute(
                "DELETE FROM active_orders WHERE order_id = ?", (order_id,)
            )
            self._get_conn().commit()
        except Exception as e:
            log.debug(f"delete_active_order error: {e}")

    def clear_all_active_orders(self) -> None:
        """Clear all active orders (called on clean startup)."""
        try:
            self._get_conn().execute("DELETE FROM active_orders")
            self._get_conn().execute("DELETE FROM dump_states")
            self._get_conn().commit()
        except Exception as e:
            log.debug(f"clear_all_active_orders error: {e}")

    def purge_all_active_orders(self) -> int:
        """Purge all active orders and dump states. Returns count deleted."""
        try:
            conn = self._get_conn()
            count = conn.execute("SELECT COUNT(*) FROM active_orders").fetchone()[0]
            conn.execute("DELETE FROM active_orders")
            conn.execute("DELETE FROM dump_states")
            conn.commit()
            return count
        except Exception as e:
            log.debug(f"purge_all_active_orders error: {e}")
            return 0

    # ── Phase 0: Data Collection Methods ──────────────────────────────────

    def log_book_snapshot(
        self, condition_id: str, merged: dict, best_bid: float,
        best_ask: float, midpoint: float, our_bid: float, our_ask: float,
        daily_rate: float, max_spread: float, agent_shares: float,
    ) -> None:
        """Record order book summary features for ML training."""
        try:
            bids = merged.get("bids", [])
            asks = merged.get("asks", [])
            spread = best_ask - best_bid

            bid_depth_5c = sum(float(b["size"]) for b in bids if float(b["price"]) >= midpoint - 0.05)
            ask_depth_5c = sum(float(a["size"]) for a in asks if float(a["price"]) <= midpoint + 0.05)
            bid_depth_10c = sum(float(b["size"]) for b in bids if float(b["price"]) >= midpoint - 0.10)
            ask_depth_10c = sum(float(a["size"]) for a in asks if float(a["price"]) <= midpoint + 0.10)
            total_bid = sum(float(b["size"]) for b in bids)
            total_ask = sum(float(a["size"]) for a in asks)

            our_bid_ahead = sum(float(b["size"]) for b in bids if float(b["price"]) > our_bid) if our_bid > 0 else 0
            our_ask_ahead = sum(float(a["size"]) for a in asks if float(a["price"]) < our_ask) if our_ask > 0 else 0

            conn = self._get_conn()
            conn.execute(
                "INSERT INTO book_snapshots "
                "(ts, condition_id, best_bid, best_ask, midpoint, spread, "
                "bid_depth_5c, ask_depth_5c, bid_depth_10c, ask_depth_10c, "
                "total_bid_depth, total_ask_depth, num_bid_levels, num_ask_levels, "
                "our_bid_price, our_ask_price, our_bid_depth_ahead, our_ask_depth_ahead, "
                "daily_rate, max_spread, agent_shares) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), condition_id, best_bid, best_ask, midpoint, spread,
                 bid_depth_5c, ask_depth_5c, bid_depth_10c, ask_depth_10c,
                 total_bid, total_ask, len(bids), len(asks),
                 our_bid, our_ask, our_bid_ahead, our_ask_ahead,
                 daily_rate, max_spread, agent_shares),
            )
            conn.commit()
        except Exception as e:
            log.debug(f"DB log_book_snapshot error: {e}")

    def log_scoring_snapshot(self, scoring_data: list[tuple]) -> None:
        """Batch-insert scoring status for all orders.

        Args:
            scoring_data: list of (order_id, condition_id, side, scoring_bool, price, shares)
        """
        if not scoring_data:
            return
        try:
            now = time.time()
            conn = self._get_conn()
            conn.executemany(
                "INSERT INTO scoring_snapshots "
                "(ts, order_id, condition_id, side, scoring, price, shares) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(now, oid, cid, side, int(scoring), price, shares)
                 for oid, cid, side, scoring, price, shares in scoring_data],
            )
            conn.commit()
        except Exception as e:
            log.debug(f"DB log_scoring_snapshot error: {e}")

    def prune_phase0_data(self, retention_days: int = 7) -> int:
        """Delete book_snapshots and scoring_snapshots older than retention window."""
        cutoff = time.time() - (retention_days * 86400)
        try:
            conn = self._get_conn()
            d1 = conn.execute("DELETE FROM book_snapshots WHERE ts < ?", (cutoff,)).rowcount
            d2 = conn.execute("DELETE FROM scoring_snapshots WHERE ts < ?", (cutoff,)).rowcount
            conn.commit()
            total = d1 + d2
            if total:
                log.info(f"Phase0 pruning: removed {d1} book + {d2} scoring snapshots (>{retention_days}d)")
            return total
        except Exception as e:
            log.debug(f"DB prune_phase0_data error: {e}")
            return 0

    def close(self) -> None:
        """Close the thread-local connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: BotDatabase | None = None
_instance_lock = threading.Lock()


def get_db() -> BotDatabase:
    """Get the singleton BotDatabase instance."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = BotDatabase()
    return _instance
