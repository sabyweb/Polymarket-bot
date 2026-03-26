"""
Arbitrage detection for the Polymarket market-making bot.

Scans for two types of risk-free or near-risk-free opportunities:

1. **YES+NO complement arbitrage**: If YES + NO tokens can be bought for
   less than $1 total, buying both and merging yields risk-free profit.
   On Polymarket, each YES+NO pair = $1 USDC via merge.

2. **Cross-market mispricing**: When the same underlying event has
   multiple markets (e.g. "Bitcoin above $100K by March" vs "Bitcoin
   above $100K by June"), detects inconsistent pricing.

This module is advisory — it logs opportunities and returns them for
the bot to act on. It does NOT place orders directly.
"""

import logging
import time

log = logging.getLogger(__name__)


class ArbitrageScanner:
    """Scans order books for arbitrage opportunities.

    Args:
        client: Authenticated ClobClient (rate-limited).
        min_profit_pct: Minimum profit percentage to flag (default 0.5%).
        cooldown_secs: Don't re-scan the same market within this window.
    """

    def __init__(
        self,
        client: object,
        min_profit_pct: float = 0.005,
        cooldown_secs: float = 300,
    ) -> None:
        self._client = client
        self._min_profit_pct = min_profit_pct
        self._cooldown_secs = cooldown_secs
        self._last_scan: dict[str, float] = {}  # condition_id → timestamp

    def scan_complement_arb(
        self, markets: list[dict],
    ) -> list[dict]:
        """Scan active markets for YES+NO complement mispricing.

        For each market, checks if best_ask(YES) + best_ask(NO) < 1.0.
        If so, buying both and merging yields risk-free profit.

        Args:
            markets: List of market dicts with token_ids.

        Returns:
            List of opportunity dicts with keys:
                condition_id, question, yes_ask, no_ask, total_cost,
                profit_per_pair, profit_pct
        """
        now = time.time()
        opportunities: list[dict] = []

        for market in markets:
            cid = market.get("condition_id", "")
            question = market.get("question", "?")

            # Respect cooldown
            if now - self._last_scan.get(cid, 0) < self._cooldown_secs:
                continue
            self._last_scan[cid] = now

            token_ids = market.get("token_ids", [])
            if len(token_ids) < 2:
                continue

            try:
                yes_book = self._client.get_order_book(token_ids[0])
                no_book = self._client.get_order_book(token_ids[1])

                if not (yes_book.asks and no_book.asks):
                    continue

                yes_ask = float(yes_book.asks[0].price)
                no_ask = float(no_book.asks[0].price)
                yes_ask_size = float(yes_book.asks[0].size)
                no_ask_size = float(no_book.asks[0].size)

                total_cost = yes_ask + no_ask
                if total_cost >= 1.0:
                    continue

                profit_per_pair = 1.0 - total_cost
                profit_pct = profit_per_pair / total_cost

                if profit_pct < self._min_profit_pct:
                    continue

                # Size limited by smaller side
                max_pairs = min(yes_ask_size, no_ask_size)
                total_profit = profit_per_pair * max_pairs

                opp = {
                    "condition_id": cid,
                    "question": question,
                    "yes_ask": yes_ask,
                    "no_ask": no_ask,
                    "total_cost": total_cost,
                    "profit_per_pair": profit_per_pair,
                    "profit_pct": profit_pct,
                    "max_pairs": max_pairs,
                    "total_profit": total_profit,
                }
                opportunities.append(opp)

                log.warning(
                    f"ARB OPPORTUNITY | {question[:40]} | "
                    f"YES_ask={yes_ask:.4f} + NO_ask={no_ask:.4f} "
                    f"= {total_cost:.4f} (< $1) | "
                    f"profit={profit_pct:.2%} | "
                    f"max_pairs={max_pairs:.0f} | "
                    f"est_profit=${total_profit:.2f}"
                )

            except Exception as e:
                log.debug(f"Arb scan failed for {question[:30]}: {e}")
                continue

        if not opportunities:
            log.debug(f"No complement arb found across {len(markets)} markets")

        return opportunities

    def scan_spread_capture(
        self, market: dict, position_tracker: object,
    ) -> dict | None:
        """Check if we can sell existing inventory above VWAP (spread capture).

        When the market moves in our favor after a fill, the current bid
        may be above our VWAP. Selling now locks in a profit instead of
        waiting for decay to bring the sell price down.

        Args:
            market: Market dict with token_ids and condition_id.
            position_tracker: PositionTracker/PositionStore instance.

        Returns:
            Dict with side, sell_price, profit_pct if opportunity found,
            else None.
        """
        cid = market.get("condition_id", "")
        token_ids = market.get("token_ids", [])
        if len(token_ids) < 2:
            return None

        for side_idx, side in enumerate(("yes", "no")):
            shares = position_tracker.get_shares(cid, side)
            avg_price = position_tracker.get_avg_price(cid, side)
            if shares < 1.0 or avg_price <= 0:
                continue

            try:
                book = self._client.get_order_book(token_ids[side_idx])
                if not book.bids:
                    continue

                best_bid = float(book.bids[0].price)
                bid_size = float(book.bids[0].size)

                # Our cost in CLOB terms
                clob_cost = avg_price if side == "yes" else (1 - avg_price)
                if clob_cost <= 0:
                    continue

                # Can we sell above cost?
                if best_bid <= clob_cost:
                    continue

                profit_pct = (best_bid - clob_cost) / clob_cost
                sellable = min(shares, bid_size)
                est_profit = (best_bid - clob_cost) * sellable

                if profit_pct < 0.005 or est_profit < 0.50:
                    continue  # Not worth the gas/effort

                log.info(
                    f"SPREAD CAPTURE | {side.upper()} | "
                    f"cost={clob_cost:.4f} | bid={best_bid:.4f} | "
                    f"profit={profit_pct:.2%} | sellable={sellable:.0f} | "
                    f"est_profit=${est_profit:.2f} | "
                    f"market={market.get('question', '?')[:40]}"
                )

                return {
                    "side": side,
                    "sell_price": best_bid,
                    "clob_cost": clob_cost,
                    "profit_pct": profit_pct,
                    "sellable": sellable,
                    "est_profit": est_profit,
                }

            except Exception as e:
                log.debug(f"Spread capture check failed for {side}: {e}")
                continue

        return None
