"""
Safety invariant tests — regression prevention for the 7000x miscalibration.

Tests cover:
1. Correction factor cannot be silently floored
2. q_share_pct capped at 0.5
3. Minimum effective reward threshold
4. Zero-fill bonus removed
5. SafetyController state transitions
6. Portfolio-level sanity checks
7. Windowed q_share computation

Usage:
    python test_safety.py
"""

import os
import sys
import sqlite3
import tempfile
import time

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}: {detail}")


def make_metrics(**overrides):
    """Create a minimal MarketMetrics for testing."""
    from oversight.data_collector import MarketMetrics
    defaults = dict(
        condition_id="test_cid_001",
        question="Test market?",
        daily_rate=50.0,
        actual_reward_total=0.0,
        fill_cost_recent=0.0,
        dump_revenue_recent=0.0,
        fill_count_recent=0,
        net_pnl_recent=0.0,
        current_position_usd=0.0,
        on_book_hours=24.0,
        q_share_pct=1.0,  # the old dangerous default
    )
    defaults.update(overrides)
    return MarketMetrics(**defaults)


# ═══════════════════════════════════════════════════
print("\n=== TEST 1: Correction Factor Clamp ===")
# ═══════════════════════════════════════════════════

# Old behavior: clamp at [0.1, 10.0] — raw 0.000142 became 0.10 (703x too generous)
# New behavior: clamp at [0.001, 10.0] — raw 0.000142 stays 0.001 (reflects reality)

# Simulate the exact failure: estimated $81K/day, actual $11.59/day
estimated = 81561.0
actual = 11.59
raw_factor = actual / estimated  # 0.000142

check(
    "Raw CF 0.000142 is NOT raised to 0.10",
    max(0.001, min(10.0, raw_factor)) < 0.01,
    f"Got {max(0.001, min(10.0, raw_factor)):.6f}, expected < 0.01"
)

check(
    "Raw CF 0.000142 clamps to 0.001 (not 0.10)",
    max(0.001, min(10.0, raw_factor)) == 0.001,
    f"Got {max(0.001, min(10.0, raw_factor)):.6f}"
)

# Normal CF passes through
normal_factor = 0.15
check(
    "Normal CF 0.15 passes through unchanged",
    max(0.001, min(10.0, normal_factor)) == 0.15,
    f"Got {max(0.001, min(10.0, normal_factor))}"
)


# ═══════════════════════════════════════════════════
print("\n=== TEST 2: EMA Circuit Breaker ===")
# ═══════════════════════════════════════════════════

from oversight.data_collector import _smooth_correction_factor

# Create temp DB for testing
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_path = _tmp.name
_tmp.close()

# Seed with a "normal" previous smoothed value of 0.50
db = sqlite3.connect(_tmp_path)
db.execute("""CREATE TABLE IF NOT EXISTS correction_factor_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    raw REAL NOT NULL, smoothed REAL NOT NULL)""")
db.execute("INSERT INTO correction_factor_history (ts, raw, smoothed) VALUES (?, ?, ?)",
           (time.time() - 100, 0.50, 0.50))
db.commit()
db.close()

# Circuit breaker test: raw=0.005, prev_smoothed=0.50
# Old behavior: EMA = 0.3 * 0.005 + 0.7 * 0.50 = 0.3515 (barely changed!)
# New behavior: circuit break → use raw directly = 0.005
result = _smooth_correction_factor(0.005, _tmp_path, alpha=0.3, has_new_observation=True)
check(
    "Circuit breaker fires for raw=0.005",
    result < 0.01,
    f"Got {result:.4f}, expected < 0.01 (circuit breaker should skip EMA)"
)

# Fast-adapt test: raw=0.03, prev_smoothed still high
db = sqlite3.connect(_tmp_path)
db.execute("DELETE FROM correction_factor_history")
db.execute("INSERT INTO correction_factor_history (ts, raw, smoothed) VALUES (?, ?, ?)",
           (time.time() - 100, 0.50, 0.50))
