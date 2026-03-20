"""
Diagnostic script to verify wallet setup before trading.

Checks FUNDER contract type, USDC.e balances, on-chain allowances,
CTF approvals, and CLOB API-side balance/allowance status.  Run this
before starting the bot to catch misconfigurations early.

Usage:
    python check_wallet.py
"""

from web3 import Web3
from dotenv import load_dotenv
import os

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

from config import (
    HOST, CHAIN_ID, PRIVATE_KEY,
    CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE,
    FUNDER, SIGNATURE_TYPE, WALLET_ADDRESS,
)

load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────
RPC_URL = "https://polygon-mainnet.g.alchemy.com/v2/" + os.getenv(
    "ALCHEMY_KEY", "alfo528x9kBrHK0G5JSfF"
)

# Polymarket contract addresses on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_ADDRESS = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

# Minimal ABIs for read-only calls
ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

CTF_ABI = [
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]

MAX_INT = 2**256 - 1


def fmt_allowance(value: int) -> str:
    """Format an on-chain allowance value for human-readable display.

    Args:
        value: Raw uint256 allowance from the contract.

    Returns:
        Formatted string like "UNLIMITED (MAX_INT)" or "123.45 USDC".
    """
    if value >= MAX_INT:
        return "UNLIMITED (MAX_INT)"
    elif value == 0:
        return "0 (NOT SET)"
    else:
        return f"{value / 1e6:.2f} USDC"


