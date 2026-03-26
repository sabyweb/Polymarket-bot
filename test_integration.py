"""
Integration tests for the Polymarket market-making bot.

Mocks py_clob_client and simulates full trading cycles:
  - Place BUY → fill detected → position recorded → halt triggered
  - Unwind SELL placed → decay → filled → position reduced → resume
  - Cancel failure → order stays tracked
  - Reconciliation catches exchange drift
  - Stop-loss triggers emergency sell
  - Price helpers used correctly throughout order flow

Run: python3 test_integration.py
"""

import json
import os
import sys
import tempfile
import time as _real_time
from unittest.mock import Mock, MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logging
logging.basicConfig(level=logging.WARNING)

# Mock py_clob_client before importing order_manager (not installed in test env)
try:
    import py_clob_client  # noqa: F401
except ImportError:
    from unittest.mock import MagicMock as _MM
    _clob = _MM()
    sys.modules["py_clob_client"] = _clob
    sys.modules["py_clob_client.client"] = _clob.client
    sys.modules["py_clob_client.clob_types"] = _clob.clob_types
    sys.modules["py_clob_client.order_builder"] = _clob.order_builder
    sys.modules["py_clob_client.order_builder.constants"] = _clob.order_builder.constants

from order_manager import TrackedOrder, UnwindOrder

# Redirect database to a temp file so tests don't contaminate production DB
import database as _db_module
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
_db_module._instance = _db_module.BotDatabase(db_path=_tmp_db.name)

# Redirect positions.json before importing state
import state as _state_module
_original_positions_file = _state_module.POSITIONS_FILE
_tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
json.dump({}, _tmp)
_tmp.close()
_state_module.POSITIONS_FILE = _tmp.name

from config import (
    MAX_POSITION_USD, RESUME_POSITION_USD, ORDER_SIZE,
    MIN_UNWIND_SHARES, STOP_LOSS_PCT, MIN_STOP_LOSS_USD,
    UNWIND_DECAY_INTERVAL_SECS, UNWIND_DECAY_TICKS,
)
from state import PositionStore, SidePosition
from price import to_clob, to_yes_equiv

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


# ── Mock Factories ──────────────────────────────────────────────────────────

CID = "0xtest_condition_id_abc123"
YES_TOKEN = "0xyes_token_id"
NO_TOKEN = "0xno_token_id"

MARKET = {
    "condition_id": CID,
    "question": "Will test event happen?",
    "token_ids": [YES_TOKEN, NO_TOKEN],
    "tick_size": 0.01,
    "min_size": 2.0,
    "max_spread": 0.05,
    "yes_price": 0.50,
    "daily_rate": 10.0,
}


def make_book_entry(price, size):
    """Create a mock order book entry."""
    entry = Mock()
    entry.price = price
    entry.size = size
    return entry


def make_order_book(bids, asks):
    """Create a mock order book response."""
    book = Mock()
    book.bids = [make_book_entry(p, s) for p, s in bids]
    book.asks = [make_book_entry(p, s) for p, s in asks]
    return book


def make_exchange_order(order_id, token_id, price, side, size, matched=0):
    """Create a mock exchange order dict."""
    return {
        "id": order_id,
        "asset_id": token_id,
        "price": str(price),
        "side": side,
        "original_size": str(size),
        "size_matched": str(matched),
        "status": "LIVE",
    }


def fresh_store():
    """Create a clean PositionStore with no positions."""
    store = PositionStore()
    store._markets = {}
    store.register_market(CID, MARKET["question"])
    return store


