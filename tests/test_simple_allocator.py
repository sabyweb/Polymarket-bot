"""Contract tests for SimpleAllocator (Path B-prime allocator replacement).

Each test names the contract it protects. Tests are deterministic, isolated,
no sleeps, no network — every external call is replaced by a stub.

Contracts under test (R6):
- C1: q_share priority 0 (API) trumps cumulative and cold-start
- C2: q_share priority 1 (cumulative DB) used when API absent
- C3: q_share priority 2 (cold-start) used when nothing else is known
- C4: markets below MIN_DAILY_RATE_USD are filtered out
- C5: markets below MIN_EXPECTED_PER_MARKET filtered out
- C6: deploys ranked by expected_daily_reward descending
- C7: MAX_DEPLOYED_MARKETS hard cap enforced
- C8: per-market budget capped at MAX_PER_MARKET_USD
- C9: total capital budget = wallet × DEPLOY_RATIO; over-budget markets routed to avoid
- C10: kill switch fires when 24h loss > KILL_LOSS_FRAC
- C11: kill switch fires when drawdown > KILL_DRAWDOWN_FRAC
- C12: kill switch returns empty deploys (no work done if killed)
- C13: API failure falls through to cumulative (does not raise)
- C14: output JSON schema matches farmer's existing reader expectations
- C15: cumulative DB ratio guards against q_score_samples < 10 and poisoned (>0.5)
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from types import SimpleNamespace

import pytest

# Module under test
import simple_allocator as sa
from simple_allocator import (
    SimpleAllocator,
    CandidateMarket,
    AllocationResult,
    COLD_START_Q_SHARE,
    MAX_DEPLOYED_MARKETS,
    MAX_PER_MARKET_USD,
    MIN_PER_MARKET_USD,
    DEPLOY_RATIO,
    KILL_LOSS_FRAC,
    KILL_DRAWDOWN_FRAC,
)


# ── Fixtures ──

def _make_allocator(http_stub=None, now=1700000000):
    """Build SimpleAllocator with no real network."""
    return SimpleAllocator(
        db_path=":memory:",
        wallet_address="0xWALLET",
        funder="0xFUNDER",
        api_key="key",
        # Valid base64url-encoded secret (16 bytes "1234567890123456")
        api_secret="MTIzNDU2Nzg5MDEyMzQ1Ng==",
        api_passphrase="pass",
        _now=lambda: now,
        _http=http_stub or (lambda *a, **k: SimpleNamespace(status_code=500, text="", json=lambda: {})),
    )


def _stub_http(routes):
    """Build an _http callable that routes by URL substring."""
    def _http(url, **kwargs):
        for substr, response in routes.items():
            if substr in url:
                return response
        return SimpleNamespace(status_code=404, text="", json=lambda: {})
    return _http


def _make_candidate(cid, daily_rate=50, min_size=20, max_spread=4.5):
    return CandidateMarket(
        condition_id=cid,
        yes_tid=f"yes_{cid[:8]}",
        no_tid=f"no_{cid[:8]}",
        daily_rate=daily_rate,
        max_spread=max_spread,
        min_size=min_size,
    )


# ── Q-share priority contracts ──

def test_C1_api_q_share_trumps_cumulative_and_cold_start():
    """C1: API value takes precedence over all other sources."""
    a = _make_allocator()
    q, src = a.estimate_q_share(
        cid="0xABC",
        api_shares={"0xABC": 0.05},          # 5%
        cumulative={"0xABC": 0.01},          # 1% (would be used if API absent)
    )
    assert q == 0.05
    assert src == "api"


def test_C2_cumulative_used_when_api_absent():
    """C2: when API doesn't return this cid, cumulative DB ratio is used."""
    a = _make_allocator()
    q, src = a.estimate_q_share(
        cid="0xABC",
        api_shares={"0xOTHER": 0.05},        # API only knows about a different cid
        cumulative={"0xABC": 0.01},
    )
    assert q == 0.01
    assert src == "cumulative"


def test_C3_cold_start_prior_when_nothing_known():
    """C3: completely unknown markets get the cold-start prior."""
    a = _make_allocator()
    q, src = a.estimate_q_share(cid="0xNEW", api_shares={}, cumulative={})
    assert q == COLD_START_Q_SHARE
    assert src == "cold_start"


# ── Filter contracts ──

def test_C4_filters_markets_below_min_daily_rate():
    """C4: markets with daily_rate < MIN_DAILY_RATE_USD are rejected entirely."""
    a = _make_allocator()
    candidates = [
        _make_candidate("0xRICH", daily_rate=100),
        _make_candidate("0xPOOR", daily_rate=5),   # below MIN_DAILY_RATE_USD (20)
    ]
    result = a.compute(
        wallet_usd=200, wallet_peak_usd=250, wallet_24h_ago_usd=200,
        realized_loss_24h=0, markets=candidates,
    )
    deploy_cids = {m.condition_id for m in result.deploys}
    assert "0xPOOR" not in deploy_cids


