"""Unit tests for V2 endpoint compatibility helpers (Plan B):

  - market._v2_sampling_to_v1_flat: translates V2 /sampling-markets shape
    into the V1 /rewards/markets/current flat shape.
  - market._gamma_paginated_keyset: cursor pagination for /markets/keyset
    (replaces deprecated `offset` pagination).
  - market.fetch_clob_rewards_params: V2 fallback when primary endpoint
    returns 5xx or empty.

These cover the Plan B patch landed for the post-2026-04-28 CLOB V2
cutover when /rewards/markets/current backend is unhealthy.
"""

import sys
from unittest.mock import patch, MagicMock


# Mock py_clob_client_v2 so production imports succeed in test env
if "py_clob_client_v2" not in sys.modules:
    mock_clob = MagicMock()
    mock_clob.clob_types = MagicMock()
    mock_clob.client = MagicMock()
    mock_clob.order_builder = MagicMock()
    mock_clob.order_builder.constants = MagicMock()
    sys.modules["py_clob_client_v2"] = mock_clob
    sys.modules["py_clob_client_v2.clob_types"] = mock_clob.clob_types
    sys.modules["py_clob_client_v2.client"] = mock_clob.client
    sys.modules["py_clob_client_v2.order_builder"] = mock_clob.order_builder
    sys.modules["py_clob_client_v2.order_builder.constants"] = mock_clob.order_builder.constants

import market  # noqa: E402


# ── _v2_sampling_to_v1_flat ──────────────────────────────────────────────
def test_translator_basic():
    """V2 nested shape → V1 flat shape with all standard fields."""
    v2 = {
        "condition_id": "0xabc",
        "rewards": {
            "rates": [{"rewards_daily_rate": 250.5}],
            "min_size": 100,
            "max_spread": 3.5,
        },
        "tokens": [{"token_id": "1"}],
        "end_date_iso": "2026-12-31T00:00:00Z",
        "game_start_time": "2026-06-01T19:00:00Z",
        "minimum_tick_size": 0.01,
        "question": "Test?",
        "active": True,
        "accepting_orders": True,
        "neg_risk": False,
    }
    flat = market._v2_sampling_to_v1_flat(v2)
    assert flat is not None
    assert flat["condition_id"] == "0xabc"
    assert flat["total_daily_rate"] == 250.5
    assert flat["rewards_min_size"] == 100
    assert flat["rewards_max_spread"] == 3.5
    assert flat["tokens"] == [{"token_id": "1"}]
    assert flat["end_date_iso"] == "2026-12-31T00:00:00Z"
    assert flat["game_start_time"] == "2026-06-01T19:00:00Z"
    assert flat["question"] == "Test?"
    assert flat["accepting_orders"] is True
    assert flat["neg_risk"] is False


def test_translator_returns_none_when_rewards_missing():
    """Markets without a `rewards` object should return None (caller skips)."""
    assert market._v2_sampling_to_v1_flat({"condition_id": "0xabc"}) is None
    assert market._v2_sampling_to_v1_flat({"condition_id": "0xabc", "rewards": None}) is None
    assert market._v2_sampling_to_v1_flat({"condition_id": "0xabc", "rewards": "garbage"}) is None


def test_translator_empty_rates_list():
    """Empty rates[] → total_daily_rate=0; market still returned (for
    downstream filter) — bot's RF_MIN_DAILY_RATE filter excludes zero."""
    v2 = {
        "condition_id": "0xabc",
        "rewards": {"rates": [], "min_size": 50, "max_spread": 3.0},
    }
    flat = market._v2_sampling_to_v1_flat(v2)
    assert flat is not None
    assert flat["total_daily_rate"] == 0
    assert flat["rewards_min_size"] == 50


def test_translator_sums_multi_rate():
    """Multiple entries in rates[] → sum (matches V1 total_daily_rate semantics)."""
    v2 = {
        "condition_id": "0xabc",
        "rewards": {
            "rates": [
                {"rewards_daily_rate": 100},
                {"rewards_daily_rate": 50},
                {"rewards_daily_rate": 25},
            ],
            "min_size": 200,
            "max_spread": 2.0,
        },
    }
    flat = market._v2_sampling_to_v1_flat(v2)
    assert flat["total_daily_rate"] == 175


def test_translator_null_game_start_time_to_empty_string():
    """null game_start_time → empty string (matches existing convention)."""
    v2 = {
        "condition_id": "0xabc",
        "rewards": {"rates": [{"rewards_daily_rate": 1}], "min_size": 1, "max_spread": 1},
        "game_start_time": None,
    }
    flat = market._v2_sampling_to_v1_flat(v2)
    assert flat["game_start_time"] == ""


def test_translator_handles_malformed_rate_entries():
    """Non-dict entries in rates[] are skipped without raising."""
    v2 = {
        "condition_id": "0xabc",
        "rewards": {
            "rates": [
                {"rewards_daily_rate": 50},
                "garbage",
                None,
                {"rewards_daily_rate": 25},
            ],
            "min_size": 100,
            "max_spread": 2.5,
        },
    }
    flat = market._v2_sampling_to_v1_flat(v2)
    assert flat["total_daily_rate"] == 75


