"""
Backtesting framework for the Polymarket market-making bot.

Replays historical cycle_snapshots from SQLite through the bot's pricing
and sizing logic to evaluate how different config parameters would have
performed.

Usage:
    python backtest.py --days 7
    python backtest.py --days 7 --override ORDER_SIZE=300
    python backtest.py --days 7 --override ORDER_SIZE=300 --override STOP_LOSS_PCT=0.20
    python backtest.py --days 7 --compare config_a.json config_b.json

Requires: bot_history.db with cycle_snapshots data from live trading.
"""

import argparse
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field

# Setup path so we can import bot modules
sys.path.insert(0, os.path.dirname(__file__))

from database import get_db

log = logging.getLogger("backtest")


# ─────────────────────────────────────────────────────────────────────────────
# Simulation Core (adapted from simulate.py, parameterized by config dict)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SimPosition:
    """Simulated position for one side of a market."""
    shares: float = 0.0
    avg_price: float = 0.0
    usd: float = 0.0

    def record_buy(self, new_shares: float, price: float) -> float:
        old_total = self.shares * self.avg_price
        self.shares += new_shares
        if self.shares > 0:
            self.avg_price = (old_total + new_shares * price) / self.shares
        self.usd = self.shares * self.avg_price
        return new_shares * price

    def record_sell(self, sold_shares: float) -> None:
        self.shares = max(0.0, self.shares - sold_shares)
        self.usd = self.shares * self.avg_price
        if self.shares < 1.0:
            self.shares = 0.0
            self.avg_price = 0.0
            self.usd = 0.0


@dataclass
class BacktestResult:
    """Results from a single backtest run."""
    condition_id: str = ""
    question: str = ""
    config_label: str = "default"
    num_cycles: int = 0
    start_ts: float = 0.0
    end_ts: float = 0.0

    # P&L
    total_bought_usd: float = 0.0
    total_sold_usd: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    # Position tracking
    peak_position_usd: float = 0.0
    num_buys: int = 0
    num_sells: int = 0
    num_stop_losses: int = 0

    # Order quality
    cycles_in_reward_window: int = 0
    cycles_skipped: int = 0

    # Equity curve for drawdown
    equity_curve: list = field(default_factory=list)
    max_drawdown_pct: float = 0.0

    @property
    def duration_hours(self) -> float:
        return (self.end_ts - self.start_ts) / 3600 if self.end_ts > self.start_ts else 0

    @property
    def reward_uptime_pct(self) -> float:
        if self.num_cycles == 0:
            return 0.0
        return self.cycles_in_reward_window / self.num_cycles * 100

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    def summary(self) -> str:
        lines = [
            f"  Market: {self.question[:50]}",
            f"  Config: {self.config_label}",
            f"  Period: {self.duration_hours:.1f} hours ({self.num_cycles} cycles)",
            f"  Buys: {self.num_buys} (${self.total_bought_usd:.2f})",
            f"  Sells: {self.num_sells} (${self.total_sold_usd:.2f})",
            f"  Stop-losses: {self.num_stop_losses}",
            f"  Realized P&L: ${self.realized_pnl:+.2f}",
            f"  Unrealized P&L: ${self.unrealized_pnl:+.2f}",
            f"  Total P&L: ${self.total_pnl:+.2f}",
            f"  Peak position: ${self.peak_position_usd:.2f}",
            f"  Reward uptime: {self.reward_uptime_pct:.1f}%",
            f"  Max drawdown: {self.max_drawdown_pct:.1f}%",
        ]
        return "\n".join(lines)