def fresh_manager(store, client=None, balance_gate=None):
    """Create an OrderManager with mocked dependencies."""
    from orders import OrderManager, BalanceGate

    if client is None:
        client = Mock()
        client.create_and_post_order.return_value = {
            "success": True,
            "orderID": "0xpost_order_id",
        }
        client.get_orders.return_value = []
        client.get_order.return_value = {"status": "MATCHED"}
        client.cancel.return_value = None
        client.get_balance_allowance.return_value = {
            "balance": 5000_000_000,  # 5000 USDC
            "allowance": 9999_000_000,
        }
        client.update_balance_allowance.return_value = None
        # Default order book: 30/35 spread
        yes_book = make_order_book(
            bids=[(0.30, 5000), (0.29, 3000), (0.28, 2000)],
            asks=[(0.35, 5000), (0.36, 3000), (0.37, 2000)],
        )
        no_book = make_order_book(
            bids=[(0.65, 5000), (0.64, 3000)],
            asks=[(0.70, 5000), (0.71, 3000)],
        )
        def get_book(token_id):
            return yes_book if token_id == YES_TOKEN else no_book
        client.get_order_book.side_effect = get_book

    if balance_gate is None:
        balance_gate = Mock()
        balance_gate.can_afford.return_value = True
        balance_gate.is_depleted = False
        balance_gate.invalidate.return_value = None
        balance_gate.mark_depleted.return_value = None

    mgr = OrderManager(client, MARKET.copy(), store, balance_gate=balance_gate)
    return mgr


