import pytest
import sqlite3
import os
from ab_cohort_pnl import compute, report


def _make_dbs(tmp_path):
    bot = tmp_path / "bot_history.db"
    reward = tmp_path / "reward_snapshots.db"
    candidate = tmp_path / "candidate_features.db"

    conn = sqlite3.connect(str(bot))
    conn.executescript(
        """
        CREATE TABLE fills (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            condition_id TEXT NOT NULL,
            shares REAL NOT NULL,
            clob_cost REAL NOT NULL,
            slippage REAL NOT NULL DEFAULT 0,
            order_age_secs REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE unwinds (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            condition_id TEXT NOT NULL,
            pnl REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE cohort_pnl (
            ts REAL NOT NULL,
            window_start_ts REAL NOT NULL,
            window_end_ts REAL NOT NULL,
            cohort INTEGER NOT NULL,
            cohort_count INTEGER NOT NULL DEFAULT 2,
            reward_earned REAL NOT NULL DEFAULT 0,
            unwind_pnl REAL NOT NULL DEFAULT 0,
            net_pnl REAL NOT NULL DEFAULT 0,
            fill_count INTEGER NOT NULL DEFAULT 0,
            filled_markets INTEGER NOT NULL DEFAULT 0,
            shares_filled REAL NOT NULL DEFAULT 0,
            gross_fill_cost REAL NOT NULL DEFAULT 0,
            total_slippage REAL NOT NULL DEFAULT 0,
            avg_fill_age_secs REAL NOT NULL DEFAULT 0,
            avg_slippage REAL NOT NULL DEFAULT 0,
            deployed_markets INTEGER NOT NULL DEFAULT 0,
            target_capital REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (window_end_ts, cohort, cohort_count)
        );
        """
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(reward))
    conn.execute(
        """CREATE TABLE reward_snapshots (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            date TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            question TEXT,
            earnings_usd REAL NOT NULL
        )"""
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(candidate))
    conn.execute(
        """CREATE TABLE candidate_features (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            cycle_ts REAL NOT NULL,
            condition_id TEXT NOT NULL,
            cohort INTEGER NOT NULL,
            action TEXT NOT NULL,
            reason TEXT,
            daily_rate REAL,
            max_spread REAL,
            min_size INTEGER,
            midpoint_guess REAL,
            volume_24h REAL,
            expected_q_share REAL,
            q_share_source TEXT,
            expected_daily_reward REAL,
            target_shares INTEGER,
            target_capital REAL,
            target_queue_usd REAL,
            hours_to_resolution REAL,
            end_date_iso TEXT,
            game_start_time TEXT,
            question TEXT
        )"""
    )
    conn.commit()
    conn.close()

    return str(bot), str(reward), str(candidate)


