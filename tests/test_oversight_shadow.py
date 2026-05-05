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


# ═════════════════════════════════════════════════════════════════════
# Phase C — Stage 2/3 promotion: action mapping
#
# These tests flip _SHADOW_ONLY=False and selectively enable
# _PAUSE_ENABLED / _KILL_ENABLED to verify evaluate() produces the
# correct action when signals fire under each promotion configuration.
# fresh_module reload ensures each test starts from default flag state
# (Stage 1, all gating off).
# ═════════════════════════════════════════════════════════════════════


def _trigger_kill_sequence(fresh_module, **extra_guard):
    """Helper: feed 10 cycles that satisfy cf_trajectory's trigger.
    Optional extra_guard fields (e.g. notional_ratio=2.0) layered on
    top of the cf/deployed pattern."""
    cfs = [0.5, 0.45, 0.4, 0.35, 0.3, 0.25, 0.2, 0.15, 0.1, 0.05]
    deployed = [40, 38, 35, 33, 30, 28, 25, 22, 18, 15]
    result = None
    for cf, dep in zip(cfs, deployed):
        live = {f"c{i}": 1.0 for i in range(dep)}
        result = fresh_module.evaluate(_g(cf=cf, live_by_cid=live, **extra_guard))
    return result


def test_pause_returned_on_notional_drift_when_promoted(fresh_module):
    fresh_module._SHADOW_ONLY = False
    fresh_module._PAUSE_ENABLED = True
    result = None
    for _ in range(5):
        result = fresh_module.evaluate(_g(notional_ratio=1.85))
    assert result["action"] == "pause"
    assert "notional_drift" in result["reason"]


def test_pause_returned_on_cluster_breadth_when_promoted(fresh_module):
    fresh_module._SHADOW_ONLY = False
    fresh_module._PAUSE_ENABLED = True
    result = None
    for _ in range(3):
        result = fresh_module.evaluate(_g(blocked_clusters={1, 2}))
    assert result["action"] == "pause"
    assert "cluster_breadth" in result["reason"]


def test_pause_returned_on_cf_soft_zone_when_promoted(fresh_module):
    fresh_module._SHADOW_ONLY = False
    fresh_module._PAUSE_ENABLED = True
    result = None
    for _ in range(5):
        result = fresh_module.evaluate(_g(cf=0.02))
    assert result["action"] == "pause"
    assert "cf_soft_zone" in result["reason"]


def test_pause_returned_on_cancel_pressure_when_promoted(fresh_module):
    fresh_module._SHADOW_ONLY = False
    fresh_module._PAUSE_ENABLED = True
    result = None
    for _ in range(6):
        result = fresh_module.evaluate(
            _g(orders_placed_prev_cycle=3, orders_cancelled_prev_cycle=8)
        )
    assert result["action"] == "pause"
    assert "cancel_pressure" in result["reason"]


def test_pause_returned_on_slow_bleed_when_promoted(fresh_module):
    fresh_module._SHADOW_ONLY = False
    fresh_module._PAUSE_ENABLED = True
    result = None
    for _ in range(6):
        result = fresh_module.evaluate(_g(daily_loss=120.0, total_capital=2000.0))
    assert result["action"] == "pause"
    assert "slow_bleed" in result["reason"]


def test_kill_returned_on_cf_trajectory_when_promoted(fresh_module):
    fresh_module._SHADOW_ONLY = False
    fresh_module._KILL_ENABLED = True
    result = _trigger_kill_sequence(fresh_module)
    assert result["action"] == "kill"
    assert "cf_trajectory" in result["reason"]


def test_kill_overrides_pause_when_both_fire(fresh_module):
    """Strict severity — when both a would_kill (cf_trajectory) and a
    would_pause (notional_drift) signal fire, kill wins."""
    fresh_module._SHADOW_ONLY = False
    fresh_module._PAUSE_ENABLED = True
    fresh_module._KILL_ENABLED = True
    # cf_trajectory's 10-cycle window also satisfies notional_drift's
    # 5-cycle window since notional_ratio=2.0 ≥ 1.8 in every cycle.
    result = _trigger_kill_sequence(fresh_module, notional_ratio=2.0)
    assert result["action"] == "kill"
    assert "cf_trajectory" in result["reason"]


def test_continue_no_signal_when_master_off_and_healthy(fresh_module):
    fresh_module._SHADOW_ONLY = False
    fresh_module._PAUSE_ENABLED = True
    fresh_module._KILL_ENABLED = True
    result = None
    for _ in range(20):
        result = fresh_module.evaluate(_g())
    assert result == {"action": "continue", "reason": "no_signal"}


def test_pause_reason_includes_signal_name(fresh_module):
    fresh_module._SHADOW_ONLY = False
    fresh_module._PAUSE_ENABLED = True
    result = None
    for _ in range(5):
        result = fresh_module.evaluate(_g(notional_ratio=1.85))
    assert result["action"] == "pause"
    assert "notional_drift" in result["reason"]


def test_kill_reason_includes_signal_name(fresh_module):
    fresh_module._SHADOW_ONLY = False
    fresh_module._KILL_ENABLED = True
    result = _trigger_kill_sequence(fresh_module)
    assert result["action"] == "kill"
    assert "cf_trajectory" in result["reason"]


def test_reason_within_200_chars(fresh_module):
    """Defensive 200-char slice on the reason string. Worst-case real
    signal names total ~80 chars so the slice never truncates today,
    but the slice operation must not raise on any string."""
    fresh_module._SHADOW_ONLY = False
    fresh_module._PAUSE_ENABLED = True
    result = None
    for _ in range(5):
        result = fresh_module.evaluate(_g(notional_ratio=1.85))
    assert isinstance(result["reason"], str)
    assert len(result["reason"]) <= 200


