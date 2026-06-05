# Merge Ground Truth — FX-094 (verified 2026-06-05)

## Incident (Becerra, 2026-06-03 ~03:27 UTC)

- Market `0xa5d79e71…` filled both sides (100 NO + 100 YES, ~$106).
- [`dump_manager.try_merge`](../dump_manager.py) called `ClobClient.merge_positions`.
- **Failure:** `'ClobClient' object has no attribute 'merge_positions'`.
- Fallback: dual-dump (lossy). Drawdown kill fired on cash-only metric (FX-095).

## SDK verification (local venv, `py-clob-client-v2==1.0.0`)

- Path: `venv/lib/python3.14/site-packages/py_clob_client_v2/client.py`
- **No** `merge_positions`, `split`, or `redeem` methods on `ClobClient`.
- FX-094 is FX-035 class: V1 method name assumed on V2 SDK.

## Wallet / signing path (from [`config.py`](../config.py))

| Setting | Value | Implication |
|---------|-------|-------------|
| `SIGNATURE_TYPE` | `2` | POLY_GNOSIS_SAFE — Safe/proxy wallet |
| `FUNDER` | env `FUNDER` | Holds positions; merge must execute **from FUNDER** |
| `BUILDER_CODE` | on orders only | Order attribution — **not** relayer auth |

Merge for Safe wallets requires **Builder Relayer** (gas-free). Direct EOA `web3` CTF call is insufficient for production FUNDER.

## Collateral & contracts (V2, from [`check_wallet.py`](../check_wallet.py))

| Item | Address |
|------|---------|
| pUSD (V2 exchange collateral) | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
| CTF | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| NegRisk Adapter | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |
| CTF collateral adapter (standard) | `0xAdA100Db00Ca00073811820692005400218FcE1f` |
| NegRisk CTF collateral adapter | `0xadA2005600Dec949baf300f4C6120000bDB6eAab` |

Standard binary merge: `mergePositions(pUSD, parentCollectionId=0, conditionId, partition=[1,2], amount)` routed through **CTF collateral adapter** (not raw CTF direct — per `poly-web3` v2 path).

Neg-risk markets: route through **NegRisk Adapter** + neg-risk collateral adapter (auto-detect via Gamma API `negRisk` flag).

## Chosen implementation

**Module:** [`ctf_merge.py`](../ctf_merge.py) wrapping `poly-web3==2.0.3` + `py-builder-relayer-client`.

Rationale: in-repo relayer reimplementation would duplicate ~500 lines; `poly-web3` is the verified V2 Safe-wallet merge path. Rejected: V1 `py-clob-client` shim.

**Required env (optional — merge disabled without them):**

- `BUILDER_API_KEY`
- `BUILDER_SECRET`
- `BUILDER_PASSPHRASE`

Relayer URL: `https://relayer-v2.polymarket.com` (poly-web3 default).

## Verification contract (phantom-merge guard)

Reuse existing pattern from `dump_manager.try_merge`:

1. `update_balance_allowance` for YES + NO token IDs.
2. Snapshot YES balance **before** merge.
3. Execute merge via relayer.
4. Snapshot YES balance **after** merge.
5. If `post_yes >= pre_yes - 0.5` → **phantom merge**, do not record unwind.

## Failure policy (FX-094 fix)

On merge unavailable or failure:

- **Do NOT** auto dual-dump (Becerra loss path).
- Fire `alert_merge_needed` Discord alert.
- Hold hedged pair (~$1/pair economic value).

## UNSURE (operator verify on Helsinki before first live merge)

- Whether Becerra (`0xa5d79e71…`) was neg-risk — check Gamma `/markets?condition_id=…`.
- Whether Builder Relayer credentials are provisioned on Helsinki `.env`.
- Relayer daily quota (100 req/day per poly-web3 FAQ) under high merge volume.