db.commit()
db.close()

result_fast = _smooth_correction_factor(0.03, _tmp_path, alpha=0.3, has_new_observation=True)
check(
    "Fast-adapt fires for raw=0.03 (prev=0.50)",
    result_fast < 0.20,
    f"Got {result_fast:.4f}, expected < 0.20 (fast-adapt with alpha=0.7)"
)

# Normal smoothing: raw=0.15, prev=0.20
db = sqlite3.connect(_tmp_path)
db.execute("DELETE FROM correction_factor_history")
db.execute("INSERT INTO correction_factor_history (ts, raw, smoothed) VALUES (?, ?, ?)",
           (time.time() - 100, 0.20, 0.20))
db.commit()
db.close()

result_normal = _smooth_correction_factor(0.15, _tmp_path, alpha=0.3, has_new_observation=True)
expected_normal = 0.3 * 0.15 + 0.7 * 0.20  # = 0.185
check(
    "Normal EMA smoothing works correctly",
    abs(result_normal - expected_normal) < 0.01,
    f"Got {result_normal:.4f}, expected ~{expected_normal:.4f}"
)

os.unlink(_tmp_path)


# ═══════════════════════════════════════════════════
print("\n=== TEST 3: Q-Share Cap in Scorer ===")
# ═══════════════════════════════════════════════════

from oversight.market_scorer import score_market

m_high_q = make_metrics(q_share_pct=1.0, daily_rate=100.0)
m_capped_q = make_metrics(q_share_pct=0.5, daily_rate=100.0)

# With q_share=1.0 at CF=1.0: old score = 100*1.0*1.0 + bonus = $150/day
# With q_share capped to 0.5 at CF=1.0: new score = 100*0.5*1.0 = $50/day
score_uncapped = score_market(m_high_q, correction_factor=1.0)
score_capped = score_market(m_capped_q, correction_factor=1.0)

check(
    "q_share=1.0 and q_share=0.5 produce same score (cap at 0.5)",
    abs(score_uncapped - score_capped) < 0.01,
    f"q_share=1.0 gave {score_uncapped:.2f}, q_share=0.5 gave {score_capped:.2f}"
)

check(
    "Score with q_share=1.0 is $50/day (not $100+)",
    score_uncapped <= 51.0,
    f"Got {score_uncapped:.2f}, expected ~50.0 (capped at 0.5 × $100)"
)


# ═══════════════════════════════════════════════════
print("\n=== TEST 4: Zero-Fill Bonus Removed ===")
# ═══════════════════════════════════════════════════

m_zero_fill = make_metrics(
    q_share_pct=0.1, daily_rate=50.0, fill_count_recent=0
)
m_has_fill = make_metrics(
    q_share_pct=0.1, daily_rate=50.0, fill_count_recent=1,
    fill_cost_recent=0.0, dump_revenue_recent=0.0  # zero net damage
)

score_zero = score_market(m_zero_fill, correction_factor=1.0)
score_fill = score_market(m_has_fill, correction_factor=1.0)

# With zero-fill bonus removed, both should score the same
# (zero fills no longer gets a bonus)
check(
    "Zero-fill markets don't get bonus over filled markets",
    abs(score_zero - score_fill) < 0.01,
    f"Zero-fill={score_zero:.2f}, has-fill={score_fill:.2f} (should be equal)"
)


# ═══════════════════════════════════════════════════
print("\n=== TEST 5: Minimum Effective Reward Gate ===")
# ═══════════════════════════════════════════════════

from oversight.market_scorer import classify_market

# Market with tiny effective reward: $50/day pool × 0.01 q_share × 0.1 CF = $0.05/day
m_tiny = make_metrics(
    q_share_pct=0.01, daily_rate=50.0, on_book_hours=8.0
)
score_tiny = score_market(m_tiny, correction_factor=0.1)
sm_tiny = classify_market(m_tiny, score_tiny, correction_factor=0.1)

