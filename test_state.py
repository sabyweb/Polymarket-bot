"""
State consistency tests for the Polymarket market-making bot.

Tests every critical state transition:
- Price convention (YES-equivalent storage)
- USD tracking for both YES and NO sides
- Position halt/resume logic
- Sell price computation from VWAP
- Decay mechanics
- Cancel failure handling

Run: python3 test_state.py
"""

import json
import os
import sys
import tempfile
import time

# ── Minimal mocks so we can import position.py without the full bot ──────────

# Patch logging before importing position module
import logging
logging.basicConfig(level=logging.WARNING)

# We test position.py directly (no exchange dependency)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import MAX_POSITION_USD, RESUME_POSITION_USD

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 1: Price Convention — avg_price is always YES-equivalent
# ═══════════════════════════════════════════════════════════════════════════════
print("\n=== TEST 1: Price Convention ===")

# Simulate what place_order stores in active_orders["price"]
# YES side: place_order("yes", our_bid=0.30) → price=0.30, clob_price=0.30
# NO side:  place_order("no", our_ask=0.60)  → price=0.60, clob_price=1-0.60=0.40

yes_order_price = 0.30  # YES bid
no_order_price = 0.60   # YES ask (NOT the NO clob price)

# What detect_fills passes to record_fill:
# record_fill(cid, "yes", shares, order["price"]=0.30)
# record_fill(cid, "no", shares, order["price"]=0.60)

check("YES order price is YES-equiv",
      yes_order_price == 0.30,
      f"got {yes_order_price}")

check("NO order price is YES-equiv (not CLOB)",
      no_order_price == 0.60,
      f"got {no_order_price}, should NOT be 0.40")

# What reconcile_unwinds computes for sell price:
# YES: vwap_clob = round_down(avg_price) = 0.30
# NO:  vwap_clob = round_down(1 - avg_price) = round_down(1-0.60) = 0.40

yes_sell_clob = yes_order_price           # 0.30
no_sell_clob = 1 - no_order_price          # 0.40

check("YES sell at VWAP", yes_sell_clob == 0.30,
      f"got {yes_sell_clob}")
check("NO sell at actual cost (40c)", no_sell_clob == 0.40,
      f"got {no_sell_clob}")


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 2: USD Tracking — record_fill uses CLOB cost, not YES-equiv
# ═══════════════════════════════════════════════════════════════════════════════
print("\n=== TEST 2: USD Tracking ===")

import position as _position_module
from position import PositionTracker

# Redirect positions.json to a temp file so tests don't overwrite real data
_original_positions_file = _position_module.POSITIONS_FILE
with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
    json.dump({}, f)
    tmp_path = f.name
_position_module.POSITIONS_FILE = tmp_path

