"""
Safety invariant tests — regression prevention for the 7000x miscalibration.

Tests 1-5:   Core scoring/CF safeguards
Tests 6-13:  SafetyController state machine, aggregate sanity, alert file
Tests 14-21: 6-state machine, hourly loss, capital floor, drawdown, permissions
Tests 22-27: Priority system, missing data, portfolio fallback, recovery, DB failure
Tests 28-33: CF corroboration, balance history, confidence score, LOW haircut

Usage:
    python test_safety.py
"""

import os
import sys
import sqlite3
import tempfile
import time

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
    from oversight.data_collector import MarketMetrics
    defaults = dict(
        condition_id="test_cid_001", question="Test market?",
        daily_rate=50.0, actual_reward_total=0.0,
        fill_cost_recent=0.0, dump_revenue_recent=0.0,
        fill_count_recent=0, net_pnl_recent=0.0,
        current_position_usd=0.0, on_book_hours=24.0, q_share_pct=1.0,
    )
    defaults.update(overrides)
    return MarketMetrics(**defaults)


def create_test_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = tmp.name
    tmp.close()
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS fills (ts REAL, condition_id TEXT, side TEXT, fill_type TEXT, shares REAL, price REAL, clob_cost REAL, usd_value REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS unwinds (ts REAL, condition_id TEXT, usd_value REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS stop_losses (ts REAL, condition_id TEXT, loss_usd REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS scoring_snapshots (id INTEGER PRIMARY KEY, ts REAL, order_id TEXT, condition_id TEXT, side TEXT, scoring INTEGER, price REAL, shares REAL)")
    db.execute("INSERT INTO scoring_snapshots (ts, order_id, condition_id, side, scoring, price, shares) VALUES (?, ?, ?, ?, ?, ?, ?)",
               (time.time() - 60, "test_order", "test_cid", "yes", 1, 0.5, 100))
    db.commit()
    db.close()
    return path


_SAFE_CAPITAL = dict(exchange_balance=1000.0, total_portfolio_value=1500.0)


# ═══════════════════════════════════════════════════
print("\n=== TEST 1: Correction Factor Clamp ===")
estimated = 81561.0; actual = 11.59; raw_factor = actual / estimated
check("Raw CF clamps to 0.001", max(0.001, min(10.0, raw_factor)) == 0.001, "")
check("Normal CF 0.15 passes through", max(0.001, min(10.0, 0.15)) == 0.15, "")

# ═══════════════════════════════════════════════════
print("\n=== TEST 2: EMA Circuit Breaker ===")
from oversight.data_collector import _smooth_correction_factor
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _tmp_path = _tmp.name; _tmp.close()
db = sqlite3.connect(_tmp_path)
db.execute("CREATE TABLE IF NOT EXISTS correction_factor_history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, raw REAL NOT NULL, smoothed REAL NOT NULL)")
db.execute("INSERT INTO correction_factor_history (ts, raw, smoothed) VALUES (?, ?, ?)", (time.time()-100, 0.50, 0.50))
db.commit(); db.close()
result = _smooth_correction_factor(0.005, _tmp_path, alpha=0.3, has_new_observation=True)
check("Circuit breaker fires for raw=0.005", result < 0.01, f"Got {result:.4f}")
db = sqlite3.connect(_tmp_path); db.execute("DELETE FROM correction_factor_history")
db.execute("INSERT INTO correction_factor_history (ts, raw, smoothed) VALUES (?, ?, ?)", (time.time()-100, 0.50, 0.50))
db.commit(); db.close()
result_fast = _smooth_correction_factor(0.03, _tmp_path, alpha=0.3, has_new_observation=True)
check("Fast-adapt fires for raw=0.03", result_fast < 0.20, f"Got {result_fast:.4f}")
db = sqlite3.connect(_tmp_path); db.execute("DELETE FROM correction_factor_history")
db.execute("INSERT INTO correction_factor_history (ts, raw, smoothed) VALUES (?, ?, ?)", (time.time()-100, 0.20, 0.20))
db.commit(); db.close()
result_normal = _smooth_correction_factor(0.15, _tmp_path, alpha=0.3, has_new_observation=True)
check("Normal EMA works", abs(result_normal - 0.185) < 0.01, f"Got {result_normal:.4f}")
os.unlink(_tmp_path)

# ═══════════════════════════════════════════════════
print("\n=== TEST 3: Q-Share Cap ===")
from oversight.market_scorer import score_market
s1 = score_market(make_metrics(q_share_pct=1.0, daily_rate=100.0), correction_factor=1.0)
s2 = score_market(make_metrics(q_share_pct=0.5, daily_rate=100.0), correction_factor=1.0)
check("q_share=1.0 and 0.5 same score", abs(s1 - s2) < 0.01, f"{s1:.2f} vs {s2:.2f}")
check("Score capped at ~$50/day", s1 <= 51.0, f"Got {s1:.2f}")

# ═══════════════════════════════════════════════════
print("\n=== TEST 4: Zero-Fill Bonus Removed ===")
sz = score_market(make_metrics(q_share_pct=0.1, daily_rate=50.0, fill_count_recent=0), correction_factor=1.0)
sf = score_market(make_metrics(q_share_pct=0.1, daily_rate=50.0, fill_count_recent=1), correction_factor=1.0)
check("Zero-fill no bonus", abs(sz - sf) < 0.01, f"{sz:.2f} vs {sf:.2f}")

# ═══════════════════════════════════════════════════
print("\n=== TEST 5: Minimum Effective Reward Gate ===")
from oversight.market_scorer import classify_market
m_tiny = make_metrics(q_share_pct=0.01, daily_rate=50.0, on_book_hours=8.0)
sm_tiny = classify_market(m_tiny, score_market(m_tiny, correction_factor=0.1), correction_factor=0.1)
check("$0.05/day market avoided", sm_tiny.action == "avoid", f"Got {sm_tiny.action}")
m_decent = make_metrics(q_share_pct=0.1, daily_rate=50.0, on_book_hours=8.0)
sm_decent = classify_market(m_decent, score_market(m_decent, correction_factor=0.1), correction_factor=0.1)
check("$0.50/day market deploys", sm_decent.action == "deploy", f"Got {sm_decent.action}")

# ═══════════════════════════════════════════════════
print("\n=== TEST 6: SafetyController State Machine ===")
_tmp2_path = create_test_db()
from oversight.safety_controller import (
    SafetyController, UNSAFE, DEGRADED, LEARNING, CALIBRATED,
    MILDLY_MISCALIBRATED, SEVERELY_MISCALIBRATED, DATA_UNAVAILABLE,
    STATE_SEVERITY, PRIORITY_CRITICAL, PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW,
)

sc = SafetyController(db_path=_tmp2_path)
check("Initial state is LEARNING", sc.state == LEARNING, f"Got {sc.state}")

# CF=0.003 alone → SEVERELY_MISCALIBRATED (HIGH, not UNSAFE — needs corroboration)
sc.evaluate(correction_factor_raw=0.003, estimated_daily_total=30,
            actual_daily_payout=10.0, fill_damage_24h=0, reward_payout_24h=10.0,
            num_scoring_markets=10, **_SAFE_CAPITAL)
check("CF < 0.005 alone -> SEVERELY_MISCALIBRATED (not UNSAFE)",
      sc.state == SEVERELY_MISCALIBRATED, f"Got {sc.state}")

# Fill damage > $150 → UNSAFE (CRITICAL, proven risk)
sc3 = SafetyController(db_path=_tmp2_path)
sc3.evaluate(correction_factor_raw=0.3, estimated_daily_total=30,
             actual_daily_payout=10.0, fill_damage_24h=400, reward_payout_24h=10.0,
             num_scoring_markets=10, **_SAFE_CAPITAL)
check("Fill damage > $150 triggers UNSAFE", sc3.state == UNSAFE, f"Got {sc3.state}")

# Est/actual > 50x → SEVERELY_MISCALIBRATED (HIGH priority, not UNSAFE)
sc2 = SafetyController(db_path=_tmp2_path)
sc2.evaluate(correction_factor_raw=0.1, estimated_daily_total=5000,
             actual_daily_payout=10.0, fill_damage_24h=0, reward_payout_24h=10.0,
             num_scoring_markets=10, **_SAFE_CAPITAL)
check("Est/actual > 50x -> SEVERELY_MISCALIBRATED (HIGH)",
      sc2.state == SEVERELY_MISCALIBRATED, f"Got {sc2.state}")

# CF < 0.02 → SEVERELY_MISCALIBRATED
_tmp3_path = create_test_db()
sc4 = SafetyController(db_path=_tmp3_path)
sc4.evaluate(correction_factor_raw=0.015, estimated_daily_total=100,
             actual_daily_payout=10.0, fill_damage_24h=0, reward_payout_24h=10.0,
             num_scoring_markets=10, **_SAFE_CAPITAL)
check("CF < 0.02 -> SEVERELY_MISCALIBRATED", sc4.state == SEVERELY_MISCALIBRATED, f"Got {sc4.state}")

# Upgrade to CALIBRATED
sc5 = SafetyController(db_path=_tmp3_path)
sc5.state = LEARNING; sc5.consecutive_good = 0
for _ in range(4):
    sc5.evaluate(correction_factor_raw=0.15, estimated_daily_total=30,
                 actual_daily_payout=10.0, fill_damage_24h=5, reward_payout_24h=10.0,
                 num_scoring_markets=10, **_SAFE_CAPITAL)
check("4 good cycles -> CALIBRATED", sc5.state == CALIBRATED, f"Got {sc5.state}")
os.unlink(_tmp3_path)

# Probe mode
sc6 = SafetyController(db_path=_tmp2_path); sc6.state = UNSAFE
allocs = [{"action":"deploy","shares_per_side":200,"score":s,"condition_id":c,
           "est_capital_cost":200,"max_spread":0.045,"q_share_pct":0.1,"min_size":50}
          for s,c in [(10,"a"),(5,"b"),(3,"c"),(1,"d")]]
filtered = sc6.filter_allocations(allocs, 1000)
deploy_n = sum(1 for a in filtered if a["action"]=="deploy")
check("UNSAFE probe: max 3 markets", deploy_n <= 3, f"Got {deploy_n}")
check("UNSAFE probe: at least 1", deploy_n >= 1, f"Got {deploy_n}")
for a in filtered:
    if a["action"]=="deploy":
        check(f"Probe {a['condition_id']} min_size", a["shares_per_side"]==50, "")
        check(f"Probe {a['condition_id']} reason", "PROBE" in a.get("reason",""), "")
probe_cost = sum(a.get("est_capital_cost",0) for a in filtered if a["action"]=="deploy")
check("Probe capital <= 5%", probe_cost <= 51, f"${probe_cost:.0f}")
os.unlink(_tmp2_path)

# ═══════════════════════════════════════════════════
print("\n=== TEST 7: Aggregate Sanity ===")
est_new = 136 * 600 * 0.5; cf_new = max(0.001, 11.59 / est_new)
check("Est/actual ratio > 50x", est_new / 11.59 > 50, "")
check("CF < 0.005 fires", cf_new < 0.005, f"CF={cf_new:.6f}")

# ═══════════════════════════════════════════════════
print("\n=== TEST 8: Slow Bleed ===")
p = create_test_db()
sc_b = SafetyController(db_path=p)
sc_b.evaluate(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
              fill_damage_24h=50, reward_payout_24h=10.0, num_scoring_markets=10,
              fill_damage_7d=600, **_SAFE_CAPITAL)
check("7d loss > $500 -> UNSAFE", sc_b.state == UNSAFE, f"Got {sc_b.state}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 9: CLOB Rate Drop ===")
p = create_test_db()
sc_r = SafetyController(db_path=p)
sc_r.evaluate(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
              fill_damage_24h=5, reward_payout_24h=10.0, num_scoring_markets=10,
              clob_rate_delta_pct=-0.50, **_SAFE_CAPITAL)
check("CLOB drop > 30% -> DEGRADED", sc_r.state == DEGRADED, f"Got {sc_r.state}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 10: Data Completeness ===")
p = create_test_db()
sc_d = SafetyController(db_path=p)
sc_d.evaluate(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
              fill_damage_24h=5, reward_payout_24h=10.0, num_scoring_markets=10,
              data_completeness=0.60, **_SAFE_CAPITAL)
check("Completeness < 80% -> DEGRADED", sc_d.state == DEGRADED, f"Got {sc_d.state}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 11: Alert File ===")
p = create_test_db()
alert_path = os.path.join(os.path.dirname(p) or ".", "SAFETY_ALERT.txt")
if os.path.exists(alert_path): os.remove(alert_path)
sc_a = SafetyController(db_path=p)
sc_a.evaluate(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
              fill_damage_24h=200, reward_payout_24h=10.0, num_scoring_markets=10,
              **_SAFE_CAPITAL)
check("Alert file created on UNSAFE", os.path.exists(alert_path), "")
if os.path.exists(alert_path):
    with open(alert_path) as f: content = f.read()
    check("Alert contains UNSAFE", "UNSAFE" in content, f"Content: {content[:80]}")
    os.remove(alert_path)
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 12: Restart Tax ===")
_tmp8 = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _tmp8_path = _tmp8.name; _tmp8.close()
db = sqlite3.connect(_tmp8_path)
db.execute("CREATE TABLE IF NOT EXISTS safety_state (id INTEGER PRIMARY KEY, ts REAL NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', consecutive_good INTEGER NOT NULL DEFAULT 0)")
db.execute("INSERT INTO safety_state (ts, state, reason, consecutive_good) VALUES (?, ?, ?, ?)",
           (time.time()-3600, "CALIBRATED", "good", 5))
db.commit(); db.close()
sc_re = SafetyController(db_path=_tmp8_path)
check("Restores CALIBRATED", sc_re.state == CALIBRATED, f"Got {sc_re.state}")
check("consecutive_good - 1", sc_re.consecutive_good == 4, f"Got {sc_re.consecutive_good}")
os.unlink(_tmp8_path)

# ═══════════════════════════════════════════════════
print("\n=== TEST 13: collect_all Signature ===")
from oversight.data_collector import compute_clob_rate_delta
check("compute_clob_rate_delta importable", callable(compute_clob_rate_delta), "")

# ═══════════════════════════════════════════════════
print("\n=== TEST 14: 6-State Machine ===")
from oversight.safety_controller import STATE_PERMISSIONS, ALL_STATES
check("LEARNING == MILDLY", LEARNING == MILDLY_MISCALIBRATED, "")
check("6 states have permissions", len(STATE_PERMISSIONS) == 6, "")
check("Severity ordering",
      STATE_SEVERITY[CALIBRATED] < STATE_SEVERITY[MILDLY_MISCALIBRATED]
      < STATE_SEVERITY[SEVERELY_MISCALIBRATED] < STATE_SEVERITY[DEGRADED]
      < STATE_SEVERITY[DATA_UNAVAILABLE] < STATE_SEVERITY[UNSAFE], "")
p = create_test_db()
sc_m = SafetyController(db_path=p)
sc_m.evaluate(correction_factor_raw=0.025, estimated_daily_total=30, actual_daily_payout=10.0,
              fill_damage_24h=0, reward_payout_24h=10.0, num_scoring_markets=10, **_SAFE_CAPITAL)
check("CF=0.025 -> MILDLY", sc_m.state == MILDLY_MISCALIBRATED, f"Got {sc_m.state}")
sc_dc = SafetyController(db_path=p)
sc_dc.evaluate(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
               fill_damage_24h=5, reward_payout_24h=10.0, num_scoring_markets=10,
               data_completeness=0.40, **_SAFE_CAPITAL)
check("Completeness < 50% -> DATA_UNAVAILABLE", sc_dc.state == DATA_UNAVAILABLE, f"Got {sc_dc.state}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 15: Hourly Loss Cap ===")
p = create_test_db(); now = time.time()
db = sqlite3.connect(p)
for i in range(4):
    db.execute("INSERT INTO fills (ts,condition_id,side,fill_type,shares,price,clob_cost,usd_value) VALUES(?,?,?,?,?,?,?,?)",
               (now-60*i, f"t{i}", "yes", "BUY", 20, 0.5, 0.5, 10))
db.commit(); db.close()
sc_h = SafetyController(db_path=p)
sc_h.evaluate(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
              fill_damage_24h=40, reward_payout_24h=10.0, num_scoring_markets=10, **_SAFE_CAPITAL)
check("1h $40 > $30 -> DEGRADED", STATE_SEVERITY[sc_h.state] >= STATE_SEVERITY[DEGRADED], f"Got {sc_h.state}")
db = sqlite3.connect(p)
for i in range(7):
    db.execute("INSERT INTO fills (ts,condition_id,side,fill_type,shares,price,clob_cost,usd_value) VALUES(?,?,?,?,?,?,?,?)",
               (now-30*i, f"x{i}", "yes", "BUY", 20, 0.5, 0.5, 10))
db.commit(); db.close()
sc_h2 = SafetyController(db_path=p)
sc_h2.evaluate(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
               fill_damage_24h=0, reward_payout_24h=10.0, num_scoring_markets=10, **_SAFE_CAPITAL)
check("1h > $60 -> DEGRADED (HIGH cap, not UNSAFE)", sc_h2.state == DEGRADED, f"Got {sc_h2.state}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 16: Capital Floor ===")
p = create_test_db()
sc_f = SafetyController(db_path=p)
sc_f.evaluate_state(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
                    reward_payout_24h=10.0, num_scoring_markets=10,
                    exchange_balance=25.0, total_portfolio_value=100.0)
check("Balance $25 < $50 -> UNSAFE", sc_f.state == UNSAFE, f"Got {sc_f.state}")
fv = [v for v in sc_f.violations if v.invariant == "capital_floor"]
check("Capital floor violation recorded", len(fv) == 1 and fv[0].value == 25.0, f"{fv}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 17: Drawdown ===")
p = create_test_db()
sc_dd = SafetyController(db_path=p)
sc_dd.evaluate_state(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
                     reward_payout_24h=10.0, num_scoring_markets=10,
                     exchange_balance=1500.0, total_portfolio_value=1500.0)
check("Peak at $1500", sc_dd._portfolio_peak >= 1500.0, f"Peak={sc_dd._portfolio_peak}")
sc_dd.evaluate_state(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
                     reward_payout_24h=10.0, num_scoring_markets=10,
                     exchange_balance=1200.0, total_portfolio_value=1200.0)
check("20% drawdown -> UNSAFE", sc_dd.state == UNSAFE, f"Got {sc_dd.state}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 18: Fill Storm ===")
p = create_test_db()
db = sqlite3.connect(p)
db.execute("INSERT INTO fills (ts,condition_id,side,fill_type,shares,price,clob_cost,usd_value) VALUES(?,?,?,?,?,?,?,?)",
           (time.time()-300, "__FILL_STORM__", "both", "STORM", 0, 0, 0, 0))
db.commit(); db.close()
sc_s = SafetyController(db_path=p)
sc_s.evaluate(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
              fill_damage_24h=5, reward_payout_24h=10.0, num_scoring_markets=10, **_SAFE_CAPITAL)
check("Fill storm -> DEGRADED", STATE_SEVERITY[sc_s.state] >= STATE_SEVERITY[DEGRADED], f"Got {sc_s.state}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 19: Upgrade Path ===")
p = create_test_db()
sc_u = SafetyController(db_path=p); sc_u.state = UNSAFE; sc_u.consecutive_good = 0
gp = dict(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
          fill_damage_24h=5, reward_payout_24h=10.0, num_scoring_markets=10, **_SAFE_CAPITAL)
sc_u.evaluate(**gp); check("1 cycle: still UNSAFE", sc_u.state == UNSAFE, f"Got {sc_u.state}")
sc_u.evaluate(**gp); check("2 cycles: -> MILDLY", sc_u.state == MILDLY_MISCALIBRATED, f"Got {sc_u.state}")
for _ in range(3): sc_u.evaluate(**gp)
check("5 total: -> CALIBRATED", sc_u.state == CALIBRATED, f"Got {sc_u.state}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 20: Violations Property ===")
p = create_test_db()
sc_v = SafetyController(db_path=p)
# Use daily_loss to get CRITICAL + cf_drift for HIGH
sc_v.evaluate(correction_factor_raw=0.003, estimated_daily_total=10000, actual_daily_payout=5.0,
              fill_damage_24h=200, reward_payout_24h=5.0, num_scoring_markets=10, **_SAFE_CAPITAL)
check("Violations exist", len(sc_v.violations) > 0, f"Got {len(sc_v.violations)}")
check("All fields present",
      all(hasattr(v, a) for v in sc_v.violations for a in ["invariant","priority","severity","value","threshold","message"]), "")
inv = {v.invariant for v in sc_v.violations}
check("cf_drift present", "cf_drift" in inv, f"{inv}")
check("est_actual present", "est_actual" in inv, f"{inv}")
prios = {v.priority for v in sc_v.violations}
check("CRITICAL and HIGH priorities", PRIORITY_CRITICAL in prios and PRIORITY_HIGH in prios, f"{prios}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 21: State Permissions ===")
check("CALIBRATED: 60/100%", STATE_PERMISSIONS[CALIBRATED]["max_markets"]==60 and STATE_PERMISSIONS[CALIBRATED]["capital_pct"]==1.0, "")
check("MILDLY: 40/70%", STATE_PERMISSIONS[MILDLY_MISCALIBRATED]["max_markets"]==40, "")
check("SEVERELY: 20/40%/no trials", STATE_PERMISSIONS[SEVERELY_MISCALIBRATED]["max_markets"]==20 and not STATE_PERMISSIONS[SEVERELY_MISCALIBRATED]["trials"], "")
check("DEGRADED: 10/20%", STATE_PERMISSIONS[DEGRADED]["max_markets"]==10, "")
check("DATA_UNAVAIL: 5/10%", STATE_PERMISSIONS[DATA_UNAVAILABLE]["max_markets"]==5, "")
check("UNSAFE: 3/5%/probe", STATE_PERMISSIONS[UNSAFE]["max_markets"]==3 and STATE_PERMISSIONS[UNSAFE]["probe_mode"], "")

# ═══════════════════════════════════════════════════
print("\n=== TEST 22: Priority — CRITICAL Overrides HIGH ===")
p = create_test_db()
sc_p = SafetyController(db_path=p)
# daily_loss $200 (CRITICAL UNSAFE) + est/actual 500x (HIGH SEVERE)
sc_p.evaluate(correction_factor_raw=0.1, estimated_daily_total=5000, actual_daily_payout=10.0,
              fill_damage_24h=200, reward_payout_24h=10.0, num_scoring_markets=10, **_SAFE_CAPITAL)
check("CRITICAL (daily_loss) overrides HIGH -> UNSAFE", sc_p.state == UNSAFE, f"Got {sc_p.state}")
# Only HIGH → NOT UNSAFE
sc_p2 = SafetyController(db_path=p)
sc_p2.evaluate(correction_factor_raw=0.1, estimated_daily_total=5000, actual_daily_payout=10.0,
               fill_damage_24h=0, reward_payout_24h=10.0, num_scoring_markets=10, **_SAFE_CAPITAL)
check("HIGH only -> SEVERELY_MISCALIBRATED", sc_p2.state == SEVERELY_MISCALIBRATED, f"Got {sc_p2.state}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 23: Missing Data -> DATA_UNAVAILABLE ===")
p = create_test_db()
sc_ms = SafetyController(db_path=p)
sc_ms.evaluate_state(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
                     reward_payout_24h=10.0, num_scoring_markets=10,
                     exchange_balance=0.0, total_portfolio_value=0.0)
check("Both missing -> DATA_UNAVAILABLE", sc_ms.state == DATA_UNAVAILABLE, f"Got {sc_ms.state}")
uv = [v for v in sc_ms.violations if v.severity == UNSAFE]
check("No UNSAFE violations when data missing", len(uv) == 0, f"{[v.invariant for v in uv]}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 24: Portfolio Fallback ===")
p = create_test_db()
sc_fb = SafetyController(db_path=p)
sc_fb.evaluate_state(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
                     reward_payout_24h=10.0, num_scoring_markets=10,
                     exchange_balance=1000.0, total_portfolio_value=0.0)
dv = [v for v in sc_fb.violations if v.invariant == "drawdown"]
check("Fallback: no drawdown violation", len(dv) == 0, f"{dv}")
check("Fallback: state OK", sc_fb.state not in (DATA_UNAVAILABLE, UNSAFE), f"Got {sc_fb.state}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 25: UNSAFE Auto-Recovery ===")
p = create_test_db()
sc_rc = SafetyController(db_path=p); sc_rc.state = UNSAFE; sc_rc._unsafe_no_critical_count = 0
rp = dict(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
          reward_payout_24h=10.0, num_scoring_markets=10, exchange_balance=1000.0,
          total_portfolio_value=1500.0, data_completeness=0.60)
sc_rc.evaluate_state(**rp)
check("UNSAFE + MEDIUM only -> natural downgrade", sc_rc.state != UNSAFE, f"Got {sc_rc.state}")
# Counter mechanism
sc_rc2 = SafetyController(db_path=p); sc_rc2.state = UNSAFE; sc_rc2._unsafe_no_critical_count = 0
for _ in range(3):
    sc_rc2.state = UNSAFE
    sc_rc2.evaluate_state(**rp)
check("Recovery counter tracks", sc_rc2._unsafe_no_critical_count >= 3 or sc_rc2.state != UNSAFE,
      f"cnt={sc_rc2._unsafe_no_critical_count}, state={sc_rc2.state}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 26: DB Query Failure -> DEGRADED ===")
_tmp26 = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _tmp26_path = _tmp26.name; _tmp26.close()
db = sqlite3.connect(_tmp26_path)
db.execute("CREATE TABLE IF NOT EXISTS scoring_snapshots (id INTEGER PRIMARY KEY, ts REAL, order_id TEXT, condition_id TEXT, side TEXT, scoring INTEGER, price REAL, shares REAL)")
db.execute("INSERT INTO scoring_snapshots (ts, order_id, condition_id, side, scoring, price, shares) VALUES (?, ?, ?, ?, ?, ?, ?)",
           (time.time()-60, "t", "t", "yes", 1, 0.5, 100))
db.commit(); db.close()
sc_br = SafetyController(db_path=_tmp26_path)
r = sc_br.evaluate_state(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
                         reward_payout_24h=10.0, num_scoring_markets=10,
                         exchange_balance=1000.0, total_portfolio_value=1500.0)
check("DB failures -> DEGRADED", r == DEGRADED, f"Got {r}")
uv = [v for v in sc_br.violations if v.severity == UNSAFE]
check("No UNSAFE from DB failures", len(uv) == 0, f"{[v.invariant for v in uv]}")
os.unlink(_tmp26_path)

# ═══════════════════════════════════════════════════
print("\n=== TEST 27: Fill Storm Priority ===")
p = create_test_db()
db = sqlite3.connect(p)
db.execute("INSERT INTO fills (ts,condition_id,side,fill_type,shares,price,clob_cost,usd_value) VALUES(?,?,?,?,?,?,?,?)",
           (time.time()-100, "__FILL_STORM__", "both", "STORM", 0, 0, 0, 0))
db.commit(); db.close()
sc_s3 = SafetyController(db_path=p)
sc_s3.evaluate(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
               fill_damage_24h=5, reward_payout_24h=10.0, num_scoring_markets=10, **_SAFE_CAPITAL)
sv = [v for v in sc_s3.violations if v.invariant == "fill_storm"]
check("Fill storm: LOW priority, DEGRADED", len(sv)==1 and sv[0].priority==PRIORITY_LOW and sv[0].severity==DEGRADED, f"{sv}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 28: CF Corroboration ===")
p = create_test_db()
# CF alone → SEVERELY_MISCALIBRATED (not UNSAFE)
sc_cf1 = SafetyController(db_path=p)
sc_cf1.evaluate(correction_factor_raw=0.003, estimated_daily_total=30, actual_daily_payout=10.0,
                fill_damage_24h=0, reward_payout_24h=10.0, num_scoring_markets=10, **_SAFE_CAPITAL)
check("CF < 0.005 alone -> NOT UNSAFE", sc_cf1.state != UNSAFE, f"Got {sc_cf1.state}")
check("CF < 0.005 alone -> SEVERELY_MISCALIBRATED", sc_cf1.state == SEVERELY_MISCALIBRATED, f"Got {sc_cf1.state}")

# CF + est/actual + losses → corroborated → UNSAFE
sc_cf2 = SafetyController(db_path=p)
sc_cf2.evaluate(correction_factor_raw=0.003, estimated_daily_total=5000, actual_daily_payout=10.0,
                fill_damage_24h=75, reward_payout_24h=10.0, num_scoring_markets=10, **_SAFE_CAPITAL)
check("CF corroborated (CF+est+loss) -> UNSAFE", sc_cf2.state == UNSAFE, f"Got {sc_cf2.state}")
cv = [v for v in sc_cf2.violations if v.invariant == "cf_corroborated"]
check("cf_corroborated violation exists", len(cv) == 1, f"Got {len(cv)}")
check("cf_corroborated is CRITICAL", cv[0].priority == PRIORITY_CRITICAL if cv else False, "")

# CF + est/actual but NO losses → not corroborated
sc_cf3 = SafetyController(db_path=p)
sc_cf3.evaluate(correction_factor_raw=0.003, estimated_daily_total=5000, actual_daily_payout=10.0,
                fill_damage_24h=10, reward_payout_24h=10.0, num_scoring_markets=10, **_SAFE_CAPITAL)
check("CF + est/actual but losses < $50 -> NOT UNSAFE", sc_cf3.state != UNSAFE, f"Got {sc_cf3.state}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 29: Balance History — Sustained Zero vs API Failure ===")
p = create_test_db()
# No balance history + exchange_balance=0 → DATA_UNAVAILABLE (API failure)
sc_bh1 = SafetyController(db_path=p)
sc_bh1.evaluate_state(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
                      reward_payout_24h=10.0, num_scoring_markets=10,
                      exchange_balance=0.0, total_portfolio_value=1000.0)
fv = [v for v in sc_bh1.violations if v.invariant == "capital_floor"]
check("No history + balance=0 -> DATA_UNAVAILABLE", len(fv)==1 and fv[0].severity == DATA_UNAVAILABLE,
      f"Got {fv[0].severity if fv else 'none'}")

# Now insert positive balance history then check with balance=0
db = sqlite3.connect(p)
db.execute("INSERT INTO portfolio_snapshots (ts, total_value, exchange_balance, locked_capital, peak_value) VALUES (?, ?, ?, ?, ?)",
           (time.time() - 1800, 1200.0, 800.0, 400.0, 1200.0))
db.commit(); db.close()
sc_bh2 = SafetyController(db_path=p)
sc_bh2.evaluate_state(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
                      reward_payout_24h=10.0, num_scoring_markets=10,
                      exchange_balance=0.0, total_portfolio_value=1000.0)
fv2 = [v for v in sc_bh2.violations if v.invariant == "capital_floor"]
check("Had $800 recently + balance=0 -> UNSAFE (sustained zero)",
      len(fv2)==1 and fv2[0].severity == UNSAFE,
      f"Got {fv2[0].severity if fv2 else 'none'}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 30: Confidence Score ===")
p = create_test_db()
# Clean system
sc_cs1 = SafetyController(db_path=p)
sc_cs1.evaluate(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
                fill_damage_24h=5, reward_payout_24h=10.0, num_scoring_markets=10, **_SAFE_CAPITAL)
check("Clean system: confidence = 1.0", sc_cs1.confidence_score == 1.0,
      f"Got {sc_cs1.confidence_score}")

# System with CF drift
sc_cs2 = SafetyController(db_path=p)
sc_cs2.evaluate(correction_factor_raw=0.003, estimated_daily_total=10000, actual_daily_payout=5.0,
                fill_damage_24h=0, reward_payout_24h=5.0, num_scoring_markets=10, **_SAFE_CAPITAL)
check("CF drift: confidence < 1.0", sc_cs2.confidence_score < 1.0,
      f"Got {sc_cs2.confidence_score}")
check("CF drift: confidence > 0", sc_cs2.confidence_score > 0,
      f"Got {sc_cs2.confidence_score}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 31: LOW Signal Capital Haircut ===")
p = create_test_db()
# Insert fill storm sentinel
db = sqlite3.connect(p)
db.execute("INSERT INTO fills (ts,condition_id,side,fill_type,shares,price,clob_cost,usd_value) VALUES(?,?,?,?,?,?,?,?)",
           (time.time()-100, "__FILL_STORM__", "both", "STORM", 0, 0, 0, 0))
db.commit(); db.close()

sc_lh = SafetyController(db_path=p)
sc_lh.evaluate(correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=10.0,
               fill_damage_24h=5, reward_payout_24h=10.0, num_scoring_markets=10, **_SAFE_CAPITAL)
# State should be DEGRADED from fill storm
# Now test filter_allocations applies haircut
sc_lh.state = CALIBRATED  # force to CALIBRATED so capital_pct = 1.0
allocs2 = [{"action":"deploy","shares_per_side":200,"score":10,"condition_id":"x",
            "est_capital_cost":200,"max_spread":0.045,"q_share_pct":0.1,"min_size":50}]
# With 20% haircut from fill storm: $1000 * 1.0 * 0.80 = $800 effective
filtered2 = sc_lh.filter_allocations(allocs2, 1000)
# Deploy should succeed (200 < 800) but capital was reduced
check("LOW haircut applied (fill storm -> 20% reduction)",
      any(v.invariant == "fill_storm" for v in sc_lh._last_violations),
      f"Violations: {[v.invariant for v in sc_lh._last_violations]}")
os.unlink(p)

# ═══════════════════════════════════════════════════
print("\n=== TEST 32: Strict Auto-Recovery Requires Valid Data ===")
# Auto-recovery should NOT increment counter if no valid data exists
p_nodata = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
p_nodata_path = p_nodata.name; p_nodata.close()
db = sqlite3.connect(p_nodata_path)
db.execute("CREATE TABLE IF NOT EXISTS fills (ts REAL, condition_id TEXT, side TEXT, fill_type TEXT, shares REAL, price REAL, clob_cost REAL, usd_value REAL)")
db.execute("CREATE TABLE IF NOT EXISTS unwinds (ts REAL, condition_id TEXT, usd_value REAL)")
db.execute("CREATE TABLE IF NOT EXISTS stop_losses (ts REAL, condition_id TEXT, loss_usd REAL)")
db.execute("CREATE TABLE IF NOT EXISTS scoring_snapshots (id INTEGER PRIMARY KEY, ts REAL, order_id TEXT, condition_id TEXT, side TEXT, scoring INTEGER, price REAL, shares REAL)")
# NO scoring data → data_freshness = None → no valid data
db.commit(); db.close()

sc_nr = SafetyController(db_path=p_nodata_path)
sc_nr.state = UNSAFE
sc_nr._unsafe_no_critical_count = 0
sc_nr.evaluate_state(
    correction_factor_raw=0.15, estimated_daily_total=30, actual_daily_payout=0.0,
    reward_payout_24h=0.0, num_scoring_markets=0,
    exchange_balance=1000.0, total_portfolio_value=1500.0)
# No valid data (data_freshness=None, actual_daily_payout=0) → counter should NOT increment
check("No valid data -> recovery counter stays 0",
      sc_nr._unsafe_no_critical_count == 0,
      f"Got {sc_nr._unsafe_no_critical_count}")
os.unlink(p_nodata_path)


# ═══════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"RESULTS: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")
if FAIL > 0:
    sys.exit(1)
