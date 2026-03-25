"""
24-hour Monte Carlo simulation of the Polymarket market-making bot.

Replicates the exact decision logic from orders.py/position.py/config.py
using synthetic price data calibrated from real log observations.

Usage:
    python simulate.py             # default seed=42
    python simulate.py --seed 123  # different scenario
"""

import argparse
import math
import random
from dataclasses import dataclass, field

# ── Config (mirrors config.py — updated 2026-03-25) ─────────────────────────
ORDER_SIZE = 250           # Reduced from $500
MAX_ORDER_BUDGET = 750     # Allows sports markets with high min_size
MAX_POSITION_USD = 400     # Reduced from $500
RESUME_POSITION_USD = 300  # Reduced from $400
CYCLE_SECS = 30
DECAY_INTERVAL_SECS = 300  # Every 5 min (was 10)
DECAY_TICKS = 1
STOP_LOSS_PCT = 0.20
MIN_STOP_LOSS_USD = 50.0   # AND absolute loss must exceed $50
STOP_LOSS_MIN_PRICE = 0.20 # Skip stop-loss on tokens under 20c
MIN_SELL_PRICE = 0.01
ACCEL_LOSS_PCT = 0.08      # Accelerate decay when loss > 8% (was 10%)
ACCEL_MULTIPLIER = 5       # 5x decay speed when accelerated (was 3x)
CHEAP_THRESHOLD = 0.25     # Tokens under 25c get 50% order size
CHEAP_SCALE = 0.50
TICK = 0.01
BUFFER_OFFSET = 0.03       # ~3c behind best (was 2c, wider buffer)
MIN_SHARES = 200


# ── Market Simulation ────────────────────────────────────────────────────────
@dataclass
class SimMarket:
    name: str
    price: float
    start_price: float
    sigma: float          # per-cycle std dev
    drift: float          # per-cycle drift
    mean_revert: float    # pull toward start (0 = none)
    daily_rate: float
    jump_prob: float = 0.0
    jump_size: float = 0.0

    def step(self):
        """Advance price by one cycle (30s)."""
        # Mean reversion
        pull = self.mean_revert * (self.start_price - self.price)
        # Random noise
        noise = random.gauss(0, self.sigma)
        # Occasional jumps
        jump = 0.0
        if self.jump_prob > 0 and random.random() < self.jump_prob:
            jump = random.choice([-1, 1]) * self.jump_size
        self.price = max(0.02, min(0.98, self.price + self.drift + pull + noise + jump))

    @property
    def best_bid(self):
        return round(self.price - 0.005, 4)

    @property
    def best_ask(self):
        return round(self.price + 0.005, 4)

    @property
    def our_bid(self):
        return round_down(self.best_bid - BUFFER_OFFSET, TICK)

    @property
    def our_ask(self):
        return round_up(self.best_ask + BUFFER_OFFSET, TICK)


def round_down(price, tick):
    return math.floor(price / tick) * tick


def round_up(price, tick):
    return math.ceil(price / tick) * tick


# ── Position Tracking ────────────────────────────────────────────────────────
@dataclass
class Position:
    shares: float = 0.0
    avg_price: float = 0.0
    usd_exposure: float = 0.0
    halted: bool = False

    def record_fill(self, shares, price):
        if self.shares + shares > 0:
            self.avg_price = (
                (self.avg_price * self.shares + price * shares)
                / (self.shares + shares)
            )
        self.shares += shares
        self.usd_exposure += shares * price
        if self.usd_exposure >= MAX_POSITION_USD:
            self.halted = True

    def record_unwind(self, shares, price):
        self.shares = max(0, self.shares - shares)
        self.usd_exposure = max(0, self.usd_exposure - shares * price)
        if self.usd_exposure <= RESUME_POSITION_USD:
            self.halted = False
        if self.shares < 1:
            self.avg_price = 0.0
            self.usd_exposure = 0.0
            self.halted = False


# ── Orders ───────────────────────────────────────────────────────────────────
@dataclass
class BuyOrder:
    side: str  # "yes" or "no"
    price: float  # clob price
    size: float
    est_cost: float

@dataclass
class SellOrder:
    side: str
    clob_price: float
    base_clob_price: float  # VWAP-based (before decay)
    size: float
    created_at: int  # cycle number when first created
    avg_price: float  # YES-equivalent avg


