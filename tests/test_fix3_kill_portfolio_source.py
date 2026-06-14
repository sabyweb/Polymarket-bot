"""FIX-3 (RC-5): the drawdown/loss kill sources its portfolio value from the
authoritative on-chain marks, not the DB `positions` table that can miss a fill.

Reproduces the 2026-06-13 FALSE drawdown-kill deadlock (a $22 on-chain fill the DB
never recorded made the cash-only metric read 16.1% > 15% and halt the farmer) and
proves the fix clears it, while staying fail-safe when the data-api is unavailable.
"""
import time
import pytest

import config
import simple_oversight as so
from simple_allocator import SimpleAllocator, KILL_DRAWDOWN_FRAC


@pytest.fixture(autouse=True)
def _reset_cache():
    so._LAST_ONCHAIN_INV = None
    yield
    so._LAST_ONCHAIN_INV = None


def _set_cfg(monkeypatch, source):
    monkeypatch.setattr(
        config,
        "cfg",
        lambda name: {
            "RF_KILL_PORTFOLIO_SOURCE": source,
            "RF_KILL_ONCHAIN_MAX_STALE_SECS": 3600.0,
        }.get(name),
    )


def _alloc():
    # check_kill_switch uses only its args + module constants, no instance state.
    return SimpleAllocator.__new__(SimpleAllocator)


# ── default behavior: "db" is a pure no-op ──

def test_db_source_is_noop(monkeypatch):
    _set_cfg(monkeypatch, "db")
    monkeypatch.setattr(so, "_onchain_inventory_value", lambda: 999.0)  # would differ
    assert so._portfolio_value_for_kill(1000.0, 1000.0) == 1000.0


# ── the fix: on-chain inventory is added back ──

def test_onchain_includes_db_missed_fill(monkeypatch):
    # 06-13: cash $1024.57, DB portfolio cash-only (missed the $22 fill); on-chain
    # inventory $22.41 -> portfolio $1046.98.
    _set_cfg(monkeypatch, "onchain")
    monkeypatch.setattr(so, "_onchain_inventory_value", lambda: 22.41)
    assert abs(so._portfolio_value_for_kill(1024.57, 1024.57) - 1046.98) < 0.01


def test_onchain_clears_the_false_drawdown_kill(monkeypatch):
    """End-to-end replay of 06-13: DB (cash-only) value FIRES the kill (false);
    the on-chain value does NOT (true dd 14.2% < 15%)."""
    peak, cash = 1220.52, 1024.57
    alloc = _alloc()

    kill_db, reason = alloc.check_kill_switch(
        wallet_usd=cash, portfolio_value_usd=cash,  # legacy cash-only
        portfolio_peak_usd=peak, realized_loss_24h=0.0,
    )
    assert kill_db is True and "drawdown" in reason  # the live false trip

    _set_cfg(monkeypatch, "onchain")
    monkeypatch.setattr(so, "_onchain_inventory_value", lambda: 22.41)
    pval = so._portfolio_value_for_kill(cash, cash)
    kill_fix, _ = alloc.check_kill_switch(
        wallet_usd=cash, portfolio_value_usd=pval,
        portfolio_peak_usd=peak, realized_loss_24h=0.0,
    )
    assert kill_fix is False
    assert (1.0 - pval / peak) < KILL_DRAWDOWN_FRAC  # 14.2% < 15%


def test_onchain_still_fires_on_a_real_drawdown(monkeypatch):
    """The fix must NOT mask a genuine drawdown: cash $900 + $20 inventory = $920
    vs peak $1220 = 24.6% -> still kills."""
    _set_cfg(monkeypatch, "onchain")
    monkeypatch.setattr(so, "_onchain_inventory_value", lambda: 20.0)
    pval = so._portfolio_value_for_kill(900.0, 900.0)
    kill, _ = _alloc().check_kill_switch(
        wallet_usd=900.0, portfolio_value_usd=pval,
        portfolio_peak_usd=1220.52, realized_loss_24h=0.0,
    )
    assert kill is True


# ── fail-safe: a missing data-api read never silently disables NOR falsely fires ──

def test_failsafe_reuses_fresh_cache(monkeypatch):
    _set_cfg(monkeypatch, "onchain")
    monkeypatch.setattr(so, "_onchain_inventory_value", lambda: 22.41)
    so._portfolio_value_for_kill(1024.57, 1024.57)          # caches inv=22.41
    monkeypatch.setattr(so, "_onchain_inventory_value", lambda: None)  # API down
    val = so._portfolio_value_for_kill(1024.57, 1024.57)
    assert abs(val - 1046.98) < 0.01  # reused fresh cache, NOT cash-only


def test_failsafe_falls_back_to_db_when_no_cache(monkeypatch):
    _set_cfg(monkeypatch, "onchain")
    monkeypatch.setattr(so, "_onchain_inventory_value", lambda: None)
    val = so._portfolio_value_for_kill(1024.57, 1030.0)
    assert val == 1030.0  # DB fallback (no worse than legacy), not a disable


def test_failsafe_ignores_stale_cache(monkeypatch):
    _set_cfg(monkeypatch, "onchain")
    so._LAST_ONCHAIN_INV = (time.time() - 99999, 22.41)  # very stale
    monkeypatch.setattr(so, "_onchain_inventory_value", lambda: None)
    val = so._portfolio_value_for_kill(1024.57, 1030.0)
    assert val == 1030.0  # stale cache ignored -> DB fallback


def test_onchain_inventory_value_sums_size_times_curprice(monkeypatch):
    """The helper marks at size*curPrice (the exchange mark)."""
    class _Resp:
        status_code = 200
        def json(self):
            return [
                {"size": 47.2, "curPrice": 0.525},   # ~24.78
                {"size": 10.0, "curPrice": 0.10},    # 1.00
            ]
    import requests
    monkeypatch.setattr(config, "FUNDER", "0xabc", raising=False)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    assert abs(so._onchain_inventory_value() - (47.2 * 0.525 + 1.0)) < 1e-6


def test_onchain_inventory_value_returns_none_on_http_error(monkeypatch):
    class _Resp:
        status_code = 503
        def json(self):
            return []
    import requests
    monkeypatch.setattr(config, "FUNDER", "0xabc", raising=False)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    assert so._onchain_inventory_value() is None
