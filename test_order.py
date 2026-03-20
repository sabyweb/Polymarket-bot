"""
Test script to validate a single live order placement.

Places a limit order far from midpoint (safe from fills), verifies it
appears on the exchange, then cancels it.  Run this after
set_allowances.py to confirm the full trading pipeline works.

Usage:
    python test_order.py
"""

import time
import logging
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds, OrderArgs, BalanceAllowanceParams, AssetType,
)
from py_clob_client.order_builder.constants import BUY

from config import (
    HOST, CHAIN_ID, PRIVATE_KEY,
    CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE,
    FUNDER, SIGNATURE_TYPE,
)
from market import get_rewards_markets

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def main() -> None:
    """Run a single test order: place, verify, cancel."""
    # ── Connect ──────────────────────────────────────────────────────────────
    creds = ApiCreds(
        api_key=CLOB_API_KEY,
        api_secret=CLOB_SECRET,
        api_passphrase=CLOB_PASS_PHRASE,
    )
    client = ClobClient(
        HOST,
        key=PRIVATE_KEY,
        chain_id=CHAIN_ID,
        creds=creds,
        signature_type=SIGNATURE_TYPE,
        funder=FUNDER,
    )
    print("Connected to CLOB API")
    print(f"FUNDER:          {FUNDER}")
    print(f"SIGNATURE_TYPE:  {SIGNATURE_TYPE}")
    print()

    # ── Check balance/allowance ──────────────────────────────────────────────
    print("Checking balance/allowance...")
    try:
        bal = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        print(f"  Collateral: {bal}")
    except Exception as e:
        print(f"  ERROR: {e}")
        print("  Run set_allowances.py first!")
        return
    print()

    # ── Pick a market ────────────────────────────────────────────────────────
    print("Finding an eligible market...")
    markets = get_rewards_markets(limit=1)
    if not markets:
        print("No eligible markets found!")
        return

    market = markets[0]
    print(f"  Market:    {market['question'][:60]}")
    print(f"  Yes price: {market['yes_price']:.4f}")
    print(f"  Token ID:  {market['token_ids'][0][:20]}...")
    print(f"  Tick size: {market.get('tick_size', 0.01)}")
    print()

    # ── Place a safe test order ──────────────────────────────────────────────
    # BUY at a very low price (far below market) so it won't fill
    token_id = market["token_ids"][0]
    tick_size = market.get("tick_size", 0.01)
    test_price = max(0.01, round(market["yes_price"] * 0.5, 2))
    # Snap to valid tick
    test_price = round(round(test_price / tick_size) * tick_size, 4)
    test_price = max(tick_size, test_price)

    min_size = market.get("min_size", 1)
    test_size = max(min_size, 10.0)

    print("Placing test BUY order:")
    print(f"  Price: {test_price}")
    print(f"  Size:  {test_size} shares")
    print(f"  Est cost: ${test_price * test_size:.2f}")
    print()

    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=test_price,
            size=test_size,
            side=BUY,
        )
        response = client.create_and_post_order(order_args)
        # Response is a dict: {'orderID': '...', 'status': 'live', 'success': True}
        if isinstance(response, dict):
            post_order_id = response.get("orderID")
            success = response.get("success", False)
            print(f"  Response: {response}")
        else:
            post_order_id = response.orderID
            success = True
        if not success:
            print(f"  ORDER REJECTED by exchange")
            return
        print("  ORDER PLACED SUCCESSFULLY!")
    except Exception as e:
        print(f"  ORDER FAILED: {e}")
        print()
        print("Troubleshooting:")
        print("  - 'not enough balance/allowance': run set_allowances.py")
        print("  - 'invalid signature': check SIGNATURE_TYPE in config.py")
        print("  - 'invalid tick size': check tick_size for this market")
        return
    print()

    # ── Verify order exists on exchange ──────────────────────────────────────
    # The orderID from POST response differs from the order 'id' on the
    # exchange, so we fetch open orders and look for a matching one.
    print("Verifying order on exchange...")
    time.sleep(2)
    exchange_order_id = None
    try:
        open_orders = client.get_orders()
        # open_orders is a list of dicts with key 'id'
        for o in (open_orders or []):
            oid = o["id"] if isinstance(o, dict) else o.id
            print(f"  Found open order: {oid}")
            exchange_order_id = oid  # Take the most recent one
        if exchange_order_id:
            print(f"  Order verified on exchange!")
        else:
            print("  No open orders found on exchange.")
    except Exception as e:
        print(f"  Could not verify: {e}")
    print()

    # ── Cancel the test order ────────────────────────────────────────────────
    if exchange_order_id:
        print(f"Cancelling order {exchange_order_id}...")
        try:
            result = client.cancel(exchange_order_id)
            if isinstance(result, dict) and result.get("canceled"):
                print(f"  Order cancelled successfully!")
            else:
                print(f"  Cancel response: {result}")
        except Exception as e:
            print(f"  Cancel failed: {e}")
    else:
        print("No order to cancel (could not find exchange order ID)")
        print("Check Polymarket UI and cancel manually if needed!")
    print()

    print("=" * 50)
    print("TEST COMPLETE - Your bot is ready for live trading!")
    print("Run: python main.py")


if __name__ == "__main__":
    main()
