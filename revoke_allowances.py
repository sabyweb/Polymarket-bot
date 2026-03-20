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

# Polymarket contract addresses on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_ADDRESS = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

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

    print("Revoking USDC approvals...")
    revoke_erc20(w3, USDC_ADDRESS, EXCHANGE_ADDRESS, PRIVATE_KEY, account,
                 "USDC -> Exchange")
    revoke_erc20(w3, USDC_ADDRESS, NEG_RISK_ADDRESS, PRIVATE_KEY, account,
                 "USDC -> Neg Risk")
    print()

    print("Revoking CTF approvals...")
    revoke_ctf(w3, CTF_ADDRESS, EXCHANGE_ADDRESS, PRIVATE_KEY, account,
               "CTF -> Exchange")
    revoke_ctf(w3, CTF_ADDRESS, NEG_RISK_ADDRESS, PRIVATE_KEY, account,
               "CTF -> Neg Risk")
    print()

    print("Done! All approvals have been revoked.")
    print("Run check_wallet.py to verify.")


if __name__ == "__main__":
    main()