def test_C5_filters_markets_with_expected_below_threshold():
    """C5: when daily_rate × q_share < MIN_EXPECTED_PER_MARKET (0.20), filter out."""
    a = _make_allocator()
    # daily_rate=20 (just above min), but q_share at cold_start (0.001) → expected=0.02
    # That's below MIN_EXPECTED_PER_MARKET (0.20). Should be filtered.
    candidate = _make_candidate("0xTHIN", daily_rate=20)
    result = a.compute(
        wallet_usd=200, wallet_peak_usd=200, wallet_24h_ago_usd=200,
        realized_loss_24h=0, markets=[candidate],
    )
    assert candidate not in result.deploys


# ── Ranking + budget contracts ──

def test_C6_deploys_ranked_by_expected_daily_reward():
    """C6: highest expected_daily_reward gets deployed first."""
    a = _make_allocator()
    # All have daily_rate large enough; q_share via cold_start (uniform).
    # So ranking is by daily_rate effectively.
    candidates = [
        _make_candidate(f"0x{i:04d}aaa", daily_rate=200 + i*10)  # different unique cids
        for i in range(5)
    ]
    # Use cumulative ratios to differentiate q_share — 1st candidate highest
    a._cumulative_override = {c.condition_id: 0.01 * (i + 1) for i, c in enumerate(candidates)}
    # Patch load_cumulative_ratios for this test
    a.load_cumulative_ratios = lambda: a._cumulative_override
    a.fetch_current_q_shares = lambda: {}

    result = a.compute(
        wallet_usd=2000, wallet_peak_usd=2000, wallet_24h_ago_usd=2000,
        realized_loss_24h=0, markets=candidates,
    )
    # Highest expected_reward = highest q_share × highest daily_rate
    # The LAST candidate has both highest daily_rate AND highest q_share → ranks first
    assert result.deploys[0].condition_id == candidates[-1].condition_id


def test_C7_hard_cap_on_max_deployed_markets():
    """C7: MAX_DEPLOYED_MARKETS is a hard ceiling, regardless of budget."""
    a = _make_allocator()
    a.fetch_current_q_shares = lambda: {}
    # Give all markets the same large q_share so they all pass the expected-reward filter
    a.load_cumulative_ratios = lambda: {f"0x{i:04d}aaa": 0.05 for i in range(50)}

    candidates = [_make_candidate(f"0x{i:04d}aaa", daily_rate=100) for i in range(50)]
    result = a.compute(
        wallet_usd=10_000, wallet_peak_usd=10_000, wallet_24h_ago_usd=10_000,
        realized_loss_24h=0, markets=candidates,
    )
    assert len(result.deploys) <= MAX_DEPLOYED_MARKETS


def test_C8_per_market_capital_capped():
    """C8: no deploy gets more than MAX_PER_MARKET_USD."""
    a = _make_allocator()
    a.fetch_current_q_shares = lambda: {}
    a.load_cumulative_ratios = lambda: {"0xBIG": 0.10}

    # Large min_size that would naturally exceed MAX_PER_MARKET_USD
    candidate = _make_candidate("0xBIG", daily_rate=1000, min_size=200)
    result = a.compute(
        wallet_usd=5000, wallet_peak_usd=5000, wallet_24h_ago_usd=5000,
        realized_loss_24h=0, markets=[candidate],
    )
    if result.deploys:
        assert result.deploys[0].target_capital <= MAX_PER_MARKET_USD + 0.01


def test_C9_budget_caps_total_at_deploy_ratio_of_wallet():
    """C9: total capital deployed ≤ wallet × DEPLOY_RATIO."""
    a = _make_allocator()
    a.fetch_current_q_shares = lambda: {}
    a.load_cumulative_ratios = lambda: {f"0x{i:04d}aaa": 0.05 for i in range(50)}

    candidates = [_make_candidate(f"0x{i:04d}aaa", daily_rate=200) for i in range(50)]
    wallet = 200
    result = a.compute(
        wallet_usd=wallet, wallet_peak_usd=wallet, wallet_24h_ago_usd=wallet,
        realized_loss_24h=0, markets=candidates,
    )
    assert result.capital_deployed <= wallet * DEPLOY_RATIO + 0.5  # small float tolerance


# ── Kill switch contracts ──

def test_C10_kill_switch_fires_on_24h_loss_breach():
    """C10: 24h realized loss > KILL_LOSS_FRAC × wallet → kill."""
    a = _make_allocator()
    wallet = 200
    realized_loss = wallet * KILL_LOSS_FRAC + 1  # just over threshold
    result = a.compute(
        wallet_usd=wallet, wallet_peak_usd=wallet, wallet_24h_ago_usd=wallet,
        realized_loss_24h=realized_loss, markets=[],
    )
    assert result.kill_switch is True
    assert "24h" in result.kill_reason or "loss" in result.kill_reason


def test_C11_kill_switch_fires_on_drawdown_breach():
    """C11: wallet falls > KILL_DRAWDOWN_FRAC from peak → kill."""
    a = _make_allocator()
    peak = 1000
    wallet = peak * (1 - KILL_DRAWDOWN_FRAC) - 1  # below threshold
    result = a.compute(
        wallet_usd=wallet, wallet_peak_usd=peak, wallet_24h_ago_usd=wallet,
        realized_loss_24h=0, markets=[],
    )
    assert result.kill_switch is True
    assert "drawdown" in result.kill_reason.lower()


