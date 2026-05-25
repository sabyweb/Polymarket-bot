"""Contract tests for simple_oversight.py entry point.

Each test names a contract per R6. No network, no sleeps, no real DB.

Contracts:
- O1: get_realized_loss_24h sums |pnl| for pnl<0 unwinds in last 24h
- O2: get_realized_loss_24h returns 0 on empty table (no exception)
- O3: get_realized_loss_24h ignores unwinds older than 24h
- O4: get_wallet_peak_usd returns max(snapshot peak, current_wallet)
- O5: get_wallet_peak_usd handles empty snapshots table
- O6: get_wallet_24h_ago returns None on no rows in window
- O7: get_wallet_24h_ago returns balance from row closest to 24h ago
- O8: write_portfolio_snapshot persists the balance with current timestamp
- O9: run_once writes alloc file even if 0 deploys
- O10: run_once logs CAPITAL_SOURCE line
- O11: run_once on wallet fetch failure returns no_capital, no alloc write
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Module under test
import simple_oversight as so
from simple_allocator import SimpleAllocator, AllocationResult


# ── Fixtures ──

def _make_temp_db():
    """Create a fresh sqlite DB with the schemas simple_oversight reads/writes."""
    db_path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE unwinds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            condition_id TEXT NOT NULL,
            side TEXT NOT NULL,
            shares REAL NOT NULL,
            sell_price REAL NOT NULL,
            usd_value REAL NOT NULL,
            vwap_cost REAL NOT NULL DEFAULT 0,
            pnl REAL NOT NULL,
            unwind_type TEXT NOT NULL DEFAULT 'normal'
        );
        CREATE TABLE portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            exchange_balance REAL NOT NULL
        );
        CREATE TABLE reward_market_stats (
            condition_id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at REAL NOT NULL DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()
    return db_path


def _make_allocator(db_path: str) -> SimpleAllocator:
    return SimpleAllocator(
        db_path=db_path,
        wallet_address="0xWALLET",
        funder="0xFUNDER",
        api_key="key",
        api_secret="MTIzNDU2Nzg5MDEyMzQ1Ng==",  # valid base64
        api_passphrase="pass",
        _now=lambda: 1700000000,
        _http=lambda *a, **k: SimpleNamespace(status_code=500, text="", json=lambda: {}),
    )


# ── get_realized_loss_24h contracts ──

def test_O1_realized_loss_sums_negative_pnl_in_window():
    """O1: sum |pnl| of unwinds with pnl<0 and ts within last 24h."""
    db = _make_temp_db()
    now = time.time()
    conn = sqlite3.connect(db)
    # Two negative unwinds in window, one positive (excluded), one outside window
    conn.executemany(
        "INSERT INTO unwinds (ts, condition_id, side, shares, sell_price, usd_value, pnl) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (now - 3600, "0xA", "yes", 10, 0.5, 5.0, -1.50),   # 1h ago, loss $1.50
            (now - 7200, "0xB", "no", 20, 0.4, 8.0, -2.25),    # 2h ago, loss $2.25
            (now - 1800, "0xC", "yes", 5, 0.6, 3.0, +0.5),     # 30m ago, GAIN — excluded
            (now - 86400 * 2, "0xD", "yes", 1, 0.5, 0.5, -10), # 2d ago — outside window
        ],
    )
    conn.commit()
    conn.close()

    loss = so.get_realized_loss_24h(db)
    assert loss == pytest.approx(1.50 + 2.25)
    os.unlink(db)


def test_O2_realized_loss_returns_zero_on_empty_table():
    """O2: empty table → 0.0, no exception."""
    db = _make_temp_db()
    loss = so.get_realized_loss_24h(db)
    assert loss == 0.0
    os.unlink(db)


def test_O3_realized_loss_excludes_old_unwinds():
    """O3: unwinds older than 24h are excluded."""
    db = _make_temp_db()
    now = time.time()
    conn = sqlite3.connect(db)
    # All losses outside 24h window
    conn.executemany(
        "INSERT INTO unwinds (ts, condition_id, side, shares, sell_price, usd_value, pnl) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (now - 86400 - 1, "0xA", "yes", 10, 0.5, 5.0, -1.50),
            (now - 86400 - 3600, "0xB", "no", 20, 0.4, 8.0, -2.25),
        ],
    )
    conn.commit()
    conn.close()

    loss = so.get_realized_loss_24h(db)
    assert loss == 0.0
    os.unlink(db)


# ── get_wallet_peak_usd contracts ──

def test_O4_wallet_peak_returns_max_of_snapshot_and_current():
    """O4: peak = max(historical_max_from_snapshots, current_wallet)."""
    db = _make_temp_db()
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO portfolio_snapshots (ts, exchange_balance) VALUES (?, ?)",
        [(1, 100), (2, 250), (3, 200)],  # peak = 250
    )
    conn.commit()
    conn.close()

    # Current < historical → use historical peak
    assert so.get_wallet_peak_usd(db, current_wallet=180) == 250
    # Current > historical → use current
    assert so.get_wallet_peak_usd(db, current_wallet=300) == 300

    os.unlink(db)


def test_O5_wallet_peak_handles_empty_snapshots():
    """O5: no snapshots → peak == current_wallet (no kill on first cycle)."""
    db = _make_temp_db()
    assert so.get_wallet_peak_usd(db, current_wallet=500) == 500
    os.unlink(db)


# ── get_wallet_24h_ago contracts ──

def test_O6_wallet_24h_ago_returns_none_when_empty():
    """O6: no rows in window → None."""
    db = _make_temp_db()
    assert so.get_wallet_24h_ago(db) is None
    os.unlink(db)


def test_O7_wallet_24h_ago_returns_balance_nearest_24h_back():
    """O7: of all rows in window (target ± 1h), return the one closest to 24h ago.

    Window = [target - 3600, target + 3600] where target = now - 86400.
    Code ranks by ABS(ts - target) and picks the smallest.
    """
    db = _make_temp_db()
    now = time.time()
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO portfolio_snapshots (ts, exchange_balance) VALUES (?, ?)",
        [
            (now - 86400 + 60, 200.0),    # 60s after target — closest
            (now - 86400 - 1800, 195.0),  # 30min before target
            (now - 86400 + 3500, 210.0),  # 58min after target (just inside +1h window)
        ],
    )
    conn.commit()
    conn.close()

    bal = so.get_wallet_24h_ago(db)
    # Closest by |ts - target|: row with offset 60s (200.0)
    assert bal == 200.0
    os.unlink(db)


def test_O7b_wallet_24h_ago_picks_correct_row_with_distant_data():
    """O7b: when rows are unequally distant, pick the closest by absolute time."""
    db = _make_temp_db()
    now = time.time()
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO portfolio_snapshots (ts, exchange_balance) VALUES (?, ?)",
        [
            (now - 86400 - 1800, 195.0),  # 30 min BEFORE target
            (now - 86400 + 3500, 210.0),  # 58 min AFTER target — further away
        ],
    )
    conn.commit()
    conn.close()

    bal = so.get_wallet_24h_ago(db)
    assert bal == 195.0  # 30 min < 58 min
    os.unlink(db)


# ── write_portfolio_snapshot contract ──

def test_O8_write_portfolio_snapshot_persists():
    """O8: write_portfolio_snapshot inserts a row with current ts."""
    db = _make_temp_db()
    before = time.time()
    so.write_portfolio_snapshot(db, 1234.56)
    after = time.time()

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT ts, exchange_balance FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    assert row is not None
    assert before <= row[0] <= after + 1
    assert row[1] == 1234.56
    os.unlink(db)


# ── run_once contracts ──

def test_O9_run_once_writes_alloc_file_even_with_zero_deploys():
    """O9: alloc file is always written each cycle (atomic), even if no deploys."""
    db = _make_temp_db()
    allocator = _make_allocator(db)
    # Patch compute to return empty result
    allocator.compute = MagicMock(return_value=AllocationResult(
        deploys=[], avoids=[], total_capital=1000, capital_deployed=0,
        expected_total_reward=0, kill_switch=False, kill_reason="",
        sources_used={"api": 0, "cumulative": 0, "cold_start": 0},
    ))

    out_path = tempfile.mktemp(suffix=".json")
    with patch.object(so, "get_live_wallet_usd", return_value=1000.0):
        result = so.run_once(allocator, db, out_path, signer_key="k", api_creds=None)

    assert os.path.exists(out_path)
    payload = json.load(open(out_path))
    assert payload["num_deploy"] == 0
    assert result["wallet"] == 1000.0
    os.unlink(out_path)
    os.unlink(db)


def test_O10_run_once_emits_capital_source_log(caplog):
    """O10: every cycle emits a [CAPITAL_SOURCE] line for operator visibility."""
    db = _make_temp_db()
    allocator = _make_allocator(db)
    allocator.compute = MagicMock(return_value=AllocationResult(
        deploys=[], avoids=[], total_capital=500, capital_deployed=0,
        expected_total_reward=0, kill_switch=False, kill_reason="",
        sources_used={"api": 0, "cumulative": 0, "cold_start": 0},
    ))

    out_path = tempfile.mktemp(suffix=".json")
    import logging
    with caplog.at_level(logging.INFO, logger="simple_oversight"):
        with patch.object(so, "get_live_wallet_usd", return_value=500.0):
            so.run_once(allocator, db, out_path, signer_key="k", api_creds=None)

    capital_lines = [r for r in caplog.records if "CAPITAL_SOURCE" in r.getMessage()]
    assert len(capital_lines) >= 1
    assert "500" in capital_lines[0].getMessage()

    os.unlink(out_path)
    os.unlink(db)


def test_O11_run_once_no_capital_on_wallet_fetch_failure():
    """O11: if live wallet fetch raises, return no_capital and DO NOT overwrite alloc.

    Stale alloc preservation matters — the farmer keeps using the prior alloc
    until a fresh one writes. Overwriting with garbage on transient API failure
    would break the farmer.
    """
    db = _make_temp_db()
    allocator = _make_allocator(db)
    allocator.compute = MagicMock()  # should never be called

    out_path = tempfile.mktemp(suffix=".json")
    # Write a sentinel "previous alloc" to verify it's preserved
    with open(out_path, "w") as f:
        json.dump({"sentinel": "previous"}, f)

    with patch.object(so, "get_live_wallet_usd", side_effect=ConnectionError("network down")):
        result = so.run_once(allocator, db, out_path, signer_key="k", api_creds=None)

    assert result.get("status") == "no_capital"
    allocator.compute.assert_not_called()

    # Previous alloc still there
    payload = json.load(open(out_path))
    assert payload.get("sentinel") == "previous"

    os.unlink(out_path)
    os.unlink(db)