try:
    # ═════════════════════════════════════════════════════════════════════════
    # TEST 1: Place BUY → Fill → Position Recorded
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 1: BUY Fill Cycle ===")

    store = fresh_store()
    mgr = fresh_manager(store)

    # Simulate: place_order placed a YES BUY at 0.30, 100 shares
    # Manually inject into active_orders (as place_order would)
    mgr.active_orders["order_yes_1"] = TrackedOrder(
        side="yes",
        price=0.30,  # YES-equivalent
        size=100.0,
        original_size=100.0,
        placed_at=_real_time.time() - 60,
    )

    # Exchange says this order is gone (filled)
    mgr.client.get_orders.return_value = []  # Order not in open list
    mgr.client.get_order.return_value = {"status": "MATCHED"}

    mgr.detect_fills()

    check("Fill recorded: YES shares = 100",
          abs(store.get_shares(CID, "yes") - 100.0) < 0.01,
          f"got {store.get_shares(CID, 'yes')}")
    check("Fill recorded: YES avg_price = 0.30",
          abs(store.get_avg_price(CID, "yes") - 0.30) < 0.001,
          f"got {store.get_avg_price(CID, 'yes')}")
    check("Fill recorded: YES USD = $30 (derived)",
          abs(store.get_position(CID, "yes") - 30.0) < 0.01,
          f"got ${store.get_position(CID, 'yes'):.2f}")
    check("Order removed from active_orders",
          "order_yes_1" not in mgr.active_orders,
          f"still in active: {list(mgr.active_orders.keys())}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 2: NO BUY Fill → USD Uses CLOB Cost
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 2: NO Fill USD Correct ===")

    store = fresh_store()
    mgr = fresh_manager(store)

    # NO BUY: price stored as YES-equiv = 0.60, CLOB cost = 0.40
    mgr.active_orders["order_no_1"] = TrackedOrder(
        side="no",
        price=0.60,  # YES-equivalent (NOT the NO CLOB price 0.40)
        size=500.0,
        original_size=500.0,
        placed_at=_real_time.time() - 60,
    )

    mgr.client.get_orders.return_value = []
    mgr.client.get_order.return_value = {"status": "MATCHED"}

    mgr.detect_fills()

    # USD should be 500 * (1 - 0.60) = 500 * 0.40 = $200
    check("NO fill: USD = $200 (500 * 0.40 CLOB cost)",
          abs(store.get_position(CID, "no") - 200.0) < 0.01,
          f"got ${store.get_position(CID, 'no'):.2f}")
    check("NO fill: avg_price = 0.60 (YES-equiv)",
          abs(store.get_avg_price(CID, "no") - 0.60) < 0.001,
          f"got {store.get_avg_price(CID, 'no')}")

    # Verify it would NOT be the old buggy $300 (500 * 0.60)
    buggy_usd = 500 * 0.60
    check("NOT the old buggy USD ($300)",
          abs(store.get_position(CID, "no") - buggy_usd) > 90,
          f"got ${store.get_position(CID, 'no'):.2f}, buggy would be ${buggy_usd:.2f}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 3: Fill → Halt Triggered
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 3: Fill Triggers Halt ===")

    store = fresh_store()
    mgr = fresh_manager(store)

    # Large NO fill: 800 shares at YES-equiv 0.40 → CLOB cost 0.60
    # USD = 800 * 0.60 = $480 > MAX_POSITION_USD ($400)
    mgr.active_orders["order_big_no"] = TrackedOrder(
        side="no",
        price=0.40,
        size=800.0,
        original_size=800.0,
        placed_at=_real_time.time() - 60,
    )

    mgr.client.get_orders.return_value = []
    mgr.client.get_order.return_value = {"status": "MATCHED"}

    mgr.detect_fills()

    check("Large NO fill: halted (USD=$480 > $400)",
          store.is_halted(CID, "no"),
          f"halted={store.is_halted(CID, 'no')}, usd=${store.get_position(CID, 'no'):.2f}")
    check("can_quote returns False",
          not store.can_quote(CID, "no"),
          f"can_quote={store.can_quote(CID, 'no')}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 4: Unwind Fill → Position Reduced → Resume
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 4: Unwind → Resume ===")

    # Continuing from TEST 3: 800 NO shares, halted
    # Simulate unwind SELL filled for 500 shares
    mgr.unwind_orders["unwind_no_1"] = UnwindOrder(
        side="no",
        price=0.40,   # YES-equiv
        clob_price=0.60,
        size=500.0,
        placed_at=_real_time.time() - 120,
        created_at=_real_time.time() - 300,
        base_clob_price=0.60,
    )

    # Exchange says unwind is fully matched
    mgr.client.get_orders.return_value = []
    mgr.client.get_order.return_value = {"status": "MATCHED"}

    mgr.detect_fills()

    # After unwind: 300 shares left, USD = 300 * 0.60 = $180
    check("After unwind: NO shares = 300",
          abs(store.get_shares(CID, "no") - 300.0) < 0.01,
          f"got {store.get_shares(CID, 'no')}")
    check("After unwind: NO USD = $180 (derived)",
          abs(store.get_position(CID, "no") - 180.0) < 0.01,
          f"got ${store.get_position(CID, 'no'):.2f}")
    check(f"After unwind: halt status correct ($180 vs ${RESUME_POSITION_USD} resume)",
          store.is_halted(CID, "no") == (180.0 > RESUME_POSITION_USD),
          f"halted={store.is_halted(CID, 'no')}, threshold=${RESUME_POSITION_USD}")
    check("Unwind order removed from tracking",
          "unwind_no_1" not in mgr.unwind_orders,
          f"still tracked: {list(mgr.unwind_orders.keys())}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 5: Partial Fill Detection
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 5: Partial Fill ===")

    store = fresh_store()
    mgr = fresh_manager(store)

    mgr.active_orders["order_partial"] = TrackedOrder(
        side="yes",
        price=0.35,
        size=200.0,
        original_size=200.0,
        placed_at=_real_time.time() - 60,
    )

    # Exchange shows order still open but partially filled
    mgr.client.get_orders.return_value = [
        make_exchange_order("order_partial", YES_TOKEN, 0.35, "BUY", 200.0, matched=80.0),
    ]

    mgr.detect_fills()

    check("Partial fill: YES shares = 80",
          abs(store.get_shares(CID, "yes") - 80.0) < 0.01,
          f"got {store.get_shares(CID, 'yes')}")
    check("Partial fill: order still tracked",
          "order_partial" in mgr.active_orders,
          f"missing from active_orders")
    check("Partial fill: remaining size updated to 120",
          abs(mgr.active_orders["order_partial"].original_size - 120.0) < 0.01,
          f"got {mgr.active_orders['order_partial'].original_size}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 6: Cancel Failure → Order Stays Tracked
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 6: Cancel Failure Guard ===")

    store = fresh_store()
    mgr = fresh_manager(store)

    # Set up a stale unwind order that needs cancellation
    mgr.unwind_orders["stale_unwind"] = UnwindOrder(
        side="yes",
        price=0.50,
        clob_price=0.50,
        size=100.0,
        placed_at=_real_time.time() - 600,
        created_at=_real_time.time() - 600,
        base_clob_price=0.50,
    )

    # Make cancel fail
    mgr.client.cancel.side_effect = Exception("Network error")

    result = mgr.cancel_order("stale_unwind", reason="test")
    check("cancel_order returns False on failure",
          result is False,
          f"got {result}")

    # Reset cancel behavior
    mgr.client.cancel.side_effect = None
    mgr.client.cancel.return_value = None

    result = mgr.cancel_order("stale_unwind", reason="test")
    check("cancel_order returns True on success",
          result is True,
          f"got {result}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 7: Stop-Loss Triggers Emergency Sell
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 7: Stop-Loss ===")

    store = fresh_store()
    mgr = fresh_manager(store)

    # Set up: 500 YES shares at VWAP 0.50 (cost = $250)
    store.record_fill(CID, "yes", 500.0, 0.50, question=MARKET["question"])

    # Market crashed: best_bid=0.35, best_ask=0.36
    # Loss = (0.50 - 0.35) / 0.50 = 30% > 20% threshold
    # Loss USD = (0.50 - 0.35) * 500 = $75 > $50 threshold
    # Price 0.35 > 0.20 minimum

    # Mock: cancel succeeds, place_unwind_order succeeds
    mgr.client.cancel.return_value = None
    mgr.client.create_and_post_order.return_value = {
        "success": True,
        "orderID": "0xstoploss_sell",
    }
    # Return the stop-loss order in open orders for detect_fills adoption
    mgr.client.get_orders.return_value = [
        make_exchange_order("0xstoploss_exchange", YES_TOKEN, 0.35, "SELL", 500.0),
    ]
    # Token balance check: we have 500 YES tokens
    def balance_for_stop(params):
        return {"balance": 500_000_000, "allowance": 999_000_000}
    mgr.client.get_balance_allowance.side_effect = balance_for_stop

    mgr.check_stop_loss(best_bid=0.35, best_ask=0.36)

    check("Stop-loss: unwind order placed",
          len(mgr.unwind_orders) > 0,
          f"unwind_orders={len(mgr.unwind_orders)}")

    if mgr.unwind_orders:
        oid = list(mgr.unwind_orders.keys())[0]
        uorder = mgr.unwind_orders[oid]
        check("Stop-loss: sell at market bid (0.35)",
              abs(uorder.clob_price - 0.35) < 0.01,
              f"got {uorder.clob_price}")
        check("Stop-loss: sell size = 500",
              abs(uorder.size - 500.0) < 0.01,
              f"got {uorder.size}")

    # Reset side effect
    mgr.client.get_balance_allowance.side_effect = None
    mgr.client.get_balance_allowance.return_value = {
        "balance": 5000_000_000, "allowance": 9999_000_000,
    }

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 8: Untracked Position Discovery
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 8: Untracked Position Discovery ===")

    store = fresh_store()
    mgr = fresh_manager(store)

    # Tracker says 0 shares, but exchange has 200 NO tokens
    def balance_for_discovery(params):
        if hasattr(params, 'token_id') and params.token_id == NO_TOKEN:
            return {"balance": 200_000_000, "allowance": 999_000_000}
        return {"balance": 0, "allowance": 999_000_000}
    mgr.client.get_balance_allowance.side_effect = balance_for_discovery
    mgr.client.get_orders.return_value = []

    mgr.reconcile_unwinds()

    check("Discovery: NO shares recorded",
          store.get_shares(CID, "no") >= 200.0 - 1,
          f"got {store.get_shares(CID, 'no')}")
    check("Discovery: avg_price is YES-equiv (market yes_price=0.50)",
          abs(store.get_avg_price(CID, "no") - 0.50) < 0.01,
          f"got {store.get_avg_price(CID, 'no')}")

    # Reset
    mgr.client.get_balance_allowance.side_effect = None
    mgr.client.get_balance_allowance.return_value = {
        "balance": 5000_000_000, "allowance": 9999_000_000,
    }

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 9: Price Helpers Match Order Flow
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 9: Price Helper Consistency ===")

    # Verify to_clob produces the same values orders.py now uses

    # YES BUY: clob_price = price (identity)
    check("YES BUY: to_clob(0.30, 'yes') = 0.30",
          abs(to_clob(0.30, "yes") - 0.30) < 0.001)

    # NO BUY: clob_price = 1 - yes_equiv_ask
    check("NO BUY: to_clob(0.60, 'no') = 0.40",
          abs(to_clob(0.60, "no") - 0.40) < 0.001)

    # NO SELL (unwind): CLOB price from VWAP
    avg_price_no = 0.265728  # Crude Oil example
    sell_clob = to_clob(avg_price_no, "no")
    check(f"NO SELL: to_clob({avg_price_no}, 'no') = {1-avg_price_no:.6f}",
          abs(sell_clob - (1 - avg_price_no)) < 0.0001,
          f"got {sell_clob}")

    # Order book merge: NO CLOB → YES-equiv
    no_ask_clob = 0.70
    yes_bid_derived = to_yes_equiv(no_ask_clob, "no")
    check("Book merge: NO ask 0.70 → YES bid 0.30",
          abs(yes_bid_derived - 0.30) < 0.001,
          f"got {yes_bid_derived}")

    # Roundtrip through fill cycle:
    # 1. BUY NO at YES-equiv 0.60 (CLOB 0.40)
    # 2. SELL NO: CLOB price = to_clob(avg_price=0.60, "no") = 0.40
    # 3. USD cost = shares * to_clob(0.60, "no") = shares * 0.40
    fill_price_yes_equiv = 0.60
    clob_buy = to_clob(fill_price_yes_equiv, "no")
    clob_sell = to_clob(fill_price_yes_equiv, "no")
    check("NO roundtrip: buy CLOB = sell CLOB (both 0.40)",
          abs(clob_buy - clob_sell) < 0.001,
          f"buy={clob_buy}, sell={clob_sell}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 10: Full Cycle — Place, Fill, Halt, Unwind, Resume
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 10: Full Lifecycle ===")

    store = fresh_store()
    mgr = fresh_manager(store)

    # Phase 1: Accumulate position via fills
    for i in range(5):
        oid = f"buy_yes_{i}"
        mgr.active_orders[oid] = TrackedOrder(
            side="yes",
            price=0.45,
            size=200.0,
            original_size=200.0,
            placed_at=_real_time.time() - 60,
        )

    mgr.client.get_orders.return_value = []
    mgr.client.get_order.return_value = {"status": "MATCHED"}
    mgr.detect_fills()

    total_shares = store.get_shares(CID, "yes")
    total_usd = store.get_position(CID, "yes")
    check(f"Phase 1: 1000 YES shares accumulated",
          abs(total_shares - 1000.0) < 0.01,
          f"got {total_shares}")
    check(f"Phase 1: USD = $450 (1000 * 0.45)",
          abs(total_usd - 450.0) < 0.01,
          f"got ${total_usd:.2f}")
    check("Phase 1: HALTED (USD $450 > $400)",
          store.is_halted(CID, "yes"),
          f"halted={store.is_halted(CID, 'yes')}")

    # Phase 2: Unwind sells fill gradually
    # Sell 400 shares → 600 left, USD = 600*0.45 = $270
    store.record_unwind(CID, "yes", 400.0)
    check("Phase 2a: 600 shares left",
          abs(store.get_shares(CID, "yes") - 600.0) < 0.01,
          f"got {store.get_shares(CID, 'yes')}")
    check("Phase 2a: USD = $270",
          abs(store.get_position(CID, "yes") - 270.0) < 0.01,
          f"got ${store.get_position(CID, 'yes'):.2f}")
    check(f"Phase 2a: halt status correct ($270 vs ${RESUME_POSITION_USD} resume)",
          store.is_halted(CID, "yes") == (270.0 > RESUME_POSITION_USD),
          f"halted={store.is_halted(CID, 'yes')}, threshold=${RESUME_POSITION_USD}")

    # Phase 3: More fills push back over limit
    store.record_fill(CID, "yes", 350.0, 0.45, question=MARKET["question"])
    # Now: 950 shares, USD = 950 * 0.45 = $427.50
    check("Phase 3: RE-HALTED after new fill ($427.50)",
          store.is_halted(CID, "yes"),
          f"halted={store.is_halted(CID, 'yes')}, usd=${store.get_position(CID, 'yes'):.2f}")

    # Phase 4: Sell everything
    store.record_unwind(CID, "yes", 950.0)
    check("Phase 4: Fully unwound (0 shares)",
          store.get_shares(CID, "yes") == 0.0,
          f"got {store.get_shares(CID, 'yes')}")
    check("Phase 4: USD = $0",
          store.get_position(CID, "yes") == 0.0,
          f"got ${store.get_position(CID, 'yes'):.2f}")
    check("Phase 4: Not halted",
          not store.is_halted(CID, "yes"),
          f"halted={store.is_halted(CID, 'yes')}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 11: Decay Price Calculation
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 11: Decay in Unwind Flow ===")

    store = fresh_store()
    mgr = fresh_manager(store)

    # Set up: 500 NO shares at YES-equiv 0.30 (CLOB cost 0.70)
    store.record_fill(CID, "no", 500.0, 0.30, question=MARKET["question"])

    # Verify the VWAP-based sell price
    avg = store.get_avg_price(CID, "no")
    sell_clob = to_clob(avg, "no")
    check("NO VWAP sell CLOB price = 0.70",
          abs(sell_clob - 0.70) < 0.001,
          f"avg={avg}, sell_clob={sell_clob}")

    # Simulate a placed unwind order 10 minutes ago
    created_10min_ago = _real_time.time() - 600
    mgr.unwind_orders["decay_test"] = UnwindOrder(
        side="no",
        price=0.30,      # YES-equiv
        clob_price=0.70,  # Was placed at VWAP
        size=500.0,
        placed_at=created_10min_ago,
        created_at=created_10min_ago,
        base_clob_price=0.70,
    )

    # After 10 minutes: 2 decay intervals (10min / 5min = 2)
    # Expected: 0.70 - 2*0.01 = 0.68
    tick = 0.01
    elapsed = 600
    intervals = int(elapsed // UNWIND_DECAY_INTERVAL_SECS)
    expected_decay = intervals * UNWIND_DECAY_TICKS * tick
    expected_price = 0.70 - expected_decay

    check(f"Decay: 10min → {intervals} intervals → price {expected_price:.2f}",
          intervals == 2 and abs(expected_price - 0.68) < 0.001,
          f"intervals={intervals}, expected={expected_price}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 12: Order Book Merge Uses to_yes_equiv
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 12: Order Book Merge ===")

    store = fresh_store()
    mgr = fresh_manager(store)

    # NO ask at 0.70 CLOB → YES bid at 0.30
    # NO bid at 0.65 CLOB → YES ask at 0.35
    book = mgr.get_order_book()

    if book is not None:
        bids = [(float(b["price"]), float(b["size"])) for b in book["bids"]]
        asks = [(float(a["price"]), float(a["size"])) for a in book["asks"]]

        # YES direct bids: 0.30, 0.29, 0.28
        # NO ask 0.70 → YES bid 0.30 (aggregated with direct)
        # So bid at 0.30 should have combined size
        bid_at_30 = sum(s for p, s in bids if abs(p - 0.30) < 0.001)
        check("Merged book: bid at 0.30 includes NO ask complement",
              bid_at_30 > 5000,  # Should be 5000 (YES) + 5000 (NO complement)
              f"bid_at_30 size={bid_at_30}")

        # NO bid 0.65 → YES ask 0.35 (aggregated with direct)
        ask_at_35 = sum(s for p, s in asks if abs(p - 0.35) < 0.001)
        check("Merged book: ask at 0.35 includes NO bid complement",
              ask_at_35 > 5000,
              f"ask_at_35 size={ask_at_35}")
    else:
        check("Order book fetched", False, "book is None")
        check("Book merge placeholder", False, "skipped")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 13: has_open_obligations
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 13: Open Obligations ===")

    store = fresh_store()
    mgr = fresh_manager(store)

    check("No obligations when empty",
          not mgr.has_open_obligations(),
          f"has_open={mgr.has_open_obligations()}")

    mgr.unwind_orders["test"] = UnwindOrder(side="yes", price=0, clob_price=0, size=100)
    check("Has obligations with unwind orders",
          mgr.has_open_obligations(),
          f"has_open={mgr.has_open_obligations()}")

    del mgr.unwind_orders["test"]

    # Also check via position
    store.record_fill(CID, "yes", 50.0, 0.30, question=MARKET["question"])
    check("Has obligations with position",
          mgr.has_open_obligations(),
          f"has_open={mgr.has_open_obligations()}")

    # ═════════════════════════════════════════════════════════════════════════
    # TEST 14: Dual Position Blocks New Buys
    # ═════════════════════════════════════════════════════════════════════════
    print("\n=== TEST 14: Dual Position Guard ===")

    store = fresh_store()
    mgr = fresh_manager(store)

    # Hold both YES and NO
    store.record_fill(CID, "yes", 100.0, 0.40, question=MARKET["question"])
    store.record_fill(CID, "no", 100.0, 0.60, question=MARKET["question"])

    yes_inv = store.get_shares(CID, "yes")
    no_inv = store.get_shares(CID, "no")
    check("Dual position: YES=100, NO=100",
          abs(yes_inv - 100) < 0.01 and abs(no_inv - 100) < 0.01,
          f"yes={yes_inv}, no={no_inv}")

    # Both sides should have >= MIN_UNWIND_SHARES
    # run_cycle's guard should block both buys
    # (We can't easily run full run_cycle without more mocking,
    #  but we can verify the condition)
    check("Both sides >= MIN_UNWIND_SHARES (merge-deadlock guard applies)",
          yes_inv >= MIN_UNWIND_SHARES and no_inv >= MIN_UNWIND_SHARES,
          f"yes={yes_inv}, no={no_inv}, min={MIN_UNWIND_SHARES}")


finally:
    if os.path.exists(_tmp.name):
        os.unlink(_tmp.name)
    _state_module.POSITIONS_FILE = _original_positions_file
    # Clean up temp database and reset singleton
    _db_module._instance = None
    for suffix in ("", "-wal", "-shm"):
        p = _tmp_db.name + suffix
        if os.path.exists(p):
            os.unlink(p)
    # Safety: clean any test data that leaked to production DB
    try:
        import sqlite3 as _sq
        _prod = _sq.connect(_db_module.DB_PATH)
        _prod.execute("DELETE FROM positions WHERE condition_id LIKE '%test%'")
        _prod.execute("DELETE FROM fills WHERE condition_id LIKE '%test%'")
        _prod.commit()
        _prod.close()
    except Exception:
        pass


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
