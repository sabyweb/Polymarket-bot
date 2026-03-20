"""
Set token allowances for Polymarket trading via the CLOB API.

Uses the CLOB server's update_balance_allowance endpoint, which properly
handles proxy wallets by routing approval calls through the proxy
contract.  This replaces the old approach of sending raw ERC20 approve()
transactions from the EOA (which set allowances on the wrong address
for proxy wallet setups).

Run this once before starting the bot:
    python set_allowances.py
"""

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

from config import (
    HOST, CHAIN_ID, PRIVATE_KEY,
    CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE,
    FUNDER, SIGNATURE_TYPE,
)

import logging

log = logging.getLogger(__name__)


def main() -> None:
    """Connect to the CLOB API and set both COLLATERAL and CONDITIONAL allowances."""
    # ── Connect to CLOB API ──────────────────────────────────────────────────
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

    print(f"Connected to Polymarket CLOB API")
    print(f"FUNDER:          {FUNDER}")
    print(f"SIGNATURE_TYPE:  {SIGNATURE_TYPE}")
    print()

    # ── Update COLLATERAL (USDC.e) allowance ─────────────────────────────────
    print("Setting COLLATERAL (USDC.e) allowance...")
    try:
        result = client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        print(f"  Result: {result}")
    except Exception as e:
        print(f"  ERROR: {e}")
    print()

    # ── Update CONDITIONAL (CTF token) allowance ─────────────────────────────
    print("Setting CONDITIONAL (CTF token) allowance...")
    try:
        result = client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL)
        )
        print(f"  Result: {result}")
    except Exception as e:
        print(f"  ERROR: {e}")
    print()

    # ── Verify allowances ────────────────────────────────────────────────────
    print("Verifying allowances...")
    try:
        collateral = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        print(f"  Collateral: {collateral}")

        conditional = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL)
        )
        print(f"  Conditional: {conditional}")
    except Exception as e:
        print(f"  ERROR verifying: {e}")
    print()

    print("Done! Run check_wallet.py for full on-chain verification.")


if __name__ == "__main__":
    main()
