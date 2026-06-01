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
- C7: SOFT sanity cap MAX_DEPLOYED_MARKETS (default 500) — was hard 20-cap pre-P2
- C8: per-market notional = cost-to-score (min_size × midpoint × 2 × (1+buffer)) — NOT wallet-fraction capped (P2 / FX-052)
- C9: total notional can exceed wallet — overcommit by design (P2 / FX-052 / Ground Rule 2)
- C16 (P2): positive-EV gate filters markets where expected_reward < expected_fill_cost
- C17 (P2): alloc.json metadata includes _notional_overcommit_ratio + _target_market_count_band
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
    MAX_DEPLOYED_MARKETS,  # P2: now a cfg-driven SOFT sanity cap (default 500)
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
    """C5: when daily_rate × q_share < MIN_EXPECTED_PER_MARKET, filter out.

    Constructs a market where expected = daily_rate × cold_start prior is
    strictly less than the MIN_EXPECTED_PER_MARKET constant, regardless of
    its concrete value. Robust to constant tuning.
    """
    from simple_allocator import MIN_EXPECTED_PER_MARKET, MIN_DAILY_RATE_USD, COLD_START_Q_SHARE
    a = _make_allocator()
    # P2: thresholds are now cfg-driven (callables). Resolve to values.
    min_expected = MIN_EXPECTED_PER_MARKET()
    min_rate = MIN_DAILY_RATE_USD()
    # Pick a daily_rate that JUST passes MIN_DAILY_RATE_USD but produces
    # expected < MIN_EXPECTED_PER_MARKET under cold-start q_share.
    # Solve: daily_rate × COLD_START_Q_SHARE < MIN_EXPECTED_PER_MARKET
    #        daily_rate < MIN_EXPECTED_PER_MARKET / COLD_START_Q_SHARE
    max_rate_to_fail_filter = min_expected / COLD_START_Q_SHARE
    target_rate = max(min_rate, max_rate_to_fail_filter * 0.5)
    # If MIN_DAILY_RATE_USD is already above the threshold this test can't run
    if target_rate >= max_rate_to_fail_filter:
        pytest.skip("constants make this contract vacuous — see C5 description")

    candidate = _make_candidate("0xTHIN", daily_rate=target_rate)
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


def test_C7_soft_sanity_cap_on_max_deployed_markets():
    """C7 (P2): MAX_DEPLOYED_MARKETS is a SOFT sanity cap (default 500),
    not the design constraint. The design point per Ground Rule 1 is 50-200
    markets in steady state — the 500 cap exists only to bound runaway from
    API anomalies (Polymarket lists ~5k markets). Pre-P2 this was a hard 20.
    """
    a = _make_allocator()
    a.fetch_current_q_shares = lambda: {}
    # 50 eligible markets all clear the EV gate
    a.load_cumulative_ratios = lambda: {f"0x{i:04d}aaa": 0.05 for i in range(50)}

    candidates = [_make_candidate(f"0x{i:04d}aaa", daily_rate=100) for i in range(50)]
    result = a.compute(
        wallet_usd=10_000, wallet_peak_usd=10_000, wallet_24h_ago_usd=10_000,
        realized_loss_24h=0, markets=candidates,
    )
    # Pre-P2 this would have been ≤20. Post-P2 sanity cap is 500 (default),
    # so all 50 eligible markets should deploy.
    assert len(result.deploys) == 50, (
        f"all 50 EV-positive markets should deploy under OverCommit semantics; "
        f"got {len(result.deploys)}"
    )
    # And confirm the soft cap is well above the actual deploy count
    assert MAX_DEPLOYED_MARKETS() >= 200, (
        f"MAX_DEPLOYED_MARKETS soft cap ({MAX_DEPLOYED_MARKETS()}) must permit "
        f"Ground Rule 1 target band (50-200)"
    )