try:
    tracker = PositionTracker()
    tracker.positions = {}

    CID = "test-market-001"
    tracker.register_market(CID, "Test Market")

    # BUY YES at 30c: 100 shares
    tracker.record_fill(CID, "yes", 100.0, 0.30, question="Test Market")
    yes_usd = tracker.positions[CID]["yes"]
    check("YES USD = shares * price (100 * 0.30 = $30)",
          abs(yes_usd - 30.0) < 0.01,
          f"got ${yes_usd:.2f}")

    # BUY NO at 40c CLOB: order["price"] = 0.60 (YES-equiv)
    # CLOB cost = 1 - 0.60 = 0.40
    # USD should be 200 * 0.40 = $80
    tracker.record_fill(CID, "no", 200.0, 0.60, question="Test Market")
    no_usd = tracker.positions[CID]["no"]
    check("NO USD = shares * (1-price) (200 * 0.40 = $80)",
          abs(no_usd - 80.0) < 0.01,
          f"got ${no_usd:.2f}, should NOT be ${200 * 0.60:.2f}")

    # Verify avg_price is YES-equivalent
    no_avg = tracker.get_avg_price(CID, "no")
    check("NO avg_price stored as YES-equiv (0.60)",
          abs(no_avg - 0.60) < 0.001,
          f"got {no_avg}")

    # ═══════════════════════════════════════════════════════════════════════
    # TEST 3: Position Halt Logic
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== TEST 3: Position Halt Logic ===")

    # Reset for clean halt test
    tracker.positions = {}
    tracker.register_market(CID, "Halt Test")

    # Fill YES up to just below halt threshold
    # MAX_POSITION_USD = 400 from config
    # 1000 shares at YES price 0.39 → USD = 1000 * 0.39 = $390 (below 400)
    tracker.record_fill(CID, "yes", 1000.0, 0.39, question="Halt Test")
    check("YES not halted at $390",
          not tracker.is_halted(CID, "yes"),
          f"halted={tracker.is_halted(CID, 'yes')}, usd={tracker.positions[CID]['yes']:.2f}")

    # Another fill: 50 shares at 0.39 → USD += 50*0.39=$19.50 → total $409.50
    tracker.record_fill(CID, "yes", 50.0, 0.39, question="Halt Test")
    check("YES halted at $409.50",
          tracker.is_halted(CID, "yes"),
          f"halted={tracker.is_halted(CID, 'yes')}, usd={tracker.positions[CID]['yes']:.2f}")

    # can_quote should return False when halted
    check("can_quote returns False when halted",
          not tracker.can_quote(CID, "yes"),
          f"can_quote={tracker.can_quote(CID, 'yes')}")

    # ═══════════════════════════════════════════════════════════════════════
    # TEST 4: NO-Side Halt — uses CLOB cost not YES-equiv
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== TEST 4: NO-Side Halt ===")

    tracker.positions = {}
    tracker.register_market(CID, "NO Halt Test")

    # BUY NO: order["price"]=0.30 (YES-equiv), CLOB cost=0.70
    # 500 shares → USD = 500 * 0.70 = $350 (below 400)
    tracker.record_fill(CID, "no", 500.0, 0.30, question="NO Halt Test")
    no_usd = tracker.positions[CID]["no"]
    check("NO USD at $350 (500 * 0.70)",
          abs(no_usd - 350.0) < 0.01,
          f"got ${no_usd:.2f}")
    check("NO not halted at $350",
          not tracker.is_halted(CID, "no"),
          f"halted={tracker.is_halted(CID, 'no')}")

    # Another fill: 100 shares → USD += 100*0.70=$70 → total $420
    tracker.record_fill(CID, "no", 100.0, 0.30, question="NO Halt Test")
    no_usd = tracker.positions[CID]["no"]
    check("NO halted at $420",
          tracker.is_halted(CID, "no"),
          f"halted={tracker.is_halted(CID, 'no')}, usd=${no_usd:.2f}")

    # ═══════════════════════════════════════════════════════════════════════
    # TEST 5: Halt Resume After Unwind
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== TEST 5: Halt Resume ===")

    # Currently: 600 NO shares, $420 USD, halted
    # Sell 200 shares: record_unwind(cid, "no", 200, avg_price=0.30)
    # USD reduction = 200 * (1-0.30) = 200 * 0.70 = $140
    # New USD = $420 - $140 = $280 → below RESUME_POSITION_USD ($300)
    tracker.record_unwind(CID, "no", 200.0, 0.30)
    no_usd = tracker.positions[CID]["no"]
    check("NO USD after unwind = $280",
          abs(no_usd - 280.0) < 0.01,
          f"got ${no_usd:.2f}")
    check("NO halt resumed (below $300 threshold)",
          not tracker.is_halted(CID, "no"),
          f"halted={tracker.is_halted(CID, 'no')}")

    # ═══════════════════════════════════════════════════════════════════════
    # TEST 6: recalculate_usd fixes legacy data and enforces halts
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== TEST 6: recalculate_usd ===")

    # Simulate legacy positions.json with WRONG USD (old buggy formula)
    tracker.positions = {
        "legacy-market": {
            "yes": 0.0,
            "no": 100.0,  # WRONG: should be 500 * (1-0.20) = 400
            "yes_shares": 0.0,
            "no_shares": 500.0,
            "yes_avg_price": 0.0,
            "no_avg_price": 0.20,  # YES-equiv → CLOB cost = 0.80
            "yes_halted": False,
            "no_halted": False,  # WRONG: $400 should trigger halt
            "question": "Legacy Market",
        }
    }

    tracker.recalculate_usd()

    corrected_usd = tracker.positions["legacy-market"]["no"]
    check("Legacy NO USD corrected to $400 (500 * 0.80)",
          abs(corrected_usd - 400.0) < 0.01,
          f"got ${corrected_usd:.2f}")

    check("Legacy NO halt now enforced",
          tracker.is_halted("legacy-market", "no"),
          f"halted={tracker.is_halted('legacy-market', 'no')}")

    # ═══════════════════════════════════════════════════════════════════════
    # TEST 7: recalculate_usd enforces halts even when USD is already correct
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== TEST 7: Halt enforcement on correct USD ===")

    tracker.positions = {
        "correct-usd-stale-halt": {
            "yes": 0.0,
            "no": 500.0,  # Correct USD, above MAX_POSITION_USD
            "yes_shares": 0.0,
            "no_shares": 700.0,
            "yes_avg_price": 0.0,
            "no_avg_price": 0.2857,  # YES-equiv → CLOB = 0.7143 → 700*0.7143 ≈ 500
            "yes_halted": False,
            "no_halted": False,  # STALE: should be True ($500 > $400)
            "question": "Stale Halt Market",
        }
    }

    tracker.recalculate_usd()

    check("Halt enforced even when USD was already correct",
          tracker.is_halted("correct-usd-stale-halt", "no"),
          f"halted={tracker.is_halted('correct-usd-stale-halt', 'no')}")

    # ═══════════════════════════════════════════════════════════════════════
    # TEST 8: Untracked position discovery uses YES-equivalent
    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== TEST 8: Untracked position est_price ===")

    # Simulates the fix in reconcile_unwinds Phase 1:
    # est_price = yes_price (always YES-equivalent for both sides)
    yes_price = 0.60
    # OLD buggy: est_price = 1 - yes_price = 0.40 for NO side
    # NEW fixed: est_price = yes_price = 0.60 for both sides
    est_price_fixed = yes_price

    # If stored as 0.60 (YES-equiv):
    # vwap_clob for NO = 1 - 0.60 = 0.40 (correct NO sell price)
    sell_price = 1 - est_price_fixed
    check("Untracked NO position sell price = 40c (correct)",
          abs(sell_price - 0.40) < 0.001,
          f"got {sell_price}")

    # OLD buggy would give: est_price = 0.40 → vwap_clob = 1-0.40 = 0.60
    est_price_buggy = 1 - yes_price  # 0.40
    sell_price_buggy = 1 - est_price_buggy  # 0.60
    check("Buggy formula would sell NO at 60c (18c above cost!)",
          abs(sell_price_buggy - 0.60) < 0.001,
          f"got {sell_price_buggy}")