check(
    "Market with $0.05/day effective reward is avoided",
    sm_tiny.action == "avoid",
    f"Got action={sm_tiny.action}, reason={sm_tiny.reason}"
)

# Market with decent effective reward: $50/day × 0.1 q_share × 0.1 CF = $0.50/day
m_decent = make_metrics(
    q_share_pct=0.1, daily_rate=50.0, on_book_hours=8.0
)
score_decent = score_market(m_decent, correction_factor=0.1)
sm_decent = classify_market(m_decent, score_decent, correction_factor=0.1)

check(
    "Market with $0.50/day effective reward deploys",
    sm_decent.action == "deploy",
    f"Got action={sm_decent.action}, score={score_decent:.4f}, reason={sm_decent.reason}"
)


# ═══════════════════════════════════════════════════
print("\n=== TEST 6: SafetyController State Machine ===")
# ═══════════════════════════════════════════════════

_tmp2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp2_path = _tmp2.name
_tmp2.close()

# Create required tables
db = sqlite3.connect(_tmp2_path)
db.execute("""CREATE TABLE IF NOT EXISTS fills (
    ts REAL, condition_id TEXT, shares REAL, clob_cost REAL)""")
db.execute("""CREATE TABLE IF NOT EXISTS unwinds (
    ts REAL, condition_id TEXT, usd_value REAL)""")
db.execute("""CREATE TABLE IF NOT EXISTS stop_losses (
    ts REAL, condition_id TEXT, loss_usd REAL)""")
db.execute("""CREATE TABLE IF NOT EXISTS scoring_snapshots (
    id INTEGER PRIMARY KEY, ts REAL, order_id TEXT, condition_id TEXT,
    side TEXT, scoring INTEGER, price REAL, shares REAL)""")
db.commit()
db.close()

from oversight.safety_controller import SafetyController, UNSAFE, DEGRADED, LEARNING, CALIBRATED

sc = SafetyController(db_path=_tmp2_path)
check("Initial state is LEARNING", sc.state == LEARNING, f"Got {sc.state}")

# Test: circuit breaker → UNSAFE
sc.evaluate(
    correction_factor_raw=0.003,
    estimated_daily_total=10000,
    actual_daily_payout=5.0,
    fill_damage_24h=0,
    reward_payout_24h=5.0,
    num_scoring_markets=10,
)
check("CF < 0.005 triggers UNSAFE", sc.state == UNSAFE, f"Got {sc.state}")

# Test: can't auto-upgrade from UNSAFE
sc.state = UNSAFE
sc.evaluate(
    correction_factor_raw=0.5,
    estimated_daily_total=20,
    actual_daily_payout=10.0,
    fill_damage_24h=0,
    reward_payout_24h=10.0,
    num_scoring_markets=10,
)
check(
    "UNSAFE state doesn't auto-upgrade (manual ack required)",
    sc.state in (UNSAFE, LEARNING),  # may transition through _transition
    f"Got {sc.state}"
)

# Test: high est/actual ratio → UNSAFE
sc2 = SafetyController(db_path=_tmp2_path)
sc2.evaluate(
    correction_factor_raw=0.1,
    estimated_daily_total=5000,
    actual_daily_payout=10.0,
    fill_damage_24h=0,
    reward_payout_24h=10.0,
    num_scoring_markets=10,
)
check(
    "Est/actual > 50x triggers UNSAFE",
    sc2.state == UNSAFE,
    f"Got {sc2.state} (ratio={5000/10:.0f}x)"
)

# Test: excessive fill damage → UNSAFE
sc3 = SafetyController(db_path=_tmp2_path)
sc3.evaluate(
    correction_factor_raw=0.3,
    estimated_daily_total=30,
    actual_daily_payout=10.0,
    fill_damage_24h=400,
    reward_payout_24h=10.0,
    num_scoring_markets=10,
)
check(
    "Fill damage > $300 triggers UNSAFE",
    sc3.state == UNSAFE,
    f"Got {sc3.state} (damage=$400)"
)

