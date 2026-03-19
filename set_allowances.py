from web3 import Web3
from dotenv import load_dotenv
import os

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
FUNDER      = os.getenv("FUNDER")
RPC_URL     = "https://polygon-mainnet.g.alchemy.com/v2/alfo528x9kBrHK0G5JSfF"

# Polymarket contract addresses on Polygon
USDC_ADDRESS     = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS      = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_ADDRESS = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

# ERC20 ABI for approve
ERC20_ABI = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    }
]

# CTF ABI for setApprovalForAll
CTF_ABI = [
    {
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"}
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "type": "function"
    }
]

MAX_INT = 2**256 - 1


def send_approval(w3, contract_address, spender, abi, private_key, account, label):
    try:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(contract_address),
            abi=abi
        )
        nonce = w3.eth.get_transaction_count(account)

        is_ctf = "setApprovalForAll" in [f["name"] for f in abi]

        if is_ctf:
            tx = contract.functions.setApprovalForAll(
                Web3.to_checksum_address(spender), True
            ).build_transaction({
                "from":     account,
                "nonce":    nonce,
                "gas":      100000,
                "gasPrice": w3.eth.gas_price
            })
        else:
            tx = contract.functions.approve(
                Web3.to_checksum_address(spender), MAX_INT
            ).build_transaction({
                "from":     account,
                "nonce":    nonce,
                "gas":      100000,
                "gasPrice": w3.eth.gas_price
            })

        signed  = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        status  = "✅" if receipt.status == 1 else "❌"
        print(f"{status}  {label} | tx: {tx_hash.hex()} | status: {receipt.status}")

    except Exception as e:
        print(f"❌  {label} failed: {e}")


if __name__ == "__main__":
    w3      = Web3(Web3.HTTPProvider(RPC_URL))
    account = Web3.to_checksum_address(
        w3.eth.account.from_key(PRIVATE_KEY).address
    )

    print(f"Connected to Polygon: {w3.is_connected()}")
    print(f"Derived address:      {account}")
    print(f"FUNDER in .env:       {FUNDER}")
    print()

    # 1. Approve USDC for Exchange
    send_approval(w3, USDC_ADDRESS, EXCHANGE_ADDRESS, ERC20_ABI,
                  PRIVATE_KEY, account, "USDC → Exchange")

    # 2. Approve USDC for Neg Risk
    send_approval(w3, USDC_ADDRESS, NEG_RISK_ADDRESS, ERC20_ABI,
                  PRIVATE_KEY, account, "USDC → Neg Risk")

    # 3. Approve CTF for Exchange
    send_approval(w3, CTF_ADDRESS, EXCHANGE_ADDRESS, CTF_ABI,
                  PRIVATE_KEY, account, "CTF → Exchange")

    # 4. Approve CTF for Neg Risk
    send_approval(w3, CTF_ADDRESS, NEG_RISK_ADDRESS, CTF_ABI,
                  PRIVATE_KEY, account, "CTF → Neg Risk")

    print()
    print("Done! You only need to run this once per wallet.")