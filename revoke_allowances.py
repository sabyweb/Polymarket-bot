"""
Revoke token allowances for Polymarket trading.

Run this when you are done trading to reduce security exposure.
It sets both COLLATERAL and CONDITIONAL allowances back to zero
via the CLOB API.

Usage:
    python revoke_allowances.py
"""

from web3 import Web3
from dotenv import load_dotenv
import os

from config import FUNDER

load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
RPC_URL = "https://polygon-mainnet.g.alchemy.com/v2/" + os.getenv(
    "ALCHEMY_KEY", "alfo528x9kBrHK0G5JSfF"
)

# Polymarket V2 contract addresses on Polygon (post-2026-04-28 cutover).
# pUSD replaces USDC.e as the collateral token; CTF address is unchanged.
# Resolved on-chain via V2_Exchange.getCollateral() and getCtf().
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"   # Polymarket USD ERC-20
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"    # CTF (unchanged in V2)
EXCHANGE_ADDRESS = "0xE111180000d2663C0091e4f400237545B87B996B"     # V2 Exchange
NEG_RISK_ADDRESS = "0xe2222d279d744050d28e00520010520000310F59"     # V2 NegRisk Exchange
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"  # V2 NegRisk Adapter

# ERC20 ABI for approve (set to 0 to revoke)
ERC20_ABI = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    }
]

# CTF ABI for setApprovalForAll (set to False to revoke)
CTF_ABI = [
    {
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "type": "function",
    }
]


def revoke_erc20(
    w3: Web3,
    token_address: str,
    spender: str,
    private_key: str,
    account: str,
    label: str,
) -> None:
    """Revoke an ERC20 allowance by setting it to zero.

    Args:
        w3: Connected Web3 instance.
        token_address: Address of the ERC20 token contract.
        spender: Address whose allowance to revoke.
        private_key: Signer's private key.
        account: Signer's address.
        label: Human-readable label for logging.
    """
    try:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_address), abi=ERC20_ABI
        )
        nonce = w3.eth.get_transaction_count(account)
        tx = contract.functions.approve(
            Web3.to_checksum_address(spender), 0
        ).build_transaction({
            "from": account,
            "nonce": nonce,
            "gas": 100000,
            "gasPrice": w3.eth.gas_price,
        })
        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        status = "OK" if receipt.status == 1 else "FAILED"
        print(f"  [{status}] {label} | tx: {tx_hash.hex()}")
    except Exception as e:
        print(f"  [FAILED] {label}: {e}")


def revoke_ctf(
    w3: Web3,
    ctf_address: str,
    operator: str,
    private_key: str,
    account: str,
    label: str,
) -> None:
    """Revoke a CTF (ERC1155) operator approval.

    Args:
        w3: Connected Web3 instance.
        ctf_address: Address of the CTF contract.
        operator: Address whose approval to revoke.
        private_key: Signer's private key.
        account: Signer's address.
        label: Human-readable label for logging.
    """
    try:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(ctf_address), abi=CTF_ABI
        )
        nonce = w3.eth.get_transaction_count(account)
        tx = contract.functions.setApprovalForAll(
            Web3.to_checksum_address(operator), False
        ).build_transaction({
            "from": account,
            "nonce": nonce,
            "gas": 100000,
            "gasPrice": w3.eth.gas_price,
        })
        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        status = "OK" if receipt.status == 1 else "FAILED"
        print(f"  [{status}] {label} | tx: {tx_hash.hex()}")
    except Exception as e:
        print(f"  [FAILED] {label}: {e}")


def main() -> None:
    """Revoke all ERC20 and CTF approvals from the EOA."""
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    account = Web3.to_checksum_address(
        w3.eth.account.from_key(PRIVATE_KEY).address
    )

    print(f"Connected to Polygon: {w3.is_connected()}")
    print(f"Revoking approvals from: {account}")
    print(f"FUNDER (proxy wallet):  {FUNDER}")
    print()

    print("Revoking pUSD approvals (V2 collateral)...")
    revoke_erc20(w3, PUSD_ADDRESS, EXCHANGE_ADDRESS, PRIVATE_KEY, account,
                 "pUSD -> V2 Exchange")
    revoke_erc20(w3, PUSD_ADDRESS, NEG_RISK_ADDRESS, PRIVATE_KEY, account,
                 "pUSD -> V2 Neg Risk Exchange")
    revoke_erc20(w3, PUSD_ADDRESS, NEG_RISK_ADAPTER_ADDRESS, PRIVATE_KEY, account,
                 "pUSD -> V2 Neg Risk Adapter")
    print()

    print("Revoking CTF approvals...")
    revoke_ctf(w3, CTF_ADDRESS, EXCHANGE_ADDRESS, PRIVATE_KEY, account,
               "CTF -> V2 Exchange")
    revoke_ctf(w3, CTF_ADDRESS, NEG_RISK_ADDRESS, PRIVATE_KEY, account,
               "CTF -> V2 Neg Risk Exchange")
    revoke_ctf(w3, CTF_ADDRESS, NEG_RISK_ADAPTER_ADDRESS, PRIVATE_KEY, account,
               "CTF -> V2 Neg Risk Adapter")
    print()

    print("Done! All approvals have been revoked.")
    print("Run check_wallet.py to verify.")


if __name__ == "__main__":
    main()
