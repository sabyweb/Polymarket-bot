"""
Pricing logic for the Polymarket market-making bot.

Co-best pricing (join at best bid/ask) with:
  - M1: EMA fair value (stable midpoint reference)
  - M2: Dynamic spread (volatility-based widening)
  - M5: Book imbalance guard (thin-side widening)
  - Inventory skew and post-fill cooldown
"""

import logging
import math
import time as _time
import config
from config import DEAD_ZONE_BUFFER  # truly immutable constant
from alerts import alert_danger_zone

log = logging.getLogger(__name__)

def _ema_alpha() -> float:
    """EMA smoothing factor: alpha = 2 / (N + 1). Read at call time for overrides."""
    return 2.0 / (config.EMA_HALF_LIFE_CYCLES + 1)


class PricingMixin:
    """Mixin providing price calculation methods for OrderManager."""

    # ── Price Calculation ────────────────────────────────────────────────────
    def calculate_order_prices(
        self, order_book: dict
    ) -> tuple[float | None, float | None]:
        """Calculate bid and ask prices using co-best pricing.

        Layers applied in order:
          1. Co-best join (join at best bid/ask)
          2. M5: Book imbalance guard (widen thin side)
          3. M2: Volatility-based widening
          4. Inventory skew
          5. Post-fill cooldown

        Args:
            order_book: Dict with 'bids' and 'asks' lists.

        Returns:
            (our_bid, our_ask) or (None, None) if conditions are not met.
        """
        try:
            tick = self.market.get("tick_size", 0.01)
            max_spread = self.market["max_spread"]
            best_bid = float(order_book["bids"][0]["price"])
            best_ask = float(order_book["asks"][0]["price"])
            raw_mid = (best_bid + best_ask) / 2

            # ── M1: Update EMA fair value ─────────────────────────────
            if not self._ema_initialized:
                self._ema_mid = raw_mid
                self._ema_initialized = True
            else:
                self._ema_mid += _ema_alpha() * (raw_mid - self._ema_mid)

            # Use EMA midpoint for all downstream calculations.
            # This smooths out transient spikes that would cause
            # our skew/zone/reward-window logic to overreact.
            midpoint = self._ema_mid

            # ── M2: Update volatility history ─────────────────────────
            self._midpoint_history.append(raw_mid)
            if len(self._midpoint_history) > config.VOL_WINDOW_CYCLES:
                self._midpoint_history = self._midpoint_history[-config.VOL_WINDOW_CYCLES:]

            # Layer 1: Co-best + depth check + reward window clamp
            our_bid, our_ask = self._spread_relative_prices(
                best_bid, best_ask, midpoint, max_spread, tick, order_book
            )

            if our_bid is None or our_ask is None:
                return None, None

            # ── M2: Volatility-based spread widening ──────────────────
            vol_ticks = self._volatility_widen_ticks(tick)
            if vol_ticks > 0:
                our_bid = self.round_to_tick(our_bid - vol_ticks * tick)
                our_ask = self.round_to_tick(our_ask + vol_ticks * tick)
                # Clamp to reward window
                reward_floor = self.round_to_tick(midpoint - max_spread)
                reward_ceil = self.round_to_tick(midpoint + max_spread)
                our_bid = max(our_bid, reward_floor)
                our_ask = min(our_ask, reward_ceil)
                log.info(
                    f"VOL WIDEN | +{vol_ticks} ticks each side | "
                    f"bid={our_bid:.4f} ask={our_ask:.4f} | "
                    f"market={self.market['question'][:40]}"
                )

            # ── Inventory skew ──────────────────────────────────────────
            if config.INVENTORY_SKEW_ENABLED:
                our_bid, our_ask = self._apply_inventory_skew(
                    our_bid, our_ask, tick, midpoint, max_spread
                )

            # ── Post-fill cooldown ─────────────────────────────────────
            now = _time.time()
            if now - self._last_fill_time.get("yes", 0) < config.POST_FILL_COOLDOWN_SECS:
                widened = self.round_to_tick(our_bid - config.POST_FILL_WIDEN_TICKS * tick)
                min_bid = self.round_to_tick(midpoint - max_spread)
                our_bid = max(widened, min_bid)
                log.info(
                    f"POST-FILL COOLDOWN | YES bid widened to {our_bid:.4f} | "
                    f"market={self.market['question'][:40]}"
                )
            if now - self._last_fill_time.get("no", 0) < config.POST_FILL_COOLDOWN_SECS:
                widened = self.round_to_tick(our_ask + config.POST_FILL_WIDEN_TICKS * tick)
                max_ask = self.round_to_tick(midpoint + max_spread)
                our_ask = min(widened, max_ask)
                log.info(
                    f"POST-FILL COOLDOWN | NO ask widened to {our_ask:.4f} | "
                    f"market={self.market['question'][:40]}"
                )

            # Safety clamps
            our_bid = max(0.01, min(our_bid, 0.98))
            our_ask = max(0.02, min(our_ask, 0.99))

            if our_bid >= our_ask:
                log.error(
                    f"CRITICAL: bid ({our_bid}) >= ask ({our_ask}) — "
                    f"aborting cycle for {self.market['question'][:40]}"
                )
                return None, None

            log.debug(
                f"Prices | raw_mid={raw_mid:.4f} | ema_mid={midpoint:.4f} | "
                f"bid={our_bid:.4f} | ask={our_ask:.4f}"
            )
            return our_bid, our_ask

        except Exception as e:
            log.error(f"Price calculation failed: {e}")
            return None, None

    # ── M2: Volatility Measurement ───────────────────────────────────────
    def _volatility_widen_ticks(self, tick: float) -> int:
        """Calculate how many extra ticks to widen based on recent volatility.

        Uses standard deviation of recent midpoint changes as the vol measure.
        In calm markets, returns 0 (pure co-best).
        In volatile markets, returns up to VOL_WIDEN_MAX_TICKS.
        """
        history = self._midpoint_history
        if len(history) < 5:
            return 0  # Not enough data yet

        # Calculate returns (absolute price changes)
        changes = [abs(history[i] - history[i - 1]) for i in range(1, len(history))]
        if not changes:
            return 0

        avg_change = sum(changes) / len(changes)
        # Standard deviation of changes
        variance = sum((c - avg_change) ** 2 for c in changes) / len(changes)
        vol = math.sqrt(variance) if variance > 0 else 0

        if vol < tick * 0.5:
            return 0  # Below noise floor — pure co-best

        # Scale: vol_ticks = vol / tick * multiplier, capped
        vol_ticks = int(vol / tick * config.VOL_SPREAD_MULTIPLIER)
        vol_ticks = min(vol_ticks, config.VOL_WIDEN_MAX_TICKS)

        return vol_ticks

    # ── M4: Dynamic Order Sizing ──────────────────────────────────────────
    def calculate_dynamic_size(self, order_book: dict) -> float:
        """Calculate optimal order size based on market conditions.

        Combines four signals into a sizing score (0-1):
          1. Reward efficiency: daily_rate / liquidity → higher = size up
          2. Stability: inverse of recent volatility → calmer = size up
          3. Depth: bid-side book depth → deeper = size up
          4. Spread width: max_spread → wider window = size up

        The score maps linearly to [DYNAMIC_SIZE_MIN, DYNAMIC_SIZE_MAX].
        Falls back to ORDER_SIZE if dynamic sizing is disabled.

        Returns:
            Target order budget in USD.
        """
        if not config.DYNAMIC_SIZING_ENABLED:
            return float(config.ORDER_SIZE)

        # ── Signal 1: Reward efficiency (0-1) ──────────────────────────
        # Estimate our reward capture rate. Markets with high daily_rate
        # relative to liquidity are more capital-efficient.
        daily_rate = self.market.get("daily_rate", 0)
        liquidity = self.market.get("liquidity", 0)
        # our_capital / (our_capital + liquidity) × daily_rate / our_capital
        # Simplify: daily_rate / (our_capital + liquidity)
        # Normalize: cap at 0.10 (10% daily return is exceptional)
        if liquidity > 0 and daily_rate > 0:
            eff = daily_rate / (config.ORDER_SIZE * 2 + liquidity)
            reward_score = min(1.0, eff / 0.10)
        else:
            reward_score = 0.3  # No data → conservative

        # ── Signal 2: Stability (0-1) ──────────────────────────────────
        # Uses the same volatility measurement as M2.
        # Low vol = stable = 1.0, high vol = unstable = 0.0
        tick = self.market.get("tick_size", 0.01)
        vol_ticks = self._volatility_widen_ticks(tick)
        # vol_ticks: 0 = calm, 3 = max volatile (VOL_WIDEN_MAX_TICKS)
        stability_score = 1.0 - (vol_ticks / max(config.VOL_WIDEN_MAX_TICKS, 1))

        # ── Signal 3: Depth (0-1) ─────────────────────────────────────
        # Sum of top-5 bid levels in dollar terms.
        # $500 = MIN_BID_DEPTH_USD (just acceptable) → 0.0
        # $5000+ = excellent → 1.0
        bid_depth = sum(
            float(level["price"]) * float(level["size"])
            for level in order_book.get("bids", [])[:5]
        )
        depth_score = min(1.0, max(0.0, (bid_depth - config.MIN_BID_DEPTH_USD) / 4500))

        # ── Signal 4: Spread width (0-1) ──────────────────────────────
        # Wider max_spread = easier to stay in reward window.
        # 1c (0.01) = tight → 0.0
        # 5c (0.05) = comfortable → 1.0
        max_spread = self.market.get("max_spread", 0.03)
        spread_score = min(1.0, max(0.0, (max_spread - 0.01) / 0.04))

        # ── Weighted combination ───────────────────────────────────────
        sizing_score = (
            config.SIZE_WEIGHT_REWARD * reward_score
            + config.SIZE_WEIGHT_STABILITY * stability_score
            + config.SIZE_WEIGHT_DEPTH * depth_score
            + config.SIZE_WEIGHT_SPREAD * spread_score
        )
        sizing_score = max(0.0, min(1.0, sizing_score))

        # Map to dollar range
        dynamic_size = config.DYNAMIC_SIZE_MIN + sizing_score * (config.DYNAMIC_SIZE_MAX - config.DYNAMIC_SIZE_MIN)

        log.info(
            f"DYNAMIC SIZE | ${dynamic_size:.0f} "
            f"(score={sizing_score:.2f}) | "
            f"reward={reward_score:.2f} stability={stability_score:.2f} "
            f"depth={depth_score:.2f} spread={spread_score:.2f} | "
            f"market={self.market['question'][:40]}"
        )

        return dynamic_size

    def _spread_relative_prices(
        self, best_bid: float, best_ask: float,
        midpoint: float, max_spread: float, tick: float,
        order_book: dict,
    ) -> tuple[float | None, float | None]:
        """Co-best pricing with book imbalance guard (M5).

        Strategy: join at the best bid/ask, but widen our quote on any
        side where the book is significantly thinner than the other.
        A thin bid side means sellers are about to push through →
        widen our bid.  A thin ask side means buyers are about to
        push through → widen our ask.

        Also checks bid-side depth — if the book is too thin to unwind,
        we skip this market for this cycle.
        """
        question = self.market["question"]

        # ── Depth calculation (both sides) ────────────────────────────
        bid_depth = sum(
            float(level["price"]) * float(level["size"])
            for level in order_book["bids"][:5]
        )
        ask_depth = sum(
            float(level["price"]) * float(level["size"])
            for level in order_book["asks"][:5]
        )

        if bid_depth < config.MIN_BID_DEPTH_USD:
            log.warning(
                f"Bid depth too thin (${bid_depth:.0f} < "
                f"${config.MIN_BID_DEPTH_USD:.0f}) for {question[:40]} — "
                f"skipping to avoid unsellable inventory"
            )
            return None, None

        # Co-best: join at the best bid and best ask
        our_bid = best_bid
        our_ask = best_ask

        # ── M5: Book imbalance guard ──────────────────────────────────
        # If one side is much thinner, informed flow is about to push
        # through our quote on that side.  Widen to avoid the hit.
        min_depth = min(bid_depth, ask_depth)
        if min_depth > 0:
            imbalance_ratio = max(bid_depth, ask_depth) / min_depth
            if imbalance_ratio >= config.BOOK_IMBALANCE_THRESHOLD:
                widen = config.BOOK_IMBALANCE_WIDEN_TICKS * tick
                if bid_depth < ask_depth:
                    # Thin bids → price likely to drop → widen our bid
                    our_bid = self.round_to_tick(our_bid - widen)
                    log.info(
                        f"IMBALANCE GUARD | Thin bids ({imbalance_ratio:.1f}:1) | "
                        f"bid widened by {config.BOOK_IMBALANCE_WIDEN_TICKS} ticks → "
                        f"{our_bid:.4f} | market={question[:40]}"
                    )
                else:
                    # Thin asks → price likely to rise → widen our ask
                    our_ask = self.round_to_tick(our_ask + widen)
                    log.info(
                        f"IMBALANCE GUARD | Thin asks ({imbalance_ratio:.1f}:1) | "
                        f"ask widened by {config.BOOK_IMBALANCE_WIDEN_TICKS} ticks → "
                        f"{our_ask:.4f} | market={question[:40]}"
                    )

        # Clamp to stay inside the reward window (midpoint ± max_spread)
        reward_floor = self.round_to_tick(midpoint - max_spread)
        reward_ceil = self.round_to_tick(midpoint + max_spread)
        our_bid = max(our_bid, reward_floor)
        our_ask = min(our_ask, reward_ceil)

        # Verify still inside reward window after rounding
        if abs(our_bid - midpoint) > max_spread + tick * 0.5:
            log.warning(
                f"Bid outside reward window "
                f"(bid={our_bid:.4f}, mid={midpoint:.4f}, max_spread={max_spread}) "
                f"for {question[:40]} — skipping"
            )
            return None, None
        if abs(our_ask - midpoint) > max_spread + tick * 0.5:
            log.warning(
                f"Ask outside reward window "
                f"(ask={our_ask:.4f}, mid={midpoint:.4f}, max_spread={max_spread}) "
                f"for {question[:40]} — skipping"
            )
            return None, None

        bid_top_depth = float(order_book["bids"][0]["price"]) * float(order_book["bids"][0]["size"])
        ask_top_depth = float(order_book["asks"][0]["price"]) * float(order_book["asks"][0]["size"])

        log.debug(
            f"Co-best pricing | ema_mid={midpoint:.4f} | "
            f"bid={our_bid:.4f} (depth=${bid_top_depth:.0f}) | "
            f"ask={our_ask:.4f} (depth=${ask_top_depth:.0f}) | "
            f"reward_window=[{reward_floor:.4f}, {reward_ceil:.4f}] | "
            f"bid_depth=${bid_depth:.0f} | ask_depth=${ask_depth:.0f} | "
            f"imbalance={bid_depth/ask_depth:.1f}:1" if ask_depth > 0 else ""
        )
        return our_bid, our_ask

    def _apply_inventory_skew(
        self, our_bid: float, our_ask: float,
        tick: float, midpoint: float, max_spread: float,
    ) -> tuple[float, float]:
        """Skew quotes based on current inventory to unwind naturally.

        When holding YES inventory: tighten the ask (sell YES faster),
        widen the bid (buy YES slower).
        When holding NO inventory: tighten the bid (which is the NO sell
        equivalent), widen the ask.

        The skew amount scales with inventory size: larger positions get
        more aggressive skew.
        """
        condition_id = self.market["condition_id"]
        yes_usd = self.position_tracker.get_position(condition_id, "yes")
        no_usd = self.position_tracker.get_position(condition_id, "no")

        if yes_usd < config.INVENTORY_SKEW_THRESHOLD and no_usd < config.INVENTORY_SKEW_THRESHOLD:
            return our_bid, our_ask

        # Calculate skew steps — each $100 of inventory = 1 step
        yes_steps = int(yes_usd / 100) if yes_usd >= config.INVENTORY_SKEW_THRESHOLD else 0
        no_steps = int(no_usd / 100) if no_usd >= config.INVENTORY_SKEW_THRESHOLD else 0
        net_skew = yes_steps - no_steps  # positive = long YES, negative = long NO

        skew_amount = abs(net_skew) * config.INVENTORY_SKEW_TICKS * tick

        if net_skew > 0:
            # Long YES → want to SELL YES → tighten ask, widen bid
            new_ask = self.round_to_tick(our_ask - skew_amount)
            new_bid = self.round_to_tick(our_bid - skew_amount)
            # Don't cross midpoint or push outside reward window
            new_ask = max(new_ask, self.round_to_tick(midpoint + tick))
            new_bid = max(new_bid, self.round_to_tick(midpoint - max_spread))
            log.info(
                f"INVENTORY SKEW | long YES ${yes_usd:.0f} | "
                f"ask {our_ask:.4f}→{new_ask:.4f} | "
                f"bid {our_bid:.4f}→{new_bid:.4f} | "
                f"market={self.market['question'][:40]}"
            )
            our_ask, our_bid = new_ask, new_bid
        elif net_skew < 0:
            # Long NO → want to SELL NO → in YES-equiv terms, tighten bid, widen ask
            new_bid = self.round_to_tick(our_bid + skew_amount)
            new_ask = self.round_to_tick(our_ask + skew_amount)
            # Don't cross midpoint or push outside reward window
            new_bid = min(new_bid, self.round_to_tick(midpoint - tick))
            new_ask = min(new_ask, self.round_to_tick(midpoint + max_spread))
            log.info(
                f"INVENTORY SKEW | long NO ${no_usd:.0f} | "
                f"bid {our_bid:.4f}→{new_bid:.4f} | "
                f"ask {our_ask:.4f}→{new_ask:.4f} | "
                f"market={self.market['question'][:40]}"
            )
            our_bid, our_ask = new_bid, new_ask

        return our_bid, our_ask

    # ── Zone Checking ────────────────────────────────────────────────────────
    def check_order_zone(
        self, order_id: str, best_bid: float, best_ask: float
    ) -> str:
        """Check which zone an order occupies relative to the midpoint.

        Zones:
            DANGER — too close to midpoint (high fill risk).
            REWARD — inside the max spread window (earning rewards).
            DEAD   — outside max spread window (earning nothing).

        Args:
            order_id: Exchange order identifier.
            best_bid: Current best bid on the book.
            best_ask: Current best ask on the book.

        Returns:
            One of "DANGER", "REWARD", "DEAD", or "UNKNOWN".
        """
        if order_id not in self.active_orders:
            return "UNKNOWN"

        order = self.active_orders[order_id]
        max_spread = self.market["max_spread"]

        # Prefer EMA midpoint (M1) if available, then market price, then raw
        if self._ema_initialized:
            midpoint = self._ema_mid
        elif self.market.get("yes_price") is not None:
            midpoint = self.market["yes_price"]
        else:
            midpoint = (best_bid + best_ask) / 2

        gap = abs(order.price - midpoint)

        if gap < config.DANGER_ZONE_CENTS:
            alert_danger_zone(
                self.market["question"], order.side.upper(), order.price, midpoint
            )
            return "DANGER"

        if gap > max_spread + DEAD_ZONE_BUFFER:
            return "DEAD"

        return "REWARD"
