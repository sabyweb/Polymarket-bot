"""Unit tests for oversight_agent.evaluate (SHADOW mode, v5.1 stage 1).

Verifies:
- Each of the 6 signals fires on its trigger condition.
- Each of the 6 signals stays silent on its negative case.
- evaluate() ALWAYS returns {"action": "continue", "reason": "shadow"}.
- Missing-data inputs do not raise and do not trigger.
- Ring buffer is bounded.
"""

import importlib
import logging

import pytest


@pytest.fixture
def fresh_module():
    """Reload oversight_agent so each test starts with an empty ring buffer."""
    import oversight_agent
    importlib.reload(oversight_agent)
    # Ensure module logger propagates to root so caplog captures it.
    logging.getLogger("oversight").propagate = True
    yield oversight_agent


def _g(**kw) -> dict:
    """Build a guard dict with healthy defaults."""
    base = {
        "kill_switch": False,
        "kill_reason": "",
        "notional_block": False,
        "blocked_clusters": set(),
        "cluster_by_cid": {},
        "cluster_notional": {},
        "live_by_cid": {"cid_a": 100.0},
        "total_live_notional": 100.0,
        "notional_ratio": 1.0,
        "total_capital": 2000.0,
        "cf": 0.5,
        "daily_loss": 0.0,
        "orders_placed_prev_cycle": 5,
        "orders_cancelled_prev_cycle": 1,
    }
    base.update(kw)
    return base


def test_returns_continue_shadow(fresh_module):
    assert fresh_module.evaluate(_g()) == {"action": "continue", "reason": "shadow"}


def test_evaluate_never_raises_on_empty_guard(fresh_module):
    assert fresh_module.evaluate({}) == {"action": "continue", "reason": "shadow"}


def test_signal_a_notional_drift_fires(fresh_module, caplog):
    caplog.set_level(logging.WARNING, logger="oversight")
    for _ in range(5):
        fresh_module.evaluate(_g(notional_ratio=1.85))
    assert any("signal=notional_drift" in r.message and "triggered=True" in r.message
               for r in caplog.records)


def test_signal_a_silent_below_threshold(fresh_module, caplog):
    caplog.set_level(logging.WARNING, logger="oversight")
    for _ in range(5):
        fresh_module.evaluate(_g(notional_ratio=1.5))
    assert not any("signal=notional_drift" in r.message and "triggered=True" in r.message
                   for r in caplog.records)


def test_signal_b_cluster_breadth_fires(fresh_module, caplog):
    caplog.set_level(logging.WARNING, logger="oversight")
    for _ in range(3):
        fresh_module.evaluate(_g(blocked_clusters={1, 2}))
    assert any("signal=cluster_breadth" in r.message and "triggered=True" in r.message
               for r in caplog.records)


def test_signal_c_cf_soft_zone_fires(fresh_module, caplog):
    caplog.set_level(logging.WARNING, logger="oversight")
    for _ in range(5):
        fresh_module.evaluate(_g(cf=0.02))
    assert any("signal=cf_soft_zone" in r.message and "triggered=True" in r.message
               for r in caplog.records)


def test_signal_d_cancel_pressure_fires(fresh_module, caplog):
    caplog.set_level(logging.WARNING, logger="oversight")
    for _ in range(6):
        fresh_module.evaluate(
            _g(orders_placed_prev_cycle=3, orders_cancelled_prev_cycle=8)
        )
    assert any("signal=cancel_pressure" in r.message and "triggered=True" in r.message
               for r in caplog.records)


def test_signal_e_cf_trajectory_fires(fresh_module, caplog):
    caplog.set_level(logging.WARNING, logger="oversight")
    cfs = [0.5, 0.45, 0.4, 0.35, 0.3, 0.25, 0.2, 0.15, 0.1, 0.05]
    deployed = [40, 38, 35, 33, 30, 28, 25, 22, 18, 15]
    for cf, dep in zip(cfs, deployed):
        live = {f"c{i}": 1.0 for i in range(dep)}
        fresh_module.evaluate(_g(cf=cf, live_by_cid=live))
    msgs = [r.message for r in caplog.records]
    assert any("signal=cf_trajectory" in m and "triggered=True" in m for m in msgs)
    assert any("would_kill=True" in m for m in msgs)


def test_signal_f_slow_bleed_fires(fresh_module, caplog):
    caplog.set_level(logging.WARNING, logger="oversight")
    # daily_loss = 120, total_capital = 2000 → frac = 0.06 > 0.05 threshold
    for _ in range(6):
        fresh_module.evaluate(_g(daily_loss=120.0, total_capital=2000.0))
    assert any("signal=slow_bleed" in r.message and "triggered=True" in r.message
               for r in caplog.records)


def test_healthy_regime_silent(fresh_module, caplog):
    caplog.set_level(logging.WARNING, logger="oversight")
    for _ in range(20):
        fresh_module.evaluate(_g())
    triggered = [r for r in caplog.records
                 if "OVERSIGHT_SHADOW" in r.message and "triggered=True" in r.message]
    assert triggered == []


def test_missing_cf_no_false_positive(fresh_module, caplog):
    caplog.set_level(logging.WARNING, logger="oversight")
    for _ in range(15):
        fresh_module.evaluate(_g(cf=None))
    assert not any(("signal=cf_soft_zone" in r.message
                    or "signal=cf_trajectory" in r.message)
                   and "triggered=True" in r.message
                   for r in caplog.records)


def test_ring_buffer_bounded(fresh_module):
    for _ in range(100):
        fresh_module.evaluate(_g())
    assert len(fresh_module._GUARD_HISTORY) == fresh_module._HISTORY_LEN


def test_returns_continue_even_when_signals_fire(fresh_module):
    result = None
    for _ in range(5):
        result = fresh_module.evaluate(_g(notional_ratio=2.0))
    assert result == {"action": "continue", "reason": "shadow"}


def test_summary_line_emitted_on_trigger(fresh_module, caplog):
    caplog.set_level(logging.WARNING, logger="oversight")
    for _ in range(5):
        fresh_module.evaluate(_g(notional_ratio=1.85))
    assert any("would_pause=True" in r.message for r in caplog.records)


def test_kill_summary_line_on_cf_trajectory(fresh_module, caplog):
    caplog.set_level(logging.WARNING, logger="oversight")
    cfs = [0.5, 0.45, 0.4, 0.35, 0.3, 0.25, 0.2, 0.15, 0.1, 0.05]
    deployed = [40, 38, 35, 33, 30, 28, 25, 22, 18, 15]
    for cf, dep in zip(cfs, deployed):
        live = {f"c{i}": 1.0 for i in range(dep)}
        fresh_module.evaluate(_g(cf=cf, live_by_cid=live))
    assert any("would_kill=True" in r.message for r in caplog.records)