# ── Order Manager (per market) ───────────────────────────────────────────────
class SimOrderManager:
    def __init__(self, market: SimMarket):
        self.market = market
        self.yes_pos = Position()
        self.no_pos = Position()
        self.buy_orders: list[BuyOrder] = []
        self.sell_orders: list[SellOrder] = []
        self.stats = {
            "realized_pnl": 0.0,
            "fills": 0,
            "unwinds": 0,
            "stop_losses": 0,
            "decay_sells": 0,
            "cycles_with_orders": 0,
        }

    def get_pos(self, side):
        return self.yes_pos if side == "yes" else self.no_pos

    def inventory_at_cost(self) -> float:
        """Total cost basis of all open inventory."""
        total = 0.0
        for side in ("yes", "no"):
            pos = self.get_pos(side)
            if pos.shares >= 1:
                # avg_price stores clob_price for both sides
                total += pos.shares * pos.avg_price
        return total

    def run_cycle(self, cycle: int, free_usdc: float) -> float:
        """Run one full cycle. Returns USDC change (negative = spent, positive = freed)."""
        usdc_delta = 0.0

        # Step 1: Detect fills
        usdc_delta += self._detect_fills(cycle)

        # Step 2: Reconcile unwinds (with decay)
        self._reconcile_unwinds(cycle)

        # Step 3: Check stop-loss
        self._check_stop_loss(cycle)

        # Step 4: Cancel stale BUY orders (price moved away)
        new_buys = []
        for order in self.buy_orders:
            if order.side == "yes":
                optimal = self.market.our_bid
            else:
                optimal = round_down(1 - self.market.our_ask, TICK)
            if abs(order.price - optimal) >= TICK:
                usdc_delta += order.est_cost  # refund
            else:
                new_buys.append(order)
        self.buy_orders = new_buys

        # Step 5: Place fresh BUY orders
        has_live_orders = len(self.buy_orders) > 0 or len(self.sell_orders) > 0

        for side in ("yes", "no"):
            pos = self.get_pos(side)
            # Skip if halted
            if pos.halted:
                continue
            # Skip if already have BUY on this side
            if any(o.side == side for o in self.buy_orders):
                continue
            # Skip if unhedged position (need to sell first)
            if pos.shares >= 1 and not any(
                s.side == side for s in self.sell_orders
            ):
                continue
            # Soft guard: block both sides only when holding dual positions
            opposite = "no" if side == "yes" else "yes"
            opp_pos = self.get_pos(opposite)
            if pos.shares >= 1 and opp_pos.shares >= 1:
                continue

            # Calculate price and size
            if side == "yes":
                clob_price = self.market.our_bid
            else:
                clob_price = round_down(1 - self.market.our_ask, TICK)

            if clob_price <= 0.01 or clob_price >= 0.99:
                continue

            eff_order = ORDER_SIZE * CHEAP_SCALE if clob_price < CHEAP_THRESHOLD else ORDER_SIZE
            size = max(MIN_SHARES, eff_order / clob_price)
            size = min(size, MAX_ORDER_BUDGET / clob_price)
            est_cost = size * clob_price

            # Balance check
            if est_cost > free_usdc + usdc_delta:
                continue

            self.buy_orders.append(BuyOrder(side, clob_price, size, est_cost))
            usdc_delta -= est_cost
            has_live_orders = True

        if has_live_orders:
            self.stats["cycles_with_orders"] += 1

        return usdc_delta

    def _detect_fills(self, cycle: int) -> float:
        """Probabilistic fill model calibrated to real Polymarket behavior.

        BUY fills: ~2-4 per market per day. When a BUY fills, the market
        moved through our price (adverse selection), so we nudge price down.

        SELL fills: Much rarer (~0.5/day). Real data shows 0 sell fills
        across 48+ hours. Sells at cost rarely execute because the market
        moved against us on the buy side.
        """
        usdc_delta = 0.0
        mid = self.market.price
        BUY_FILL_PROB = 0.0012   # ~3.5 fills/day/side
        SELL_FILL_PROB = 0.0002  # ~0.6 fills/day/side (sells are hard)

        # Check BUY fills
        remaining_buys = []
        for order in self.buy_orders:
            if order.side == "yes":
                distance = mid - order.price
            else:
                distance = (1 - mid) - order.price
            if distance < 0:
                fill_prob = 0.5
            else:
                fill_prob = BUY_FILL_PROB * max(0.1, 1.0 - distance / 0.05)
            filled = random.random() < fill_prob

            if filled:
                pos = self.get_pos(order.side)
                pos.record_fill(order.size, order.price)
                self.stats["fills"] += 1
                # Adverse selection: market moved THROUGH our price to fill us.
                # Set mid so our token value is at our price minus overshoot.
                overshoot = random.uniform(0.01, 0.02)
                if order.side == "yes":
                    # YES buy at P fills when mid drops to P. Overshoot below.
                    self.market.price = max(0.02, order.price - overshoot)
                else:
                    # NO buy at P fills when NO value (1-mid) drops to P.
                    # So mid rises to (1-P). Overshoot above.
                    self.market.price = min(0.98, (1 - order.price) + overshoot)
            else:
                remaining_buys.append(order)

        self.buy_orders = remaining_buys

        # Check SELL fills (much harder — adverse selection means market
        # is usually below our sell price after we got filled)
        remaining_sells = []
        for order in self.sell_orders:
            if order.side == "yes":
                distance = order.clob_price - mid
            else:
                distance = order.clob_price - (1 - mid)
            if distance < 0:
                fill_prob = min(0.3, abs(distance) * 10)  # in-the-money but capped
            else:
                fill_prob = SELL_FILL_PROB * max(0.05, 1.0 - distance / 0.05)
            filled = random.random() < fill_prob

            if filled:
                pos = self.get_pos(order.side)
                buy_cost = order.avg_price
                sell_price = order.clob_price
                pnl = (sell_price - buy_cost) * order.size
                self.stats["realized_pnl"] += pnl
                self.stats["unwinds"] += 1
                pos.record_unwind(order.size, buy_cost)
                usdc_delta += sell_price * order.size
            else:
                remaining_sells.append(order)

        self.sell_orders = remaining_sells
        return usdc_delta

    def _reconcile_unwinds(self, cycle: int):
        """Place/update SELL orders with time decay."""
        for side in ("yes", "no"):
            pos = self.get_pos(side)
            if pos.shares < 1 or pos.avg_price <= 0:
                continue

            # Calculate VWAP-based clob price
            # avg_price stores clob_price for both sides (NO = NO token price)
            vwap_clob = round_down(pos.avg_price, TICK)

            # Check existing sell orders for this side
            covered = 0.0
            stale = []
            for i, order in enumerate(self.sell_orders):
                if order.side != side:
                    continue
                covered += order.size

                # Calculate expected decayed price (accelerated if underwater)
                dt = DECAY_TICKS
                if vwap_clob > 0:
                    mbid = self.market.best_bid if side == "yes" else max(MIN_SELL_PRICE, round(1 - self.market.best_ask, 4))
                    if (vwap_clob - mbid) / vwap_clob >= ACCEL_LOSS_PCT:
                        dt = DECAY_TICKS * ACCEL_MULTIPLIER
                elapsed_secs = (cycle - order.created_at) * CYCLE_SECS
                decay_intervals = int(elapsed_secs // DECAY_INTERVAL_SECS)
                decay_amount = decay_intervals * dt * TICK
                expected = max(MIN_SELL_PRICE, order.base_clob_price - decay_amount)

                # Check if VWAP shifted
                if abs(order.base_clob_price - vwap_clob) >= TICK:
                    stale.append(i)
                # Check if price needs to decay (only lower, never raise)
                elif order.clob_price > expected + TICK * 0.5:
                    stale.append(i)
                    if order.clob_price > expected:
                        self.stats["decay_sells"] += 1

            # Remove stale orders (preserve oldest created_at)
            oldest_created = cycle
            for i in sorted(stale, reverse=True):
                oldest_created = min(oldest_created, self.sell_orders[i].created_at)
                self.sell_orders.pop(i)
                covered = sum(
                    s.size for s in self.sell_orders if s.side == side
                )

            # Place new sell if unhedged
            unhedged = pos.shares - covered
            if unhedged < 1:
                continue

            # Calculate decayed price preserving original age
            carry_created = oldest_created
            for s in self.sell_orders:
                if s.side == side:
                    carry_created = min(carry_created, s.created_at)

            # Accelerated decay when underwater
            dt2 = DECAY_TICKS
            if vwap_clob > 0:
                mbid2 = self.market.best_bid if side == "yes" else max(MIN_SELL_PRICE, round(1 - self.market.best_ask, 4))
                if (vwap_clob - mbid2) / vwap_clob >= ACCEL_LOSS_PCT:
                    dt2 = DECAY_TICKS * ACCEL_MULTIPLIER
            elapsed_secs = (cycle - carry_created) * CYCLE_SECS
            decay_intervals = int(elapsed_secs // DECAY_INTERVAL_SECS)
            decay_amount = decay_intervals * dt2 * TICK
            decayed = max(MIN_SELL_PRICE, vwap_clob - decay_amount)

            self.sell_orders.append(SellOrder(
                side=side,
                clob_price=round(decayed, 4),
                base_clob_price=vwap_clob,
                size=unhedged,
                created_at=carry_created,
                avg_price=pos.avg_price,
            ))

    def _check_stop_loss(self, cycle: int):
        """Trigger stop-loss if unrealized loss >= threshold."""
        for side in ("yes", "no"):
            pos = self.get_pos(side)
            if pos.shares < 1 or pos.avg_price <= 0:
                continue

            # avg_price stores clob_price for both sides
            our_cost = round_down(pos.avg_price, TICK)
            if side == "yes":
                market_bid = self.market.best_bid
            else:
                market_bid = max(MIN_SELL_PRICE, round(1 - self.market.best_ask, 4))

            if our_cost <= 0:
                continue

            # Skip stop-loss on cheap tokens — let decay handle
            if our_cost < STOP_LOSS_MIN_PRICE:
                continue

            loss_pct = (our_cost - market_bid) / our_cost
            loss_usd = (our_cost - market_bid) * pos.shares
            if loss_pct < STOP_LOSS_PCT or loss_usd < MIN_STOP_LOSS_USD:
                continue

            # STOP-LOSS: cancel existing sells, place at market bid
            self.sell_orders = [s for s in self.sell_orders if s.side != side]
            sell_price = max(MIN_SELL_PRICE, round_down(market_bid, TICK))
            self.sell_orders.append(SellOrder(
                side=side,
                clob_price=sell_price,
                base_clob_price=round_down(pos.avg_price, TICK),
                size=pos.shares,
                created_at=cycle,
                avg_price=pos.avg_price,
            ))
            self.stats["stop_losses"] += 1

    def unrealized_pnl(self) -> float:
        """Calculate current unrealized P&L across both sides."""
        pnl = 0.0
        for side in ("yes", "no"):
            pos = self.get_pos(side)
            if pos.shares < 1:
                continue
            # avg_price stores clob_price for both sides
            cost = pos.avg_price
            if side == "yes":
                value = self.market.best_bid
            else:
                value = max(0, 1 - self.market.best_ask)
            pnl += (value - cost) * pos.shares
        return pnl

    def inventory_value(self) -> float:
        """Current market value of all inventory."""
        val = 0.0
        if self.yes_pos.shares >= 1:
            val += self.yes_pos.shares * self.market.best_bid
        if self.no_pos.shares >= 1:
            val += self.no_pos.shares * max(0, 1 - self.market.best_ask)
        return val

    def locked_in_orders(self) -> float:
        """USDC locked in open BUY orders."""
        return sum(o.est_cost for o in self.buy_orders)


# ── Simulator ────────────────────────────────────────────────────────────────
class Simulator:
    def __init__(self, starting_balance: float, seed: int):
        random.seed(seed)
        self.starting_balance = starting_balance
        self.free_usdc = starting_balance
        self.markets = [
            SimMarket("M1:MeanRevert", 0.30, 0.30, 0.002, 0.0, 0.001, 400),
            SimMarket("M2:TrendDown", 0.25, 0.25, 0.002, -0.0005, 0.0, 500),
            SimMarket("M3:Volatile", 0.50, 0.50, 0.005, 0.0, 0.0, 300,
                       jump_prob=0.005, jump_size=0.02),
            SimMarket("M4:Stable", 0.15, 0.15, 0.001, 0.0, 0.001, 600),
        ]
        self.managers = [SimOrderManager(m) for m in self.markets]
        self.hourly_data: list[dict] = []
        self.max_drawdown = 0.0
        self.peak_equity = starting_balance

    def _total_equity(self):
        return self.free_usdc + sum(
            m.inventory_value() + m.locked_in_orders() for m in self.managers
        )

    def run(self, hours: int = 24):
        total_cycles = hours * 3600 // CYCLE_SECS

        for cycle in range(total_cycles):
            # Step prices
            for m in self.markets:
                m.step()

            # Run each manager
            for mgr in self.managers:
                delta = mgr.run_cycle(cycle, self.free_usdc)
                self.free_usdc += delta

            # Track drawdown
            equity = self._total_equity()
            self.peak_equity = max(self.peak_equity, equity)
            drawdown = self.peak_equity - equity
            self.max_drawdown = max(self.max_drawdown, drawdown)

            # Hourly snapshot
            cycles_per_hour = 3600 // CYCLE_SECS
            if (cycle + 1) % cycles_per_hour == 0:
                hour = (cycle + 1) // cycles_per_hour
                self._snapshot(hour)

    def _snapshot(self, hour: int):
        data = {"hour": hour, "free_usdc": self.free_usdc, "markets": []}
        for mgr in self.managers:
            mdata = {
                "name": mgr.market.name,
                "price": mgr.market.price,
                "yes": {"shares": mgr.yes_pos.shares, "avg": mgr.yes_pos.avg_price,
                         "halted": mgr.yes_pos.halted},
                "no": {"shares": mgr.no_pos.shares, "avg": mgr.no_pos.avg_price,
                        "halted": mgr.no_pos.halted},
                "sell_count": len(mgr.sell_orders),
                "buy_count": len(mgr.buy_orders),
                "realized": mgr.stats["realized_pnl"],
                "unrealized": mgr.unrealized_pnl(),
                "fills": mgr.stats["fills"],
                "unwinds": mgr.stats["unwinds"],
                "stop_losses": mgr.stats["stop_losses"],
                "decay_sells": mgr.stats["decay_sells"],
            }
            data["markets"].append(mdata)
        self.hourly_data.append(data)

    def print_report(self, seed: int = 42):
        total_cycles = 24 * 3600 // CYCLE_SECS

        for snap in self.hourly_data:
            hr = snap["hour"]
            total_realized = sum(m["realized"] for m in snap["markets"])
            total_unrealized = sum(m["unrealized"] for m in snap["markets"])
            print(f"\n{'='*60}")
            print(f"  HOUR {hr:2d}  |  Free USDC: ${snap['free_usdc']:,.0f}")
            print(f"{'='*60}")
            for m in snap["markets"]:
                parts = []
                if m["yes"]["shares"] >= 1:
                    h_tag = " [HALTED]" if m["yes"]["halted"] else ""
                    parts.append(
                        f"YES {m['yes']['shares']:.0f}sh @{m['yes']['avg']:.2f}{h_tag}"
                    )
                if m["no"]["shares"] >= 1:
                    h_tag = " [HALTED]" if m["no"]["halted"] else ""
                    parts.append(
                        f"NO {m['no']['shares']:.0f}sh @{m['no']['avg']:.2f}{h_tag}"
                    )
                pos_str = " | ".join(parts) if parts else "flat"
                sl_str = f" SL:{m['stop_losses']}" if m["stop_losses"] else ""
                dec_str = f" dec:{m['decay_sells']}" if m["decay_sells"] else ""
                print(
                    f"  {m['name']:18s} px={m['price']:.3f} | "
                    f"R=${m['realized']:+.0f} U=${m['unrealized']:+.0f} | "
                    f"fills={m['fills']} unw={m['unwinds']}{sl_str}{dec_str}"
                )
                if pos_str != "flat":
                    print(f"  {'':18s} {pos_str}")

        # 24-hour summary
        print(f"\n{'#'*60}")
        print(f"  24-HOUR SIMULATION SUMMARY (seed={seed})")
        print(f"{'#'*60}")

        total_realized = 0.0
        total_unrealized = 0.0
        total_fills = 0
        total_unwinds = 0
        total_stop_losses = 0
        total_decay = 0
        total_reward_cycles = 0

        for mgr in self.managers:
            total_realized += mgr.stats["realized_pnl"]
            total_unrealized += mgr.unrealized_pnl()
            total_fills += mgr.stats["fills"]
            total_unwinds += mgr.stats["unwinds"]
            total_stop_losses += mgr.stats["stop_losses"]
            total_decay += mgr.stats["decay_sells"]
            total_reward_cycles += mgr.stats["cycles_with_orders"]

        utilization = total_reward_cycles / (total_cycles * len(self.managers)) * 100
        total_daily_rate = sum(m.daily_rate for m in self.markets)
        est_rewards = total_daily_rate * (utilization / 100)
        net_pnl = total_realized + total_unrealized + est_rewards

        print(f"\n  Starting balance:   ${self.starting_balance:,.0f}")
        print(f"  Free USDC now:      ${self.free_usdc:,.0f}")
        equity = self._total_equity()
        print(f"  Total equity:       ${equity:,.0f}")
        print(f"\n  Realized P&L:       ${total_realized:+,.0f}")
        print(f"  Unrealized P&L:     ${total_unrealized:+,.0f}")
        print(f"  Est. rewards:       ${est_rewards:+,.0f} "
              f"({utilization:.0f}% utilization × ${total_daily_rate:.0f}/day)")
        print(f"  ────────────────────────────")
        print(f"  NET P&L (incl rewards): ${net_pnl:+,.0f}")
        print(f"\n  Fills:              {total_fills}")
        print(f"  Unwinds:            {total_unwinds}")
        print(f"  Stop-losses:        {total_stop_losses}")
        print(f"  Decay adjustments:  {total_decay}")
        print(f"  Max drawdown:       ${self.max_drawdown:,.0f}")
        print(f"  Capital util:       {utilization:.1f}%")

        # Per-market breakdown
        print(f"\n  Per-Market Breakdown:")
        print(f"  {'Market':18s} {'Realized':>10s} {'Unrealized':>10s} "
              f"{'Fills':>6s} {'Unwinds':>8s} {'SL':>4s} {'Decay':>6s}")
        for mgr in self.managers:
            print(
                f"  {mgr.market.name:18s} "
                f"${mgr.stats['realized_pnl']:>+9.0f} "
                f"${mgr.unrealized_pnl():>+9.0f} "
                f"{mgr.stats['fills']:>6d} "
                f"{mgr.stats['unwinds']:>8d} "
                f"{mgr.stats['stop_losses']:>4d} "
                f"{mgr.stats['decay_sells']:>6d}"
            )

        # Accounting check
        total_locked = sum(m.locked_in_orders() for m in self.managers)
        total_inv_cost = sum(m.inventory_at_cost() for m in self.managers)
        print(f"\n  Accounting:")
        print(f"    Free USDC:        ${self.free_usdc:,.2f}")
        print(f"    Locked in BUYs:   ${total_locked:,.2f}")
        print(f"    Inventory (cost): ${total_inv_cost:,.2f}")
        conserved = self.free_usdc + total_locked + total_inv_cost
        expected_conserved = self.starting_balance + total_realized
        print(f"    Conserved total:  ${conserved:,.2f} "
              f"(expected ${expected_conserved:,.2f}, diff ${conserved - expected_conserved:,.2f})")

        # Inventory remaining
        print(f"\n  Open Inventory:")
        for mgr in self.managers:
            for side in ("yes", "no"):
                pos = mgr.get_pos(side)
                if pos.shares >= 1:
                    cost = pos.avg_price
                    if side == "yes":
                        value = mgr.market.best_bid
                    else:
                        value = max(0, 1 - mgr.market.best_ask)
                    pnl_pct = (value - cost) / cost * 100 if cost > 0 else 0
                    print(
                        f"    {mgr.market.name:18s} {side.upper():3s} "
                        f"{pos.shares:.0f}sh @{cost:.3f} "
                        f"now={value:.3f} ({pnl_pct:+.1f}%)"
                    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="24h bot simulation")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--balance", type=float, default=4000)
    args = parser.parse_args()

    sim = Simulator(starting_balance=args.balance, seed=args.seed)
    sim.run(hours=args.hours)
    sim.print_report(seed=args.seed)