def test_C8_per_market_notional_is_cost_to_score_not_wallet_fraction():
    """C8 (P2 / FX-052): per-market notional = min_size × midpoint × 2 × (1+buffer).
    NOT bounded by a wallet fraction. Pre-P2 was capped at MAX_PER_MARKET_USD=$60.
    """
    a = _make_allocator()
    a.fetch_current_q_shares = lambda: {}
    a.load_cumulative_ratios = lambda: {"0xBIG": 0.10}

    # min_size=200, midpoint=0.5 → cost-to-score = 200 × 1.0 × 1.10 = $220
    candidate = _make_candidate("0xBIG", daily_rate=1000, min_size=200)
    result = a.compute(
        wallet_usd=5000, wallet_peak_usd=5000, wallet_24h_ago_usd=5000,
        realized_loss_24h=0, markets=[candidate],
    )
    assert len(result.deploys) == 1, "high-EV market must deploy"
    target = result.deploys[0].target_capital
    # Expected: 200 × min(0.5, 0.5) × 2 × (1 + 0.10) = 200 × 1.0 × 1.10 = $220
    assert 190.0 < target < 250.0, (
        f"per-market notional should reflect cost-to-score (~$220 for min_size=200), "
        f"NOT capped at $60 wallet fraction; got ${target:.2f}"
    )


def test_C9_total_notional_can_exceed_wallet_overcommit_by_design():
    """C9 (P2 / FX-052 / Ground Rule 2): total notional can exceed wallet.

    100 markets × ~$22 cost-to-score (min_size=20, midpoint=0.5, buffer=10%)
    = $2200 on a $500 wallet (4.4× overcommit). Pre-P2 this would have been
    capped at wallet × 0.95 = $475 (Rule 2 violation).
    """
    a = _make_allocator()
    a.fetch_current_q_shares = lambda: {}
    # 100 markets all positive-EV
    a.load_cumulative_ratios = lambda: {f"0x{i:04d}aaa": 0.10 for i in range(100)}

    candidates = [_make_candidate(f"0x{i:04d}aaa", daily_rate=500) for i in range(100)]
    wallet = 500  # $500 wallet — small enough that 100 markets × $22 overcommits clearly
    result = a.compute(
        wallet_usd=wallet, wallet_peak_usd=wallet, wallet_24h_ago_usd=wallet,
        realized_loss_24h=0, markets=candidates,
    )
    overcommit_ratio = result.capital_deployed / wallet
    # Per Ground Rule 2: 3-8× design point. 100 markets × $22 ≈ $2200 / $500 = 4.4×.
    assert overcommit_ratio > 3.0, (
        f"overcommit operation must push total notional into 3-8× wallet band; "
        f"got {overcommit_ratio:.2f}× (${result.capital_deployed:.2f} / ${wallet:.2f})"
    )
    assert len(result.deploys) == 100, (
        f"all 100 EV-positive markets must deploy under OverCommit semantics; "
        f"got {len(result.deploys)}"
    )


def test_C16_positive_ev_gate_filters_negative_ev_markets():
    """C16 (P2 / FX-052): markets where expected_reward < expected_fill_cost
    are routed to avoid. Keeps deploys positive-EV per Ground Rules 1 + 3.
    """
    a = _make_allocator()
    # daily_rate=10, q_share=0.001 → expected_reward = $0.01/day
    # cost-to-score = 50 × 1.0 × 1.10 = $55. expected_fill_cost = 55 × 0.02 = $1.10
    # $0.01/day < $1.10/fill → negative EV → must be avoided
    a.fetch_current_q_shares = lambda: {}
    a.load_cumulative_ratios = lambda: {"0xLOW": 0.001}

    low_ev = _make_candidate("0xLOW", daily_rate=10)
    result = a.compute(
        wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
        realized_loss_24h=0, markets=[low_ev],
    )
    assert len(result.deploys) == 0, (
        f"negative-EV market must be filtered (expected_reward $0.01/day < "
        f"expected_fill_cost ~$1.10/fill); got {len(result.deploys)} deploys"
    )
    assert "0xLOW" in [m.condition_id for m in result.avoids]