# ── _gamma_paginated_keyset ──────────────────────────────────────────────
def test_keyset_paginates_until_terminal():
    """Cursor loop terminates when next_cursor empty."""
    pages = [
        {"markets": [{"id": 1}, {"id": 2}], "next_cursor": "abc"},
        {"markets": [{"id": 3}], "next_cursor": "def"},
        {"markets": [], "next_cursor": ""},   # empty markets — stop
    ]
    call_count = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        i = call_count["n"]
        call_count["n"] += 1
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = pages[i] if i < len(pages) else {"markets": [], "next_cursor": ""}
        return resp

    with patch("market.requests.get", side_effect=fake_get):
        result = market._gamma_paginated_keyset({"limit": 100}, max_pages=10)
    assert result == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_keyset_terminates_on_repeat_cursor():
    """If server returns same cursor twice, we stop (avoid infinite loop)."""
    page = {"markets": [{"id": 1}], "next_cursor": "stuck"}

    def fake_get(url, params=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = page
        return resp

    with patch("market.requests.get", side_effect=fake_get):
        result = market._gamma_paginated_keyset({"limit": 100}, max_pages=10)
    # First page returns, second page sees same cursor and bails out
    assert len(result) == 1


def test_keyset_handles_request_error_silently():
    """Network error in middle of pagination returns whatever we have so far."""
    pages = [
        {"markets": [{"id": 1}], "next_cursor": "abc"},
    ]
    call_count = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        if call_count["n"] >= len(pages):
            raise Exception("network blip")
        i = call_count["n"]
        call_count["n"] += 1
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = pages[i]
        return resp

    with patch("market.requests.get", side_effect=fake_get):
        result = market._gamma_paginated_keyset({"limit": 100}, max_pages=10)
    assert result == [{"id": 1}]


def test_keyset_respects_max_pages():
    """max_pages bounds the loop even if server keeps returning new cursors."""
    counter = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        counter["n"] += 1
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "markets": [{"id": counter["n"]}],
            "next_cursor": f"c{counter['n']}",
        }
        return resp

    with patch("market.requests.get", side_effect=fake_get):
        result = market._gamma_paginated_keyset({"limit": 100}, max_pages=3)
    assert len(result) == 3
    assert counter["n"] == 3


# ── fetch_clob_rewards_params V2 fallback dispatch ──────────────────────
def test_fetch_rewards_params_uses_primary_when_healthy():
    """When primary returns data, fallback is NOT called."""
    primary_data = {
        "data": [{
            "condition_id": "0xabc",
            "rewards_min_size": 50,
            "rewards_max_spread": 4.0,
        }],
        "next_cursor": "LTE=",   # Polymarket's terminal sentinel
    }

    def fake_requests_get(url, params=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = primary_data
        return resp

    market._clob_rewards_cache = {}  # reset
    with patch("market.requests.get", side_effect=fake_requests_get) as mock_req, \
         patch("market._fetch_v2_sampling_rewards_params") as mock_fb:
        result = market.fetch_clob_rewards_params()
    assert "0xabc" in result
    assert result["0xabc"]["min_size"] == 50
    assert result["0xabc"]["max_spread"] == 0.04   # 4.0 cents → 0.04
    mock_fb.assert_not_called()
    # Pagination should have terminated cleanly on "LTE=" sentinel —
    # exactly one request, not a runaway loop.
    assert mock_req.call_count == 1


def test_fetch_rewards_params_falls_back_on_5xx():
    """When primary raises, V2 fallback is called and result returned."""
    def fake_requests_get(url, params=None, timeout=None):
        raise Exception("HTTP 500 simulated")

    market._clob_rewards_cache = {}
    with patch("market.requests.get", side_effect=fake_requests_get), \
         patch("market._fetch_v2_sampling_rewards_params",
               return_value={"0xfb": {"min_size": 100, "max_spread": 0.025}}) as mock_fb:
        result = market.fetch_clob_rewards_params()
    assert result == {"0xfb": {"min_size": 100, "max_spread": 0.025}}
    mock_fb.assert_called_once()


def test_fetch_rewards_params_falls_back_on_empty_data():
    """When primary returns {data: []}, fallback is also called."""
    def fake_requests_get(url, params=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {"data": [], "next_cursor": "LTE="}
        return resp

    market._clob_rewards_cache = {}
    with patch("market.requests.get", side_effect=fake_requests_get), \
         patch("market._fetch_v2_sampling_rewards_params",
               return_value={"0xfb": {"min_size": 1, "max_spread": 0.01}}) as mock_fb:
        result = market.fetch_clob_rewards_params()
    assert result == {"0xfb": {"min_size": 1, "max_spread": 0.01}}
    mock_fb.assert_called_once()


def test_fetch_rewards_params_uses_cache_when_both_fail():
    """When both primary and fallback empty, returns last-known cache."""
    def fake_requests_get(url, params=None, timeout=None):
        raise Exception("500")

    market._clob_rewards_cache = {"0xcached": {"min_size": 50, "max_spread": 0.045}}
    with patch("market.requests.get", side_effect=fake_requests_get), \
         patch("market._fetch_v2_sampling_rewards_params", return_value={}):
        result = market.fetch_clob_rewards_params()
    assert result == {"0xcached": {"min_size": 50, "max_spread": 0.045}}