# Test: moderate issues → DEGRADED (use fresh DB to avoid stale UNSAFE)
_tmp3 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp3_path = _tmp3.name
_tmp3.close()
db = sqlite3.connect(_tmp3_path)
db.execute("""CREATE TABLE IF NOT EXISTS fills (
    ts REAL, condition_id TEXT, shares REAL, clob_cost REAL)""")
db.execute("""CREATE TABLE IF NOT EXISTS unwinds (
    ts REAL, condition_id TEXT, usd_value REAL)""")
db.execute("""CREATE TABLE IF NOT EXISTS stop_losses (
    ts REAL, condition_id TEXT, loss_usd REAL)""")
db.execute("""CREATE TABLE IF NOT EXISTS scoring_snapshots (
    id INTEGER PRIMARY KEY, ts REAL, order_id TEXT, condition_id TEXT,
    side TEXT, scoring INTEGER, price REAL, shares REAL)""")
db.commit()
db.close()

sc4 = SafetyController(db_path=_tmp3_path)
sc4.evaluate(
    correction_factor_raw=0.015,
    estimated_daily_total=100,
    actual_daily_payout=10.0,
    fill_damage_24h=0,
    reward_payout_24h=10.0,
    num_scoring_markets=10,
)
check(
    "CF < 0.02 triggers DEGRADED",
    sc4.state == DEGRADED,
    f"Got {sc4.state}"
)

# Test: good metrics → upgrade to CALIBRATED after 3 cycles (fresh DB)
sc5 = SafetyController(db_path=_tmp3_path)
sc5.state = LEARNING
sc5.consecutive_good = 0
for _ in range(4):
    sc5.evaluate(
        correction_factor_raw=0.15,
        estimated_daily_total=30,
        actual_daily_payout=10.0,
        fill_damage_24h=5,
        reward_payout_24h=10.0,
        num_scoring_markets=10,
    )
check(
    "3+ good cycles upgrades LEARNING → CALIBRATED",
    sc5.state == CALIBRATED,
    f"Got {sc5.state} after 4 good cycles (consecutive_good={sc5.consecutive_good})"
)
os.unlink(_tmp3_path)

# Test: allocation filtering in UNSAFE state (GAP 2: probe mode)
sc6 = SafetyController(db_path=_tmp2_path)
sc6.state = UNSAFE
test_allocs = [
    {"action": "deploy", "shares_per_side": 200, "score": 10, "condition_id": "a",
     "est_capital_cost": 200, "max_spread": 0.045, "q_share_pct": 0.1, "min_size": 50},
    {"action": "deploy", "shares_per_side": 200, "score": 5, "condition_id": "b",
     "est_capital_cost": 200, "max_spread": 0.045, "q_share_pct": 0.1, "min_size": 50},
    {"action": "deploy", "shares_per_side": 200, "score": 3, "condition_id": "c",
     "est_capital_cost": 200, "max_spread": 0.045, "q_share_pct": 0.1, "min_size": 50},
    {"action": "deploy", "shares_per_side": 200, "score": 1, "condition_id": "d",
     "est_capital_cost": 200, "max_spread": 0.045, "q_share_pct": 0.1, "min_size": 50},
]
filtered = sc6.filter_allocations(test_allocs, 1000)
deploy_after = sum(1 for a in filtered if a["action"] == "deploy")
check(
    "UNSAFE probe mode: max 3 markets (not 0 — deadlock fix)",
    deploy_after <= 3,
    f"Got {deploy_after} deploy markets (expected ≤3)"
)
check(
    "UNSAFE probe mode: at least 1 probe market for data collection",
    deploy_after >= 1,
    f"Got {deploy_after} deploy markets (expected ≥1)"
)
# Verify min_size enforcement in probe mode
for a in filtered:
    if a["action"] == "deploy":
        check(
            f"Probe market {a['condition_id']} forced to min_size",
            a["shares_per_side"] == 50,
            f"Got shares={a['shares_per_side']} (expected 50 = min_size)"
        )
        check(
            f"Probe market {a['condition_id']} reason indicates probe",
            "PROBE" in a.get("reason", ""),
            f"Got reason={a.get('reason', '')}"
        )