def test_C17_alloc_json_metadata_includes_overcommit_fields():
    """C17 (P2): write_allocation_json stamps _notional_overcommit_ratio
    and _target_market_count_band in top-level metadata for monitoring.
    """
    a = _make_allocator()
    a.fetch_current_q_shares = lambda: {}
    a.load_cumulative_ratios = lambda: {"0xA": 0.05}
    candidates = [_make_candidate("0xA", daily_rate=200)]
    result = a.compute(
        wallet_usd=1000, wallet_peak_usd=1000, wallet_24h_ago_usd=1000,
        realized_loss_24h=0, markets=candidates,
    )
    tmp = tempfile.mktemp(suffix=".json")
    a.write_allocation_json(result, output_path=tmp)
    with open(tmp) as f:
        data = json.load(f)
    assert "_notional_overcommit_ratio" in data
    assert "_target_market_count_band" in data
    assert data["_target_market_count_band"] == [50, 200]
    assert data["version"] == "simple-1.2"
    os.unlink(tmp)


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

        # Per-deploy schema (the bits the farmer reads — verified by grep audit of
        # reward_farmer.py:950-1000 — farmer reads condition_id (mandatory), action,
        # shares_per_side (NOT "shares"), daily_rate, min_size, max_spread, end_date_iso,
        # _total_capital).
        deploys = [m for m in payload["markets"] if m.get("action") == "deploy"]
        if deploys:
            d = deploys[0]
            required = ["condition_id", "action", "shares_per_side", "daily_rate",
                        "min_size", "max_spread", "end_date_iso", "est_capital_cost",
                        "_total_capital"]
            for field in required:
                assert field in d, f"deploy row missing required field: {field}"
            # Negative assertion — guard against accidental rename back to "shares"
            assert "shares" not in d or "shares_per_side" in d, (
                "schema regression: 'shares' present without 'shares_per_side' "
                "(farmer reads 'shares_per_side')"
            )
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


# ── FX-056 extreme-price filter contracts ──

def test_C16_extreme_low_midpoint_filtered():
    """C16: markets with midpoint < EXTREME_PRICE_LOW (0.10) are filtered out.

    The 2026-05-25 fill on 0x46c09232 (midpoint ~$0.08) took 13.3% slippage
    on dump — net negative even with the market's high daily_rate. FX-056
    excludes these structurally.
    """
    from simple_allocator import EXTREME_PRICE_LOW
    a = _make_allocator()
    a.fetch_current_q_shares = lambda: {}
    a.load_cumulative_ratios = lambda: {"0xCHEAP": 0.05}

    cheap = _make_candidate("0xCHEAP", daily_rate=500)
    cheap.midpoint_guess = EXTREME_PRICE_LOW - 0.01  # below floor
    result = a.compute(
        wallet_usd=200, wallet_peak_usd=200, wallet_24h_ago_usd=200,
        realized_loss_24h=0, markets=[cheap],
    )
    deploy_cids = {m.condition_id for m in result.deploys}
    assert "0xCHEAP" not in deploy_cids


def test_C17_extreme_high_midpoint_filtered():
    """C17: markets with midpoint > EXTREME_PRICE_HIGH (0.90) are filtered out."""
    from simple_allocator import EXTREME_PRICE_HIGH
    a = _make_allocator()
    a.fetch_current_q_shares = lambda: {}
    a.load_cumulative_ratios = lambda: {"0xPRICEY": 0.05}

    pricey = _make_candidate("0xPRICEY", daily_rate=500)
    pricey.midpoint_guess = EXTREME_PRICE_HIGH + 0.01  # above ceiling
    result = a.compute(
        wallet_usd=200, wallet_peak_usd=200, wallet_24h_ago_usd=200,
        realized_loss_24h=0, markets=[pricey],
    )
    deploy_cids = {m.condition_id for m in result.deploys}
    assert "0xPRICEY" not in deploy_cids


def test_C18_mid_priced_market_passes_filter():
    """C18: markets in [EXTREME_PRICE_LOW, EXTREME_PRICE_HIGH] are kept.

    Also confirms the default midpoint_guess (0.5) — used when the API
    didn't return a tokens-array price hint — passes through fail-open.
    """
    a = _make_allocator()
    a.fetch_current_q_shares = lambda: {}
    a.load_cumulative_ratios = lambda: {"0xMID": 0.05, "0xDEFAULT": 0.05}

    mid = _make_candidate("0xMID", daily_rate=500)
    mid.midpoint_guess = 0.50  # explicit mid

    default_mp = _make_candidate("0xDEFAULT", daily_rate=500)
    # midpoint_guess left at dataclass default (0.5) — represents "unknown"

    result = a.compute(
        wallet_usd=2000, wallet_peak_usd=2000, wallet_24h_ago_usd=2000,
        realized_loss_24h=0, markets=[mid, default_mp],
    )
    deploy_cids = {m.condition_id for m in result.deploys}
    assert "0xMID" in deploy_cids
    assert "0xDEFAULT" in deploy_cids  # fail-open contract


