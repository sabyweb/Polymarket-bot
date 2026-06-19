"""Regression guard for the Halt-Doctor diagnosis classifier (safety-adjacent — must not drift).

Verdicts must stay fail-safe: only a RECENT trip that is POSITIVELY contradicted by authoritative
ground truth may be FALSE_POSITIVE (the only auto-recovery-eligible verdict). Everything else escalates.
"""
from ab.halt_diagnose import parse_kill_reason, classify_drawdown

REASON = "oversight:drawdown 20.1% > 20% from peak $1220.52 (portfolio=$974.87, cash=$974.78)"


def test_parse_real_drawdown_reason():
    p = parse_kill_reason(REASON)
    assert p["kill_type"] == "drawdown"
    assert abs(p["threshold_pct"] - 20.0) < 1e-9
    assert abs(p["peak"] - 1220.52) < 1e-6
    assert abs(p["portfolio"] - 974.87) < 1e-6


def test_live_halt_is_real_resolved():
    claimed = dict(threshold_pct=20.0, peak=1220.52, portfolio=974.87)
    assert classify_drawdown(claimed, dict(portfolio=985.15, peak=1220.52), 233000)[0] == "REAL_RESOLVED"


def test_db_miss_recent_is_false_positive():
    claimed = dict(threshold_pct=20.0, peak=1220.52, portfolio=970.0)
    assert classify_drawdown(claimed, dict(portfolio=1050.0, peak=1220.52), 300)[0] == "FALSE_POSITIVE"


def test_still_breached_is_real_active():
    claimed = dict(threshold_pct=20.0, peak=1220.52, portfolio=970.0)
    assert classify_drawdown(claimed, dict(portfolio=950.0, peak=1220.52), 300)[0] == "REAL_ACTIVE"


def test_stale_trip_never_false_positive():
    # same DB-miss numerics but the trip is old => cannot trust claimed-vs-current => NOT auto-recoverable
    claimed = dict(threshold_pct=20.0, peak=1220.52, portfolio=970.0)
    assert classify_drawdown(claimed, dict(portfolio=1050.0, peak=1220.52), 200000)[0] == "REAL_RESOLVED"


def test_missing_authoritative_is_uncertain():
    claimed = dict(threshold_pct=20.0, peak=1220.52, portfolio=970.0)
    assert classify_drawdown(claimed, dict(portfolio=None, peak=None), 300)[0] == "UNCERTAIN"
