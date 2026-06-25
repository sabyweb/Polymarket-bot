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

-- A/B cohort P&L snapshots. Produced by ab_cohort_pnl.py each oversight cycle.
-- One row per cohort per (window_end_ts, cohort_count). Cohorts are defined by
-- the candidate_features table. cohort_count records the experiment generation
-- (e.g. 2 for the old 2-cohort run, 3 for the new 3-cohort run) so historical
-- rows remain interpretable after cohort reassignment.
CREATE TABLE IF NOT EXISTS cohort_pnl (
    ts                REAL NOT NULL,          -- when this row was computed
    window_start_ts   REAL NOT NULL,          -- start of the rolling window
    window_end_ts     REAL NOT NULL,          -- end of the rolling window
    cohort            INTEGER NOT NULL,       -- cohort id under the experiment generation
    cohort_count      INTEGER NOT NULL DEFAULT 2, -- RF_AB_COHORT_COUNT at compute time
    reward_earned     REAL NOT NULL DEFAULT 0,
    unwind_pnl        REAL NOT NULL DEFAULT 0,
    net_pnl           REAL NOT NULL DEFAULT 0,  -- reward_earned + unwind_pnl
    fill_count        INTEGER NOT NULL DEFAULT 0,
    filled_markets    INTEGER NOT NULL DEFAULT 0,
    shares_filled     REAL NOT NULL DEFAULT 0,
    gross_fill_cost   REAL NOT NULL DEFAULT 0,
    total_slippage    REAL NOT NULL DEFAULT 0,
    avg_fill_age_secs REAL NOT NULL DEFAULT 0,
    avg_slippage      REAL NOT NULL DEFAULT 0,
    deployed_markets  INTEGER NOT NULL DEFAULT 0,
    target_capital    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (window_end_ts, cohort, cohort_count)
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

-- B-5: Position correction audit trail. Captures every set_shares/reset_side
-- mutation so P&L, kill-switch, and post-incident analysis can see write-downs.
CREATE TABLE IF NOT EXISTS position_corrections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    condition_id    TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    old_shares      REAL    NOT NULL,
    new_shares      REAL    NOT NULL,
    old_avg_price   REAL    NOT NULL DEFAULT 0,
    new_avg_price   REAL    NOT NULL DEFAULT 0,
    reason          TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pc_cid_ts ON position_corrections(condition_id, ts);

-- Oversight/farmer portfolio value history (used for drawdown and peak tracking)
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL    NOT NULL,
    total_value      REAL    NOT NULL,
    exchange_balance REAL    NOT NULL DEFAULT 0,
    locked_capital   REAL    NOT NULL DEFAULT 0,
    peak_value       REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ps_ts ON portfolio_snapshots(ts);

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

-- Markets the bot has definitively confirmed are dead at the orderbook level
-- (create_and_post_order returned "orderbook does not exist"). Once a cid
-- lands here, every order path skips it until the periodic re-probe finds the
-- orderbook alive again (FX-007 / FX-005 / FX-006 / FX-008 / FX-009 / FX-028).
CREATE TABLE IF NOT EXISTS unliquidatable_markets (
    condition_id  TEXT PRIMARY KEY,
    reason        TEXT NOT NULL DEFAULT '',
    marked_at     REAL NOT NULL,
    last_retry_at REAL NOT NULL DEFAULT 0
);

-- FX-049: Wallet-invariant reconciliation history. Defense-in-depth backstop
-- against any cash-accounting drift between bot DB and on-chain wallet. Each
-- row captures one reconcile event: the bot's expected vs actual wallet pUSD
-- and the divergence at that moment. The LATEST row's wallet snapshot serves
-- as the BASELINE for the next reconcile cycle — divergences are computed
-- incrementally over the window since the prior baseline (rolling
-- comparison, not all-time).
--
-- Status values:
--   'baseline'   — first reconcile run (no prior row); just snapshotted
--   'ok'         — |divergence| ≤ threshold
--   'desync'     — |divergence| > threshold; [CRITICAL] WALLET_DESYNC fired
--   'fail_open'  — data fetch error; preserved row but no alert
CREATE TABLE IF NOT EXISTS wallet_reconcile_history (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 REAL NOT NULL,
    actual_wallet      REAL NOT NULL,
    expected_wallet    REAL NOT NULL,
    divergence         REAL NOT NULL,
    status             TEXT NOT NULL DEFAULT 'ok',
    baseline_ts        REAL NOT NULL,
    baseline_wallet    REAL NOT NULL,
    fills_delta        REAL NOT NULL DEFAULT 0,
    unwinds_delta      REAL NOT NULL DEFAULT 0,
    rewards_delta      REAL NOT NULL DEFAULT 0
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

-- FX-051: Per-market rolling ROI snapshots (Ground Rule 3 foundation).
-- One row per (condition_id, window). Upserted on every oversight cycle by
-- MarketROITracker.tick(). Consumed by DecisionPolicy + SimpleAllocator's
-- excluded_cids filter.
CREATE TABLE IF NOT EXISTS market_roi (
    condition_id          TEXT NOT NULL,
    window                TEXT NOT NULL,           -- '1h' | '24h' | '7d'
    window_end_ts         REAL NOT NULL,
    reward_earned         REAL NOT NULL DEFAULT 0, -- best-effort from API/cache
    fill_loss             REAL NOT NULL DEFAULT 0, -- SUM(-pnl) from unwinds, pnl<0
    capital_committed_avg REAL NOT NULL DEFAULT 0, -- time-weighted in window
    roi                   REAL NOT NULL DEFAULT 0,
    fill_count            INTEGER NOT NULL DEFAULT 0,
    fill_rate_per_hour    REAL NOT NULL DEFAULT 0,
    samples               INTEGER NOT NULL DEFAULT 0,
    last_updated          REAL NOT NULL,
    PRIMARY KEY (condition_id, window)
);
CREATE INDEX IF NOT EXISTS idx_market_roi_window ON market_roi(window);
CREATE INDEX IF NOT EXISTS idx_market_roi_updated ON market_roi(last_updated);

-- FX-051: Per-cycle capital allocation snapshots. One row per (cycle, deploy).
-- Time-integrated by the tracker to compute capital_committed_avg.
-- Pruned to last ~14 days by MarketROITracker.prune_old_snapshots().
CREATE TABLE IF NOT EXISTS capital_committed_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    condition_id    TEXT NOT NULL,
    est_capital_cost REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ccs_cid_ts ON capital_committed_snapshots(condition_id, ts);
CREATE INDEX IF NOT EXISTS idx_ccs_ts ON capital_committed_snapshots(ts);

-- FX-051: Per-market cooldown state. DecisionPolicy inserts a row when a
-- market's recent ROI breaches the cooldown threshold; the row is removed
-- (or just ignored once `cooldown_until` passes) on reactivation.
CREATE TABLE IF NOT EXISTS market_cooldowns (
    condition_id        TEXT PRIMARY KEY,
    cooled_at           REAL NOT NULL,
    cooldown_until      REAL NOT NULL,
    reason              TEXT NOT NULL,
    roi_at_cooldown     REAL NOT NULL DEFAULT 0,
    fill_loss_at_cooldown REAL NOT NULL DEFAULT 0,
    samples_at_cooldown INTEGER NOT NULL DEFAULT 0,
    cooldown_generation INTEGER NOT NULL DEFAULT 1,
    lifetime_cool_count INTEGER NOT NULL DEFAULT 1,
    chronic_blocked     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_market_cooldowns_until ON market_cooldowns(cooldown_until);

-- FX-051: Daily reward cache. /rewards/user/markets API returns per-market
-- per-date reward totals; we cache to avoid refetching within one cycle.
-- Keyed by (date, condition_id); date is UTC YYYY-MM-DD.
CREATE TABLE IF NOT EXISTS daily_reward_cache (
    date            TEXT NOT NULL,
    condition_id    TEXT NOT NULL,
    reward_earned   REAL NOT NULL DEFAULT 0,
    fetched_at      REAL NOT NULL,
    PRIMARY KEY (date, condition_id)
);
CREATE INDEX IF NOT EXISTS idx_drc_date ON daily_reward_cache(date);

-- 24h CLOB volume cache. Populated by volume_cache.py from Gamma via CLOB slug.
-- Used by SimpleAllocator for the C1 volume cap and candidate feature logging.
CREATE TABLE IF NOT EXISTS volume_24h_cache (
    condition_id   TEXT PRIMARY KEY,
    slug           TEXT,
    volume_24h     REAL NOT NULL DEFAULT 0,
    fetched_at     REAL NOT NULL,
    source         TEXT NOT NULL DEFAULT 'gamma'
);
CREATE INDEX IF NOT EXISTS idx_v24_fetched_at ON volume_24h_cache(fetched_at);

-- FX-061 (P11 of 9/10 plan): q_share recalibration events. When the API
-- q_share for a held market diverges from the cumulative DB ratio by
-- more than RF_QSHARE_DIVERGENCE_RATIO, decision_policy inserts a row
-- here for audit trail. Allocator reads recent events (last 24h) and
-- adds the cid to its `q_share_distrust_cids` set so non-API q_share
-- estimates for that cid get an extra 0.5× factor.
--
-- This implements the ground_rules.md §3 trigger #6 action: "Update
-- bot's per-market q_share to API value; recalibrate scoring". The
-- API-value-update part is already automatic via Priority 0 in
-- estimate_q_share. This table operationalizes the "recalibrate scoring"
-- part — when API and cumulative disagree, we trust API while held
-- AND remember to distrust cumulative for that cid going forward.
--
-- One row per detection event (not per cid). cid appears multiple times
-- if divergence persists across cycles. Pruned to last 7 days by
-- tracker.prune_old_snapshots() (same retention as capital snapshots).
CREATE TABLE IF NOT EXISTS q_share_recalibration_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    condition_id    TEXT NOT NULL,
    api_q_share     REAL NOT NULL,
    cumulative_q_share REAL NOT NULL,
    divergence_ratio REAL NOT NULL  -- max/min(api, cumul); always >= 1.0
);
CREATE INDEX IF NOT EXISTS idx_qsre_cid_ts ON q_share_recalibration_events(condition_id, ts);
CREATE INDEX IF NOT EXISTS idx_qsre_ts ON q_share_recalibration_events(ts);

-- B-3: Persistent kill-switch sentinel. Single-row table (id=1).
CREATE TABLE IF NOT EXISTS kill_state (
    id            INTEGER PRIMARY KEY,
    active        INTEGER NOT NULL DEFAULT 0,
    reason        TEXT    NOT NULL DEFAULT '',
    triggered_at  REAL    NOT NULL DEFAULT 0,
    updated_at    REAL    NOT NULL DEFAULT 0
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

    def _rollback_quiet(self) -> None:
        """FX-080: roll back the thread-local connection's pending transaction so
        a FAILED write never leaves an open transaction behind.

        An un-rolled-back transaction holds the WAL write lock — every other
        writer then gets ``database is locked`` (busy_timeout exhausted), and WAL
        checkpointing stalls (unbounded WAL growth). That was the oversight-process
        wedge that froze ``wallet_reconcile_history`` and made the ROI cache upsert
        fail every cycle. Call from any write method's ``except`` block; it is a
        no-op when there is nothing to roll back (and never raises)."""
        try:
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.rollback()
        except Exception:
            pass

    def _init_schema(self) -> None:
        """Create tables if they don't exist, and migrate existing tables."""
        try:
            conn = self._get_conn()
            conn.executescript(_SCHEMA)
            # Migrate: add M3 fill quality columns if missing
            self._migrate_fill_quality(conn)
            # Migrate: add enrichment columns for iteration data
            self._migrate_enrichment_columns(conn)
            self._migrate_cooldown_escalation(conn)
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

    def _migrate_cooldown_escalation(self, conn: sqlite3.Connection) -> None:
        """FX-097: escalating cooldown columns on market_cooldowns."""
        for col, typedef in (
            ("cooldown_generation", "INTEGER NOT NULL DEFAULT 1"),
            ("lifetime_cool_count", "INTEGER NOT NULL DEFAULT 1"),
            ("chronic_blocked", "INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                existing = {row[1] for row in conn.execute(
                    "PRAGMA table_info(market_cooldowns)"
                )}
                if col not in existing:
                    conn.execute(
                        f"ALTER TABLE market_cooldowns ADD COLUMN {col} {typedef}"
                    )
                    log.info(f"Migrated market_cooldowns: added '{col}'")
            except Exception as e:
                log.warning(f"Migration market_cooldowns.{col}: {e}")

    def _migrate_enrichment_columns(self, conn: sqlite3.Connection) -> None:
        """Add iteration-critical context columns to existing tables."""
        migrations = [
            # fills: order age, position after, reward rate at fill time
            ("fills", "order_age_secs", "REAL NOT NULL DEFAULT 0"),
            ("fills", "position_usd_after", "REAL NOT NULL DEFAULT 0"),
            ("fills", "reward_rate_hr", "REAL NOT NULL DEFAULT 0"),
            # FX-054: fill provenance + idempotency. order_id is the CLOB
            # order that produced this fill (empty for reconcile-after-
            # unknown / drift-sweep fills where no specific order is known).
            # fill_event_id is a caller-supplied dedup key — same value
            # across retries of the same logical fill so INSERT OR IGNORE
            # collapses them into one row. Empty string means "no dedup"
            # (legacy callers + tests that don't care). The unique partial
            # index below enforces dedup only on non-empty values so
            # multiple legacy '' rows stay legal.
            ("fills", "order_id", "TEXT NOT NULL DEFAULT ''"),
            ("fills", "fill_event_id", "TEXT NOT NULL DEFAULT ''"),
            # unwinds: hold duration, fill type, reward earned during hold
            ("unwinds", "hold_duration_secs", "REAL NOT NULL DEFAULT 0"),
            ("unwinds", "unwind_type", "TEXT NOT NULL DEFAULT ''"),
            ("unwinds", "reward_earned_est", "REAL NOT NULL DEFAULT 0"),
            # FX-067: idempotency key, mirrors fills.fill_event_id (FX-054).
            # Non-empty value dedups via the partial unique index below;
            # empty '' keeps append-only semantics for legacy callers.
            ("unwinds", "unwind_event_id", "TEXT NOT NULL DEFAULT ''"),
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

        # FX-054: partial unique index on fill_event_id. Enforces idempotency
        # ONLY for non-empty event ids so legacy '' rows and tests that
        # don't supply an event id remain insertable.
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_event_id "
                "ON fills(fill_event_id) WHERE fill_event_id != ''"
            )
        except Exception as e:
            log.warning(f"Migration fills idx_fills_event_id: {e}")

        # FX-067: partial unique index on unwind_event_id (mirrors FX-054's
        # fills index). Enforces idempotency ONLY for non-empty ids so legacy
        # '' rows + tests stay insertable.
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_unwinds_event_id "
                "ON unwinds(unwind_event_id) WHERE unwind_event_id != ''"
            )
        except Exception as e:
            log.warning(f"Migration unwinds idx_unwinds_event_id: {e}")

    # ── Logging Methods ───────────────────────────────────────────────────────

    def log_fill(
        self, condition_id: str, question: str, side: str,
        fill_type: str, shares: float, price: float,
        clob_cost: float, usd_value: float,
        midpoint: float = 0.0, slippage: float = 0.0,
        order_age_secs: float = 0.0, position_usd_after: float = 0.0,
        reward_rate_hr: float = 0.0,
        order_id: str = "",
        fill_event_id: str = "",
    ) -> bool:
        """Record a BUY fill with context for iteration analysis.

        FX-054: idempotent + truthful return value.
          • Pass a non-empty ``fill_event_id`` to dedup retries. INSERT OR
            IGNORE relies on the partial unique index `idx_fills_event_id`
            (`fill_event_id != ''`). Repeated calls with the same id
            return ``False`` (collision = no-op) on the second+ try.
          • Pass an empty ``fill_event_id`` (default) to keep pre-FX-054
            append-only semantics — used by tests + legacy migration data.
          • Exceptions are no longer swallowed at debug. Any error from
            the DB layer surfaces as ``log.warning`` AND returns ``False``
            so the caller can react (re-queue, alert, kill switch). The
            pre-FX-054 behaviour silently dropped fills on lock
            contention / schema mismatch / disk pressure with no signal
            other than `[FILL_WRITE] succeeded` lying.

        Returns:
            True on successful INSERT (one row added).
            False on idempotent collision OR DB error.
            Callers that distinguish "collision (safe)" from "error
            (unsafe)" should check the DB row count themselves.
        """
        # FX-054: defensive coercion of None → '' for the NOT NULL TEXT
        # columns. INSERT OR IGNORE silently swallows ALL constraint
        # violations (including NOT NULL), so a None value here would
        # disappear with no signal — exactly the silent-failure surface
        # this fix is designed to close. Caller bugs (e.g., drift sweep
        # passing slot.order_id when slot.order_id was cleared upstream)
        # are surfaced as empty-string rows in the DB rather than missing
        # rows.
        order_id = order_id if order_id is not None else ""
        fill_event_id = fill_event_id if fill_event_id is not None else ""
        try:
            conn = self._get_conn()
            cur = conn.execute(
                "INSERT OR IGNORE INTO fills (ts, condition_id, question, side, "
                "fill_type, shares, price, clob_cost, usd_value, "
                "midpoint, slippage, order_age_secs, position_usd_after, "
                "reward_rate_hr, order_id, fill_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), condition_id, question, side,
                 fill_type, shares, price, clob_cost, usd_value,
                 midpoint, slippage, order_age_secs, position_usd_after,
                 reward_rate_hr, order_id, fill_event_id),
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception as e:
            self._rollback_quiet()
            log.warning(
                f"[FILL_WRITE] DB log_fill error: cid={condition_id[:12]} "
                f"side={side} shares={shares:.2f} event_id={fill_event_id[:32]} "
                f"err={type(e).__name__}: {e}"
            )
            return False

    def log_fill_storm_marker(self, ts: float | None = None) -> bool:
        """Persist a cross-market fill-storm audit marker into the fills table.

        A sentinel row — condition_id='__FILL_STORM__', side='both',
        fill_type='STORM_ALERT', zero shares/price/cost/value — recording WHEN
        the farmer tripped its global fill-storm breaker, so the oversight/agent
        and post-incident analysis can see it. It is NOT a real fill: the zeros
        keep it out of pnl/share sums and the sentinel condition_id keeps it out
        of per-market fill counts.

        Replaces a former ``self.db.execute_sql(<raw INSERT>)`` call site that
        could never work (BotDatabase has no execute_sql) and was swallowed by a
        bare ``except: pass`` — so storms halted correctly (via
        ``self._fill_storm_until``) but left no audit trail. Typed + truthful +
        rollback-on-failure, matching every other writer (FX-080). The 8-column
        INSERT is schema-safe: every omitted fills column is NOT NULL DEFAULT.

        Returns True iff the row was written, False on DB error.
        """
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO fills (ts, condition_id, side, fill_type, "
                "shares, price, clob_cost, usd_value) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts if ts is not None else time.time(),
                 "__FILL_STORM__", "both", "STORM_ALERT", 0, 0, 0, 0),
            )
            conn.commit()
            return True
        except Exception as e:
            self._rollback_quiet()
            log.warning(f"DB log_fill_storm_marker error: {e}")
            return False

    def fill_event_exists(self, fill_event_id: str) -> bool:
        """FX-065: True iff a fills row with this (non-empty) event_id exists.

        Used by ``OrderLifecycle.handle_fill`` to guard ``PositionStore.record_fill``
        with the SAME idempotency key the ``fills`` table uses. Pre-FX-065,
        ``record_fill`` ran before AND outside the ``log_fill`` INSERT-OR-IGNORE
        boundary, so a re-handled fill (network retry, SDK-detect then
        stale-check on a grown partial, drift-sweep overlap) collapsed to one
        ``fills`` row (correct) but added the shares to PositionStore a SECOND
        time → inflated position + corrupted VWAP → fed the dump cost-basis
        (FX-066) and the kill-switch loss math.

        Empty/None event_id ⇒ False (legacy append-only callers can't dedup).
        Any DB error ⇒ False (fail toward recording — a missed dedup
        double-counts, but failing closed here would DROP a real fill, which
        is strictly worse for loss accounting).
        """
        if not fill_event_id:
            return False
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT 1 FROM fills WHERE fill_event_id = ? LIMIT 1",
                (fill_event_id,),
            ).fetchone()
            return row is not None
        except Exception as e:
            log.debug(f"fill_event_exists error: {e}")
            return False

    def fills_vwap(self, condition_id: str, side: str) -> tuple[float, float]:
        """FX-066 Tier 2: reconstruct (total_shares, VWAP) for a cid/side from
        the fills table. ``fills.price`` is stored YES-equivalent (see the fills
        schema), so the VWAP is directly usable as PositionStore ``avg_price``.

        Used to set the cost basis when registering an orphan / recovered
        position from on-chain balance. ``set_shares`` otherwise leaves
        ``avg_price=0`` → ``get_avg_price=0`` → ``vwap_cost=0`` at dump time
        (the FX-066 loss-as-profit bug, Tier-1-floored but magnitude-blind)
        AND ``get_position()=0`` → the position is invisible to the farmer's
        notional guardrails.

        Returns (0.0, 0.0) when there are no fills (a true orphan with no local
        record — cost basis genuinely unknown, so the caller leaves avg_price
        unset and the Tier 1 floor handles the dump) or on any DB error.
        """
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT COALESCE(SUM(shares), 0), "
                "       COALESCE(SUM(shares * price), 0) "
                "FROM fills WHERE condition_id = ? AND side = ?",
                (condition_id, side),
            ).fetchone()
            total_shares = float(row[0]) if row else 0.0
            sum_shares_price = float(row[1]) if row else 0.0
            if total_shares <= 0:
                return 0.0, 0.0
            return total_shares, round(sum_shares_price / total_shares, 6)
        except Exception as e:
            log.debug(f"fills_vwap error: {e}")
            return 0.0, 0.0

    def log_unwind(
        self, condition_id: str, question: str, side: str,
        shares: float, sell_price: float, usd_value: float,
        vwap_cost: float = 0.0,
        hold_duration_secs: float = 0.0, unwind_type: str = "",
        reward_earned_est: float = 0.0,
        unwind_event_id: str = "",
    ) -> bool:
        """Record a SELL (unwind) fill with hold context.

        FX-067: idempotent + truthful return value, mirroring FX-054's
        log_fill hardening. The realized loss/profit recorded here is the
        ONLY input to the 24h-realized-loss kill switch (both the farmer's
        and the oversight's), so a silently-dropped row blinds the kill.
          • Pass a non-empty ``unwind_event_id`` to dedup re-processing of
            the same dump across cycles / a restart between record and the
            dump-state clear. INSERT OR IGNORE relies on the partial unique
            index ``idx_unwinds_event_id`` (``unwind_event_id != ''``).
          • Empty ``unwind_event_id`` (default) keeps pre-FX-067 append-only
            semantics.
          • Exceptions are no longer swallowed at debug — any DB error
            surfaces as ``log.warning`` so a missed loss row is visible
            (pre-FX-067 it was a silent debug line, exactly the 2026-05-25
            "8 SELLs on-chain, 1 unwinds row" failure mode).

        Returns:
            True on successful INSERT (one row added).
            False on idempotent collision OR DB error.
        """
        unwind_event_id = unwind_event_id if unwind_event_id is not None else ""
        pnl = usd_value - vwap_cost
        try:
            conn = self._get_conn()
            cur = conn.execute(
                "INSERT OR IGNORE INTO unwinds (ts, condition_id, question, side, "
                "shares, sell_price, usd_value, vwap_cost, pnl, "
                "hold_duration_secs, unwind_type, reward_earned_est, unwind_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), condition_id, question, side,
                 shares, sell_price, usd_value, vwap_cost, pnl,
                 hold_duration_secs, unwind_type, reward_earned_est, unwind_event_id),
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception as e:
            self._rollback_quiet()
            log.warning(
                f"[UNWIND_WRITE] DB log_unwind error: cid={condition_id[:12]} "
                f"side={side} shares={shares:.2f} pnl=${pnl:+.2f} "
                f"event_id={unwind_event_id[:32]} err={type(e).__name__}: {e}"
            )
            return False

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
            self._rollback_quiet()
            log.warning(f"DB log_order_placed error: {e}")

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
            self._rollback_quiet()
            log.warning(f"DB log_order_cancelled error: {e}")

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
            self._rollback_quiet()
            log.warning(f"DB log_cycle_snapshot error: {e}")

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
            self._rollback_quiet()
            log.warning(f"DB log_merge error: {e}")

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
            self._rollback_quiet()
            log.warning(f"DB log_stop_loss error: {e}")

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
            self._rollback_quiet()
            log.warning(f"DB log_hourly_snapshot error: {e}")

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
            self._rollback_quiet()
            log.warning(f"DB log_market_selection error: {e}")

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
            self._rollback_quiet()
            log.warning(f"DB log_reward_comparison error: {e}")

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

    def log_cohort_pnl(self, rows: list[dict]) -> None:
        """Upsert A/B cohort P&L snapshots produced by ab_cohort_pnl.py."""
        if not rows:
            return
        try:
            conn = self._get_conn()
            conn.executemany(
                """INSERT INTO cohort_pnl (
                    ts, window_start_ts, window_end_ts, cohort, cohort_count, reward_earned,
                    unwind_pnl, net_pnl, fill_count, filled_markets, shares_filled,
                    gross_fill_cost, total_slippage, avg_fill_age_secs, avg_slippage,
                    deployed_markets, target_capital
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(window_end_ts, cohort, cohort_count) DO UPDATE SET
                    ts=excluded.ts,
                    window_start_ts=excluded.window_start_ts,
                    reward_earned=excluded.reward_earned,
                    unwind_pnl=excluded.unwind_pnl,
                    net_pnl=excluded.net_pnl,
                    fill_count=excluded.fill_count,
                    filled_markets=excluded.filled_markets,
                    shares_filled=excluded.shares_filled,
                    gross_fill_cost=excluded.gross_fill_cost,
                    total_slippage=excluded.total_slippage,
                    avg_fill_age_secs=excluded.avg_fill_age_secs,
                    avg_slippage=excluded.avg_slippage,
                    deployed_markets=excluded.deployed_markets,
                    target_capital=excluded.target_capital""",
                [
                    (
                        r["ts"], r["window_start_ts"], r["window_end_ts"], r["cohort"],
                        r["cohort_count"], r["reward_earned"], r["unwind_pnl"], r["net_pnl"],
                        r["fill_count"], r["filled_markets"], r["shares_filled"],
                        r["gross_fill_cost"], r["total_slippage"], r["avg_fill_age_secs"],
                        r["avg_slippage"], r["deployed_markets"], r["target_capital"],
                    )
                    for r in rows
                ],
            )
            conn.commit()
        except Exception as e:
            self._rollback_quiet()
            log.warning(f"DB log_cohort_pnl error: {e}")

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
            self._rollback_quiet()
            log.warning(f"DB save_position error: {e}")

    def save_all_positions(self, positions: dict, corrections: list[dict] | None = None) -> bool:
        """Batch UPSERT all positions in a single transaction.

        Args:
            positions: Dict of {condition_id: {question, yes_shares, ...}}
                matching the format from PositionStore._save().
            corrections: Optional list of position correction dicts from
                PositionStore to persist atomically with the positions write.

        Returns:
            True on commit, False on error.
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
            for corr in corrections or []:
                conn.execute(
                    "INSERT INTO position_corrections "
                    "(ts, condition_id, side, old_shares, new_shares, old_avg_price, new_avg_price, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (corr["ts"], corr["condition_id"], corr["side"],
                     corr["old_shares"], corr["new_shares"],
                     corr["old_avg_price"], corr["new_avg_price"],
                     corr.get("reason", "")),
                )
            conn.commit()
            return True
        except Exception as e:
            log.warning(f"DB save_all_positions error: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            return False

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

    def get_position_corrections(self, condition_id: str | None = None,
                                 since_ts: float | None = None,
                                 limit: int = 10000) -> list[dict]:
        """Return position correction rows for diagnostics.

        Args:
            condition_id: Optional filter by market.
            since_ts: Optional filter for ts >= since_ts.
            limit: Max rows to return.
        """
        try:
            conn = self._get_conn()
            query = "SELECT * FROM position_corrections WHERE 1=1"
            params: list = []
            if condition_id is not None:
                query += " AND condition_id = ?"
                params.append(condition_id)
            if since_ts is not None:
                query += " AND ts >= ?"
                params.append(since_ts)
            query += " ORDER BY ts DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            log.debug(f"DB get_position_corrections error: {e}")
            return []

    def delete_position(self, condition_id: str) -> None:
        """Remove a market's position."""
        try:
            conn = self._get_conn()
            conn.execute(
                "DELETE FROM positions WHERE condition_id = ?", (condition_id,)
            )
            conn.commit()
        except Exception as e:
            self._rollback_quiet()
            log.warning(f"DB delete_position error: {e}")

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
            self._rollback_quiet()
            log.warning(f"DB save_reward_state error: {e}")

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
            self._rollback_quiet()
            log.warning(f"DB save_usdc_balance error: {e}")

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

    def record_heartbeat(self, process: str, ts: "float | None" = None) -> bool:
        """FX-083: write a liveness heartbeat for `process` (e.g. 'farmer',
        'oversight') into reward_tracker_state. Called at the top of each cycle,
        mode-independent (liveness is not gated on trading, so a dry shadow is
        still monitored). FX-080 rollback on failure; never raises. Returns True
        on success."""
        if ts is None:
            ts = time.time()
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO reward_tracker_state (key, value) "
                "VALUES (?, ?)", (f"heartbeat:{process}", str(float(ts))),
            )
            conn.commit()
            return True
        except Exception as e:
            self._rollback_quiet()
            log.warning(f"DB record_heartbeat({process}) error: {e}")
            return False

    def get_heartbeat(self, process: str) -> "float | None":
        """FX-083: last heartbeat unix-ts for `process`, or None if never
        written / on error (caller fails OPEN — no false stale-page on missing
        data). Read-only — no transaction to roll back."""
        try:
            row = self._get_conn().execute(
                "SELECT value FROM reward_tracker_state WHERE key = ?",
                (f"heartbeat:{process}",),
            ).fetchone()
            return float(row["value"]) if row else None
        except Exception as e:
            log.debug(f"DB get_heartbeat({process}) error: {e}")
            return None

    # ── B-3: Persistent kill-switch sentinel ────────────────────────────

    def set_kill_switch(self, active: bool, reason: str = "", triggered_at: float = 0.0) -> bool:
        """Persist kill-switch state. Returns True on commit, False on error."""
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO kill_state (id, active, reason, triggered_at, updated_at) "
                "VALUES (1, ?, ?, ?, ?)",
                (1 if active else 0, str(reason)[:500], float(triggered_at), time.time()),
            )
            conn.commit()
            return True
        except Exception as e:
            self._rollback_quiet()
            log.warning(f"DB set_kill_switch error: {e}")
            return False

    def get_kill_switch(self) -> dict | None:
        """Return kill state for id=1, or None if no row. Raises on DB error
        so the caller can fail-safe to halted."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT active, reason, triggered_at FROM kill_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "active": bool(row["active"]),
            "reason": str(row["reason"]),
            "triggered_at": float(row["triggered_at"]),
        }

    def clear_kill_switch(self) -> bool:
        """Operator-only: clear the persistent kill switch sentinel."""
        try:
            conn = self._get_conn()
            conn.execute("DELETE FROM kill_state WHERE id = 1")
            conn.commit()
            return True
        except Exception as e:
            self._rollback_quiet()
            log.warning(f"DB clear_kill_switch error: {e}")
            return False

    def get_wallet_peak_usd(self) -> float | None:
        """FX-082/FX-095: portfolio high-water mark = MAX(total_value).

        B-5/resume-harness: if the operator has recorded a portfolio peak reset
        (e.g., to start a bounded experiment after a drawdown), only snapshots
        at or after the reset timestamp count toward the peak. The latest
        snapshot is also considered so the peak starts from the current value
        when the reset is first applied.
        """
        try:
            conn = self._get_conn()
            reset_ts = None
            row = conn.execute(
                "SELECT value FROM reward_tracker_state WHERE key = 'portfolio_peak_reset_ts'"
            ).fetchone()
            if row:
                reset_ts = float(row["value"])

            candidates: list[float] = []
            if reset_ts is not None:
                peak_row = conn.execute(
                    "SELECT MAX(total_value) FROM portfolio_snapshots WHERE ts >= ?",
                    (reset_ts,),
                ).fetchone()
                if peak_row and peak_row[0] is not None:
                    candidates.append(float(peak_row[0]))
                latest_row = conn.execute(
                    "SELECT total_value FROM portfolio_snapshots ORDER BY ts DESC LIMIT 1"
                ).fetchone()
                if latest_row and latest_row[0] is not None:
                    candidates.append(float(latest_row[0]))
            else:
                peak_row = conn.execute(
                    "SELECT MAX(total_value) FROM portfolio_snapshots"
                ).fetchone()
                if peak_row and peak_row[0] is not None:
                    candidates.append(float(peak_row[0]))

            return max(candidates) if candidates else None
        except Exception as e:
            log.debug(f"DB get_wallet_peak_usd error: {e}")
            return None

    def set_portfolio_peak_reset_ts(self, ts: float) -> bool:
        """Operator-only: record a portfolio peak reset timestamp.

        After this call, get_wallet_peak_usd() will ignore portfolio snapshots
        taken before ts, allowing a bounded experiment to start from the current
        portfolio value instead of a stale all-time peak.
        """
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO reward_tracker_state (key, value) VALUES (?, ?)",
                ("portfolio_peak_reset_ts", str(float(ts))),
            )
            conn.commit()
            return True
        except Exception as e:
            self._rollback_quiet()
            log.warning(f"DB set_portfolio_peak_reset_ts error: {e}")
            return False

    def clear_portfolio_peak_reset_ts(self) -> bool:
        """Operator-only: remove the portfolio peak reset sentinel."""
        try:
            conn = self._get_conn()
            conn.execute(
                "DELETE FROM reward_tracker_state WHERE key = 'portfolio_peak_reset_ts'"
            )
            conn.commit()
            return True
        except Exception as e:
            self._rollback_quiet()
            log.warning(f"DB clear_portfolio_peak_reset_ts error: {e}")
            return False

    def get_portfolio_value_usd(self) -> float | None:
        """FX-095: latest total_value from portfolio_snapshots, or None."""
        try:
            row = self._get_conn().execute(
                "SELECT total_value FROM portfolio_snapshots ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            return float(row[0]) if row and row[0] is not None else None
        except Exception as e:
            log.debug(f"DB get_portfolio_value_usd error: {e}")
            return None

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
            log.warning(f"DB save_all_reward_stats error: {e}")
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
            self._rollback_quiet()
            log.warning(f"write_placement_feedback error: {e}")

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
            self._rollback_quiet()
            log.warning(f"save_performance_snapshot error: {e}")

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
            self._rollback_quiet()
            log.warning(f"save_performance_batch error: {e}")

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
            self._rollback_quiet()
            log.warning(f"save_dump_state error: {e}")

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
            self._rollback_quiet()
            log.warning(f"delete_dump_state error: {e}")

    # ── Unliquidatable Markets (FX-005/006/007/008/009/028) ──────────
    # A market lands in this table when the bot definitively confirms its
    # orderbook is dead (create_and_post_order returned "orderbook does not
    # exist"). Both BUY (order_lifecycle) and SELL (dump_manager) paths
    # consult this table to skip the cid. The periodic re-probe (every ~6h,
    # FX-028) clears entries whose orderbook has returned.

    def mark_unliquidatable(self, condition_id: str, reason: str = "") -> None:
        """Persist a cid as unliquidatable. Idempotent — INSERT OR REPLACE
        means re-marking refreshes the reason + marked_at without creating
        duplicates."""
        try:
            self._get_conn().execute(
                """INSERT OR REPLACE INTO unliquidatable_markets
                   (condition_id, reason, marked_at, last_retry_at)
                   VALUES (?, ?, ?, 0)""",
                (condition_id, reason, time.time()),
            )
            self._get_conn().commit()
        except Exception as e:
            self._rollback_quiet()
            log.warning(f"mark_unliquidatable error: {e}")

    def is_unliquidatable(self, condition_id: str) -> bool:
        """Single-cid lookup. Returns False on DB errors (conservative —
        don't block legitimate placements on a transient DB hiccup)."""
        try:
            row = self._get_conn().execute(
                "SELECT 1 FROM unliquidatable_markets WHERE condition_id = ?",
                (condition_id,),
            ).fetchone()
            return row is not None
        except Exception as e:
            log.debug(f"is_unliquidatable error: {e}")
            return False

    def delete_unliquidatable(self, condition_id: str) -> None:
        """Re-enable retries for a cid (used by the periodic re-probe when
        the orderbook returns to life)."""
        try:
            self._get_conn().execute(
                "DELETE FROM unliquidatable_markets WHERE condition_id = ?",
                (condition_id,),
            )
            self._get_conn().commit()
        except Exception as e:
            self._rollback_quiet()
            log.warning(f"delete_unliquidatable error: {e}")

    def update_unliquidatable_retry(self, condition_id: str) -> None:
        """Stamp last_retry_at without un-marking (re-probe found the
        orderbook still dead)."""
        try:
            self._get_conn().execute(
                "UPDATE unliquidatable_markets SET last_retry_at = ? "
                "WHERE condition_id = ?",
                (time.time(), condition_id),
            )
            self._get_conn().commit()
        except Exception as e:
            self._rollback_quiet()
            log.warning(f"update_unliquidatable_retry error: {e}")

    def load_unliquidatable_set(self) -> set[str]:
        """Return the full set of unliquidatable cids.

        Currently used by tooling / future operator scripts only — the
        production hot paths consult ``is_unliquidatable(cid)`` per call
        (a single indexed PK lookup, ~µs on local SQLite, well under any
        meaningful cycle budget). Kept as a stable API surface so a
        startup cache can be wired in later without breaking callers.
        Empty set on DB errors so the farmer doesn't crash."""
        try:
            rows = self._get_conn().execute(
                "SELECT condition_id FROM unliquidatable_markets"
            ).fetchall()
            return {r["condition_id"] for r in rows}
        except Exception as e:
            log.debug(f"load_unliquidatable_set error: {e}")
            return set()

    def get_unliquidatable_for_reprobe(self, stale_secs: float) -> list[tuple[str, float]]:
        """Return (cid, last_retry_at) pairs whose last_retry_at is older
        than stale_secs (or 0 = never retried). Drives the periodic
        re-probe (FX-028); the farmer calls get_merged_book on each
        returned cid and either delete_unliquidatable (on success) or
        update_unliquidatable_retry (on continued failure)."""
        try:
            cutoff = time.time() - stale_secs
            rows = self._get_conn().execute(
                "SELECT condition_id, last_retry_at FROM unliquidatable_markets "
                "WHERE last_retry_at < ? OR last_retry_at = 0",
                (cutoff,),
            ).fetchall()
            return [(r["condition_id"], r["last_retry_at"]) for r in rows]
        except Exception as e:
            log.debug(f"get_unliquidatable_for_reprobe error: {e}")
            return []

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
            self._rollback_quiet()
            log.warning(f"save_active_order error: {e}")

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
            self._rollback_quiet()
            log.warning(f"delete_active_order error: {e}")

    # ── FX-049: Wallet reconciliation history ───────────────────────────

    def load_latest_wallet_reconcile(self) -> dict | None:
        """Return the most recent reconcile row, or None on first run.

        Used by ``oversight.wallet_reconciliation`` to establish the
        BASELINE for incremental divergence computation. None on first run
        triggers the baseline-snapshot path (no alert, just stamp).
        """
        try:
            r = self._get_conn().execute(
                "SELECT * FROM wallet_reconcile_history ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            return dict(r) if r else None
        except Exception as e:
            log.debug(f"load_latest_wallet_reconcile error: {e}")
            return None

    def insert_wallet_reconcile(
        self, *, actual_wallet: float, expected_wallet: float,
        divergence: float, status: str,
        baseline_ts: float, baseline_wallet: float,
        fills_delta: float = 0.0, unwinds_delta: float = 0.0,
        rewards_delta: float = 0.0,
    ) -> None:
        """Persist one reconcile event. Caller computes the math; this is
        a thin write-through. ``status`` is one of 'baseline' / 'ok' /
        'desync' / 'fail_open'.
        """
        try:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO wallet_reconcile_history
                   (ts, actual_wallet, expected_wallet, divergence, status,
                    baseline_ts, baseline_wallet,
                    fills_delta, unwinds_delta, rewards_delta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (time.time(), actual_wallet, expected_wallet, divergence,
                 status, baseline_ts, baseline_wallet,
                 fills_delta, unwinds_delta, rewards_delta),
            )
            conn.commit()
        except Exception as e:
            self._rollback_quiet()
            log.warning(f"insert_wallet_reconcile error: {e}")

    def sum_fills_usd_since(self, since_ts: float) -> float:
        """Σ usd_value of fills strictly after ``since_ts``. 0 on empty/err.

        Used by FX-049 reconciler to compute bot-DB's expected wallet
        outflow attributable to BUY fills in the window.
        """
        try:
            row = self._get_conn().execute(
                "SELECT COALESCE(SUM(usd_value), 0) FROM fills WHERE ts > ?",
                (since_ts,),
            ).fetchone()
            return float(row[0] or 0)
        except Exception as e:
            log.debug(f"sum_fills_usd_since error: {e}")
            return 0.0

    def sum_unwinds_usd_since(self, since_ts: float) -> float:
        """Σ usd_value of unwinds strictly after ``since_ts``. 0 on empty/err.

        Used by FX-049 reconciler to compute bot-DB's expected wallet
        inflow attributable to dump SELLs in the window. Post-FX-050 this
        is fee-adjusted to net cash received, matching wallet ground
        truth.
        """
        try:
            row = self._get_conn().execute(
                "SELECT COALESCE(SUM(usd_value), 0) FROM unwinds WHERE ts > ?",
                (since_ts,),
            ).fetchone()
            return float(row[0] or 0)
        except Exception as e:
            log.debug(f"sum_unwinds_usd_since error: {e}")
            return 0.0

    def clear_all_active_orders(self) -> None:
        """Clear all active orders (called on clean startup)."""
        try:
            self._get_conn().execute("DELETE FROM active_orders")
            self._get_conn().execute("DELETE FROM dump_states")
            self._get_conn().commit()
        except Exception as e:
            self._rollback_quiet()
            log.warning(f"clear_all_active_orders error: {e}")

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
            self._rollback_quiet()
            log.warning(f"purge_all_active_orders error: {e}")
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
            self._rollback_quiet()
            log.warning(f"DB log_book_snapshot error: {e}")

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
            self._rollback_quiet()
            log.warning(f"DB log_scoring_snapshot error: {e}")

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
            self._rollback_quiet()
            log.warning(f"DB prune_phase0_data error: {e}")
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