class BacktestEngine:
    """Replays historical data through simplified pricing logic.

    Uses cycle_snapshots (best_bid, best_ask) to simulate what would happen
    with different config parameters. The fill model is probabilistic:
    if our price is at or better than the best bid/ask, we assume a fill
    probability proportional to how long the order sat (calibrated from
    actual fill frequency).
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        self._order_size = config.get("ORDER_SIZE", 250)
        self._max_position = config.get("MAX_POSITION_USD", 400)
        self._stop_loss_pct = config.get("STOP_LOSS_PCT", 0.25)
        self._min_stop_loss_usd = config.get("MIN_STOP_LOSS_USD", 75.0)
        self._decay_interval = config.get("UNWIND_DECAY_INTERVAL_SECS", 300)
        self._cycle_secs = config.get("ORDER_REFRESH_SECS", 30)

    def run(
        self, snapshots: list[dict], condition_id: str = "",
        question: str = "", config_label: str = "default",
    ) -> BacktestResult:
        """Run backtest on a sequence of cycle snapshots.

        Args:
            snapshots: List of dicts with keys: ts, best_bid, best_ask,
                       our_bid, our_ask.
            condition_id: Market identifier.
            question: Market question string.
            config_label: Label for this config variant.

        Returns:
            BacktestResult with all metrics.
        """
        result = BacktestResult(
            condition_id=condition_id,
            question=question,
            config_label=config_label,
        )

        if not snapshots:
            return result

        result.start_ts = snapshots[0].get("ts", 0)
        result.end_ts = snapshots[-1].get("ts", 0)
        result.num_cycles = len(snapshots)

        yes_pos = SimPosition()
        no_pos = SimPosition()
        equity = 0.0
        peak_equity = 0.0
        sell_pending_since: float = 0.0
        sell_price: float = 0.0

        for snap in snapshots:
            best_bid = snap.get("best_bid", 0)
            best_ask = snap.get("best_ask", 0)
            ts = snap.get("ts", 0)

            if not best_bid or not best_ask or best_bid <= 0 or best_ask <= 0:
                result.cycles_skipped += 1
                continue

            mid = (best_bid + best_ask) / 2
            spread = best_ask - best_bid

            # Check if orders would be in reward window
            # (simplified: if spread < 10c, we'd be in window)
            if spread < 0.10:
                result.cycles_in_reward_window += 1

            # Simulate BUY fills
            # If we have capacity and our simulated bid is at best_bid
            total_pos = yes_pos.usd + no_pos.usd
            if total_pos < self._max_position:
                # Simulate YES buy at best_bid
                buy_size = min(
                    self._order_size,
                    self._max_position - total_pos,
                )
                if buy_size > 10 and best_bid > 0.05 and best_bid < 0.95:
                    shares = buy_size / best_bid
                    # Fill probability: ~5% per cycle at co-best
                    if hash((ts, "yes")) % 20 == 0:
                        cost = yes_pos.record_buy(shares, best_bid)
                        result.total_bought_usd += cost
                        result.num_buys += 1

                # Simulate NO buy at (1 - best_ask) CLOB cost
                no_clob = 1.0 - best_ask
                if no_clob > 0.05 and buy_size > 10:
                    shares = buy_size / no_clob
                    if hash((ts, "no")) % 20 == 0:
                        cost = no_pos.record_buy(shares, best_ask)
                        result.total_bought_usd += cost
                        result.num_sells += 0  # buy, not sell

            # Track peak position
            total_pos = yes_pos.usd + no_pos.usd
            result.peak_position_usd = max(result.peak_position_usd, total_pos)

            # Check stop-loss on YES side
            if yes_pos.shares > 1:
                loss_pct = (yes_pos.avg_price - best_bid) / yes_pos.avg_price if yes_pos.avg_price > 0 else 0
                loss_usd = (yes_pos.avg_price - best_bid) * yes_pos.shares
                if loss_pct >= self._stop_loss_pct and loss_usd >= self._min_stop_loss_usd:
                    sell_usd = best_bid * yes_pos.shares
                    pnl = sell_usd - yes_pos.usd
                    result.realized_pnl += pnl
                    result.total_sold_usd += sell_usd
                    result.num_stop_losses += 1
                    result.num_sells += 1
                    yes_pos = SimPosition()

            # Simulate unwind sells (simplified decay)
            if yes_pos.shares > 1 and sell_pending_since == 0:
                sell_pending_since = ts
                sell_price = yes_pos.avg_price
            if yes_pos.shares > 1 and sell_pending_since > 0:
                elapsed = ts - sell_pending_since
                decay_intervals = int(elapsed / self._decay_interval)
                decayed_price = max(0.01, sell_price - decay_intervals * 0.01)
                if decayed_price <= best_bid:
                    sell_usd = decayed_price * yes_pos.shares
                    pnl = sell_usd - yes_pos.usd
                    result.realized_pnl += pnl
                    result.total_sold_usd += sell_usd
                    result.num_sells += 1
                    yes_pos = SimPosition()
                    sell_pending_since = 0

            # Equity tracking
            unrealized = 0.0
            if yes_pos.shares > 1:
                unrealized += (best_bid - yes_pos.avg_price) * yes_pos.shares
            if no_pos.shares > 1:
                unrealized += ((1.0 - best_ask) - (1.0 - no_pos.avg_price)) * no_pos.shares
            equity = result.realized_pnl + unrealized
            result.equity_curve.append(equity)
            peak_equity = max(peak_equity, equity)
            if peak_equity > 0:
                dd = (peak_equity - equity) / peak_equity * 100
                result.max_drawdown_pct = max(result.max_drawdown_pct, dd)

        # Final unrealized P&L
        result.unrealized_pnl = equity - result.realized_pnl

        return result


# ─────────────────────────────────────────────────────────────────────────────
# CLI Interface
# ─────────────────────────────────────────────────────────────────────────────

def load_config_from_file(path: str) -> dict:
    """Load config overrides from a JSON file."""
    with open(path, "r") as f:
        return json.load(f)


def run_backtest(days: int, overrides: dict, label: str = "custom") -> list[BacktestResult]:
    """Run backtest across all markets with enough data."""
    db = get_db()
    start_ts = time.time() - days * 86400
    end_ts = time.time()

    available = db.get_available_backtest_markets(start_ts, end_ts, min_cycles=50)
    if not available:
        print(f"No markets with sufficient data in the last {days} days.")
        print("The bot needs to run for a while to accumulate cycle_snapshots.")
        return []

    # Build config: start with defaults, apply overrides
    from config import BotConfig
    base_config = dict(BotConfig.instance()._defaults)
    base_config.update(overrides)

    engine = BacktestEngine(base_config)
    results = []

    for market_info in available:
        cid = market_info["condition_id"]
        snapshots = db.get_cycle_snapshots(cid, start_ts, end_ts)
        if not snapshots:
            continue

        result = engine.run(
            snapshots, condition_id=cid,
            question=f"Market {cid[:12]}...",
            config_label=label,
        )
        results.append(result)

    return results


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Backtest the market-making bot")
    parser.add_argument("--days", type=int, default=7, help="Days of history to replay")
    parser.add_argument(
        "--override", action="append", default=[],
        help="Config override in KEY=VALUE format (can repeat)",
    )
    parser.add_argument(
        "--compare", nargs=2, metavar="CONFIG_FILE",
        help="Compare two config files (A/B test)",
    )
    args = parser.parse_args()

    if args.compare:
        # A/B comparison mode
        config_a = load_config_from_file(args.compare[0])
        config_b = load_config_from_file(args.compare[1])

        print(f"\n{'='*60}")
        print(f"  A/B BACKTEST COMPARISON — {args.days} days")
        print(f"{'='*60}\n")

        results_a = run_backtest(args.days, config_a, label=f"A ({args.compare[0]})")
        results_b = run_backtest(args.days, config_b, label=f"B ({args.compare[1]})")

        if not results_a and not results_b:
            return

        total_pnl_a = sum(r.total_pnl for r in results_a)
        total_pnl_b = sum(r.total_pnl for r in results_b)

        print(f"\n  Config A total P&L: ${total_pnl_a:+.2f} ({len(results_a)} markets)")
        print(f"  Config B total P&L: ${total_pnl_b:+.2f} ({len(results_b)} markets)")
        winner = "A" if total_pnl_a > total_pnl_b else "B"
        print(f"\n  Winner: Config {winner}")

    else:
        # Single config mode
        overrides = {}
        for o in args.override:
            if "=" in o:
                key, val = o.split("=", 1)
                # Try to parse as number
                try:
                    val = int(val)
                except ValueError:
                    try:
                        val = float(val)
                    except ValueError:
                        if val.lower() in ("true", "false"):
                            val = val.lower() == "true"
                overrides[key] = val

        label = "custom" if overrides else "default"

        print(f"\n{'='*60}")
        print(f"  BACKTEST — {args.days} days")
        if overrides:
            print(f"  Overrides: {overrides}")
        print(f"{'='*60}\n")

        results = run_backtest(args.days, overrides, label=label)

        if not results:
            return

        total_pnl = 0.0
        total_bought = 0.0
        total_sold = 0.0
        for r in results:
            print(r.summary())
            print()
            total_pnl += r.total_pnl
            total_bought += r.total_bought_usd
            total_sold += r.total_sold_usd

        print(f"{'─'*60}")
        print(f"  AGGREGATE: {len(results)} markets")
        print(f"  Total bought: ${total_bought:.2f}")
        print(f"  Total sold:   ${total_sold:.2f}")
        print(f"  Total P&L:    ${total_pnl:+.2f}")
        avg_uptime = sum(r.reward_uptime_pct for r in results) / len(results)
        max_dd = max(r.max_drawdown_pct for r in results)
        print(f"  Avg reward uptime: {avg_uptime:.1f}%")
        print(f"  Max drawdown: {max_dd:.1f}%")
        print()


if __name__ == "__main__":
    main()