def test_C19_fetch_reward_markets_extracts_tokens_price():
    """C19: when /rewards/markets/current includes a tokens array with a
    price field, fetch_reward_markets populates midpoint_guess so the
    FX-056 filter can act on real data."""
    routes = {
        "/rewards/markets/current": SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {
                "data": [
                    {
                        "condition_id": "0xWITHPRICE",
                        "yes_token_id": "yes_tok",
                        "no_token_id": "no_tok",
                        "native_daily_rate": 50.0,
                        "rewards_max_spread": 4.5,
                        "rewards_min_size": 20,
                        "tokens": [{"price": 0.07, "outcome": "Yes"}],
                    }
                ],
                "next_cursor": "LTE=",
            },
        ),
    }
    a = _make_allocator(http_stub=_stub_http(routes))
    markets = a.fetch_reward_markets()
    assert len(markets) == 1
    assert markets[0].condition_id == "0xWITHPRICE"
    assert markets[0].midpoint_guess == pytest.approx(0.07)


def test_C20_fetch_reward_markets_defaults_midpoint_when_tokens_absent():
    """C20: when the API response has no tokens array (or no price),
    midpoint_guess stays at the dataclass default 0.5 — the fail-open
    path that lets FX-056 filter passes unrecognized markets through
    rather than blackholing them."""
    routes = {
        "/rewards/markets/current": SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {
                "data": [
                    {
                        "condition_id": "0xNOPRICE",
                        "native_daily_rate": 50.0,
                        "rewards_max_spread": 4.5,
                        "rewards_min_size": 20,
                        # no tokens field
                    }
                ],
                "next_cursor": "LTE=",
            },
        ),
    }
    a = _make_allocator(http_stub=_stub_http(routes))
    markets = a.fetch_reward_markets()
    assert len(markets) == 1
    assert markets[0].midpoint_guess == 0.5


# ── FX-051 cooldown filter contracts ──

def test_C21_excluded_cids_removed_from_eligible():
    """C21: markets in excluded_cids are filtered before ranking — they
    do not appear in result.deploys regardless of how high their score is.

    This is the FX-051 cooldown integration point: DecisionPolicy.get_excluded_cids()
    returns the set of cids the allocator must exclude this cycle.
    """
    a = _make_allocator()
    a.fetch_current_q_shares = lambda: {}
    a.load_cumulative_ratios = lambda: {"0xCOOLED": 0.10, "0xHOT": 0.10}

    # Both should clear all the other filters
    cooled = _make_candidate("0xCOOLED", daily_rate=1000)  # high reward
    hot = _make_candidate("0xHOT", daily_rate=100)         # lower reward
    result = a.compute(
        wallet_usd=2000, wallet_peak_usd=2000, wallet_24h_ago_usd=2000,
        realized_loss_24h=0, markets=[cooled, hot],
        excluded_cids={"0xCOOLED"},
    )
    deploy_cids = {m.condition_id for m in result.deploys}
    assert "0xCOOLED" not in deploy_cids
    assert "0xHOT" in deploy_cids


def test_C22_empty_excluded_cids_is_passthrough():
    """C22: when excluded_cids is None or empty, the allocator behaves
    identically to pre-FX-051 — no filter applied."""
    a = _make_allocator()
    a.fetch_current_q_shares = lambda: {}
    a.load_cumulative_ratios = lambda: {"0xOK": 0.05}
    cand = _make_candidate("0xOK", daily_rate=500)

    r_none = a.compute(
        wallet_usd=500, wallet_peak_usd=500, wallet_24h_ago_usd=500,
        realized_loss_24h=0, markets=[cand], excluded_cids=None,
    )
    r_empty = a.compute(
        wallet_usd=500, wallet_peak_usd=500, wallet_24h_ago_usd=500,
        realized_loss_24h=0, markets=[cand], excluded_cids=set(),
    )
    assert {m.condition_id for m in r_none.deploys} == {"0xOK"}
    assert {m.condition_id for m in r_empty.deploys} == {"0xOK"}


