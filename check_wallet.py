"""
Diagnostic script to verify wallet setup before trading.

Checks FUNDER contract type, pUSD (V2 collateral) and legacy USDC.e
balances, on-chain allowances against the V2 Exchange / V2 NegRisk
Exchange / V2 NegRisk Adapter, CTF approvals, and CLOB API-side
balance/allowance status.  Run this before starting the bot to catch
misconfigurations early.

Usage:
    python check_wallet.py
"""

from web3 import Web3
from dotenv import load_dotenv
import os

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, BalanceAllowanceParams, AssetType, BuilderConfig

from config import (
    HOST, CHAIN_ID, PRIVATE_KEY,
    CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE,
    FUNDER, SIGNATURE_TYPE, BUILDER_CODE, WALLET_ADDRESS,
)

load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────
RPC_URL = "https://polygon-mainnet.g.alchemy.com/v2/" + os.getenv(
    "ALCHEMY_KEY", "alfo528x9kBrHK0G5JSfF"
)

# Polymarket V2 contract addresses on Polygon (post-2026-04-28 cutover).
# pUSD replaces USDC.e as the collateral token; CTF address is unchanged.
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"   # V2 collateral
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # legacy (pre-V2)
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE_ADDRESS = "0xE111180000d2663C0091e4f400237545B87B996B"     # V2 Exchange
NEG_RISK_ADDRESS = "0xe2222d279d744050d28e00520010520000310F59"     # V2 NegRisk Exchange
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"  # V2 NegRisk Adapter

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
        Formatted string like "UNLIMITED (MAX_INT)" or "123.45 pUSD".
    """
    if value >= MAX_INT:
        return "UNLIMITED (MAX_INT)"
    elif value == 0:
        return "0 (NOT SET)"
    else:
        return f"{value / 1e6:.2f} pUSD"


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

    # ── 2. Check pUSD balances (V2) + legacy USDC.e balances ─────────────────
    pusd = w3.eth.contract(
        address=Web3.to_checksum_address(PUSD_ADDRESS), abi=ERC20_ABI
    )
    usdc_e = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_E_ADDRESS), abi=ERC20_ABI
    )

    funder_pusd = pusd.functions.balanceOf(funder_addr).call()
    eoa_pusd = pusd.functions.balanceOf(eoa_addr).call()
    funder_usdc_e = usdc_e.functions.balanceOf(funder_addr).call()
    eoa_usdc_e = usdc_e.functions.balanceOf(eoa_addr).call()

    funder_balance = funder_pusd  # used in summary block below
    print(f"{'=' * 60}")
    print(f"2. COLLATERAL BALANCES")
    print(f"   pUSD (V2,  {PUSD_ADDRESS}):")
    print(f"     FUNDER:  ${funder_pusd / 1e6:.2f}")
    print(f"     EOA:     ${eoa_pusd / 1e6:.2f}")
    print(f"   USDC.e (legacy, {USDC_E_ADDRESS}):")
    print(f"     FUNDER:  ${funder_usdc_e / 1e6:.2f}")
    print(f"     EOA:     ${eoa_usdc_e / 1e6:.2f}")
    if funder_pusd == 0 and funder_usdc_e == 0:
        print("   NOTE: FUNDER has no pUSD and no USDC.e. Either the wallet is unfunded "
              "or funds are elsewhere.")
    print()

    # ── 3. pUSD allowances FROM FUNDER on V2 contracts ───────────────────────
    funder_to_exchange = pusd.functions.allowance(
        funder_addr, Web3.to_checksum_address(EXCHANGE_ADDRESS)
    ).call()
    funder_to_negrisk = pusd.functions.allowance(
        funder_addr, Web3.to_checksum_address(NEG_RISK_ADDRESS)
    ).call()
    funder_to_negrisk_adapter = pusd.functions.allowance(
        funder_addr, Web3.to_checksum_address(NEG_RISK_ADAPTER_ADDRESS)
    ).call()

    print(f"{'=' * 60}")
    print("3. pUSD ALLOWANCES FROM FUNDER (proxy wallet) — V2 contracts")
    print(f"   FUNDER -> V2 Exchange:           {fmt_allowance(funder_to_exchange)}")
    print(f"   FUNDER -> V2 Neg Risk Exchange:  {fmt_allowance(funder_to_negrisk)}")
    print(f"   FUNDER -> V2 Neg Risk Adapter:   {fmt_allowance(funder_to_negrisk_adapter)}")
    if funder_to_exchange == 0 or funder_to_negrisk == 0 or funder_to_negrisk_adapter == 0:
        print("   PROBLEM: Allowances from FUNDER are not set!")
        print("   -> Run set_allowances.py.")
    print()

    # ── 4. pUSD allowances FROM EOA on V2 contracts ──────────────────────────
    eoa_to_exchange = pusd.functions.allowance(
        eoa_addr, Web3.to_checksum_address(EXCHANGE_ADDRESS)
    ).call()
    eoa_to_negrisk = pusd.functions.allowance(
        eoa_addr, Web3.to_checksum_address(NEG_RISK_ADDRESS)
    ).call()

    print(f"{'=' * 60}")
    print("4. pUSD ALLOWANCES FROM EOA — V2 contracts")
    print(f"   EOA -> V2 Exchange:           {fmt_allowance(eoa_to_exchange)}")
    print(f"   EOA -> V2 Neg Risk Exchange:  {fmt_allowance(eoa_to_negrisk)}")
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
    funder_ctf_negrisk_adapter = ctf.functions.isApprovedForAll(
        funder_addr, Web3.to_checksum_address(NEG_RISK_ADAPTER_ADDRESS)
    ).call()
    eoa_ctf_exchange = ctf.functions.isApprovedForAll(
        eoa_addr, Web3.to_checksum_address(EXCHANGE_ADDRESS)
    ).call()
    eoa_ctf_negrisk = ctf.functions.isApprovedForAll(
        eoa_addr, Web3.to_checksum_address(NEG_RISK_ADDRESS)
    ).call()

    print(f"{'=' * 60}")
    print("5. CTF (ERC1155) APPROVALS — V2 contracts")
    print(f"   FUNDER -> V2 Exchange:           {funder_ctf_exchange}")
    print(f"   FUNDER -> V2 Neg Risk Exchange:  {funder_ctf_negrisk}")
    print(f"   FUNDER -> V2 Neg Risk Adapter:   {funder_ctf_negrisk_adapter}")
    print(f"   EOA -> V2 Exchange:              {eoa_ctf_exchange}")
    print(f"   EOA -> V2 Neg Risk Exchange:     {eoa_ctf_negrisk}")
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
            builder_config=BuilderConfig(builder_code=BUILDER_CODE) if BUILDER_CODE else None,
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
        issues.append("FUNDER has 0 pUSD — wallet may be unfunded post-V2-migration")
    if funder_to_exchange == 0:
        issues.append("FUNDER has no pUSD allowance for V2 Exchange — run set_allowances.py")
    if funder_to_negrisk == 0:
        issues.append("FUNDER has no pUSD allowance for V2 Neg Risk Exchange — run set_allowances.py")
    if funder_to_negrisk_adapter == 0:
        issues.append("FUNDER has no pUSD allowance for V2 Neg Risk Adapter — run set_allowances.py")
    if not funder_ctf_exchange:
        issues.append("FUNDER has no CTF approval for V2 Exchange — run set_allowances.py")
    if not funder_ctf_negrisk:
        issues.append("FUNDER has no CTF approval for V2 Neg Risk Exchange — run set_allowances.py")
    if not funder_ctf_negrisk_adapter:
        issues.append("FUNDER has no CTF approval for V2 Neg Risk Adapter — run set_allowances.py")
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