def test_evaluate_handles_malformed_signal_returns(fresh_module, caplog):
    """Per-signal try/except hardening (Phase C decision C6). One bad
    detector logs an error but does NOT suppress the remaining detectors,
    nor does it raise out of evaluate()."""
    caplog.set_level(logging.ERROR, logger="oversight")

    def bad_signal_fn():
        raise RuntimeError("synthetic detector failure")

    # Inject a bad would_pause signal alongside the real six.
    fresh_module._SIGNALS = list(fresh_module._SIGNALS) + [
        ("bad_signal", bad_signal_fn, "would_pause"),
    ]
    fresh_module._SHADOW_ONLY = False
    fresh_module._PAUSE_ENABLED = True

    result = None
    for _ in range(5):
        result = fresh_module.evaluate(_g(notional_ratio=1.85))

    # The bad signal logged an error.
    assert any("[OVERSIGHT_SHADOW_ERROR]" in r.message
               and "bad_signal" in r.message
               for r in caplog.records)
    # The real notional_drift signal still produced its action.
    assert result["action"] == "pause"
    assert "notional_drift" in result["reason"]


def test_check_signals_returns_lists(fresh_module):
    """Signature change: _check_signals_and_log now returns
    (fired_pause, fired_kill). Smoke test that the return shape is
    correct in both healthy and triggered states."""
    healthy = fresh_module._check_signals_and_log(_g())
    assert healthy == ([], [])

    # Force the ring buffer with notional_drift triggers.
    for _ in range(5):
        fresh_module._GUARD_HISTORY.append(
            fresh_module._snapshot(_g(notional_ratio=1.85))
        )
    fired = fresh_module._check_signals_and_log(_g(notional_ratio=1.85))
    assert isinstance(fired, tuple) and len(fired) == 2
    assert "notional_drift" in fired[0]   # would_pause list
    assert fired[1] == []                 # would_kill list empty


# ═════════════════════════════════════════════════════════════════════
# Phase C — promotion-flag isolation
#
# These tests fix one or more flags to specific non-default values to
# verify each flag is independently honoured. The full 2x2x2 flag
# matrix isn't enumerated — only the cells that exercise distinct
# code paths in evaluate().
# ═════════════════════════════════════════════════════════════════════


def test_pause_disabled_returns_continue_even_when_signal_fires(fresh_module):
    """Master gate off, _PAUSE_ENABLED=False, _KILL_ENABLED=False: a
    would_pause signal still fires (logged) but evaluate returns
    continue/no_signal because no promotion flag activates the action."""
    fresh_module._SHADOW_ONLY = False
    fresh_module._PAUSE_ENABLED = False
    fresh_module._KILL_ENABLED = False
    result = None
    for _ in range(5):
        result = fresh_module.evaluate(_g(notional_ratio=1.85))
    assert result == {"action": "continue", "reason": "no_signal"}


def test_kill_signal_falls_to_pause_when_kill_disabled(fresh_module):
    """Phase C decision C3: when _KILL_ENABLED=False but _PAUSE_ENABLED=True,
    a fired would_kill signal falls through to pause action — preserves
    safety intent (skip placements while operator investigates) without
    escalating to terminal kill state."""
    fresh_module._SHADOW_ONLY = False
    fresh_module._PAUSE_ENABLED = True
    fresh_module._KILL_ENABLED = False
    result = _trigger_kill_sequence(fresh_module)
    assert result["action"] == "pause"
    assert "cf_trajectory" in result["reason"]


def test_master_shadow_only_overrides_promotion_flags(fresh_module):
    """_SHADOW_ONLY=True is the master gate — even with both promotion
    flags True and signals firing, evaluate returns continue/shadow.
    This is the single-flag revert path: flipping master back to True
    restores Stage 1 behaviour without code change."""
    fresh_module._SHADOW_ONLY = True
    fresh_module._PAUSE_ENABLED = True
    fresh_module._KILL_ENABLED = True
    result = None
    for _ in range(5):
        result = fresh_module.evaluate(_g(notional_ratio=1.85))
    assert result == {"action": "continue", "reason": "shadow"}


def test_kill_enabled_without_pause_still_acts_on_kill_signals(fresh_module):
    """_KILL_ENABLED=True alone (without _PAUSE_ENABLED=True) is a valid
    state: would_kill signals translate to kill, would_pause signals
    are silently ignored (would fall to no_signal). Permits a
    promotion path that activates Stage 3 directly without Stage 2 if
    the operator chooses."""
    fresh_module._SHADOW_ONLY = False
    fresh_module._PAUSE_ENABLED = False
    fresh_module._KILL_ENABLED = True

    # Kill signal still fires its action.
    result = _trigger_kill_sequence(fresh_module)
    assert result["action"] == "kill"
    assert "cf_trajectory" in result["reason"]


def test_pause_signal_with_kill_only_promotion_returns_continue(fresh_module):
    """_KILL_ENABLED=True, _PAUSE_ENABLED=False: a would_pause signal
    fires but evaluate returns continue/no_signal — no action because
    pause flag is off and the kill flag is irrelevant for would_pause
    signals."""
    fresh_module._SHADOW_ONLY = False
    fresh_module._PAUSE_ENABLED = False
    fresh_module._KILL_ENABLED = True
    result = None
    for _ in range(5):
        result = fresh_module.evaluate(_g(notional_ratio=1.85))
    assert result == {"action": "continue", "reason": "no_signal"}