def test_C23_excluded_cids_param_omitted_works():
    """C23: existing callers that don't pass excluded_cids still work
    (backwards-compat contract — Phase 1 callers pre-FX-051)."""
    a = _make_allocator()
    a.fetch_current_q_shares = lambda: {}
    a.load_cumulative_ratios = lambda: {"0xLEGACY": 0.05}
    cand = _make_candidate("0xLEGACY", daily_rate=500)
    # No excluded_cids kwarg
    result = a.compute(
        wallet_usd=500, wallet_peak_usd=500, wallet_24h_ago_usd=500,
        realized_loss_24h=0, markets=[cand],
    )
    assert {m.condition_id for m in result.deploys} == {"0xLEGACY"}


# ── FX-090 adverse-selection / time-to-event filter contracts ──
#
# The allocator ranks by daily_rate × q_share, so the highest-rate markets are
# disproportionately short-dated daily/news/sports markets near a decisive
# event — exactly the ones the farmer's EXPIRY SWEEP / RF_GAME_BLOCK refuse to
# place (→ 0 orders, farming nothing) and that fill adversely. FX-090 excludes
# them UPSTREAM in the allocator. Defaults: RF_ALLOC_MIN_HOURS_TO_RESOLUTION=48,
# RF_ALLOC_MIN_HOURS_TO_GAME_START=12.

from datetime import datetime as _dt, timezone as _tz

_NOW = 1700000000  # matches _make_allocator default _now