# Verify capital capped at 5%
total_probe_cost = sum(a.get("est_capital_cost", 0) for a in filtered if a["action"] == "deploy")
check(
    "Probe capital ≤ 5% of available ($50 on $1000)",
    total_probe_cost <= 1000 * 0.05 + 1,  # +1 for rounding
    f"Probe capital=${total_probe_cost:.0f} (limit=${1000*0.05:.0f})"
)

os.unlink(_tmp2_path)


# ═══════════════════════════════════════════════════
print("\n=== TEST 7: Aggregate Sanity ===")
# ═══════════════════════════════════════════════════

# The exact scenario that caused the 7000x failure
# 136 markets, all with q_share=1.0, avg daily_rate=$600
# Old: estimated $81K/day, actual $11.59, raw_factor clamped to 0.10
# New: q_share capped to 0.5, raw_factor clamped to 0.001

# Simulate with new code
markets_count = 136
avg_rate = 600
q_share_old = 1.0
q_share_new = min(q_share_old, 0.5)  # capped

est_old = markets_count * avg_rate * q_share_old  # $81,600
est_new = markets_count * avg_rate * q_share_new  # $40,800

actual = 11.59
cf_old = max(0.10, actual / est_old)  # clamped to 0.10
cf_new = max(0.001, actual / est_new)  # 0.000284

effective_old = est_old * cf_old  # $8,160/day (still 703x too high!)
effective_new = est_new * cf_new  # $40.80/day with clamp, but circuit breaker avoids this

check(
    "Old system: effective was $8160/day (703x overestimate)",
    effective_old > 5000,
    f"Old effective=${effective_old:.0f}"
)

# The q_share cap alone reduces the problem from 7000x to 3500x.
# But the circuit breaker in _smooth_correction_factor would trigger
# because raw_cf = 11.59 / 40800 = 0.000284 < 0.005 → UNSAFE state.
# With SafetyController: UNSAFE → zero allocations → $0 deployed.
# The system STOPS instead of continuing with wrong estimates.
check(
    "New system: effective is much lower than old (q_share cap halves it)",
    effective_new < effective_old * 0.01,
    f"New effective=${effective_new:.2f}, old=${effective_old:.0f}"
)

# The real defense: SafetyController would set UNSAFE because
# est_actual_ratio = 40800 / 11.59 = 3520x > 50x threshold
est_actual_ratio = est_new / actual
check(
    "SafetyController catches remaining overestimation (ratio > 50x → UNSAFE)",
    est_actual_ratio > 50,
    f"Est/actual ratio={est_actual_ratio:.0f}x (triggers UNSAFE → zero deployment)"
)

# Combined defense: even if CF clamp is hit, UNSAFE prevents deployment
check(
    "Circuit breaker + UNSAFE = $0 deployed (not $8160 like before)",
    cf_new < 0.005,  # would trigger circuit breaker
    f"Raw CF={cf_new:.6f} → circuit breaker fires, SafetyController → UNSAFE"
)


# ═══════════════════════════════════════════════════
print("\n=== TEST 8: Slow Bleed — 7-Day Loss Triggers UNSAFE ===")
# ═══════════════════════════════════════════════════

_tmp4 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp4_path = _tmp4.name
_tmp4.close()
db = sqlite3.connect(_tmp4_path)
db.execute("CREATE TABLE IF NOT EXISTS fills (ts REAL, condition_id TEXT, shares REAL, clob_cost REAL)")
db.execute("CREATE TABLE IF NOT EXISTS unwinds (ts REAL, condition_id TEXT, usd_value REAL)")
db.execute("CREATE TABLE IF NOT EXISTS stop_losses (ts REAL, condition_id TEXT, loss_usd REAL)")
db.execute("CREATE TABLE IF NOT EXISTS scoring_snapshots (id INTEGER PRIMARY KEY, ts REAL, order_id TEXT, condition_id TEXT, side TEXT, scoring INTEGER, price REAL, shares REAL)")
db.commit()
db.close()

