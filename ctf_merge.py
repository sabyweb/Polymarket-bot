"""FX-094 — Centralized YES+NO merge to recover pUSD.

Wraps poly-web3 + Builder Relayer for Safe/proxy wallets (SIGNATURE_TYPE=2).
See docs/merge_ground_truth.md for verified contract paths.

Fail policy: returns False on any error; caller must NOT dual-dump immediately.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

log = logging.getLogger(__name__)

_RELAYER_URL = "https://relayer-v2.polymarket.com"


def _builder_creds_configured() -> bool:
    return bool(
        os.getenv("BUILDER_API_KEY")
        and os.getenv("BUILDER_SECRET")
        and os.getenv("BUILDER_PASSPHRASE")
    )


def _make_poly_service(clob_client: Any) -> Any | None:
    """Lazy-init PolyWeb3Service. Returns None if deps or creds missing."""
    if not _builder_creds_configured():
        log.warning(
            "[MERGE] Builder Relayer creds missing "
            "(BUILDER_API_KEY/SECRET/PASSPHRASE) — merge disabled"
        )
        return None
    try:
        from py_builder_relayer_client.client import RelayClient
        from py_builder_signing_sdk.config import BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
        from poly_web3 import PolyWeb3Service
        from config import CHAIN_ID, PRIVATE_KEY
    except ImportError as e:
        log.warning(f"[MERGE] poly-web3 / relayer deps not installed: {e}")
        return None

    relayer = RelayClient(
        _RELAYER_URL,
        CHAIN_ID,
        PRIVATE_KEY,
        BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=os.getenv("BUILDER_API_KEY", ""),
                secret=os.getenv("BUILDER_SECRET", ""),
                passphrase=os.getenv("BUILDER_PASSPHRASE", ""),
            )
        ),
    )
    rpc = os.getenv(
        "POLYGON_RPC_URL",
        "https://polygon-mainnet.g.alchemy.com/v2/"
        + os.getenv("ALCHEMY_KEY", ""),
    )
    return PolyWeb3Service(
        clob_client=clob_client,
        relayer_client=relayer,
        rpc_url=rpc,
    )


def try_merge_positions(
    clob_client: Any,
    *,
    condition_id: str,
    amount: float,
    yes_tid: str,
    negative_risk: bool | None = None,
    verify_balance_fn: Optional[Any] = None,
) -> tuple[bool, str]:
    """Merge `amount` YES+NO pairs back to pUSD via Builder Relayer.

    Args:
        clob_client: underlying ClobClient (not RateLimitedClient wrapper).
        condition_id: market condition ID (0x-prefixed hex).
        amount: whole pairs to merge (human shares).
        yes_tid: YES token ID for phantom-merge balance check.
        negative_risk: optional hint; None → auto-detect via Gamma API.
        verify_balance_fn: optional callable returning YES share float;
            used for pre/post phantom-merge guard. Signature: () -> float.

    Returns:
        (success, reason) — reason empty on success.
    """
    if amount < 1.0:
        return False, "amount < 1"

    service = _make_poly_service(clob_client)
    if service is None:
        return False, "merge_unavailable"

    pre_yes: float | None = None
    if verify_balance_fn is not None:
        try:
            pre_yes = float(verify_balance_fn())
        except Exception as e:
            log.warning(f"[MERGE] pre-balance read failed: {e}")

    try:
        result = service.merge(
            condition_id,
            amount,
            negative_risk=negative_risk,
        )
    except Exception as e:
        log.warning(f"[MERGE] relayer merge failed for {condition_id[:12]}: {e}")
        return False, f"merge_exception: {e}"

    if result is None:
        return False, "merge_returned_none"

    if verify_balance_fn is not None and pre_yes is not None:
        try:
            post_yes = float(verify_balance_fn())
        except Exception as e:
            log.warning(f"[MERGE] post-balance read failed: {e}")
            return False, "post_balance_read_failed"
        if post_yes >= pre_yes - 0.5:
            log.critical(
                f"PHANTOM MERGE: relayer returned but YES balance unchanged "
                f"(pre={pre_yes:.0f} post={post_yes:.0f}, expected -{amount:.0f}) | "
                f"cid={condition_id[:12]}"
            )
            return False, "phantom_merge"

    log.info(
        f"MERGE SUCCESS | {amount:.0f} pairs | cid={condition_id[:12]} | "
        f"result={result!r:.120}"
    )
    return True, ""