def main() -> None:
    """Run all diagnostic checks and print a summary."""
    # ── Connect to Polygon ───────────────────────────────────────────────────
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    print(f"Connected to Polygon: {w3.is_connected()}")
    print()

    eoa_addr = Web3.to_checksum_address(WALLET_ADDRESS)
    funder_addr = Web3.to_checksum_address(FUNDER)

    print(f"EOA (derived from key):  {eoa_addr}")
    print(f"FUNDER (proxy wallet):   {funder_addr}")
    print(f"SIGNATURE_TYPE:          {SIGNATURE_TYPE}")
    print(f"  (0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE)")
    print()

    # ── 1. Check if FUNDER is a smart contract ───────────────────────────────
    code = w3.eth.get_code(funder_addr)
    is_contract = len(code) > 0
    print(f"{'=' * 60}")
    print("1. FUNDER CONTRACT CHECK")
    print(f"   Is smart contract: {is_contract}")
    if is_contract:
        print(f"   Bytecode length:   {len(code)} bytes")
        print("   -> This confirms FUNDER is a proxy wallet (not an EOA)")
        print("   -> SIGNATURE_TYPE should be 1 (POLY_PROXY) or 2 (POLY_GNOSIS_SAFE)")
    else:
        print("   -> FUNDER is an EOA, SIGNATURE_TYPE should be 0")
    print()

    # ── 2. Check USDC.e balances ─────────────────────────────────────────────
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI
    )

    funder_balance = usdc.functions.balanceOf(funder_addr).call()
    eoa_balance = usdc.functions.balanceOf(eoa_addr).call()

    print(f"{'=' * 60}")
    print(f"2. USDC.e BALANCES (token: {USDC_ADDRESS})")
    print(f"   FUNDER balance:  ${funder_balance / 1e6:.2f}")
    print(f"   EOA balance:     ${eoa_balance / 1e6:.2f}")
    if funder_balance == 0:
        print("   WARNING: FUNDER has no USDC.e! Check if funds are in native USDC.")
    print()

    # ── 3. Check USDC.e allowances FROM FUNDER ───────────────────────────────
    funder_to_exchange = usdc.functions.allowance(
        funder_addr, Web3.to_checksum_address(EXCHANGE_ADDRESS)
    ).call()
    funder_to_negrisk = usdc.functions.allowance(
        funder_addr, Web3.to_checksum_address(NEG_RISK_ADDRESS)
    ).call()

    print(f"{'=' * 60}")
    print("3. USDC.e ALLOWANCES FROM FUNDER (proxy wallet)")
    print(f"   FUNDER -> Exchange:  {fmt_allowance(funder_to_exchange)}")
    print(f"   FUNDER -> Neg Risk:  {fmt_allowance(funder_to_negrisk)}")
    if funder_to_exchange == 0 or funder_to_negrisk == 0:
        print("   PROBLEM: Allowances from FUNDER are not set!")
        print("   -> Run the updated set_allowances.py to fix this.")
    print()

    # ── 4. Check USDC.e allowances FROM EOA ──────────────────────────────────
    eoa_to_exchange = usdc.functions.allowance(
        eoa_addr, Web3.to_checksum_address(EXCHANGE_ADDRESS)
    ).call()
    eoa_to_negrisk = usdc.functions.allowance(
        eoa_addr, Web3.to_checksum_address(NEG_RISK_ADDRESS)
    ).call()

    print(f"{'=' * 60}")
    print("4. USDC.e ALLOWANCES FROM EOA")
    print(f"   EOA -> Exchange:  {fmt_allowance(eoa_to_exchange)}")
    print(f"   EOA -> Neg Risk:  {fmt_allowance(eoa_to_negrisk)}")
    print()

    # ── 5. Check CTF approvals ───────────────────────────────────────────────
    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI
    )

    funder_ctf_exchange = ctf.functions.isApprovedForAll(
        funder_addr, Web3.to_checksum_address(EXCHANGE_ADDRESS)
    ).call()
    funder_ctf_negrisk = ctf.functions.isApprovedForAll(
        funder_addr, Web3.to_checksum_address(NEG_RISK_ADDRESS)
    ).call()
    eoa_ctf_exchange = ctf.functions.isApprovedForAll(
        eoa_addr, Web3.to_checksum_address(EXCHANGE_ADDRESS)
    ).call()
    eoa_ctf_negrisk = ctf.functions.isApprovedForAll(
        eoa_addr, Web3.to_checksum_address(NEG_RISK_ADDRESS)
    ).call()

    print(f"{'=' * 60}")
    print("5. CTF (ERC1155) APPROVALS")
    print(f"   FUNDER -> Exchange:  {funder_ctf_exchange}")
    print(f"   FUNDER -> Neg Risk:  {funder_ctf_negrisk}")
    print(f"   EOA -> Exchange:     {eoa_ctf_exchange}")
    print(f"   EOA -> Neg Risk:     {eoa_ctf_negrisk}")
    print()

    # ── 6. Check CLOB-side balance/allowance ─────────────────────────────────
    print(f"{'=' * 60}")
    print("6. CLOB API BALANCE/ALLOWANCE CHECK")
    try:
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

        collateral = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        print(f"   Collateral: {collateral}")

        conditional = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL)
        )
        print(f"   Conditional: {conditional}")
    except Exception as e:
        print(f"   ERROR: {e}")
    print()

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    issues: list[str] = []
    if not is_contract:
        issues.append("FUNDER is not a contract — check wallet setup")
    if funder_balance == 0:
        issues.append("FUNDER has 0 USDC.e — funds may be in wrong token")
    if funder_to_exchange == 0:
        issues.append("FUNDER has no USDC allowance for Exchange — run set_allowances.py")
    if funder_to_negrisk == 0:
        issues.append("FUNDER has no USDC allowance for Neg Risk — run set_allowances.py")
    if not funder_ctf_exchange:
        issues.append("FUNDER has no CTF approval for Exchange — run set_allowances.py")
    if not funder_ctf_negrisk:
        issues.append("FUNDER has no CTF approval for Neg Risk — run set_allowances.py")
    if SIGNATURE_TYPE == 2 and is_contract:
        issues.append(
            "SIGNATURE_TYPE=2 (Gnosis Safe) — verify this is correct, "
            "may need 1 (POLY_PROXY)"
        )

    if issues:
        for i, issue in enumerate(issues, 1):
            print(f"   {i}. {issue}")
    else:
        print("   All checks passed! Wallet setup looks correct.")


if __name__ == "__main__":
    main()