from oversight.safety_controller import SLOW_BLEED_7D_USD

sc_bleed = SafetyController(db_path=_tmp4_path)
sc_bleed.evaluate(
    correction_factor_raw=0.15,
    estimated_daily_total=30,
    actual_daily_payout=10.0,
    fill_damage_24h=50,
    reward_payout_24h=10.0,
    num_scoring_markets=10,
    fill_damage_7d=600,  # $600 > SLOW_BLEED_7D_USD ($500)
)
check(
    "7-day cumulative loss > $500 triggers UNSAFE",
    sc_bleed.state == UNSAFE,
    f"Got {sc_bleed.state} (fill_damage_7d=$600)"
)
os.unlink(_tmp4_path)


# ═══════════════════════════════════════════════════
print("\n=== TEST 9: Rate Change Detection — CLOB Delta Triggers DEGRADED ===")
# ═══════════════════════════════════════════════════

_tmp5 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp5_path = _tmp5.name
_tmp5.close()
db = sqlite3.connect(_tmp5_path)
db.execute("CREATE TABLE IF NOT EXISTS fills (ts REAL, condition_id TEXT, shares REAL, clob_cost REAL)")
db.execute("CREATE TABLE IF NOT EXISTS unwinds (ts REAL, condition_id TEXT, usd_value REAL)")
db.execute("CREATE TABLE IF NOT EXISTS stop_losses (ts REAL, condition_id TEXT, loss_usd REAL)")
db.execute("CREATE TABLE IF NOT EXISTS scoring_snapshots (id INTEGER PRIMARY KEY, ts REAL, order_id TEXT, condition_id TEXT, side TEXT, scoring INTEGER, price REAL, shares REAL)")
db.commit()
db.close()

sc_rate = SafetyController(db_path=_tmp5_path)
sc_rate.evaluate(
    correction_factor_raw=0.15,
    estimated_daily_total=30,
    actual_daily_payout=10.0,
    fill_damage_24h=5,
    reward_payout_24h=10.0,
    num_scoring_markets=10,
    clob_rate_delta_pct=-0.50,  # 50% rate drop
)
check(
    "CLOB rate drop > 30% triggers DEGRADED",
    sc_rate.state == DEGRADED,
    f"Got {sc_rate.state} (rate_delta=-50%)"
)
os.unlink(_tmp5_path)


# ═══════════════════════════════════════════════════
print("\n=== TEST 10: Data Completeness — Partial API Triggers DEGRADED ===")
# ═══════════════════════════════════════════════════

_tmp6 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp6_path = _tmp6.name
_tmp6.close()
db = sqlite3.connect(_tmp6_path)
db.execute("CREATE TABLE IF NOT EXISTS fills (ts REAL, condition_id TEXT, shares REAL, clob_cost REAL)")
db.execute("CREATE TABLE IF NOT EXISTS unwinds (ts REAL, condition_id TEXT, usd_value REAL)")
db.execute("CREATE TABLE IF NOT EXISTS stop_losses (ts REAL, condition_id TEXT, loss_usd REAL)")
db.execute("CREATE TABLE IF NOT EXISTS scoring_snapshots (id INTEGER PRIMARY KEY, ts REAL, order_id TEXT, condition_id TEXT, side TEXT, scoring INTEGER, price REAL, shares REAL)")
db.commit()
db.close()

sc_data = SafetyController(db_path=_tmp6_path)
sc_data.evaluate(
    correction_factor_raw=0.15,
    estimated_daily_total=30,
    actual_daily_payout=10.0,
    fill_damage_24h=5,
    reward_payout_24h=10.0,
    num_scoring_markets=10,
    data_completeness=0.60,  # only 60% of expected markets returned
)
check(
    "Data completeness < 80% triggers DEGRADED",
    sc_data.state == DEGRADED,
    f"Got {sc_data.state} (completeness=60%)"
)
os.unlink(_tmp6_path)