def _iso(hours_from_now, now_epoch=_NOW):
    """ISO8601 (…Z) timestamp `hours_from_now` hours from the fixed test clock."""
    return _dt.fromtimestamp(now_epoch + hours_from_now * 3600, tz=_tz.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _alloc_with_q(cid_to_q):
    a = _make_allocator()
    a.fetch_current_q_shares = lambda: {}
    a.load_cumulative_ratios = lambda: dict(cid_to_q)
    return a


def _compute_one(a, candidates):
    return a.compute(
        wallet_usd=2000, wallet_peak_usd=2000, wallet_24h_ago_usd=2000,
        realized_loss_24h=0, markets=candidates,
    )


def test_C24_filters_market_resolving_within_horizon():
    """C24: a market resolving inside RF_ALLOC_MIN_HOURS_TO_RESOLUTION (48h) is
    excluded — this is the daily 'Up or Down on <today>' class."""
    a = _alloc_with_q({"0xSOON": 0.05})
    soon = _make_candidate("0xSOON", daily_rate=500)
    soon.end_date_iso = _iso(10)  # resolves in 10h < 48h floor
    result = _compute_one(a, [soon])
    assert "0xSOON" not in {m.condition_id for m in result.deploys}
    assert "0xSOON" in {m.condition_id for m in result.avoids}


def test_C25_keeps_market_resolving_far_out():
    """C25: a market resolving well beyond the horizon (5 days) is kept."""
    a = _alloc_with_q({"0xFAR": 0.05})
    far = _make_candidate("0xFAR", daily_rate=500)
    far.end_date_iso = _iso(120)  # 5 days out
    result = _compute_one(a, [far])
    assert "0xFAR" in {m.condition_id for m in result.deploys}


def test_C26_filters_already_resolved_market():
    """C26: a market already past its end_date (negative hours) is excluded."""
    a = _alloc_with_q({"0xPAST": 0.05})
    past = _make_candidate("0xPAST", daily_rate=500)
    past.end_date_iso = _iso(-5)  # resolved 5h ago
    result = _compute_one(a, [past])
    assert "0xPAST" not in {m.condition_id for m in result.deploys}


def test_C27_filters_imminent_game_start():
    """C27 (the user's scenario): a sports market whose game starts soon is
    excluded even though resolution is far out — informed flow picks off stale
    quotes once the event begins."""
    a = _alloc_with_q({"0xGAME": 0.05})
    g = _make_candidate("0xGAME", daily_rate=500)
    g.end_date_iso = _iso(120)      # resolution far → resolution axis won't catch it
    g.game_start_time = _iso(2)     # kickoff in 2h < 12h game floor
    result = _compute_one(a, [g])
    assert "0xGAME" not in {m.condition_id for m in result.deploys}


def test_C28_keeps_distant_game():
    """C28: a sports market whose game is days away (and resolves far out) is kept."""
    a = _alloc_with_q({"0xLATER": 0.05})
    g = _make_candidate("0xLATER", daily_rate=500)
    g.end_date_iso = _iso(168)      # a week out
    g.game_start_time = _iso(120)   # game in 5 days > 12h floor
    result = _compute_one(a, [g])
    assert "0xLATER" in {m.condition_id for m in result.deploys}


def test_C29_enriches_via_provider_and_excludes():
    """C29: when a candidate carries no timing, the allocator enriches it via the
    timing provider (prod: CLOB /markets/{cid}) and applies the filter."""
    a = _alloc_with_q({"0xENR": 0.05})
    a._timing_provider = lambda cid: ("", _iso(5))  # resolves in 5h
    cand = _make_candidate("0xENR", daily_rate=500)  # no timing on the candidate
    result = _compute_one(a, [cand])
    assert "0xENR" not in {m.condition_id for m in result.deploys}


def test_C30_fail_open_on_timing_error():
    """C30: if timing can't be obtained (provider raises), the market is NOT
    excluded — fail-open; the farmer's live sweep is the backstop."""
    def boom(cid):
        raise ConnectionError("network down")
    a = _alloc_with_q({"0xFAILOPEN": 0.05})
    a._timing_provider = boom
    cand = _make_candidate("0xFAILOPEN", daily_rate=500)
    result = _compute_one(a, [cand])
    assert "0xFAILOPEN" in {m.condition_id for m in result.deploys}


def test_C31_filter_disabled_via_knobs(monkeypatch):
    """C31: setting both hour-floors to 0 disables the filter — a near-resolution
    market deploys (escape hatch / reversibility)."""
    monkeypatch.setattr(sa, "ALLOC_MIN_HOURS_TO_RESOLUTION", lambda: 0.0)
    monkeypatch.setattr(sa, "ALLOC_MIN_HOURS_TO_GAME_START", lambda: 0.0)
    a = _alloc_with_q({"0xDIS": 0.05})
    cand = _make_candidate("0xDIS", daily_rate=500)
    cand.end_date_iso = _iso(1)  # would be excluded if the filter were on
    result = _compute_one(a, [cand])
    assert "0xDIS" in {m.condition_id for m in result.deploys}


def test_C32_backfills_safe_market_when_top_excluded():
    """C32: the highest-reward market being near-resolution does not waste a
    deploy slot — the allocator walks past it to a safe lower-ranked market."""
    a = _alloc_with_q({"0xTOP": 0.10, "0xSAFE": 0.05})
    top = _make_candidate("0xTOP", daily_rate=1000)   # ranks first (expected=100)
    top.end_date_iso = _iso(3)                        # near resolution → excluded
    safe = _make_candidate("0xSAFE", daily_rate=500)  # expected=25
    safe.end_date_iso = _iso(200)                     # far → safe
    result = _compute_one(a, [top, safe])
    deploys = {m.condition_id for m in result.deploys}
    assert "0xTOP" not in deploys
    assert "0xSAFE" in deploys


def test_C33_alloc_json_carries_timing_when_known():
    """C33: when timing is known, the deploy row carries end_date_iso +
    game_start_time so the farmer doesn't have to re-fetch (and its own
    sweep has the data immediately)."""
    a = _alloc_with_q({"0xT": 0.05})
    cand = _make_candidate("0xT", daily_rate=500)
    cand.end_date_iso = _iso(200)
    result = _compute_one(a, [cand])
    tmp = tempfile.mktemp(suffix=".json")
    a.write_allocation_json(result, output_path=tmp)
    with open(tmp) as f:
        data = json.load(f)
    deploy = next(m for m in data["markets"] if m.get("action") == "deploy")
    assert deploy["end_date_iso"] == cand.end_date_iso
    assert "game_start_time" in deploy
    os.unlink(tmp)