def test_cohort_pnl_combines_reward_and_unwind_pnl(tmp_path, capsys):
    bot, reward, candidate = _make_dbs(tmp_path)
    now = 1000000.0

    # C0 market: reward $10, one fill, one unwind profit $5
    conn = sqlite3.connect(candidate)
    conn.execute(
        "INSERT INTO candidate_features (ts, cycle_ts, condition_id, cohort, action, target_capital) VALUES (?,?,?,?,?,?)",
        (now - 100, now - 100, "0xC0", 0, "deploy", 100.0),
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(reward)
    conn.execute(
        "INSERT INTO reward_snapshots (ts, date, condition_id, earnings_usd) VALUES (?,?,?,?)",
        (now - 50, "2026-06-24", "0xC0", 10.0),
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(bot)
    conn.execute(
        "INSERT INTO fills (ts, condition_id, shares, clob_cost, slippage, order_age_secs) VALUES (?,?,?,?,?,?)",
        (now - 200, "0xC0", 10.0, 0.5, -0.01, 60.0),
    )
    conn.execute(
        "INSERT INTO unwinds (ts, condition_id, pnl) VALUES (?,?,?)",
        (now - 150, "0xC0", 5.0),
    )
    conn.commit()
    conn.close()

    rows = compute(
        window_hours=1,
        db_path=bot,
        reward_db_path=reward,
        candidate_db_path=candidate,
        _now=lambda: now,
        _cohort_count=2,
    )

    assert len(rows) == 2
    c0 = next(r for r in rows if r["cohort"] == 0)
    c1 = next(r for r in rows if r["cohort"] == 1)

    assert c0["reward_earned"] == 10.0
    assert c0["unwind_pnl"] == 5.0
    assert c0["net_pnl"] == 15.0
    assert c0["fill_count"] == 1
    assert c0["filled_markets"] == 1
    assert c0["deployed_markets"] == 1
    assert c0["target_capital"] == 100.0
    assert c0["avg_fill_age_secs"] == 60.0
    assert c0["avg_slippage"] == -0.01

    assert c1["deployed_markets"] == 0
    assert c1["net_pnl"] == 0.0


def test_cohort_pnl_idempotent(tmp_path):
    bot, reward, candidate = _make_dbs(tmp_path)
    now = 1000000.0

    for db in (candidate,):
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO candidate_features (ts, cycle_ts, condition_id, cohort, action, target_capital) VALUES (?,?,?,?,?,?)",
            (now - 100, now - 100, "0xC1", 1, "deploy", 50.0),
        )
        conn.commit()
        conn.close()

    conn = sqlite3.connect(reward)
    conn.execute(
        "INSERT INTO reward_snapshots (ts, date, condition_id, earnings_usd) VALUES (?,?,?,?)",
        (now - 50, "2026-06-24", "0xC1", 3.0),
    )
    conn.commit()
    conn.close()

    compute(window_hours=1, db_path=bot, reward_db_path=reward, candidate_db_path=candidate, _now=lambda: now, _cohort_count=2)
    compute(window_hours=1, db_path=bot, reward_db_path=reward, candidate_db_path=candidate, _now=lambda: now, _cohort_count=2)

    conn = sqlite3.connect(bot)
    rows = conn.execute("SELECT COUNT(*), net_pnl FROM cohort_pnl WHERE cohort=1").fetchone()
    conn.close()
    assert rows[0] == 1
    assert rows[1] == 3.0


def test_cohort_pnl_missing_reward_falls_back_to_zero(tmp_path):
    bot, reward, candidate = _make_dbs(tmp_path)
    now = 1000000.0

    conn = sqlite3.connect(candidate)
    conn.execute(
        "INSERT INTO candidate_features (ts, cycle_ts, condition_id, cohort, action, target_capital) VALUES (?,?,?,?,?,?)",
        (now - 100, now - 100, "0xLOSS", 1, "deploy", 100.0),
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(bot)
    conn.execute("INSERT INTO unwinds (ts, condition_id, pnl) VALUES (?,?,?)", (now - 50, "0xLOSS", -7.0))
    conn.commit()
    conn.close()

    rows = compute(window_hours=1, db_path=bot, reward_db_path=reward, candidate_db_path=candidate, _now=lambda: now, _cohort_count=2)
    c1 = next(r for r in rows if r["cohort"] == 1)
    assert c1["reward_earned"] == 0.0
    assert c1["unwind_pnl"] == -7.0
    assert c1["net_pnl"] == -7.0


def test_report_output(tmp_path, capsys):
    bot, reward, candidate = _make_dbs(tmp_path)
    now = 1000000.0

    conn = sqlite3.connect(candidate)
    conn.execute(
        "INSERT INTO candidate_features (ts, cycle_ts, condition_id, cohort, action, target_capital) VALUES (?,?,?,?,?,?)",
        (now - 100, now - 100, "0xR", 0, "deploy", 200.0),
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(reward)
    conn.execute(
        "INSERT INTO reward_snapshots (ts, date, condition_id, earnings_usd) VALUES (?,?,?,?)",
        (now - 50, "2026-06-24", "0xR", 1.5),
    )
    conn.commit()
    conn.close()

    report(window_hours=1, db_path=bot, reward_db_path=reward, candidate_db_path=candidate, _now=lambda: now, _cohort_count=2)
    out = capsys.readouterr().out
    assert "Cohort 0" in out
    assert "net pnl" in out


def test_three_cohort_pnl_and_cohort_count_tag(tmp_path):
    """Cohort P&L supports 3 cohorts and tags rows with cohort_count."""
    bot, reward, candidate = _make_dbs(tmp_path)
    now = 1000000.0

    conn = sqlite3.connect(candidate)
    for cid, cohort in [("0xC0", 0), ("0xC1", 1), ("0xC2", 2)]:
        conn.execute(
            "INSERT INTO candidate_features (ts, cycle_ts, condition_id, cohort, action, target_capital) VALUES (?,?,?,?,?,?)",
            (now - 100, now - 100, cid, cohort, "deploy", 100.0),
        )
    conn.commit()
    conn.close()

    rows = compute(
        window_hours=1,
        db_path=bot,
        reward_db_path=reward,
        candidate_db_path=candidate,
        _now=lambda: now,
        _cohort_count=3,
    )

    assert {r["cohort"] for r in rows} == {0, 1, 2}
    assert all(r["cohort_count"] == 3 for r in rows)
    c2 = next(r for r in rows if r["cohort"] == 2)
    assert c2["deployed_markets"] == 1
    assert c2["target_capital"] == 100.0
