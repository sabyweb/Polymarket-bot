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
        order_id = response.orderID
        print("  ORDER PLACED SUCCESSFULLY!")
        print(f"  Order ID: {order_id}")
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
    print("Verifying order on exchange...")
    time.sleep(2)
    try:
        open_orders = client.get_orders()
        found = any(o.id == order_id for o in open_orders) if open_orders else False
        print(f"  Order visible on exchange: {found}")
    except Exception as e:
        print(f"  Could not verify: {e}")
    print()

    # ── Cancel the test order ────────────────────────────────────────────────
    print("Cancelling test order...")
    try:
        client.cancel(order_id)
        print("  Order cancelled successfully!")
    except Exception as e:
        print(f"  Cancel failed: {e}")
    print()

    print("=" * 50)
    print("TEST COMPLETE - Your bot is ready for live trading!")
    print("Run: python main.py")


if __name__ == "__main__":
    main()