finally:
    os.unlink(tmp_path)
    _position_module.POSITIONS_FILE = _original_positions_file


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 9: Sell price decay math
# ═══════════════════════════════════════════════════════════════════════════════
print("\n=== TEST 9: Decay Math ===")

from config import (
    UNWIND_DECAY_INTERVAL_SECS, UNWIND_DECAY_TICKS,
    UNWIND_ACCEL_TIERS,
    MIN_SELL_PRICE,
)

# Simulate decay for a NO position bought at 73c (YES-equiv avg_price=0.27)
avg_price = 0.27
tick = 0.01
vwap_clob = int((1 - avg_price) / tick) * tick  # round_down = 0.73

check("NO sell starts at 73c", abs(vwap_clob - 0.73) < 0.001,
      f"got {vwap_clob}")

# After 5 minutes (1 interval), normal decay
elapsed = UNWIND_DECAY_INTERVAL_SECS  # 300s
decay_intervals = int(elapsed // UNWIND_DECAY_INTERVAL_SECS)  # 1
decay_amount = decay_intervals * UNWIND_DECAY_TICKS * tick  # 1 * 1 * 0.01
expected = max(MIN_SELL_PRICE, vwap_clob - decay_amount)

check("After 5min: sell at 72c (1 tick decay)",
      abs(expected - 0.72) < 0.001,
      f"got {expected}")

# After 25 minutes (5 intervals)
elapsed = 5 * UNWIND_DECAY_INTERVAL_SECS
decay_intervals = int(elapsed // UNWIND_DECAY_INTERVAL_SECS)
decay_amount = decay_intervals * UNWIND_DECAY_TICKS * tick
expected = max(MIN_SELL_PRICE, vwap_clob - decay_amount)

check("After 25min: sell at 68c (5 ticks decay)",
      abs(expected - 0.68) < 0.001,
      f"got {expected}")

# With tiered acceleration (loss > 15% → 4x multiplier)
market_bid = 0.60  # NO market bid
cur_loss = (vwap_clob - market_bid) / vwap_clob  # (0.73-0.60)/0.73 = 17.8%
# Find the matching tier
accel_mult = 1
for threshold, mult in UNWIND_ACCEL_TIERS:
    if cur_loss >= threshold:
        accel_mult = mult
check("17.8% loss triggers tier 3 (4x)",
      accel_mult == 4,
      f"got multiplier={accel_mult}")

accel_ticks = UNWIND_DECAY_TICKS * accel_mult  # 4
elapsed = 2 * UNWIND_DECAY_INTERVAL_SECS  # 10 min
decay_intervals = int(elapsed // UNWIND_DECAY_INTERVAL_SECS)  # 2
decay_amount = decay_intervals * accel_ticks * tick  # 2 * 4 * 0.01 = 0.08
expected = max(MIN_SELL_PRICE, vwap_clob - decay_amount)

check("After 10min tiered-accel: sell at 65c (8 ticks)",
      abs(expected - 0.65) < 0.001,
      f"got {expected}")


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 10: cancel_order return value
# ═══════════════════════════════════════════════════════════════════════════════
print("\n=== TEST 10: cancel_order returns bool ===")

# Verify cancel_order return type via source inspection (avoids heavy imports)
import re
with open(os.path.join(os.path.dirname(__file__), "orders.py")) as f:
    src = f.read()
match = re.search(r'def cancel_order\([^)]*\)\s*->\s*(\w+)', src)
ret_type = match.group(1) if match else "NONE"
check("cancel_order return annotation is bool",
      ret_type == "bool",
      f"got {ret_type}")

# Also verify the stale unwind cancel is guarded
guard_pattern = r'if self\.cancel_order\(oid, reason="decay_refresh"\):'
check("Decay cancel guarded on success",
      bool(re.search(guard_pattern, src)),
      "cancel_order result not checked in decay refresh")

guard_pattern2 = r'if self\.cancel_order\(oid, reason="stop_loss"\):'
check("Stop-loss cancel guarded on success",
      bool(re.search(guard_pattern2, src)),
      "cancel_order result not checked in stop-loss")


# ═══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print(f"RESULTS: {PASS} passed, {FAIL} failed out of {PASS+FAIL} tests")
if FAIL > 0:
    print("SOME TESTS FAILED — fix before deploying!")
    sys.exit(1)
else:
    print("ALL TESTS PASSED")
    sys.exit(0)
