"""
Tests for the new state management module (state.py).

Covers every state transition, the derived-USD invariant, halt/resume
hysteresis, persistence backward compatibility, and edge cases.

Run: python3 test_state_v2.py
"""

import json
import os
import sys
import tempfile

# Setup path and logging
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logging
logging.basicConfig(level=logging.WARNING)

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


# Redirect positions.json to a temp file
import state as _state_module
_original_positions_file = _state_module.POSITIONS_FILE

with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
    json.dump({}, f)
    tmp_path = f.name
_state_module.POSITIONS_FILE = tmp_path

try:
    from state import SidePosition, PositionStore

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 1: SidePosition — derived USD
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 1: SidePosition Derived USD ===")

    yes = SidePosition("yes", shares=100.0, avg_price=0.30)
    check("YES USD = shares * price (100 * 0.30 = $30)",
          abs(yes.usd - 30.0) < 0.01,
          f"got ${yes.usd:.2f}")
    check("YES clob_cost = avg_price",
          abs(yes.clob_cost - 0.30) < 0.001,
          f"got {yes.clob_cost}")

    no = SidePosition("no", shares=200.0, avg_price=0.60)
    check("NO USD = shares * (1 - avg_price) (200 * 0.40 = $80)",
          abs(no.usd - 80.0) < 0.01,
          f"got ${no.usd:.2f}")
    check("NO clob_cost = 1 - avg_price",
          abs(no.clob_cost - 0.40) < 0.001,
          f"got {no.clob_cost}")

    empty = SidePosition("yes")
    check("Empty position USD = 0",
          empty.usd == 0.0,
          f"got ${empty.usd:.2f}")
    check("Empty position is_empty = True",
          empty.is_empty,
          f"got is_empty={empty.is_empty}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 2: SidePosition — record_fill VWAP
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 2: record_fill VWAP ===")

    sp = SidePosition("yes")
    sp.record_fill(100.0, 0.30)  # Buy 100 YES at 30c
    check("After first fill: shares=100",
          abs(sp.shares - 100.0) < 0.01,
          f"got {sp.shares}")
    check("After first fill: avg_price=0.30",
          abs(sp.avg_price - 0.30) < 0.001,
          f"got {sp.avg_price}")

    sp.record_fill(100.0, 0.50)  # Buy 100 more YES at 50c
    check("After second fill: shares=200",
          abs(sp.shares - 200.0) < 0.01,
          f"got {sp.shares}")
    check("After second fill: VWAP=0.40 ((30*100+50*100)/200)",
          abs(sp.avg_price - 0.40) < 0.001,
          f"got {sp.avg_price}")
    check("After second fill: USD=200*0.40=$80",
          abs(sp.usd - 80.0) < 0.01,
          f"got ${sp.usd:.2f}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 3: SidePosition — NO fill USD via derived
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 3: NO Fill Derived USD ===")

    no_sp = SidePosition("no")
    # BUY NO: YES-equiv price = 0.60, CLOB cost = 0.40
    no_sp.record_fill(500.0, 0.60)
    check("NO 500 shares at YES-equiv 0.60: USD = 500 * 0.40 = $200",
          abs(no_sp.usd - 200.0) < 0.01,
          f"got ${no_sp.usd:.2f}")
    check("NO avg_price = 0.60 (YES-equiv)",
          abs(no_sp.avg_price - 0.60) < 0.001,
          f"got {no_sp.avg_price}")
    check("NO clob_cost = 0.40",
          abs(no_sp.clob_cost - 0.40) < 0.001,
          f"got {no_sp.clob_cost}")

    # Add more at different price
    # BUY NO: YES-equiv price = 0.70, CLOB cost = 0.30
    no_sp.record_fill(500.0, 0.70)
    expected_avg = (0.60 * 500 + 0.70 * 500) / 1000  # 0.65
    expected_usd = 1000 * (1 - 0.65)  # 1000 * 0.35 = $350
    check("NO 1000 shares VWAP=0.65: USD = 1000 * 0.35 = $350",
          abs(no_sp.usd - expected_usd) < 0.01,
          f"got ${no_sp.usd:.2f}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 4: record_unwind — no price needed
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 4: record_unwind ===")

    sp = SidePosition("no", shares=1000.0, avg_price=0.60)
    initial_usd = sp.usd  # 1000 * 0.40 = $400
    check("Before unwind: USD = $400",
          abs(initial_usd - 400.0) < 0.01,
          f"got ${initial_usd:.2f}")

    sp.record_unwind(500.0)
    check("After unwind 500: shares=500",
          abs(sp.shares - 500.0) < 0.01,
          f"got {sp.shares}")
    check("After unwind 500: USD = 500 * 0.40 = $200",
          abs(sp.usd - 200.0) < 0.01,
          f"got ${sp.usd:.2f}")
    check("VWAP unchanged after unwind",
          abs(sp.avg_price - 0.60) < 0.001,
          f"got {sp.avg_price}")

    # Unwind all remaining
    sp.record_unwind(500.0)
    check("After full unwind: shares=0",
          sp.shares == 0.0,
          f"got {sp.shares}")
    check("After full unwind: USD=0",
          sp.usd == 0.0,
          f"got ${sp.usd:.2f}")
    check("After full unwind: avg_price=0 (cleaned)",
          sp.avg_price == 0.0,
          f"got {sp.avg_price}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 5: Halt/Resume hysteresis
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 5: Halt/Resume Hysteresis ===")

    sp = SidePosition("yes", shares=1000.0, avg_price=0.39)
    check("At $390: not halted",
          not sp.halted and sp.usd < MAX_POSITION_USD,
          f"usd=${sp.usd:.2f}, halted={sp.halted}")

    sp.record_fill(50.0, 0.39)  # Now 1050 * 0.39 = $409.50
    sp.check_limits()
    check("At $409.50: halted",
          sp.halted,
          f"usd=${sp.usd:.2f}, halted={sp.halted}")

    # Sell 100 shares: 950 * 0.39 = $370.50 — still above RESUME ($300)
    sp.record_unwind(100.0)
    sp.check_limits()
    check("At $370.50: still halted (hysteresis)",
          sp.halted,
          f"usd=${sp.usd:.2f}, halted={sp.halted}")

    # Sell more: 180 shares → 770 * 0.39 = $300.30 — still above RESUME
    sp.record_unwind(180.0)
    sp.check_limits()
    check("At $300.30: still halted (barely above resume)",
          sp.halted,
          f"usd=${sp.usd:.2f}, halted={sp.halted}")

    # Sell 2 more: 768 * 0.39 = $299.52 — below RESUME
    sp.record_unwind(2.0)
    sp.check_limits()
    check("At $299.52: resumed (below $300)",
          not sp.halted,
          f"usd=${sp.usd:.2f}, halted={sp.halted}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 6: NO-side halt uses CLOB cost
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 6: NO-Side Halt ===")

    no_sp = SidePosition("no")
    # BUY NO: 500 shares, YES-equiv 0.30, CLOB cost 0.70
    # USD = 500 * 0.70 = $350 (below $400 halt)
    no_sp.record_fill(500.0, 0.30)
    no_sp.check_limits()
    check("NO $350: not halted",
          not no_sp.halted,
          f"usd=${no_sp.usd:.2f}")

    # 100 more: 600 * 0.70 = $420 (above $400 halt)
    no_sp.record_fill(100.0, 0.30)
    no_sp.check_limits()
    check("NO $420: halted",
          no_sp.halted,
          f"usd=${no_sp.usd:.2f}")

    # BUG CHECK: Old system would compute NO USD as 600 * 0.30 = $180 (wrong!)
    # and never halt. New system correctly uses 600 * 0.70 = $420.
    wrong_usd = 600 * 0.30  # $180 — the old bug
    check("Old buggy USD would be $180 (not halted) — we compute $420",
          abs(no_sp.usd - 420.0) < 0.01 and abs(wrong_usd - 180.0) < 0.01,
          f"new=${no_sp.usd:.2f}, old_bug=${wrong_usd:.2f}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 7: PositionStore — full API
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 7: PositionStore Full API ===")

    store = PositionStore()
    store._markets = {}  # Clean slate

    CID = "test-market-001"
    store.register_market(CID, "Test Market")

    # record_fill YES
    store.record_fill(CID, "yes", 100.0, 0.30, question="Test Market")
    check("get_shares YES = 100",
          abs(store.get_shares(CID, "yes") - 100.0) < 0.01,
          f"got {store.get_shares(CID, 'yes')}")
    check("get_avg_price YES = 0.30",
          abs(store.get_avg_price(CID, "yes") - 0.30) < 0.001,
          f"got {store.get_avg_price(CID, 'yes')}")
    check("get_position YES (USD) = $30",
          abs(store.get_position(CID, "yes") - 30.0) < 0.01,
          f"got ${store.get_position(CID, 'yes'):.2f}")

    # record_fill NO
    store.record_fill(CID, "no", 200.0, 0.60, question="Test Market")
    check("get_shares NO = 200",
          abs(store.get_shares(CID, "no") - 200.0) < 0.01,
          f"got {store.get_shares(CID, 'no')}")
    check("get_position NO (USD) = $80",
          abs(store.get_position(CID, "no") - 80.0) < 0.01,
          f"got ${store.get_position(CID, 'no'):.2f}")

    # record_unwind — price parameter ignored
    store.record_unwind(CID, "yes", 50.0)
    check("After unwind YES 50: shares=50",
          abs(store.get_shares(CID, "yes") - 50.0) < 0.01,
          f"got {store.get_shares(CID, 'yes')}")
    check("After unwind YES 50: USD=$15",
          abs(store.get_position(CID, "yes") - 15.0) < 0.01,
          f"got ${store.get_position(CID, 'yes'):.2f}")

    # can_quote
    check("can_quote YES (not halted) = True",
          store.can_quote(CID, "yes"),
          f"got {store.can_quote(CID, 'yes')}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 8: PositionStore — halt via record_fill
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 8: PositionStore Halt via Fill ===")

    store._markets = {}
    store.register_market(CID, "Halt Test")

    # Fill YES to above limit: 1100 * 0.39 = $429
    store.record_fill(CID, "yes", 1100.0, 0.39, question="Halt Test")
    check("YES halted after large fill ($429)",
          store.is_halted(CID, "yes"),
          f"halted={store.is_halted(CID, 'yes')}, usd=${store.get_position(CID, 'yes'):.2f}")
    check("can_quote returns False when halted",
          not store.can_quote(CID, "yes"),
          f"can_quote={store.can_quote(CID, 'yes')}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 9: PositionStore — recalculate_usd (halt enforcement)
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 9: recalculate_usd ===")

    store._markets = {}
    store.register_market(CID, "Stale Halt Test")

    # Manually set a position that should be halted but isn't
    sp = store._get_side(CID, "yes")
    sp.shares = 1500.0
    sp.avg_price = 0.40  # USD = 1500 * 0.40 = $600
    sp.halted = False     # Stale — should be halted

    store.recalculate_usd()
    check("recalculate_usd enforces halt on stale position",
          store.is_halted(CID, "yes"),
          f"halted={store.is_halted(CID, 'yes')}, usd=${store.get_position(CID, 'yes'):.2f}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 10: Persistence — write & reload
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 10: Persistence ===")

    store._markets = {}
    store.register_market(CID, "Persist Test")
    store.record_fill(CID, "no", 300.0, 0.70, question="Persist Test")

    # Force save (already happened in record_fill)
    # Read the JSON directly
    with open(tmp_path, "r") as f:
        saved = json.load(f)

    check("Saved JSON has condition_id",
          CID in saved,
          f"keys: {list(saved.keys())}")

    pos = saved[CID]
    check("Saved no_shares = 300",
          abs(pos["no_shares"] - 300.0) < 0.01,
          f"got {pos['no_shares']}")
    check("Saved no_avg_price = 0.70",
          abs(pos["no_avg_price"] - 0.70) < 0.001,
          f"got {pos['no_avg_price']}")
    # USD is written for debugging — check it's derived correctly
    expected_no_usd = 300 * (1 - 0.70)  # 300 * 0.30 = $90
    check("Saved 'no' (USD) is derived correctly = $90",
          abs(pos["no"] - expected_no_usd) < 0.01,
          f"got ${pos['no']:.2f}")

    # Reload from disk
    store2 = PositionStore()
    check("Reloaded shares match",
          abs(store2.get_shares(CID, "no") - 300.0) < 0.01,
          f"got {store2.get_shares(CID, 'no')}")
    check("Reloaded USD is derived (not from stored 'no' field)",
          abs(store2.get_position(CID, "no") - 90.0) < 0.01,
          f"got ${store2.get_position(CID, 'no'):.2f}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 11: Backward compat — load old format positions.json
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 11: Old Format Backward Compat ===")

    # Write old-format JSON (with flat keys + wrong USD)
    old_format = {
        "0xabc123": {
            "yes": 9999.0,           # Wrong USD — should be IGNORED on load
            "no": 480.69,            # Wrong USD (the old bug) — should be IGNORED
            "yes_shares": 0.0,
            "no_shares": 1809.0,
            "yes_avg_price": 0.0,
            "no_avg_price": 0.265728,
            "yes_halted": False,
            "no_halted": False,      # Should be True (USD > $400)
            "question": "Crude Oil Test",
        }
    }
    with open(tmp_path, "w") as f:
        json.dump(old_format, f)

    store3 = PositionStore()

    # The stored "no" USD was 480.69 (wrong). Correct value:
    # 1809 * (1 - 0.265728) = 1809 * 0.734272 = $1328.35
    correct_usd = 1809.0 * (1 - 0.265728)
    check("Old-format NO USD is DERIVED, not from stored value",
          abs(store3.get_position("0xabc123", "no") - correct_usd) < 1.0,
          f"got ${store3.get_position('0xabc123', 'no'):.2f}, expected ${correct_usd:.2f}")
    check("Old-format stored 'no' was $480.69 (wrong), we compute $1328",
          abs(store3.get_position("0xabc123", "no") - 480.69) > 800,
          f"if this fails, we're using stored USD")

    # After recalculate_usd, halt should be enforced
    store3.recalculate_usd()
    check("Old format: halt enforced after recalculate_usd",
          store3.is_halted("0xabc123", "no"),
          f"halted={store3.is_halted('0xabc123', 'no')}, usd=${store3.get_position('0xabc123', 'no'):.2f}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 12: get_all_positions backward compat format
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 12: get_all_positions Format ===")

    store._markets = {}
    store.register_market("cid1", "Market 1")
    store.record_fill("cid1", "yes", 100.0, 0.50, question="Market 1")

    all_pos = store.get_all_positions()
    pos = all_pos["cid1"]
    check("get_all_positions has 'yes' (USD) field",
          "yes" in pos,
          f"keys: {list(pos.keys())}")
    check("get_all_positions has 'yes_shares' field",
          "yes_shares" in pos,
          f"keys: {list(pos.keys())}")
    check("get_all_positions 'yes' = derived USD ($50)",
          abs(pos["yes"] - 50.0) < 0.01,
          f"got {pos['yes']}")
    check("get_all_positions 'question' field present",
          pos["question"] == "Market 1",
          f"got {pos.get('question')}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 13: Edge cases
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 13: Edge Cases ===")

    # Unwind more than held (overshoot protection)
    sp = SidePosition("yes", shares=10.0, avg_price=0.50)
    sp.record_unwind(20.0)  # More than held
    check("Overshoot unwind: shares clamped to 0",
          sp.shares == 0.0,
          f"got {sp.shares}")
    check("Overshoot unwind: avg_price cleaned to 0",
          sp.avg_price == 0.0,
          f"got {sp.avg_price}")

    # Dust position cleanup
    sp = SidePosition("no", shares=0.5, avg_price=0.30)
    check("Dust position (0.5 shares) is_empty = True",
          sp.is_empty,
          f"is_empty={sp.is_empty}")

    # Zero avg_price
    sp = SidePosition("yes", shares=100.0, avg_price=0.0)
    check("Zero avg_price: USD = 0 (not NaN/error)",
          sp.usd == 0.0,
          f"got {sp.usd}")
    check("Zero avg_price: clob_cost = 0",
          sp.clob_cost == 0.0,
          f"got {sp.clob_cost}")

    # correct_to_exchange
    sp = SidePosition("no", shares=500.0, avg_price=0.40)
    sp.correct_to_exchange(300.0)
    check("correct_to_exchange: shares updated",
          abs(sp.shares - 300.0) < 0.01,
          f"got {sp.shares}")
    check("correct_to_exchange: VWAP preserved",
          abs(sp.avg_price - 0.40) < 0.001,
          f"got {sp.avg_price}")
    check("correct_to_exchange: USD recomputed (300 * 0.60 = $180)",
          abs(sp.usd - 180.0) < 0.01,
          f"got ${sp.usd:.2f}")

    sp.correct_to_exchange(0.0)
    check("correct_to_exchange(0): fully reset",
          sp.shares == 0.0 and sp.avg_price == 0.0,
          f"shares={sp.shares}, avg={sp.avg_price}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 14: record_fill returns CLOB cost
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 14: record_fill Return Value ===")

    sp = SidePosition("yes")
    cost = sp.record_fill(100.0, 0.30)
    check("YES fill returns CLOB cost (100 * 0.30 = $30)",
          abs(cost - 30.0) < 0.01,
          f"got ${cost:.2f}")

    sp = SidePosition("no")
    cost = sp.record_fill(100.0, 0.60)
    check("NO fill returns CLOB cost (100 * 0.40 = $40)",
          abs(cost - 40.0) < 0.01,
          f"got ${cost:.2f}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 15: PositionStore — auto-registration
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 15: Auto-Registration ===")

    store._markets = {}
    # record_fill on unregistered market should auto-register
    store.record_fill("new-cid", "yes", 50.0, 0.40, question="Auto Market")
    check("Auto-registered market exists",
          store.get_shares("new-cid", "yes") > 0,
          f"shares={store.get_shares('new-cid', 'yes')}")
    all_pos = store.get_all_positions()
    check("Auto-registered question captured",
          all_pos["new-cid"]["question"] == "Auto Market",
          f"got {all_pos['new-cid'].get('question')}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 16: USD never drifts from true value
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 16: USD Never Drifts ===")

    # Simulate 50 fills and 30 unwinds, check USD matches derived
    import random
    random.seed(42)
    sp = SidePosition("no")
    for _ in range(50):
        shares = random.uniform(10, 200)
        price = random.uniform(0.20, 0.80)
        sp.record_fill(shares, price)
    for _ in range(30):
        shares = random.uniform(5, 50)
        sp.record_unwind(shares)

    # USD must ALWAYS equal shares * clob_cost
    expected = sp.shares * sp.clob_cost
    check("After 80 operations: USD == shares * clob_cost (exact)",
          abs(sp.usd - expected) < 0.001,
          f"usd=${sp.usd:.4f}, expected=${expected:.4f}")

    # Verify it's not possible for USD to be wrong — it's a property, not state
    # This is the fundamental improvement: there IS no stored USD to corrupt
    check("SidePosition has no 'usd' in __slots__ (it's a property)",
          "usd" not in SidePosition.__slots__,
          f"__slots__={SidePosition.__slots__}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 17: Sell price computation (for orders.py compatibility)
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 17: Sell Price Computation ===")

    # YES sell: CLOB price = avg_price
    yes_sp = SidePosition("yes", shares=100.0, avg_price=0.30)
    yes_sell_clob = yes_sp.avg_price
    check("YES sell CLOB price = avg_price (0.30)",
          abs(yes_sell_clob - 0.30) < 0.001,
          f"got {yes_sell_clob}")

    # NO sell: CLOB price = 1 - avg_price
    no_sp = SidePosition("no", shares=200.0, avg_price=0.60)
    no_sell_clob = 1 - no_sp.avg_price
    check("NO sell CLOB price = 1 - avg_price (0.40)",
          abs(no_sell_clob - 0.40) < 0.001,
          f"got {no_sell_clob}")

    # This matches what reconcile_unwinds computes:
    # vwap_clob = avg_price (YES) or round_down(1 - avg_price) (NO)
    check("NO clob_cost == sell price (both = 1 - avg_price)",
          abs(no_sp.clob_cost - no_sell_clob) < 0.001,
          f"clob_cost={no_sp.clob_cost}, sell={no_sell_clob}")

finally:
    os.unlink(tmp_path)
    _state_module.POSITIONS_FILE = _original_positions_file


# ═════════════════════════════════════════════════════════════════════════════
# Results
# ═════════════════════════════════════════════════════════════════════════════
print(f"\n{'=' * 60}")
print(f"RESULTS: {PASS} passed, {FAIL} failed out of {PASS + FAIL} tests")
if FAIL == 0:
    print("ALL TESTS PASSED")
else:
    print(f"FAILURES DETECTED")
    sys.exit(1)