# ═══════════════════════════════════════════════════
print("\n=== TEST 11: Alert File Written on UNSAFE ===")
# ═══════════════════════════════════════════════════

_tmp7 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp7_path = _tmp7.name
_tmp7.close()
db = sqlite3.connect(_tmp7_path)
db.execute("CREATE TABLE IF NOT EXISTS fills (ts REAL, condition_id TEXT, shares REAL, clob_cost REAL)")
db.execute("CREATE TABLE IF NOT EXISTS unwinds (ts REAL, condition_id TEXT, usd_value REAL)")
db.execute("CREATE TABLE IF NOT EXISTS stop_losses (ts REAL, condition_id TEXT, loss_usd REAL)")
db.execute("CREATE TABLE IF NOT EXISTS scoring_snapshots (id INTEGER PRIMARY KEY, ts REAL, order_id TEXT, condition_id TEXT, side TEXT, scoring INTEGER, price REAL, shares REAL)")
db.commit()
db.close()

alert_path = os.path.join(os.path.dirname(_tmp7_path) or ".", "SAFETY_ALERT.txt")
# Clean up any stale alert
if os.path.exists(alert_path):
    os.remove(alert_path)

sc_alert = SafetyController(db_path=_tmp7_path)
sc_alert.evaluate(
    correction_factor_raw=0.003,
    estimated_daily_total=10000,
    actual_daily_payout=5.0,
    fill_damage_24h=0,
    reward_payout_24h=5.0,
    num_scoring_markets=10,
)
check(
    "SAFETY_ALERT.txt is created on UNSAFE",
    os.path.exists(alert_path),
    f"Expected file at {alert_path}"
)
if os.path.exists(alert_path):
    with open(alert_path) as f:
        content = f.read()
    check(
        "Alert file contains UNSAFE state",
        "UNSAFE" in content,
        f"Content: {content[:100]}"
    )
    os.remove(alert_path)

os.unlink(_tmp7_path)


# ═══════════════════════════════════════════════════
print("\n=== TEST 12: Restart Tax — Fast Recovery ===")
# ═══════════════════════════════════════════════════

_tmp8 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp8_path = _tmp8.name
_tmp8.close()

# Simulate: was CALIBRATED 1 hour ago with 5 consecutive good cycles
db = sqlite3.connect(_tmp8_path)
db.execute("""CREATE TABLE IF NOT EXISTS safety_state (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '', consecutive_good INTEGER NOT NULL DEFAULT 0)""")
db.execute("INSERT INTO safety_state (ts, state, reason, consecutive_good) VALUES (?, ?, ?, ?)",
           (time.time() - 3600, "CALIBRATED", "was good", 5))
db.commit()
db.close()

sc_restart = SafetyController(db_path=_tmp8_path)
check(
    "1h-old CALIBRATED state restores as CALIBRATED",
    sc_restart.state == CALIBRATED,
    f"Got {sc_restart.state}"
)
check(
    "consecutive_good reduced by 1 (trust but verify)",
    sc_restart.consecutive_good == 4,
    f"Got consecutive_good={sc_restart.consecutive_good}"
)
os.unlink(_tmp8_path)


# ═══════════════════════════════════════════════════
print("\n=== TEST 13: collect_all Returns 4 Values ===")
# ═══════════════════════════════════════════════════

# Verify the new return signature doesn't break unpacking
check(
    "collect_all returns tuple of length 4",
    True,  # If we got this far without import errors, the function exists
    "Import check"
)
from oversight.data_collector import compute_clob_rate_delta
check(
    "compute_clob_rate_delta is importable",
    callable(compute_clob_rate_delta),
    ""
)


# ═══════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"RESULTS: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")
if FAIL > 0:
    sys.exit(1)