def test_C12_killed_result_has_no_deploys():
    """C12: when killed, deploys is empty regardless of what markets were passed."""
    a = _make_allocator()
    a.fetch_current_q_shares = lambda: {"0xX": 0.5}
    a.load_cumulative_ratios = lambda: {}

    candidates = [_make_candidate("0xX", daily_rate=1000)]
    # Force kill via huge loss
    result = a.compute(
        wallet_usd=200, wallet_peak_usd=200, wallet_24h_ago_usd=200,
        realized_loss_24h=999, markets=candidates,
    )
    assert result.kill_switch is True
    assert result.deploys == []
    assert result.capital_deployed == 0


# ── Robustness contracts ──

def test_C13_api_failure_falls_through_silently():
    """C13: q_share API returning non-200 must not raise; fall through to cumulative."""
    error_http = _stub_http({
        "/rewards/user/percentages": SimpleNamespace(status_code=500, text="oops", json=lambda: {}),
    })
    a = _make_allocator(http_stub=error_http)
    # If we ask for q_share and API errors, we should get empty dict back (no raise)
    shares = a.fetch_current_q_shares()
    assert shares == {}


def test_C13b_api_exception_falls_through_silently():
    """C13b: q_share API raising an exception is caught."""
    def raise_http(*a, **k):
        raise ConnectionError("network down")

    a = _make_allocator(http_stub=raise_http)
    shares = a.fetch_current_q_shares()
    assert shares == {}


# ── Output schema contract ──

def test_C14_output_json_has_required_farmer_fields():
    """C14: market_allocations.json deploys carry all fields the farmer reads.

    The farmer relies on: condition_id, yes_tid, no_tid, action, shares,
    daily_rate, max_spread, min_size, est_capital_cost, _total_capital.
    Missing any one breaks the farmer's placement loop.
    """
    a = _make_allocator()
    a.fetch_current_q_shares = lambda: {}
    a.load_cumulative_ratios = lambda: {"0xABC": 0.05}

    candidates = [_make_candidate("0xABC", daily_rate=500)]
    result = a.compute(
        wallet_usd=200, wallet_peak_usd=200, wallet_24h_ago_usd=200,
        realized_loss_24h=0, markets=candidates,
    )

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as tmp:
        out_path = tmp.name

    try:
        a.write_allocation_json(result, output_path=out_path)
        with open(out_path) as f:
            payload = json.load(f)

        # Top-level schema
        assert payload["version"].startswith("simple")
        assert "generated_at" in payload
        assert "num_deploy" in payload
        assert "num_avoid" in payload
        assert "total_capital_deployed" in payload
        assert "markets" in payload

        # Per-deploy schema (the bits the farmer reads)
        deploys = [m for m in payload["markets"] if m.get("action") == "deploy"]
        if deploys:
            d = deploys[0]
            for field in ["condition_id", "yes_tid", "no_tid", "action",
                          "shares", "daily_rate", "max_spread", "min_size",
                          "est_capital_cost", "_total_capital"]:
                assert field in d, f"deploy row missing required field: {field}"
    finally:
        os.unlink(out_path)


# ── Cumulative-ratio sanity contract ──

def test_C15_cumulative_ratios_skip_low_samples_and_poisoned():
    """C15: load_cumulative_ratios filters out cids with too few samples or
    poisoned ratios (>0.5, which is the FX-005 era saturation signature)."""
    # Build a tiny in-memory DB with reward_market_stats
    db_path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE reward_market_stats ("
        "condition_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL NOT NULL DEFAULT 0)"
    )
    # Healthy: 100 samples, ratio 0.01
    conn.execute("INSERT INTO reward_market_stats VALUES (?, ?, ?)",
                 ("0xOK", json.dumps({
                     "total_q_score": 100.0, "total_market_q": 10000.0,
                     "q_score_samples": 100}), 0))
    # Too few samples
    conn.execute("INSERT INTO reward_market_stats VALUES (?, ?, ?)",
                 ("0xFEW", json.dumps({
                     "total_q_score": 100.0, "total_market_q": 10000.0,
                     "q_score_samples": 3}), 0))
    # Poisoned (ratio > 0.5 = pre-Option-B saturation)
    conn.execute("INSERT INTO reward_market_stats VALUES (?, ?, ?)",
                 ("0xPOI", json.dumps({
                     "total_q_score": 9000.0, "total_market_q": 10000.0,
                     "q_score_samples": 100}), 0))
    conn.commit()
    conn.close()

    a = SimpleAllocator(
        db_path=db_path, wallet_address="x", funder="x",
        api_key="x", api_secret="MTIzNDU2Nzg5MDEyMzQ1Ng==", api_passphrase="x",
    )
    ratios = a.load_cumulative_ratios()
    assert "0xOK" in ratios
    assert ratios["0xOK"] == pytest.approx(0.01)
    assert "0xFEW" not in ratios   # skipped: too few samples
    assert "0xPOI" not in ratios   # skipped: poisoned ratio

    os.unlink(db_path)
