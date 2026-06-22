"""B-2 — total_capital SPOF backstop (RF_KILL_PERSIST_TOTAL_CAPITAL_ENABLED).

The farmer's capital-relative kills (realized-loss, unrealized-loss, notional, cluster, rapid-growth)
divide by total_capital read from market_allocations.json. That read has NO TTL, so it returns None
only when the file is MISSING/CORRUPT/unstamped — and then ALL those limbs skip at once (the SPOF).
`_total_capital_armed` caches the last good value and reuses it on a None read (if fresher than the
cap) so the kills stay armed across a transient file loss. OFF = byte-identical (raw read, no cache).
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "py_clob_client_v2" not in sys.modules:
    _m = MagicMock()
    sys.modules["py_clob_client_v2"] = _m
    sys.modules["py_clob_client_v2.clob_types"] = _m.clob_types
    sys.modules["py_clob_client_v2.client"] = _m.client
    sys.modules["py_clob_client_v2.order_builder"] = _m.order_builder
    sys.modules["py_clob_client_v2.order_builder.constants"] = _m.order_builder.constants
    _m.order_builder.constants.BUY = "BUY"
    _m.order_builder.constants.SELL = "SELL"

from config import BotConfig  # noqa: E402
from reward_farmer import RewardFarmer  # noqa: E402


@pytest.fixture
def cfg_overrides():
    bc = BotConfig.instance()
    saved = dict(bc._overrides)
    try:
        yield bc._overrides
    finally:
        bc._overrides.clear()
        bc._overrides.update(saved)


def _farmer(raw):
    """A bare farmer whose raw alloc read returns `raw`, with an empty cache."""
    rf = RewardFarmer.__new__(RewardFarmer)
    rf._last_total_capital = None
    rf._last_total_capital_ts = 0.0
    rf._guardrail_total_capital_from_alloc = MagicMock(return_value=raw)
    return rf


def test_off_is_byte_identical(cfg_overrides):
    """OFF: returns the raw read unchanged; cache is never touched."""
    cfg_overrides["RF_KILL_PERSIST_TOTAL_CAPITAL_ENABLED"] = False
    rf = _farmer(900.0)
    assert rf._total_capital_armed() == 900.0
    assert rf._last_total_capital is None          # not cached when off
    rf2 = _farmer(None)
    assert rf2._total_capital_armed() is None       # None passes through (today's behavior)
    assert rf2._last_total_capital is None


def test_on_caches_valid(cfg_overrides):
    cfg_overrides["RF_KILL_PERSIST_TOTAL_CAPITAL_ENABLED"] = True
    rf = _farmer(900.0)
    assert rf._total_capital_armed() == 900.0
    assert rf._last_total_capital == 900.0
    assert rf._last_total_capital_ts > 0.0


def test_on_serves_fresh_cache_when_alloc_none(cfg_overrides):
    """The SPOF fix: alloc goes missing (raw None) but a fresh cache keeps the kills armed."""
    cfg_overrides["RF_KILL_PERSIST_TOTAL_CAPITAL_ENABLED"] = True
    rf = _farmer(900.0)
    rf._total_capital_armed()                       # cache 900
    rf._guardrail_total_capital_from_alloc.return_value = None  # alloc now missing/corrupt
    assert rf._total_capital_armed() == 900.0       # served from fresh cache => kills stay armed


def test_on_expired_cache_returns_none(cfg_overrides):
    """Cache older than the cap => fall through to None (today's block-new behavior; never worse)."""
    cfg_overrides["RF_KILL_PERSIST_TOTAL_CAPITAL_ENABLED"] = True
    cfg_overrides["RF_TOTAL_CAPITAL_MAX_STALE_SECS"] = 7200.0
    rf = _farmer(None)
    rf._last_total_capital = 900.0
    rf._last_total_capital_ts = time.time() - 8000.0   # older than the 2h cap
    assert rf._total_capital_armed() is None


def test_on_no_cache_returns_none(cfg_overrides):
    """No prior good value + None raw (e.g. first-startup-before-any-file) => None (inherent)."""
    cfg_overrides["RF_KILL_PERSIST_TOTAL_CAPITAL_ENABLED"] = True
    rf = _farmer(None)
    assert rf._total_capital_armed() is None


def test_on_stale_high_cache_never_false_kills(cfg_overrides):
    """A stale-HIGH cache only raises the denominator => thresholds (frac*T) go UP => kills LESS
    likely, never a false kill. The served value is the higher last-good, not a lower live value."""
    cfg_overrides["RF_KILL_PERSIST_TOTAL_CAPITAL_ENABLED"] = True
    rf = _farmer(1200.0)
    rf._total_capital_armed()                       # cache 1200 (peak)
    rf._guardrail_total_capital_from_alloc.return_value = None
    assert rf._total_capital_armed() == 1200.0      # higher T => higher thresholds => safer
